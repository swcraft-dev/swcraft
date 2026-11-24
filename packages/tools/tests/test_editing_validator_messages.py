"""No validator message names where the model is stored: the message gate over a corpus of broken models."""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import test_data_description_validator as data_description_suite
import test_validator_application_types as application_types_suite
import test_validator_compositions as compositions_suite
import test_validator_data_types as data_types_suite
import test_validator_rule_coverage as rule_coverage_suite
import validator_package
import yaml
from conftest import BASE_VECTOR, model_element_storage_name, write_model_element
from fixture_scope_support import classes_consuming
from swcraft_tools import validator
from swcraft_tools.diagnostics import MEMBER
from swcraft_tools.editing.address import AddressError
from swcraft_tools.editing.diagnosis import subject_address
from swcraft_tools.validator import CONSISTENCY_RULES, ConsistencyResult, check_cross_layer_consistency
from swcraft_tools.validator.model_view import ModelInput, as_view
from swcraft_tools.validator.results import consistency_result

#: The directory every message-gate trigger builds its model under: no model string holds it, so any
#: fragment of it in a message names a location.
MESSAGE_PROBE_DIRNAME = "no-validator-message-may-name-this-directory"

#: A drive-letter absolute path: the absolute spelling a Windows host produces.
_DRIVE_LETTER_PATH = re.compile(r"\b[A-Za-z]:[\\/]")


def _put(model_root: Path, relative: str, document: dict[str, Any]) -> None:
    path = model_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _change(model_root: Path, relative: str, mutate: Callable[[dict[str, Any]], object]) -> None:
    document = yaml.safe_load((model_root / relative).read_text(encoding="utf-8"))
    mutate(document)
    _put(model_root, relative, document)


_PROVIDER = "swc/example/ExampleSWC.yaml"
_ROOT = "compositions/Root.yaml"


def _probe_requirement(requirement_id: str) -> dict[str, Any]:
    """An architecture requirement no element satisfies."""
    return {
        "id": requirement_id,
        "title": "Probe",
        "description": "A requirement the message gate writes.",
        "version": 1,
        "source": "architecture",
    }


_TASK = "tasks/Task_10ms.yaml"
_SEQUENCE = "sequences/task10ms_consumer_cycle.yaml"


def _an_instance_ending_on_a_composition(model_root: Path, relative: str, field_name: str) -> None:
    """An ``instance`` entry whose path ends on a member instantiating a composition."""
    _put(
        model_root,
        "compositions/ProbeInner.yaml",
        {"name": "ProbeInner", "title": "Probe", "members": [{"name": "inner", "type": "ExampleSWC"}]},
    )
    _change(model_root, _ROOT, lambda d: d["members"].append({"name": "probe", "composition": "ProbeInner"}))
    _change(model_root, relative, lambda d: d[field_name].append({"instance": "probe", "runnable": "ExampleRun"}))


_CONSUMER = "swc/example/ExampleConsumerSWC.yaml"
_SR_INTERFACE = "interfaces/IExample.yaml"
_CS_INTERFACE = "interfaces/IDiagnosticService.yaml"
_TASK = "tasks/Task_10ms.yaml"
_SEQUENCE = "sequences/task10ms_consumer_cycle.yaml"
_REQUIREMENT = "requirements/REQ_EXAMPLE_001.yaml"
_CONNECTOR = "connectors/Example_to_Consumer.yaml"


def _operation_event(port: str, operation: str = "ReadDtc") -> dict[str, str]:
    return {"type": "operation", "port": port, "operation": operation}


def _data_event(port: str, element: str = "value") -> dict[str, str]:
    """A data-received event through *port* on *element*."""
    return {"type": "data", "port": port, "element": element}


def _async_calls(model_root: Path, *runnables: tuple[list[dict[str, Any]], list[dict[str, Any]]]) -> None:
    """The consumer gains a ``rDiag`` client port and one runnable ``AsyncN`` per ``(events, calls)``.

    The reference model's own runnable is untouched, so no other rule moves.
    """

    def mutate(document: dict[str, Any]) -> None:
        document["ports"].append({"name": "rDiag", "direction": "require", "interface": "IDiagnosticService"})
        for index, (events, calls) in enumerate(runnables):
            document["runnables"].append(
                {
                    "name": f"Async{index}",
                    "events": events or [{"type": "timing", "period": 0.1}],
                    "calls": [{"port": "rDiag", "operation": "ReadDtc", **call} for call in calls],
                }
            )

    _change(model_root, _CONSUMER, mutate)


def _returned_diag() -> dict[str, str]:
    """A call-returned event of ``rDiag.ReadDtc``."""
    return {"type": "call_returned", "port": "rDiag", "operation": "ReadDtc"}


def _serve(model_root: Path, events: list[dict[str, str]], provide_diagnostics: bool = False) -> None:
    """``ExampleSWC`` gains a runnable ``Serve`` started by *events* -- and a ``pDiag`` provide port when asked."""

    def mutate(document: dict[str, Any]) -> None:
        if provide_diagnostics:
            document["ports"].append({"name": "pDiag", "direction": "provide", "interface": "IDiagnosticService"})
        document["runnables"].append({"name": "Serve", "events": events})

    _change(model_root, _PROVIDER, mutate)


def _typed_element(model_root: Path, type_name: str, init: Any, *, application: bool = False) -> None:
    """Give ``IExample.value`` another type and an ``init``; *application* goes into ``application_type``."""

    def retype(document: dict[str, Any]) -> None:
        element = document["data_elements"][0]
        if application:
            document["data_elements"][0] = {
                ("application_type" if key == "implementation_type" else key): value for key, value in element.items()
            }
            element = document["data_elements"][0]
            element["application_type"] = type_name
        else:
            element["implementation_type"] = type_name
        element["init"] = init

    _change(model_root, _SR_INTERFACE, retype)


#: The gate's own triggers for the three rules whose message could name a location on disk.
MESSAGE_PATH_BREAKS: dict[str, Callable[[Path], None]] = {
    "data-type.array-undeclared-element-type": lambda root: _put(
        root,
        model_element_storage_name("ProbeArray", kind="array"),
        {"name": "ProbeArray", "kind": "array", "element_type": "Nope", "length": 2},
    ),
    "identifier.declared": lambda root: _put(
        root,
        model_element_storage_name("Probe Type", kind="value_type"),
        {"name": "Probe Type", "kind": "value_type", "base": "float32_base"},
    ),
    # An ENUM: `{x}_CompuMethod` is the one data-type form a declared name still lengthens.
    "identifier.generated": lambda root: _put(
        root,
        model_element_storage_name("LongName", kind="enum"),
        {"name": "V" * 124, "kind": "enum", "literals": [{"name": "P", "value": 0}]},
    ),
}

#: One compact break of the reference model per rule no shared table reaches. A rule counts as
#: reached when ANY trigger mints it, so a key here is intent, not the measurement.
MESSAGE_GATE_BREAKS: dict[str, Callable[[Path], None]] = {
    # The implementation composites' ten cross-element rules, each over data types of its own so no
    # other rule moves, except the last, which retypes the one connected data element.
    "data-type.implementation-element-undeclared-type": lambda root: _put(
        root,
        model_element_storage_name("ProbeFrame", kind="implementation_record"),
        {"name": "ProbeFrame", "kind": "implementation_record", "elements": [{"name": "a", "type": "Nope"}]},
    ),
    # A base type is no `implementation_types` element, so each `*-not-implementation-type` trigger
    # names an application type of its own as the refused kind.
    "data-type.implementation-element-not-implementation-type": lambda root: (
        _put(
            root,
            model_element_storage_name("ProbeAppSpeed", kind="application_type"),
            {"name": "ProbeAppSpeed", "kind": "application_type"},
        ),
        _put(
            root,
            model_element_storage_name("ProbeFrame", kind="implementation_record"),
            {
                "name": "ProbeFrame",
                "kind": "implementation_record",
                "elements": [{"name": "a", "type": "ProbeAppSpeed"}],
            },
        ),
    ),
    "data-type.implementation-record-duplicate-element-name": lambda root: _put(
        root,
        model_element_storage_name("ProbeFrame", kind="implementation_record"),
        {
            "name": "ProbeFrame",
            "kind": "implementation_record",
            "elements": [{"name": "a", "type": "uint8"}, {"name": "a", "type": "uint8"}],
        },
    ),
    "data-type.implementation-array-undeclared-element-type": lambda root: _put(
        root,
        model_element_storage_name("ProbeSamples", kind="implementation_array"),
        {
            "name": "ProbeSamples",
            "kind": "implementation_array",
            "element_type": "Nope",
            "length": 2,
            "element_name": "E",
        },
    ),
    "data-type.implementation-array-element-not-implementation-type": lambda root: (
        _put(
            root,
            model_element_storage_name("ProbeAppSpeed", kind="application_type"),
            {"name": "ProbeAppSpeed", "kind": "application_type"},
        ),
        _put(
            root,
            model_element_storage_name("ProbeSamples", kind="implementation_array"),
            {
                "name": "ProbeSamples",
                "kind": "implementation_array",
                "element_type": "ProbeAppSpeed",
                "length": 2,
                "element_name": "E",
            },
        ),
    ),
    "data-type.pointer-undeclared-target": lambda root: _put(
        root,
        model_element_storage_name("ProbePtr", kind="pointer"),
        {"name": "ProbePtr", "kind": "pointer", "target": "Nope"},
    ),
    "data-type.pointer-target-not-implementation-type": lambda root: (
        _put(
            root,
            model_element_storage_name("ProbeAppSpeed", kind="application_type"),
            {"name": "ProbeAppSpeed", "kind": "application_type"},
        ),
        _put(
            root,
            model_element_storage_name("ProbePtr", kind="pointer"),
            {"name": "ProbePtr", "kind": "pointer", "target": "ProbeAppSpeed"},
        ),
    ),
    "mapping-set.incompatible-composite-map": lambda root: (
        _put(
            root,
            model_element_storage_name("ProbeSpeed", kind="application_type"),
            {"name": "ProbeSpeed", "kind": "application_type"},
        ),
        _put(
            root,
            model_element_storage_name("ProbePose", kind="record"),
            {"name": "ProbePose", "kind": "record", "fields": [{"name": "x", "type": "ProbeSpeed"}]},
        ),
        _put(
            root,
            model_element_storage_name("ProbeMaps", layer="mapping_sets"),
            {"name": "ProbeMaps", "maps": [{"application": "ProbePose", "implementation": "uint8"}]},
        ),
    ),
    "mapping-set.pointer-mapped": lambda root: (
        _put(
            root,
            model_element_storage_name("ProbeSpeed", kind="application_type"),
            {"name": "ProbeSpeed", "kind": "application_type"},
        ),
        _put(
            root,
            model_element_storage_name("ProbePtr", kind="pointer"),
            {"name": "ProbePtr", "kind": "pointer", "target": "uint8"},
        ),
        _put(
            root,
            model_element_storage_name("ProbeMaps", layer="mapping_sets"),
            {"name": "ProbeMaps", "maps": [{"application": "ProbeSpeed", "implementation": "ProbePtr"}]},
        ),
    ),
    "connector.pointer-between-application-components": lambda root: (
        _put(
            root,
            model_element_storage_name("ProbePtr", kind="pointer"),
            {"name": "ProbePtr", "kind": "pointer", "target": "uint8"},
        ),
        _change(root, _SR_INTERFACE, lambda d: d["data_elements"][0].update(implementation_type="ProbePtr")),
    ),
    "connector.duplicate-name": lambda root: _change(
        root, _CONNECTOR, lambda d: _put(root, "connectors/Again.yaml", d)
    ),
    "connector.duplicate-link": lambda root: _change(
        root, _CONNECTOR, lambda d: _put(root, "connectors/Second.yaml", {**d, "name": "Second"})
    ),
    "connector.endpoint-unknown-swc": lambda root: _change(root, _CONNECTOR, lambda d: d["to"].update(swc="Nope")),
    # The link lives in the element: a member naming no declared requirement.
    "trace.link-unknown-requirement": lambda root: _change(
        root, _PROVIDER, lambda d: d.setdefault("satisfies", []).append("NOPE@1")
    ),
    # The provider's own list names the reference model's requirement a second time.
    "trace.duplicate-link": lambda root: _change(
        root, _PROVIDER, lambda d: d.setdefault("satisfies", []).append(d["satisfies"][0])
    ),
    # A link to an id holding `.`, and an architecture requirement with a blank description.
    "trace.link-unpublishable-id": lambda root: (
        _put(root, "requirements/SYS.REQ.12.yaml", _probe_requirement("SYS.REQ.12")),
        _change(root, _PROVIDER, lambda d: d.setdefault("satisfies", []).append("SYS.REQ.12@1")),
    ),
    "trace.requirement-blank-text": lambda root: _put(
        root, "requirements/REQ_BLANK.yaml", {**_probe_requirement("REQ_BLANK"), "description": ""}
    ),
    # The traceability rules, each on the reference model's requirement or one of its own.
    "trace.requirement-unallocated": lambda root: _put(
        root, "requirements/REQ_PROBE.yaml", _probe_requirement("REQ_PROBE")
    ),
    # A stub is a requirement whose `stub` is true; the composer over `model_root` reaches it.
    "trace.requirement-stub": lambda root: _put(
        root, "requirements/SG_PROBE.yaml", {**_probe_requirement("SG_PROBE"), "source": "external", "stub": True}
    ),
    "trace.link-version-not-stated": lambda root: _change(
        root, _PROVIDER, lambda d: d.update(satisfies=["REQ_EXAMPLE_001"])
    ),
    "trace.link-needs-confirmation": lambda root: _change(root, _REQUIREMENT, lambda d: d.update(version=2)),
    "trace.derived-from-unknown-target": lambda root: _change(
        root, _REQUIREMENT, lambda d: d.update(derived_from=["NOPE@1", "hardware:NoCore"])
    ),
    "trace.derived-from-cycle": lambda root: _change(
        root, _REQUIREMENT, lambda d: d.update(derived_from=["REQ_EXAMPLE_001@1"])
    ),
    "trace.decomposition-context-mismatch": lambda root: (
        _put(root, "requirements/REQ_PARENT.yaml", {**_probe_requirement("REQ_PARENT"), "asil": "C"}),
        _put(
            root,
            "requirements/REQ_CHILD.yaml",
            {**_probe_requirement("REQ_CHILD"), "asil": "B(D)", "derived_from": ["REQ_PARENT@1"]},
        ),
    ),
    "data-type.base-type-native-declaration-missing": lambda root: _put(
        root,
        model_element_storage_name("raw_base", kind="base_type"),
        {"name": "raw_base", "kind": "base_type", "size": 8, "encoding": "NONE"},
    ),
    # The occupant is an application type of the enum's own package: a value type of the form's name
    # is legal.
    "data-type.compu-method-name-collision": lambda root: (
        _put(
            root,
            model_element_storage_name("Gear", kind="enum"),
            {"name": "Gear", "kind": "enum", "literals": [{"name": "P", "value": 0}]},
        ),
        _put(
            root,
            model_element_storage_name("Gear_CompuMethod", kind="application_type"),
            {"name": "Gear_CompuMethod", "kind": "application_type"},
        ),
    ),
    "data-type.duplicate-name": lambda root: _change(
        root,
        "data_types/implementation/float32.yaml",
        lambda d: _put(root, model_element_storage_name("float32_again", kind=d["kind"]), d),
    ),
    "data-type.enum-duplicate-literal-name": lambda root: _put(
        root,
        model_element_storage_name("Gear", kind="enum"),
        {"name": "Gear", "kind": "enum", "literals": [{"name": "P", "value": 0}, {"name": "P", "value": 1}]},
    ),
    "data-type.enum-duplicate-literal-value": lambda root: _put(
        root,
        model_element_storage_name("Gear", kind="enum"),
        {"name": "Gear", "kind": "enum", "literals": [{"name": "P", "value": 0}, {"name": "R", "value": 0}]},
    ),
    # The three stub rules: the first two quote an AUTOSAR reference path, no storage location.
    "data-type.external-incomplete-reference": lambda root: _put(
        root,
        model_element_storage_name("ForeignT", kind="external_type"),
        {"name": "ForeignT", "kind": "external_type", "external_ref": "not a path", "external_dest": "SW-BASE-TYPE"},
    ),
    "data-type.record-duplicate-field-name": lambda root: _put(
        root,
        model_element_storage_name("Rec", kind="record"),
        {"name": "Rec", "kind": "record", "fields": [{"name": "a", "type": "float32"}, {"name": "a", "type": "uint8"}]},
    ),
    "data-type.record-field-undeclared-type": lambda root: _put(
        root,
        model_element_storage_name("Rec", kind="record"),
        {"name": "Rec", "kind": "record", "fields": [{"name": "a", "type": "Nope"}]},
    ),
    "hardware.accessible-not-a-core": lambda root: _change(
        root, "hardware/RAM0.yaml", lambda d: d["accessible"].append("CPU")
    ),
    "hardware.accessible-undeclared-core": lambda root: _change(
        root, "hardware/RAM0.yaml", lambda d: d["accessible"].append("CORE9")
    ),
    "hardware.layer-absent": lambda root: shutil.rmtree(root / "hardware"),
    "hardware.memory-not-a-memory": lambda root: _change(root, _PROVIDER, lambda d: d["memory"].update(CORE0=1)),
    "hardware.missing-budget": lambda root: _change(root, _TASK, lambda d: d.pop("wcet")),
    "hardware.multiple-processors": lambda root: _put(
        root, "hardware/CPU2.yaml", {"name": "CPU2", "kind": "processor"}
    ),
    "hardware.no-processor": lambda root: (root / "hardware" / "CPU.yaml").unlink(),
    "hardware.rate-monotonic-violation": lambda root: _put(
        root,
        "tasks/Task_20ms.yaml",
        # A higher number is more urgent: the longer period outranking the shorter is the inversion.
        {"name": "Task_20ms", "period": 0.02, "priority": 9, "partition": "PARTITION0", "wcet": "10us"},
    ),
    # The four reference rules of a task's `partition`, then the partition's `core`.
    "hardware.undeclared-partition": lambda root: _change(root, _TASK, lambda d: d.update(partition="PARTITION9")),
    "hardware.partition-not-a-partition": lambda root: _change(root, _TASK, lambda d: d.update(partition="RAM0")),
    "hardware.partition-undeclared-core": lambda root: _change(
        root, "hardware/PARTITION0.yaml", lambda d: d.update(core="CORE9")
    ),
    "hardware.partition-core-not-a-core": lambda root: _change(
        root, "hardware/PARTITION0.yaml", lambda d: d.update(core="RAM0")
    ),
    "hardware.undeclared-memory": lambda root: _change(root, _PROVIDER, lambda d: d["memory"].update(RAM9=1)),
    "hardware.unreachable-memory": lambda root: (
        _put(root, "hardware/CORE1.yaml", {"name": "CORE1", "kind": "core", "threshold": 0.7}),
        _put(root, "hardware/PARTITION1.yaml", {"name": "PARTITION1", "kind": "partition", "core": "CORE1"}),
        _change(root, _TASK, lambda d: d.update(partition="PARTITION1")),
    ),
    "interface.argument-undeclared-type": lambda root: _change(
        root, _CS_INTERFACE, lambda d: d["operations"][0]["arguments"][0].update(implementation_type="Nope")
    ),
    "interface.data-element-undeclared-type": lambda root: _change(
        root, _SR_INTERFACE, lambda d: d["data_elements"][0].update(implementation_type="Nope")
    ),
    "interface.external-incomplete-reference": lambda root: _put(
        root,
        "interfaces/IForeign.yaml",
        {
            "name": "IForeign",
            "kind": "external_interface",
            "external_ref": "/Foreign/PortInterfaces/IForeign",
            "external_dest": "NOT-A-PORT-INTERFACE",
        },
    ),
    "interface.init-array-length-mismatch": lambda root: (
        _put(
            root,
            model_element_storage_name("Pair", kind="array"),
            {"name": "Pair", "kind": "array", "element_type": "float32", "length": 2},
        ),
        _typed_element(root, "Pair", [1.0], application=True),
    ),
    "interface.init-constant-name-collision": lambda root: _put(
        root,
        "interfaces/IExample_value_Init.yaml",
        {
            "name": "IExample_value_Init",
            "kind": "sender_receiver",
            "data_elements": [{"name": "v", "implementation_type": "float32"}],
        },
    ),
    "interface.init-on-external-type": lambda root: (
        _put(
            root,
            model_element_storage_name("ForeignT", kind="external_type"),
            {
                "name": "ForeignT",
                "kind": "external_type",
                "external_ref": "/Foreign/ImplementationDataTypes/ForeignT",
                "external_dest": "IMPLEMENTATION-DATA-TYPE",
            },
        ),
        _typed_element(root, "ForeignT", 1),
    ),
    "interface.init-out-of-range": lambda root: (
        _put(
            root,
            model_element_storage_name("Percent", kind="value_type"),
            {"name": "Percent", "kind": "value_type", "base": "float32_base", "value_min": 0, "value_max": 100},
        ),
        _typed_element(root, "Percent", 200),
    ),
    "interface.init-record-field-mismatch": lambda root: (
        _put(
            root,
            model_element_storage_name("Rec", kind="record"),
            {"name": "Rec", "kind": "record", "fields": [{"name": "a", "type": "float32"}]},
        ),
        _typed_element(root, "Rec", {"b": 1.0}, application=True),
    ),
    "interface.init-shape-mismatch": lambda root: _typed_element(root, "float32", [1.0, 2.0]),
    "interface.init-string-scalar": lambda root: _typed_element(root, "float32", "fast"),
    "interface.init-unknown-enum-literal": lambda root: (
        _put(
            root,
            model_element_storage_name("Gear", kind="enum"),
            {"name": "Gear", "kind": "enum", "literals": [{"name": "P", "value": 0}]},
        ),
        _typed_element(root, "Gear", "X", application=True),
    ),
    "name.duplicate-argument": lambda root: _change(
        root, _CS_INTERFACE, lambda d: d["operations"][0]["arguments"][1].update(name="dtc_id")
    ),
    "name.duplicate-data-element": lambda root: _change(
        root, _SR_INTERFACE, lambda d: d["data_elements"].append({"name": "value", "implementation_type": "uint8"})
    ),
    "name.duplicate-interface": lambda root: _change(
        root, _SR_INTERFACE, lambda d: _put(root, "interfaces/IExample_again.yaml", d)
    ),
    "name.duplicate-operation": lambda root: _change(
        root, _CS_INTERFACE, lambda d: d["operations"][1].update(name="ReadDtc")
    ),
    "name.duplicate-runnable": lambda root: _change(
        root, _PROVIDER, lambda d: d["runnables"].append({"name": "ExampleRun"})
    ),
    # `ExampleRun`'s timing event is named `TMT_ExampleRun`; the runnable below takes that SHORT-NAME.
    "name.runnable-event-collision": lambda root: _change(
        root, _PROVIDER, lambda d: d["runnables"].append({"name": "TMT_ExampleRun"})
    ),
    # `ExampleRun`'s timing event states the name `Tick`, and a second runnable's init event too.
    "name.duplicate-event": lambda root: _change(
        root,
        _PROVIDER,
        lambda d: (
            d["runnables"][0]["events"][0].update(name="Tick"),
            d["runnables"].append({"name": "Other", "events": [{"type": "init", "name": "Tick"}]}),
        ),
    ),
    "name.duplicate-swc": lambda root: _change(root, _PROVIDER, lambda d: _put(root, "swc/other/ExampleSWC.yaml", d)),
    # A requirement id is a name two files may not declare.
    "name.duplicate-requirement": lambda root: _change(
        root, _REQUIREMENT, lambda d: _put(root, "requirements/REQ_EXAMPLE_001~1.yaml", d)
    ),
    "name.swc-implementation-collision": lambda root: _put(
        root,
        "swc/example/ExampleSWC_Implementation.yaml",
        {"name": "ExampleSWC_Implementation", "type": "application"},
    ),
    # `ExampleRun` declares a budget, so the writer generates `ExampleSWC_Timing`; a component and a
    # composition of that name take it.
    "name.swc-timing-collision": lambda root: (
        _change(root, _PROVIDER, lambda d: d["runnables"][0].update(wcet="1us", wcet_type="gross")),
        _put(root, "swc/example/ExampleSWC_Timing.yaml", {"name": "ExampleSWC_Timing", "type": "application"}),
    ),
    "name.composition-timing-collision": lambda root: (
        _change(root, _PROVIDER, lambda d: d["runnables"][0].update(wcet="1us", wcet_type="gross")),
        _put(root, "compositions/ExampleSWC_Timing.yaml", {"name": "ExampleSWC_Timing", "title": "Timing"}),
    ),
    "port.undeclared-interface": lambda root: _change(
        root, _PROVIDER, lambda d: d["ports"][0].update(interface="Nope")
    ),
    "safety.component-below-requirements": lambda root: (
        _change(root, _REQUIREMENT, lambda d: d.update(asil="D")),
        _change(root, _PROVIDER, lambda d: d.update(asil="A")),
    ),
    "safety.consumer-above-element": lambda root: (
        _change(root, _SR_INTERFACE, lambda d: d["data_elements"][0].update(asil="A")),
        _change(root, _PROVIDER, lambda d: d.update(asil="A")),
        _change(root, _CONSUMER, lambda d: d.update(asil="D")),
    ),
    "safety.measure-on-provide-port": lambda root: _change(
        root, _PROVIDER, lambda d: d["ports"][0].update(measures={"value": "end-to-end protection"})
    ),
    "safety.measure-unknown-element": lambda root: _change(
        root, _CONSUMER, lambda d: d["ports"][0].update(measures={"nope": "end-to-end protection"})
    ),
    # A blank text under a key the consumer's interface declares, so no other measure rule moves.
    "safety.measure-blank": lambda root: _change(
        root, _CONSUMER, lambda d: d["ports"][0].update(measures={"value": " "})
    ),
    "safety.memory-below-component": lambda root: (
        _change(root, "hardware/RAM0.yaml", lambda d: d.update(asil_capability="QM")),
        _change(root, _PROVIDER, lambda d: d.update(asil="B")),
    ),
    "safety.provider-below-element": lambda root: (
        _change(root, _SR_INTERFACE, lambda d: d["data_elements"][0].update(asil="D")),
        _change(root, _PROVIDER, lambda d: d.update(asil="A")),
    ),
    "safety.task-mixed-asil": lambda root: (
        _change(root, _TASK, lambda d: d["runnables"].append({"swc": "ExampleSWC", "runnable": "ExampleRun"})),
        _change(root, _PROVIDER, lambda d: d.update(asil="D")),
    ),
    # A rated partition above its core's declared capability.
    "safety.partition-on-unqualified-core": lambda root: (
        _change(root, "hardware/CORE0.yaml", lambda d: d.update(asil_capability="A")),
        _change(root, "hardware/PARTITION0.yaml", lambda d: d.update(asil="D")),
    ),
    # The three per-type rules over what `Task_10ms` runs in `PARTITION0`: the consumer above the
    # partition; its derived `QM` below a `D` partition with no argument; its `RAM0` budget under a
    # `B` partition on a memory able to host only `A`.
    "safety.component-above-partition": lambda root: (
        _change(root, "hardware/PARTITION0.yaml", lambda d: d.update(asil="A")),
        _change(root, _CONSUMER, lambda d: d.update(asil="D")),
    ),
    "safety.component-below-partition": lambda root: _change(
        root, "hardware/PARTITION0.yaml", lambda d: d.update(asil="D")
    ),
    "safety.partition-memory-below-level": lambda root: (
        _change(root, "hardware/PARTITION0.yaml", lambda d: d.update(asil="B")),
        _change(root, "hardware/RAM0.yaml", lambda d: d.update(asil_capability="A")),
        _change(root, _CONSUMER, lambda d: d.update(asil="B")),
    ),
    # The two trust rules: `PARTITION0` `trusted` beside a second, empty partition rated `D` on the
    # same core: rated below it, and unrated.
    "safety.trusted-partition-below-processor-level": lambda root: (
        _change(root, "hardware/PARTITION0.yaml", lambda d: d.update(asil="B", trust="trusted")),
        _put(
            root,
            "hardware/PARTITION_HIGH.yaml",
            {"name": "PARTITION_HIGH", "kind": "partition", "core": "CORE0", "asil": "D"},
        ),
    ),
    "safety.trusted-partition-unrated": lambda root: (
        _change(root, "hardware/PARTITION0.yaml", lambda d: d.update(trust="trusted")),
        _put(
            root,
            "hardware/PARTITION_HIGH.yaml",
            {"name": "PARTITION_HIGH", "kind": "partition", "core": "CORE0", "asil": "D"},
        ),
    ),
    "sequence.duplicate-name": lambda root: _change(root, _SEQUENCE, lambda d: _put(root, "sequences/again.yaml", d)),
    "sequence.step-not-mapped": lambda root: _change(
        root, _SEQUENCE, lambda d: d["steps"].append({"swc": "ExampleSWC", "runnable": "ExampleRun"})
    ),
    # The unknown-name rules are asked only of a step the task maps, so the task
    # names the same unknown pair; otherwise `step-not-mapped` answers first.
    "sequence.step-unknown-runnable": lambda root: (
        _change(root, _TASK, lambda d: d["runnables"][0].update(runnable="Nope")),
        _change(root, _SEQUENCE, lambda d: d["steps"][0].update(runnable="Nope")),
    ),
    "sequence.step-unknown-swc": lambda root: (
        _change(root, _TASK, lambda d: d["runnables"][0].update(swc="Nope")),
        _change(root, _SEQUENCE, lambda d: d["steps"][0].update(swc="Nope")),
    ),
    "sequence.unknown-task": lambda root: _change(root, _SEQUENCE, lambda d: d.update(task="Nope")),
    "swc.access.direction-mismatch": lambda root: _change(
        root, _PROVIDER, lambda d: d["runnables"][0].update(reads=[{"port": "pOut", "element": "value"}])
    ),
    "swc.access.interface-kind-mismatch": lambda root: _change(
        root, _CONSUMER, lambda d: d["runnables"][0].update(calls=[{"port": "rIn", "operation": "ReadDtc"}])
    ),
    "swc.access.unknown-element": lambda root: _change(
        root, _CONSUMER, lambda d: d["runnables"][0].update(reads=[{"port": "rIn", "element": "nope"}])
    ),
    "swc.access.unknown-operation": lambda root: _change(
        root,
        _CONSUMER,
        lambda d: (
            d["ports"].append({"name": "rDiag", "direction": "require", "interface": "IDiagnosticService"}),
            d["runnables"][0].update(calls=[{"port": "rDiag", "operation": "Nope"}]),
        ),
    ),
    "swc.access.unknown-port": lambda root: _change(
        root, _CONSUMER, lambda d: d["runnables"][0].update(reads=[{"port": "nope", "element": "value"}])
    ),
    # `R_{port}_{element}`: port `r_x` reading `c` and port `r` reading `x_c` both derive `R_r_x_c`.
    "swc.access.name-collision": lambda root: (
        _change(
            root,
            _SR_INTERFACE,
            lambda d: d["data_elements"].extend(
                [{"name": "c", "implementation_type": "float32"}, {"name": "x_c", "implementation_type": "float32"}]
            ),
        ),
        _change(
            root,
            _CONSUMER,
            lambda d: (
                d["ports"].extend(
                    [
                        {"name": "r_x", "direction": "require", "interface": "IExample"},
                        {"name": "r", "direction": "require", "interface": "IExample"},
                    ]
                ),
                d["runnables"][0].update(reads=[{"port": "r_x", "element": "c"}, {"port": "r", "element": "x_c"}]),
            ),
        ),
    ),
    # The six operation-event rules, each on a runnable of its own so no other rule moves.
    "swc.operation-event.unknown-port": lambda root: _serve(root, [_operation_event("nope")]),
    "swc.operation-event.direction-mismatch": lambda root: (
        _change(
            root,
            _PROVIDER,
            lambda d: d["ports"].append({"name": "rDiag", "direction": "require", "interface": "IDiagnosticService"}),
        ),
        _serve(root, [_operation_event("rDiag")]),
    ),
    "swc.operation-event.interface-kind-mismatch": lambda root: _serve(root, [_operation_event("pOut")]),
    "swc.operation-event.unknown-operation": lambda root: _serve(root, [_operation_event("pDiag", "Nope")], True),
    "swc.operation-event.duplicate-binding": lambda root: (
        _serve(root, [_operation_event("pDiag")], True),
        _change(
            root,
            _PROVIDER,
            lambda d: d["runnables"].append({"name": "ServeAgain", "events": [_operation_event("pDiag")]}),
        ),
    ),
    "swc.operation-event.incompatible-signatures": lambda root: (
        _change(
            root,
            _CS_INTERFACE,
            lambda d: d["operations"].append(
                {"name": "Reset", "arguments": [{"name": "mode", "direction": "in", "implementation_type": "uint8"}]}
            ),
        ),
        _serve(root, [_operation_event("pDiag"), _operation_event("pDiag", "Reset")], True),
    ),
    "swc.external-incomplete-reference": lambda root: _put(
        root,
        "swc/example/Stub.yaml",
        {"name": "Stub", "type": "application", "external": True, "external_ref": "not a path at all"},
    ),
    "swc.external-reference-without-stub": lambda root: _change(
        root, _PROVIDER, lambda d: d.update(external_ref="/Foreign/Pkg/ComponentTypes/Stub")
    ),
    "swc.external-reference-inside-generated-package": lambda root: _put(
        root,
        "swc/example/Stub.yaml",
        {
            "name": "Stub",
            "type": "application",
            "external": True,
            "external_ref": "/ComponentTypes/ExampleSWC",
            "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
        },
    ),
    "swc.external-with-ports": lambda root: _change(
        root,
        _PROVIDER,
        lambda d: d.update(
            external=True,
            external_ref="/Foreign/Pkg/ComponentTypes/ExampleSWC",
            external_dest="APPLICATION-SW-COMPONENT-TYPE",
        ),
    ),
    "task.duplicate-name": lambda root: _change(root, _TASK, lambda d: _put(root, "tasks/again.yaml", d)),
    "task.mapping-unknown-runnable": lambda root: _change(
        root, _TASK, lambda d: d["runnables"][0].update(runnable="Nope")
    ),
    "task.mapping-unknown-swc": lambda root: _change(root, _TASK, lambda d: d["runnables"][0].update(swc="Nope")),
    # The split rule: the consumer, placed once in `Root`, gains a runnable a task of a second
    # partition runs, so its one instance would run in two partitions.
    "task.instance-in-multiple-partitions": lambda root: (
        _change(root, _CONSUMER, lambda d: d["runnables"].append({"name": "ExampleConsumerSplitRun"})),
        _put(root, "hardware/PARTITION1.yaml", {"name": "PARTITION1", "kind": "partition", "core": "CORE0"}),
        _put(
            root,
            "tasks/Task_20ms.yaml",
            {
                "name": "Task_20ms",
                "period": 0.02,
                "priority": 4,
                "partition": "PARTITION1",
                "runnables": [{"swc": "ExampleConsumerSWC", "runnable": "ExampleConsumerSplitRun"}],
            },
        ),
    ),
    # The consumer's runnable declares a cost above `Task_10ms`'s 1200us budget.
    "task.runnable-cost-exceeds-task": lambda root: _change(
        root, _CONSUMER, lambda d: d["runnables"][0].update(wcet="5000us", wcet_type="gross")
    ),
    # The six port, event and access cross-element rules, each one compact break.
    "swc.port.com-spec-unknown-element": lambda root: _change(
        root, _CONSUMER, lambda d: d["ports"][0].update(com_specs=[{"data_element": "absent"}])
    ),
    "swc.port.com-spec-inapplicable": lambda root: _change(
        root, _PROVIDER, lambda d: d["ports"][0].update(com_specs=[{"data_element": "value", "enable_update": True}])
    ),
    # An `init` on a com-spec entry for an element a stub lists.
    "swc.port.com-spec-on-stub": lambda root: (
        _put(
            root,
            "interfaces/IStubbed.yaml",
            {
                "name": "IStubbed",
                "kind": "external_interface",
                "external_ref": "/Foreign/PortInterfaces/IStubbed",
                "external_dest": "SENDER-RECEIVER-INTERFACE",
                "data_elements": ["x"],
            },
        ),
        _change(
            root,
            _CONSUMER,
            lambda d: d["ports"].append(
                {
                    "name": "rStub",
                    "direction": "require",
                    "interface": "IStubbed",
                    "com_specs": [{"data_element": "x", "init": 0}],
                }
            ),
        ),
    ),
    "swc.port.queued-receiver-without-queue-length": lambda root: _queue(root),
    "swc.timing_event.disabled-mode-port": lambda root: _change(
        root,
        _CONSUMER,
        lambda d: d["runnables"][0]["events"][0].update(
            disabled_modes=[{"port": "rMode", "mode_group_ref": "/Modes/IModes/Mode", "mode_ref": "/Modes/Group/Off"}]
        ),
    ),
    "swc.access.queued-read-by-value": lambda root: (
        _queue(root, queue_length=1),
        _change(
            root,
            _CONSUMER,
            lambda d: d["runnables"][0].update(reads=[{"port": "rIn", "element": "value", "by": "value"}]),
        ),
    ),
    # The four inter-runnable variable rules, over the consumer's runnable and a variable of its own.
    "swc.variable.undeclared-type": lambda root: _change(
        root,
        _CONSUMER,
        lambda d: d.update(variables=[{"name": "Shared", "access": "explicit", "implementation_type": "Nope"}]),
    ),
    "swc.variable.pointer-type": lambda root: (
        _put(
            root,
            model_element_storage_name("ProbePtr", kind="pointer"),
            {"name": "ProbePtr", "kind": "pointer", "target": "float32"},
        ),
        _change(
            root,
            _CONSUMER,
            lambda d: d.update(variables=[{"name": "Shared", "access": "explicit", "implementation_type": "ProbePtr"}]),
        ),
    ),
    "swc.variable-access.unknown-variable": lambda root: _change(
        root, _CONSUMER, lambda d: d["runnables"][0].update(reads_variables=[{"variable": "Nope"}])
    ),
    "swc.variable.read-without-init": lambda root: _change(
        root,
        _CONSUMER,
        lambda d: (
            d.update(variables=[{"name": "Shared", "access": "implicit", "implementation_type": "float32"}]),
            d["runnables"][0].update(reads_variables=[{"variable": "Shared"}]),
        ),
    ),
    # The exclusive-area cross-element rule: an area the component does not declare.
    "swc.exclusive-area.unknown-area": lambda root: _change(
        root, _CONSUMER, lambda d: d["runnables"][0].update(runs_inside=[{"area": "Nope"}])
    ),
    # The four data-received event rules, each through an event appended to a reference runnable.
    "swc.data-event.unknown-port": lambda root: _change(
        root, _CONSUMER, lambda d: d["runnables"][0]["events"].append(_data_event("nope"))
    ),
    "swc.data-event.direction-mismatch": lambda root: _change(
        root, _PROVIDER, lambda d: d["runnables"][0]["events"].append(_data_event("pOut"))
    ),
    "swc.data-event.interface-kind-mismatch": lambda root: _change(
        root,
        _CONSUMER,
        lambda d: (
            d["ports"].append({"name": "rDiag", "direction": "require", "interface": "IDiagnosticService"}),
            d["runnables"][0]["events"].append(_data_event("rDiag")),
        ),
    ),
    "swc.data-event.unknown-element": lambda root: _change(
        root, _CONSUMER, lambda d: d["runnables"][0]["events"].append(_data_event("rIn", "nope"))
    ),
    # The implicit read's two rules over the consumer's read of `rIn.value`; the asynchronous call's
    # seven over a client port and runnables the consumer gains (`_async_calls`).
    "swc.access.queued-implicit-read": lambda root: (
        _change(root, _SR_INTERFACE, lambda d: d["data_elements"][0].update(sw_impl_policy="queued")),
        _change(
            root,
            _CONSUMER,
            lambda d: (
                d["ports"][0].update(com_specs=[{"data_element": "value", "queue_length": 4}]),
                d["runnables"][0].update(reads=[{"port": "rIn", "element": "value", "by": "implicit"}]),
            ),
        ),
    ),
    "swc.access.implicit-read-enable-update": lambda root: _change(
        root,
        _CONSUMER,
        lambda d: (
            d["ports"][0].update(com_specs=[{"data_element": "value", "enable_update": True}]),
            d["runnables"][0].update(reads=[{"port": "rIn", "element": "value", "by": "implicit"}]),
        ),
    ),
    "swc.call.mixed-synchrony": lambda root: _async_calls(
        root, ([], [{"asynchronous": True, "poll": True}]), ([], [{}])
    ),
    "swc.call.timeout-mismatch": lambda root: _async_calls(
        root,
        ([], [{"asynchronous": True, "poll": True, "timeout": 0.1}]),
        ([], [{"asynchronous": True, "poll": True, "timeout": 0.2}]),
    ),
    "swc.call.async-without-result": lambda root: _async_calls(root, ([], [{"asynchronous": True}])),
    "swc.call.several-results": lambda root: _async_calls(
        root, ([], [{"asynchronous": True, "poll": True}]), ([_returned_diag()], [])
    ),
    "swc.call-returned-event.unknown-call": lambda root: _async_calls(root, ([_returned_diag()], [])),
    "swc.call-returned-event.ambiguous-call": lambda root: _async_calls(
        root, ([], [{"asynchronous": True}]), ([], [{"asynchronous": True}]), ([_returned_diag()], [])
    ),
    "swc.call.init-runnable-result": lambda root: _async_calls(
        root, ([{"type": "init"}], [{"asynchronous": True, "poll": True}])
    ),
    "composition.port-service-interface": lambda root: (
        _change(root, _SR_INTERFACE, lambda d: d.update(is_service=True)),
        _change(
            root,
            "compositions/Root.yaml",
            lambda d: d.update(ports=[{"name": "pRoot", "direction": "provide", "interface": "IExample"}]),
        ),
    ),
    "data-type.conversion-without-unit": lambda root: _put(
        root,
        model_element_storage_name("ProbeSpeed", kind="application_type"),
        {"name": "ProbeSpeed", "kind": "application_type", "conversion": {"factor": 2}},
    ),
    # The two runnable-symbol rules: a symbol that is no C identifier, a second runnable's same name.
    "swc.runnable.symbol-not-c-identifier": lambda root: _change(
        root, _PROVIDER, lambda d: d["runnables"][0].update(symbol="1bad")
    ),
    "name.duplicate-runnable-symbol": lambda root: _change(
        root, _PROVIDER, lambda d: d["runnables"].append({"name": "Second", "symbol": d["runnables"][0]["name"]})
    ),
    # The free-text rule: a description XML 1.0 cannot carry.
    "text.xml-unrepresentable-character": lambda root: _put(
        root,
        model_element_storage_name("ProbeText", kind="value_type"),
        {"name": "ProbeText", "kind": "value_type", "base": "float32_base", "description": "bell\x07"},
    ),
    # The instance identity: the resolver's three refusals and two resolved duplicates. A second
    # root member of the consumer's type makes the `swc` entry and step ambiguous at once.
    "task.instance-ambiguous": lambda root: _change(
        root, _ROOT, lambda d: d["members"].append({"name": "ConsumerTwo", "type": "ExampleConsumerSWC"})
    ),
    "task.instance-unknown": lambda root: _change(
        root, _TASK, lambda d: d["runnables"].append({"instance": "NoSuchMember", "runnable": "ExampleRun"})
    ),
    "task.instance-not-a-component": lambda root: _an_instance_ending_on_a_composition(root, _TASK, "runnables"),
    "task.resolved-duplicate-runnable": lambda root: _change(
        root,
        _TASK,
        lambda d: d["runnables"].append({"instance": "ExampleConsumerSWC", "runnable": "ExampleConsumerRun"}),
    ),
    "sequence.resolved-duplicate-step": lambda root: _change(
        root,
        _SEQUENCE,
        lambda d: d["steps"].append({"instance": "ExampleConsumerSWC", "runnable": "ExampleConsumerRun"}),
    ),
    "sequence.step-instance-unknown": lambda root: _change(
        root, _SEQUENCE, lambda d: d["steps"].append({"instance": "NoSuchMember", "runnable": "ExampleRun"})
    ),
    "sequence.step-instance-not-a-component": lambda root: _an_instance_ending_on_a_composition(
        root, _SEQUENCE, "steps"
    ),
    # A key no field of its kind declares, in the provider's port, so the subject runs through a
    # member to a key step (the loader drops the key).
    "model.unknown-key": lambda root: _change(root, _PROVIDER, lambda d: d["ports"][0].update(intrface="IExample")),
}


def _queue(model_root: Path, queue_length: int | None = None) -> None:
    """`IExample.value` queued; with *queue_length*, the consumer's `rIn` gives it one."""
    _change(model_root, _SR_INTERFACE, lambda d: d["data_elements"][0].update(sw_impl_policy="queued"))
    if queue_length is not None:
        _change(
            model_root,
            _CONSUMER,
            lambda d: d["ports"][0].update(com_specs=[{"data_element": "value", "queue_length": queue_length}]),
        )


_STUB_SR_REF = "/Foreign/PortInterfaces/IForeign"
_STUB_TYPE_REF = "/Foreign/ImplementationDataTypes/ForeignT"
#: Spelled like an AUTOSAR reference path — and like an absolute POSIX path — with
#: one segment that is not an Identifier, so the stub rule quotes it back.
_STUB_SR_REF_MALFORMED = "/Foreign/Port-Interfaces/IForeign"
_STUB_TYPE_REF_MALFORMED = "/Foreign/Implementation-Data-Types/ForeignT"


def _interface_stub(model_root: Path, name: str, dest: str, ref: str = _STUB_SR_REF) -> None:
    _put(
        model_root,
        f"interfaces/{name}.yaml",
        {"name": name, "kind": "external_interface", "external_ref": ref, "external_dest": dest},
    )


def _type_stub(
    model_root: Path, name: str = "ForeignT", dest: str = "IMPLEMENTATION-DATA-TYPE", ref: str = _STUB_TYPE_REF
) -> None:
    write_model_element(model_root, {"name": name, "kind": "external_type", "external_ref": ref, "external_dest": dest})


def _consumer_port_on_a_stub(model_root: Path, dest: str, list_name: str, entry: dict[str, str]) -> None:
    """A require port on an interface stub, and one access entry through it."""
    _interface_stub(model_root, "IForeign", dest)

    def mutate(document: dict[str, Any]) -> None:
        document["ports"].append({"name": "rForeign", "direction": "require", "interface": "IForeign"})
        document["runnables"][0][list_name] = [{"port": "rForeign", **entry}]

    _change(model_root, _CONSUMER, mutate)


def _consumer_event_on_a_stub(model_root: Path, dest: str) -> None:
    """A require port on an interface stub of *dest*, and one data-received event through it."""
    _interface_stub(model_root, "IForeign", dest)

    def mutate(document: dict[str, Any]) -> None:
        document["ports"].append({"name": "rForeign", "direction": "require", "interface": "IForeign"})
        document["runnables"][0]["events"].append(_data_event("rForeign"))

    _change(model_root, _CONSUMER, mutate)


def _measure_on_a_stub_port(model_root: Path) -> None:
    _interface_stub(model_root, "IForeign", "SENDER-RECEIVER-INTERFACE")
    _change(model_root, _PROVIDER, lambda d: d["ports"][0].update(interface="IForeign"))
    _change(
        model_root,
        _CONSUMER,
        lambda d: d["ports"][0].update(interface="IForeign", measures={"value": "end-to-end protection"}),
    )


def _a_delegation_across_a_stub(model_root: Path) -> None:
    _interface_stub(model_root, "IForeign", "CLIENT-SERVER-INTERFACE")
    _change(
        model_root,
        "compositions/Root.yaml",
        lambda d: d.update(
            ports=[{"name": "pExt", "direction": "provide", "interface": "IForeign"}],
            delegations=[{"name": "dExt", "port": "pExt", "member": "ExampleSWC", "member_port": "pOut"}],
        ),
    )


def _an_init_through_a_chain_to_a_stub(model_root: Path) -> None:
    _type_stub(model_root)
    write_model_element(model_root, {"name": "RefForeign_T", "kind": "reference_type", "base": "ForeignT"})
    _typed_element(model_root, "RefForeign_T", 1)


@dataclass(frozen=True)
class StubTrigger:
    """One break that makes a finding speak about an external stub.

    ``fragment`` is a piece of the message only a stub's case says, so a trigger that stops reaching
    the stub's wording fails.
    """

    rule: str
    fragment: str
    apply: Callable[[Path], None]


#: Every finding the external stubs make the validator word: the three stub rules (the reference
#: path quoted, malformed, half blank), a stub's illegal name, an ``init`` reaching a stub, an access
#: through a stub port, the kind compared across a stub, a composite member typed by a stub, a
#: ``measures`` key on a stub port. ``test_every_stub_finding_is_reached_in_its_stub_wording`` proves
#: each. Does not reach the writer's refusals (the MCP reply gate's) or a stub shape no row builds.
STUB_MESSAGE_BREAKS: dict[str, StubTrigger] = {
    "interface stub with a DEST outside the set": StubTrigger(
        "interface.external-incomplete-reference",
        "NOT-A-PORT-INTERFACE",
        lambda root: _interface_stub(root, "IForeign", "NOT-A-PORT-INTERFACE"),
    ),
    "interface stub quoting a path with a segment that is no Identifier": StubTrigger(
        "interface.external-incomplete-reference",
        _STUB_SR_REF_MALFORMED,
        lambda root: _interface_stub(root, "IForeign", "SENDER-RECEIVER-INTERFACE", ref=_STUB_SR_REF_MALFORMED),
    ),
    "interface stub with a blank half": StubTrigger(
        "interface.external-incomplete-reference",
        "IForeign",
        lambda root: _interface_stub(root, "IForeign", "", ref=_STUB_SR_REF),
    ),
    "type stub with a DEST outside the set": StubTrigger(
        "data-type.external-incomplete-reference",
        "SW-BASE-TYPE",
        lambda root: _type_stub(root, dest="SW-BASE-TYPE"),
    ),
    "type stub quoting a path with a segment that is no Identifier": StubTrigger(
        "data-type.external-incomplete-reference",
        _STUB_TYPE_REF_MALFORMED,
        lambda root: _type_stub(root, ref=_STUB_TYPE_REF_MALFORMED),
    ),
    "interface stub with an illegal name": StubTrigger(
        "identifier.declared",
        "I Foreign",
        lambda root: _interface_stub(root, "I Foreign", "SENDER-RECEIVER-INTERFACE"),
    ),
    "type stub with an illegal name": StubTrigger(
        "identifier.declared", "Foreign T", lambda root: _type_stub(root, name="Foreign T")
    ),
    "an init through a reference chain ending at a stub": StubTrigger(
        "interface.init-on-external-type", "ForeignT", _an_init_through_a_chain_to_a_stub
    ),
    "a read through a stub port": StubTrigger(
        "swc.access.unknown-element",
        "external interface stub",
        lambda root: _consumer_port_on_a_stub(root, "SENDER-RECEIVER-INTERFACE", "reads", {"element": "value"}),
    ),
    "an operation event through a stub port": StubTrigger(
        "swc.operation-event.unknown-operation",
        "external interface stub",
        lambda root: (
            _interface_stub(root, "IForeign", "CLIENT-SERVER-INTERFACE"),
            _change(
                root,
                _PROVIDER,
                lambda d: (
                    d["ports"].append({"name": "pForeign", "direction": "provide", "interface": "IForeign"}),
                    d["runnables"].append({"name": "Serve", "events": [_operation_event("pForeign", "Op")]}),
                ),
            ),
        ),
    ),
    # Through a sender/receiver stub not listing the element, and a stub of another `DEST`, decided
    # by the stub's kind before any member lookup.
    "a data-received event through a stub port": StubTrigger(
        "swc.data-event.unknown-element",
        "external interface stub",
        lambda root: _consumer_event_on_a_stub(root, "SENDER-RECEIVER-INTERFACE"),
    ),
    "a data-received event through a mode-switch stub port": StubTrigger(
        "swc.data-event.interface-kind-mismatch",
        "external_dest 'MODE-SWITCH-INTERFACE'",
        lambda root: _consumer_event_on_a_stub(root, "MODE-SWITCH-INTERFACE"),
    ),
    "a call through a stub port": StubTrigger(
        "swc.access.unknown-operation",
        "external interface stub",
        lambda root: _consumer_port_on_a_stub(root, "CLIENT-SERVER-INTERFACE", "calls", {"operation": "Op"}),
    ),
    "a connector across a stub of another kind": StubTrigger(
        "connector.interface-mismatch",
        "external_interface (CLIENT-SERVER-INTERFACE)",
        lambda root: (
            _interface_stub(root, "IForeign", "CLIENT-SERVER-INTERFACE"),
            _change(root, _PROVIDER, lambda d: d["ports"][0].update(interface="IForeign")),
        ),
    ),
    "a delegation across a stub of another kind": StubTrigger(
        "composition.delegation-interface-mismatch",
        "external_interface (CLIENT-SERVER-INTERFACE)",
        _a_delegation_across_a_stub,
    ),
    # A record field's `type` resolves in `application_types` alone, so a type stub of that name is
    # an undeclared name of the role's layer.
    "a record field typed by a stub": StubTrigger(
        "data-type.record-field-undeclared-type",
        "ForeignT",
        lambda root: (
            _type_stub(root),
            _put(
                root,
                model_element_storage_name("Rec", kind="record"),
                {"name": "Rec", "kind": "record", "fields": [{"name": "a", "type": "ForeignT"}]},
            ),
        ),
    ),
    "a measure on a stub port": StubTrigger(
        "safety.measure-unknown-element", "external interface stub", _measure_on_a_stub_port
    ),
}


#: Every finding the exclusive areas make an OLDER rule word (their own rule is
#: :data:`MESSAGE_GATE_BREAKS`'): an area named like a runnable, an area name no Identifier, areas on
#: a component stub, an area description XML cannot carry.
#: ``test_every_exclusive_area_finding_is_reached_in_its_wording`` proves each. Does not reach the
#: writer's refusals of the same models or a shape no row builds.
AREA_MESSAGE_BREAKS: dict[str, StubTrigger] = {
    "an area named like a runnable": StubTrigger(
        "name.name-space-collision",
        "(exclusive_areas)",
        lambda root: _change(
            root, _CONSUMER, lambda d: d.update(exclusive_areas=[{"name": d["runnables"][0]["name"]}])
        ),
    ),
    "an area name that is no Identifier": StubTrigger(
        "identifier.declared",
        "exclusive area 'speed state'",
        lambda root: _change(root, _CONSUMER, lambda d: d.update(exclusive_areas=[{"name": "speed state"}])),
    ),
    "areas on a component stub": StubTrigger(
        "swc.external-with-ports",
        "exclusive_areas",
        lambda root: _change(
            root,
            _PROVIDER,
            lambda d: d.update(
                external=True,
                external_ref="/Foreign/Pkg/ComponentTypes/ExampleSWC",
                external_dest="APPLICATION-SW-COMPONENT-TYPE",
                exclusive_areas=[{"name": "Guarded"}],
            ),
        ),
    ),
    "an area description XML cannot carry": StubTrigger(
        "text.xml-unrepresentable-character",
        "exclusive area 'Guarded'",
        lambda root: _change(
            root, _CONSUMER, lambda d: d.update(exclusive_areas=[{"name": "Guarded", "description": "a\x01b"}])
        ),
    ),
}


def _probe_record(model_root: Path, *elements: tuple[str, str], name: str = "ProbeFrame") -> None:
    write_model_element(
        model_root,
        {"name": name, "kind": "implementation_record", "elements": [{"name": n, "type": t} for n, t in elements]},
    )


#: Every finding the implementation composites make an OLDER rule word (their ten rules are
#: :data:`MESSAGE_GATE_BREAKS`'): non-Identifier names, a closed pointer graph, an ``init`` on each
#: kind and one reaching a type stub inside a composite, a map onto an alias chain ending at a pointer.
#: ``test_every_implementation_composite_finding_is_reached_in_its_wording`` proves each. Does not
#: reach the writer's refusals (the MCP reply gate's) or a shape no row builds.
IMPL_COMPOSITE_MESSAGE_BREAKS: dict[str, StubTrigger] = {
    "an implementation element name that is no Identifier": StubTrigger(
        "identifier.declared", "element 'a b'", lambda root: _probe_record(root, ("a b", "uint8"))
    ),
    "an implementation array element_name that is no Identifier": StubTrigger(
        "identifier.declared",
        "element name 'b c'",
        lambda root: _put(
            root,
            model_element_storage_name("ProbeSamples", kind="implementation_array"),
            {
                "name": "ProbeSamples",
                "kind": "implementation_array",
                "element_type": "uint8",
                "length": 2,
                "element_name": "b c",
            },
        ),
    ),
    "a pointer graph that closes on itself": StubTrigger(
        "data-type.cycle",
        "ProbePtr",
        lambda root: (
            _probe_record(root, ("next", "ProbePtr")),
            _put(
                root,
                model_element_storage_name("ProbePtr", kind="pointer"),
                {"name": "ProbePtr", "kind": "pointer", "target": "ProbeFrame"},
            ),
        ),
    ),
    "an init naming another member on an implementation record": StubTrigger(
        "interface.init-record-field-mismatch",
        "ProbeFrame",
        lambda root: (_probe_record(root, ("a", "uint8")), _typed_element(root, "ProbeFrame", {"b": 1})),
    ),
    "an init of another length on an implementation array": StubTrigger(
        "interface.init-array-length-mismatch",
        "ProbeSamples",
        lambda root: (
            _put(
                root,
                model_element_storage_name("ProbeSamples", kind="implementation_array"),
                {
                    "name": "ProbeSamples",
                    "kind": "implementation_array",
                    "element_type": "uint8",
                    "length": 2,
                    "element_name": "E",
                },
            ),
            _typed_element(root, "ProbeSamples", [1]),
        ),
    ),
    "an init on a pointer": StubTrigger(
        "interface.init-shape-mismatch",
        "ProbePtr",
        lambda root: (
            _put(
                root,
                model_element_storage_name("ProbePtr", kind="pointer"),
                {"name": "ProbePtr", "kind": "pointer", "target": "uint8"},
            ),
            _typed_element(root, "ProbePtr", 1),
        ),
    ),
    "an init reaching a type stub inside a composite": StubTrigger(
        "interface.init-on-external-type",
        "ForeignT",
        lambda root: (
            _type_stub(root),
            _probe_record(root, ("a", "ForeignT")),
            _typed_element(root, "ProbeFrame", {"a": 1}),
        ),
    ),
    "a map onto an alias chain that ends at a pointer": StubTrigger(
        "mapping-set.pointer-mapped",
        "'ProbeRef' -> 'ProbePtr'",
        lambda root: (
            _put(
                root,
                model_element_storage_name("ProbeSpeed", kind="application_type"),
                {"name": "ProbeSpeed", "kind": "application_type"},
            ),
            _put(
                root,
                model_element_storage_name("ProbePtr", kind="pointer"),
                {"name": "ProbePtr", "kind": "pointer", "target": "uint8"},
            ),
            _put(
                root,
                model_element_storage_name("ProbeRef", kind="reference_type"),
                {"name": "ProbeRef", "kind": "reference_type", "base": "ProbePtr"},
            ),
            _put(
                root,
                model_element_storage_name("ProbeMaps", layer="mapping_sets"),
                {"name": "ProbeMaps", "maps": [{"application": "ProbeSpeed", "implementation": "ProbeRef"}]},
            ),
        ),
    ),
}

#: Every rule of ``validator.CONSISTENCY_RULES`` the triggers do NOT mint, with its reason: a stated
#: limit, never a skip. Held by set EQUALITY with the measured complement.
UNREACHED_BY_THE_MESSAGE_GATE: dict[str, str] = {
    "interface.init-unwritable-integer": (
        "an integer too long for `sys.get_int_max_str_digits()` cannot be loaded from YAML at all, so no file-built "
        "model reaches the check; `test_validator_init.py` builds it in memory"
    ),
    "storage.filename-case-collision": (
        "two file names differing only in case cannot coexist on a case-insensitive filesystem, so a trigger would "
        "make the gate's answer depend on the host; `test_validator_storage.py` witnesses it, probe-gated"
    ),
    "storage.reserved-device-name": (
        "a file named for a Windows device is no file at all on a Windows host, so a trigger would make the gate's "
        "answer depend on the host; `test_reserved_device_names.py` witnesses it, probe-gated on disk, and asserts "
        "its message carries no file name, separator or directory over a synthesized listing on every host"
    ),
}


def _reference_model(directory: Path) -> Path:
    return fixtures.build_fixture(directory / "project", BASE_VECTOR) / "model"


def _compositions_model(directory: Path) -> Path:
    destination = directory / "model"
    shutil.copytree(compositions_suite.COMPOSITIONS_MODEL_ROOT, destination)
    return destination


def _message_triggers() -> list[tuple[str, Callable[[Path], Path], Callable[[Path], None]]]:
    """Every trigger: a label, the base model it breaks, and the break."""
    triggers = [(f"message-gate {rule}", _reference_model, break_it) for rule, break_it in MESSAGE_PATH_BREAKS.items()]
    triggers += [(f"message-gate {rule}", _reference_model, break_it) for rule, break_it in MESSAGE_GATE_BREAKS.items()]
    triggers += [(f"stub {label}", _reference_model, trigger.apply) for label, trigger in STUB_MESSAGE_BREAKS.items()]
    triggers += [
        (f"impl-composite {label}", _reference_model, trigger.apply)
        for label, trigger in IMPL_COMPOSITE_MESSAGE_BREAKS.items()
    ]
    triggers += [(f"area {label}", _reference_model, trigger.apply) for label, trigger in AREA_MESSAGE_BREAKS.items()]
    triggers += [
        (f"rule-coverage {rule}", _reference_model, break_it)
        for _label, break_it, rule, _subjects in rule_coverage_suite.FORMERLY_UNDIAGNOSED
    ]
    triggers += [(f"data-types {rule}", _reference_model, row[0]) for rule, row in data_types_suite.BREAKS.items()]
    triggers += [
        (f"application-types {rule}", _reference_model, entry.apply)
        for rule, entry in application_types_suite.BREAKS.items()
    ]
    triggers += [
        (f"compositions {rule}", _compositions_model, break_it)
        for rule, break_it in compositions_suite.COMPOSITION_FAMILY_BREAKS.items()
    ]
    # The ten rules of the three element layers.
    triggers += [
        (f"data-description-elements {rule}", _reference_model, row[0])
        for rule, row in data_description_suite.BREAKS.items()
    ]
    return triggers


def _location_carriers(messages: Iterable[str], model_root: Path) -> list[str]:
    """The messages naming *model_root*, the system temporary directory, a file, or a drive path."""
    temporary = Path(tempfile.gettempdir())
    markers = {
        MESSAGE_PROBE_DIRNAME,
        ".yaml",
        str(model_root),
        model_root.as_posix(),
        str(model_root.resolve()),
        model_root.resolve().as_posix(),
        str(temporary),
        str(temporary.resolve()),
    }
    return [text for text in messages if any(marker in text for marker in markers) or _DRIVE_LETTER_PATH.search(text)]


@dataclass(frozen=True)
class MessageMeasurement:
    """What one run of every trigger minted: the rules reached and the messages naming a location."""

    reached: frozenset[str]
    carriers: Mapping[str, tuple[str, ...]]
    #: Every ``(rule, message)`` each trigger minted, by the trigger's label.
    minted: Mapping[str, tuple[tuple[str, str], ...]]
    #: Every emitted subject whose ``Subject.to_json()`` differs from the protocol's
    #: ``subject_address(subject).to_json()``, as ``(label, rule, plain, protocol)``.
    subject_mismatches: tuple[tuple[str, str, Any, Any], ...] = ()
    #: How many emitted subjects were compared, and the ``(label, rule, reason)`` of
    #: each one skipped because ``Address`` refuses its element.
    subjects_compared: int = 0
    subjects_skipped: tuple[tuple[str, str, str], ...] = ()
    #: The compared subjects whose path holds a member step (the nested places).
    nested_subjects_compared: int = 0


def _measure_messages(directory: Path) -> MessageMeasurement:
    reached: set[str] = set()
    carriers: dict[str, tuple[str, ...]] = {}
    minted: dict[str, tuple[tuple[str, str], ...]] = {}
    mismatches: list[tuple[str, str, Any, Any]] = []
    skipped: list[tuple[str, str, str]] = []
    compared = 0
    nested = 0
    for index, (label, base, break_it) in enumerate(_message_triggers()):
        model_root = base(directory / MESSAGE_PROBE_DIRNAME / f"{index:03d}")
        break_it(model_root)
        result = check_cross_layer_consistency(str(model_root))
        # Both halves: a warning's message reaches the CLI and MCP replies like an error's.
        records = (*result.records, *result.warnings)
        reached.update(record.rule for record in records)
        minted[label] = tuple((record.rule, record.message) for record in records)
        leaking = _location_carriers((f"{record.rule}: {record.message}" for record in records), model_root)
        if leaking:
            carriers[label] = tuple(leaking)
        # The subject-JSON agreement: `check-model --format json` spells a subject through
        # `diagnostics.Subject.to_json`, the protocol through `Address.to_json`.
        for record in records:
            for subject in record.subjects:
                try:
                    protocol = subject_address(subject).to_json()
                except AddressError as exc:
                    skipped.append((label, record.rule, str(exc)))
                    continue
                compared += 1
                nested += any(step.kind == MEMBER for step in subject.path)
                plain = subject.to_json()
                if plain != protocol:
                    mismatches.append((label, record.rule, plain, protocol))
    return MessageMeasurement(
        frozenset(reached),
        carriers,
        minted,
        subject_mismatches=tuple(mismatches),
        subjects_compared=compared,
        subjects_skipped=tuple(skipped),
        nested_subjects_compared=nested,
    )


@pytest.fixture(scope="module")
def measured_messages(tmp_path_factory: pytest.TempPathFactory) -> MessageMeasurement:
    """Every trigger run once per module; both assertions read the one measurement."""
    return _measure_messages(tmp_path_factory.mktemp("message-gate"))


@pytest.mark.xdist_group("editing-invariants-messages")
class TestNoValidatorMessageCarriesAStoragePath:
    """No validator message names where the model is stored.

    Pass D carries a finding's ``message`` into the response verbatim, and
    :class:`TestNoResponseCarriesAStoragePath` exempts :data:`PROSE_KEYS`. Every trigger builds a
    model under :data:`MESSAGE_PROBE_DIRNAME`, breaks it, and every message
    ``check_cross_layer_consistency`` renders must name no location. The triggers are this module's
    break tables plus the validator suites' (``FORMERLY_UNDIAGNOSED``, the data-type ``BREAKS``,
    ``COMPOSITION_FAMILY_BREAKS``); a rule counts as reached when any trigger mints it.

    Does not reach:

    * a rule no trigger mints (:data:`UNREACHED_BY_THE_MESSAGE_GATE`);
    * a POSIX absolute path other than the probe's own location: an AUTOSAR reference path is spelled
      the same and the stub rules quote one on purpose
      (:meth:`test_an_autosar_reference_path_is_not_a_storage_location` holds both halves);
    * a path an author typed into a free-text field, which is echoed, not storage;
    * engine exception messages (``test_editing_validation.py``'s
      ``test_the_raise_names_the_layer_and_element_and_never_where_the_model_is_staged``);
    * what an MCP tool replies (``packages/mcp/tests/test_no_tool_reply_carries_a_path.py``).
    """

    def test_no_message_names_where_the_model_is_stored(self, measured_messages: MessageMeasurement) -> None:
        assert dict(measured_messages.carriers) == {}

    def test_every_emitted_subject_s_json_is_the_protocol_address_s(
        self, measured_messages: MessageMeasurement
    ) -> None:
        """``Subject.to_json() == subject_address(subject).to_json()`` on every subject the triggers emit.

        ``check-model --format json`` spells a subject through ``diagnostics.Subject.to_json`` (``cli``
        may not import ``editing/``). A subject whose element ``Address`` refuses would be skipped; the
        count is held at zero.

        Does not reach a subject no trigger emits: no ``ports/*/satisfies`` or
        ``runnables/*/satisfies`` subject (``test_cli.py``'s
        ``TestCheckModelJsonForm.test_the_nested_link_subjects_are_the_protocol_address_s_json``).
        """
        measured = measured_messages
        assert measured.subjects_skipped == ()
        assert measured.subjects_compared > 0
        assert measured.nested_subjects_compared > 0, "no compared subject names a collection member"
        assert measured.subject_mismatches == (), (
            f"{len(measured.subject_mismatches)} of {measured.subjects_compared} subjects disagree; "
            f"skipped: {list(measured.subjects_skipped)}"
        )

    def test_the_rules_it_reaches_and_does_not_are_stated_by_id(self, measured_messages: MessageMeasurement) -> None:
        measured = measured_messages
        rules = set(CONSISTENCY_RULES)
        assert measured.reached <= rules, sorted(measured.reached - rules)
        unreached = rules - measured.reached
        assert unreached == set(UNREACHED_BY_THE_MESSAGE_GATE), (
            f"reached {len(measured.reached)} of {len(rules)}; unreached now {sorted(unreached)}"
        )
        assert set(MESSAGE_PATH_BREAKS) <= measured.reached

    @pytest.mark.parametrize("label", sorted(STUB_MESSAGE_BREAKS))
    def test_every_stub_finding_is_reached_in_its_stub_wording(
        self, measured_messages: MessageMeasurement, label: str
    ) -> None:
        """Each stub trigger minted its rule, in the words only a stub's case uses — so the scan saw that message."""
        trigger = STUB_MESSAGE_BREAKS[label]
        minted = measured_messages.minted[f"stub {label}"]
        messages = [message for rule, message in minted if rule == trigger.rule]
        assert messages, minted
        assert any(trigger.fragment in message for message in messages), messages

    @pytest.mark.parametrize("label", sorted(IMPL_COMPOSITE_MESSAGE_BREAKS))
    def test_every_implementation_composite_finding_is_reached_in_its_wording(
        self, measured_messages: MessageMeasurement, label: str
    ) -> None:
        """Each implementation-composite trigger mints its rule, naming the composite kind's element."""
        trigger = IMPL_COMPOSITE_MESSAGE_BREAKS[label]
        minted = measured_messages.minted[f"impl-composite {label}"]
        messages = [message for rule, message in minted if rule == trigger.rule]
        assert messages, minted
        assert any(trigger.fragment in message for message in messages), messages

    @pytest.mark.parametrize("label", sorted(AREA_MESSAGE_BREAKS))
    def test_every_exclusive_area_finding_is_reached_in_its_wording(
        self, measured_messages: MessageMeasurement, label: str
    ) -> None:
        """Each exclusive-area trigger minted its widened rule, naming the area."""
        trigger = AREA_MESSAGE_BREAKS[label]
        minted = measured_messages.minted[f"area {label}"]
        messages = [message for rule, message in minted if rule == trigger.rule]
        assert messages, minted
        assert any(trigger.fragment in message for message in messages), messages

    def test_an_autosar_reference_path_is_not_a_storage_location(
        self, measured_messages: MessageMeasurement, tmp_path: Path
    ) -> None:
        quoted = [
            message
            for label, minted in measured_messages.minted.items()
            if label.startswith("stub ")
            for _rule, message in minted
            if _STUB_SR_REF_MALFORMED in message or _STUB_TYPE_REF_MALFORMED in message
        ]
        # Non-vacuous: the stub rules DO put an AUTOSAR reference path into a message,
        assert len(quoted) >= 2, quoted
        # ...the gate does not read one as a location,
        assert _location_carriers(quoted, tmp_path / "model") == []
        # ...and the probe's own location, spelled the same way, is still one.
        model_root = tmp_path / MESSAGE_PROBE_DIRNAME / "model"
        assert _location_carriers([f"external_ref '{model_root.as_posix()}' is not declared"], model_root) != []

    def test_the_gate_fails_on_a_message_that_names_the_model_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The defect re-introduced into ONE check's messages, and the gate going red on it."""
        original = validator.check_identifier_conformance

        def leaking(model_root: ModelInput) -> ConsistencyResult:
            records = original(model_root).records
            root = as_view(model_root).model_root
            return consistency_result(
                [replace(record, message=f"{record.message} ({root}/data_types/)") for record in records]
            )

        validator_package.patch_everywhere(monkeypatch, "check_identifier_conformance", leaking)
        carriers = _measure_messages(tmp_path).carriers
        assert "message-gate identifier.declared" in carriers, carriers
        # Only the patched check's messages carry the root; every other rule's stay clean.
        assert all(text.startswith("identifier.") for texts in carriers.values() for text in texts), carriers


def test_the_message_gate_runs_on_one_worker_so_its_measurement_is_built_once() -> None:
    """``--dist loadgroup`` keeps every consumer of the module-scoped ``measured_messages`` on one worker."""
    marks = getattr(TestNoValidatorMessageCarriesAStoragePath, "pytestmark", [])
    assert [(mark.name, mark.args) for mark in marks if mark.name == "xdist_group"] == [
        ("xdist_group", ("editing-invariants-messages",))
    ]
    assert classes_consuming(Path(__file__), "measured_messages") == {"TestNoValidatorMessageCarriesAStoragePath"}
