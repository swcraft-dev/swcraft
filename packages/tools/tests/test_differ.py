"""Tests for differ.py (TDD)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from conftest import _dump, _layer_element_classes, _list_or_mapping_backed
from differ_support import make_connector, make_snapshot
from pydantic import BaseModel
from swcraft_tools.differ import (
    ChangedItem,
    CollectionChange,
    LayerDiff,
    ModelDiff,
    RenameHint,
    _diff_element,
    _identity_residue,
    _item_owners,
    _model_members,
    _render_key,
    build_key_map,
    collection_identity,
    collection_rule,
    diff_snapshots,
    format_diff,
    format_diff_json,
)
from swcraft_tools.model_registry import production_registry
from swcraft_tools.schema import (
    BaseDataType,
    Composition,
    CompositionMember,
    CompositionPort,
    Core,
    CSArgument,
    CSInterface,
    CSOperation,
    DataElement,
    DataReadRef,
    Delegation,
    EnumDataType,
    EnumLiteral,
    Memory,
    OperationCallRef,
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
    ValueDataType,
)
from swcraft_tools.snapshot import ModelSnapshot

#: ``(owner class, field name) -> CollectionRule`` over the production contract, read
#: from the kernel; the projection :func:`~swcraft_tools.differ.collection_rule` resolves per call.
COLLECTION_IDENTITY = collection_identity(production_registry())


# added / removed / unchanged / changed.


def test_item_present_only_in_new_is_added():
    old = make_snapshot(swcs=[SWComponent(name="A")])
    new = make_snapshot(swcs=[SWComponent(name="A"), SWComponent(name="B")])

    diff = diff_snapshots(old, new)

    assert [s.name for s in diff.swcs.added] == ["B"]
    assert diff.swcs.removed == []
    assert diff.swcs.changed == []


def test_item_present_only_in_old_is_removed():
    old = make_snapshot(swcs=[SWComponent(name="A"), SWComponent(name="B")])
    new = make_snapshot(swcs=[SWComponent(name="A")])

    diff = diff_snapshots(old, new)

    assert diff.swcs.added == []
    assert [s.name for s in diff.swcs.removed] == ["B"]
    assert diff.swcs.changed == []


def test_item_present_in_both_with_identical_content_is_unchanged():
    old = make_snapshot(swcs=[SWComponent(name="A", description="same")])
    new = make_snapshot(swcs=[SWComponent(name="A", description="same")])

    diff = diff_snapshots(old, new)

    assert diff.swcs.added == []
    assert diff.swcs.removed == []
    assert diff.swcs.changed == []


def test_item_present_in_both_with_differing_field_is_changed():
    old = make_snapshot(swcs=[SWComponent(name="A", description="old desc")])
    new = make_snapshot(swcs=[SWComponent(name="A", description="new desc")])

    diff = diff_snapshots(old, new)

    assert diff.swcs.added == []
    assert diff.swcs.removed == []
    assert len(diff.swcs.changed) == 1
    changed = diff.swcs.changed[0]
    assert changed.key == "A"
    assert ("description", "old desc", "new desc") in changed.changed_fields


def test_renamed_element_is_reported_as_removed_plus_added_not_merged():
    """A rename is reported as a removal plus an addition, never detected or merged."""
    old = make_snapshot(swcs=[SWComponent(name="OldName", description="same desc")])
    new = make_snapshot(swcs=[SWComponent(name="NewName", description="same desc")])

    diff = diff_snapshots(old, new)

    assert [s.name for s in diff.swcs.added] == ["NewName"]
    assert [s.name for s in diff.swcs.removed] == ["OldName"]
    assert diff.swcs.changed == []


# key_fn: Requirement diffs by .id, other layers by .name.


def test_requirement_layer_diffs_by_id_not_title():
    old = make_snapshot(
        requirements=[Requirement(id="REQ-1", title="Old title", description="D1", version=1, source="architecture")]
    )
    new = make_snapshot(
        requirements=[Requirement(id="REQ-1", title="New title", description="D1", version=1, source="architecture")]
    )

    diff = diff_snapshots(old, new)

    assert diff.requirements.added == []
    assert diff.requirements.removed == []
    assert len(diff.requirements.changed) == 1
    changed = diff.requirements.changed[0]
    assert changed.key == "REQ-1"
    assert ("title", "Old title", "New title") in changed.changed_fields


def test_requirement_with_different_id_is_added_and_removed_even_if_title_same():
    old = make_snapshot(
        requirements=[Requirement(id="REQ-1", title="Same title", description="D", version=1, source="architecture")]
    )
    new = make_snapshot(
        requirements=[Requirement(id="REQ-2", title="Same title", description="D", version=1, source="architecture")]
    )

    diff = diff_snapshots(old, new)

    assert [r.id for r in diff.requirements.added] == ["REQ-2"]
    assert [r.id for r in diff.requirements.removed] == ["REQ-1"]
    assert diff.requirements.changed == []


# ModelDiff.is_empty.


def test_model_diff_is_empty_true_for_identical_snapshots():
    old = make_snapshot(swcs=[SWComponent(name="A")])
    new = make_snapshot(swcs=[SWComponent(name="A")])

    diff = diff_snapshots(old, new)

    assert isinstance(diff, ModelDiff)
    assert diff.is_empty is True


def test_model_diff_is_empty_false_when_any_layer_changed():
    old = make_snapshot(swcs=[SWComponent(name="A")])
    new = make_snapshot(swcs=[SWComponent(name="A"), SWComponent(name="B")])

    diff = diff_snapshots(old, new)

    assert diff.is_empty is False


# The hardware layer.


def _cpu(name="CORE0", threshold=0.7, **kwargs) -> Core:
    return Core(name=name, kind="core", threshold=threshold, **kwargs)


def _mem(name="RAM0", size=1024, threshold=0.8, **kwargs) -> Memory:
    return Memory(
        name=name,
        kind="memory",
        type="RAM",
        size=size,
        accessible=["CORE0"],
        threshold=threshold,
        **kwargs,
    )


def test_resource_present_only_in_new_is_added():
    old = make_snapshot(hardware=[_cpu()])
    new = make_snapshot(hardware=[_cpu(), _mem()])

    diff = diff_snapshots(old, new)

    assert [r.name for r in diff.hardware.added] == ["RAM0"]
    assert diff.hardware.removed == []
    assert diff.hardware.changed == []
    assert diff.is_empty is False


def test_resource_present_only_in_old_is_removed():
    old = make_snapshot(hardware=[_cpu(), _mem()])
    new = make_snapshot(hardware=[_cpu()])

    diff = diff_snapshots(old, new)

    assert diff.hardware.added == []
    assert [r.name for r in diff.hardware.removed] == ["RAM0"]


def test_resource_threshold_change_is_reported_as_changed_field():
    old = make_snapshot(hardware=[_cpu(threshold=0.7)])
    new = make_snapshot(hardware=[_cpu(threshold=0.9)])

    diff = diff_snapshots(old, new)

    assert len(diff.hardware.changed) == 1
    changed = diff.hardware.changed[0]
    assert changed.key == "CORE0"
    assert ("threshold", 0.7, 0.9) in changed.changed_fields


def test_identical_hardware_is_unchanged():
    old = make_snapshot(hardware=[_cpu(), _mem()])
    new = make_snapshot(hardware=[_cpu(), _mem()])

    diff = diff_snapshots(old, new)

    assert diff.hardware.is_empty is True
    assert diff.is_empty is True


def test_duplicate_resource_name_raises_value_error():
    old = make_snapshot(hardware=[_cpu(name="CORE0"), _mem(name="CORE0")])
    new = make_snapshot(hardware=[])

    with pytest.raises(ValueError, match="duplicate key 'CORE0' in layer 'hardware'"):
        diff_snapshots(old, new)


def test_task_wcet_change_surfaces_in_the_tasks_layer_diff():
    old = make_snapshot(tasks=[Task(name="T", period=0.01, priority=10, partition="PARTITION0", wcet="400us")])
    new = make_snapshot(tasks=[Task(name="T", period=0.01, priority=10, partition="PARTITION0", wcet="250us")])

    diff = diff_snapshots(old, new)

    assert len(diff.tasks.changed) == 1
    assert ("wcet", "400us", "250us") in diff.tasks.changed[0].changed_fields


def test_swc_memory_budget_change_surfaces_in_the_swcs_layer_diff():
    old = make_snapshot(swcs=[SWComponent(name="A", memory={"RAM0": 2048})])
    new = make_snapshot(swcs=[SWComponent(name="A", memory={"RAM0": 4096})])

    diff = diff_snapshots(old, new)

    assert len(diff.swcs.changed) == 1
    assert ("memory", {"RAM0": 2048}, {"RAM0": 4096}) in diff.swcs.changed[0].changed_fields


# Aliased fields are compared and displayed under their YAML names.


def test_connector_endpoint_change_reports_aliased_field_names():
    old = make_snapshot(connectors=[make_connector("C1", "SwcA", "pOut", "SwcB", "pIn")])
    new = make_snapshot(connectors=[make_connector("C1", "SwcA", "pOut", "SwcC", "pIn")])

    diff = diff_snapshots(old, new)

    assert diff.connectors.added == []
    assert diff.connectors.removed == []
    assert len(diff.connectors.changed) == 1
    changed = diff.connectors.changed[0]
    field_names = [f[0] for f in changed.changed_fields]
    assert "to" in field_names
    assert "from_" not in field_names
    assert "from" not in field_names  # unchanged in this case, so absent entirely


def test_connector_from_endpoint_change_reports_aliased_from_name():
    old = make_snapshot(connectors=[make_connector("C1", "SwcA", "pOut", "SwcB", "pIn")])
    new = make_snapshot(connectors=[make_connector("C1", "SwcZ", "pOut", "SwcB", "pIn")])

    diff = diff_snapshots(old, new)

    assert len(diff.connectors.changed) == 1
    changed = diff.connectors.changed[0]
    field_names = [f[0] for f in changed.changed_fields]
    assert "from" in field_names
    assert "from_" not in field_names


def test_interface_kind_change_same_name_does_not_crash_and_reports_all_fields():
    """A union element changing `kind` under one key reports fields present on one side only, without a KeyError."""
    old = make_snapshot(
        interfaces=[
            SRInterface(
                name="ISame",
                kind="sender_receiver",
                data_elements=[DataElement(name="speed", implementation_type="float32")],
            )
        ]
    )
    new = make_snapshot(
        interfaces=[
            CSInterface(
                name="ISame",
                kind="client_server",
                operations=[CSOperation(name="GetSpeed")],
            )
        ]
    )

    diff = diff_snapshots(old, new)

    assert len(diff.interfaces.changed) == 1
    changed = diff.interfaces.changed[0]
    field_names = {f[0] for f in changed.changed_fields}
    assert "kind" in field_names
    assert "data_elements" in field_names  # only present on the old side
    assert "operations" in field_names  # only present on the new side


# A duplicate identity key is a hard error.


def test_duplicate_identity_key_in_one_layer_raises_value_error():
    old = make_snapshot(
        tasks=[
            Task(name="Task_10ms", period=0.01, priority=1, partition="PARTITION0"),
            Task(name="Task_10ms", period=0.01, priority=2, partition="PARTITION0"),
        ]
    )
    new = make_snapshot(tasks=[Task(name="Task_10ms", period=0.01, priority=1, partition="PARTITION0")])

    with pytest.raises(ValueError, match="Task_10ms"):
        diff_snapshots(old, new)


def test_duplicate_identity_key_in_new_layer_raises_value_error():
    """The duplicate-key guard holds on the new side too."""
    old = make_snapshot(tasks=[Task(name="Task_10ms", period=0.01, priority=1, partition="PARTITION0")])
    new = make_snapshot(
        tasks=[
            Task(name="Task_10ms", period=0.01, priority=1, partition="PARTITION0"),
            Task(name="Task_10ms", period=0.01, priority=2, partition="PARTITION0"),
        ]
    )

    with pytest.raises(ValueError, match="Task_10ms"):
        diff_snapshots(old, new)


def test_changed_item_dataclass_fields():
    item = ChangedItem(key="A", changed_fields=[("x", 1, 2)])
    assert item.key == "A"
    assert item.changed_fields == [("x", 1, 2)]


# Per-item collection diff.


def _port(name, direction="require", interface="ISpeed", **kwargs) -> Port:
    return Port(name=name, direction=direction, interface=interface, **kwargs)


def _changed_item(layer_diff, key) -> ChangedItem:
    matching = [changed for changed in layer_diff.changed if changed.key == key]
    assert len(matching) == 1, f"expected exactly one changed element '{key}', got {layer_diff.changed}"
    return matching[0]


def _collection(layer_diff, key, field_name) -> CollectionChange:
    changed = _changed_item(layer_diff, key)
    matching = [entry for entry in changed.collection_changes if entry.field == field_name]
    assert len(matching) == 1, f"expected one '{field_name}' collection change, got {changed.collection_changes}"
    return matching[0]


def _swcs(old_ports, new_ports) -> LayerDiff:
    old = make_snapshot(swcs=[SWComponent(name="A", ports=list(old_ports))])
    new = make_snapshot(swcs=[SWComponent(name="A", ports=list(new_ports))])
    return diff_snapshots(old, new).swcs


P_SPEED = _port("rSpeed")
P_TORQUE = _port("pTorqueLimit", direction="provide", interface="ITorque")
P_ENGINE = _port("rEngineSpeed")


def test_a_port_added_is_reported_as_one_added_item_not_as_the_whole_list():
    """The headline defect: adding a port must name the port."""
    change = _collection(_swcs([P_SPEED], [P_SPEED, P_TORQUE]), "A", "ports")

    assert change.added == [_dump(P_TORQUE)]
    assert change.removed == []
    assert change.changed == []
    assert change.reordered is False


def test_a_port_removed_is_reported_as_one_removed_item():
    change = _collection(_swcs([P_SPEED, P_TORQUE], [P_SPEED]), "A", "ports")

    assert change.added == []
    assert change.removed == [_dump(P_TORQUE)]
    assert change.changed == []


def test_a_port_that_kept_its_name_is_reported_as_changed_with_only_the_differing_field():
    """A port re-pointed at another interface is one changed item recording the interface alone."""
    change = _collection(_swcs([P_SPEED], [_port("rSpeed", interface="IVehicleSpeed")]), "A", "ports")

    assert change.added == []
    assert change.removed == []
    assert change.changed == [
        ChangedItem(
            key="rSpeed",
            changed_fields=[("interface", "ISpeed", "IVehicleSpeed")],
            path=({"field": "ports"}, {"member": "rSpeed"}),
        )
    ]


def test_an_addition_a_removal_and_a_change_in_one_collection_are_reported_side_by_side():
    change = _collection(
        _swcs(
            [P_SPEED, P_ENGINE],
            [_port("rSpeed", interface="IVehicleSpeed"), P_TORQUE],
        ),
        "A",
        "ports",
    )

    assert change.added == [_dump(P_TORQUE)]
    assert change.removed == [_dump(P_ENGINE)]
    assert [item.key for item in change.changed] == ["rSpeed"]


def test_the_collection_change_names_the_field_it_details():
    assert _collection(_swcs([P_SPEED], [P_SPEED, P_TORQUE]), "A", "ports").field == "ports"


def test_the_whole_value_tuple_stays_in_changed_fields_beside_the_per_item_detail():
    """The per-item detail is additive: `changed_fields` still names `ports`, which the authorization gate reads."""
    changed = _changed_item(_swcs([P_SPEED], [P_SPEED, P_TORQUE]), "A")

    assert [name for name, _, _ in changed.changed_fields] == ["ports"]
    assert changed.changed_fields[0][1] == [_dump(P_SPEED)]
    assert changed.changed_fields[0][2] == [_dump(P_SPEED), _dump(P_TORQUE)]


def test_an_unchanged_collection_produces_no_collection_change_entry_at_all():
    old = make_snapshot(swcs=[SWComponent(name="A", description="old", ports=[P_SPEED])])
    new = make_snapshot(swcs=[SWComponent(name="A", description="new", ports=[P_SPEED])])

    changed = _changed_item(diff_snapshots(old, new).swcs, "A")

    assert [name for name, _, _ in changed.changed_fields] == ["description"]
    assert changed.collection_changes == []


def test_a_scalar_field_never_produces_a_collection_change():
    old = make_snapshot(swcs=[SWComponent(name="A", type="application")])
    new = make_snapshot(swcs=[SWComponent(name="A", type="service")])

    assert _changed_item(diff_snapshots(old, new).swcs, "A").collection_changes == []


def test_items_are_matched_by_identity_and_not_by_position():
    """Inserting at the front must not report every later port as changed."""
    change = _collection(_swcs([P_SPEED], [P_TORQUE, P_SPEED]), "A", "ports")

    assert change.added == [_dump(P_TORQUE)]
    assert change.removed == []
    assert change.changed == []
    assert change.reordered is False


def test_a_reordered_collection_reports_a_reorder_and_nothing_else():
    """A reordered list is a changed field, so the reorder is carried by a `CollectionChange`."""
    layer_diff = _swcs([P_SPEED, P_TORQUE], [P_TORQUE, P_SPEED])

    assert [name for name, _, _ in _changed_item(layer_diff, "A").changed_fields] == ["ports"]
    change = _collection(layer_diff, "A", "ports")
    assert change.added == []
    assert change.removed == []
    assert change.changed == []
    assert change.reordered is True


def test_a_reorder_beside_an_addition_reports_both():
    change = _collection(_swcs([P_SPEED, P_ENGINE], [P_ENGINE, P_TORQUE, P_SPEED]), "A", "ports")

    assert change.added == [_dump(P_TORQUE)]
    assert change.reordered is True


def test_a_field_absent_from_one_side_reports_its_items_rather_than_the_whole_field():
    """A collection present on the old side only after a `kind` change is all removals."""
    old = make_snapshot(
        interfaces=[
            SRInterface(
                name="ISame",
                kind="sender_receiver",
                data_elements=[DataElement(name="speed", implementation_type="float32")],
            )
        ]
    )
    new = make_snapshot(
        interfaces=[CSInterface(name="ISame", kind="client_server", operations=[CSOperation(name="Get")])]
    )

    layer_diff = diff_snapshots(old, new).interfaces

    removed_side = _collection(layer_diff, "ISame", "data_elements")
    assert removed_side.removed == [_dump(DataElement(name="speed", implementation_type="float32"))]
    assert removed_side.added == []
    added_side = _collection(layer_diff, "ISame", "operations")
    assert added_side.added == [_dump(CSOperation(name="Get"))]


def test_a_second_collection_on_the_same_element_gets_its_own_entry():
    old = make_snapshot(swcs=[SWComponent(name="A", ports=[P_SPEED], runnables=[Runnable(name="Run")])])
    new = make_snapshot(swcs=[SWComponent(name="A", ports=[P_SPEED, P_TORQUE], runnables=[])])

    changed = _changed_item(diff_snapshots(old, new).swcs, "A")

    assert [entry.field for entry in changed.collection_changes] == ["ports", "runnables"]


def test_a_literals_collection_is_diffed_by_name_like_every_other_name_keyed_one():
    idle = EnumLiteral(name="Idle", value=0)
    old = make_snapshot(implementation_types=[EnumDataType(name="EMode", kind="enum", literals=[idle])])
    new = make_snapshot(
        implementation_types=[
            EnumDataType(name="EMode", kind="enum", literals=[idle, EnumLiteral(name="Run", value=1)])
        ]
    )

    change = _collection(diff_snapshots(old, new).implementation_types, "EMode", "literals")

    assert change.added == [_dump(EnumLiteral(name="Run", value=1))]


# The two composite-keyed collections


def _task(*refs) -> Task:
    return Task(name="Task_10ms", period=0.01, priority=5, partition="PARTITION0", runnables=list(refs))


REF_SPEED = TaskRunnableRef(swc="SpeedCtrlSWC", runnable="SpeedCtrl_Run")
REF_TORQUE = TaskRunnableRef(swc="SpeedCtrlSWC", runnable="TorqueCalc_Run")


def _task_runnables_change(old_refs, new_refs) -> CollectionChange:
    old = make_snapshot(tasks=[_task(*old_refs)])
    new = make_snapshot(tasks=[_task(*new_refs)])
    return _collection(diff_snapshots(old, new).tasks, "Task_10ms", "runnables")


def test_a_task_runnable_mapping_is_diffed_by_the_swc_runnable_pair():
    old = make_snapshot(tasks=[_task(REF_SPEED)])
    new = make_snapshot(tasks=[_task(REF_SPEED, REF_TORQUE)])

    change = _collection(diff_snapshots(old, new).tasks, "Task_10ms", "runnables")

    assert change.added == [_dump(REF_TORQUE)]
    assert change.changed == []


def test_a_mapping_that_moved_to_another_swc_is_a_removal_plus_an_addition():
    """Changing either half of the pair is a different item, never one that changed."""
    moved = TaskRunnableRef(swc="Dashboard", runnable="SpeedCtrl_Run")
    old = make_snapshot(tasks=[_task(REF_SPEED)])
    new = make_snapshot(tasks=[_task(moved)])

    change = _collection(diff_snapshots(old, new).tasks, "Task_10ms", "runnables")

    assert change.added == [_dump(moved)]
    assert change.removed == [_dump(REF_SPEED)]
    assert change.changed == []


def test_a_rewrite_from_swc_to_instance_is_a_remove_and_an_add():
    """The composite is syntactic: the same occurrence named by the other selector is another member."""
    old = make_snapshot(sequences=[Sequence(name="cycle", task="T", steps=[SequenceStep(swc="A", runnable="R")])])
    new = make_snapshot(sequences=[Sequence(name="cycle", task="T", steps=[SequenceStep(instance="A", runnable="R")])])

    change = _collection(diff_snapshots(old, new).sequences, "cycle", "steps")

    assert [change.rule.key(item) for item in change.removed] == [("A", None, "R")]
    assert [change.rule.key(item) for item in change.added] == [(None, "A", "R")]
    assert change.changed == []


def test_a_composite_keyed_item_that_changed_carries_the_pair_as_its_key():
    old = make_snapshot(sequences=[Sequence(name="cycle", task="T", steps=[SequenceStep(swc="A", runnable="R")])])
    new = make_snapshot(
        sequences=[Sequence(name="cycle", task="T", steps=[SequenceStep(swc="A", runnable="R", note="then this")])]
    )

    change = _collection(diff_snapshots(old, new).sequences, "cycle", "steps")

    assert change.changed == [
        ChangedItem(
            key=("A", None, "R"),
            changed_fields=[("note", "", "then this")],
            # The comparison key is the triple as a tuple; the address step is the
            # same triple as an object, the absent selector `None`.
            path=({"field": "steps"}, {"member": {"swc": "A", "instance": None, "runnable": "R"}}),
        )
    ]


def test_reordering_sequence_steps_is_reported_as_a_reorder():
    """A reorder of `Sequence.steps`, whose order is semantic, is reported."""
    steps = [SequenceStep(swc="A", runnable="R1"), SequenceStep(swc="A", runnable="R2")]
    old = make_snapshot(sequences=[Sequence(name="cycle", task="T", steps=steps)])
    new = make_snapshot(sequences=[Sequence(name="cycle", task="T", steps=list(reversed(steps)))])

    change = _collection(diff_snapshots(old, new).sequences, "cycle", "steps")

    assert (change.added, change.removed, change.changed) == ([], [], [])
    assert change.reordered is True


# The invariant the renderers' skip rule rests on


def _every_collection_change(diff: ModelDiff) -> list[CollectionChange]:
    """Every collection change in the diff, in every layer and at every depth."""

    def walk(item: ChangedItem) -> list[CollectionChange]:
        found = []
        for entry in item.collection_changes:
            found.append(entry)
            for nested in entry.changed:
                found.extend(walk(nested))
        return found

    return [
        entry
        for layer in dataclasses.fields(ModelDiff)
        for changed in getattr(diff, layer.name).changed
        for entry in walk(changed)
    ]


@pytest.mark.parametrize(
    ("old_ports", "new_ports"),
    [
        pytest.param([P_SPEED], [P_SPEED, P_TORQUE], id="added"),
        pytest.param([P_SPEED, P_TORQUE], [P_SPEED], id="removed"),
        pytest.param([P_SPEED], [_port("rSpeed", interface="IOther")], id="changed"),
        pytest.param([P_SPEED, P_TORQUE], [P_TORQUE, P_SPEED], id="reordered"),
        pytest.param([P_SPEED], [], id="emptied"),
        pytest.param([], [P_SPEED], id="filled"),
        pytest.param([P_SPEED, P_TORQUE], [P_TORQUE, P_ENGINE], id="everything-at-once"),
    ],
)
def test_an_empty_non_reordered_collection_change_is_never_constructed(old_ports, new_ports):
    """A `CollectionChange` always carries a fact, so the renderers' skip of `changed_fields` drops nothing."""
    for change in _every_collection_change(
        diff_snapshots(
            make_snapshot(swcs=[SWComponent(name="A", ports=list(old_ports))]),
            make_snapshot(swcs=[SWComponent(name="A", ports=list(new_ports))]),
        )
    ):
        assert change.added or change.removed or change.changed or change.reordered


def test_a_collection_field_that_reduces_to_empty_on_both_sides_is_still_reported_whole():
    """Empty on one side and absent on the other yields no `CollectionChange`, only the whole-value tuple.

    `schema.py` refuses such dumps on a validated model; `test_differ_coverage.py`
    pins that dependency.
    """
    old_dump = {"name": "IThing", "kind": "sender_receiver", "data_elements": []}
    new_dump = {"name": "IThing", "kind": "client_server", "operations": []}

    changed_fields, collection_changes = _diff_element(old_dump, new_dump, (SRInterface, CSInterface))

    assert collection_changes == []
    assert [name for name, _, _ in changed_fields] == ["kind", "data_elements", "operations"]


def test_every_changed_collection_field_of_a_supported_shape_yields_a_collection_change():
    """Every changed collection field of one element is named by a `CollectionChange`."""
    old = make_snapshot(
        swcs=[SWComponent(name="A", type="application", ports=[P_SPEED, P_TORQUE], runnables=[Runnable(name="R")])]
    )
    new = make_snapshot(swcs=[SWComponent(name="A", type="service", ports=[P_TORQUE, P_SPEED], runnables=[])])

    changed = _changed_item(diff_snapshots(old, new).swcs, "A")

    detailed = {entry.field for entry in changed.collection_changes}
    assert detailed == {"ports", "runnables"}
    assert {name for name, _, _ in changed.changed_fields} == {"type", "ports", "runnables"}


# The declared shapes


def test_collection_change_defaults_to_an_empty_non_reordered_change():
    change = CollectionChange(field="ports", rule=collection_rule(SWComponent, "ports"))
    assert (change.added, change.removed, change.changed, change.rename_hints) == ([], [], [], [])
    assert change.reordered is False


def test_a_collection_change_carries_the_rule_that_produced_it():
    """A change carries its rule: `field` alone is ambiguous (`runnables` of an SWC and of a task)."""
    ports = _collection(_swcs([P_SPEED], [P_SPEED, P_TORQUE]), "A", "ports")
    task_runnables = _task_runnables_change([REF_SPEED], [REF_SPEED, REF_TORQUE])

    assert ports.rule is collection_rule(SWComponent, "ports")
    assert task_runnables.rule is collection_rule(Task, "runnables")
    assert ports.rule.key(ports.added[0]) == "pTorqueLimit"


def test_changed_item_carries_collection_changes():
    item = ChangedItem(key="A")
    assert item.collection_changes == []


def test_layer_diff_carries_rename_hints():
    """A `LayerDiff` from a real diff has the field, empty."""
    assert LayerDiff().rename_hints == []
    assert diff_snapshots(make_snapshot(), make_snapshot(swcs=[SWComponent(name="A")])).swcs.rename_hints == []


def test_rename_hint_names_the_two_keys_it_pairs():
    hint = RenameHint(removed_key="rSpeed", added_key="rVehicleSpeed")
    assert (hint.removed_key, hint.added_key) == ("rSpeed", "rVehicleSpeed")


# Recursion into nested collections.


def _nested(layer_diff, key, field_name, item_key, nested_field) -> CollectionChange:
    """The `nested_field` collection change of one item of `field_name`, by lookup."""
    outer = _collection(layer_diff, key, field_name)
    matching = [item for item in outer.changed if item.key == item_key]
    assert len(matching) == 1, f"expected one changed '{item_key}' in '{field_name}', got {outer.changed}"
    nested = [entry for entry in matching[0].collection_changes if entry.field == nested_field]
    assert len(nested) == 1, f"expected one '{nested_field}' change on '{item_key}', got {matching[0]}"
    return nested[0]


def _swcs_with_runnables(old_runnables, new_runnables) -> LayerDiff:
    old = make_snapshot(swcs=[SWComponent(name="A", runnables=list(old_runnables))])
    new = make_snapshot(swcs=[SWComponent(name="A", runnables=list(new_runnables))])
    return diff_snapshots(old, new).swcs


def _interfaces_with_operations(old_operations, new_operations) -> LayerDiff:
    old = make_snapshot(interfaces=[CSInterface(name="ICtrl", kind="client_server", operations=list(old_operations))])
    new = make_snapshot(interfaces=[CSInterface(name="ICtrl", kind="client_server", operations=list(new_operations))])
    return diff_snapshots(old, new).interfaces


def _timing(period: float) -> dict:
    """A timing event as the differ dumps it, every field included (`name` is `None` while derived)."""
    return {"type": "timing", "period": period, "disabled_modes": [], "name": None}


#: An init event as the differ dumps it, its `name` included.
INIT = {"type": "init", "name": None}

ARG_SPEED = CSArgument(name="speed", direction="in", implementation_type="uint16")
ARG_STATUS = CSArgument(name="status", direction="out", implementation_type="uint8")


# Runnables -> events, the first of the two nested paths


def test_a_runnable_whose_events_changed_reports_a_nested_collection_change():
    layer = _swcs_with_runnables(
        [Runnable(name="R", events=[INIT])],
        [Runnable(name="R", events=[INIT, _timing(0.01)])],
    )

    nested = _nested(layer, "A", "runnables", "R", "events")

    assert nested.added == [_timing(0.01)]
    assert (nested.removed, nested.changed, nested.reordered) == ([], [], False)


def test_the_nested_item_keeps_its_own_whole_value_tuple_beside_the_detail():
    """The additive contract holds at every depth: the nested item records `events` in `changed_fields`."""
    layer = _swcs_with_runnables(
        [Runnable(name="R", events=[INIT])],
        [Runnable(name="R", events=[INIT, _timing(0.01)])],
    )

    runnable = [item for item in _collection(layer, "A", "runnables").changed if item.key == "R"][0]

    assert [name for name, _, _ in runnable.changed_fields] == ["events"]
    assert runnable.changed_fields[0][1:] == ([INIT], [INIT, _timing(0.01)])


def test_a_scalar_change_beside_a_nested_collection_change_is_reported_once_each():
    layer = _swcs_with_runnables(
        [Runnable(name="R", min_start_interval=0.0, events=[INIT])],
        [Runnable(name="R", min_start_interval=0.5, events=[])],
    )

    runnable = [item for item in _collection(layer, "A", "runnables").changed if item.key == "R"][0]

    assert {name for name, _, _ in runnable.changed_fields} == {"min_start_interval", "events"}
    assert [entry.field for entry in runnable.collection_changes] == ["events"]


def test_an_event_whose_period_changed_is_a_removal_plus_an_addition_never_a_change():
    """An event's value is its key, so a different period is a different event."""
    layer = _swcs_with_runnables(
        [Runnable(name="R", events=[_timing(0.01)])],
        [Runnable(name="R", events=[_timing(0.02)])],
    )

    nested = _nested(layer, "A", "runnables", "R", "events")

    assert (nested.added, nested.removed) == ([_timing(0.02)], [_timing(0.01)])
    assert nested.changed == []


def test_a_runnable_added_whole_is_not_reported_as_a_nested_change():
    layer = _swcs_with_runnables([], [Runnable(name="R", events=[INIT])])

    change = _collection(layer, "A", "runnables")

    assert change.added == [Runnable(name="R", events=[INIT]).model_dump(by_alias=True)]
    assert change.changed == []


def test_a_runnable_renamed_and_re_evented_reports_both_facts_without_conflating_them():
    """A rename is a removal plus an addition, and the event change rides on those two entries only."""
    layer = _swcs_with_runnables(
        [Runnable(name="Rold", events=[INIT])],
        [Runnable(name="Rnew", events=[_timing(0.01)])],
    )

    change = _collection(layer, "A", "runnables")

    assert change.changed == []
    assert [(item["name"], item["events"]) for item in change.removed] == [("Rold", [INIT])]
    assert [(item["name"], item["events"]) for item in change.added] == [("Rnew", [_timing(0.01)])]


# Operations -> arguments, the second and last nested path


def test_an_operation_that_gained_an_argument_reports_a_nested_collection_change():
    layer = _interfaces_with_operations(
        [CSOperation(name="Compute", arguments=[ARG_SPEED])],
        [CSOperation(name="Compute", arguments=[ARG_SPEED, ARG_STATUS])],
    )

    nested = _nested(layer, "ICtrl", "operations", "Compute", "arguments")

    assert nested.added == [_dump(ARG_STATUS)]
    assert (nested.removed, nested.changed, nested.reordered) == ([], [], False)


def test_an_argument_that_kept_its_name_reports_only_the_field_that_differs():
    layer = _interfaces_with_operations(
        [CSOperation(name="Compute", arguments=[ARG_SPEED])],
        [
            CSOperation(
                name="Compute", arguments=[CSArgument(name="speed", direction="in", implementation_type="uint32")]
            )
        ],
    )

    nested = _nested(layer, "ICtrl", "operations", "Compute", "arguments")

    assert [(item.key, item.changed_fields) for item in nested.changed] == [
        ("speed", [("implementation_type", "uint16", "uint32")]),
    ]
    assert (nested.added, nested.removed) == ([], [])


def test_reordering_an_operations_arguments_is_reported_as_a_nested_reorder():
    layer = _interfaces_with_operations(
        [CSOperation(name="Compute", arguments=[ARG_SPEED, ARG_STATUS])],
        [CSOperation(name="Compute", arguments=[ARG_STATUS, ARG_SPEED])],
    )

    nested = _nested(layer, "ICtrl", "operations", "Compute", "arguments")

    assert (nested.added, nested.removed, nested.changed) == ([], [], [])
    assert nested.reordered is True


def test_two_operations_changed_at_once_each_carry_their_own_nested_detail():
    layer = _interfaces_with_operations(
        [
            CSOperation(name="Compute", arguments=[ARG_SPEED]),
            CSOperation(name="Reset", arguments=[]),
        ],
        [
            CSOperation(name="Compute", arguments=[]),
            CSOperation(name="Reset", arguments=[ARG_STATUS]),
        ],
    )

    assert _nested(layer, "ICtrl", "operations", "Compute", "arguments").removed == [_dump(ARG_SPEED)]
    assert _nested(layer, "ICtrl", "operations", "Reset", "arguments").added == [_dump(ARG_STATUS)]


# The completeness guard: the model bottoms out at two levels


def _collection_paths(
    model: type[BaseModel], prefix: tuple[str, ...] = (), visiting: frozenset = frozenset()
) -> set[tuple[str, ...]]:
    """Every chain of nested collection fields reachable from *model*, from ``schema.py``'s annotations.

    The classifier is the one `test_differ_coverage.py` uses, so the two cannot
    disagree. `visiting` guards a self-referencing model.
    """
    if model in visiting:
        return set()
    visiting = visiting | {model}
    paths: set[tuple[str, ...]] = set()
    for field_name, field_info in model.model_fields.items():
        members = _model_members(field_info.annotation)
        if _list_or_mapping_backed(field_info.annotation):
            path = (*prefix, field_name)
            paths.add(path)
        else:
            path = prefix
        for member in members:
            paths |= _collection_paths(member, path, visiting)
    return paths


def _schema_collection_paths() -> set[tuple[str, ...]]:
    """Every nested-collection chain reachable from the layers' element classes."""
    roots = _layer_element_classes()
    assert len(roots) > 10, f"the walk found no layer element classes: {roots}"
    return {path for root in roots for path in _collection_paths(root)}


def test_the_model_bottoms_out_at_two_levels_of_collection():
    """The nested paths are named, not counted, so a new level fails here with its path.

    The one third-level path, `disabled_modes`, belongs to a `whole_value`
    member, which is compared whole: the differ does not recurse into it.
    """
    paths = _schema_collection_paths()

    assert max(len(path) for path in paths) == 3, f"nesting deeper than three levels: {sorted(paths)}"
    assert {path for path in paths if len(path) == 3} == {("runnables", "events", "disabled_modes")}
    assert collection_rule(Runnable, "events").strategy == "whole_value"
    assert {path for path in paths if len(path) == 2} == {
        ("runnables", "events"),
        ("operations", "arguments"),
        ("runnables", "reads"),
        ("runnables", "writes"),
        ("runnables", "calls"),
        ("ports", "measures"),
        ("ports", "com_specs"),
        ("ports", "satisfies"),
        ("runnables", "satisfies"),
        ("runnables", "reads_variables"),
        ("runnables", "writes_variables"),
        ("runnables", "can_enter"),
        ("runnables", "runs_inside"),
    }


def test_every_two_level_path_has_an_identity_rule():
    """Every nested path is claimed by the vocabulary.

    Table against table: it never runs the differ (`test_differ_coverage.py` does).
    """
    for outer, inner in {path for path in _schema_collection_paths() if len(path) == 2}:
        owners = {owner for (owner, field_name) in COLLECTION_IDENTITY if field_name == outer}
        item_types = {item for owner in owners for item in _item_owners(owner, outer)}
        assert any((item, inner) in COLLECTION_IDENTITY for item in item_types), f"{outer} -> {inner}"


# The multiset semantics for `events`.


def _runnable_events(old_events, new_events) -> LayerDiff:
    return _swcs_with_runnables(
        [Runnable(name="R", events=list(old_events))],
        [Runnable(name="R", events=list(new_events))],
    )


def _events_change(old_events, new_events) -> CollectionChange:
    """The `events` collection change of the one runnable `R` on the one SWC `A`."""
    return _nested(_runnable_events(old_events, new_events), "A", "runnables", "R", "events")


@pytest.mark.parametrize(
    ("old_events", "new_events"),
    [
        pytest.param([_timing(0.01)], [_timing(0.02)], id="alone"),
        pytest.param([_timing(0.01), _timing(0.01)], [_timing(0.02), _timing(0.01)], id="beside-a-duplicate"),
    ],
)
def test_an_events_period_change_is_a_removal_plus_an_addition_whatever_sits_beside_it(old_events, new_events):
    """A different period is a different event, whether or not a duplicate sits beside it.

    Keying by `type` with a value fallback would answer differently with a
    duplicate present; the count-based rule does not.
    """
    change = _events_change(old_events, new_events)

    assert (change.removed, change.added) == ([_timing(0.01)], [_timing(0.02)])
    assert change.changed == []
    assert change.reordered is False


def test_reordering_two_events_with_different_keys_is_a_reorder_and_nothing_else():
    """A swap adds and removes nothing but is reported: it is observable in the ARXML event names."""
    change = _events_change([_timing(0.01), _timing(0.02)], [_timing(0.02), _timing(0.01)])

    assert (change.added, change.removed, change.changed) == ([], [], [])
    assert change.reordered is True


def test_swapping_two_identical_events_is_not_reported_at_all():
    """Swapping two equal events changes nothing: the dumps are equal."""
    events = [_timing(0.01), _timing(0.01)]

    layer = _runnable_events(events, list(reversed(events)))

    assert layer.is_empty


def test_losing_one_of_two_identical_events_reports_exactly_one_removal():
    """The count is compared, not presence: losing one of two copies is a single removal."""
    change = _events_change([_timing(0.01), _timing(0.01)], [_timing(0.01)])

    assert change.removed == [_timing(0.01)]
    assert (change.added, change.changed, change.reordered) == ([], [], False)


def test_losing_a_duplicate_event_from_around_another_one_is_a_removal_and_not_also_a_reorder():
    """A removal a different copy explains without moving anything is not a reorder."""
    change = _events_change([_timing(0.01), INIT, _timing(0.01)], [INIT, _timing(0.01)])

    assert change.removed == [_timing(0.01)]
    assert (change.added, change.changed, change.reordered) == ([], [], False)


def test_a_genuine_swap_beside_a_duplicate_event_is_still_a_reorder():
    """Non-vacuity: a duplicate does not silence a real reorder."""
    change = _events_change([_timing(0.01), INIT, _timing(0.01)], [_timing(0.01), _timing(0.01), INIT])

    assert (change.added, change.removed, change.changed) == ([], [], [])
    assert change.reordered is True


def test_adding_a_second_identical_event_reports_exactly_one_addition():
    """Gaining a second identical event is one addition: a runnable may carry two."""
    change = _events_change([_timing(0.01)], [_timing(0.01), _timing(0.01)])

    assert change.added == [_timing(0.01)]
    assert (change.removed, change.changed, change.reordered) == ([], [], False)


def test_a_duplicate_inside_events_is_legal_where_a_duplicate_layer_key_is_not():
    """Duplicates are legal inside a collection, so the differ counts them instead of refusing.

    `build_key_map`'s duplicate rule covers the top-level layers only.
    """
    with pytest.raises(ValueError, match="duplicate key"):
        build_key_map([SWComponent(name="A"), SWComponent(name="A")], lambda item: item.name, "swcs")

    change = _events_change([INIT], [INIT, _timing(0.01), _timing(0.01)])

    assert change.added == [_timing(0.01), _timing(0.01)]
    assert (change.removed, change.changed, change.reordered) == ([], [], False)


# Mapping and scalar-list collections.


def _memory_layer(old_memory, new_memory) -> LayerDiff:
    old = make_snapshot(swcs=[SWComponent(name="A", memory=dict(old_memory))])
    new = make_snapshot(swcs=[SWComponent(name="A", memory=dict(new_memory))])
    return diff_snapshots(old, new).swcs


def _memory_change(old_memory, new_memory) -> CollectionChange:
    return _collection(_memory_layer(old_memory, new_memory), "A", "memory")


def _linked(links) -> SWComponent:
    """A component whose own ``satisfies`` holds *links* (contract ``11.0.0``: the link lives in the element)."""
    return SWComponent(name="Ctl", satisfies=list(links))


def _links_layer(old_links, new_links) -> LayerDiff:
    old = make_snapshot(swcs=[_linked(old_links)])
    new = make_snapshot(swcs=[_linked(new_links)])
    return diff_snapshots(old, new).swcs


def _links_change(old_links, new_links) -> CollectionChange:
    return _collection(_links_layer(old_links, new_links), "Ctl", "satisfies")


def test_a_memory_pool_added_is_reported_by_name():
    """The mapping's items are its keys, so an added pool is the pool name."""
    change = _memory_change({"bss": 1024}, {"bss": 1024, "heap": 4096})

    assert change.added == ["heap"]
    assert (change.removed, change.changed, change.reordered) == ([], [], False)


def test_a_memory_pool_removed_is_reported_by_name():
    change = _memory_change({"bss": 1024, "heap": 4096}, {"bss": 1024})

    assert change.removed == ["heap"]
    assert (change.added, change.changed, change.reordered) == ([], [], False)


def test_a_changed_memory_budget_is_one_changed_pool_carrying_its_old_and_new_value():
    """A pool whose budget changed is one changed item keyed by the pool name."""
    change = _memory_change({"bss": 1024}, {"bss": 2048})

    assert (change.added, change.removed) == ([], [])
    assert [item.key for item in change.changed] == ["bss"]
    assert change.changed[0].changed_fields == [("value", 1024, 2048)]


def test_a_memory_change_keeps_its_whole_value_tuple_beside_the_detail():
    """The additive contract holds for a mapping: `changed_fields` still names `memory`."""
    changed = _changed_item(_memory_layer({"bss": 1024}, {"bss": 2048}), "A")

    assert changed.changed_fields == [("memory", {"bss": 1024}, {"bss": 2048})]


def test_a_pool_added_a_pool_removed_and_a_budget_changed_are_reported_side_by_side():
    change = _memory_change({"bss": 1024, "stack": 512}, {"bss": 2048, "heap": 4096})

    assert change.added == ["heap"]
    assert change.removed == ["stack"]
    assert [item.key for item in change.changed] == ["bss"]


def test_reordering_memory_alone_produces_no_diff_at_all():
    """A reordered mapping dumps equal, so it never reaches the diff."""
    layer_diff = _memory_layer({"bss": 1024, "heap": 4096}, {"heap": 4096, "bss": 1024})

    assert layer_diff.is_empty


def test_an_absent_memory_and_an_empty_memory_are_not_reported_as_differing():
    old = make_snapshot(swcs=[SWComponent(name="A")])
    new = make_snapshot(swcs=[SWComponent(name="A", memory={})])

    assert diff_snapshots(old, new).swcs.is_empty


def test_a_link_added_is_reported_as_one_added_id():
    """`satisfies` is a `list[str]`: the string is the item and its own key."""
    change = _links_change(["REQ-2@1"], ["REQ-2@1", "REQ-3@1"])

    assert change.added == ["REQ-3@1"]
    assert (change.removed, change.changed, change.reordered) == ([], [], False)


def test_a_link_removed_is_reported_as_one_removed_id():
    change = _links_change(["REQ-2@1", "REQ-3@1"], ["REQ-2@1"])

    assert change.removed == ["REQ-3@1"]
    assert (change.added, change.changed, change.reordered) == ([], [], False)


def test_a_replaced_link_is_a_removal_plus_an_addition_never_a_changed_item():
    """The string is the whole item, so two links can never pair and be `changed`."""
    change = _links_change(["REQ-2@1"], ["REQ-3@1"])

    assert (change.added, change.removed) == (["REQ-3@1"], ["REQ-2@1"])
    assert change.changed == []


def test_a_confirmation_edit_is_one_link_removed_and_one_added():
    """A link confirmed for a newer version is a removal and an addition, never an update."""
    change = _links_change(["REQ-2@1"], ["REQ-2@2"])

    assert (change.added, change.removed, change.changed) == (["REQ-2@2"], ["REQ-2@1"], [])


def test_reordering_links_alone_reports_a_reorder_and_never_the_two_whole_lists():
    """A reordered `satisfies`, list-backed though its order means nothing, is carried by a `CollectionChange`."""
    layer_diff = _links_layer(["REQ-2@1", "REQ-3@1"], ["REQ-3@1", "REQ-2@1"])

    assert [name for name, _, _ in _changed_item(layer_diff, "Ctl").changed_fields] == ["satisfies"]
    change = _collection(layer_diff, "Ctl", "satisfies")
    assert (change.added, change.removed, change.changed) == ([], [], [])
    assert change.reordered is True


def test_a_reordered_links_list_is_detailed_so_changed_fields_can_be_skipped():
    """Every changed mapping or scalar-list field is named by a `CollectionChange`."""
    for layer_diff, key in (
        (_links_layer(["REQ-2@1", "REQ-3@1"], ["REQ-3@1", "REQ-2@1"]), "Ctl"),
        (_memory_layer({"bss": 1024}, {"bss": 2048}), "A"),
    ):
        changed = _changed_item(layer_diff, key)
        detailed = {entry.field for entry in changed.collection_changes}
        assert {name for name, _, _ in changed.changed_fields} == detailed


# Rename hints.


def _requirement_layer(old_id, new_id) -> LayerDiff:
    old = make_snapshot(
        requirements=[Requirement(id=old_id, title="Speed", description="How fast", version=1, source="architecture")]
    )
    new = make_snapshot(
        requirements=[Requirement(id=new_id, title="Speed", description="How fast", version=1, source="architecture")]
    )
    return diff_snapshots(old, new).requirements


# The top level: a removed and an added element of one layer


def test_an_element_removed_and_another_added_identical_apart_from_name_hints_a_rename():
    layer = diff_snapshots(
        make_snapshot(swcs=[SWComponent(name="SpeedCtrlSWC", ports=[P_SPEED])]),
        make_snapshot(swcs=[SWComponent(name="VehicleSpeedSWC", ports=[P_SPEED])]),
    ).swcs

    assert layer.rename_hints == [RenameHint(removed_key="SpeedCtrlSWC", added_key="VehicleSpeedSWC")]
    assert [element.name for element in layer.removed] == ["SpeedCtrlSWC"]
    assert [element.name for element in layer.added] == ["VehicleSpeedSWC"]


def test_the_top_level_hint_strips_whatever_the_layers_identity_field_is_called():
    """The stripped identity field follows the layer: `requirements` key on `id`."""
    assert _requirement_layer("REQ-1", "REQ-2").rename_hints == [RenameHint(removed_key="REQ-1", added_key="REQ-2")]


def test_an_element_differing_in_more_than_its_identity_field_produces_no_top_level_hint():
    layer = diff_snapshots(
        make_snapshot(
            requirements=[
                Requirement(id="REQ-1", title="Speed", description="How fast", version=1, source="architecture")
            ]
        ),
        make_snapshot(
            requirements=[
                Requirement(
                    id="REQ-2", title="Speed", description="How fast, revised", version=1, source="architecture"
                )
            ]
        ),
    ).requirements

    assert layer.rename_hints == []
    assert ([r.id for r in layer.removed], [r.id for r in layer.added]) == (["REQ-1"], ["REQ-2"])


def _compositions(old_composition: Composition, new_composition: Composition) -> LayerDiff:
    """The ``compositions`` layer of one two-snapshot diff."""
    return diff_snapshots(
        make_snapshot(compositions=[old_composition]), make_snapshot(compositions=[new_composition])
    ).compositions


def _composition(**changes) -> Composition:
    """``Root`` with one port, one member and one delegation, and *changes* applied."""
    document = {
        "name": "Root",
        "title": "Root composition",
        "root": True,
        "ports": [CompositionPort(name="pSpeed", direction="provide", interface="ISpeed")],
        "members": [CompositionMember(name="sensor", type="SpeedSensor")],
        "delegations": [Delegation(name="dSpeed", port="pSpeed", member="sensor", member_port="pOut")],
    }
    document.update(changes)
    return Composition(**document)


class TestTheCompositionsLayerIsDiffedLikeEveryOther:
    """The compositions layer's collections, diffed: an item changed in place, renamed, added."""

    def test_a_member_that_changed_its_type_is_one_changed_item(self) -> None:
        """A member re-pointed at another type keeps its name, so it is one changed item."""
        change = _collection(
            _compositions(_composition(), _composition(members=[CompositionMember(name="sensor", type="OtherSensor")])),
            "Root",
            "members",
        )

        assert (change.added, change.removed) == ([], [])
        assert [entry.key for entry in change.changed] == ["sensor"]
        assert change.changed[0].changed_fields == [("type", "SpeedSensor", "OtherSensor")]

    def test_a_renamed_boundary_port_hints_the_rename(self) -> None:
        """A renamed item is removed and added, with a hint pairing them."""
        renamed = CompositionPort(name="pVehicleSpeed", direction="provide", interface="ISpeed")
        change = _collection(_compositions(_composition(), _composition(ports=[renamed])), "Root", "ports")

        assert change.rename_hints == [RenameHint(removed_key="pSpeed", added_key="pVehicleSpeed")]
        assert (change.removed, change.added) == ([_dump(_composition().ports[0])], [_dump(renamed)])
        assert change.changed == []

    def test_a_second_delegation_is_one_added_item(self) -> None:
        """The addition shape, and the one the coverage suite's perturbation makes."""
        second = Delegation(name="dTorque", port="pSpeed", member="sensor", member_port="pTorque")
        change = _collection(
            _compositions(_composition(), _composition(delegations=[_composition().delegations[0], second])),
            "Root",
            "delegations",
        )

        assert change.added == [_dump(second)]
        assert (change.removed, change.changed) == ([], [])

    def test_the_text_renderer_names_the_layer_and_the_element(self) -> None:
        """A changed composition is rendered by ``format_diff``."""
        diff = diff_snapshots(
            make_snapshot(compositions=[_composition()]),
            make_snapshot(compositions=[_composition(title="Renamed title")]),
        )

        text = format_diff(diff)

        assert "Compositions:" in text
        assert "Root" in text
        assert diff.compositions.is_empty is False


def test_a_top_level_hint_never_crosses_layers():
    """A rename hint never pairs elements across layers."""
    old = make_snapshot(
        requirements=[Requirement(id="Vehicle", title="T", description="D", version=1, source="architecture")],
        compositions=[],
    )
    new = make_snapshot(
        compositions=[Composition(name="Vehicle", title="T")],
    )
    diff = diff_snapshots(old, new)

    assert diff.requirements.rename_hints == []
    assert diff.compositions.rename_hints == []


# The nested level: one collection of one parent


def test_a_port_renamed_inside_one_swc_hints_the_rename_and_leaves_both_entries():
    """A renamed port, `rSpeed -> rVehicleSpeed`, is hinted and stays a removal and an addition."""
    renamed = _port("rVehicleSpeed")
    change = _collection(_swcs([P_SPEED], [renamed]), "A", "ports")

    assert change.rename_hints == [RenameHint(removed_key="rSpeed", added_key="rVehicleSpeed")]
    assert (change.removed, change.added) == ([_dump(P_SPEED)], [_dump(renamed)])
    assert change.changed == []


def test_a_nested_hint_never_crosses_parents():
    """Ports of one SWC are never rename candidates for ports of another."""
    old = make_snapshot(
        swcs=[SWComponent(name="A", ports=[P_SPEED]), SWComponent(name="B", ports=[P_TORQUE])],
    )
    new = make_snapshot(
        swcs=[SWComponent(name="A", ports=[P_TORQUE]), SWComponent(name="B", ports=[P_SPEED])],
    )
    diff = diff_snapshots(old, new)

    for key in ("A", "B"):
        assert _collection(diff.swcs, key, "ports").rename_hints == []


def test_a_near_match_differing_in_one_more_field_produces_no_nested_hint():
    """Exact equality after stripping the identity — not similarity."""
    change = _collection(_swcs([P_SPEED], [_port("rVehicleSpeed", interface="IVehicleSpeed")]), "A", "ports")

    assert change.rename_hints == []
    assert (len(change.removed), len(change.added)) == (1, 1)


def test_an_argument_renamed_inside_one_operation_hints_the_rename():
    """The same rename rule applies to an operation's arguments."""
    renamed = CSArgument(name="vehicleSpeed", direction="in", implementation_type="uint16")
    nested = _nested(
        _interfaces_with_operations(
            [CSOperation(name="SetSpeed", arguments=[ARG_SPEED])],
            [CSOperation(name="SetSpeed", arguments=[renamed])],
        ),
        "ICtrl",
        "operations",
        "SetSpeed",
        "arguments",
    )

    assert nested.rename_hints == [RenameHint(removed_key="speed", added_key="vehicleSpeed")]


# Only for an unambiguous 1:1 match


@pytest.mark.parametrize(
    ("removed_names", "added_names", "expected"),
    [
        pytest.param(["rSpeed"], ["rVehicleSpeed"], [RenameHint("rSpeed", "rVehicleSpeed")], id="one-to-one"),
        pytest.param(["rA", "rB"], ["rC", "rD"], [], id="two-to-two"),
        pytest.param(["rA", "rB"], ["rC"], [], id="two-to-one"),
        pytest.param(["rA"], ["rB", "rC"], [], id="one-to-two"),
    ],
)
def test_a_hint_is_emitted_only_for_an_unambiguous_one_to_one_match(removed_names, added_names, expected):
    """Ports sharing one residue are not paired: no false pair in a crowded group."""
    change = _collection(
        _swcs([_port(name) for name in removed_names], [_port(name) for name in added_names]),
        "A",
        "ports",
    )

    assert change.rename_hints == expected
    assert (len(change.removed), len(change.added)) == (len(removed_names), len(added_names))


def test_one_unambiguous_pair_beside_an_ambiguous_group_still_hints():
    """The 1:1 rule is per residue group, not per collection."""
    change = _collection(
        _swcs(
            [_port("rA"), _port("rB"), _port("pOnly", direction="provide")],
            [_port("rC"), _port("rD"), _port("pRenamed", direction="provide")],
        ),
        "A",
        "ports",
    )

    assert change.rename_hints == [RenameHint("pOnly", "pRenamed")]


# The collections where stripping the identity leaves nothing


def test_an_event_has_no_identity_field_to_strip():
    """An event's key is its whole value, so its residue is empty and the rule declines by itself."""
    events = collection_rule(Runnable, "events")
    ports = collection_rule(SWComponent, "ports")

    assert _identity_residue(_timing(0.01), events.key) is None
    assert _identity_residue(INIT, events.key) is None
    assert _identity_residue(_dump(P_SPEED), ports.key) == {
        "direction": "require",
        "interface": "ISpeed",
        "observed_interface": None,
        "description": "",
        "measures": {},
        "com_specs": [],
        "satisfies": [],
    }


@pytest.mark.parametrize(
    "make_change",
    [
        pytest.param(lambda: _events_change([INIT], [_timing(0.01)]), id="events"),
        pytest.param(lambda: _events_change([_timing(0.01)], [_timing(0.02)]), id="events-same-type"),
        pytest.param(lambda: _links_change(["REQ-2@1"], ["REQ-3@1"]), id="satisfies"),
        pytest.param(lambda: _memory_change({"bss": 1024}, {"heap": 1024}), id="memory"),
        pytest.param(lambda: _task_runnables_change([REF_SPEED], [REF_TORQUE]), id="task-runnables"),
    ],
)
def test_a_collection_whose_identity_is_its_whole_item_never_hints(make_change):
    """Collections whose identity is every field have an empty residue."""
    change = make_change()

    assert change.rename_hints == []
    assert change.removed and change.added


# A pair needs shared content, not shared emptiness


def _sequence_steps_change(old_steps, new_steps) -> CollectionChange:
    old = make_snapshot(sequences=[Sequence(name="cycle", task="T", steps=list(old_steps))])
    new = make_snapshot(sequences=[Sequence(name="cycle", task="T", steps=list(new_steps))])
    return _collection(diff_snapshots(old, new).sequences, "cycle", "steps")


def _swc_runnables_change(old_runnables, new_runnables) -> CollectionChange:
    old = make_snapshot(swcs=[SWComponent(name="A", runnables=list(old_runnables))])
    new = make_snapshot(swcs=[SWComponent(name="A", runnables=list(new_runnables))])
    return _collection(diff_snapshots(old, new).swcs, "A", "runnables")


def test_the_two_reference_pair_collections_answer_alike_for_a_one_for_one_replacement():
    """Replacing a reference with an unrelated one gets no hint, in a task and in a sequence alike.

    Guards a hint firing on every 1-for-1 step replacement, paired on an empty `note`.
    """
    steps = _sequence_steps_change(
        [SequenceStep(swc="A", runnable="r1")],
        [SequenceStep(swc="B", runnable="r2")],
    )
    refs = _task_runnables_change(
        [TaskRunnableRef(swc="A", runnable="r1")],
        [TaskRunnableRef(swc="B", runnable="r2")],
    )

    assert steps.rename_hints == refs.rename_hints == []
    assert (steps.removed, steps.added) != ([], [])
    assert (refs.removed, refs.added) != ([], [])


def test_a_step_carrying_the_same_note_on_both_sides_still_hints():
    """Steps sharing a non-empty note are paired on it: the decline is about the residue, not the collection."""
    change = _sequence_steps_change(
        [SequenceStep(swc="A", runnable="r1", note="drive the wheel")],
        [SequenceStep(swc="B", runnable="r2", note="drive the wheel")],
    )

    assert change.rename_hints == [RenameHint(removed_key=("A", None, "r1"), added_key=("B", None, "r2"))]


def test_a_runnable_carrying_no_events_is_not_hinted_as_a_rename():
    """Two event-free runnables share no content, so they are not paired."""
    change = _swc_runnables_change([Runnable(name="Init_Run")], [Runnable(name="Shutdown_Run")])

    assert change.rename_hints == []
    assert (change.removed, change.added) == (
        [_dump(Runnable(name="Init_Run"))],
        [_dump(Runnable(name="Shutdown_Run"))],
    )


def test_a_runnable_carrying_the_same_events_still_hints_a_rename():
    """Runnables sharing their events are paired on them."""
    change = _swc_runnables_change(
        [Runnable(name="Init_Run", events=[_timing(0.01)])],
        [Runnable(name="Startup_Run", events=[_timing(0.01)])],
    )

    assert change.rename_hints == [RenameHint(removed_key="Init_Run", added_key="Startup_Run")]


def test_an_element_with_nothing_but_its_name_is_not_hinted_as_a_rename():
    """At the top level too, an empty SWC has nothing to be paired on."""
    layer = diff_snapshots(
        make_snapshot(swcs=[SWComponent(name="A"), SWComponent(name="Keep", ports=[P_SPEED])]),
        make_snapshot(swcs=[SWComponent(name="B"), SWComponent(name="Keep", ports=[P_SPEED])]),
    ).swcs

    assert layer.rename_hints == []
    assert ([element.name for element in layer.removed], [element.name for element in layer.added]) == (["A"], ["B"])


def test_an_init_only_change_is_reported_at_the_member_it_belongs_to():
    """A changed `init` is reported whole-value at the data element, one level down, with its path."""

    def interface(init):
        return SRInterface(
            name="ISpeed",
            kind="sender_receiver",
            data_elements=[DataElement(name="speed", implementation_type="float32", init=init)],
        )

    diff = diff_snapshots(make_snapshot(interfaces=[interface(None)]), make_snapshot(interfaces=[interface(1.5)]))

    collection = _collection(diff.interfaces, "ISpeed", "data_elements")
    assert [item.key for item in collection.changed] == ["speed"]
    assert collection.changed[0].changed_fields == [("init", None, 1.5)]
    assert collection.changed[0].path == ({"field": "data_elements"}, {"member": "speed"})


def test_an_unchanged_init_is_not_a_difference():
    """Non-vacuity: the same `init` on both sides reports nothing."""

    def interface():
        return SRInterface(
            name="ISpeed",
            kind="sender_receiver",
            data_elements=[DataElement(name="speed", implementation_type="float32", init={"a": [1, 2]})],
        )

    diff = diff_snapshots(make_snapshot(interfaces=[interface()]), make_snapshot(interfaces=[interface()]))

    assert diff.interfaces.changed == []


def _renamed_data_element() -> CollectionChange:
    def interface(element_name):
        return SRInterface(
            name="ISpeed",
            kind="sender_receiver",
            data_elements=[DataElement(name=element_name, implementation_type="float32")],
        )

    layer = diff_snapshots(
        make_snapshot(interfaces=[interface("speed")]), make_snapshot(interfaces=[interface("vehicleSpeed")])
    )
    return _collection(layer.interfaces, "ISpeed", "data_elements")


def _renamed_argument() -> CollectionChange:
    def interface(argument_name):
        return CSInterface(
            name="ICompute",
            kind="client_server",
            operations=[
                CSOperation(
                    name="Compute",
                    arguments=[CSArgument(name=argument_name, direction="in", implementation_type="uint16")],
                )
            ],
        )

    layer = diff_snapshots(
        make_snapshot(interfaces=[interface("speed")]), make_snapshot(interfaces=[interface("vehicleSpeed")])
    )
    return _nested(layer.interfaces, "ICompute", "operations", "Compute", "arguments")


def _renamed_literal() -> CollectionChange:
    def data_type(literal_name):
        return EnumDataType(name="EMode", kind="enum", literals=[EnumLiteral(name=literal_name, value=0)])

    layer = diff_snapshots(
        make_snapshot(implementation_types=[data_type("Idle")]),
        make_snapshot(implementation_types=[data_type("Ready")]),
    )
    return _collection(layer.implementation_types, "EMode", "literals")


def _renamed_record_field() -> CollectionChange:
    def data_type(field_name):
        return RecordDataType(name="RPoint", kind="record", fields=[RecordField(name=field_name, type="float32")])

    layer = diff_snapshots(
        make_snapshot(implementation_types=[data_type("x")]),
        make_snapshot(implementation_types=[data_type("abscissa")]),
    )
    return _collection(layer.implementation_types, "RPoint", "fields")


@pytest.mark.parametrize(
    ("make_change", "expected"),
    [
        pytest.param(
            lambda: _collection(_swcs([P_SPEED], [_port("rVehicleSpeed")]), "A", "ports"),
            RenameHint(removed_key="rSpeed", added_key="rVehicleSpeed"),
            id="ports",
        ),
        pytest.param(_renamed_data_element, RenameHint("speed", "vehicleSpeed"), id="data_elements"),
        pytest.param(_renamed_argument, RenameHint("speed", "vehicleSpeed"), id="arguments"),
        pytest.param(_renamed_literal, RenameHint("Idle", "Ready"), id="literals"),
        pytest.param(_renamed_record_field, RenameHint("x", "abscissa"), id="fields"),
    ],
)
def test_a_genuine_rename_still_hints_in_every_name_keyed_collection(make_change, expected):
    """Items sharing a required field beside their name are still paired."""
    assert make_change().rename_hints == [expected]


def test_a_residue_keeps_what_an_item_says_and_drops_what_it_leaves_default():
    """The residue drops the identity and declared defaults; without the owner class nothing else is dropped."""
    ports = collection_rule(SWComponent, "ports")

    assert _identity_residue(_dump(P_SPEED), ports.key, (Port,)) == {"direction": "require", "interface": "ISpeed"}
    assert _identity_residue(_dump(_port("rSpeed", description="how fast")), ports.key, (Port,)) == {
        "direction": "require",
        "interface": "ISpeed",
        "description": "how fast",
    }
    assert (
        _identity_residue(_dump(Runnable(name="Init_Run")), collection_rule(SWComponent, "runnables").key, (Runnable,))
        is None
    )


# A hint is a note, never a merge


def test_a_hint_never_moves_an_item_from_added_or_removed_into_changed():
    """Hints at both levels at once leave every entry where the identity comparison put it."""
    old = make_snapshot(
        swcs=[SWComponent(name="A", ports=[P_SPEED]), SWComponent(name="B", ports=[P_TORQUE])],
    )
    new = make_snapshot(
        swcs=[SWComponent(name="A", ports=[_port("rVehicleSpeed")]), SWComponent(name="C", ports=[P_TORQUE])],
    )
    layer = diff_snapshots(old, new).swcs

    assert layer.rename_hints == [RenameHint(removed_key="B", added_key="C")]
    assert [element.name for element in layer.removed] == ["B"]
    assert [element.name for element in layer.added] == ["C"]
    assert [changed.key for changed in layer.changed] == ["A"]

    ports = _collection(layer, "A", "ports")
    assert ports.rename_hints == [RenameHint(removed_key="rSpeed", added_key="rVehicleSpeed")]
    assert (ports.removed, ports.added) == ([_dump(P_SPEED)], [_dump(_port("rVehicleSpeed"))])
    assert ports.changed == []


def test_a_changed_element_declares_no_rename_hints_of_its_own():
    """`ChangedItem` carries no rename hints; they live on the layer and the collection change.

    An always-empty attribute would read as "no rename here".
    """
    layer = _swcs([P_SPEED], [_port("rVehicleSpeed")])

    assert not hasattr(_changed_item(layer, "A"), "rename_hints")
    assert _collection(layer, "A", "ports").rename_hints == [RenameHint("rSpeed", "rVehicleSpeed")]


# format_diff(): human-readable rendering.


def test_format_diff_empty_diff_renders_no_changes_message():
    old = make_snapshot(swcs=[SWComponent(name="A")])
    new = make_snapshot(swcs=[SWComponent(name="A")])

    diff = diff_snapshots(old, new)
    text = format_diff(diff)

    assert diff.is_empty is True
    assert "no changes" in text.lower()


def test_format_diff_only_renders_non_empty_layer_sections():
    old = make_snapshot(swcs=[SWComponent(name="A")])
    new = make_snapshot(swcs=[SWComponent(name="A"), SWComponent(name="B")])

    diff = diff_snapshots(old, new)
    text = format_diff(diff)

    # only the swcs layer changed — no noise from the other layers
    assert "swc" in text.lower()
    for other_label in (
        "compositions",
        "interfaces",
        "data types",
        "connectors",
        "requirements",
        "tasks",
        "sequences",
        "hardware",
    ):
        assert other_label not in text.lower()


def test_format_diff_added_element_has_plus_marker():
    old = make_snapshot(swcs=[])
    new = make_snapshot(swcs=[SWComponent(name="B")])

    diff = diff_snapshots(old, new)
    text = format_diff(diff)

    lines = [line for line in text.splitlines() if "B" in line]
    assert any(line.strip().startswith("+") for line in lines)


def test_format_diff_removed_element_has_minus_marker():
    old = make_snapshot(swcs=[SWComponent(name="B")])
    new = make_snapshot(swcs=[])

    diff = diff_snapshots(old, new)
    text = format_diff(diff)

    lines = [line for line in text.splitlines() if "B" in line]
    assert any(line.strip().startswith("-") for line in lines)


def test_format_diff_changed_element_has_tilde_marker_and_field_values():
    old = make_snapshot(swcs=[SWComponent(name="A", description="old desc")])
    new = make_snapshot(swcs=[SWComponent(name="A", description="new desc")])

    diff = diff_snapshots(old, new)
    text = format_diff(diff)

    lines = [line for line in text.splitlines() if line.strip().startswith("~")]
    assert any("A" in line for line in lines)
    assert "old desc" in text
    assert "new desc" in text
    assert "description" in text


def test_format_diff_requirement_layer_uses_id_as_key():
    old = make_snapshot(
        requirements=[Requirement(id="REQ-1", title="Old title", description="D1", version=1, source="architecture")]
    )
    new = make_snapshot(
        requirements=[Requirement(id="REQ-1", title="New title", description="D1", version=1, source="architecture")]
    )

    diff = diff_snapshots(old, new)
    text = format_diff(diff)

    assert "REQ-1" in text
    assert "Old title" in text
    assert "New title" in text


def test_format_diff_renders_the_hardware_layer():
    old = make_snapshot(hardware=[_cpu(threshold=0.7), _mem(name="ROM0")])
    new = make_snapshot(hardware=[_cpu(threshold=0.9), _mem(name="RAM0")])

    diff = diff_snapshots(old, new)
    text = format_diff(diff)

    assert "Hardware:" in text
    assert "+ RAM0 (added)" in text
    assert "- ROM0 (removed)" in text
    assert "~ CORE0" in text
    assert "threshold: 0.7 -> 0.9" in text


def test_format_diff_output_has_no_terminal_escape_codes():
    old = make_snapshot(swcs=[SWComponent(name="A")])
    new = make_snapshot(swcs=[SWComponent(name="A"), SWComponent(name="B")])

    diff = diff_snapshots(old, new)
    text = format_diff(diff)

    assert "\x1b" not in text


# format_diff(): per-item detail.


def _swcs_snapshot(swc: SWComponent) -> ModelSnapshot:
    return make_snapshot(swcs=[swc])


# The hint pairs a removed port with an added one (a changed item is never a
# candidate), while a second port changed its interface in place.
WORKED_OLD = SWComponent(
    name="SpeedCtrlSWC",
    type="application",
    ports=[_port("rSpeed"), _port("pStatus", direction="provide", interface="IStatus")],
    runnables=[Runnable(name="SpeedCtrl_Run"), Runnable(name="TorqueCalc_Run")],
)
WORKED_NEW = SWComponent(
    name="SpeedCtrlSWC",
    type="service",
    ports=[
        _port("pTorqueLimit", direction="provide", interface="ITorque"),
        _port("rVehicleSpeed"),
        _port("pStatus", direction="provide", interface="IState"),
    ],
    runnables=[Runnable(name="TorqueCalc_Run"), Runnable(name="SpeedCtrl_Run")],
)

WORKED_TEXT = """\
SWCs:
  ~ SpeedCtrlSWC
      type: 'application' -> 'service'
      ports:
        + pTorqueLimit (added)
        + rVehicleSpeed (added)
        - rSpeed (removed)
        ~ pStatus
            interface: 'IStatus' -> 'IState'
        (possibly a rename: rSpeed -> rVehicleSpeed)
      runnables:
        (order changed)"""


def _worked_text() -> str:
    return format_diff(diff_snapshots(_swcs_snapshot(WORKED_OLD), _swcs_snapshot(WORKED_NEW)))


def test_the_worked_example_renders_exactly_as_the_plan_shows_it():
    """Pinned character for character: per-item detail, a rename note, a pure reorder, no whole list."""
    assert _worked_text() == WORKED_TEXT


def test_a_detailed_field_prints_once_and_never_beside_its_whole_value():
    """The added port is named once and no unchanged field of a port is printed."""
    text = _worked_text()

    assert text.count("pTorqueLimit") == 1
    assert text.count("ports:") == 1
    assert "description" not in text  # a field every port carries and none changed


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param(
            _swcs_snapshot(SWComponent(name="A", ports=[P_SPEED])),
            _swcs_snapshot(SWComponent(name="A", ports=[P_SPEED, P_TORQUE])),
            id="ports-added",
        ),
        pytest.param(
            _swcs_snapshot(SWComponent(name="A", ports=[P_SPEED, P_TORQUE])),
            _swcs_snapshot(SWComponent(name="A", ports=[P_TORQUE, P_SPEED])),
            id="ports-reordered",
        ),
        pytest.param(
            _swcs_snapshot(SWComponent(name="A", runnables=[Runnable(name="R", events=[_timing(0.01)])])),
            _swcs_snapshot(SWComponent(name="A", runnables=[Runnable(name="R", events=[_timing(0.02)])])),
            id="events-nested",
        ),
        pytest.param(
            _swcs_snapshot(SWComponent(name="A", memory={"bss": 1024})),
            _swcs_snapshot(SWComponent(name="A", memory={"bss": 2048})),
            id="memory",
        ),
        pytest.param(
            _swcs_snapshot(_linked(["REQ-2@1"])),
            _swcs_snapshot(_linked(["REQ-2@1", "REQ-3@1"])),
            id="satisfies",
        ),
        pytest.param(
            make_snapshot(tasks=[_task(REF_SPEED)]),
            make_snapshot(tasks=[_task(REF_SPEED, REF_TORQUE)]),
            id="task-runnables",
        ),
        pytest.param(
            make_snapshot(
                interfaces=[CSInterface(name="ICtrl", kind="client_server", operations=[CSOperation(name="Set")])]
            ),
            make_snapshot(
                interfaces=[
                    CSInterface(
                        name="ICtrl",
                        kind="client_server",
                        operations=[CSOperation(name="Set", arguments=[ARG_SPEED])],
                    )
                ]
            ),
            id="arguments-nested",
        ),
    ],
)
def test_no_detailed_field_ever_falls_back_to_printing_its_whole_value(old, new):
    """A field named by a `CollectionChange` prints as a header with its items, never `field: <old> -> <new>`.

    The `emitted` assertion shows each mutation really produced a change.
    """
    diff = diff_snapshots(old, new)
    text = format_diff(diff)

    emitted = _every_collection_change(diff)
    assert emitted
    for change in emitted:
        assert any(line.strip() == f"{change.field}:" for line in text.splitlines())
        assert f"{change.field}: " not in text


def test_a_scalar_field_beside_a_collection_field_still_renders_from_changed_fields():
    old = _swcs_snapshot(SWComponent(name="A", description="old desc", ports=[P_SPEED]))
    new = _swcs_snapshot(SWComponent(name="A", description="new desc", ports=[P_SPEED, P_TORQUE]))

    text = format_diff(diff_snapshots(old, new))

    assert "description: 'old desc' -> 'new desc'" in text
    assert "+ pTorqueLimit (added)" in text


def test_a_pure_reorder_renders_as_one_line_and_never_as_the_two_whole_lists():
    """A pure reorder prints as a reorder note, not as both port lists."""
    old = _swcs_snapshot(SWComponent(name="A", ports=[P_SPEED, P_TORQUE]))
    new = _swcs_snapshot(SWComponent(name="A", ports=[P_TORQUE, P_SPEED]))

    text = format_diff(diff_snapshots(old, new))

    assert text == "SWCs:\n  ~ A\n      ports:\n        (order changed)"


def test_a_nested_rename_hint_reads_as_a_hint_and_not_as_a_verdict():
    """A rename hint is a note beside the two entries, never a merge."""
    text = _worked_text()

    hints = [line.strip() for line in text.splitlines() if "rename" in line]
    assert hints == ["(possibly a rename: rSpeed -> rVehicleSpeed)"]
    assert "- rSpeed (removed)" in text
    assert "+ rVehicleSpeed (added)" in text


def test_a_top_level_rename_hint_renders_beside_the_layers_own_entries():
    old = make_snapshot(swcs=[SWComponent(name="SpeedCtrlSWC", ports=[P_SPEED])])
    new = make_snapshot(swcs=[SWComponent(name="VehicleSpeedSWC", ports=[P_SPEED])])

    text = format_diff(diff_snapshots(old, new))

    assert text == (
        "SWCs:\n"
        "  + VehicleSpeedSWC (added)\n"
        "  - SpeedCtrlSWC (removed)\n"
        "  (possibly a rename: SpeedCtrlSWC -> VehicleSpeedSWC)"
    )


def test_a_composite_key_renders_as_its_parts():
    """A composite reference renders as the entry writes it, `<selector>.<runnable>`.

    Guards the tuple form `(SpeedCtrlSWC, None, ...)`.
    """
    text = format_diff(
        diff_snapshots(make_snapshot(tasks=[_task(REF_SPEED)]), make_snapshot(tasks=[_task(REF_TORQUE)]))
    )

    assert "+ SpeedCtrlSWC.TorqueCalc_Run (added)" in text
    assert "- SpeedCtrlSWC.SpeedCtrl_Run (removed)" in text
    assert "None" not in text


def test_an_instance_entry_renders_its_path_and_runnable():
    """An entry written with `instance:` renders by its instance path, not as a tuple with `None`."""
    added = TaskRunnableRef(instance="Chassis/BrakeSubsystem/BrakeFadeMonitor", runnable="DetectFade")
    text = format_diff(
        diff_snapshots(make_snapshot(tasks=[_task(REF_SPEED)]), make_snapshot(tasks=[_task(REF_SPEED, added)]))
    )

    assert "+ Chassis/BrakeSubsystem/BrakeFadeMonitor.DetectFade (added)" in text


def test_a_changed_step_and_its_hint_render_in_the_written_form():
    """The `~` line of a changed composite item, and a rename hint between two, take the same form."""
    old = make_snapshot(sequences=[Sequence(name="cycle", task="T", steps=[SequenceStep(swc="A", runnable="R")])])
    new = make_snapshot(
        sequences=[Sequence(name="cycle", task="T", steps=[SequenceStep(swc="A", runnable="R", note="then this")])]
    )

    assert "~ A.R" in format_diff(diff_snapshots(old, new))


def test_the_json_form_of_a_composite_key_is_unchanged():
    """The text fix leaves the document alone: every part, the absent one as `null`."""
    added = TaskRunnableRef(instance="Chassis/Sub/Member", runnable="Run")
    diff = diff_snapshots(make_snapshot(tasks=[_task(REF_SPEED)]), make_snapshot(tasks=[_task(REF_SPEED, added)]))
    document = json.loads(format_diff_json(diff))

    [collection] = document["layers"]["tasks"]["changed"][0]["collections"]
    assert collection["added"] == [[None, "Chassis/Sub/Member", "Run"]]


def test_an_access_and_a_call_render_as_port_dot_member():
    """The other three composite rules take the same written form: `pSpeed.speed`, `pDiag.Report`.

    The rules are `reads`, `writes` (`{port, element}`) and `calls` (`{port, operation}`).
    """
    old = make_snapshot(swcs=[SWComponent(name="A", runnables=[Runnable(name="Run")])])
    new = make_snapshot(
        swcs=[
            SWComponent(
                name="A",
                runnables=[
                    Runnable(
                        name="Run",
                        reads=[DataReadRef(port="pSpeed", element="speed")],
                        calls=[OperationCallRef(port="pDiag", operation="Report")],
                    )
                ],
            )
        ]
    )

    text = format_diff(diff_snapshots(old, new))

    assert "+ pSpeed.speed (added)" in text
    assert "+ pDiag.Report (added)" in text


def test_a_value_key_keeps_its_parenthesised_form():
    """An `events` value key is no composite reference, so it keeps `(timing, 0.01)`."""
    assert _render_key(("timing", 0.01)) == "(timing, 0.01)"


def test_an_unchanged_layer_still_produces_no_output_at_all():
    """Layers nothing happened in stay silent, with per-item detail present."""
    text = _worked_text()

    assert text.splitlines()[0] == "SWCs:"
    for other_label in ("Systems:", "Containers:", "Interfaces:", "Tasks:", "Requirements:", "Resources:"):
        assert other_label not in text


def test_an_empty_diff_still_renders_the_no_changes_message():
    assert format_diff(diff_snapshots(_swcs_snapshot(WORKED_OLD), _swcs_snapshot(WORKED_OLD))) == "No changes."


def test_the_nested_output_is_still_plain_markdown():
    """No escape codes, nesting by two-space indentation, so the text survives a paste into Markdown."""
    text = _worked_text()

    assert "\x1b" not in text
    for line in text.splitlines():
        assert "\t" not in line
        assert not any(ord(character) < 32 for character in line)
        assert line == line.rstrip()
        assert (len(line) - len(line.lstrip(" "))) % 2 == 0


# format_diff_json(): the machine-readable renderer.


#: One diff carrying every fact either renderer can report: a layer's own
#: added/removed/rename hint, a changed element with a scalar field beside a
#: collection detailed item by item (with its own rename hint), a pure reorder,
#: and a nested collection two levels down.
JSON_OLD = make_snapshot(
    swcs=[
        WORKED_OLD,
        SWComponent(name="Dashboard", ports=[P_ENGINE]),
        SWComponent(name="Gateway", runnables=[Runnable(name="Gw_Run", events=[_timing(0.01)])]),
    ],
)
JSON_NEW = make_snapshot(
    swcs=[
        WORKED_NEW,
        SWComponent(name="DashboardSWC", ports=[P_ENGINE]),
        SWComponent(name="Gateway", runnables=[Runnable(name="Gw_Run", events=[_timing(0.02)])]),
    ],
)


def _json_diff() -> ModelDiff:
    return diff_snapshots(JSON_OLD, JSON_NEW)


def _json_document() -> dict:
    return json.loads(format_diff_json(_json_diff()))


def _layer_attributes() -> list[str]:
    """The layers, in `ModelDiff` field order, which is not the rendered order (see `_json_layer_keys`)."""
    return [layer.name for layer in dataclasses.fields(ModelDiff)]


def _json_layer_keys() -> list[str]:
    """The layer keys in the order `--format json` writes them: the contract's `displayOrder`.

    The key set comes from `ModelDiff` and the order from the registry, a two-sided check.
    """
    registry = production_registry()
    return [registry.layer(name).snapshot_attribute for name in registry.display_order]


# The root: a required, integer schema version


def test_the_json_root_is_a_versioned_envelope():
    """The root is a required integer `schema_version` and `layers`, nothing else.

    A consumer that does not know a version rejects the document by one field.
    """
    document = _json_document()

    assert list(document) == ["schema_version", "layers"]
    assert document["schema_version"] == 2
    assert type(document["schema_version"]) is int  # not a string, and not a bool
    assert isinstance(document["layers"], dict)


def test_the_envelope_names_all_ten_layers_whatever_changed():
    """A layer with no changes carries four empty lists, so the key set is fixed."""
    document = _json_document()

    assert sorted(document["layers"]) == sorted(_layer_attributes())
    assert list(document["layers"]) == _json_layer_keys()
    assert document["layers"]["tasks"] == {"added": [], "removed": [], "changed": [], "rename_hints": []}


def test_an_empty_diff_still_emits_the_versioned_envelope_and_not_a_message():
    """An empty diff is a parseable document, not prose."""
    text = format_diff_json(diff_snapshots(JSON_OLD, JSON_OLD))
    document = json.loads(text)

    assert "No changes." not in text
    assert document["schema_version"] == 2
    assert list(document["layers"]) == _json_layer_keys()
    assert all(
        layer == {"added": [], "removed": [], "changed": [], "rename_hints": []}
        for layer in document["layers"].values()
    )


# Every fact the text renderer shows, asserted field by field


def test_the_json_reports_the_layers_own_added_removed_and_rename_hint():
    layer = _json_document()["layers"]["swcs"]

    assert layer["added"] == ["DashboardSWC"]
    assert layer["removed"] == ["Dashboard"]
    assert layer["rename_hints"] == [{"removed_key": "Dashboard", "added_key": "DashboardSWC"}]
    assert [changed["key"] for changed in layer["changed"]] == ["SpeedCtrlSWC", "Gateway"]


def test_the_json_reports_a_scalar_field_beside_a_collection_detailed_item_by_item():
    """Added and removed ports are named, and `ports` never appears as a whole-value pair."""
    changed = {item["key"]: item for item in _json_document()["layers"]["swcs"]["changed"]}
    speed = changed["SpeedCtrlSWC"]
    collections = {entry["field"]: entry for entry in speed["collections"]}

    assert speed["fields"] == [{"field": "type", "old": "application", "new": "service"}]
    assert collections["ports"]["added"] == ["pTorqueLimit", "rVehicleSpeed"]
    assert collections["ports"]["removed"] == ["rSpeed"]
    assert collections["ports"]["changed"] == [
        {
            "key": "pStatus",
            "fields": [{"field": "interface", "old": "IStatus", "new": "IState"}],
            "collections": [],
        }
    ]
    assert collections["ports"]["rename_hints"] == [{"removed_key": "rSpeed", "added_key": "rVehicleSpeed"}]
    assert collections["ports"]["reordered"] is False


def test_the_json_reports_a_pure_reorder_as_a_reorder():
    """A pure reorder is carried by `reordered` alone, every item list empty."""
    changed = {item["key"]: item for item in _json_document()["layers"]["swcs"]["changed"]}
    collections = {entry["field"]: entry for entry in changed["SpeedCtrlSWC"]["collections"]}

    assert collections["runnables"] == {
        "field": "runnables",
        "added": [],
        "removed": [],
        "changed": [],
        "rename_hints": [],
        "reordered": True,
    }


def test_the_json_recurses_into_a_nested_collection():
    """A nested `events` change sits under its changed runnable, an event's key as its parts."""
    changed = {item["key"]: item for item in _json_document()["layers"]["swcs"]["changed"]}
    runnables = {entry["field"]: entry for entry in changed["Gateway"]["collections"]}["runnables"]
    nested = runnables["changed"][0]
    events = {entry["field"]: entry for entry in nested["collections"]}["events"]

    assert nested["key"] == "Gw_Run"
    assert events["added"] == [["timing", 0.02]]
    assert events["removed"] == [["timing", 0.01]]
    assert (events["changed"], events["rename_hints"], events["reordered"]) == ([], [], False)


def test_a_composite_key_is_reported_as_its_parts_and_not_as_a_rendered_string():
    """A composite reference stays three values, the absent selector `null`."""
    document = json.loads(
        format_diff_json(
            diff_snapshots(make_snapshot(tasks=[_task(REF_SPEED)]), make_snapshot(tasks=[_task(REF_TORQUE)]))
        )
    )
    change = document["layers"]["tasks"]["changed"][0]["collections"][0]

    assert change["added"] == [["SpeedCtrlSWC", None, "TorqueCalc_Run"]]
    assert change["removed"] == [["SpeedCtrlSWC", None, "SpeedCtrl_Run"]]


def test_a_field_absent_from_one_side_is_reported_as_null():
    """A field absent on one side after a `kind` change is `null`, not the differ's sentinel."""
    old = make_snapshot(
        implementation_types=[BaseDataType(name="Speed", kind="base_type", size=16, encoding="unsigned")]
    )
    new = make_snapshot(implementation_types=[ValueDataType(name="Speed", kind="value_type", base="uint16")])

    changed = json.loads(format_diff_json(diff_snapshots(old, new)))["layers"]["implementation_types"]["changed"][0]
    fields = {entry["field"]: entry for entry in changed["fields"]}

    assert fields["kind"] == {"field": "kind", "old": "base_type", "new": "value_type"}
    assert fields["size"] == {"field": "size", "old": 16, "new": None}  # absent from the new side
    assert fields["base"] == {"field": "base", "old": None, "new": "uint16"}  # absent from the old side


def _every_json_collection(document: dict) -> list[dict]:
    """Every collection object in the document, at every depth, in its order."""

    def walk(item: dict) -> list[dict]:
        found = []
        for entry in item["collections"]:
            found.append(entry)
            for nested in entry["changed"]:
                found.extend(walk(nested))
        return found

    return [entry for layer in document["layers"].values() for changed in layer["changed"] for entry in walk(changed)]


def test_every_collection_change_the_differ_built_reaches_the_json_with_its_reorder_flag():
    """Every collection change of the diff, `reordered` included, is in the document."""
    diff = _json_diff()
    emitted = _every_collection_change(diff)

    assert [(change.field, change.reordered) for change in emitted] == [
        (entry["field"], entry["reordered"]) for entry in _every_json_collection(json.loads(format_diff_json(diff)))
    ]
    assert any(change.reordered for change in emitted)


def _every_json_changed_item(document: dict) -> list[dict]:
    """Every changed item in the document, at every depth."""

    def walk(item: dict) -> list[dict]:
        found = [item]
        for entry in item["collections"]:
            for nested in entry["changed"]:
                found.extend(walk(nested))
        return found

    return [item for layer in document["layers"].values() for changed in layer["changed"] for item in walk(changed)]


def test_no_detailed_field_is_reported_as_a_whole_value_pair_in_the_json_either():
    """A field named by a collection change is never also among the whole-value `fields`, at any depth."""
    items = _every_json_changed_item(_json_document())

    for item in items:
        detailed = {entry["field"] for entry in item["collections"]}
        reported = {entry["field"] for entry in item["fields"]}
        assert not (detailed & reported)
    # non-vacuity: this diff really does have an item reporting in both forms
    assert any(item["fields"] and item["collections"] for item in items)


# Strict JSON: no NaN, no Infinity


def _reject_json_constant(literal: str) -> float:
    """`json.loads` refusing a bare `NaN`/`Infinity`, as a strict parser does (Python's default accepts them)."""
    raise AssertionError(f"the document carries the non-JSON literal {literal!r}")


def _strictly_parsed(text: str) -> dict:
    return json.loads(text, parse_constant=_reject_json_constant)


def _non_finite(model: type, **fields):
    """Build a model element carrying a non-finite float, past validation.

    The schema refuses these at load; `model_construct` still exercises the
    renderer's own guard, kept because the JSON document is a versioned contract.
    """
    return model.model_construct(**fields)


def _nan_runnable_diff() -> ModelDiff:
    """A diff whose new side carries a NaN, built past the schema guard."""
    return diff_snapshots(
        make_snapshot(swcs=[SWComponent(name="SwcOne", runnables=[Runnable(name="Run", min_start_interval=0.01)])]),
        make_snapshot(
            swcs=[
                SWComponent.model_construct(
                    name="SwcOne",
                    type="application",
                    description="",
                    ports=[],
                    runnables=[_non_finite(Runnable, name="Run", min_start_interval=float("nan"), events=[])],
                    memory={},
                )
            ]
        ),
    )


def test_a_nan_is_emitted_as_a_string_and_never_as_a_bare_literal():
    """RFC 8259 has no `NaN`: a non-finite value never reaches the document bare."""
    text = format_diff_json(_nan_runnable_diff())

    assert ": NaN" not in text
    document = _strictly_parsed(text)
    runnable = document["layers"]["swcs"]["changed"][0]["collections"][0]["changed"][0]
    assert runnable["fields"] == [{"field": "min_start_interval", "old": 0.01, "new": "NaN"}]


def test_both_infinities_are_emitted_as_strings_too():
    """`Infinity` and `-Infinity` in a top-level whole-value field are handled too."""
    old = make_snapshot(implementation_types=[ValueDataType(name="Speed", kind="value_type", base="float32")])
    new = make_snapshot(
        implementation_types=[
            _non_finite(
                ValueDataType,
                name="Speed",
                kind="value_type",
                base="float32",
                unit="",
                description="",
                value_min=float("-inf"),
                value_max=float("inf"),
            )
        ]
    )

    text = format_diff_json(diff_snapshots(old, new))

    assert ": Infinity" not in text and ": -Infinity" not in text
    fields = _strictly_parsed(text)["layers"]["implementation_types"]["changed"][0]["fields"]
    assert {"field": "value_min", "old": None, "new": "-Infinity"} in fields
    assert {"field": "value_max", "old": None, "new": "Infinity"} in fields


def test_a_non_finite_nested_inside_a_whole_value_is_replaced_too():
    """A non-finite float nested in a list of dicts is handled; the diff is hand-built for the shape."""
    item = ChangedItem(key="SwcOne", changed_fields=[("events", [{"type": "timing", "period": float("inf")}], [])])
    diff = dataclasses.replace(diff_snapshots(make_snapshot(), make_snapshot()), swcs=LayerDiff(changed=[item]))

    document = _strictly_parsed(format_diff_json(diff))

    assert document["layers"]["swcs"]["changed"][0]["fields"] == [
        {"field": "events", "old": [{"type": "timing", "period": "Infinity"}], "new": []}
    ]


def test_a_finite_float_stays_a_json_number():
    """Ordinary floats, including one inside an `events` key, stay numbers."""
    document = _strictly_parsed(format_diff_json(_json_diff()))

    gateway = [item for item in document["layers"]["swcs"]["changed"] if item["key"] == "Gateway"][0]
    events = gateway["collections"][0]["changed"][0]["collections"][0]
    assert events["removed"] == [["timing", 0.01]]
    assert events["added"] == [["timing", 0.02]]


def test_the_encoder_is_strict_so_a_hole_in_the_walk_could_not_pass_silently(monkeypatch):
    """A value the walk missed is a loud error, not an unparseable document (`allow_nan=False`)."""
    monkeypatch.setattr("swcraft_tools.differ._json_finite", lambda document: document)

    with pytest.raises(ValueError, match="Out of range float"):
        format_diff_json(_nan_runnable_diff())


# Determinism


def test_the_json_output_is_byte_identical_for_the_same_two_snapshots():
    """Byte-level determinism is part of the schema contract."""
    first = format_diff_json(diff_snapshots(JSON_OLD, JSON_NEW))
    second = format_diff_json(diff_snapshots(JSON_OLD, JSON_NEW))

    assert first == second
    assert first.endswith("}\n")


# The text renderer stays what it was


def _original_format_diff(diff: ModelDiff) -> str:
    """The original text renderer, reproduced as an independent statement of "unchanged output".

    One row differs: ``requirements`` leads, because requirements drive
    everything else; the per-section rendering is untouched.
    """
    sections = []
    for attr, label, key_attr in (
        ("requirements", "Requirements", "id"),
        ("compositions", "Compositions", "name"),
        ("swcs", "SWCs", "name"),
        ("interfaces", "Interfaces", "name"),
        # The `implementation_types` attribute renders under the `Data Types` label.
        ("implementation_types", "Data Types", "name"),
        ("connectors", "Connectors", "name"),
        ("tasks", "Tasks", "name"),
        ("sequences", "Sequences", "name"),
        ("hardware", "Hardware", "name"),
    ):
        layer_diff = getattr(diff, attr)
        if layer_diff.is_empty:
            continue
        lines = [f"{label}:"]
        for item in layer_diff.added:
            lines.append(f"  + {getattr(item, key_attr)} (added)")
        for item in layer_diff.removed:
            lines.append(f"  - {getattr(item, key_attr)} (removed)")
        for changed in layer_diff.changed:
            lines.append(f"  ~ {changed.key}")
            for field_name, old_value, new_value in changed.changed_fields:
                lines.append(f"      {field_name}: {old_value!r} -> {new_value!r}")
        sections.append("\n".join(lines))
    if not sections:
        return "No changes."
    return "\n\n".join(sections)


def test_the_text_renderer_is_byte_identical_to_the_original_when_no_collection_changed():
    """A diff with no collection change renders exactly as the original renderer did.

    Otherwise every reader of `format_diff` (the agent's approval prompt, the MCP
    diff tool) sees a gratuitous change.
    """
    old = make_snapshot(
        swcs=[SWComponent(name="A", description="old desc"), SWComponent(name="B", description="the b one")],
        requirements=[Requirement(id="REQ-1", title="Old title", description="D1", version=1, source="architecture")],
    )
    new = make_snapshot(
        swcs=[SWComponent(name="A", description="new desc"), SWComponent(name="C", description="the c one")],
        requirements=[Requirement(id="REQ-1", title="New title", description="D1", version=1, source="architecture")],
    )
    diff = diff_snapshots(old, new)

    assert not diff.is_empty  # or both renderers would agree on "No changes."
    assert _every_collection_change(diff) == []  # the condition the claim is made under
    assert diff.swcs.rename_hints == []  # and no note the original had no way to print
    assert format_diff(diff) == _original_format_diff(diff)


# Per-leaf addressing.

#: The three step forms `$defs/pathStep` admits. There is deliberately no
#: ordinal one, in either direction: an approved `{index: 0}` would denote a
#: different member as soon as anything were inserted before it.
_STEP_FORMS = {"field", "member", "key"}

#: The deep fixture pair: one diff reaching every shape an address has a step for.
#: `fixtures/differ_golden/` records its rendered diff, so "every leaf is addressable"
#: and "not one rendered byte moved" are claims over the same input.
DEEP_OLD = make_snapshot(
    swcs=[
        SWComponent(
            name="SpeedCtrlSWC",
            ports=[_port("rSpeed"), _port("pStatus", direction="provide", interface="IStatus")],
            runnables=[
                Runnable(name="SpeedCtrl_Run", min_start_interval=0.01, events=[_timing(0.01), INIT]),
                Runnable(name="TorqueCalc_Run"),
            ],
            memory={"RAM": 1024, "ROM": 4096},
            satisfies=["REQ-SPEED-001@1", "REQ-SPEED-002@1"],
        )
    ],
    interfaces=[
        CSInterface(
            name="ICtrl",
            kind="client_server",
            operations=[CSOperation(name="ReadDtc", arguments=[ARG_SPEED, ARG_STATUS])],
        )
    ],
    implementation_types=[
        EnumDataType(
            name="Gear",
            kind="enum",
            literals=[EnumLiteral(name="Idle", value=0), EnumLiteral(name="Drive", value=1)],
        )
    ],
    requirements=[
        Requirement(id="REQ-SPEED-001", title="Speed", description="d", version=1, source="architecture"),
        Requirement(id="REQ-SPEED-002", title="Torque", description="d", version=1, source="architecture"),
    ],
    tasks=[
        Task(
            name="Task_10ms",
            period=0.01,
            priority=10,
            partition="PARTITION0",
            runnables=[
                TaskRunnableRef(swc="SpeedCtrlSWC", runnable="SpeedCtrl_Run"),
                TaskRunnableRef(swc="SpeedCtrlSWC", runnable="TorqueCalc_Run"),
            ],
        )
    ],
    sequences=[
        Sequence(
            name="SpeedFlow",
            task="Task_10ms",
            steps=[
                SequenceStep(swc="SpeedCtrlSWC", runnable="SpeedCtrl_Run", note="reads the sensor"),
                SequenceStep(swc="SpeedCtrlSWC", runnable="TorqueCalc_Run"),
            ],
        )
    ],
)

DEEP_NEW = make_snapshot(
    swcs=[
        SWComponent(
            name="SpeedCtrlSWC",
            ports=[_port("pStatus", direction="provide", interface="IState"), _port("rSpeed")],
            runnables=[
                Runnable(name="SpeedCtrl_Run", min_start_interval=0.02, events=[_timing(0.02), INIT]),
                Runnable(name="TorqueCalc_Run"),
            ],
            memory={"RAM": 2048, "FLASH": 512},
            satisfies=["REQ-SPEED-001@1"],
        )
    ],
    interfaces=[
        CSInterface(
            name="ICtrl",
            kind="client_server",
            operations=[
                CSOperation(
                    name="ReadDtc",
                    arguments=[CSArgument(name="speed", direction="in", implementation_type="uint32"), ARG_STATUS],
                )
            ],
        )
    ],
    implementation_types=[
        EnumDataType(
            name="Gear",
            kind="enum",
            literals=[EnumLiteral(name="Drive", value=1), EnumLiteral(name="Idle", value=0)],
        )
    ],
    requirements=[
        Requirement(id="REQ-SPEED-001", title="Speed", description="d", version=1, source="architecture"),
        Requirement(id="REQ-SPEED-002", title="Torque", description="d", version=1, source="architecture"),
    ],
    tasks=[
        Task(
            name="Task_10ms",
            period=0.01,
            priority=10,
            partition="PARTITION0",
            runnables=[TaskRunnableRef(swc="SpeedCtrlSWC", runnable="SpeedCtrl_Run")],
        )
    ],
    sequences=[
        Sequence(
            name="SpeedFlow",
            task="Task_10ms",
            steps=[
                SequenceStep(swc="SpeedCtrlSWC", runnable="SpeedCtrl_Run", note="reads the wheel sensor"),
                SequenceStep(swc="SpeedCtrlSWC", runnable="TorqueCalc_Run"),
            ],
        )
    ],
)


def _deep_diff() -> ModelDiff:
    return diff_snapshots(DEEP_OLD, DEEP_NEW)


def _every_changed_item(diff: ModelDiff, layer_attr: str | None = None) -> list[ChangedItem]:
    """Every changed item of the diff, at every depth, in one layer or in all."""

    def walk(item: ChangedItem) -> list[ChangedItem]:
        found = [item]
        for entry in item.collection_changes:
            for nested in entry.changed:
                found.extend(walk(nested))
        return found

    attributes = [layer_attr] if layer_attr else _layer_attributes()
    return [found for attr in attributes for item in getattr(diff, attr).changed for found in walk(item)]


def _collection_at(diff: ModelDiff, layer_attr: str, path: tuple) -> CollectionChange:
    """The one collection change of one layer reached by *path*."""
    matching = [
        entry
        for item in _every_changed_item(diff, layer_attr)
        for entry in item.collection_changes
        if entry.path == path
    ]
    assert len(matching) == 1, f"expected one collection change at {path}, got {len(matching)}"
    return matching[0]


def _item_at(diff: ModelDiff, path: tuple) -> ChangedItem:
    """The one changed item reached by *path*."""
    matching = [item for item in _every_changed_item(diff) if item.path == path]
    assert len(matching) == 1, f"expected one changed item at {path}, got {len(matching)}"
    return matching[0]


# The steps themselves


def test_an_element_carries_no_path_because_its_address_is_its_identity():
    """An element's own path is empty: the steps begin inside it."""
    diff = _deep_diff()

    assert [item.key for item in diff.swcs.changed] == ["SpeedCtrlSWC"]
    assert diff.swcs.changed[0].path == ()


def test_a_changed_collection_field_is_addressed_at_its_field_step():
    """A collection change's path ends at its field, where a pure reorder is reported."""
    element = _deep_diff().swcs.changed[0]

    assert [entry.path for entry in element.collection_changes] == [
        ({"field": "ports"},),
        ({"field": "runnables"},),
        ({"field": "memory"},),
        ({"field": "satisfies"},),
    ]


def test_a_changed_member_is_addressed_by_its_identity_beneath_the_field():
    ports = _collection_at(_deep_diff(), "swcs", ({"field": "ports"},))

    assert [item.path for item in ports.changed] == [({"field": "ports"}, {"member": "pStatus"})]


def test_a_leaf_three_levels_down_is_addressed_by_every_step_that_leads_to_it():
    """A changed runnable field's address is the item's path plus the field's own step."""
    runnable = _item_at(_deep_diff(), ({"field": "runnables"}, {"member": "SpeedCtrl_Run"}))
    changed = [name for name, _old, _new in runnable.changed_fields]

    assert "min_start_interval" in changed
    assert (*runnable.path, {"field": "min_start_interval"}) == (
        {"field": "runnables"},
        {"member": "SpeedCtrl_Run"},
        {"field": "min_start_interval"},
    )


def test_a_nested_collection_extends_the_path_of_the_member_that_holds_it():
    """`runnables -> events`, the first of the model's two three-level paths."""
    runnable = _item_at(_deep_diff(), ({"field": "runnables"}, {"member": "SpeedCtrl_Run"}))
    events = runnable.collection_changes[0]

    assert events.field == "events"
    assert events.path == ({"field": "runnables"}, {"member": "SpeedCtrl_Run"}, {"field": "events"})


def test_the_second_three_level_path_is_addressed_the_same_way():
    """The same holds for `operations -> arguments`."""
    argument = _item_at(
        _deep_diff(),
        ({"field": "operations"}, {"member": "ReadDtc"}, {"field": "arguments"}, {"member": "speed"}),
    )

    assert argument.changed_fields == [("implementation_type", "uint16", "uint32")]


def test_a_composite_member_is_addressed_by_the_declared_pair_as_an_object():
    """A composite reaches an address as an object, the absent selector as `None`."""
    step = _item_at(
        _deep_diff(),
        ({"field": "steps"}, {"member": {"swc": "SpeedCtrlSWC", "instance": None, "runnable": "SpeedCtrl_Run"}}),
    )

    assert step.key == ("SpeedCtrlSWC", None, "SpeedCtrl_Run")
    assert step.changed_fields == [("note", "reads the sensor", "reads the wheel sensor")]


def test_a_whole_value_member_is_addressed_by_its_own_value():
    """An `events` step carries the dumped member itself.

    The delta projection publishes it reduced (`editing.address.reduce_whole_value`).
    """
    events = _collection_at(
        _deep_diff(), "swcs", ({"field": "runnables"}, {"member": "SpeedCtrl_Run"}, {"field": "events"})
    )

    assert [events.rule.member_identity(item) for item in events.added] == [_timing(0.02)]
    assert [events.rule.member_identity(item) for item in events.removed] == [_timing(0.01)]


def test_a_whole_value_string_member_is_addressed_by_the_string_itself():
    links = _collection_at(_deep_diff(), "swcs", ({"field": "satisfies"},))

    assert [links.rule.member_identity(item) for item in links.removed] == ["REQ-SPEED-002@1"]


def test_a_mapping_entry_is_addressed_by_a_key_step_and_never_by_a_member_one():
    """A mapping entry, unique by construction, is reached by `{key}`."""
    memory = _collection_at(_deep_diff(), "swcs", ({"field": "memory"},))

    assert [item.path for item in memory.changed] == [({"field": "memory"}, {"key": "RAM"})]
    assert memory.changed[0].changed_fields == [("value", 1024, 2048)]


def test_an_added_or_removed_member_is_addressed_under_its_collections_own_path():
    """An added or removed member's address is composable from the collection change alone."""
    task_runnables = _collection_at(_deep_diff(), "tasks", ({"field": "runnables"},))
    removed = task_runnables.removed[0]

    assert (*task_runnables.path, {"member": task_runnables.rule.member_identity(removed)}) == (
        {"field": "runnables"},
        {"member": {"swc": "SpeedCtrlSWC", "instance": None, "runnable": "TorqueCalc_Run"}},
    )


def test_a_pure_reorder_is_reachable_at_the_collections_field_address():
    """A reorder's address is the field's: the order of the whole changed."""
    literals = _collection_at(_deep_diff(), "implementation_types", ({"field": "literals"},))

    assert literals.reordered is True
    assert literals.path == ({"field": "literals"},)


# The invariants every path has to satisfy


def _every_path(diff: ModelDiff) -> list[tuple]:
    return [item.path for item in _every_changed_item(diff)] + [entry.path for entry in _every_collection_change(diff)]


def test_every_step_of_every_path_is_one_of_the_three_declared_forms():
    """Every step is a one-key object carrying `field`, `member` or `key`; no ordinal step."""
    steps = [step for path in _every_path(_deep_diff()) for step in path]

    assert steps
    for step in steps:
        assert isinstance(step, dict)
        assert len(step) == 1
        assert set(step) <= _STEP_FORMS


def test_a_collection_change_extends_the_path_of_the_item_that_holds_it():
    """A collection's path is its owner's plus its field step; a nested item's, its collection's plus one step."""
    for item in _every_changed_item(_deep_diff()):
        for entry in item.collection_changes:
            assert entry.path == (*item.path, {"field": entry.field})
            for nested in entry.changed:
                assert nested.path[: len(entry.path)] == entry.path
                assert len(nested.path) == len(entry.path) + 1


def test_no_path_step_ever_names_storage():
    """An address names no storage location: no directory, file name, extension, separator or placement."""
    rendered = json.dumps(_every_path(_deep_diff()))

    for forbidden in ("/", "\\", "..", ".yaml", "group"):
        assert forbidden not in rendered


def test_the_placement_of_an_swc_takes_no_part_in_the_comparison():
    """`group` is not compared: it is no field of `SWComponent`, and an element moved is removed and added."""
    old = make_snapshot(swcs=[SWComponent(name="A", description="d")])
    new = make_snapshot(swcs=[SWComponent(name="A", description="d")])
    old.swc_placement = {"A": {"group": "sensors"}}
    new.swc_placement = {"A": {"group": "actuators"}}

    assert diff_snapshots(old, new).is_empty
    assert "group" not in SWComponent.model_fields


# The rendered diff is unchanged, byte for byte

GOLDEN_DIFF_DIR = Path(__file__).resolve().parent / "fixtures" / "differ_golden"


def test_the_rendered_text_diff_is_byte_identical_to_the_golden():
    """The text rendering of the deep pair matches its golden byte for byte.

    A reviewer approves a change set on this text, so per-leaf addressing must not move a byte.
    """
    golden = (GOLDEN_DIFF_DIR / "deep-pair.txt").read_text(encoding="utf-8")

    assert format_diff(_deep_diff()) + "\n" == golden


def test_the_rendered_json_diff_is_byte_identical_to_the_golden():
    """The JSON rendering of the deep pair matches its golden byte for byte."""
    golden = (GOLDEN_DIFF_DIR / "deep-pair.json").read_text(encoding="utf-8")

    assert format_diff_json(_deep_diff()) == golden


def test_the_json_document_discloses_no_address_at_all():
    """The paths are structure, not output: the JSON document carries no path key."""
    text = format_diff_json(_deep_diff())
    document = json.loads(text)
    changed = document["layers"]["swcs"]["changed"][0]

    assert document["schema_version"] == 2
    assert '"path"' not in text
    assert list(changed) == ["key", "fields", "collections"]
    assert all(
        list(entry) == ["field", "added", "removed", "changed", "rename_hints", "reordered"]
        for entry in changed["collections"]
    )


def test_an_integer_too_wide_to_render_is_described_instead_of_aborting_the_diff():
    """A changed integer too wide for `repr` is described, not raised on, in the text rendering.

    Guards `ValueError` from `int.__repr__`'s digit limit on a hand-authored hex `init`.
    **Does not reach** `--format json`, which serializes the value and still raises:
    a placeholder there would put a false number in a document a program reads.
    """
    wide = int("f" * 4000, 16)
    old = make_snapshot(
        interfaces=[
            SRInterface(
                name="IValue",
                kind="sender_receiver",
                data_elements=[DataElement(name="value", implementation_type="uint8", init=1)],
            )
        ]
    )
    new = make_snapshot(
        interfaces=[
            SRInterface(
                name="IValue",
                kind="sender_receiver",
                data_elements=[DataElement(name="value", implementation_type="uint8", init=wide)],
            )
        ]
    )

    text = format_diff(diff_snapshots(old, new))

    assert "init: 1 -> <an integer of 16000 bits, too wide to render>" in text
    with pytest.raises(ValueError, match="Exceeds the limit"):
        format_diff_json(diff_snapshots(old, new))


@pytest.mark.parametrize(
    ("ordinary", "carrying", "expected"),
    [
        pytest.param(
            {"load": 1},
            {"load": int("f" * 4000, 16)},
            "init: {'load': 1} -> {'load': <an integer of 16000 bits, too wide to render>}",
            id="record",
        ),
        pytest.param(
            [1, 2],
            [1, int("f" * 4000, 16)],
            "init: [1, 2] -> [1, <an integer of 16000 bits, too wide to render>]",
            id="array",
        ),
    ],
)
def test_a_container_init_carrying_such_an_integer_is_described_too(ordinary, carrying, expected):
    """A too-wide integer inside a record or array `init` is described too.

    The ordinary and the carrying side take different branches, so a renderer
    describing every container would fail. **Does not reach** `--format json`,
    as above.
    """
    old = make_snapshot(
        interfaces=[
            SRInterface(
                name="IValue",
                kind="sender_receiver",
                data_elements=[DataElement(name="value", implementation_type="uint8", init=ordinary)],
            )
        ]
    )
    new = make_snapshot(
        interfaces=[
            SRInterface(
                name="IValue",
                kind="sender_receiver",
                data_elements=[DataElement(name="value", implementation_type="uint8", init=carrying)],
            )
        ]
    )

    text = format_diff(diff_snapshots(old, new))

    assert expected in text
    with pytest.raises(ValueError, match="Exceeds the limit"):
        format_diff_json(diff_snapshots(old, new))


def test_a_composite_identity_key_renders_through_the_same_policy():
    """`_render_key` renders a model value through `rendering.render_model_scalar` too.

    No model value reaches it with a wide integer today; the rule holds anyway so
    the next value-keyed collection needs no exception.
    """
    wide = int("f" * 4000, 16)

    assert _render_key(("ExampleSWC", 1)) == "(ExampleSWC, 1)"
    assert _render_key(("ExampleSWC", wide)) == "(ExampleSWC, <an integer of 16000 bits, too wide to render>)"
