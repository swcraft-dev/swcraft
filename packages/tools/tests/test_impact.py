"""Tests for impact.py."""

from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import textwrap

import pytest
from conftest import REPO_ROOT, parser_subcommand_options
from differ_support import make_connector, make_snapshot
from impact_record_support import restore_requirement_lines
from model_contract_fixtures import load_binding, load_contract, merged_binding, merged_contract
from swcraft_tools import query
from swcraft_tools.cli import main as cli_main
from swcraft_tools.cli.dispatch import build_parser
from swcraft_tools.impact import (
    DEFAULT_DIRECTION,
    DIRECTIONS,
    Dependent,
    ElementRef,
    ImpactCursorNotFound,
    ImpactPage,
    build_reference_graph,
    count_impact,
    format_impact,
    format_impact_scope,
    format_impact_summary,
    impact_cursor,
    impact_of,
    page_impact,
)
from swcraft_tools.model_registry import ModelRegistry, build_model_registry, production_registry, use_registry
from swcraft_tools.model_state import ModelState
from swcraft_tools.query import EdgeDetail, build_graphs
from swcraft_tools.schema import (
    ApplicationDataType,
    ArrayDataType,
    BaseDataType,
    Composition,
    CompositionMember,
    Connector,
    Core,
    CSArgument,
    CSInterface,
    CSOperation,
    DataConstr,
    DataElement,
    ExternalDataType,
    Linear,
    MappingSet,
    Memory,
    Partition,
    Port,
    RecordDataType,
    RecordField,
    Requirement,
    Runnable,
    Sequence,
    SequenceStep,
    SRInterface,
    SWComponent,
    Task,
    TaskRunnableRef,
    Unit,
    ValueDataType,
)
from swcraft_tools.snapshot import ModelSnapshot, load_snapshot
from synthetic_support import SyntheticElement

# data type <- interface


def test_data_type_referenced_by_interface_data_element_shows_interface_as_dependent():
    base = BaseDataType(name="float32_base", kind="base_type", size=32, encoding="IEEE754")
    speed = ValueDataType(name="float32", kind="value_type", base="float32_base")
    iface = SRInterface(
        name="ISpeed",
        kind="sender_receiver",
        data_elements=[DataElement(name="speed", implementation_type="float32")],
    )
    snapshot = make_snapshot(base_types=[base], implementation_types=[speed], interfaces=[iface])

    result = impact_of(snapshot, "float32")

    assert result["implementation_type"] == [Dependent(ref=ElementRef("interface", "ISpeed"))]


def test_two_hop_transitive_chain_primitive_alias_interface():
    """primitive <- alias <- interface: alias and interface are both transitive dependents of the primitive.

    Not an alias-over-alias chain, which schema validation forbids.
    """
    primitive = BaseDataType(name="float32", kind="base_type", size=32, encoding="IEEE754")
    alias = ValueDataType(name="Speed_t", kind="value_type", base="float32")
    iface = SRInterface(
        name="ISpeed",
        kind="sender_receiver",
        data_elements=[DataElement(name="speed", implementation_type="Speed_t")],
    )
    # The start is a `base_type`: a value type's `base` resolves in `base_types` alone.
    snapshot = make_snapshot(base_types=[primitive], implementation_types=[alias], interfaces=[iface])

    result = impact_of(snapshot, "float32")

    refs = {d.ref for d in result["base_type"]}
    assert ElementRef("implementation_type", "Speed_t") in refs
    assert ElementRef("interface", "ISpeed") in refs
    assert len(result["base_type"]) == 2


def test_record_field_type_shows_record_as_dependent_of_referenced_data_type():
    """A record field's ``type`` makes the record a dependent of the referenced application type."""
    primitive = ApplicationDataType(name="Speed_Phys", kind="application_type")
    record = RecordDataType(name="Vector3", kind="record", fields=[RecordField(name="x", type="Speed_Phys")])
    snapshot = make_snapshot(application_types=[primitive, record])

    result = impact_of(snapshot, "Speed_Phys")

    assert result["application_type"] == [Dependent(ref=ElementRef("application_type", "Vector3"))]


def test_array_element_type_shows_array_as_dependent_of_referenced_data_type():
    """An array's ``element_type`` makes the array a dependent of the referenced application type."""
    primitive = ApplicationDataType(name="Speed_Phys", kind="application_type")
    array = ArrayDataType(name="FloatArray", kind="array", element_type="Speed_Phys", length=4)
    snapshot = make_snapshot(application_types=[primitive, array])

    result = impact_of(snapshot, "Speed_Phys")

    assert result["application_type"] == [Dependent(ref=ElementRef("application_type", "FloatArray"))]


def test_cs_interface_operation_argument_type_shows_interface_as_dependent():
    """An operation argument's ``type`` makes the client-server interface a dependent of the data type."""
    base = BaseDataType(name="float32_base", kind="base_type", size=32, encoding="IEEE754")
    primitive = ValueDataType(name="float32", kind="value_type", base="float32_base")
    iface = CSInterface(
        name="ISpeedOps",
        kind="client_server",
        operations=[
            CSOperation(
                name="GetSpeed",
                arguments=[CSArgument(name="out_speed", direction="out", implementation_type="float32")],
            )
        ],
    )
    snapshot = make_snapshot(base_types=[base], implementation_types=[primitive], interfaces=[iface])

    result = impact_of(snapshot, "float32")

    assert result["implementation_type"] == [Dependent(ref=ElementRef("interface", "ISpeedOps"))]


# One entry per dependent, every detail in emission order


def test_dependent_reachable_via_two_edges_is_one_entry_carrying_both_details():
    """A SWC reached through two ports appears once, carrying both ports' details in emission order.

    Guards the defect where the first detail won and the second port was dropped from the report.
    """
    iface = SRInterface(
        name="ISpeed",
        kind="sender_receiver",
        data_elements=[DataElement(name="speed", implementation_type="float32")],
    )
    swc = SWComponent(
        name="DualPortSWC",
        ports=[
            Port(name="pIn1", direction="require", interface="ISpeed"),
            Port(name="pIn2", direction="require", interface="ISpeed"),
        ],
    )
    snapshot = make_snapshot(interfaces=[iface], swcs=[swc])

    result = impact_of(snapshot, "ISpeed")

    assert result["interface"] == [
        Dependent(
            ref=ElementRef("swc", "DualPortSWC"),
            details=(EdgeDetail("port", "pIn1"), EdgeDetail("port", "pIn2")),
        )
    ]
    assert format_impact("ISpeed", result).splitlines()[-1] == "  swc DualPortSWC (via ports pIn1, pIn2)"


# SWC referenced by connector, task mapping, and requirement link


def test_swc_referenced_by_connector_and_task_reported_with_detail_and_no_requirement():
    """The requirement a component satisfies is what the component depends on, never its dependent."""
    swc = SWComponent(name="SpeedCtrlSWC", satisfies=["REQ-1@1"])
    connector = make_connector("C1", "SpeedCtrlSWC", "pOut", "OtherSWC", "pIn")
    task = Task(
        name="Task_10ms",
        period=0.01,
        priority=1,
        partition="PARTITION0",
        runnables=[TaskRunnableRef(swc="SpeedCtrlSWC", runnable="SpeedCtrl_Run")],
    )
    req = Requirement(id="REQ-1", title="T", description="D", version=1, source="architecture")
    snapshot = make_snapshot(swcs=[swc], connectors=[connector], tasks=[task], requirements=[req])

    result = impact_of(snapshot, "SpeedCtrlSWC")

    deps_by_ref = {d.ref: d.detail for d in result["swc"]}
    # The connector names the endpoint on this component, owner first.
    assert deps_by_ref[ElementRef("connector", "C1")] == "via port SpeedCtrlSWC.pOut"
    assert deps_by_ref[ElementRef("task", "Task_10ms")] == "runnable SpeedCtrl_Run"
    assert len(result["swc"]) == 2


def test_a_requirement_lists_the_elements_that_satisfy_it_a_port_and_a_runnable_by_their_detail():
    """A requirement lists every element whose ``satisfies`` names it, the version suffix ignored.

    A port's and a runnable's link count as their component's, with the ``port`` and ``runnable`` details.
    """
    linked = SWComponent(name="Own", satisfies=["REQ-1@3"])
    by_port = SWComponent(
        name="ByPort", ports=[Port(name="pIn", direction="require", interface="I", satisfies=["REQ-1"])]
    )
    by_runnable = SWComponent(name="ByRunnable", runnables=[Runnable(name="Run10ms", satisfies=["REQ-1@1"])])
    task = Task(name="T", period=0.01, priority=1, partition="P", satisfies=["REQ-1@1"])
    req = Requirement(id="REQ-1", title="T", description="D", version=3, source="architecture")
    snapshot = make_snapshot(swcs=[linked, by_port, by_runnable], tasks=[task], requirements=[req])

    result = impact_of(snapshot, "REQ-1", depth=1)

    assert {d.ref: d.detail for d in result["requirement"]} == {
        ElementRef("swc", "Own"): "",
        ElementRef("swc", "ByPort"): "via port pIn",
        ElementRef("swc", "ByRunnable"): "runnable Run10ms",
        ElementRef("task", "T"): "",
    }


def test_the_impact_of_a_requirement_on_the_frozen_impact_model_is_this_literal_record():
    """The impact of ``REQ_SPEED_001`` on the frozen impact model, recorded literally in walk order.

    A change detector beside ``impact_edges.json``; the frozen digests do not reach this direction.
    """
    snapshot = load_snapshot(
        str(REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "recorded_oracles" / "impact_model")
    )

    result = impact_of(snapshot, "REQ_SPEED_001", depth=1)

    assert [(d.ref.layer, d.ref.name, d.detail) for d in result["requirement"]] == [
        ("swc", "SpeedControl", "runnable SpeedControl_Run"),
        ("swc", "SpeedSensor", "via port pOut"),
        ("swc", "Shared", ""),
        ("interface", "IDiagnostics", ""),
        ("interface", "ISpeed", ""),
        ("requirement", "REQ_SPEED_002", ""),
        ("composition", "Powertrain", ""),
        ("task", "Task_10ms", ""),
        ("hardware", "CORE0", ""),
        ("hardware", "CPU", ""),
        ("hardware", "PARTITION0", ""),
        ("hardware", "RAM0", ""),
    ]
    assert list(result) == ["requirement"]


def test_port_interface_edge_shows_swc_as_dependent_of_interface_with_port_detail():
    iface = SRInterface(
        name="ISpeed",
        kind="sender_receiver",
        data_elements=[DataElement(name="speed", implementation_type="float32")],
    )
    swc = SWComponent(
        name="SpeedCtrlSWC",
        ports=[Port(name="pIn", direction="require", interface="ISpeed")],
    )
    snapshot = make_snapshot(interfaces=[iface], swcs=[swc])

    result = impact_of(snapshot, "ISpeed")

    assert result["interface"] == [
        Dependent(ref=ElementRef("swc", "SpeedCtrlSWC"), details=(EdgeDetail("port", "pIn"),))
    ]


# sequence -> task


def test_sequence_referencing_task_appears_as_dependent_of_task():
    task = Task(
        name="Task_10ms",
        period=0.01,
        priority=1,
        partition="PARTITION0",
        runnables=[TaskRunnableRef(swc="SpeedCtrlSWC", runnable="SpeedCtrl_Run")],
    )
    seq = Sequence(
        name="Seq1",
        task="Task_10ms",
        steps=[SequenceStep(swc="SpeedCtrlSWC", runnable="SpeedCtrl_Run")],
    )
    snapshot = make_snapshot(tasks=[task], sequences=[seq])

    result = impact_of(snapshot, "Task_10ms")

    assert result["task"] == [Dependent(ref=ElementRef("sequence", "Seq1"))]


def test_sequence_step_shows_swc_as_dependent_with_runnable_step_detail():
    swc = SWComponent(name="SpeedCtrlSWC")
    task = Task(
        name="Task_10ms",
        period=0.01,
        priority=1,
        partition="PARTITION0",
        runnables=[TaskRunnableRef(swc="SpeedCtrlSWC", runnable="SpeedCtrl_Run")],
    )
    seq = Sequence(
        name="Seq1",
        task="Task_10ms",
        steps=[SequenceStep(swc="SpeedCtrlSWC", runnable="SpeedCtrl_Run")],
    )
    snapshot = make_snapshot(swcs=[swc], tasks=[task], sequences=[seq])

    result = impact_of(snapshot, "SpeedCtrlSWC")

    deps_by_ref = {d.ref: d.detail for d in result["swc"]}
    assert deps_by_ref[ElementRef("task", "Task_10ms")] == "runnable SpeedCtrl_Run"
    assert deps_by_ref[ElementRef("sequence", "Seq1")] == "step: SpeedCtrl_Run"


def test_an_entry_naming_an_occurrence_by_instance_is_not_a_dependent_of_its_type():
    """A stated limitation: an ``instance`` value is a path, not a declared reference, so ``impact`` does not follow it.

    The task and the sequence naming the occurrence are not dependents of its type; the composition
    placing it is. If they ARE listed, the limitation is lifted: say so in the YAML conventions and
    the guide, and delete this test.
    """
    swc = SWComponent(name="SpeedCtrlSWC")
    root = Composition(
        name="Root", title="Root", root=True, members=[CompositionMember(name="ctrl", type="SpeedCtrlSWC")]
    )
    task = Task(
        name="Task_10ms",
        period=0.01,
        priority=1,
        partition="PARTITION0",
        runnables=[TaskRunnableRef(instance="ctrl", runnable="SpeedCtrl_Run")],
    )
    seq = Sequence(name="Seq1", task="Task_10ms", steps=[SequenceStep(instance="ctrl", runnable="SpeedCtrl_Run")])
    snapshot = make_snapshot(swcs=[swc], compositions=[root], tasks=[task], sequences=[seq])

    result = impact_of(snapshot, "SpeedCtrlSWC")

    assert {dependent.ref for dependent in result["swc"]} == {ElementRef("composition", "Root")}


# composition -> member


def test_a_composition_shows_as_dependent_of_the_component_it_places():
    """The hierarchy edge is the member's declared reference, walked like any other."""
    swc = SWComponent(name="SpeedSensor")
    composition = Composition(
        name="Root",
        title="Root",
        root=True,
        members=[CompositionMember(name="Sensor", type="SpeedSensor")],
    )
    snapshot = make_snapshot(swcs=[swc], compositions=[composition])

    result = impact_of(snapshot, "SpeedSensor")

    assert result["swc"] == [
        Dependent(ref=ElementRef("composition", "Root"), details=(EdgeDetail("member", "Sensor"),))
    ]


def test_a_composition_shows_as_dependent_of_the_composition_it_places():
    """A composition placing another composition is its dependent."""
    leaf = Composition(name="Leaf", title="Leaf")
    root = Composition(
        name="Root",
        title="Root",
        root=True,
        members=[CompositionMember(name="Inner", composition="Leaf")],
    )
    snapshot = make_snapshot(compositions=[leaf, root])

    result = impact_of(snapshot, "Leaf")

    assert result["composition"] == [
        Dependent(ref=ElementRef("composition", "Root"), details=(EdgeDetail("member", "Inner"),))
    ]


def test_a_connector_shows_as_dependent_of_the_composition_it_is_drawn_inside():
    """A connector naming a composition depends on it; an unset ``composition`` contributes no edge.

    Both halves are asserted: an edge builder that treated ``None`` as the root's name would pass
    the positive half alone.
    """
    root = Composition(name="Root", title="Root", root=True)
    inside = make_connector("Sensor_to_Control", "SpeedSensor", "pOut", "SpeedControl", "rIn")
    inside.composition = "Root"
    unplaced = make_connector("Unplaced", "SpeedSensor", "pOut", "SpeedControl", "rIn")

    result = impact_of(make_snapshot(compositions=[root], connectors=[inside, unplaced]), "Root")

    assert result["composition"] == [Dependent(ref=ElementRef("connector", "Sensor_to_Control"))]


def test_a_connector_shows_as_dependent_of_the_composition_its_endpoint_names():
    """An endpoint whose type is a composition makes the connector its dependent.

    The edge resolves into ``compositions``, not ``swc``; the connector's line names the endpoint,
    qualified by its owner.
    """
    leaf = Composition(name="Diagnostics", title="Diagnostics")
    across = Connector(
        name="Diag_to_Powertrain",
        **{
            "from": {"composition": "Diagnostics", "port": "pDiag"},
            "to": {"composition": "Powertrain", "port": "rDiagIn"},
        },
    )

    result = impact_of(make_snapshot(compositions=[leaf], connectors=[across]), "Diagnostics")

    assert result["composition"] == [
        Dependent(
            ref=ElementRef("connector", "Diag_to_Powertrain"),
            details=(EdgeDetail("endpoint", "Diagnostics.pDiag"),),
        )
    ]


def test_a_composition_shows_as_dependent_of_the_interface_its_boundary_port_names():
    """A composition boundary port's ``interface`` makes the composition a dependent of that interface.

    The one composition reference that makes an interface's dependents include a composition, and
    otherwise witnessed only by recorded reports, which would ratify a wrong target layer or detail
    rather than catch it.
    """
    iface = SRInterface(
        name="ISignal",
        kind="sender_receiver",
        data_elements=[DataElement(name="value", implementation_type="uint8")],
    )
    root = Composition(
        name="Root",
        title="Root",
        root=True,
        ports=[{"name": "rIn", "direction": "require", "interface": "ISignal"}],
    )

    result = impact_of(make_snapshot(compositions=[root], interfaces=[iface]), "ISignal")

    assert result["interface"] == [
        Dependent(ref=ElementRef("composition", "Root"), details=(EdgeDetail("port", "rIn"),))
    ]


def _without_the_reference(field_id: str) -> ModelRegistry:
    """Build a registry from the shipped contract less the ``reference`` declaration on *field_id*."""
    contract = copy.deepcopy(load_contract())
    fields = [
        field
        for layer in contract["layers"]
        for kind in layer["kinds"]
        for field in kind["fields"]
        if field["id"] == field_id
    ]
    assert len(fields) == 1 and "reference" in fields[0], field_id
    del fields[0]["reference"]
    return build_model_registry(contract=contract, binding=load_binding())


def _composition_member_type() -> tuple[ModelSnapshot, str, str, Dependent]:
    root = Composition(name="Root", title="Root", root=True, members=[CompositionMember(name="m", type="Sensor")])
    snapshot = make_snapshot(swcs=[SWComponent(name="Sensor")], compositions=[root])
    return snapshot, "Sensor", "swc", Dependent(ElementRef("composition", "Root"), (EdgeDetail("member", "m"),))


def _composition_member_composition() -> tuple[ModelSnapshot, str, str, Dependent]:
    root = Composition(name="Root", title="Root", root=True, members=[CompositionMember(name="m", composition="Leaf")])
    snapshot = make_snapshot(compositions=[Composition(name="Leaf", title="Leaf"), root])
    return snapshot, "Leaf", "composition", Dependent(ElementRef("composition", "Root"), (EdgeDetail("member", "m"),))


def _composition_port_interface() -> tuple[ModelSnapshot, str, str, Dependent]:
    iface = SRInterface(
        name="ISignal", kind="sender_receiver", data_elements=[DataElement(name="value", implementation_type="uint8")]
    )
    root = Composition(
        name="Root", title="Root", root=True, ports=[{"name": "rIn", "direction": "require", "interface": "ISignal"}]
    )
    snapshot = make_snapshot(compositions=[root], interfaces=[iface])
    return snapshot, "ISignal", "interface", Dependent(ElementRef("composition", "Root"), (EdgeDetail("port", "rIn"),))


def _connector_composition() -> tuple[ModelSnapshot, str, str, Dependent]:
    inside = make_connector("Inside", "SpeedSensor", "pOut", "SpeedControl", "rIn")
    inside.composition = "Root"
    snapshot = make_snapshot(compositions=[Composition(name="Root", title="Root", root=True)], connectors=[inside])
    return snapshot, "Root", "composition", Dependent(ElementRef("connector", "Inside"))


def _connector_endpoint_composition() -> tuple[ModelSnapshot, str, str, Dependent]:
    across = Connector(
        name="Across",
        **{"from": {"composition": "Leaf", "port": "pOut"}, "to": {"composition": "Other", "port": "rIn"}},
    )
    snapshot = make_snapshot(compositions=[Composition(name="Leaf", title="Leaf")], connectors=[across])
    return (
        snapshot,
        "Leaf",
        "composition",
        Dependent(ElementRef("connector", "Across"), (EdgeDetail("endpoint", "Leaf.pOut"),)),
    )


#: Per declared reference touching the compositions layer: the snapshot, the start element,
#: its layer token, and the one dependent the edge yields.
_COMPOSITION_EDGE_CASES = {
    "field:compositions.composition_member.type": _composition_member_type,
    "field:compositions.composition_member.composition": _composition_member_composition,
    "field:compositions.composition_port.interface": _composition_port_interface,
    "field:connectors.connector.composition": _connector_composition,
    "field:connectors.connector_endpoint.composition": _connector_endpoint_composition,
}


def test_the_five_new_references_are_walked_by_the_generic_builder_and_not_by_impact_code() -> None:
    """Each composition edge is walked because the contract declares it, and only then.

    Every declared reference starting or ending in ``compositions`` (read off the shipped registry,
    so a new one has no case and fails) is answered under the shipped registry and under one with
    that ``reference`` removed; an edge walked by code of its own would still be reported the
    second time.

    Does not reach a compositions-layer name in ``impact.py`` that walks nothing, nor
    ``query/edges.py``'s ``_EDGE_DETAILS``, which decides an edge's detail line and never whether
    it is walked.
    """
    touching_compositions = {
        edge.field_id
        for edge in production_registry().references()
        if "compositions" in (edge.source_layer, edge.target_layer)
    }
    assert touching_compositions == set(_COMPOSITION_EDGE_CASES)

    for field_id, case in _COMPOSITION_EDGE_CASES.items():
        snapshot, start, layer, dependent = case()
        assert dependent in impact_of(snapshot, start)[layer], field_id

        with use_registry(_without_the_reference(field_id)):
            undeclared = impact_of(snapshot, start)
        assert dependent.ref not in {each.ref for each in undeclared[layer]}, field_id


# hardware: memory <- SWC, core <- partition <- task, core <- memory


def _memory(name, *, size=1024, threshold=0.8, accessible=("CORE0",)) -> Memory:
    return Memory(
        name=name,
        kind="memory",
        type="RAM",
        size=size,
        accessible=list(accessible),
        threshold=threshold,
    )


def test_memory_lists_swcs_declaring_that_budget_with_the_budget_detail():
    """A memory lists the SWCs whose ``memory`` key names it, with the declared budget as detail."""
    ram = _memory("RAM0", size=262144)
    swc_a = SWComponent(name="EngineController", memory={"RAM0": 2048})
    swc_b = SWComponent(name="SpeedSensorSWC", memory={"RAM0": 512, "ROM": 8192})
    swc_c = SWComponent(name="NoBudgetSWC")
    snapshot = make_snapshot(hardware=[ram], swcs=[swc_a, swc_b, swc_c])

    result = impact_of(snapshot, "RAM0")

    deps_by_ref = {d.ref: d.detail for d in result["hardware"]}
    assert deps_by_ref == {
        ElementRef("swc", "EngineController"): "memory budget 2048 bytes",
        ElementRef("swc", "SpeedSensorSWC"): "memory budget 512 bytes",
    }


def test_memory_impact_is_transitive_through_the_consuming_swc():
    """A task mapping a runnable of a budget-declaring SWC is a transitive dependent of the memory."""
    ram = _memory("RAM0")
    swc = SWComponent(name="EngineController", memory={"RAM0": 256})
    task = Task(
        name="Task_10ms",
        period=0.01,
        priority=1,
        partition="PARTITION0",
        runnables=[TaskRunnableRef(swc="EngineController", runnable="Run")],
    )
    snapshot = make_snapshot(hardware=[ram], swcs=[swc], tasks=[task])

    result = impact_of(snapshot, "RAM0")

    refs = {d.ref for d in result["hardware"]}
    assert ElementRef("swc", "EngineController") in refs
    assert ElementRef("task", "Task_10ms") in refs


def test_memory_with_no_consumers_reports_empty_dependents():
    snapshot = make_snapshot(hardware=[_memory("RAM0")], swcs=[SWComponent(name="Lonely")])

    assert impact_of(snapshot, "RAM0") == {"hardware": []}


def test_core_lists_the_partitions_on_it_and_their_tasks():
    """A core reaches the partitions on it and their tasks in two hops, each detail naming its element."""
    core2 = Core(name="CORE2", kind="core", threshold=0.7)
    partition2 = Partition(name="PARTITION2", kind="partition", core="CORE2")
    partition0 = Partition(name="PARTITION0", kind="partition", core="CORE0")
    on_core2_a = Task(name="Task_1ms", period=0.001, priority=30, partition="PARTITION2")
    on_core2_b = Task(name="Task_5ms", period=0.005, priority=20, partition="PARTITION2")
    other_core = Task(name="Task_10ms", period=0.01, priority=10, partition="PARTITION0")
    snapshot = make_snapshot(hardware=[core2, partition2, partition0], tasks=[on_core2_a, on_core2_b, other_core])

    result = impact_of(snapshot, "CORE2")

    deps_by_ref = {d.ref: d.detail for d in result["hardware"]}
    assert deps_by_ref == {
        ElementRef("hardware", "PARTITION2"): "core CORE2",
        ElementRef("task", "Task_1ms"): "partition PARTITION2",
        ElementRef("task", "Task_5ms"): "partition PARTITION2",
    }


def test_core_impact_is_transitive_through_the_assigned_task():
    core0 = Core(name="CORE0", kind="core", threshold=0.7)
    partition0 = Partition(name="PARTITION0", kind="partition", core="CORE0")
    task = Task(name="Task_10ms", period=0.01, priority=1, partition="PARTITION0")
    seq = Sequence(name="Seq1", task="Task_10ms", steps=[SequenceStep(swc="A", runnable="Run")])
    snapshot = make_snapshot(hardware=[core0, partition0], tasks=[task], sequences=[seq])

    result = impact_of(snapshot, "CORE0")

    refs = {d.ref for d in result["hardware"]}
    assert ElementRef("task", "Task_10ms") in refs
    assert ElementRef("sequence", "Seq1") in refs


def test_core_with_no_matching_task_reports_empty_dependents():
    core7 = Core(name="CORE7", kind="core", threshold=0.7)
    task = Task(name="Task_10ms", period=0.01, priority=1, partition="PARTITION0")
    snapshot = make_snapshot(hardware=[core7], tasks=[task])

    assert impact_of(snapshot, "CORE7") == {"hardware": []}


def test_a_memory_depends_on_every_core_it_admits():
    """A memory depends on every core its ``accessible`` names, the one edge inside a single layer.

    The node is the core and the dependent is the memory, as ``direction: target-to-source`` states.
    """
    core0 = Core(name="CORE0", kind="core", threshold=0.7)
    core1 = Core(name="CORE1", kind="core", threshold=0.6)
    shared = _memory("SHARED", accessible=("CORE0", "CORE1"))
    private = _memory("PRIVATE0", accessible=("CORE0",))
    snapshot = make_snapshot(hardware=[core0, core1, shared, private])

    assert {d.ref for d in impact_of(snapshot, "CORE0")["hardware"]} == {
        ElementRef("hardware", "SHARED"),
        ElementRef("hardware", "PRIVATE0"),
    }
    assert [d.detail for d in impact_of(snapshot, "CORE1")["hardware"]] == ["accessible from CORE1"]
    assert impact_of(snapshot, "SHARED") == {"hardware": []}


def test_a_hardware_name_resolves_through_bare_name_lookup_across_all_layers():
    """Bare-name lookup spans every layer: a name shared by hardware and another layer reports each match."""
    ram = _memory("Shared")
    swc = SWComponent(name="Shared", memory={"Shared": 64})
    snapshot = make_snapshot(hardware=[ram], swcs=[swc])

    result = impact_of(snapshot, "Shared")

    assert set(result.keys()) == {"swc", "hardware"}
    assert result["hardware"] == [Dependent(ref=ElementRef("swc", "Shared"), details=(EdgeDetail("budget", "64"),))]


def test_duplicate_hardware_name_in_one_snapshot_raises_value_error():
    """A duplicate hardware identity key is a hard error, not a silently dropped element."""
    snapshot = make_snapshot(hardware=[_memory("Dup", size=1024), _memory("Dup", size=2048)])

    with pytest.raises(ValueError, match="Dup"):
        build_reference_graph(snapshot)


def test_format_impact_renders_hardware_dependents_with_the_generic_layer_label():
    ram = _memory("RAM0")
    swc = SWComponent(name="EngineController", memory={"RAM0": 2048})
    snapshot = make_snapshot(hardware=[ram], swcs=[swc])

    rendered = format_impact("RAM0", impact_of(snapshot, "RAM0"))

    assert "Impact of 'RAM0':" in rendered
    assert "hardware:" in rendered
    assert "swc EngineController (memory budget 2048 bytes)" in rendered


# no dependents / no match / cross-layer collision / cycle safety


def test_element_with_no_dependents_returns_empty_list_not_error():
    swc = SWComponent(name="Lonely")
    snapshot = make_snapshot(swcs=[swc])

    result = impact_of(snapshot, "Lonely")

    assert result == {"swc": []}


def test_name_matching_no_layer_returns_empty_dict():
    snapshot = make_snapshot(swcs=[SWComponent(name="A")])

    result = impact_of(snapshot, "NoSuchElement")

    assert result == {}


def test_name_matching_two_different_layers_reports_both_separately():
    swc = SWComponent(name="Shared")
    task = Task(name="Shared", period=0.01, priority=1, partition="PARTITION0")
    snapshot = make_snapshot(swcs=[swc], tasks=[task])

    result = impact_of(snapshot, "Shared")

    assert set(result.keys()) == {"swc", "task"}


def test_self_referencing_cycle_does_not_infinite_loop():
    """A record whose field names the record itself is a real self-loop, and ``impact_of`` terminates on it.

    Pydantic does not reject the self-reference (unlike a value type's ``base``), and the real
    ``impact_of``/``build_reference_graph`` is exercised, not a copied traversal.
    """
    self_referencing = RecordDataType(name="X", kind="record", fields=[RecordField(name="f", type="X")])
    snapshot = make_snapshot(application_types=[self_referencing])

    graph = build_reference_graph(snapshot)
    assert graph[ElementRef("application_type", "X")] == {ElementRef("application_type", "X"): ()}

    result = impact_of(snapshot, "X")

    # The self-loop must not report X as its own dependent.
    assert result == {"application_type": []}


# A duplicate identity key is a hard error


def test_duplicate_swc_name_in_one_snapshot_raises_value_error_from_build_reference_graph():
    snapshot = make_snapshot(swcs=[SWComponent(name="Dup"), SWComponent(name="Dup")])

    with pytest.raises(ValueError, match="Dup"):
        build_reference_graph(snapshot)


def test_duplicate_swc_name_in_one_snapshot_raises_value_error_from_impact_of():
    snapshot = make_snapshot(swcs=[SWComponent(name="Dup"), SWComponent(name="Dup")])

    with pytest.raises(ValueError, match="Dup"):
        impact_of(snapshot, "Dup")


# The layer token is a declared contract fact


def _taxes_registry() -> ModelRegistry:
    """Build a registry whose synthetic layer is renamed ``taxes`` and declares the token ``tax``.

    The synthetic layer has no ``snapshotAttributes`` row, so stripping a trailing ``s`` from the
    layer name, the rule the declaration replaces, would answer ``taxe``.
    """
    contract = merged_contract()
    layer = next(entry for entry in contract["layers"] if entry["name"] == "synthetic")
    layer["name"] = "taxes"
    layer["storage"]["directory"] = "taxes"
    layer["token"] = "tax"
    return build_model_registry(contract=contract, binding=merged_binding())


def test_a_layer_whose_token_the_old_rule_cannot_derive_round_trips_through_impact() -> None:
    """The reverse graph's node, ``impact_of``'s key and the report all say ``tax``, never ``taxe``."""
    registry = _taxes_registry()
    state = ModelState(
        layers={
            "compositions": [Composition(name="S", title="The referenced composition")],
            "taxes": [SyntheticElement(name="T1", title="One", composition="S")],
        }
    )

    with use_registry(registry):
        snapshot = ModelSnapshot.from_state(state, registry=registry)
        graph = build_reference_graph(snapshot)
        result = impact_of(snapshot, "S")
        report = format_impact("S", result)
        # Start-element key maps are keyed by token too; only a start in `taxes` reaches them.
        started_from_the_layer = impact_of(snapshot, "T1")

    dependent = ElementRef("tax", "T1")
    assert graph[ElementRef("composition", "S")] == {dependent: ()}
    assert result == {"composition": [Dependent(dependent)]}
    assert "  tax T1" in report
    assert started_from_the_layer == {"tax": []}

    assert "taxe" not in report, "the derivation this declaration replaced would have answered `taxe`"
    assert registry.layer("taxes").snapshot_attribute is None, (
        "the case only holds while the layer has no adapter attribute to derive from"
    )


def test_the_declared_token_is_what_impact_publishes_for_every_shipped_layer() -> None:
    """``ElementRef.layer`` is ``LayerFacts.token`` for every shipped layer, with no second rule.

    Asserted over what ``impact_of`` publishes for one element per layer, not over ``layer_token``'s
    body, so a call site that grew its own layer-naming rule fails here.
    """
    registry = production_registry()
    snapshot = make_snapshot(
        compositions=[Composition(name="OneComposition", title="T", root=True)],
        swcs=[SWComponent(name="OneSWC")],
        interfaces=[
            SRInterface(
                name="OneInterface",
                kind="sender_receiver",
                data_elements=[DataElement(name="d", implementation_type="OneDataType")],
            )
        ],
        # A value type over a base type, so both layers carry an element.
        implementation_types=[ValueDataType(name="OneDataType", kind="value_type", base="OneBaseType")],
        base_types=[BaseDataType(name="OneBaseType", kind="base_type", size=32, encoding="IEEE754")],
        application_types=[ApplicationDataType(name="OneApplicationType", kind="application_type")],
        connectors=[make_connector("OneConnector", "OneSWC", "pA", "OneSWC", "pB")],
        requirements=[Requirement(id="REQ-ONE", title="T", description="D", version=1, source="architecture")],
        tasks=[
            Task(
                name="OneTask",
                period=0.01,
                priority=1,
                partition="PARTITION0",
                runnables=[TaskRunnableRef(swc="OneSWC", runnable="Run")],
            )
        ],
        sequences=[Sequence(name="OneSequence", task="OneTask", steps=[SequenceStep(swc="OneSWC", runnable="Run")])],
        hardware=[Memory(name="OneMemory", kind="memory", type="RAM", size=1024, accessible=["CORE0"], threshold=0.8)],
        mapping_sets=[MappingSet(name="OneMappingSet")],
        units=[Unit(name="OneUnit")],
        compu_methods=[Linear(name="OneCompuMethod", kind="linear", unit="OneUnit")],
        data_constraints=[DataConstr(name="OneDataConstr")],
    )
    named = {
        "compositions": "OneComposition",
        "swc": "OneSWC",
        "interfaces": "OneInterface",
        "implementation_types": "OneDataType",
        "connectors": "OneConnector",
        "requirements": "REQ-ONE",
        "tasks": "OneTask",
        "sequences": "OneSequence",
        "hardware": "OneMemory",
        "mapping_sets": "OneMappingSet",
        "base_types": "OneBaseType",
        "application_types": "OneApplicationType",
        "units": "OneUnit",
        "compu_methods": "OneCompuMethod",
        "data_constraints": "OneDataConstr",
    }
    assert set(named) == set(registry.layer_names), "the fixture no longer covers every declared layer"

    published = {layer: sorted(impact_of(snapshot, identity)) for layer, identity in named.items()}
    assert published == {layer: [registry.layer(layer).token] for layer in named}, (
        "a layer's published token is not the one the contract declares"
    )

    # And the graph's own refs, which come from the other two call sites.
    refs = {
        ref for referenced, dependents in build_reference_graph(snapshot).items() for ref in (referenced, *dependents)
    }
    assert refs, "the fixture produced no edges; the graph half of this assertion reads nothing"
    tokens = {layer.token for layer in registry.layers}
    assert {ref.layer for ref in refs} <= tokens, sorted({ref.layer for ref in refs} - tokens)


# The two forms: the full renderer is frozen, the summary drops the detail


#: SHA-256 of ``swcraft impact --project examples/basic/swcraft.yaml NAME``'s stdout, recorded
#: before the summary form existed: ``format_impact``'s text plus the newline ``print`` adds.
#: The output names no path, so the digest is checkout-independent. Not re-recorded when the
#: model or the report moved: each later change is undone before hashing (the helpers below),
#: so the recorded bytes still grade every other line.
BASIC_IMPACT_DIGESTS: dict[str, str] = {
    "IExample": "b04a53519d31e1cd2c4ad17815670f1a60bc87ffc716c672ff970bd4bd3d7286",
    "ExampleSWC": "31f8cbb0fea0ff48a39520106c45636b4971a1b101b2a403f6fb1e2d3104c8f5",
    "RAM0": "a673b239eb10395f038e9bad2cb0ca175822f399e28433fe57590cf2c0a655f0",
}

#: The prefix of the composition rows the report gained when ``examples/basic`` gained a root
#: composition; they are removed before hashing.
COMPOSITION_ROW_PREFIX = "  composition "

#: The removed composition rows per report, as a list so their number and order are pinned and
#: the subtraction cannot pass by removing nothing or too much.
BASIC_IMPACT_COMPOSITION_ROWS: dict[str, list[str]] = {
    "IExample": ["  composition Root (members ExampleSWC, ExampleConsumerSWC)\n"],
    "ExampleSWC": ["  composition Root (member ExampleSWC)\n"],
    "RAM0": ["  composition Root (members ExampleSWC, ExampleConsumerSWC)\n"],
}


#: The connector lines extended since the recording to name their endpoints, as ``(today,
#: recorded)``; undone before hashing, and the undo asserts it found exactly these lines.
_BOTH_ENDPOINTS = "  connector Example_to_Consumer (via ports ExampleSWC.pOut, ExampleConsumerSWC.rIn)\n"
EXTENDED_CONNECTOR_ROWS: dict[str, list[tuple[str, str]]] = {
    "IExample": [(_BOTH_ENDPOINTS, "  connector Example_to_Consumer\n")],
    "ExampleSWC": [
        ("  connector Example_to_Consumer (via port ExampleSWC.pOut)\n", "  connector Example_to_Consumer\n")
    ],
    "RAM0": [(_BOTH_ENDPOINTS, "  connector Example_to_Consumer\n")],
}


def _restore_11_0_rows(rendered: str, name: str) -> str:
    """Put back the requirement rows that left the reports when the link moved into the element.

    Applied before the composition split, because the recorded positions count the composition rows.
    """
    return restore_requirement_lines(rendered, "examples/basic/model", name)


def _undo_extended_connector_rows(rendered: str, name: str) -> str:
    """Restore the recorded connector lines, after checking each extended one is there once."""
    for today, recorded in EXTENDED_CONNECTOR_ROWS[name]:
        assert rendered.count(today) == 1, (today, rendered)
        rendered = rendered.replace(today, recorded)
    return rendered


def _split_composition_rows(rendered: str) -> tuple[str, list[str]]:
    """Return *rendered* without its composition rows, and the rows removed."""
    lines = rendered.splitlines(keepends=True)
    added = [line for line in lines if line.startswith(COMPOSITION_ROW_PREFIX)]
    kept = "".join(line for line in lines if not line.startswith(COMPOSITION_ROW_PREFIX))
    return kept, added


@pytest.mark.parametrize("name", sorted(BASIC_IMPACT_DIGESTS))
def test_the_full_renderer_is_byte_identical_to_the_recorded_cli_output(name: str) -> None:
    """``format_impact`` still renders the recorded bytes: the oracle is a digest, not a re-derivation."""
    snapshot = load_snapshot(str(REPO_ROOT / "examples" / "basic" / "model"))
    rendered = _restore_11_0_rows(format_impact(name, impact_of(snapshot, name)), name)

    kept, added = _split_composition_rows(rendered)
    assert added == BASIC_IMPACT_COMPOSITION_ROWS[name], added

    digest = hashlib.sha256((_undo_extended_connector_rows(kept + "\n", name)).encode("utf-8")).hexdigest()
    assert digest == BASIC_IMPACT_DIGESTS[name]


@pytest.mark.parametrize("name", sorted(BASIC_IMPACT_DIGESTS))
def test_the_connector_to_component_edges_are_unchanged_by_the_compositions_layer(name: str) -> None:
    """The connector-to-component edge is unchanged by the compositions layer.

    The connector row is in the kept half, no added row is a connector's, and the endpoint field
    still resolves into ``swc`` per the shipped registry. Does not reach an endpoint that names a
    composition: ``examples/basic`` has none.
    """
    snapshot = load_snapshot(str(REPO_ROOT / "examples" / "basic" / "model"))
    rendered = _restore_11_0_rows(format_impact(name, impact_of(snapshot, name)), name)

    kept, added = _split_composition_rows(rendered)

    assert added == BASIC_IMPACT_COMPOSITION_ROWS[name], added
    assert EXTENDED_CONNECTOR_ROWS[name][0][0] in kept, kept
    assert not any("connector" in row for row in added), added

    endpoint = next(
        edge
        for edge in production_registry().references()
        if edge.field_id == "field:connectors.connector_endpoint.swc"
    )
    assert endpoint.target_layer == "swc"


def test_the_summary_groups_dependents_by_their_own_layer_in_declared_display_order():
    """The summary regroups dependents by their own layer in ``registry.display_order``, with counts and no detail."""
    result = {
        "interface": [
            Dependent(ElementRef("sequence", "cycle"), (EdgeDetail("step", "Run"),)),
            Dependent(ElementRef("swc", "Consumer"), (EdgeDetail("port", "rIn"),)),
            Dependent(ElementRef("swc", "Producer"), (EdgeDetail("port", "pOut"),)),
            Dependent(ElementRef("requirement", "REQ-1")),
            Dependent(ElementRef("connector", "P_to_C")),
        ]
    }

    rendered = format_impact_summary("ISpeed", result)

    assert rendered.splitlines() == [
        "Impact of 'ISpeed' (summary):",
        "",
        "interface:",
        "  requirement (1): REQ-1",
        "  swc (2): Consumer, Producer",
        "  connector (1): P_to_C",
        "  sequence (1): cycle",
    ]


def test_the_summary_carries_no_detail_string_the_full_form_renders():
    """The summary carries no detail string of the full form.

    The property is the absence of detail strings, never a share of the size: on a hub interface
    the identities are the bulk.
    """
    result = {"hardware": [Dependent(ElementRef("swc", "EngineController"), (EdgeDetail("budget", "2048"),))]}

    full = format_impact("RAM0", result)
    summary = format_impact_summary("RAM0", result)

    assert "memory budget 2048 bytes" in full
    assert "memory budget 2048 bytes" not in summary
    assert "EngineController" in summary
    assert len(summary) < len(full)


def test_the_summary_keeps_the_matched_layer_grouping_of_the_full_form():
    """A name matching two layers reports both, in the summary too."""
    result = {
        "swc": [Dependent(ElementRef("connector", "C1"))],
        "interface": [Dependent(ElementRef("swc", "S1"), (EdgeDetail("port", "p"),))],
    }

    rendered = format_impact_summary("Ambiguous", result)

    assert "\nswc:\n" in rendered
    assert "\ninterface:\n" in rendered


def test_a_match_with_no_dependents_says_so_in_both_forms():
    """A match with no dependents renders ``(no dependents)`` in both forms."""
    assert format_impact_summary("Lonely", {"swc": []}).splitlines() == [
        "Impact of 'Lonely' (summary):",
        "",
        "swc:",
        "  (no dependents)",
    ]
    assert format_impact("Lonely", {"swc": []}).splitlines() == [
        "Impact of 'Lonely':",
        "",
        "swc:",
        "  (no dependents)",
    ]


def test_the_summary_not_found_message_is_the_full_form_s_byte_for_byte():
    """The not-found line is the same in both forms."""
    assert format_impact_summary("NoSuchElement", {}) == format_impact("NoSuchElement", {})
    assert format_impact_summary("NoSuchElement", {}) == "Element 'NoSuchElement' not found in any layer."


def test_a_dependent_layer_the_registry_does_not_declare_is_rendered_last_not_dropped():
    """A dependent token the registry does not declare sorts last and is still rendered.

    Not a supported input; a summary that dropped a dependent is a failure no count would show.
    """
    result = {
        "swc": [
            Dependent(ElementRef("undeclared_layer", "X")),
            Dependent(ElementRef("connector", "C1")),
        ]
    }

    rendered = format_impact_summary("Thing", result)

    assert rendered.splitlines()[2:] == ["swc:", "  connector (1): C1", "  undeclared_layer (1): X"]


# The direction, the depth and the page. The walks are `test_query.py`'s; this section holds the
# surface `impact_of` publishes over them and the page that windows a rendered report.


def _two_hop_snapshot() -> ModelSnapshot:
    """``uint8 <- ISig <- {S0, S1} <- C0``: the smallest model that tells direction, depth and page apart."""
    return make_snapshot(
        # An external type stub with no reference of its own, so the walk ends at it.
        implementation_types=[
            ExternalDataType(
                name="uint8",
                kind="external_type",
                external_ref="/Synthetic/uint8",
                external_dest="IMPLEMENTATION-DATA-TYPE",
            )
        ],
        interfaces=[
            SRInterface(
                name="ISig", kind="sender_receiver", data_elements=[DataElement(name="v", implementation_type="uint8")]
            )
        ],
        swcs=[
            SWComponent(name="S0", ports=[Port(name="pOut", direction="provide", interface="ISig")]),
            SWComponent(name="S1", ports=[Port(name="rIn", direction="require", interface="ISig")]),
        ],
        connectors=[make_connector("C0", "S0", "pOut", "S1", "rIn")],
    )


def test_the_two_directions_are_the_same_edges_read_from_opposite_ends():
    """Forward goes through ``port.interface``; reverse is the default answer."""
    snapshot = _two_hop_snapshot()

    reverse = impact_of(snapshot, "ISig")
    forward = impact_of(snapshot, "S0", direction="dependencies")

    assert sorted(dep.ref.name for dep in reverse["interface"]) == ["C0", "S0", "S1"]
    assert sorted((dep.ref.layer, dep.ref.name) for dep in forward["swc"]) == [
        ("implementation_type", "uint8"),
        ("interface", "ISig"),
    ]
    # The detail names the edge, not the end, so it reads the same both ways.
    assert [dep.detail for dep in forward["swc"] if dep.ref.name == "ISig"] == ["via port pOut"]


def test_an_unknown_direction_is_refused_by_the_engines_own_vocabulary():
    """An unknown direction is refused with the engine's own vocabulary."""
    with pytest.raises(ValueError, match="unknown direction 'sideways'"):
        impact_of(_two_hop_snapshot(), "ISig", direction="sideways")


def test_the_published_direction_vocabulary_and_the_branch_that_resolves_it_agree():
    """``DIRECTIONS`` and the names ``Graphs.direction`` resolves agree, both ways round."""
    graphs = build_graphs(_two_hop_snapshot().state)

    assert DIRECTIONS == ("dependents", "dependencies")
    assert DEFAULT_DIRECTION in DIRECTIONS
    # `is not None` would pass with the two branches swapped.
    assert graphs.direction("dependents") is graphs.reverse
    assert graphs.direction("dependencies") is graphs.forward

    # A branch name `DIRECTIONS` does not carry is invisible to every call, so the branch
    # literals are read off the source.
    source = textwrap.dedent(inspect.getsource(query.Graphs.direction))
    branched = tuple(
        node.comparators[0].value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "name"
        and isinstance(node.comparators[0], ast.Constant)
    )
    assert branched == DIRECTIONS, "`Graphs.direction` resolves a vocabulary `DIRECTIONS` does not publish"

    for name in ("", "reverse", "forward", "Dependents"):
        with pytest.raises(ValueError):
            graphs.direction(name)


def test_depth_one_is_the_first_level_and_the_default_is_every_hop():
    """The depth bound cuts rows, never detail."""
    snapshot = _two_hop_snapshot()

    everything = impact_of(snapshot, "uint8")["implementation_type"]
    one_hop = impact_of(snapshot, "uint8", depth=1)["implementation_type"]

    assert sorted((dep.ref.layer, dep.ref.name) for dep in everything) == [
        ("connector", "C0"),
        ("interface", "ISig"),
        ("swc", "S0"),
        ("swc", "S1"),
    ]
    assert [(dep.ref.layer, dep.ref.name) for dep in one_hop] == [("interface", "ISig")]
    assert [dep.detail for dep in one_hop] == [dep.detail for dep in everything[:1]]


def test_a_depth_below_one_is_refused_rather_than_answered_empty():
    """``0`` can only be a caller's mistake: ``1`` is already the first level."""
    with pytest.raises(ValueError, match="depth must be at least 1"):
        impact_of(_two_hop_snapshot(), "uint8", depth=0)


def test_naming_the_two_new_parameters_at_their_defaults_changes_nothing():
    """Naming direction and depth at their defaults is the unnamed call.

    Not a backward-compatibility oracle: both sides are today's code. The recorded bytes are
    ``test_query.py``'s.
    """
    snapshot = _two_hop_snapshot()

    assert impact_of(snapshot, "uint8") == impact_of(snapshot, "uint8", direction="dependents", depth=None)
    assert format_impact("uint8", impact_of(snapshot, "uint8")) == format_impact(
        "uint8", impact_of(snapshot, "uint8", direction=DEFAULT_DIRECTION, depth=None)
    )


# The page


def _rows(result: dict[str, list[Dependent]]) -> list[tuple[str, str]]:
    return [(dep.ref.layer, dep.ref.name) for deps in result.values() for dep in deps]


def test_a_page_reports_the_whole_set_and_windows_it():
    """``total`` is pre-window; ``returned`` is the window; ``next`` continues it."""
    result = impact_of(_two_hop_snapshot(), "uint8")

    windowed, page = page_impact(result, 2)

    assert page.total == 4
    assert page.returned == 2
    assert page.truncated is True
    assert page.next == impact_cursor("implementation_type", result["implementation_type"][1].ref)
    assert _rows(windowed) == _rows(result)[:2]


def test_truncated_is_not_next_s_presence_and_the_two_come_apart_at_both_ends():
    """A complete answer is untruncated; the last window is truncated with no ``next``."""
    result = impact_of(_two_hop_snapshot(), "uint8")

    whole, whole_page = page_impact(result, 99)
    assert (whole_page.total, whole_page.returned, whole_page.truncated, whole_page.next) == (4, 4, False, None)
    assert _rows(whole) == _rows(result)

    _, first = page_impact(result, 3)
    _, last = page_impact(result, 3, first.next)
    assert (last.returned, last.truncated, last.next) == (1, True, None)


def test_a_cursor_walk_covers_the_listing_exactly_once_and_in_the_report_s_order():
    """A cursor walk returns every row once, in the order the report renders them."""
    result = impact_of(_two_hop_snapshot(), "uint8")

    walked: list[tuple[str, str]] = []
    cursor: str | None = None
    for _ in range(10):
        windowed, page = page_impact(result, 1, cursor)
        walked.extend(_rows(windowed))
        cursor = page.next
        if cursor is None:
            break

    assert walked == _rows(result)


def test_after_the_last_row_is_an_empty_window_and_not_a_refusal():
    """A walk ending exactly on the boundary reports an empty window, not a refusal."""
    result = impact_of(_two_hop_snapshot(), "uint8")
    last_layer, last_name = _rows(result)[-1]

    windowed, page = page_impact(result, 2, impact_cursor("implementation_type", ElementRef(last_layer, last_name)))

    assert _rows(windowed) == []
    assert (page.total, page.returned, page.truncated, page.next) == (4, 0, True, None)


def test_a_cursor_no_row_carries_is_refused_rather_than_read_as_the_beginning():
    """A cursor no row carries is refused, so an edit that removed the row is visible."""
    result = impact_of(_two_hop_snapshot(), "uint8")

    with pytest.raises(ImpactCursorNotFound, match="implementation_type/swc:Gone"):
        page_impact(result, 2, "implementation_type/swc:Gone")


def test_a_limit_below_one_is_refused():
    with pytest.raises(ValueError, match="limit must be at least 1"):
        page_impact(impact_of(_two_hop_snapshot(), "uint8"), 0)


@pytest.mark.parametrize("limit", [True, False, 1.0, 2.5, "2"])
def test_a_limit_that_is_not_a_whole_number_is_refused_here_too(limit):
    """A non-integer limit is refused by the same ``query.edges.check_limit`` guard ``slice_layer`` uses.

    Guards ``True`` paging one row and ``1.0`` escaping as a ``TypeError``. Does not reach the wire:
    the transport coerces or refuses a non-integer first (``packages/mcp/tests/test_diff_impact_tools.py``).
    """
    with pytest.raises(ValueError, match="whole number of rows"):
        page_impact(impact_of(_two_hop_snapshot(), "uint8"), limit)


@pytest.mark.parametrize("depth", [True, False, 1.0, "1"])
def test_a_depth_that_is_not_a_whole_number_is_refused_at_this_surface(depth):
    """``impact_of`` keeps the walk's guard against a non-integer depth."""
    with pytest.raises(ValueError, match="whole number or None"):
        impact_of(_two_hop_snapshot(), "uint8", depth=depth)


def test_a_matched_layer_with_no_rows_at_all_survives_every_window():
    """A matched layer with no rows keeps its ``(no dependents)`` section in every window.

    Includes an empty match beside a windowed layer: the all-empty case alone exercises no window.
    """
    result = {"implementation_type": [], "swc": []}

    windowed, page = page_impact(result, 1)

    assert windowed == {"implementation_type": [], "swc": []}
    assert (page.total, page.returned, page.truncated, page.next) == (0, 0, False, None)

    beside = {
        "implementation_type": [],
        "interface": [Dependent(ElementRef("swc", "S0")), Dependent(ElementRef("swc", "S1"))],
    }
    first, page = page_impact(beside, 1)
    assert first == {"implementation_type": [], "interface": [Dependent(ElementRef("swc", "S0"))]}
    assert (page.total, page.returned, page.truncated) == (2, 1, True)

    second, page = page_impact(beside, 1, page.next)
    assert second == {"implementation_type": [], "interface": [Dependent(ElementRef("swc", "S1"))]}
    assert (page.total, page.returned, page.truncated, page.next) == (2, 1, True, None)


def test_a_layer_whose_rows_are_all_outside_the_window_contributes_no_section():
    """A layer with no rows in the window contributes no section: an empty one would read "no dependents"."""
    result = {
        "interface": [Dependent(ElementRef("swc", "S0"), (EdgeDetail("port", "pOut"),))],
        "swc": [Dependent(ElementRef("connector", "C0"))],
    }

    windowed, page = page_impact(result, 1)

    assert windowed == {"interface": [Dependent(ElementRef("swc", "S0"), (EdgeDetail("port", "pOut"),))]}
    assert page.next == "interface/swc:S0"


def test_a_cursor_carries_the_matched_layer_so_one_name_in_two_layers_cannot_collide():
    """A cursor carries the matched layer, so one name matched in two layers cannot alias."""
    shared = Dependent(ElementRef("connector", "C0"))
    result = {"interface": [shared], "swc": [shared]}

    cursors = [impact_cursor(layer, dep.ref) for layer, deps in result.items() for dep in deps]

    assert cursors == ["interface/connector:C0", "swc/connector:C0"]
    assert len(set(cursors)) == 2
    windowed, page = page_impact(result, 1, cursors[0])
    assert windowed == {"swc": [shared]}
    assert page.next is None


# The scope footer


def test_the_scope_footer_is_empty_at_every_default():
    """At every default the footer is empty, so a plain report is unchanged."""
    assert format_impact_scope() == ""
    assert format_impact_scope(direction=DEFAULT_DIRECTION, depth=None, page=None) == ""


def test_the_scope_footer_names_each_non_default_and_only_those():
    assert format_impact_scope(direction="dependencies") == "(direction: dependencies)"
    assert format_impact_scope(depth=2) == "(depth: 2)"
    assert format_impact_scope(page=ImpactPage(total=4, returned=2, truncated=True, next="a/b:c")) == (
        '(page: 2 of 4, truncated; next: after="a/b:c")'
    )
    assert format_impact_scope(page=ImpactPage(total=4, returned=4, truncated=False)) == "(page: 4 of 4, complete)"
    assert (
        format_impact_scope(direction="dependencies", depth=1, page=ImpactPage(total=9, returned=1, truncated=True))
        == "(direction: dependencies; depth: 1; page: 1 of 9, truncated)"
    )
    kinds = (("swc", 3), ("connector", 1))
    assert format_impact_scope(page=ImpactPage(total=4, returned=2, truncated=True, next="a/b:c", kinds=kinds)) == (
        '(page: 2 of 4, truncated; in all: swc 3, connector 1; next: after="a/b:c")'
    )
    assert format_impact_scope(page=ImpactPage(total=4, returned=0, truncated=True, kinds=kinds)) == (
        "(page: 0 of 4, truncated; in all: swc 3, connector 1)"
    )
    assert (
        format_impact_scope(page=ImpactPage(total=4, returned=4, truncated=False, kinds=kinds))
        == "(page: 4 of 4, complete)"
    )


# The per-kind totals of a truncated page


def _mixed_kinds_snapshot() -> ModelSnapshot:
    """Build one interface reached by components, a composition, connectors and a task.

    The first window holds components only, and every other kind lies beyond it.
    """
    iface = SRInterface(
        name="ISig", kind="sender_receiver", data_elements=[DataElement(name="v", implementation_type="uint8")]
    )
    return make_snapshot(
        interfaces=[iface],
        swcs=[
            SWComponent(name="S0", ports=[Port(name="pOut", direction="provide", interface="ISig")]),
            SWComponent(name="S1", ports=[Port(name="rIn", direction="require", interface="ISig")]),
        ],
        compositions=[
            Composition(
                name="Root",
                title="Root",
                root=True,
                ports=[{"name": "rIn", "direction": "require", "interface": "ISig"}],
            )
        ],
        connectors=[make_connector("C0", "S0", "pOut", "S1", "rIn"), make_connector("C1", "S0", "pOut", "S1", "rIn")],
        # A requirement is no dependent of a component, so the fourth kind is a task.
        tasks=[
            Task(
                name="T0", period=0.01, priority=1, partition="P", runnables=[TaskRunnableRef(swc="S0", runnable="R0")]
            )
        ],
    )


def _summary_counts(summary: str) -> list[tuple[str, int]]:
    """Return a one-match summary's ``  <token> (<n>): …`` lines as ``(token, n)``."""
    counts = []
    for line in summary.splitlines():
        if line.startswith("  ") and " (" in line and "): " in line:
            token, rest = line.strip().split(" (", 1)
            counts.append((token, int(rest.split(")", 1)[0])))
    return counts


def test_a_truncated_first_page_of_one_kind_names_every_kind_of_the_whole_report():
    """A truncated first page of components alone names all four kinds of the whole report."""
    result = impact_of(_mixed_kinds_snapshot(), "ISig")

    windowed, page = page_impact(result, 2)

    assert {layer for layer, _ in _rows(windowed)} == {"swc"}
    assert dict(page.kinds) == {"swc": 2, "composition": 1, "connector": 2, "task": 1}
    assert sum(count for _, count in page.kinds) == page.total == 6
    assert list(page.kinds) == _summary_counts(format_impact_summary("ISig", result))
    rendered = format_impact_scope(page=page)
    in_all = ", ".join(f"{token} {count}" for token, count in page.kinds)
    assert rendered == f'(page: 2 of 6, truncated; in all: {in_all}; next: after="{page.next}")'


def test_the_last_page_and_a_page_past_the_end_carry_the_totals_and_a_complete_page_none():
    result = impact_of(_mixed_kinds_snapshot(), "ISig")
    _, first = page_impact(result, 4)

    _, last = page_impact(result, 4, first.next)
    assert (last.returned, last.truncated, last.next) == (2, True, None)
    assert "; in all: " in format_impact_scope(page=last)

    last_layer, last_name = _rows(result)[-1]
    _, past = page_impact(result, 4, impact_cursor("interface", ElementRef(last_layer, last_name)))
    assert (past.returned, past.truncated) == (0, True)
    assert format_impact_scope(page=past).startswith("(page: 0 of 6, truncated; in all: ")

    _, whole = page_impact(result, 99)
    assert whole.truncated is False
    assert format_impact_scope(page=whole) == "(page: 6 of 6, complete)"


def test_the_totals_count_rows_so_a_dependent_under_two_matches_counts_twice():
    shared = Dependent(ElementRef("connector", "C0"))
    result = {"interface": [shared], "swc": [shared, Dependent(ElementRef("task", "T"))]}

    _, page = page_impact(result, 1)

    assert dict(page.kinds) == {"connector": 2, "task": 1}
    assert sum(count for _, count in page.kinds) == page.total == 3


def test_a_kind_the_registry_does_not_declare_ranks_last_in_the_totals():
    result = {
        "swc": [
            Dependent(ElementRef("zz_undeclared", "X")),
            Dependent(ElementRef("aa_undeclared", "Y")),
            Dependent(ElementRef("connector", "C1")),
        ]
    }

    _, page = page_impact(result, 1)

    assert page.kinds == (("connector", 1), ("aa_undeclared", 1), ("zz_undeclared", 1))


# The CLI


#: ``swcraft impact --project examples/basic/swcraft.yaml IExample``'s stdout, measured before
#: the direction, depth and page parameters existed; compared after the same undo steps as
#: ``BASIC_IMPACT_DIGESTS``.
RECORDED_BASELINE_IMPACT: dict[str, object] = {"name": "IExample", "stdout_bytes": 278, "stdout_lines": 9}


def test_the_rendered_report_matches_the_recorded_baseline_exactly(capsys, monkeypatch):
    """The CLI prints ``format_impact``'s bytes and nothing else, matching the recorded baseline.

    The CLI is run, not ``format_impact(...) + "\n"``, so a footer or a second ``print`` in
    ``cmd_impact`` fails; the renderer is asserted beside it, so a divergence says which moved.
    """
    project = REPO_ROOT / "examples" / "basic" / "swcraft.yaml"
    name = RECORDED_BASELINE_IMPACT["name"]

    monkeypatch.setattr("sys.argv", ["swcraft", "impact", "--project", str(project), name])
    cli_main()
    stdout = capsys.readouterr().out

    kept, added = _split_composition_rows(_restore_11_0_rows(stdout, name))
    assert added, "no composition row was removed; the subtraction below would pass vacuously"
    kept = _undo_extended_connector_rows(kept, name)

    assert len(kept.encode("utf-8")) == RECORDED_BASELINE_IMPACT["stdout_bytes"]
    assert len(kept.splitlines()) == RECORDED_BASELINE_IMPACT["stdout_lines"]
    assert hashlib.sha256(kept.encode("utf-8")).hexdigest() == BASIC_IMPACT_DIGESTS[name]

    snapshot = load_snapshot(str(REPO_ROOT / "examples" / "basic" / "model"))
    assert stdout == format_impact(name, impact_of(snapshot, name)) + "\n"


def test_the_impact_subcommand_gained_only_depth_and_format():
    """``swcraft impact`` takes ``--project``, ``--depth`` and ``--format`` beyond the shared options.

    Direction and the page stay the MCP tool's.

    The parser is read, not the help text, through ``conftest``'s ``parser_subcommand_options``.
    """
    # The shared set is the top-level options `parser_subcommand_options` unions into every
    # subcommand; differencing against it is a direct oracle, where an equality with a
    # sibling's flags would read a flag added there as one removed here.
    options = parser_subcommand_options(build_parser())
    shared = frozenset.intersection(*options.values())
    assert "--project" not in shared, "the shared set is the top-level one, so the difference below is `impact`'s"
    assert options["impact"] - shared == frozenset({"--project", "--depth", "--format"})


# The count: a projection of the walked answer, never a second walk

BASIC_MODEL = REPO_ROOT / "examples" / "basic" / "model"


def _summary_counted_per_match(text: str) -> dict[str, dict[str, int]]:
    """Count the summary's rows per matched layer and kind, read off its text (the count's oracle)."""
    counted: dict[str, dict[str, int]] = {}
    section = ""
    for line in text.splitlines()[1:]:
        if line and not line.startswith(" "):
            section = line.rstrip(":")
            counted[section] = {}
        elif line.startswith("  ") and "(" in line and line != "  (no dependents)":
            kind, _, rest = line.strip().partition(" (")
            counted[section][kind] = int(rest.split(")", 1)[0])
    return counted


def _most_used_interface(snapshot: ModelSnapshot) -> str:
    names = [ref.name for ref in build_reference_graph(snapshot) if ref.layer == "interface"]
    return max(sorted(names), key=lambda name: sum(len(deps) for deps in impact_of(snapshot, name).values()))


def test_the_count_of_the_basic_examples_most_used_interface_is_its_summary_counted() -> None:
    snapshot = load_snapshot(str(BASIC_MODEL))
    name = _most_used_interface(snapshot)
    result = impact_of(snapshot, name)

    counted = count_impact(result)

    expected = _summary_counted_per_match(format_impact_summary(name, result))
    assert {layer: figures["by_kind"] for layer, figures in counted.items()} == expected
    for layer, figures in counted.items():
        assert figures["total"] == len(result[layer]) == sum(figures["by_kind"].values())
    assert sum(figures["total"] for figures in counted.values()) > 1, "the most-used interface has dependents"


def test_the_count_respects_the_depth_and_the_direction_it_was_walked_at() -> None:
    snapshot = load_snapshot(str(BASIC_MODEL))
    name = _most_used_interface(snapshot)

    for direction in DIRECTIONS:
        for depth in (None, 1):
            result = impact_of(snapshot, name, direction=direction, depth=depth)
            assert count_impact(result) == {
                layer: {
                    "total": len(deps),
                    "by_kind": _summary_counted_per_match(format_impact_summary(name, result))[layer],
                }
                for layer, deps in result.items()
            }, (direction, depth)
    unbounded = count_impact(impact_of(snapshot, name))
    first_level = count_impact(impact_of(snapshot, name, depth=1))
    assert sum(f["total"] for f in first_level.values()) < sum(f["total"] for f in unbounded.values())


def test_the_count_of_a_match_with_no_dependents_and_of_no_match() -> None:
    assert count_impact({"interface": []}) == {"interface": {"total": 0, "by_kind": {}}}
    assert count_impact({}) == {}


def test_the_count_and_a_pages_totals_are_one_tally() -> None:
    snapshot = load_snapshot(str(BASIC_MODEL))
    name = _most_used_interface(snapshot)
    result = impact_of(snapshot, name)
    (layer,) = result

    _, page = page_impact(result, 1)

    assert dict(page.kinds) == count_impact(result)[layer]["by_kind"]
