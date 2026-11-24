"""Tests for the collection identity vocabulary in differ.py.

* **Correctness** — one row per collection, each rule exercised against a real
  dumped element, so a rule that disagrees with ``model_dump(by_alias=True)``
  fails here rather than in the renderer.
* **Order** — which collections carry a reorder signal, why the split falls
  where it does (evidence tests reproduce the observable consequence), and what
  ``_reordered`` answers for a pair of key sequences.
* **Address form** — ``member_identity`` is the same identity as the key.

Completeness (the vocabulary names exactly the collections ``schema.py``
reaches) is not answered here: ``test_differ_coverage.py`` derives that
universe once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import _dump
from pydantic import BaseModel
from swcraft_tools.differ import _reordered, collection_identity, collection_rule
from swcraft_tools.model_registry import production_registry
from swcraft_tools.schema import (
    CanEnterArea,
    Composition,
    CompositionMember,
    CompositionPort,
    Core,
    CSArgument,
    CSInterface,
    CSOperation,
    DataElement,
    DataEvent,
    DataReadRef,
    DataTypeMap,
    DataWriteRef,
    Delegation,
    DisabledMode,
    EnumDataType,
    EnumLiteral,
    ExclusiveArea,
    ExternalInterface,
    ImplementationElement,
    ImplementationRecordDataType,
    InitEvent,
    MappingSet,
    Memory,
    OperationCallRef,
    Partition,
    Port,
    PortComSpec,
    Processor,
    RecordDataType,
    RecordField,
    Requirement,
    Runnable,
    RunsInsideArea,
    Sequence,
    SequenceStep,
    SRInterface,
    SWComponent,
    TableEntry,
    Task,
    TaskRunnableRef,
    TextTable,
    TimingEvent,
    Variable,
    VariableReadRef,
    VariableWriteRef,
)
from swcraft_tools.validator import check_sequence_consistency
from swcraft_tools.writer.arxml_generation import generate_arxml

#: ``(owner class, field name) -> CollectionRule`` over the production contract, read
#: from the kernel; the projection :func:`~swcraft_tools.differ.collection_rule` resolves per call.
COLLECTION_IDENTITY = collection_identity(production_registry())


def _key(owner: type[BaseModel], field_name: str, element: Any) -> Any:
    rule = collection_rule(owner, field_name)
    assert rule is not None, f"no rule for {owner.__name__}.{field_name}"
    return rule.key(element)


# The `.name`-keyed collections.

NAME_KEYED = [
    pytest.param(SWComponent, "ports", Port(name="pSpeed", direction="provide", interface="ISpeed"), id="ports"),
    pytest.param(SWComponent, "runnables", Runnable(name="RunEngine"), id="swc-runnables"),
    pytest.param(
        SWComponent,
        "variables",
        Variable(name="FilteredSpeed", access="implicit", implementation_type="uint16"),
        id="swc-variables",
    ),
    pytest.param(SWComponent, "exclusive_areas", ExclusiveArea(name="SpeedState"), id="swc-exclusive_areas"),
    pytest.param(
        SRInterface, "data_elements", DataElement(name="speed", implementation_type="uint32"), id="data_elements"
    ),
    pytest.param(CSInterface, "operations", CSOperation(name="ReadDtc"), id="operations"),
    pytest.param(
        CSOperation, "arguments", CSArgument(name="dtc", direction="out", implementation_type="uint32"), id="arguments"
    ),
    pytest.param(EnumDataType, "literals", EnumLiteral(name="Idle", value=0), id="literals"),
    pytest.param(RecordDataType, "fields", RecordField(name="speed", type="uint32"), id="fields"),
    pytest.param(
        ImplementationRecordDataType,
        "elements",
        ImplementationElement(name="speed", type="uint32_impl"),
        id="elements",
    ),
    pytest.param(TextTable, "entries", TableEntry(name="Idle", value=0), id="entries"),
    pytest.param(
        Composition,
        "ports",
        CompositionPort(name="pSpeed", direction="provide", interface="ISpeed"),
        id="composition-ports",
    ),
    pytest.param(Composition, "members", CompositionMember(name="sensor", type="SpeedSensor"), id="members"),
    pytest.param(
        Composition,
        "delegations",
        Delegation(name="dSpeed", port="pSpeed", member="sensor", member_port="pOut"),
        id="delegations",
    ),
]


@pytest.mark.parametrize(("owner", "field_name", "item"), NAME_KEYED)
def test_a_name_keyed_collection_keys_on_its_items_name(owner, field_name, item):
    assert _key(owner, field_name, _dump(item)) == item.name


@pytest.mark.parametrize(("owner", "field_name", "item"), NAME_KEYED)
def test_a_name_keyed_item_that_changed_everything_but_its_name_keeps_its_key(owner, field_name, item):
    """Identity is the name alone, so a changed item is not a removal plus an addition."""
    dumped = _dump(item)
    mutated = {name: (value if name == "name" else f"{value}X") for name, value in dumped.items()}
    assert _key(owner, field_name, mutated) == _key(owner, field_name, dumped)


def test_the_name_keyed_rows_cover_every_name_keyed_collection():
    """A further ``.name``-keyed collection must not slip past the rows above.

    Decided by behaviour: every rule keys one probe carrying every identity field,
    and the ones answering with the probe's name are ``.name``-keyed.
    """
    probe = {
        "name": "probe",
        "swc": "A",
        "instance": None,
        "runnable": "R",
        "type": "init",
        "port": "P",
        "element": "E",
        "operation": "O",
        # The identity fields of the `field`-keyed collections not keyed by `name`:
        # present so those rules can answer, with a value other than "probe".
        "application": "Kph",
        "data_element": "speed",
        "variable": "FilteredSpeed",
        "area": "SpeedState",
    }
    covered = {(owner, field_name) for owner, field_name, _ in (row.values for row in NAME_KEYED)}
    name_keyed = {entry for entry, rule in COLLECTION_IDENTITY.items() if rule.key(probe) == "probe"}
    assert covered == name_keyed


# The two composite-keyed collections.


@pytest.mark.parametrize(
    ("owner", "field_name", "item"),
    [
        pytest.param(Task, "runnables", TaskRunnableRef(swc="EngineController", runnable="RunEngine"), id="task"),
        pytest.param(Sequence, "steps", SequenceStep(swc="EngineController", runnable="RunEngine"), id="sequence"),
    ],
)
def test_a_reference_collection_keys_on_the_swc_runnable_pair(owner, field_name, item):
    # The absent `instance` selector is `None`.
    assert _key(owner, field_name, _dump(item)) == ("EngineController", None, "RunEngine")


@pytest.mark.parametrize(
    ("owner", "field_name", "make"),
    [
        pytest.param(Task, "runnables", TaskRunnableRef, id="task"),
        pytest.param(Sequence, "steps", SequenceStep, id="sequence"),
    ],
)
def test_two_references_differing_only_in_swc_are_distinct_items(owner, field_name, make):
    """The same runnable name under two SWCs is two items, never one that changed."""
    first = _key(owner, field_name, _dump(make(swc="EngineController", runnable="Run")))
    second = _key(owner, field_name, _dump(make(swc="Dashboard", runnable="Run")))
    assert first != second


def test_a_sequence_steps_note_does_not_take_part_in_its_identity():
    """``note`` is annotation, so editing it is a changed step, not a removal and an addition."""
    plain = _dump(SequenceStep(swc="A", runnable="R"))
    annotated = _dump(SequenceStep(swc="A", runnable="R", note="then this"))
    assert _key(Sequence, "steps", plain) == _key(Sequence, "steps", annotated)


# The mapping and the scalar list.


def test_memory_is_a_mapping_keyed_by_its_pool_name():
    rule = collection_rule(SWComponent, "memory")
    assert rule.kind == "mapping"
    dumped = _dump(SWComponent(name="A", memory={"PoolA": 128, "PoolB": 64}))["memory"]
    assert [rule.key(element) for element in dumped] == ["PoolA", "PoolB"]


def test_satisfies_is_a_list_whose_string_is_its_own_key():
    rule = collection_rule(SWComponent, "satisfies")
    assert rule.kind == "list"
    dumped = _dump(SWComponent(name="A", satisfies=["REQ-1@1", "REQ-2@3"]))["satisfies"]
    assert [rule.key(element) for element in dumped] == ["REQ-1@1", "REQ-2@3"]


def test_the_two_mapping_backed_collections_are_the_only_ones():
    """The reorder split rests on the mapping-backed collections, so they are pinned.

    A mapping's items are its keys: an entry is added or removed by name, and a
    changed value is one changed item.
    """
    mappings = {entry for entry, rule in COLLECTION_IDENTITY.items() if rule.kind == "mapping"}
    assert mappings == {(SWComponent, "memory"), (Port, "measures")}


# `events`: the canonical value key.


def test_an_init_event_keys_as_its_type_alone():
    assert _key(Runnable, "events", _dump(InitEvent(type="init"))) == ("init",)


def test_a_timing_event_keys_as_its_type_and_period():
    assert _key(Runnable, "events", _dump(TimingEvent(type="timing", period=0.01))) == ("timing", 0.01)


def test_two_timing_events_with_different_periods_have_different_keys():
    """The period is part of the key, so a changed period is one event removed and one added."""
    slow = _key(Runnable, "events", _dump(TimingEvent(type="timing", period=0.02)))
    fast = _key(Runnable, "events", _dump(TimingEvent(type="timing", period=0.01)))
    assert slow != fast


def test_two_identical_timing_events_have_the_same_key():
    """Two copies collide, so the multiset comparison reports "lost one of two" as one removal."""
    first = _key(Runnable, "events", _dump(TimingEvent(type="timing", period=0.01)))
    second = _key(Runnable, "events", _dump(TimingEvent(type="timing", period=0.01)))
    assert first == second


def test_an_init_event_and_a_timing_event_never_share_a_key():
    init = _key(Runnable, "events", _dump(InitEvent(type="init")))
    timing = _key(Runnable, "events", _dump(TimingEvent(type="timing", period=0.01)))
    assert init != timing


def test_an_event_key_is_hashable_because_the_comparison_counts_keys():
    keys = [
        _key(Runnable, "events", _dump(InitEvent(type="init"))),
        _key(Runnable, "events", _dump(TimingEvent(type="timing", period=0.01))),
    ]
    assert len(set(keys)) == 2


# A field that is no collection at all.


def test_a_field_that_is_not_a_collection_has_no_rule():
    assert collection_rule(SWComponent, "type") is None
    assert collection_rule(Task, "period") is None
    assert collection_rule(Port, "name") is None


# The ordered/unordered classification.

#: Which collections carry a reorder signal: the independent statement of what the
#: implementation's derivation from ``kind`` must produce. Every list-backed
#: collection is True, even where order means nothing; a mapping carries no order.
REORDER_SIGNAL = {
    (SWComponent, "ports"): True,
    (SWComponent, "runnables"): True,
    (SWComponent, "memory"): False,
    (Port, "measures"): False,
    (SRInterface, "data_elements"): True,
    (CSInterface, "operations"): True,
    (CSOperation, "arguments"): True,
    (EnumDataType, "literals"): True,
    (RecordDataType, "fields"): True,
    (Task, "runnables"): True,
    (Sequence, "steps"): True,
    (Runnable, "events"): True,
    (Runnable, "reads"): True,
    (Runnable, "writes"): True,
    (Runnable, "calls"): True,
    (Memory, "accessible"): True,
    (Composition, "ports"): True,
    (Composition, "members"): True,
    (Composition, "delegations"): True,
    (MappingSet, "maps"): True,
    (SWComponent, "data_type_mappings"): True,
    (ExternalInterface, "operations"): True,
    (ExternalInterface, "data_elements"): True,
    (ImplementationRecordDataType, "elements"): True,
    (TextTable, "entries"): True,
    (Port, "com_specs"): True,
    (TimingEvent, "disabled_modes"): True,
    (DataEvent, "disabled_modes"): True,
    (SWComponent, "satisfies"): True,
    (Port, "satisfies"): True,
    (Runnable, "satisfies"): True,
    (SRInterface, "satisfies"): True,
    (CSInterface, "satisfies"): True,
    (ExternalInterface, "satisfies"): True,
    (Requirement, "derived_from"): True,
    (Composition, "satisfies"): True,
    (Task, "satisfies"): True,
    (Processor, "satisfies"): True,
    (Core, "satisfies"): True,
    (Memory, "satisfies"): True,
    (Partition, "satisfies"): True,
    (SWComponent, "variables"): True,
    (Runnable, "reads_variables"): True,
    (Runnable, "writes_variables"): True,
    (SWComponent, "exclusive_areas"): True,
    (Runnable, "can_enter"): True,
    (Runnable, "runs_inside"): True,
}


def test_the_classification_covers_every_collection_in_the_vocabulary():
    assert set(REORDER_SIGNAL) == set(COLLECTION_IDENTITY)


@pytest.mark.parametrize(
    ("owner", "field_name", "expected"),
    [
        pytest.param(owner, name, expected, id=f"{owner.__name__}.{name}")
        for (owner, name), expected in REORDER_SIGNAL.items()
    ],
)
def test_each_collection_carries_the_reorder_signal_the_classification_pins(owner, field_name, expected):
    assert COLLECTION_IDENTITY[(owner, field_name)].reports_reorder is expected


def test_the_mappings_are_the_only_collections_exempt_from_the_reorder_signal():
    """Only the mappings are exempt from the reorder signal: a mapping carries no order."""
    exempt = {entry for entry, rule in COLLECTION_IDENTITY.items() if not rule.reports_reorder}
    assert exempt == {(SWComponent, "memory"), (Port, "measures")}


def test_satisfies_carries_the_signal_although_its_order_means_nothing():
    """``satisfies`` carries the signal: as unordered, a pure reorder would print both whole lists."""
    assert COLLECTION_IDENTITY[(SWComponent, "satisfies")].reports_reorder is True


def test_a_reordered_list_differs_under_model_dump_while_a_reordered_mapping_does_not():
    """The reason for the split: ``model_dump`` comparison is order-sensitive for a list, not for a mapping."""
    ports = [
        Port(name="pSpeed", direction="provide", interface="ISpeed"),
        Port(name="rTorque", direction="require", interface="ITorque"),
    ]
    assert _dump(SWComponent(name="A", ports=ports)) != _dump(SWComponent(name="A", ports=list(reversed(ports))))

    pools = {"PoolA": 128, "PoolB": 64}
    reversed_pools = dict(reversed(list(pools.items())))
    assert _dump(SWComponent(name="A", memory=pools)) == _dump(SWComponent(name="A", memory=reversed_pools))


def test_a_reordered_satisfies_list_differs_under_model_dump():
    """The criterion is the backing type, not the meaning: a reordered ``satisfies`` still differs."""
    links = ["REQ-2@1", "REQ-7@1"]
    forward = SWComponent(name="A", satisfies=links)
    backward = SWComponent(name="A", satisfies=list(reversed(links)))
    assert _dump(forward) != _dump(backward)


# Evidence for the order-semantic claims.


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def _write_sequence_model(model_root: Path, steps: list[dict]) -> None:
    """A model whose one task runs two runnables in a fixed order, with a sequence of *steps*."""
    _write_yaml(
        model_root / "swc" / "body" / "SpeedCtrlSWC.yaml",
        {
            "name": "SpeedCtrlSWC",
            "type": "application",
            "ports": [],
            "runnables": [{"name": "SpeedCtrl_Run"}, {"name": "TorqueCalc_Run"}],
            "handle_termination_and_restart": "no_support",
        },
    )
    _write_yaml(
        model_root / "tasks" / "Task_10ms.yaml",
        {
            "name": "Task_10ms",
            "period": 0.01,
            "priority": 5,
            "partition": "PARTITION0",
            "runnables": [
                {"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"},
                {"swc": "SpeedCtrlSWC", "runnable": "TorqueCalc_Run"},
            ],
        },
    )
    _write_yaml(
        model_root / "sequences" / "task10ms_cycle.yaml",
        {"name": "task10ms_cycle", "task": "Task_10ms", "steps": steps},
    )


_ORDERED_STEPS = [
    {"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"},
    {"swc": "SpeedCtrlSWC", "runnable": "TorqueCalc_Run"},
]


def test_evidence_reordering_sequence_steps_makes_the_model_inconsistent(tmp_path):
    """``Sequence.steps``' order is semantic: the same steps reordered fail ``check_sequence_consistency``."""
    _write_sequence_model(tmp_path, _ORDERED_STEPS)
    assert check_sequence_consistency(str(tmp_path)).consistent is True

    _write_sequence_model(tmp_path, list(reversed(_ORDERED_STEPS)))
    result = check_sequence_consistency(str(tmp_path))
    assert result.consistent is False
    assert any("out of order" in issue for issue in result.issues)


def _generate_with_events(model_root: Path, events: list[dict], output_name: str) -> str:
    """Generate ARXML for a one-runnable SWC carrying *events*, in that order."""
    _write_yaml(
        model_root / "swc" / "body" / "SpeedCtrlSWC.yaml",
        {
            "name": "SpeedCtrlSWC",
            "type": "application",
            "ports": [],
            "runnables": [{"name": "SpeedCtrl_Run", "events": events}],
            "handle_termination_and_restart": "no_support",
        },
    )
    output = model_root / "dist" / output_name
    generate_arxml(str(model_root), str(output))
    return output.read_text(encoding="utf-8")


_FAST = {"type": "timing", "period": 0.01}
_SLOW = {"type": "timing", "period": 0.02}


def test_evidence_reordering_two_differently_periodded_events_changes_the_arxml(tmp_path):
    """``Runnable.events``' order is semantic: event names are positional, so a swap swaps the periods."""
    forward = _generate_with_events(tmp_path, [_FAST, _SLOW], "forward.arxml")
    backward = _generate_with_events(tmp_path, [_SLOW, _FAST], "backward.arxml")
    assert forward != backward

    def periods(text: str) -> list[str]:
        return [line.strip() for line in text.splitlines() if "<PERIOD>" in line or "TMT_" in line]

    assert periods(forward) == [
        "<SHORT-NAME>TMT_SpeedCtrl_Run_0</SHORT-NAME>",
        "<PERIOD>0.01</PERIOD>",
        "<SHORT-NAME>TMT_SpeedCtrl_Run_1</SHORT-NAME>",
        "<PERIOD>0.02</PERIOD>",
    ]
    assert periods(backward) == [
        "<SHORT-NAME>TMT_SpeedCtrl_Run_0</SHORT-NAME>",
        "<PERIOD>0.02</PERIOD>",
        "<SHORT-NAME>TMT_SpeedCtrl_Run_1</SHORT-NAME>",
        "<PERIOD>0.01</PERIOD>",
    ]


# `_reordered`: a pure function over key sequences.


def test_two_swapped_keys_are_a_reorder():
    """The positive control for the tests below."""
    assert _reordered(["a", "b"], ["b", "a"]) is True


def test_an_unchanged_key_sequence_is_not_a_reorder():
    assert _reordered(["a", "b", "c"], ["a", "b", "c"]) is False


def test_an_insertion_at_the_front_is_not_a_reorder():
    """Only the keys present on both sides take part."""
    assert _reordered(["a", "b"], ["new", "a", "b"]) is False


def test_a_removal_is_not_a_reorder():
    assert _reordered(["a", "b", "c"], ["a", "c"]) is False


def test_an_addition_and_a_removal_together_are_not_a_reorder():
    assert _reordered(["a", "b", "c"], ["a", "c", "new"]) is False


def test_a_swap_is_still_reported_when_an_item_is_added_beside_it():
    assert _reordered(["a", "b"], ["b", "new", "a"]) is True


def test_swapping_two_identical_keys_is_not_a_reorder():
    """Two identical events swapped are equal key sequences: nothing observable to report."""
    key = ("timing", 0.01)
    assert _reordered([key, key], [key, key]) is False


def test_losing_one_of_two_identical_keys_is_not_a_reorder():
    """A key surviving fewer times is a removal; only copies on both sides are compared."""
    key = ("timing", 0.01)
    assert _reordered([key, key, ("init",)], [key, ("init",)]) is False


def test_losing_a_duplicate_from_around_another_key_is_not_a_reorder():
    """Which copies survive is what explains the two sequences, not position.

    Guards keeping each key's first occurrences, which invented a reorder for a
    plain removal.
    """
    timing = ("timing", 0.01)
    init = ("init",)

    assert _reordered([timing, init, timing], [init, timing]) is False
    assert _reordered([init, timing], [timing, init, timing]) is False


def test_a_swap_beside_a_duplicate_is_still_a_reorder():
    """Non-vacuity: a duplicate does not silence a real reorder."""
    timing = ("timing", 0.01)
    init = ("init",)

    assert _reordered([timing, init, timing], [timing, timing, init]) is True


def test_a_swap_between_differently_keyed_events_is_a_reorder():
    """The ARXML-observable case: ``TMT_X_0`` and ``TMT_X_1`` swap periods."""
    fast = ("timing", 0.01)
    slow = ("timing", 0.02)
    assert _reordered([fast, slow], [slow, fast]) is True


def test_composite_keys_are_compared_as_whole_keys():
    """A tuple key is compared whole, not by its parts."""
    first = ("SpeedCtrlSWC", "SpeedCtrl_Run")
    second = ("SpeedCtrlSWC", "TorqueCalc_Run")
    assert _reordered([first, second], [second, first]) is True
    assert _reordered([first, second], [first, second]) is False


@pytest.mark.parametrize(
    ("old_keys", "new_keys"),
    [
        pytest.param([], [], id="both-empty"),
        pytest.param(["a"], [], id="emptied"),
        pytest.param([], ["a"], id="filled"),
        pytest.param(["a"], ["a"], id="single-key"),
        pytest.param(["a"], ["b"], id="single-key-replaced"),
        pytest.param(["a", "b"], ["c", "d"], id="nothing-in-common"),
    ],
)
def test_degenerate_key_sequences_are_never_a_reorder(old_keys, new_keys):
    assert _reordered(old_keys, new_keys) is False


# The same identity in the address vocabulary: `CollectionRule.member_identity` names
# a member for the editing protocol's `{member: ...}` step, pinned against `key`.

#: (owner, field, one dumped item, the identity an address step carries).
MEMBER_IDENTITY_ROWS = [
    pytest.param(
        SWComponent, "ports", _dump(Port(name="pSpeed", direction="provide", interface="ISpeed")), "pSpeed", id="ports"
    ),
    # Addressed by the application type it maps.
    pytest.param(
        MappingSet,
        "maps",
        _dump(DataTypeMap(application="Kph", implementation="Speed")),
        "Kph",
        id="maps",
    ),
    pytest.param(SWComponent, "data_type_mappings", "Physical", "Physical", id="data_type_mappings"),
    pytest.param(ExternalInterface, "operations", "Get", "Get", id="stub-operations"),
    pytest.param(ExternalInterface, "data_elements", "speed", "speed", id="stub-data_elements"),
    pytest.param(
        ImplementationRecordDataType,
        "elements",
        _dump(ImplementationElement(name="speed", type="uint32_impl")),
        "speed",
        id="elements",
    ),
    pytest.param(TextTable, "entries", _dump(TableEntry(name="Idle", value=0)), "Idle", id="entries"),
    pytest.param(SWComponent, "runnables", _dump(Runnable(name="RunEngine")), "RunEngine", id="swc-runnables"),
    pytest.param(SWComponent, "memory", "PoolA", "PoolA", id="memory"),
    pytest.param(Port, "measures", "speed", "speed", id="measures"),
    pytest.param(
        SRInterface,
        "data_elements",
        _dump(DataElement(name="speed", implementation_type="uint32")),
        "speed",
        id="data_elements",
    ),
    pytest.param(CSInterface, "operations", _dump(CSOperation(name="ReadDtc")), "ReadDtc", id="operations"),
    pytest.param(
        CSOperation,
        "arguments",
        _dump(CSArgument(name="dtc", direction="out", implementation_type="uint32")),
        "dtc",
        id="arguments",
    ),
    pytest.param(EnumDataType, "literals", _dump(EnumLiteral(name="Idle", value=0)), "Idle", id="literals"),
    pytest.param(RecordDataType, "fields", _dump(RecordField(name="speed", type="uint32")), "speed", id="fields"),
    pytest.param(
        Task,
        "runnables",
        _dump(TaskRunnableRef(swc="EngineController", runnable="RunEngine")),
        {"swc": "EngineController", "instance": None, "runnable": "RunEngine"},
        id="task-runnables",
    ),
    pytest.param(
        Sequence,
        "steps",
        _dump(SequenceStep(swc="EngineController", runnable="RunEngine", note="first")),
        {"swc": "EngineController", "instance": None, "runnable": "RunEngine"},
        id="steps",
    ),
    # The dumped value, defaults included; the delta projection publishes it
    # reduced (`editing.address.reduce_whole_value`).
    pytest.param(
        Runnable,
        "events",
        _dump(TimingEvent(type="timing", period=0.01)),
        {"type": "timing", "period": 0.01, "disabled_modes": [], "name": None},
        id="events",
    ),
    pytest.param(
        Port,
        "com_specs",
        _dump(PortComSpec(data_element="speed", enable_update=True)),
        "speed",
        id="com_specs",
    ),
    pytest.param(
        TimingEvent,
        "disabled_modes",
        _dump(DisabledMode(port="rMode", mode_group_ref="/Ifs/IMode/mode", mode_ref="/Modes/M/OFF")),
        {"port": "rMode", "mode_group_ref": "/Ifs/IMode/mode", "mode_ref": "/Modes/M/OFF"},
        id="disabled_modes",
    ),
    pytest.param(
        DataEvent,
        "disabled_modes",
        _dump(DisabledMode(port="rMode", mode_group_ref="/Ifs/IMode/mode", mode_ref="/Modes/M/OFF")),
        {"port": "rMode", "mode_group_ref": "/Ifs/IMode/mode", "mode_ref": "/Modes/M/OFF"},
        id="data_event-disabled_modes",
    ),
    # A trace link is its own identity, `<id>@<version>` whole.
    *(
        pytest.param(owner, "satisfies", "REQ-1@2", "REQ-1@2", id=f"{owner.__name__}-satisfies")
        for owner in (
            SWComponent,
            Port,
            Runnable,
            SRInterface,
            CSInterface,
            ExternalInterface,
            Composition,
            Task,
            Processor,
            Core,
            Memory,
            Partition,
        )
    ),
    pytest.param(Requirement, "derived_from", "hardware:Core0", "hardware:Core0", id="derived_from"),
    pytest.param(Memory, "accessible", "CORE0", "CORE0", id="accessible"),
    # `by` on a read is a form, not an identity.
    pytest.param(
        Runnable,
        "reads",
        _dump(DataReadRef(port="rSpeed", element="value", by="value")),
        {"port": "rSpeed", "element": "value"},
        id="reads",
    ),
    pytest.param(
        Runnable,
        "writes",
        _dump(DataWriteRef(port="pTorque", element="value")),
        {"port": "pTorque", "element": "value"},
        id="writes",
    ),
    pytest.param(
        Runnable,
        "calls",
        _dump(OperationCallRef(port="rDiag", operation="ReadDtc")),
        {"port": "rDiag", "operation": "ReadDtc"},
        id="calls",
    ),
    # A variable access entry's stated `name` is no identity.
    pytest.param(
        SWComponent,
        "variables",
        _dump(Variable(name="FilteredSpeed", access="implicit", implementation_type="uint16", init=0)),
        "FilteredSpeed",
        id="variables",
    ),
    pytest.param(
        Runnable,
        "reads_variables",
        _dump(VariableReadRef(variable="FilteredSpeed", name="RV_Filtered")),
        "FilteredSpeed",
        id="reads_variables",
    ),
    pytest.param(
        Runnable,
        "writes_variables",
        _dump(VariableWriteRef(variable="FilteredSpeed")),
        "FilteredSpeed",
        id="writes_variables",
    ),
    pytest.param(
        SWComponent,
        "exclusive_areas",
        _dump(ExclusiveArea(name="SpeedState", description="Guards the speed.")),
        "SpeedState",
        id="exclusive_areas",
    ),
    pytest.param(Runnable, "can_enter", _dump(CanEnterArea(area="OffsetTable")), "OffsetTable", id="can_enter"),
    pytest.param(Runnable, "runs_inside", _dump(RunsInsideArea(area="SpeedState")), "SpeedState", id="runs_inside"),
    pytest.param(
        Composition,
        "ports",
        _dump(CompositionPort(name="pSpeed", direction="provide", interface="ISpeed")),
        "pSpeed",
        id="composition-ports",
    ),
    pytest.param(
        Composition,
        "members",
        _dump(CompositionMember(name="sensor", type="SpeedSensor")),
        "sensor",
        id="members",
    ),
    pytest.param(
        Composition,
        "delegations",
        _dump(Delegation(name="dSpeed", port="pSpeed", member="sensor", member_port="pOut")),
        "dSpeed",
        id="delegations",
    ),
]


def _probed_identity_fields(rule, item: Any) -> tuple[str, ...]:
    """The fields ``rule.key`` really reads: those whose replacement by a sentinel changes the key."""
    probe = object()
    identity = rule.key(item)
    return tuple(name for name in item if rule.key({**item, name: probe}) != identity)


def test_the_address_rows_cover_every_collection_in_the_vocabulary():
    covered = {
        (owner, field_name) for owner, field_name, _item, _identity in (row.values for row in MEMBER_IDENTITY_ROWS)
    }
    assert covered == set(COLLECTION_IDENTITY)


@pytest.mark.parametrize(("owner", "field_name", "item", "identity"), MEMBER_IDENTITY_ROWS)
def test_a_member_reaches_an_address_in_the_form_its_strategy_publishes(owner, field_name, item, identity):
    """A string, an object or the member's own value: the three forms ``$defs/pathStep``'s ``member`` admits."""
    assert collection_rule(owner, field_name).member_identity(item) == identity


@pytest.mark.parametrize(("owner", "field_name", "item", "identity"), MEMBER_IDENTITY_ROWS)
def test_the_declared_strategy_is_what_the_key_function_actually_reads(owner, field_name, item, identity):
    """The declared identity fields are exactly the ones the key moves with.

    The strategy is declared, not derived: the probe cannot tell ``whole_value``
    from a ``composite`` that covers every field.
    """
    rule = collection_rule(owner, field_name)
    if not isinstance(item, dict):
        # The item is its key: no field to probe.
        assert rule.strategy == "whole_value"
        assert rule.member_identity(item) is item
        return

    read = _probed_identity_fields(rule, item)
    if rule.strategy == "whole_value":
        assert set(read) == set(item), "a whole_value key must move with every part of its member"
        assert rule.identity_fields == ()
    else:
        assert set(read) == set(rule.identity_fields)
        assert len(rule.identity_fields) == (1 if rule.strategy == "field" else len(identity))


@pytest.mark.parametrize(("owner", "field_name", "item", "identity"), MEMBER_IDENTITY_ROWS)
def test_the_address_identity_and_the_comparison_key_name_the_same_member(owner, field_name, item, identity):
    """Whatever changes the address identity changes the comparison key, and only that."""
    rule = collection_rule(owner, field_name)
    if not isinstance(item, dict):
        return

    for name in item:
        moved = {**item, name: f"{item[name]}-moved" if isinstance(item[name], str) else 99}
        assert (rule.key(moved) == rule.key(item)) == (rule.member_identity(moved) == rule.member_identity(item))


def test_a_composite_identity_is_an_object_and_not_the_comparison_tuple():
    """An address names a composite's parts; the comparison key is a positional tuple."""
    rule = collection_rule(Task, "runnables")
    item = _dump(TaskRunnableRef(swc="EngineController", runnable="RunEngine"))

    assert rule.key(item) == ("EngineController", None, "RunEngine")
    assert rule.member_identity(item) == {"swc": "EngineController", "instance": None, "runnable": "RunEngine"}


def test_a_name_keyed_identity_ignores_everything_but_the_name():
    """An address names a changed member, not one that no longer exists."""
    rule = collection_rule(SWComponent, "ports")
    before = _dump(Port(name="pSpeed", direction="provide", interface="ISpeed"))
    after = _dump(Port(name="pSpeed", direction="require", interface="IOther"))

    assert rule.member_identity(before) == rule.member_identity(after) == "pSpeed"


def test_a_whole_value_member_is_addressed_by_the_dumped_value_itself():
    """A whole-value member has no addressable interior: a changed part is a different member."""
    rule = collection_rule(Runnable, "events")
    slow = _dump(TimingEvent(type="timing", period=0.02))
    fast = _dump(TimingEvent(type="timing", period=0.01))

    assert rule.member_identity(slow) == slow
    assert rule.member_identity(slow) != rule.member_identity(fast)
