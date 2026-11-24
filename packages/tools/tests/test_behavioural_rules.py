"""The seven cross-element rules over the behavioural values, and the widened rules.

* **the seven rules** (:data:`RULES`) -- each declared, bound, answered with
  ``consistency-violation``, firing on the offending shapes only, listed by ``check-model`` at
  both manifest versions, and refused by pass D in a change set;
* **the widened compatibility** -- the connector and delegation rules fire for a pair differing
  only in ``is_service`` or in queued-ness;
* **the widened stub rule** -- ``swc.external-with-ports`` fires for a stub port with ``com_specs``;
* **no committed tree** reports a finding of any of them.

Does not reach the ARXML writer's guards that refuse the same models
(``test_behavioural_emission.py``) or the editor-schema limitation rows
(``test_editor_schema_limits.py``).
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import validator_package
import yaml
from committed_trees_support import PARTIAL_TREE_BASES, committed_model_roots, tree_id
from conftest import REPO_ROOT, model_element_storage_name, write_model_element
from model_contract_fixtures import load_binding, load_contract
from project_support import files_of
from swcraft_tools.arxml_version import ArxmlVersion
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.diagnosis import RULE_CODES
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.model_state import ElementParseError
from swcraft_tools.validator import (
    CONSISTENCY_RULES,
    check_cross_layer_consistency,
)
from tracked_files_support import listed_files

BASIC = REPO_ROOT / "examples" / "basic"
PROVIDER = "swc/example/ExampleSWC.yaml"
CONSUMER = "swc/example/ExampleConsumerSWC.yaml"
INTERFACE = "interfaces/IExample.yaml"
ROOT = "compositions/Root.yaml"
MODE_STUB_REF = "/Modes/PortInterfaces/IModes"
MODE_GROUP_REF = f"{MODE_STUB_REF}/Mode"
MODE_REF = "/Modes/ModeDeclarationGroups/Modes/Off"

#: Each rule and the one predicate that mints it; the binding names it, or the step of it in
#: :data:`BOUND_STEPS`.
RULES: dict[str, str] = {
    "swc.port.com-spec-unknown-element": "port_com_spec_records",
    "swc.port.com-spec-inapplicable": "port_com_spec_records",
    "swc.port.queued-receiver-without-queue-length": "queued_receiver_records",
    "swc.timing_event.disabled-mode-port": "disabled_mode_records",
    "swc.access.queued-read-by-value": "queued_read_records",
    "composition.port-service-interface": "service_composition_port_records",
    "data-type.conversion-without-unit": "conversion_unit_records",
}

#: The step of its predicate that mints a rule, the function `binding.json` names, where it is not
#: the predicate itself.
BOUND_STEPS: dict[str, str] = {
    "swc.port.com-spec-unknown-element": "_com_spec_entry_records",
    "swc.port.com-spec-inapplicable": "_com_spec_entry_records",
}


# Builders over a copy of examples/basic


def _basic(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(BASIC, project, ignore=shutil.ignore_patterns("dist", "views"))
    return project


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _put(model: Path, relative: str, document: dict[str, Any]) -> None:
    path = model / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _edit(model: Path, relative: str, mutate: Callable[[dict[str, Any]], object]) -> None:
    document = _load(model / relative)
    mutate(document)
    _put(model, relative, document)


def _com_specs(model: Path, relative: str, *entries: dict[str, Any]) -> None:
    _edit(model, relative, lambda d: d["ports"][0].update(com_specs=list(entries)))


def _queued(model: Path) -> None:
    _edit(model, INTERFACE, lambda d: d["data_elements"][0].update(sw_impl_policy="queued"))


def _mode_stub(model: Path, dest: str = "MODE-SWITCH-INTERFACE") -> None:
    _put(
        model,
        "interfaces/IModes.yaml",
        {"name": "IModes", "kind": "external_interface", "external_ref": MODE_STUB_REF, "external_dest": dest},
    )


def _disabled_mode(
    model: Path,
    *,
    port: str = "rMode",
    direction: str = "require",
    interface: str | None = "IModes",
    group: str = MODE_GROUP_REF,
) -> None:
    def mutate(document: dict[str, Any]) -> None:
        declared: dict[str, Any] = {"name": "rMode", "direction": direction}
        if interface is not None:
            declared["interface"] = interface
        document["ports"].append(declared)
        document["runnables"][0]["events"][0]["disabled_modes"] = [
            {"port": port, "mode_group_ref": group, "mode_ref": MODE_REF}
        ]

    _edit(model, CONSUMER, mutate)


def _records(model: Path, rule: str) -> list[Any]:
    return [record for record in check_cross_layer_consistency(str(model)).records if record.rule == rule]


def _messages(model: Path, rule: str) -> list[str]:
    return [record.message for record in _records(model, rule)]


# Each rule's break, for the shared tests: a copy of the example, broken once


def _break_unknown_element(model: Path) -> None:
    _com_specs(model, CONSUMER, {"data_element": "absent"})


def _break_inapplicable(model: Path) -> None:
    _com_specs(model, PROVIDER, {"data_element": "value", "enable_update": True})


def _break_queued_receiver(model: Path) -> None:
    _queued(model)


def _break_disabled_mode(model: Path) -> None:
    _mode_stub(model)
    _disabled_mode(model, port="rAbsent")


def _break_queued_read(model: Path) -> None:
    _queued(model)
    _com_specs(model, CONSUMER, {"data_element": "value", "queue_length": 1})
    _edit(
        model, CONSUMER, lambda d: d["runnables"][0].update(reads=[{"port": "rIn", "element": "value", "by": "value"}])
    )


def _break_service_port(model: Path) -> None:
    _edit(model, INTERFACE, lambda d: d.update(is_service=True))
    _edit(model, ROOT, lambda d: d.update(ports=[{"name": "pRoot", "direction": "provide", "interface": "IExample"}]))


def _break_conversion(model: Path) -> None:
    write_model_element(model, {"name": "Speed", "kind": "application_type", "conversion": {"factor": 2}})


BREAKS: dict[str, tuple[Callable[[Path], None], str]] = {
    "swc.port.com-spec-unknown-element": (
        _break_unknown_element,
        "SWC 'ExampleConsumerSWC' port 'rIn' declares com_specs for data element 'absent', but sender/receiver "
        "interface 'IExample' declares no data element 'absent'.",
    ),
    "swc.port.com-spec-inapplicable": (
        _break_inapplicable,
        "SWC 'ExampleSWC' provide port 'pOut' states 'enable_update' for data element 'value', but a provide "
        "port's sender com-spec has no place for a receiver attribute.",
    ),
    "swc.port.queued-receiver-without-queue-length": (
        _break_queued_receiver,
        "SWC 'ExampleConsumerSWC' require port 'rIn' receives queued data element 'value' of interface 'IExample' "
        "and states no queue_length for it: a queued receiver com-spec carries QUEUE-LENGTH (multiplicity 1), "
        "which neither AUTOSAR nor SWCraft defaults. Add a com_specs entry for the element with a queue_length.",
    ),
    "swc.timing_event.disabled-mode-port": (
        _break_disabled_mode,
        "SWC 'ExampleConsumerSWC' runnable 'ExampleConsumerRun' timing event (period 0.01s) is disabled in mode "
        f"'{MODE_REF}' on port 'rAbsent', but the component declares no port 'rAbsent'.",
    ),
    "swc.access.queued-read-by-value": (
        _break_queued_read,
        "SWC 'ExampleConsumerSWC' runnable 'ExampleConsumerRun' reads queued data element 'value' through port "
        "'rIn' by value; a queued element is received by argument only. Remove `by: value` from the read.",
    ),
    "composition.port-service-interface": (
        _break_service_port,
        "Composition 'Root' port 'pRoot' is typed by interface 'IExample', which declares is_service: true; a "
        "composition may not own a port typed by a service interface.",
    ),
    "data-type.conversion-without-unit": (
        _break_conversion,
        "Application type 'Speed' declares a conversion and no unit: SWCraft writes the conversion as a LINEAR "
        "compu method, which must refer to a unit. Declare the type's unit.",
    ),
}


# The rules: declared, bound, answered


class TestTheRules:
    def test_the_breaks_cover_exactly_the_seven(self) -> None:
        assert set(BREAKS) == set(RULES)

    @pytest.mark.parametrize("rule", sorted(RULES))
    def test_declared_bound_and_answered_with_consistency_violation(self, rule: str) -> None:
        (declared,) = [entry for entry in load_contract()["rules"] if entry["id"] == f"rule:{rule}"]
        assert declared["family"] == "cross-element"
        assert declared["summary"] == CONSISTENCY_RULES[rule]
        module = importlib.import_module("swcraft_tools.validator")
        defining = importlib.import_module(vars(module)[RULES[rule]].__module__)
        bound = {"module": defining.__name__, "callable": BOUND_STEPS.get(rule, RULES[rule])}
        assert load_binding()["rules"][f"rule:{rule}"] == bound
        assert bound["callable"] in validator_package.reached_functions(defining, RULES[rule])
        assert RULE_CODES[rule].code == "consistency-violation"

    def test_the_contract_rule_counts_by_family(self) -> None:
        families = [entry["family"] for entry in load_contract()["rules"]]
        # Every rule the contract declares, by family; a rule added or removed moves them.
        assert len(families) == 352
        assert (families.count("cross-element"), families.count("element-local")) == (207, 144)

    def test_the_conversion_field_descriptions_say_a_conversion_needs_a_unit(self) -> None:
        (layer,) = [layer for layer in load_contract()["layers"] if layer["id"] == "layer:application_types"]
        (kind,) = [kind for kind in layer["kinds"] if kind["id"] == "kind:application_types.application_type"]
        fields = {field["name"]: field["description"] for field in kind["fields"]}
        assert "a type that declares a `conversion` must declare one" in fields["unit"]
        assert (
            "`check-model` reports a conversion without a unit and the ARXML writer refuses it"
            in (fields["conversion"])
        )


# Each rule on its offending shapes, and silent on the lawful ones


@pytest.mark.parametrize("rule", sorted(BREAKS))
def test_each_rule_fires_on_its_break_with_its_message(rule: str, tmp_path: Path) -> None:
    model = _basic(tmp_path) / "model"
    assert _records(model, rule) == []

    BREAKS[rule][0](model)

    assert _messages(model, rule) == [BREAKS[rule][1]]


class TestComSpecUnknownElement:
    RULE = "swc.port.com-spec-unknown-element"

    def test_a_declared_element_is_silent(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _com_specs(model, CONSUMER, {"data_element": "value", "enable_update": True})
        assert _records(model, self.RULE) == []

    def test_an_entry_on_a_client_server_port(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _edit(model, CONSUMER, lambda d: d["ports"][0].update(interface="IDiagnosticService"))
        _com_specs(model, CONSUMER, {"data_element": "ReadDtc"})
        (message,) = _messages(model, self.RULE)
        assert "its interface 'IDiagnosticService' is client/server" in message

    def test_an_entry_on_an_interface_stub_port(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _mode_stub(model, dest="SENDER-RECEIVER-INTERFACE")
        _edit(model, CONSUMER, lambda d: d["ports"][0].update(interface="IModes"))
        _com_specs(model, CONSUMER, {"data_element": "value"})
        (message,) = _messages(model, self.RULE)
        # The entry resolves against the stub's `data_elements`, empty here.
        assert "external interface stub 'IModes' does not list data element 'value' in its `data_elements`" in message

    def test_an_entry_on_an_undeclared_interface(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _edit(model, CONSUMER, lambda d: d["ports"][0].update(interface="INope"))
        _com_specs(model, CONSUMER, {"data_element": "value"})
        (message,) = _messages(model, self.RULE)
        assert "its interface 'INope' is not declared" in message

    def test_an_external_component_is_the_stub_rules(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _put(
            model,
            "swc/example/StubSWC.yaml",
            {
                "name": "StubSWC",
                "type": "application",
                "external": True,
                "external_ref": "/Foreign/ComponentTypes/StubSWC",
                "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
                "ports": [{"name": "rX", "direction": "require", "com_specs": [{"data_element": "x"}]}],
            },
        )
        assert _records(model, self.RULE) == []
        (message,) = _messages(model, "swc.external-with-ports")
        assert "port 'rX' declares com_specs" in message

    def test_the_subject_is_the_entry(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _break_unknown_element(model)
        (record,) = _records(model, self.RULE)
        (subject,) = record.subjects
        assert (subject.layer, subject.element) == ("swc", "ExampleConsumerSWC")
        assert [step.value for step in subject.path] == ["ports", "rIn", "com_specs", "absent"]


class TestComSpecInapplicable:
    RULE = "swc.port.com-spec-inapplicable"

    @pytest.mark.parametrize("attribute", ["handle_out_of_range", "uses_end_to_end_protection"])
    def test_the_two_attributes_every_side_carries_are_silent_on_a_provide_port(
        self, attribute: str, tmp_path: Path
    ) -> None:
        model = _basic(tmp_path) / "model"
        value: object = "saturate" if attribute == "handle_out_of_range" else True
        _com_specs(model, PROVIDER, {"data_element": "value", attribute: value})
        assert _records(model, self.RULE) == []

    def test_queue_length_on_an_element_that_is_not_queued(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _com_specs(model, CONSUMER, {"data_element": "value", "queue_length": 3})
        (message,) = _messages(model, self.RULE)
        assert "states 'queue_length' for data element 'value', but the element is not queued" in message

    @pytest.mark.parametrize(
        ("attribute", "value"),
        [
            ("enable_update", True),
            ("alive_timeout", 0.5),
            ("handle_never_received", True),
            ("handle_timeout_type", "replace"),
        ],
    )
    def test_a_non_queued_receiver_attribute_on_a_queued_element(
        self, attribute: str, value: object, tmp_path: Path
    ) -> None:
        model = _basic(tmp_path) / "model"
        _queued(model)
        _com_specs(model, CONSUMER, {"data_element": "value", "queue_length": 2, attribute: value})
        (message,) = _messages(model, self.RULE)
        assert f"states '{attribute}' for data element 'value', but the element is queued" in message
        assert _records(model, "swc.port.queued-receiver-without-queue-length") == []

    def test_every_misplaced_attribute_is_named_in_one_finding(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _com_specs(model, PROVIDER, {"data_element": "value", "enable_update": True, "queue_length": 1})
        (message,) = _messages(model, self.RULE)
        assert "states 'enable_update' and 'queue_length'" in message

    def test_the_lawful_receiver_attributes_are_silent(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _com_specs(
            model,
            CONSUMER,
            {
                "data_element": "value",
                "enable_update": True,
                "alive_timeout": 0.1,
                "handle_never_received": True,
                "handle_timeout_type": "replace",
                "handle_out_of_range": "default",
                "uses_end_to_end_protection": True,
            },
        )
        assert _records(model, self.RULE) == []


class TestQueuedReceiverWithoutQueueLength:
    RULE = "swc.port.queued-receiver-without-queue-length"

    def test_a_queue_length_is_silent(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _queued(model)
        _com_specs(model, CONSUMER, {"data_element": "value", "queue_length": 4})
        assert _records(model, self.RULE) == []

    def test_the_provide_port_is_never_read(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _queued(model)
        assert [message.split("'")[1] for message in _messages(model, self.RULE)] == ["ExampleConsumerSWC"]

    def test_an_explicit_standard_policy_is_not_queued(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _edit(model, INTERFACE, lambda d: d["data_elements"][0].update(sw_impl_policy="standard"))
        assert _records(model, self.RULE) == []


class TestDisabledModePort:
    RULE = "swc.timing_event.disabled-mode-port"

    def test_a_require_port_on_a_mode_switch_stub_is_silent(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _mode_stub(model)
        _disabled_mode(model)
        assert _records(model, self.RULE) == []

    @pytest.mark.parametrize(
        ("shape", "fragment"),
        [
            ({"direction": "provide"}, "port 'rMode' is a provide port, and a mode arrives through a require port"),
            ({"interface": "IExample"}, "port 'rMode' is typed by 'IExample', which is not an external interface stub"),
            ({"interface": None}, "port 'rMode' declares no interface, which is not an external interface stub"),
            (
                {"group": f"{MODE_STUB_REF}/Nested/Mode"},
                f"mode_group_ref '{MODE_STUB_REF}/Nested/Mode' does not lie directly under the external_ref "
                f"'{MODE_STUB_REF}' of interface 'IModes'",
            ),
        ],
        ids=["provide", "declared-interface", "no-interface", "group-not-a-child"],
    )
    def test_each_offending_shape(self, shape: dict[str, Any], fragment: str, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _mode_stub(model)
        _disabled_mode(model, **shape)
        (message,) = _messages(model, self.RULE)
        assert fragment in message

    def test_a_stub_of_another_kind(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _mode_stub(model, dest="SENDER-RECEIVER-INTERFACE")
        _disabled_mode(model)
        (message,) = _messages(model, self.RULE)
        assert "whose external_dest is MODE-SWITCH-INTERFACE" in message

    def test_the_subject_is_the_event_by_its_full_identity(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _break_disabled_mode(model)
        (record,) = _records(model, self.RULE)
        (subject,) = record.subjects
        assert [step.value for step in subject.path][:3] == ["runnables", "ExampleConsumerRun", "events"]
        assert subject.path[-1].value == {
            "type": "timing",
            "period": 0.01,
            "disabled_modes": [{"port": "rAbsent", "mode_group_ref": MODE_GROUP_REF, "mode_ref": MODE_REF}],
        }


class TestQueuedReadByValue:
    RULE = "swc.access.queued-read-by-value"

    def test_a_read_by_argument_is_silent(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _break_queued_read(model)
        _edit(model, CONSUMER, lambda d: d["runnables"][0]["reads"][0].pop("by"))
        assert _records(model, self.RULE) == []

    def test_a_by_value_read_of_an_element_that_is_not_queued_is_silent(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _edit(
            model,
            CONSUMER,
            lambda d: d["runnables"][0].update(reads=[{"port": "rIn", "element": "value", "by": "value"}]),
        )
        assert _records(model, self.RULE) == []

    def test_an_unresolved_read_is_the_access_rules(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _queued(model)
        _edit(
            model,
            CONSUMER,
            lambda d: d["runnables"][0].update(reads=[{"port": "rIn", "element": "nope", "by": "value"}]),
        )
        assert _records(model, self.RULE) == []
        assert _records(model, "swc.access.unknown-element") != []


class TestServiceCompositionPort:
    RULE = "composition.port-service-interface"

    def test_a_port_on_a_non_service_interface_is_silent(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _break_service_port(model)
        _edit(model, INTERFACE, lambda d: d.pop("is_service"))
        assert _records(model, self.RULE) == []

    def test_a_client_server_service_interface(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _edit(model, "interfaces/IDiagnosticService.yaml", lambda d: d.update(is_service=True))
        _edit(
            model,
            ROOT,
            lambda d: d.update(ports=[{"name": "pDiag", "direction": "provide", "interface": "IDiagnosticService"}]),
        )
        (message,) = _messages(model, self.RULE)
        assert "port 'pDiag' is typed by interface 'IDiagnosticService'" in message


class TestConversionWithoutUnit:
    RULE = "data-type.conversion-without-unit"

    @pytest.mark.parametrize("conversion", [{}, {"factor": 2, "offset": 1}], ids=["identity", "linear"])
    def test_every_declared_conversion_needs_a_unit(self, conversion: dict[str, Any], tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        write_model_element(model, {"name": "Speed", "kind": "application_type", "conversion": conversion})
        assert len(_records(model, self.RULE)) == 1

    def test_a_blank_unit_is_no_unit(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        write_model_element(
            model, {"name": "Speed", "kind": "application_type", "unit": "   ", "conversion": {"factor": 2}}
        )
        assert len(_records(model, self.RULE)) == 1

    @pytest.mark.parametrize(
        "document",
        [
            {"name": "Speed", "kind": "application_type", "unit": "kph", "conversion": {"factor": 2}},
            {"name": "Speed", "kind": "application_type", "range": {"max": 4}},
        ],
        ids=["with-unit", "no-conversion"],
    )
    def test_silent_otherwise(self, document: dict[str, Any], tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _put(model, model_element_storage_name("Speed", kind=document["kind"]), document)
        assert _records(model, self.RULE) == []


# check-model, at both manifest versions


def _check_model(project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", "check-model", "--project", str(project / "swcraft.yaml")],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
    )


@pytest.mark.parametrize("version", [str(version) for version in ArxmlVersion])
@pytest.mark.parametrize("rule", sorted(BREAKS))
def test_check_model_lists_each_rule_at_each_version(rule: str, version: str, tmp_path: Path) -> None:
    """None of the seven reads the version: each is listed alike at 4.2.2 and 4.3.0."""
    project = _basic(tmp_path)
    manifest = _load(project / "swcraft.yaml")
    manifest["arxml_version"] = version
    _put(project, "swcraft.yaml", manifest)
    BREAKS[rule][0](project / "model")

    result = _check_model(project)

    assert result.returncode == 1, result.stdout
    assert BREAKS[rule][1] in result.stderr, result.stderr


# Pass D


def _approve(proposal: Any) -> ApprovalDecision:
    return ApprovalDecision(
        change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=True
    )


def _change_set(project: Path, operations: list[dict[str, Any]]) -> dict[str, Any]:
    token = handle_request({"operation": "discover"}, project=str(project))["baseline"]
    return handle_request(
        {"operation": "change_set", "baseline": token, "intent": "behavioural values", "operations": operations},
        project=str(project),
        review=_approve,
    )


def _at(layer: str, element: str, *path: dict[str, Any]) -> dict[str, Any]:
    return {"layer": layer, "element": element, "path": list(path)}


def _port_com_spec(swc: str, port: str, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation": "create",
        "address": _at(
            "swc", swc, {"field": "ports"}, {"member": port}, {"field": "com_specs"}, {"member": entry["data_element"]}
        ),
        "value": entry,
    }


_QUEUED = {
    "operation": "update",
    "address": _at(
        "interfaces", "IExample", {"field": "data_elements"}, {"member": "value"}, {"field": "sw_impl_policy"}
    ),
    "value": "queued",
}

#: One change set per rule that introduces it into the example.
CHANGE_SETS: dict[str, list[dict[str, Any]]] = {
    "swc.port.com-spec-unknown-element": [_port_com_spec("ExampleConsumerSWC", "rIn", {"data_element": "absent"})],
    "swc.port.com-spec-inapplicable": [
        _port_com_spec("ExampleSWC", "pOut", {"data_element": "value", "enable_update": True})
    ],
    "swc.port.queued-receiver-without-queue-length": [_QUEUED],
    "swc.timing_event.disabled-mode-port": [
        {
            "operation": "create",
            "address": _at(
                "swc",
                "ExampleConsumerSWC",
                {"field": "runnables"},
                {"member": "ExampleConsumerRun"},
                {"field": "events"},
                {
                    "member": {
                        "type": "timing",
                        "period": 0.02,
                        "disabled_modes": [{"port": "rAbsent", "mode_group_ref": MODE_GROUP_REF, "mode_ref": MODE_REF}],
                    }
                },
            ),
            "value": {
                "type": "timing",
                "period": 0.02,
                "disabled_modes": [{"port": "rAbsent", "mode_group_ref": MODE_GROUP_REF, "mode_ref": MODE_REF}],
            },
        }
    ],
    "swc.access.queued-read-by-value": [
        _QUEUED,
        _port_com_spec("ExampleConsumerSWC", "rIn", {"data_element": "value", "queue_length": 1}),
        {
            "operation": "create",
            "address": _at(
                "swc",
                "ExampleConsumerSWC",
                {"field": "runnables"},
                {"member": "ExampleConsumerRun"},
                {"field": "reads"},
                {"member": {"port": "rIn", "element": "value"}},
            ),
            "value": {"port": "rIn", "element": "value", "by": "value"},
        },
    ],
    "composition.port-service-interface": [
        {"operation": "update", "address": _at("interfaces", "IExample", {"field": "is_service"}), "value": True},
        {
            "operation": "create",
            "address": _at("compositions", "Root", {"field": "ports"}, {"member": "pRoot"}),
            "value": {"name": "pRoot", "direction": "provide", "interface": "IExample"},
        },
    ],
    "data-type.conversion-without-unit": [
        {
            "operation": "create",
            "address": {"layer": "application_types", "element": "Speed"},
            "value": {"name": "Speed", "kind": "application_type", "conversion": {"factor": 2}},
        }
    ],
}


def test_the_change_sets_cover_exactly_the_seven() -> None:
    assert set(CHANGE_SETS) == set(RULES)


@pytest.mark.parametrize("rule", sorted(CHANGE_SETS))
def test_pass_d_refuses_a_change_set_that_introduces_each(rule: str, tmp_path: Path) -> None:
    project = _basic(tmp_path)
    before = files_of(project / "model")

    response = _change_set(project, CHANGE_SETS[rule])

    assert response["response"] == "error", response
    assert response["error"] == "consistency-violation", response
    messages = [response["message"], *(entry["message"] for entry in response.get("additional", []))]
    # The created event is a second one, at 0.02 s, beside the example's own.
    expected = BREAKS[rule][1].replace("(period 0.01s)", "(period 0.02s)")
    assert any(message.startswith(expected) for message in messages), response
    assert files_of(project / "model") == before


# The widened compatibility


def _twin(model: Path, **changes: Any) -> None:
    """`IExample2`: `IExample` with *changes* applied, its data element's first."""
    document = _load(model / INTERFACE)
    document["name"] = "IExample2"
    element_changes = changes.pop("element", {})
    document["data_elements"][0].update(element_changes)
    document.update(changes)
    _put(model, "interfaces/IExample2.yaml", document)


DIFFERENCES: dict[str, tuple[dict[str, Any], str]] = {
    "is_service": ({"is_service": True}, "is_service differs: true vs false"),
    "queued": ({"element": {"sw_impl_policy": "queued"}}, "data element 'value': sw_impl_policy queued vs not queued"),
}


class TestTheConnectorRule:
    RULE = "connector.interface-mismatch"

    @pytest.mark.parametrize("difference", sorted(DIFFERENCES))
    def test_a_pair_differing_only_there_is_a_mismatch(self, difference: str, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        changes, subject = DIFFERENCES[difference]
        _twin(model, **changes)
        _edit(model, CONSUMER, lambda d: d["ports"][0].update(interface="IExample2"))
        (message,) = _messages(model, self.RULE)
        assert message.endswith(f": {subject}."), message

    def test_two_absent_values_are_compatible(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _twin(model)
        _edit(model, CONSUMER, lambda d: d["ports"][0].update(interface="IExample2"))
        assert _records(model, self.RULE) == []

    def test_an_explicit_standard_against_an_absent_policy_is_compatible(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _twin(model, element={"sw_impl_policy": "standard"})
        _edit(model, CONSUMER, lambda d: d["ports"][0].update(interface="IExample2"))
        assert _records(model, self.RULE) == []


def _delegated(model: Path, outer_interface: str) -> None:
    """The root's require port `rRoot`, typed by *outer_interface*, delegated onto a third component's `rIn`."""
    _put(
        model,
        "swc/example/SinkSWC.yaml",
        {
            "name": "SinkSWC",
            "type": "application",
            "ports": [{"name": "rIn", "direction": "require", "interface": "IExample"}],
            "memory": {"RAM0": 16},
            "handle_termination_and_restart": "no_support",
        },
    )

    def mutate(document: dict[str, Any]) -> None:
        document["members"].append({"name": "SinkSWC", "type": "SinkSWC"})
        document["ports"] = [{"name": "rRoot", "direction": "require", "interface": outer_interface}]
        document["delegations"] = [{"name": "dIn", "port": "rRoot", "member": "SinkSWC", "member_port": "rIn"}]

    _edit(model, ROOT, mutate)


class TestTheDelegationRule:
    RULE = "composition.delegation-interface-mismatch"

    @pytest.mark.parametrize("difference", sorted(DIFFERENCES))
    def test_a_pair_differing_only_there_is_a_mismatch(self, difference: str, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        changes, subject = DIFFERENCES[difference]
        _twin(model, **changes)
        _delegated(model, "IExample2")
        # The inner port's interface is the required side of a require delegation.
        inner, outer = ("false", "true") if difference == "is_service" else ("not queued", "queued")
        expected = subject.replace("true vs false", f"{inner} vs {outer}").replace(
            "queued vs not queued", f"{inner} vs {outer}"
        )
        (message,) = _messages(model, self.RULE)
        assert expected in message, message

    def test_two_absent_values_are_compatible(self, tmp_path: Path) -> None:
        model = _basic(tmp_path) / "model"
        _twin(model)
        _delegated(model, "IExample2")
        assert _records(model, self.RULE) == []


# No committed tree reports a finding of any of them


#: The one listed tree that does not load, and the words it is refused with: the editor-schema
#: fixture of documents the editor accepts and the loader does not.
_UNLOADABLE_TREE = ("packages/tools/tests/fixtures/editor_schema/accepted_but_invalid", "duplicate reads entry")


_WIDENED_SUBJECTS = ("is_service differs", "sw_impl_policy")


def test_no_committed_tree_reports_a_finding_of_the_seven_or_of_the_widening() -> None:
    """No committed model tree reports a finding of the seven rules or of the widened ones.

    Does not reach the one tree that does not load (an editor-schema fixture of
    deliberately invalid documents), held to its parse error by name; any other
    exception fails.
    """
    roots = committed_model_roots()
    unloadable, words = _UNLOADABLE_TREE
    judged = 0
    for root in roots:
        if tree_id(root) == unloadable:
            with pytest.raises(ElementParseError, match=words):
                check_cross_layer_consistency(str(root))
            continue
        records = check_cross_layer_consistency(str(root)).records
        judged += 1
        new = [record.message for record in records if record.rule in RULES]
        widened = [
            record.message
            for record in records
            if record.rule in ("connector.interface-mismatch", "composition.delegation-interface-mismatch")
            and any(subject in record.message for subject in _WIDENED_SUBJECTS)
        ]
        assert new == [] and widened == [], tree_id(root)
    assert judged == len(roots) - 1, judged
    assert judged >= 15, judged


def test_each_partial_tree_base_still_holds_listed_files() -> None:
    """An exemption from the gate above names a base that is still there, so it exempts something."""
    for base in PARTIAL_TREE_BASES:
        assert base.endswith("/"), base
        assert listed_files(base.rstrip("/")), f"{base} holds no listed file: drop it from PARTIAL_TREE_BASES"
