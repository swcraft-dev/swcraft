"""The generic element writer.

Three groups of question, in the order they matter:

* **Byte-for-byte reproduction.** Writing an *unmodified* element must reproduce
  its file exactly, for every layer and element kind. It is a stronger and far
  cheaper check than a round trip through the reader — it fails on a changed key
  order, a dropped optional field, a differently rendered float — and it is the
  only thing that catches a divergence between ``elements.element_to_dict``'s
  derived serialization and the hand-written
  ``yaml_documents.swc_to_dict``/``task_to_dict`` style. The corpus is authored
  **through** ``mutations.py`` wherever a public ``create_*`` exists, in two
  variants (every optional field set, and none of them), and the set of element
  kinds it covers is *derived from the registry*: a new top-level kind fails this
  file until the corpus grows. A kind no ``create_*`` builds is hand-authored in
  the writer's own layout; :data:`_RICH_COMPOSITION` says why that is the
  stronger fixture rather than a shortcut.
* **The path guard.** A target that is not a composition of single path segments
  is refused before any filesystem access, and no write can land outside
  ``model_root``.
* **The write contract.** A ``create`` never overwrites; a non-conforming
  emitted ``SHORT-NAME`` is refused and nothing is written; a removal leaves the
  now-empty directory in place; each call emits exactly one audit entry.

The completeness half — that every element kind is *classified* as name-writing
or not — is ``test_identifier_coverage.py``'s.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import at_most_steps, model_element_path
from pydantic import BaseModel, TypeAdapter
from swcraft_tools.contract import load_binding, load_contract
from swcraft_tools.editing.registry import build_registry
from swcraft_tools.model_registry import (
    FieldFacts,
    ModelRegistry,
    build_model_registry,
    production_registry,
    use_registry,
)
from swcraft_tools.model_state import scan_layer
from swcraft_tools.schema import (
    ApplicationDataType,
    BaseDataType,
    Composition,
    DataEvent,
    Requirement,
    SWComponent,
    Task,
)
from swcraft_tools.writer import elements, mutations, yaml_documents
from swcraft_tools.writer.elements import (
    READ_BACK_MISMATCH,
    UNEXPECTED_EXCEPTION,
    EditedText,
    _address_of,
    _writer_args,
    edited_text,
    element_to_dict,
    remove_element,
    undeclared_keys_of,
    write_element,
)
from swcraft_tools.yaml_load import load_yaml_document

# The four emitted name sites of one composition file, named by the module that
# owns the enumeration rather than restated: one table, three consumers.
from test_writer_compositions import COMPOSITION_NAME_SITES, CompositionNameSite, one_of_each_site

# ---------------------------------------------------------------------------
# The corpus, authored through mutations.py
# ---------------------------------------------------------------------------


#: The one composition of the rich corpus, authored as **bytes**: the layer has no
#: public ``create_*``, so there is no second style to disagree with, and a file
#: the writer produced would only witness its own idempotence. Written in the
#: writer's own layout (``yaml.safe_dump(default_flow_style=False,
#: sort_keys=False)``), for :data:`_SWC_WITHOUT_MIN_START_INTERVAL`'s reason.
#:
#: Every optional field is set, including ``root``, and the two members exercise
#: both halves of the "exactly one of ``type`` and ``composition``" rule — the
#: unset one is absent from the file, which is what ``element_to_dict`` does
#: with a ``None``.
_RICH_COMPOSITION = (
    "name: Vehicle\n"
    "title: The vehicle composition\n"
    "description: Every optional field set.\n"
    "root: true\n"
    "ports:\n"
    "- name: pSpeed\n"
    "  direction: provide\n"
    "  interface: ISpeed\n"
    "  description: The speed boundary port\n"
    "members:\n"
    "- name: sensor\n"
    "  type: Sensor\n"
    "  description: The sensing component\n"
    "- name: inner\n"
    "  composition: Inner\n"
    "  description: A nested composition\n"
    "delegations:\n"
    "- name: dSpeed\n"
    "  port: pSpeed\n"
    "  member: sensor\n"
    "  member_port: pSpeed\n"
    "  description: Speed out\n"
)

#: The same layer with every optional field at its default. ``root`` is the
#: layer's one ``omitAtDefault`` field and is therefore absent; the three
#: collections are not, so they are written empty — the pair of facts
#: :class:`TestSerializationDetail` pins for the runnable, in the one other
#: layer that has both.
_MINIMAL_COMPOSITION = "name: Inner\ntitle: The inner composition\nports: []\nmembers: []\ndelegations: []\n"


def _write_composition(root: Path, document: str) -> None:
    """Place one hand-authored composition file, creating the layer directory."""
    directory = root / "compositions"
    directory.mkdir(exist_ok=True)
    name = document.partition("\n")[0].removeprefix("name: ")
    with open(directory / f"{name}.yaml", "w", encoding="utf-8", newline="\n") as handle:
        handle.write(document)


def _build_rich(root: Path) -> None:
    """Every element kind, with every optional field set to a non-default value."""
    r = str(root)

    mutations.create_hardware_element(r, "CPU", "processor", description="The application processor")
    mutations.create_hardware_element(
        r, "CORE0", "core", threshold=0.7, hw_id=0, enforcement="advisory", description="Application core"
    )
    mutations.create_hardware_element(
        r,
        "RAM",
        "memory",
        threshold=0.8,
        memory_type="RAM",
        size=65536,
        accessible=["CORE0"],
        enforcement="strict",
        description="On-chip RAM",
    )
    # A partition, every optional field set.
    mutations.create_hardware_element(
        r,
        "PARTITION0",
        "partition",
        core="CORE0",
        asil="B",
        coexistence="The QM component is separated by the memory protection unit",
        description="The application partition",
    )

    mutations.create_data_type(
        r, "uint8", "base_type", size=8, encoding="unsigned", native_declaration="unsigned char", description="A byte"
    )
    mutations.create_data_type(
        r, "Speed", "value_type", base="uint8", unit="km/h", value_min=0.0, value_max=250.0, description="Vehicle speed"
    )
    # `create_data_type` builds no reference type, so the file is written the way
    # the generic writer writes one: declaration order, the empty default omitted.
    (model_element_path(root, "WheelSpeed", kind="reference_type")).write_text(
        "name: WheelSpeed\nkind: reference_type\nbase: Speed\ndescription: Another name for Speed\n",
        encoding="utf-8",
        newline="\n",
    )
    mutations.create_data_type(
        r, "Mode", "enum", literals=[{"name": "Off", "value": 0}, {"name": "On", "value": 1}], description="A mode"
    )
    # `create_data_type` builds no application type, so the file is written the
    # way the generic writer writes one: declaration order, every optional field set.
    (model_element_path(root, "Distance", kind="application_type")).write_text(
        "name: Distance\nkind: application_type\nunit: m\nconversion:\n  factor: 0.1\n  offset: -40.0\n"
        "range:\n  min: 0.0\n  max: 400.0\ndescription: A distance\n",
        encoding="utf-8",
        newline="\n",
    )
    mutations.create_data_type(
        r,
        "Point",
        "record",
        fields=[{"name": "x", "type": "Distance"}, {"name": "y", "type": "Distance"}],
        description="A point",
    )
    mutations.create_data_type(r, "Buffer", "array", element_type="Distance", length=8, description="A buffer")
    # The external stubs, the implementation kinds and the data description
    # elements below: no `create_*` builds them, so each is hand-written the same
    # way.
    (model_element_path(root, "ForeignT", kind="external_type")).write_text(
        "name: ForeignT\nkind: external_type\nexternal_ref: /Foreign/ImplementationDataTypes/ForeignT\n"
        "external_dest: IMPLEMENTATION-DATA-TYPE\ndescription: Declared by another file of the delivery\n",
        encoding="utf-8",
        newline="\n",
    )
    (model_element_path(root, "Frame", kind="implementation_record")).write_text(
        "name: Frame\nkind: implementation_record\nelements:\n- name: speed\n  type: Speed\n"
        "- name: wheel\n  type: WheelSpeed\ndescription: A stored frame\n",
        encoding="utf-8",
        newline="\n",
    )
    (model_element_path(root, "Frames", kind="implementation_array")).write_text(
        "name: Frames\nkind: implementation_array\nelement_type: Frame\nlength: 4\nelement_name: Element\n"
        "description: Four frames\n",
        encoding="utf-8",
        newline="\n",
    )
    (model_element_path(root, "FramesPtr", kind="pointer")).write_text(
        "name: FramesPtr\nkind: pointer\ntarget: Frames\ndescription: Where the frames are\n",
        encoding="utf-8",
        newline="\n",
    )
    # The value type naming a texttable goes through `create_data_type`, which
    # takes it.
    (model_element_path(root, "Kmh", layer="units")).parent.mkdir(parents=True, exist_ok=True)
    (model_element_path(root, "Kmh", layer="units")).write_text(
        "name: Kmh\ndisplay_name: km/h\ndescription: Kilometres per hour\n", encoding="utf-8", newline="\n"
    )
    (model_element_path(root, "GearTable", kind="texttable")).parent.mkdir(parents=True, exist_ok=True)
    (model_element_path(root, "GearTable", kind="texttable")).write_text(
        "name: GearTable\nkind: texttable\nentries:\n- name: Park\n  value: 0\n  description: Parked\n"
        "- name: Drive\n  value: 1\n  description: Driving\ndescription: The gears\n",
        encoding="utf-8",
        newline="\n",
    )
    (model_element_path(root, "SpeedCompu", kind="linear")).write_text(
        "name: SpeedCompu\nkind: linear\nunit: Kmh\nfactor: 0.1\noffset: -40.0\ndescription: The speed scaling\n",
        encoding="utf-8",
        newline="\n",
    )
    (model_element_path(root, "SpeedLimits", layer="data_constraints")).parent.mkdir(parents=True, exist_ok=True)
    (model_element_path(root, "SpeedLimits", layer="data_constraints")).write_text(
        "name: SpeedLimits\nmin: 0.0\nmax: 400.0\nlevel: 0\ndescription: The physical limits\n",
        encoding="utf-8",
        newline="\n",
    )
    (model_element_path(root, "SpeedPhys", kind="application_type")).write_text(
        "name: SpeedPhys\nkind: application_type\nunit_ref: Kmh\ncompu_method_ref: SpeedCompu\n"
        "data_constr_ref: SpeedLimits\ndescription: A speed by its three elements\n",
        encoding="utf-8",
        newline="\n",
    )
    mutations.create_data_type(r, "Gear", "enum", description="A gear", compu_method_ref="GearTable")
    mutations.create_data_type(
        r, "GearValue", "value_type", base="uint8", description="A stored gear", compu_method_ref="GearTable"
    )
    (model_element_path(root, "GearRef", kind="reference_type")).write_text(
        "name: GearRef\nkind: reference_type\nbase: GearValue\ncompu_method_ref: GearTable\n"
        "description: Another name for GearValue\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "interfaces").mkdir(exist_ok=True)
    (root / "interfaces" / "IForeign.yaml").write_text(
        "name: IForeign\nkind: external_interface\nexternal_ref: /Foreign/PortInterfaces/IForeign\n"
        "external_dest: SENDER-RECEIVER-INTERFACE\ndescription: Declared by another file of the delivery\n",
        encoding="utf-8",
        newline="\n",
    )
    model_element_path(root, "Stored", layer="mapping_sets").parent.mkdir(parents=True, exist_ok=True)
    (model_element_path(root, "Stored", layer="mapping_sets")).write_text(
        "name: Stored\nmaps:\n- application: Distance\n  implementation: Speed\n"
        "description: Where the physical view is stored\n",
        encoding="utf-8",
        newline="\n",
    )

    mutations.create_interface(
        r,
        "ISpeed",
        [{"name": "value", "implementation_type": "Speed"}, {"name": "valid", "implementation_type": "uint8"}],
        description="Speed",
    )
    mutations.create_cs_interface(
        r,
        "IDiag",
        [
            {"name": "ReadDtc", "arguments": [{"name": "dtc", "direction": "in", "implementation_type": "uint8"}]},
            {"name": "ClearDtc", "arguments": []},
        ],
        description="Diagnostics",
    )

    mutations.create_swc(r, "Sensor", "sensors", "application", None, {"RAM": 2048})
    mutations.add_port(r, "Sensor", "pSpeed", "provide", "ISpeed")
    mutations.add_runnable(r, "Sensor", "SensorRun", 0.005)
    mutations.add_timing_event(r, "Sensor", "SensorRun", 0.01)
    mutations.add_runnable(r, "Sensor", "SensorInit")
    mutations.add_init_event(r, "Sensor", "SensorInit")

    mutations.create_swc(r, "Control", "control")
    mutations.add_port(r, "Control", "rSpeed", "require", "ISpeed")
    mutations.add_runnable(r, "Control", "ControlRun")

    mutations.create_connector(r, "Sensor_to_Control", "Sensor", "pSpeed", "Control", "rSpeed", "The speed link")

    mutations.create_requirement(r, "REQ_001", "The speed shall be sampled", "Full prose.", "spec")
    mutations.add_trace_link(r, "REQ_001", "Sensor")
    mutations.add_trace_link(r, "REQ_001", "Control")

    mutations.create_task(r, "Task_10ms", 0.01, 5, "PARTITION0", "10 ms task", None, "1200us")
    mutations.map_runnable_to_task(r, "Task_10ms", "Sensor", "SensorRun")
    mutations.map_runnable_to_task(r, "Task_10ms", "Control", "ControlRun")

    mutations.create_sequence(
        r,
        "speed_cycle",
        "Task_10ms",
        [
            {"swc": "Sensor", "runnable": "SensorRun", "note": "samples"},
            {"swc": "Control", "runnable": "ControlRun", "note": "actuates"},
        ],
        title="The speed cycle",
    )

    _write_composition(root, _RICH_COMPOSITION)


def _build_minimal(root: Path) -> None:
    """Every element kind, with every optional field left at its default.

    The other half of the omission rule: ``element_to_dict`` drops a field
    holding an omittable default and keeps ``Task.partition``/``SWComponent.ports``
    at the same falsy value, so a corpus that only ever set them would prove
    neither.
    """
    r = str(root)

    mutations.create_hardware_element(r, "CPU", "processor")
    mutations.create_hardware_element(r, "CORE0", "core", threshold=0.7)
    mutations.create_hardware_element(
        r, "RAM", "memory", threshold=0.8, memory_type="RAM", size=65536, accessible=["CORE0"]
    )
    mutations.create_hardware_element(r, "PARTITION0", "partition", core="CORE0")

    mutations.create_data_type(r, "uint8", "base_type", size=8, encoding="unsigned")
    mutations.create_data_type(r, "Speed", "value_type", base="uint8")
    (model_element_path(root, "WheelSpeed", kind="reference_type")).write_text(
        "name: WheelSpeed\nkind: reference_type\nbase: Speed\n", encoding="utf-8", newline="\n"
    )
    mutations.create_data_type(r, "Mode", "enum", literals=[{"name": "Off", "value": 0}])
    # An application type declaring no unit, no conversion and no range, and a
    # mapping set with no description, hand-written for `_build_rich`'s reason.
    (model_element_path(root, "Distance", kind="application_type")).write_text(
        "name: Distance\nkind: application_type\n", encoding="utf-8", newline="\n"
    )
    mutations.create_data_type(r, "Point", "record", fields=[{"name": "x", "type": "Distance"}])
    mutations.create_data_type(r, "Buffer", "array", element_type="Distance", length=8)
    # The two stubs with their reference pair left blank: that loads, and is
    # `check-model`'s finding rather than the schema's, so it is the stub at
    # every default.
    (model_element_path(root, "ForeignT", kind="external_type")).write_text(
        "name: ForeignT\nkind: external_type\n", encoding="utf-8", newline="\n"
    )
    # The three implementation kinds with no description, the one optional field.
    (model_element_path(root, "Frame", kind="implementation_record")).write_text(
        "name: Frame\nkind: implementation_record\nelements:\n- name: speed\n  type: Speed\n",
        encoding="utf-8",
        newline="\n",
    )
    (model_element_path(root, "Frames", kind="implementation_array")).write_text(
        "name: Frames\nkind: implementation_array\nelement_type: Frame\nlength: 1\nelement_name: Element\n",
        encoding="utf-8",
        newline="\n",
    )
    (model_element_path(root, "FramesPtr", kind="pointer")).write_text(
        "name: FramesPtr\nkind: pointer\ntarget: Frames\n", encoding="utf-8", newline="\n"
    )
    # The data description elements, every optional field left out.
    (model_element_path(root, "Kmh", layer="units")).parent.mkdir(parents=True, exist_ok=True)
    (model_element_path(root, "Kmh", layer="units")).write_text("name: Kmh\n", encoding="utf-8", newline="\n")
    (model_element_path(root, "GearTable", kind="texttable")).parent.mkdir(parents=True, exist_ok=True)
    (model_element_path(root, "GearTable", kind="texttable")).write_text(
        "name: GearTable\nkind: texttable\nentries:\n- name: Park\n  value: 0\n", encoding="utf-8", newline="\n"
    )
    (model_element_path(root, "SpeedCompu", kind="linear")).write_text(
        "name: SpeedCompu\nkind: linear\nunit: Kmh\nfactor: 1.0\noffset: 0.0\n", encoding="utf-8", newline="\n"
    )
    (model_element_path(root, "SpeedLimits", layer="data_constraints")).parent.mkdir(parents=True, exist_ok=True)
    (model_element_path(root, "SpeedLimits", layer="data_constraints")).write_text(
        "name: SpeedLimits\n", encoding="utf-8", newline="\n"
    )
    (root / "interfaces").mkdir(exist_ok=True)
    (root / "interfaces" / "IForeign.yaml").write_text(
        "name: IForeign\nkind: external_interface\n", encoding="utf-8", newline="\n"
    )
    model_element_path(root, "Stored", layer="mapping_sets").parent.mkdir(parents=True, exist_ok=True)
    (model_element_path(root, "Stored", layer="mapping_sets")).write_text(
        "name: Stored\nmaps:\n- application: Distance\n  implementation: Speed\n",
        encoding="utf-8",
        newline="\n",
    )

    mutations.create_interface(r, "ISpeed", [{"name": "value", "implementation_type": "Speed"}])
    mutations.create_cs_interface(r, "IDiag", [{"name": "ReadDtc"}])

    # No ports, no runnables, no memory, no description — the bare component.
    mutations.create_swc(r, "Sensor", "sensors")

    # A second and third component carry the endpoints the connector and the
    # sequence need; ``Sensor`` above stays bare so the empty-collection case is
    # still in the corpus.
    mutations.create_swc(r, "Producer", "sensors")
    mutations.add_port(r, "Producer", "pSpeed", "provide", "ISpeed")
    mutations.add_runnable(r, "Producer", "ProducerRun")
    mutations.create_swc(r, "Consumer", "control")
    mutations.add_port(r, "Consumer", "rSpeed", "require", "ISpeed")
    mutations.create_connector(r, "Producer_to_Consumer", "Producer", "pSpeed", "Consumer", "rSpeed")

    mutations.create_requirement(r, "REQ_001", "The speed shall be sampled", "Full prose.")

    # No wcet and no description — ``steps`` may not be empty, so the task
    # carries the one mapping the sequence needs.
    mutations.create_task(r, "Task_10ms", 0.01, 5, "PARTITION0")
    mutations.map_runnable_to_task(r, "Task_10ms", "Producer", "ProducerRun")

    mutations.create_sequence(r, "speed_cycle", "Task_10ms", [{"swc": "Producer", "runnable": "ProducerRun"}])

    _write_composition(root, _MINIMAL_COMPOSITION)


#: Directory name -> the layer's declared annotation, so a file is validated
#: through exactly the (possibly discriminated) union its layer declares rather
#: than through a class this test picked.
_LAYER_ADAPTERS = {
    layer.storage.directory: TypeAdapter(layer.element_annotation) for layer in production_registry().layers
}


def _layer_directory(target: str) -> str:
    """The declared layer directory *target* (a model-root-relative path) lies in.

    The longest declared directory that is a prefix of it: ``data_types/base``
    (the ``base_types`` layer) lies inside ``data_types``, so the first path
    segment does not name the layer.
    """
    return max((d for d in _LAYER_ADAPTERS if target.startswith(f"{d}/")), key=len)


#: Every top-level element kind the registry declares. Derived, because a
#: hand-listed corpus cannot notice a new kind.
_TOP_LEVEL_KINDS = frozenset(kind.kind for layer in build_registry().layers for kind in layer.top_level_kinds)


def _reproduce_every_file(model_root: Path) -> set[str]:
    """Rewrite every element in *model_root* unchanged; return the kinds covered."""
    registry = build_registry()
    covered: set[str] = set()
    for path in sorted((model_root.rglob("*.yaml")), key=lambda path: path.as_posix()):
        target = path.relative_to(model_root).as_posix()
        element = _LAYER_ADAPTERS[_layer_directory(target)].validate_python(
            yaml.safe_load(path.read_text(encoding="utf-8"))
        )
        before = path.read_bytes()

        write_element(str(model_root), target, element)

        assert path.read_bytes() == before, f"{target} was not reproduced byte-for-byte"
        descriptor = registry.kind_of_model(type(element))
        assert descriptor is not None, type(element)
        covered.add(descriptor.kind)
    return covered


#: One SWC file without ``Runnable.min_start_interval``: the key is simply absent.
#: Authored as bytes rather than produced by the writer, because a fixture the
#: writer generates cannot witness that the writer preserves what it did not
#: generate.
_SWC_WITHOUT_MIN_START_INTERVAL = (
    "name: SensorSwc\n"
    "type: application\n"
    "ports: []\n"
    "runnables:\n"
    "- name: SensorMain\n"
    "  events:\n"
    "  - type: timing\n"
    "    period: 0.02\n"
)


#: One SWC file whose runnable carries every access list, both exclusive area
#: lists over the component's two areas (one described, one not), and two
#: data-received events beside the timing event (one bare, one disabling a mode
#: and stating its name), in the writer's own layout; one read is ``by: value``
#: and the rest by argument. Authored as bytes for ``_SWC_WITHOUT_MIN_START_INTERVAL``'s reason:
#: the order and the absent ``by`` keys are the assertion.
_RUNNABLE_WITH_ACCESS_SWC = (
    "name: Control\n"
    "type: application\n"
    "ports:\n"
    "- name: rSpeed\n"
    "  direction: require\n"
    "  interface: ISpeed\n"
    "- name: pTorque\n"
    "  direction: provide\n"
    "  interface: ITorque\n"
    "- name: rDiag\n"
    "  direction: require\n"
    "  interface: IDiag\n"
    "runnables:\n"
    "- name: ControlRun\n"
    "  events:\n"
    "  - type: timing\n"
    "    period: 0.01\n"
    "  - type: data\n"
    "    port: rSpeed\n"
    "    element: value\n"
    "  - type: data\n"
    "    port: rSpeed\n"
    "    element: valid\n"
    "    disabled_modes:\n"
    "    - port: rMode\n"
    "      mode_group_ref: /Modes/EcuMode\n"
    "      mode_ref: /Modes/EcuMode/Sleep\n"
    "    name: DRT_SpeedValid\n"
    "  reads:\n"
    "  - port: rSpeed\n"
    "    element: value\n"
    "  - port: rSpeed\n"
    "    element: valid\n"
    "    by: value\n"
    "  writes:\n"
    "  - port: pTorque\n"
    "    element: valid\n"
    "  - port: pTorque\n"
    "    element: value\n"
    "  calls:\n"
    "  - port: rDiag\n"
    "    operation: ReadDtc\n"
    "  can_enter:\n"
    "  - area: DtcTable\n"
    "  runs_inside:\n"
    "  - area: ControlState\n"
    "  - area: DtcTable\n"
    "exclusive_areas:\n"
    "- name: ControlState\n"
    "  description: Guards the control state.\n"
    "- name: DtcTable\n"
)

#: The same component with no access list on its runnable.
_RUNNABLE_WITHOUT_ACCESS_SWC = (
    "name: Control\n"
    "type: application\n"
    "ports:\n"
    "- name: rSpeed\n"
    "  direction: require\n"
    "  interface: ISpeed\n"
    "runnables:\n"
    "- name: ControlRun\n"
    "  events:\n"
    "  - type: timing\n"
    "    period: 0.01\n"
)


def registry_field(kind: str, name: str) -> FieldFacts:
    """The kernel's facts for one declared field, by element kind."""
    facts = production_registry().field_facts(kind, name)
    assert facts is not None, f"{kind}.{name} is not a declared field"
    return facts


def _registry_without_the_declared_exception() -> ModelRegistry:
    """The shipped contract with every ``omitAtDefault`` declaration removed.

    The reverted mutation the byte assertions are graded against. It strips
    the key rather than setting it ``false``, because ``false`` and absent must
    behave alike. The count is asserted so that a further declaration, or a lost
    one, fails here beside the contract's own set gate rather than silently
    changing what the positive control below strips.
    """
    contract = copy.deepcopy(load_contract())
    stripped = [
        field.pop("omitAtDefault")
        for layer in contract["layers"]
        for kind in layer["kinds"]
        for field in kind["fields"]
        if "omitAtDefault" in field
    ]
    # The contract's `omitAtDefault` declarations; moves with each one added or removed.
    assert stripped == [True] * 38, stripped
    return build_model_registry(contract=contract, binding=load_binding())


@pytest.fixture
def rich_model(tmp_path: Path) -> Path:
    root = tmp_path / "model"
    root.mkdir()
    _build_rich(root)
    return root


@pytest.fixture
def minimal_model(tmp_path: Path) -> Path:
    root = tmp_path / "model"
    root.mkdir()
    _build_minimal(root)
    return root


class TestByteForByteReproduction:
    """The check that catches a serialization divergence, in both directions."""

    def test_every_rich_element_is_reproduced_and_every_kind_is_covered(self, rich_model: Path) -> None:
        assert _reproduce_every_file(rich_model) == set(_TOP_LEVEL_KINDS)

    def test_every_minimal_element_is_reproduced_and_every_kind_is_covered(self, minimal_model: Path) -> None:
        assert _reproduce_every_file(minimal_model) == set(_TOP_LEVEL_KINDS)

    @pytest.mark.parametrize("variant", ["rich_model", "minimal_model"])
    def test_the_corpus_covers_all_fifteen_layers(self, variant: str, request: pytest.FixtureRequest) -> None:
        root: Path = request.getfixturevalue(variant)
        directories = {_layer_directory(path.relative_to(root).as_posix()) for path in root.rglob("*.yaml")}
        declared = {layer.storage.directory for layer in production_registry().layers}
        assert len(declared) == 15
        assert declared == directories, sorted(declared.symmetric_difference(directories))

    def test_reproduction_is_idempotent(self, rich_model: Path) -> None:
        _reproduce_every_file(rich_model)
        _reproduce_every_file(rich_model)


class TestSerializationDetail:
    """The two halves of the omission rule, pinned individually.

    They are what a "drop every falsy value" implementation gets wrong, and the
    corpus above would only report as one opaque byte mismatch.
    """

    def test_a_field_at_an_omittable_default_is_left_out(self, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir()
        write_element(str(root), "swc/g/A.yaml", SWComponent(name="A"))
        data = yaml.safe_load((root / "swc" / "g" / "A.yaml").read_text(encoding="utf-8"))
        assert "description" not in data
        assert "memory" not in data

    def test_a_falsy_value_that_is_not_an_omittable_default_is_kept(self, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir()
        write_element(str(root), "tasks/T.yaml", Task(name="T", period=0.01, priority=0, partition="PARTITION0"))
        data = yaml.safe_load((root / "tasks" / "T.yaml").read_text(encoding="utf-8"))
        assert data["priority"] == 0
        assert data["runnables"] == []

    def test_a_required_empty_collection_is_still_written(self, tmp_path: Path) -> None:
        """``SWComponent.ports``, a required collection, is written even when empty."""
        root = tmp_path / "model"
        root.mkdir()
        write_element(str(root), "swc/g/A.yaml", SWComponent(name="A"))
        assert yaml.safe_load((root / "swc" / "g" / "A.yaml").read_text(encoding="utf-8"))["ports"] == []

    def test_an_empty_satisfies_and_derived_from_are_left_out_and_a_non_empty_one_written(self, tmp_path: Path) -> None:
        """Both trace lists are declared ``omitAtDefault``, so an unlinked element keeps its bytes."""
        root = tmp_path / "model"
        root.mkdir()
        plain = Requirement(id="R", title="T", description="D", version=1, source="architecture")
        derived = Requirement(
            id="S", title="T", description="D", version=2, source="architecture", derived_from=["R@1"]
        )
        write_element(str(root), "requirements/R.yaml", plain)
        write_element(str(root), "requirements/S.yaml", derived)
        write_element(str(root), "swc/g/A.yaml", SWComponent(name="A"))
        write_element(str(root), "swc/g/B.yaml", SWComponent(name="B", satisfies=["R@1"]))

        def load(relative: str) -> dict[str, Any]:
            return yaml.safe_load((root / relative).read_text(encoding="utf-8"))

        assert "derived_from" not in load("requirements/R.yaml")
        assert load("requirements/S.yaml")["derived_from"] == ["R@1"]
        assert "satisfies" not in load("swc/g/A.yaml")
        assert load("swc/g/B.yaml")["satisfies"] == ["R@1"]

    def test_the_derived_rule_still_emits_the_two_falsy_defaults(self, tmp_path: Path) -> None:
        """`Task.runnables` and `SWComponent.ports`, the reason "falsy" is not the rule.

        `_is_omitted_at_default`'s derived branch drops a default of `None`,
        `""` or `{}` and nothing else; the exceptions are declared one by one as
        `fieldDecl.omitAtDefault`. Both fields below hold a falsy default outside
        that set and are written out: a reader taking the key for "omit a falsy
        default" would erase `runnables` from every task file and `ports` from
        every component file.
        """
        root = tmp_path / "model"
        root.mkdir()
        write_element(str(root), "tasks/T.yaml", Task(name="T", period=0.01, priority=0, partition="PARTITION0"))
        write_element(str(root), "swc/g/A.yaml", SWComponent(name="A"))

        task = yaml.safe_load((root / "tasks" / "T.yaml").read_text(encoding="utf-8"))
        component = yaml.safe_load((root / "swc" / "g" / "A.yaml").read_text(encoding="utf-8"))
        assert task["runnables"] == [] and Task.model_fields["runnables"].default == []
        assert component["ports"] == [] and SWComponent.model_fields["ports"].default == []
        assert not registry_field("task", "runnables").omit_at_default
        assert not registry_field("swc", "ports").omit_at_default

    def test_a_runnable_file_without_min_start_interval_round_trips_byte_identically(self, tmp_path: Path) -> None:
        """A runnable file without `min_start_interval` round-trips byte-identically.

        Reading it fills the field with its `0.0` default, and writing the element
        back must not put the key into the file — that is what
        `fieldDecl.omitAtDefault` declares, and only a byte comparison catches the
        key arriving with an equal value.
        """
        root = tmp_path / "model"
        root.mkdir()
        (root / "swc" / "g").mkdir(parents=True)
        (root / "swc" / "g" / "SensorSwc.yaml").write_text(_SWC_WITHOUT_MIN_START_INTERVAL, encoding="utf-8")

        element = TypeAdapter(SWComponent).validate_python(yaml.safe_load(_SWC_WITHOUT_MIN_START_INTERVAL))
        assert element.runnables[0].min_start_interval == 0.0, "the round trip would be vacuous"

        write_element(str(root), "swc/g/SensorSwc.yaml", element)
        assert (root / "swc" / "g" / "SensorSwc.yaml").read_text(encoding="utf-8") == _SWC_WITHOUT_MIN_START_INTERVAL

    def test_without_the_declared_exception_the_key_comes_back(self, tmp_path: Path) -> None:
        """The positive control: the round trip above rests on the contract, not on luck.

        `0.0` is outside the derived `None` / `""` / `{}` rule, so with the
        declaration stripped from the contract the writer emits
        `min_start_interval: 0.0` into a file that never had the key. Without
        this, the byte assertion above would still pass against a serializer
        that had lost the exception for some unrelated reason.
        """
        root = tmp_path / "model"
        root.mkdir()
        element = TypeAdapter(SWComponent).validate_python(yaml.safe_load(_SWC_WITHOUT_MIN_START_INTERVAL))
        with use_registry(_registry_without_the_declared_exception()):
            write_element(str(root), "swc/g/SensorSwc.yaml", element)
        text = (root / "swc" / "g" / "SensorSwc.yaml").read_text(encoding="utf-8")
        assert "min_start_interval: 0.0" in text
        assert text != _SWC_WITHOUT_MIN_START_INTERVAL

    def test_an_aliased_field_is_written_under_its_alias(self, minimal_model: Path) -> None:
        text = (minimal_model / "connectors" / "Producer_to_Consumer.yaml").read_text(encoding="utf-8")
        assert "from:" in text
        assert "from_" not in text


class TestRunnableAccessRoundTrip:
    """`Runnable.reads` / `writes` / `calls` through `write_element`, byte for byte.

    A runnable that declares every list is reproduced with the lists in file
    order and `by` only where the file had it; one that declares none is
    reproduced without the keys, which rests on the contract's `omitAtDefault`
    exactly as `min_start_interval` does above.

    Does not reach `yaml_documents.runnable_to_dict`, the hand-written serializer
    the importer and the mutations write through;
    :class:`TestTheTwoWritersAgreeOnTheAccessLists` holds it to this one.
    """

    def test_a_runnable_with_every_list_is_reproduced_in_order(self, tmp_path: Path) -> None:
        root = tmp_path / "model"
        (root / "swc" / "g").mkdir(parents=True)
        (root / "swc" / "g" / "Control.yaml").write_text(_RUNNABLE_WITH_ACCESS_SWC, encoding="utf-8")

        element = TypeAdapter(SWComponent).validate_python(yaml.safe_load(_RUNNABLE_WITH_ACCESS_SWC))
        runnable = element.runnables[0]
        assert [read.by for read in runnable.reads] == [None, "value"], "the round trip would be vacuous"
        assert [write.element for write in runnable.writes] == ["valid", "value"], "file order, not sorted"

        write_element(str(root), "swc/g/Control.yaml", element)
        assert (root / "swc" / "g" / "Control.yaml").read_text(encoding="utf-8") == _RUNNABLE_WITH_ACCESS_SWC

    def test_a_runnable_without_lists_is_written_as_before_this_phase(self, tmp_path: Path) -> None:
        """No `reads: []`, `writes: []` or `calls: []` arrives in a file that never had them."""
        root = tmp_path / "model"
        (root / "swc" / "g").mkdir(parents=True)
        (root / "swc" / "g" / "Control.yaml").write_text(_RUNNABLE_WITHOUT_ACCESS_SWC, encoding="utf-8")

        element = TypeAdapter(SWComponent).validate_python(yaml.safe_load(_RUNNABLE_WITHOUT_ACCESS_SWC))
        assert (element.runnables[0].reads, element.runnables[0].writes, element.runnables[0].calls) == ([], [], [])

        write_element(str(root), "swc/g/Control.yaml", element)
        assert (root / "swc" / "g" / "Control.yaml").read_text(encoding="utf-8") == _RUNNABLE_WITHOUT_ACCESS_SWC


class TestTheTwoWritersAgreeOnTheAccessLists:
    """The hand-written and the generic serializer agree on a runnable with the three lists.

    `yaml_documents.runnable_to_dict` (which the importer and the mutations write
    through) and `elements.element_to_dict` must produce the same dict, and the
    bytes the round-trip class above pins. The corpus at the top of this module is
    the parity gate for *mutation-authored* elements; this class asks the two
    functions directly.

    Does not reach the mutations themselves (their preservation tests, one per
    mutation sharing `swc_to_dict`).
    """

    def test_a_runnable_with_every_list_serializes_identically_through_both(self) -> None:
        """The lists, the exclusive areas, the area lists and the data-received events serialize alike through both."""
        element = TypeAdapter(SWComponent).validate_python(yaml.safe_load(_RUNNABLE_WITH_ACCESS_SWC))
        runnable = element.runnables[0]
        assert [area.description for area in element.exclusive_areas] == ["Guards the control state.", ""]
        assert [entry.area for entry in runnable.can_enter] == ["DtcTable"], "the comparison would be vacuous"
        assert [entry.area for entry in runnable.runs_inside] == ["ControlState", "DtcTable"], "declared order"
        # One data-received event at every default and one populating every field, so the comparison reaches
        # every key of the kind.
        data_events = [event for event in runnable.events if isinstance(event, DataEvent)]
        assert [(event.element, len(event.disabled_modes), event.name) for event in data_events] == [
            ("value", 0, None),
            ("valid", 1, "DRT_SpeedValid"),
        ], "the comparison would be vacuous"
        hand_written = yaml_documents.swc_to_dict(element)
        generic = element_to_dict(element)
        assert hand_written == generic
        assert yaml.safe_dump(hand_written, default_flow_style=False, sort_keys=False) == _RUNNABLE_WITH_ACCESS_SWC

    def test_by_is_written_only_where_it_is_set(self) -> None:
        element = TypeAdapter(SWComponent).validate_python(yaml.safe_load(_RUNNABLE_WITH_ACCESS_SWC))
        reads = yaml_documents.runnable_to_dict(element.runnables[0])["reads"]
        assert reads == [{"port": "rSpeed", "element": "value"}, {"port": "rSpeed", "element": "valid", "by": "value"}]

    def test_a_runnable_without_lists_carries_no_list_key_through_either(self) -> None:
        """Both writers honour `omitAtDefault`, so a runnable without lists carries no list key through either."""
        element = TypeAdapter(SWComponent).validate_python(yaml.safe_load(_RUNNABLE_WITHOUT_ACCESS_SWC))
        hand_written = yaml_documents.swc_to_dict(element)
        assert hand_written == element_to_dict(element)
        assert set(hand_written["runnables"][0]) == {"name", "events"}
        assert yaml.safe_dump(hand_written, default_flow_style=False, sort_keys=False) == _RUNNABLE_WITHOUT_ACCESS_SWC


# ---------------------------------------------------------------------------
# The target guard
# ---------------------------------------------------------------------------

_ILLEGAL_TARGETS = [
    pytest.param("", id="empty"),
    pytest.param("   ", id="blank"),
    pytest.param("/abs/A.yaml", id="absolute"),
    pytest.param("../A.yaml", id="parent"),
    pytest.param("swc/../../A.yaml", id="traversal-mid-path"),
    pytest.param("swc//A.yaml", id="empty-segment"),
    pytest.param("swc/./A.yaml", id="dot-segment"),
    pytest.param("swc/..secret/A.yaml", id="embedded-dotdot"),
    pytest.param("swc\\group\\A.yaml", id="backslash"),
    pytest.param("swc/g\x00/A.yaml", id="nul"),
]


class TestTargetGuard:
    """A placement value reaches this module only as a path segment of *target*."""

    @pytest.mark.parametrize("target", _ILLEGAL_TARGETS)
    def test_an_illegal_target_is_refused_before_any_filesystem_access(self, target: str, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir()

        with pytest.raises(ValueError):
            write_element(str(root), target, SWComponent(name="A"))

        assert list(root.rglob("*")) == [], "a refused target must touch nothing at all"

    @pytest.mark.parametrize("target", _ILLEGAL_TARGETS)
    def test_remove_applies_the_same_guard(self, target: str, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir()
        with pytest.raises(ValueError):
            remove_element(str(root), target)

    def test_a_write_never_lands_outside_model_root(self, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir()
        sibling = tmp_path / "elsewhere"
        sibling.mkdir()

        with pytest.raises(ValueError, match="outside|single path segment|relative"):
            write_element(str(root), "../elsewhere/A.yaml", SWComponent(name="A"))

        assert list(sibling.rglob("*")) == []

    def test_a_non_string_target_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir()
        with pytest.raises(ValueError, match="must be a string"):
            write_element(str(root), 7, SWComponent(name="A"))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The write contract
# ---------------------------------------------------------------------------


class _RecordingAuditor:
    """The four-key ``AuditLog`` shape, captured rather than written."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def log_write(self, operation: str, args: dict, result: str) -> None:
        self.entries.append({"operation": operation, "args": args, "result": result})


class TestWriteContract:
    def test_a_create_never_overwrites_an_existing_file(self, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir()
        write_element(str(root), "swc/g/A.yaml", SWComponent(name="A"))
        before = (root / "swc" / "g" / "A.yaml").read_bytes()

        with pytest.raises(ValueError, match="already exists"):
            write_element(str(root), "swc/g/A.yaml", SWComponent(name="A", description="new"), create=True)

        assert (root / "swc" / "g" / "A.yaml").read_bytes() == before

    def test_a_write_without_create_replaces_the_file_whole(self, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir()
        write_element(str(root), "swc/g/A.yaml", SWComponent(name="A", description="old"))
        write_element(str(root), "swc/g/A.yaml", SWComponent(name="A"))
        assert "description" not in (root / "swc" / "g" / "A.yaml").read_text(encoding="utf-8")

    def test_the_directory_is_a_side_effect_of_the_write(self, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir()
        assert not (root / "swc" / "deep").exists()
        write_element(str(root), "swc/deep/A.yaml", SWComponent(name="A"))
        assert (root / "swc" / "deep").is_dir()

    def test_each_call_emits_exactly_one_audit_entry(self, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir()
        auditor = _RecordingAuditor()

        path = write_element(str(root), "swc/g/A.yaml", SWComponent(name="A"), auditor)
        remove_element(str(root), "swc/g/A.yaml", auditor, address=("swc", "A"))

        assert [entry["operation"] for entry in auditor.entries] == ["write_element", "remove_element"]
        assert [entry["args"] for entry in auditor.entries] == [
            {"target": "swc/g/A.yaml", "layer": "swc", "identity": "A"}
        ] * 2
        assert {entry["result"] for entry in auditor.entries} == {path}

    def test_the_audit_entry_records_the_address_the_file_name_does_not_carry(self, tmp_path: Path) -> None:
        # A file name equals the identity only when nothing collides, so the
        # address an entry stands for is recorded beside the target and never
        # re-derived from it. The layer comes from the kernel registry's kind of
        # the element's class.
        root = tmp_path / "model"
        root.mkdir()
        auditor = _RecordingAuditor()
        element = BaseDataType(name="float32", kind="base_type", size=32, encoding="IEEE754")

        write_element(str(root), "data_types/base/float32~1.yaml", element, auditor, create=True)
        remove_element(str(root), "data_types/base/float32~1.yaml", auditor, address=("base_types", "float32"))

        assert [entry["args"] for entry in auditor.entries] == [
            {"target": "data_types/base/float32~1.yaml", "layer": "base_types", "identity": "float32"}
        ] * 2

    def test_a_removal_given_no_address_records_the_target_alone(self, tmp_path: Path) -> None:
        # `remove_element` holds a path and no element, so only its caller can
        # say which element the file held; without that, the audit reader
        # composes the address back from the target.
        root = tmp_path / "model"
        root.mkdir()
        write_element(str(root), "swc/g/A.yaml", SWComponent(name="A"))
        auditor = _RecordingAuditor()

        remove_element(str(root), "swc/g/A.yaml", auditor)

        assert [entry["args"] for entry in auditor.entries] == [{"target": "swc/g/A.yaml"}]

    def test_an_element_the_registry_binds_no_address_for_is_recorded_by_its_target_alone(self) -> None:
        # `write_element`'s audit shape for an element whose address is not known:
        # a class the kernel registry binds to no kind, and a bound class whose
        # identity value is not a string. `write_element` itself refuses an
        # unclassified class before it writes, so the shape is pinned on the two
        # helpers it composes its entry from.
        from pydantic import BaseModel

        class Unbound(BaseModel):
            name: str

        assert _address_of(Unbound(name="A")) is None
        assert _address_of(SWComponent.model_construct(name=17)) is None
        assert _address_of(SWComponent(name="A")) == ("swc", "A")
        assert _writer_args("swc/g/A.yaml", None) == {"target": "swc/g/A.yaml"}
        assert _writer_args("swc/g/A.yaml", ("swc", "A")) == {"target": "swc/g/A.yaml", "layer": "swc", "identity": "A"}

    def test_a_refusal_emits_no_audit_entry(self, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir()
        auditor = _RecordingAuditor()

        with pytest.raises(ValueError):
            write_element(str(root), "swc/g/My SWC.yaml", SWComponent(name="My SWC"), auditor)

        assert auditor.entries == []

    def test_a_dict_payload_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir()
        with pytest.raises(TypeError, match="schema model instance"):
            write_element(str(root), "swc/g/A.yaml", {"name": "A"})  # type: ignore[arg-type]

    def test_an_unclassified_element_class_is_refused_rather_than_written(self, tmp_path: Path) -> None:
        """Fail-closed: a kind with no name policy is never written unchecked."""
        from swcraft_tools.schema import Port

        root = tmp_path / "model"
        root.mkdir()
        with pytest.raises(TypeError, match="no emitted-SHORT-NAME policy"):
            write_element(str(root), "swc/g/A.yaml", Port(name="p", direction="provide", interface="I"))
        assert list(root.rglob("*")) == []


class TestRemoveContract:
    def test_removing_the_last_element_leaves_its_directory_in_place(self, tmp_path: Path) -> None:
        """No directory is ever removed by the protocol.

        ``hardware`` is the layer where it matters most — a directory that
        vanished with its last element would turn ``hardware.layer-absent`` on
        under a client that had only removed one core, reporting the whole layer
        missing instead of the one reference that no longer resolves.
        """
        root = tmp_path / "model"
        root.mkdir()
        mutations.create_hardware_element(str(root), "CORE0", "core", threshold=0.7)

        remove_element(str(root), "hardware/CORE0.yaml")

        assert (root / "hardware").is_dir()
        assert list((root / "hardware").iterdir()) == []

    def test_removing_the_last_swc_leaves_its_group_directory_in_place(self, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir()
        write_element(str(root), "swc/sensors/A.yaml", SWComponent(name="A"))

        remove_element(str(root), "swc/sensors/A.yaml")

        assert (root / "swc" / "sensors").is_dir()

    def test_removing_a_missing_element_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir()
        with pytest.raises(ValueError, match="No element file to remove"):
            remove_element(str(root), "swc/g/A.yaml")

    def test_a_directory_is_never_removed_as_an_element(self, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir()
        (root / "swc").mkdir()
        with pytest.raises(ValueError, match="No element file to remove"):
            remove_element(str(root), "swc")
        assert (root / "swc").is_dir()


# ---------------------------------------------------------------------------
# The emitted-SHORT-NAME guards, at the depths only this writer reaches
# ---------------------------------------------------------------------------

#: 126 characters: legal on its own, illegal the moment either event prefix goes
#: in front of it.
_LONG_RUNNABLE = "R" * 126


class TestNestedNameGuards:
    """``write_element`` writes a whole component at once, so it alone can emit a bad *nested* or *counted* name.

    ``test_identifier_coverage.py`` covers one declared-name refusal per element
    kind; these are the cases only the whole-file write can reach.
    """

    def _refuses(self, root: Path, element: Any) -> str:
        before = sorted((path.relative_to(root) for path in root.rglob("*")), key=lambda path: path.as_posix())
        with pytest.raises(ValueError) as excinfo:
            write_element(str(root), "swc/g/A.yaml", element)
        assert sorted((path.relative_to(root) for path in root.rglob("*")), key=lambda path: path.as_posix()) == before
        assert "legal AUTOSAR Identifier" in str(excinfo.value), str(excinfo.value)
        return str(excinfo.value)

    def test_a_bad_port_name_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir()
        swc = SWComponent(name="A", ports=[{"name": "my port", "direction": "provide", "interface": "I"}])
        assert "port name" in self._refuses(root, swc)

    def test_a_bad_runnable_name_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir()
        swc = SWComponent(name="A", runnables=[{"name": "my run"}])
        assert "runnable name" in self._refuses(root, swc)

    def test_a_runnable_whose_event_name_overflows_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir()
        swc = SWComponent(
            name="A",
            runnables=[{"name": _LONG_RUNNABLE, "events": [{"type": "timing", "period": 0.01}]}],
        )
        assert "TIMING-EVENT" in self._refuses(root, swc)

    def test_the_retroactive_event_counter_is_checked_in_its_final_shape(self, tmp_path: Path) -> None:
        """The case ``add_init_event`` cannot reach and this writer can.

        A 125-character runnable emits a legal 128-character ``IT_`` name while
        it carries one event, and an illegal 130-character ``{base}_0`` /
        ``{base}_1`` pair the moment a second event wants the same base — the
        retroactive counter renames the *first* one too. Only a whole-component
        write can put both events in the file in one act, which is why the guard
        checks the emitted form rather than ``{prefix}{runnable}``.
        """
        base = "R" * (128 - len("IT_"))
        root = tmp_path / "model"
        root.mkdir()
        one_event = SWComponent(name="A", runnables=[{"name": base, "events": [{"type": "init"}]}])
        write_element(str(root), "swc/g/A.yaml", one_event)

        two_events = SWComponent(
            name="A",
            runnables=[{"name": base, "events": [{"type": "init"}, {"type": "init"}]}],
        )
        assert "INIT-EVENT" in self._refuses(root, two_events)

    def test_a_bad_data_element_name_is_refused(self, tmp_path: Path) -> None:
        from swcraft_tools.schema import SRInterface

        root = tmp_path / "model"
        root.mkdir()
        iface = SRInterface(
            name="I", kind="sender_receiver", data_elements=[{"name": "my el", "implementation_type": "uint8"}]
        )
        with pytest.raises(ValueError, match="data element name"):
            write_element(str(root), "interfaces/I.yaml", iface)
        assert list(root.rglob("*")) == []

    def test_a_bad_operation_argument_name_is_refused(self, tmp_path: Path) -> None:
        from swcraft_tools.schema import CSInterface

        root = tmp_path / "model"
        root.mkdir()
        iface = CSInterface(
            name="I",
            kind="client_server",
            operations=[
                {"name": "Op", "arguments": [{"name": "my arg", "direction": "in", "implementation_type": "uint8"}]}
            ],
        )
        with pytest.raises(ValueError, match="argument name"):
            write_element(str(root), "interfaces/I.yaml", iface)
        assert list(root.rglob("*")) == []

    def test_a_bad_implementation_record_element_name_is_refused(self, tmp_path: Path) -> None:
        """An implementation record's element is an IMPLEMENTATION-DATA-TYPE-ELEMENT with a SHORT-NAME."""
        from swcraft_tools.schema import ImplementationRecordDataType

        root = tmp_path / "model"
        root.mkdir()
        record = ImplementationRecordDataType(
            name="Frame", kind="implementation_record", elements=[{"name": "my el", "type": "uint8"}]
        )
        with pytest.raises(ValueError, match="Implementation record 'Frame' element name"):
            write_element(str(root), "data_types/implementation/Frame.yaml", record)
        assert list(root.rglob("*")) == []

    def test_a_bad_implementation_array_element_name_is_refused(self, tmp_path: Path) -> None:
        """The array's one sub-element carries ``element_name`` as its SHORT-NAME."""
        from swcraft_tools.schema import ImplementationArrayDataType

        root = tmp_path / "model"
        root.mkdir()
        array = ImplementationArrayDataType(
            name="Samples", kind="implementation_array", element_type="uint8", length=2, element_name="my el"
        )
        with pytest.raises(ValueError, match="Implementation array 'Samples' element name"):
            write_element(str(root), "data_types/implementation/Samples.yaml", array)
        assert list(root.rglob("*")) == []

    def test_a_reserved_data_type_name_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir()
        # `{x}_CompuMethod` over an **enum** `x` is the one data-type name still
        # reserved for a KIND.
        mutations.create_data_type(str(root), "Gear_T", "enum", literals=[{"name": "PARK", "value": 0}])

        # The method collides inside the enum's package only, so the occupant is
        # an application type.
        with pytest.raises(ValueError, match="collides"):
            write_element(
                str(root),
                "data_types/application/Gear_T_CompuMethod.yaml",
                ApplicationDataType(name="Gear_T_CompuMethod", kind="application_type"),
            )
        assert not (root / "data_types" / "application" / "Gear_T_CompuMethod.yaml").exists()

    def test_a_base_type_under_a_generated_name_of_another_package_is_written(self, tmp_path: Path) -> None:
        """A base type lies in ``/DataTypes/Base``, the enum's method in ``/DataTypes/Application``."""
        root = tmp_path / "model"
        root.mkdir()
        mutations.create_data_type(str(root), "Gear_T", "enum", literals=[{"name": "PARK", "value": 0}])

        write_element(
            str(root),
            "data_types/base/Gear_T_CompuMethod.yaml",
            BaseDataType(name="Gear_T_CompuMethod", kind="base_type", size=32, encoding="IEEE754"),
        )

        assert (model_element_path(root, "Gear_T_CompuMethod", kind="base_type")).exists()

    def test_a_reserved_swc_name_is_refused(self, tmp_path: Path) -> None:
        """``{A}_Implementation`` beside a declared ``A`` is refused.

        The only reservation in the component namespace is this collision with the
        ``SWC-IMPLEMENTATION`` generated for an existing component, which is why
        the guard needs one declared beside it.
        """
        root = tmp_path / "model"
        root.mkdir()
        mutations.create_swc(str(root), "A", group="g")

        with pytest.raises(ValueError, match="auto-generated SWC-IMPLEMENTATION name"):
            write_element(str(root), "swc/g/A_Implementation.yaml", SWComponent(name="A_Implementation"))
        assert not (root / "swc" / "g" / "A_Implementation.yaml").exists()

    def test_a_layer_with_no_short_name_takes_a_name_the_identifier_rule_refuses(self, tmp_path: Path) -> None:
        """The out-of-scope half, measured rather than asserted.

        A requirement is never emitted into ARXML, so applying an AUTOSAR
        constraint to its identity would be the tool inventing a rule the
        standard does not ask for.
        """
        root = tmp_path / "model"
        root.mkdir()
        write_element(
            str(root),
            "requirements/REQ 001.yaml",
            Requirement(id="REQ 001", title="T", description="D", version=1, source="architecture"),
        )
        assert (root / "requirements" / "REQ 001.yaml").is_file()


class TestTheCompositionsFourNameSites:
    """``elements._check_composition_names``, at every site it guards.

    A case that drives the *reporting* check does not grade the write path, and
    ``generate arxml`` has no ``check-model`` in front of it while ``validate``
    degrades to structural mode without a licensed XSD, so an unguarded site
    here ships an invalid artifact.

    Driven from :data:`~test_writer_compositions.COMPOSITION_NAME_SITES`, whose
    own derivation test fails on a new site, so none can arrive ungraded.

    The **exporter's** twin (``arxml_generation._check_composition_names``) and
    the reporting check are graded in ``test_validator_compositions.py``, from
    the same table.
    """

    @pytest.mark.parametrize("site", COMPOSITION_NAME_SITES, ids=lambda site: site.id)
    def test_a_bad_name_at_this_site_is_refused_and_nothing_is_written(
        self, site: CompositionNameSite, tmp_path: Path
    ) -> None:
        root = tmp_path / "model"
        root.mkdir()
        composition = Composition.model_validate(site.plant(one_of_each_site(), "my bad name"))

        with pytest.raises(ValueError) as excinfo:
            write_element(str(root), "compositions/Root.yaml", composition)

        message = str(excinfo.value)
        assert "legal AUTOSAR Identifier" in message, message
        assert site.guard_label in message, message
        assert list(root.rglob("*")) == [], "a refused write left a file behind"

    @pytest.mark.parametrize("site", COMPOSITION_NAME_SITES, ids=lambda site: site.id)
    def test_a_name_at_the_limit_at_this_site_is_written(self, site: CompositionNameSite, tmp_path: Path) -> None:
        """A 128-character name is written at every site, so the refusals above cannot pass on a refuse-all guard.

        ``test_validator_compositions.py`` holds the 129 twin on the same four sites.
        """
        root = tmp_path / "model"
        root.mkdir()
        composition = Composition.model_validate(site.plant(one_of_each_site(), "N" * 128))

        write_element(str(root), "compositions/Root.yaml", composition)

        assert (root / "compositions" / "Root.yaml").is_file()


# ---------------------------------------------------------------------------
# The line edit: what a change set's update writes
# ---------------------------------------------------------------------------

#: A file in no form the writer would give it: a comment, its keys in another
#: order, and a comment on the line of the value that changes.
_HAND_FILE = "# The sensor.\ndescription: old  # why\nname: A\n"
_TARGET = "swc/g/A.yaml"


class _Holder(BaseModel):
    """A class no contract binds, whose field keeps an integer an integer."""

    value: int | float


def _old() -> SWComponent:
    return SWComponent(name="A", description="old")


def _new() -> SWComponent:
    return SWComponent(name="A", description="new")


def _whole(element: SWComponent) -> bytes:
    """Today's write, stated independently of the module's own dump."""
    return yaml.safe_dump(element_to_dict(element), default_flow_style=False, sort_keys=False).encode("utf-8")


def _hand_model(tmp_path: Path, text: str = _HAND_FILE) -> Path:
    root = tmp_path / "model"
    path = root / _TARGET
    path.parent.mkdir(parents=True)
    path.write_bytes(text.encode("utf-8"))
    return root


class TestAnEditLands:
    def test_only_the_changed_line_moves(self, tmp_path: Path) -> None:
        root = _hand_model(tmp_path)

        edited = edited_text(str(root), _TARGET, _new(), _old)
        write_element(str(root), _TARGET, _new(), edited=edited)

        expected = "# The sensor.\ndescription: new  # why\nname: A\n"
        assert edited == EditedText(expected, None)
        assert (root / _TARGET).read_bytes() == expected.encode("utf-8")

    def test_a_crlf_file_keeps_its_line_ends_through_the_one_write(self, tmp_path: Path) -> None:
        root = _hand_model(tmp_path, _HAND_FILE.replace("\n", "\r\n"))

        write_element(str(root), _TARGET, _new(), edited=edited_text(str(root), _TARGET, _new(), _old))

        expected = _HAND_FILE.replace("old", "new").replace("\n", "\r\n")
        assert (root / _TARGET).read_bytes() == expected.encode("utf-8")

    def test_edited_text_writes_nothing(self, tmp_path: Path) -> None:
        root = _hand_model(tmp_path)

        edited_text(str(root), _TARGET, _new(), _old)

        assert (root / _TARGET).read_bytes() == _HAND_FILE.encode("utf-8")

    def test_the_entry_records_the_lines_form_and_no_reason(self, tmp_path: Path) -> None:
        root = _hand_model(tmp_path)
        auditor = _RecordingAuditor()

        write_element(str(root), _TARGET, _new(), auditor, edited=edited_text(str(root), _TARGET, _new(), _old))

        assert [entry["args"] for entry in auditor.entries] == [
            {"target": _TARGET, "layer": "swc", "identity": "A", "form": "lines"}
        ]

    def test_the_guards_run_on_an_edited_write_too(self, tmp_path: Path) -> None:
        root = _hand_model(tmp_path)
        illegal = SWComponent(name="A", ports=[{"name": "a b", "direction": "provide", "interface": "I"}])

        with pytest.raises(ValueError, match="port name"):
            write_element(str(root), _TARGET, illegal, edited=EditedText("name: A\n", None))

        assert (root / _TARGET).read_bytes() == _HAND_FILE.encode("utf-8")


class TestARenameEditsTheTextItIsGiven:
    """``source_text``: a rename edits the removed file's text in place of *target*'s."""

    _RENAMED = "swc/g/B.yaml"

    def test_only_the_identity_line_moves_and_the_target_is_not_read(self, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir()
        renamed = SWComponent(name="B", description="old")

        edited = edited_text(str(root), self._RENAMED, renamed, _old, source_text=_HAND_FILE)

        assert edited == EditedText(_HAND_FILE.replace("name: A\n", "name: B\n"), None)
        assert not (root / self._RENAMED).exists()

    def test_the_carried_text_lands_through_a_create(self, tmp_path: Path) -> None:
        root = tmp_path / "model"
        root.mkdir()
        renamed = SWComponent(name="B", description="old")
        auditor = _RecordingAuditor()

        edited = edited_text(str(root), self._RENAMED, renamed, _old, source_text=_HAND_FILE)
        write_element(str(root), self._RENAMED, renamed, auditor, create=True, edited=edited)

        assert (root / self._RENAMED).read_bytes() == _HAND_FILE.replace("name: A", "name: B").encode("utf-8")
        assert [entry["args"] for entry in auditor.entries] == [
            {"target": self._RENAMED, "layer": "swc", "identity": "B", "form": "lines"}
        ]

    def test_a_file_in_the_writers_form_gives_the_whole_write(self, tmp_path: Path) -> None:
        renamed = SWComponent(name="B", description="old")

        edited = edited_text(str(tmp_path), self._RENAMED, renamed, _old, source_text=_whole(_old()).decode("utf-8"))

        assert edited.text is not None
        assert edited.text.encode("utf-8") == _whole(renamed)

    def test_a_refused_form_gives_no_text_and_says_whether_it_stood_in_the_writers_form(self, tmp_path: Path) -> None:
        renamed = SWComponent(name="B", description="old")

        mixed = edited_text(str(tmp_path), self._RENAMED, renamed, _old, source_text="description: old\r\nname: A\n")

        assert mixed == EditedText(None, "the file has mixed line ends")


#: The unknown-key oracle's corpus: one element file per kind it plants into.
_UNKNOWN_KEY_CORPUS = Path(__file__).resolve().parent / "fixtures" / "unknown_key_oracle" / "model"


class TestAFallbackNamesTheUndeclaredKeysItDrops:
    """``EditedText.dropped_keys``: the undeclared keys a fallback drops, on a fallback alone, in document order."""

    _STRAY = "# The sensor.\nzz_first: 1\ndescription: old\r\nname: A\nrunnables:\n- name: R\n  zz_second: 2\n"

    def test_a_refused_form_names_every_undeclared_key_in_document_order(self, tmp_path: Path) -> None:
        edited = edited_text(str(_hand_model(tmp_path, self._STRAY)), _TARGET, _new(), _old)

        assert edited == EditedText(None, "the file has mixed line ends", dropped_keys=("zz_first", "zz_second"))

    def test_a_rename_fallback_names_the_keys_of_the_text_it_was_given(self, tmp_path: Path) -> None:
        renamed = SWComponent(name="B", description="old")

        edited = edited_text(str(tmp_path), "swc/g/B.yaml", renamed, _old, source_text=self._STRAY)

        assert edited.dropped_keys == ("zz_first", "zz_second")

    def test_a_read_back_mismatch_names_them(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(elements, "_read_back_holds", lambda *_args: False)
        text = self._STRAY.replace("\r\n", "\n")

        edited = edited_text(str(_hand_model(tmp_path, text)), _TARGET, _new(), _old)

        assert edited == EditedText(None, READ_BACK_MISMATCH, dropped_keys=("zz_first", "zz_second"))

    def test_an_edit_that_lands_names_none(self, tmp_path: Path) -> None:
        # The line edit keeps the keys, so nothing is dropped: the change set's pass D refuses the key.
        text = "# The sensor.\nzz_first: 1\ndescription: old\nname: A\nzz_second: 2\n"

        edited = edited_text(str(_hand_model(tmp_path, text)), _TARGET, _new(), _old)

        assert edited.text is not None
        assert "zz_first" in edited.text
        assert "zz_second" in edited.text
        assert edited.dropped_keys == ()

    def test_a_file_holding_none_names_none(self, tmp_path: Path) -> None:
        edited = edited_text(str(_hand_model(tmp_path, "description: old\r\nname: A\n")), _TARGET, _new(), _old)

        assert edited == EditedText(None, "the file has mixed line ends")

    def test_a_text_the_predicate_cannot_read_names_none_and_raises_nothing(self) -> None:
        assert undeclared_keys_of(None, _old()) == ()
        assert undeclared_keys_of("name: [unclosed\n", _old()) == ()
        assert undeclared_keys_of("value: 1\nzz: 2\n", _Holder(value=1)) == ()

    def test_an_unexpected_exception_names_them(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def defect(*_args: object) -> str:
            raise RuntimeError("a defect")

        monkeypatch.setattr(elements, "edit_element_text", defect)
        text = self._STRAY.replace("\r\n", "\n")

        edited = edited_text(str(_hand_model(tmp_path, text)), _TARGET, _new(), _old)

        assert edited == EditedText(
            None, UNEXPECTED_EXCEPTION.format("RuntimeError"), dropped_keys=("zz_first", "zz_second")
        )

    def test_the_predicate_answers_for_every_element_of_the_corpus(self) -> None:
        # The positive guard beside the narrow catch: one key planted at the top of each
        # corpus element -- a component, a composition, an application type, a computation
        # method (a closed kind included: the predicate answers for it all the same).
        registry = production_registry()
        seen = []
        for layer in registry.layer_names:
            adapter = TypeAdapter(registry.layer(layer).element_annotation)
            for entry in scan_layer(_UNKNOWN_KEY_CORPUS, layer, registry=registry).files:
                text = entry.path.read_text(encoding="utf-8")
                element = adapter.validate_python(load_yaml_document(text))
                seen.append(layer)

                assert undeclared_keys_of(text + "zz_planted: 1\n", element) == ("zz_planted",), entry.path.name
        assert len(seen) == 4

    def test_the_predicate_answers_for_a_requirement(self) -> None:
        text = "id: R\ntitle: T\ndescription: D\nversion: 1\nsource: architecture\nzz_planted: 1\n"
        element = Requirement(id="R", title="T", description="D", version=1, source="architecture")

        assert undeclared_keys_of(text, element) == ("zz_planted",)


class TestEveryFallbackIsTodaysWrite:
    """Each route: no text, a reason, and the whole write's bytes."""

    @staticmethod
    def _assert_written_whole(
        root: Path, element: SWComponent, edited: EditedText, reason: str, dropped: tuple[str, ...] = ()
    ) -> None:
        assert edited == EditedText(None, reason, dropped_keys=dropped)
        auditor = _RecordingAuditor()
        write_element(str(root), _TARGET, element, auditor, edited=edited)
        assert (root / _TARGET).read_bytes() == _whole(element)
        assert [entry["args"] for entry in auditor.entries] == [
            {"target": _TARGET, "layer": "swc", "identity": "A", "form": "whole", "reason": reason}
        ]

    def test_a_refused_form(self, tmp_path: Path) -> None:
        root = _hand_model(tmp_path, "description: old\r\nname: A\n")

        edited = edited_text(str(root), _TARGET, _new(), _old)

        self._assert_written_whole(root, _new(), edited, "the file has mixed line ends")

    def test_a_read_back_that_differs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _hand_model(tmp_path)
        monkeypatch.setattr(elements, "edit_element_text", lambda *_: "description: other\nname: A\n")

        edited = edited_text(str(root), _TARGET, _new(), _old)

        self._assert_written_whole(root, _new(), edited, READ_BACK_MISMATCH)

    def test_the_read_back_tells_an_integer_from_a_float(self) -> None:
        # `5 == 5.0`, so a plain comparison would prove the wrong text.
        assert not elements._read_back_holds("value: 5\n", _Holder(value=5.0), {"value": 5.0})
        assert elements._read_back_holds("value: 5.0\n", _Holder(value=5.0), {"value": 5.0})

    def test_a_read_back_that_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _hand_model(tmp_path)
        monkeypatch.setattr(elements, "edit_element_text", lambda *_: "description: no name\n")

        edited = edited_text(str(root), _TARGET, _new(), _old)

        self._assert_written_whole(root, _new(), edited, UNEXPECTED_EXCEPTION.format("ValidationError"))

    def test_an_unexpected_exception_names_its_class_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _hand_model(tmp_path)

        def stops(*_: object) -> str:
            raise RuntimeError("text out of the model")

        monkeypatch.setattr(elements, "edit_element_text", stops)

        edited = edited_text(str(root), _TARGET, _new(), _old)

        self._assert_written_whole(root, _new(), edited, UNEXPECTED_EXCEPTION.format("RuntimeError"))

    def test_a_prior_that_does_not_validate(self, tmp_path: Path) -> None:
        root = _hand_model(tmp_path)

        def prior() -> SWComponent:
            return SWComponent.model_validate({})

        edited = edited_text(str(root), _TARGET, _new(), prior)

        self._assert_written_whole(root, _new(), edited, UNEXPECTED_EXCEPTION.format("ValidationError"))

    def test_an_empty_edit_list(self, tmp_path: Path) -> None:
        root = _hand_model(tmp_path)

        edited = edited_text(str(root), _TARGET, _old(), _old)

        self._assert_written_whole(root, _old(), edited, "the line edit found no line to change")

    def test_a_prior_that_disagrees_with_the_file_gives_no_byte_identical_edit(self, tmp_path: Path) -> None:
        # No audit entry describes a file left unchanged as a line edit.
        root = _hand_model(tmp_path, _HAND_FILE.replace("old", "new"))

        edited = edited_text(str(root), _TARGET, _new(), _old)

        self._assert_written_whole(root, _new(), edited, "the line edit found no line to change")

    def test_a_recursive_alias_in_an_undeclared_key_ends_in_the_whole_write(self, tmp_path: Path) -> None:
        # It validates, because `extra="ignore"`; a walk after its end would spin past every `except`.
        text = "name: A\nrunnables:\n- name: R\n  junk: &j\n  - *j\n"
        root = _hand_model(tmp_path, text)
        old = SWComponent.model_validate(load_yaml_document(text))
        new = SWComponent.model_validate(load_yaml_document(text.replace("name: R", "name: S")))

        with at_most_steps():
            edited = edited_text(str(root), _TARGET, new, lambda: old)

        # The undeclared key is named as dropped, and its alias stops nothing.
        reason = "the file writes a value through an alias, an anchor or a tag"
        self._assert_written_whole(root, new, edited, reason, ("junk",))

    @pytest.mark.parametrize("target", ["swc/g/Absent.yaml", "../A.yaml"])
    def test_a_file_that_cannot_be_read_raises_nothing(self, target: str, tmp_path: Path) -> None:
        root = _hand_model(tmp_path)

        edited = edited_text(str(root), target, _new(), _old)

        assert edited.text is None
        assert edited.reason is not None


class TestAFileInTheWritersForm:
    """A whole write of it costs nothing: it says so, and the audit still says ``whole``."""

    _LIMIT = "the change touches too many places in the file"

    @staticmethod
    def _elements(suffix: str) -> SWComponent:
        ports = [{"name": f"P{index}{suffix}", "direction": "provide", "interface": "I"} for index in range(70)]
        return SWComponent.model_validate({"name": "A", "ports": ports})

    def test_past_the_limit_it_stands_in_the_form_of_the_old_side(self, tmp_path: Path) -> None:
        old, new = self._elements(""), self._elements("x")
        root = _hand_model(tmp_path, _whole(old).decode("utf-8"))

        edited = edited_text(str(root), _TARGET, new, lambda: old)
        auditor = _RecordingAuditor()
        write_element(str(root), _TARGET, new, auditor, edited=edited)

        assert edited == EditedText(None, self._LIMIT, in_writer_form=True)
        assert (root / _TARGET).read_bytes() == _whole(new)
        assert auditor.entries[0]["args"]["form"] == "whole"
        assert auditor.entries[0]["args"]["reason"] == self._LIMIT

    def test_a_file_past_the_recompose_budget_ends_in_the_whole_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("swcraft_tools.yaml_text_edit._RECOMPOSE_BUDGET", 10)
        old = SWComponent.model_validate({"name": "A", "description": "old"})
        new = SWComponent.model_validate({"name": "A", "description": "new"})
        root = _hand_model(tmp_path, "# kept\n" + _whole(old).decode("utf-8"))

        edited = edited_text(str(root), _TARGET, new, lambda: old)
        write_element(str(root), _TARGET, new, _RecordingAuditor(), edited=edited)

        assert edited == EditedText(None, "the change touches too many places for a file of this size")
        assert (root / _TARGET).read_bytes() == _whole(new)

    def test_a_list_past_the_match_budget_ends_in_the_whole_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("swcraft_tools.yaml_text_edit._MATCH_BUDGET", 1000)
        reason = "a list changed along too many items to match them"
        ports = [{"name": f"P{index}", "direction": "provide", "interface": "I"} for index in range(402)]
        changed = [{**ports[0], "name": "First"}, *ports[1:-1], {**ports[-1], "name": "Last"}]
        old = SWComponent.model_validate({"name": "A", "ports": ports})
        new = SWComponent.model_validate({"name": "A", "ports": changed})
        root = _hand_model(tmp_path, "# kept\n" + _whole(old).decode("utf-8"))

        edited = edited_text(str(root), _TARGET, new, lambda: old)
        auditor = _RecordingAuditor()
        write_element(str(root), _TARGET, new, auditor, edited=edited)

        assert edited == EditedText(None, reason)
        assert (root / _TARGET).read_bytes() == _whole(new)
        assert auditor.entries[0]["args"]["form"] == "whole"
        assert auditor.entries[0]["args"]["reason"] == reason

    def test_it_stands_in_the_form_of_the_new_side(self, tmp_path: Path) -> None:
        root = _hand_model(tmp_path, _whole(_new()).decode("utf-8"))

        edited = edited_text(str(root), _TARGET, _new(), _new)

        assert edited == EditedText(None, "the line edit found no line to change", in_writer_form=True)

    def test_a_comment_takes_a_file_out_of_it(self, tmp_path: Path) -> None:
        old, new = self._elements(""), self._elements("x")
        root = _hand_model(tmp_path, "# kept\n" + _whole(old).decode("utf-8"))

        assert edited_text(str(root), _TARGET, new, lambda: old) == EditedText(None, self._LIMIT)

    def test_a_line_edit_never_pays_for_the_old_side(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _hand_model(tmp_path, _whole(_old()).decode("utf-8"))
        dumped: list[Any] = []
        real = elements._dump_element
        monkeypatch.setattr(elements, "_dump_element", lambda data: dumped.append(data) or real(data))

        assert edited_text(str(root), _TARGET, _new(), _old).text is not None

        assert dumped == [element_to_dict(_new())]


class TestAWriteGivenNoEditIsTheWriteOfToday:
    def test_the_bytes_and_the_entry_are_unchanged(self, tmp_path: Path) -> None:
        root = _hand_model(tmp_path)
        auditor = _RecordingAuditor()

        write_element(str(root), _TARGET, _new(), auditor)

        assert (root / _TARGET).read_bytes() == _whole(_new())
        assert [entry["args"] for entry in auditor.entries] == [{"target": _TARGET, "layer": "swc", "identity": "A"}]
