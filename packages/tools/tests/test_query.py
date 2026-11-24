"""The model query engine: two maps, two walks, two orders.

``swcraft_tools.query`` resolves the declared topology; ``impact.py`` renders over it. This suite
holds that:

- the **reverse** direction is the map ``impact`` publishes, and the proof is byte identity of
  ``format_impact`` on names the frozen oracle in ``fixtures/recorded_oracles/impact_edges.json`` does
  **not** carry — ``Speed`` and ``float32``, whose dependent sets span more than one layer *and* run
  through the diamond ``Speed -> {SpeedSensor, SpeedControl} -> Task_10ms``. The oracle's own names
  (``SpeedSensor``, ``Shared``, ``RAM0``, ``CORE0``) traverse no diamond, so the texts here are what
  pins the walk on it;
- the **forward** direction is the same edges read the other way rather than a second traversal;
- the two walks are pinned **separately**: from ``Speed``, the transitive walk reaches ``Task_10ms``
  through ``SpeedSensor`` and the bounded walk through ``SpeedControl``, so the two answers differ in
  a rendered *detail* and not only in order. A rewrite of one walk into the other cannot pass both.

**What this suite does not reach.** A baseline of ``swcraft impact`` on real tool exports: nothing
in this checkout can assert one. Which fields the contract *declares* an edge on — that is
``packages/tools/tests/test_model_relations.py``'s both-ways gate over ``_EDGE_DETAILS``, and a field
the contract declares no edge on is invisible to both suites alike. The identity-key uniqueness
refusal, which is ``differ.build_key_map``'s and is covered by ``test_impact.py``. And it pins the
bounded walk's order only as *this* graph walked from *this* start: neither walk promises a minimal
hop count, so no test here asserts one.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
import json
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import yaml
from conftest import SAFETY_MODEL_ROOT, _write_manifest, write_model_element
from impact_record_support import restore_requirement_rows
from swcraft_tools import impact, query
from swcraft_tools.differ import collection_rule
from swcraft_tools.editing import address
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.protocol import WhereClause
from swcraft_tools.editing.read import read_value
from swcraft_tools.impact import build_reference_graph, format_impact, impact_of
from swcraft_tools.model_registry import current_registry
from swcraft_tools.model_state import ModelState, load_model_state
from swcraft_tools.project import require_project
from swcraft_tools.query import (
    ABSENT,
    Clause,
    Dependent,
    EdgeDetail,
    ElementRef,
    QueryError,
    QueryPage,
    Slice,
    build_graphs,
    dependencies,
    dependents,
    format_query,
    format_query_json,
    format_related,
    format_related_json,
    slice_layer,
)
from swcraft_tools.query import answer as query_answer
from swcraft_tools.query import edges as query_edges
from swcraft_tools.query import formatting as query_formatting
from swcraft_tools.query import slicing as query_slicing
from swcraft_tools.schema import (
    ApplicationDataType,
    BaseDataType,
    Composition,
    CompositionMember,
    CompositionPort,
    Connector,
    Conversion,
    Core,
    DataElement,
    DataTypeMap,
    MappingSet,
    Memory,
    Partition,
    Port,
    Processor,
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
    ValueRange,
)
from swcraft_tools.snapshot import ModelSnapshot, load_snapshot
from swcraft_tools.validator import effective_asil_levels

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "recorded_oracles"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def wide_state() -> ModelState:
    """The impact fixture, loaded through the shipped registry.

    ``Speed``'s dependents span seven layers, and the model carries a diamond, ``Speed -> ISpeed ->
    {SpeedControl, SpeedSensor}`` rejoining at ``Task_10ms``, ``Sensor_to_Control``,
    ``REQ_SPEED_001`` and ``speed_cycle``.
    """
    return load_model_state(str(FIXTURE_ROOT / "impact_model"))


@pytest.fixture(scope="module")
def wide_snapshot(wide_state: ModelState) -> ModelSnapshot:
    """The same model behind ``impact.py``'s ``ModelSnapshot`` surface, loaded once for both."""
    return ModelSnapshot.from_state(wide_state)


@pytest.fixture(scope="module")
def frozen_oracle() -> dict[str, Any]:
    """``impact_edges.json``, read and never written: the recorded oracle."""
    return json.loads((FIXTURE_ROOT / "impact_edges.json").read_text(encoding="utf-8"))


def _rows(result: dict[str, list[Dependent]]) -> dict[str, list[tuple[str, str, str]]]:
    """A result as plain tuples, so a mismatch reads as a diff and not as reprs."""
    return {layer: [(d.ref.layer, d.ref.name, d.detail) for d in deps] for layer, deps in result.items()}


def _state(**kwargs: Any) -> ModelState:
    defaults: dict[str, Any] = dict(
        compositions=[],
        swcs=[],
        interfaces=[],
        implementation_types=[],
        connectors=[],
        requirements=[],
        tasks=[],
        sequences=[],
        hardware=[],
        mapping_sets=[],
        base_types=[],
        application_types=[],
        units=[],
        compu_methods=[],
        data_constraints=[],
    )
    defaults.update(kwargs)
    return ModelSnapshot(**defaults).state


#: ``format_impact`` on the two diamond-traversing names of the wide fixture, recorded from the
#: traversal. They are literals rather than a regenerated file for the reason ``impact_edges.json``
#: is: a recorded oracle that a later run can rewrite proves nothing about the run that rewrote it.
#: An edit is made only when the model or a rendered detail moves, never the traversal: the
#: comparison is run first, and only the lines the moved model or detail explains change.
_FROZEN_DIAMOND_REPORTS: dict[str, str] = {
    "Speed": (
        "Impact of 'Speed':\n"
        "\n"
        "implementation_type:\n"
        "  interface ISpeed\n"
        "  implementation_type SpeedRef\n"
        "  mapping_set SpeedMapping\n"
        "  swc SpeedControl (via port rIn)\n"
        "  swc SpeedSensor (via port pOut)\n"
        "  composition Powertrain (via port pSpeedOut; members sensor, control)\n"
        "  connector Diag_to_Powertrain (via port Powertrain.rDiagIn)\n"
        "  connector Sensor_to_Control (via ports SpeedSensor.pOut, SpeedControl.rIn)\n"
        "  composition VehicleTop (member powertrain)\n"
        "  task Task_10ms (runnables SpeedSensor_Run, SpeedControl_Run)\n"
        "  requirement REQ_SPEED_001\n"
        "  sequence speed_cycle (steps: SpeedSensor_Run, SpeedControl_Run)"
    ),
    "float32_base": (
        "Impact of 'float32_base':\n"
        "\n"
        "base_type:\n"
        "  implementation_type Speed\n"
        "  implementation_type float32\n"
        "  interface ISpeed\n"
        "  implementation_type SpeedRef\n"
        "  mapping_set SpeedMapping\n"
        "  swc SpeedControl (via port rIn)\n"
        "  swc SpeedSensor (via port pOut)\n"
        "  composition Powertrain (via port pSpeedOut; members sensor, control)\n"
        "  connector Diag_to_Powertrain (via port Powertrain.rDiagIn)\n"
        "  connector Sensor_to_Control (via ports SpeedSensor.pOut, SpeedControl.rIn)\n"
        "  composition VehicleTop (member powertrain)\n"
        "  task Task_10ms (runnables SpeedSensor_Run, SpeedControl_Run)\n"
        "  requirement REQ_SPEED_001\n"
        "  sequence speed_cycle (steps: SpeedSensor_Run, SpeedControl_Run)"
    ),
}


# ---------------------------------------------------------------------------
# One pass, two maps
# ---------------------------------------------------------------------------


def test_the_two_maps_hold_exactly_the_same_edges(wide_state) -> None:
    """The forward map and the reverse map hold the same ``(referenced, dependent)`` pairs.

    ``build_graphs`` writes both directions from one resolved triple; a second resolution pass for
    the forward direction could differ here unnoticed, because no rendered surface prints the forward
    map.
    """
    graphs = build_graphs(wide_state)
    reverse_pairs = {(node, reached) for node, edges in graphs.reverse.items() for reached in edges}
    forward_pairs = {(reached, node) for node, edges in graphs.forward.items() for reached in edges}
    assert reverse_pairs == forward_pairs
    assert reverse_pairs, "the fixture resolved no edges at all"


def test_the_two_maps_agree_on_every_edges_detail(wide_state) -> None:
    """The detail names the EDGE, not the end it is read from: ``via port pOut`` both ways."""
    graphs = build_graphs(wide_state)
    # Non-vacuity here rather than only in the neighbouring test: a map that
    # came back empty would satisfy every comparison below by making none.
    assert graphs.reverse and any(detail for edges in graphs.reverse.values() for detail in edges.values())
    for node, edges in graphs.reverse.items():
        for reached, detail in edges.items():
            assert graphs.forward[reached][node] == detail, (node, reached)


def test_build_reference_graph_is_the_reverse_map(wide_state, wide_snapshot, frozen_oracle) -> None:
    """The published name and the engine's map are one object, not two builds."""
    graphs = build_graphs(wide_state)
    assert build_reference_graph(wide_snapshot) == graphs.reverse
    assert [
        {
            "layer": node.layer,
            "name": node.name,
            "dependents": [
                {
                    "layer": r.layer,
                    "name": r.name,
                    "detail": query_edges._render_details(d),
                    "details": [query_formatting._detail_json(detail) for detail in d],
                }
                for r, d in edges.items()
            ],
        }
        for node, edges in graphs.reverse.items()
    ] == _relabel_base_types(
        frozen_oracle["graph"],
        {element.name for element in wide_state["base_types"]},
        {element.name for element in wide_state["application_types"]},
    )


def _relabel_base_types(value: Any, base_names: set[str], application_names: set[str]) -> Any:
    """The frozen oracle with each ``data_type`` row under its layer's token.

    A base type becomes ``base_type``, an application type ``application_type`` and every other such
    row ``implementation_type``; every edge, detail and position is still the recorded one.
    """
    if isinstance(value, list):
        return [_relabel_base_types(item, base_names, application_names) for item in value]
    if isinstance(value, dict):
        relabelled = {key: _relabel_base_types(item, base_names, application_names) for key, item in value.items()}
        if relabelled.get("layer") == "data_type" and relabelled.get("name") in base_names:
            relabelled["layer"] = "base_type"
        if relabelled.get("layer") == "data_type" and relabelled.get("name") in application_names:
            relabelled["layer"] = "application_type"
        if relabelled.get("layer") == "data_type":
            relabelled["layer"] = "implementation_type"
        return relabelled
    return value


def test_an_unknown_direction_is_refused_and_names_both(wide_state) -> None:
    """A closed two-value vocabulary refuses rather than defaulting."""
    graphs = build_graphs(wide_state)
    with pytest.raises(ValueError) as excinfo:
        graphs.direction("dependants")
    message = str(excinfo.value)
    assert "dependants" in message
    assert "dependents" in message and "dependencies" in message


# ---------------------------------------------------------------------------
# The reverse direction: unchanged, and byte-identical where it is rendered
# ---------------------------------------------------------------------------


def test_dependents_reproduces_the_recorded_pre_restructure_rows(wide_state, frozen_oracle) -> None:
    """``dependents`` answers the rows ``impact_edges.json`` recorded, row for row.

    Compared against the **recorded values**, not a live ``impact_of`` call: ``impact_of`` delegates
    to ``query.related`` over the same ``ModelState``, so that comparison would be one call written
    twice; the oracle file, read and never written, is the only thing here that can disagree. What
    it does **not** reach: names other than the oracle's four; the names that traverse the
    diamond are pinned by ``format_impact``'s bytes below.
    """
    for name, expected in frozen_oracle["impact_of"].items():
        recorded = {
            layer: [(row["layer"], row["name"], row["detail"]) for row in rows] for layer, rows in expected.items()
        }
        assert _rows(dependents(wide_state, name)) == recorded, name


def test_impact_of_is_dependents_at_the_default_depth(wide_state, wide_snapshot, frozen_oracle) -> None:
    """The adapter's surface answers what the engine answers.

    Deliberately weaker than the test above: both sides read the same ``ModelState``, so the claim is
    only that the two *surfaces* agree.
    """
    for name in frozen_oracle["impact_of"]:
        assert _rows(dependents(wide_state, name)) == _rows(impact_of(wide_snapshot, name)), name


@pytest.mark.parametrize("name", sorted(_FROZEN_DIAMOND_REPORTS))
def test_format_impact_is_byte_identical_through_the_diamond(wide_snapshot, name) -> None:
    """The widened byte oracle: the names the frozen fixture file does not carry.

    Both walk the diamond, and ``float32_base`` walks it one hop further out through the ``base`` of
    the value type ``Speed``. A reordering of the transitive walk changes these bytes.
    """
    # The `requirement REQ_SPEED_001` row left both reports when the link moved into the element;
    # the record keeps it, so it is restored where it stood and its absence asserted.
    result = restore_requirement_rows(impact_of(wide_snapshot, name), _IMPACT_MODEL_ROOT, name)
    assert format_impact(name, result) == _FROZEN_DIAMOND_REPORTS[name]


def test_the_transitive_walk_marks_visited_at_push(wide_state) -> None:
    """One entry per reached element, carrying the details of every edge that reached it.

    ``Task_10ms`` is reachable from ``Speed`` through both ``SpeedSensor`` and ``SpeedControl``;
    visited-at-push is why it appears once, and the LIFO order is why the sensor's detail comes first.
    The control component's edge, met second, adds its detail to the row.
    """
    rows = _rows(dependents(wide_state, "Speed"))["implementation_type"]
    tasks = [row for row in rows if row[0] == "task"]
    assert tasks == [("task", "Task_10ms", "runnables SpeedSensor_Run, SpeedControl_Run")]


# ---------------------------------------------------------------------------
# The physical view, and the layer that pairs it
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def physics_state() -> ModelState:
    """The smallest model that carries the three mapping edges at once.

    Hand-built rather than added to ``impact_model/``: a mapping set the ``Speed`` walk reaches would
    move the hand-written level-by-level order pins out of ``Speed``. The shape is one signal, twice:
    ``Speed_Kph`` is the physical view, ``float32`` the implementation type that stores it,
    ``PhysicalMapping`` the pair, and ``SpeedSensor`` both names the set and carries the port whose
    interface types an element by the physical view.
    """
    return ModelSnapshot(
        compositions=[],
        swcs=[
            SWComponent(
                name="SpeedSensor",
                type="application",
                ports=[Port(name="pOut", direction="provide", interface="ISpeed")],
                data_type_mappings=["PhysicalMapping"],
            )
        ],
        interfaces=[
            SRInterface(
                name="ISpeed",
                kind="sender_receiver",
                data_elements=[DataElement(name="value", application_type="Speed_Kph")],
            )
        ],
        implementation_types=[
            ValueDataType(name="float32", kind="value_type", base="float32_base"),
        ],
        connectors=[],
        requirements=[],
        tasks=[],
        sequences=[],
        hardware=[],
        mapping_sets=[
            MappingSet(
                name="PhysicalMapping",
                maps=[DataTypeMap(application="Speed_Kph", implementation="float32")],
            )
        ],
        base_types=[
            BaseDataType(
                name="float32_base", kind="base_type", size=32, encoding="IEEE754", native_declaration="float"
            ),
        ],
        application_types=[
            ApplicationDataType(
                name="Speed_Kph",
                kind="application_type",
                unit="kph",
                conversion=Conversion(factor=0.1, offset=0.0),
                range=ValueRange(min=0.0, max=300.0),
            ),
        ],
        # The three layers of the forms a type names, empty.
        units=[],
        compu_methods=[],
        data_constraints=[],
    ).state


class TestTheWalkReachesThroughTheMappingLayer:
    """The three mapping edges, walked rather than only declared.

    What this class does **not** reach: the ``conversion``, the ``unit`` and the ``range`` of an
    application type are fields of one element and draw no edge, so no walk reports them — they reach
    a reader through ``read``, ``query``'s layer slice and ``diff``, and the address each takes is
    pinned in ``test_editing_delta.py``.
    """

    def test_impact_of_the_physical_view_names_the_set_the_component_and_the_port(self, physics_state) -> None:
        """The physical view's report, as the bytes the CLI prints.

        Three edges in one report: ``field:interfaces.data_element.application_type`` reaches
        the interface, ``field:mapping_sets.map.application`` the set, and
        ``field:swc.port.interface`` the component — which
        ``field:swc.swc.data_type_mappings`` reaches a second time, adding no
        detail, so the row keeps the port it was first reached with.
        """
        snapshot = ModelSnapshot.from_state(physics_state)
        assert format_impact("Speed_Kph", impact_of(snapshot, "Speed_Kph")) == (
            "Impact of 'Speed_Kph':\n"
            "\n"
            "application_type:\n"
            "  interface ISpeed\n"
            "  mapping_set PhysicalMapping\n"
            "  swc SpeedSensor (via port pOut)"
        )

    def test_the_implementation_side_reaches_the_component_through_the_set_alone(self, physics_state) -> None:
        """The discriminating case: ``float32`` names no interface.

        It is reached by ``field:mapping_sets.map.implementation`` and by nothing
        else, so the component's row carries **no** detail — which is what says
        the port detail above came from the port edge and not from the set.
        """
        snapshot = ModelSnapshot.from_state(physics_state)
        assert format_impact("float32", impact_of(snapshot, "float32")) == (
            "Impact of 'float32':\n\nimplementation_type:\n  mapping_set PhysicalMapping\n  swc SpeedSensor"
        )

    def test_the_set_itself_names_every_component_that_maps_with_it(self, physics_state) -> None:
        assert _rows(dependents(physics_state, "PhysicalMapping")) == {"mapping_set": [("swc", "SpeedSensor", "")]}

    def test_the_component_depends_on_the_set_and_on_both_sides_of_its_pairs(self, physics_state) -> None:
        """The forward direction: a component's closure now carries the physics.

        ``Speed_Kph`` is reached twice over — through the interface its port names
        and through the set the component maps with — and appears once.
        """
        reached = {(row[0], row[1]) for row in _rows(dependencies(physics_state, "SpeedSensor"))["swc"]}
        assert {
            ("mapping_set", "PhysicalMapping"),
            ("application_type", "Speed_Kph"),
            ("implementation_type", "float32"),
        } <= reached

    def test_the_walk_document_says_what_the_report_says(self, physics_state) -> None:
        """``query --dependents --format json`` over the new layer says what the report says.

        Row for row and in order, the rendered ``detail`` and the structured ``details`` agreeing: the
        check ``TestTheRecordedReportsMoveOnlyWhereTheLogSays`` runs over the recorded models.
        """
        document = json.loads(
            format_related_json("Speed_Kph", query.related(physics_state, "Speed_Kph", "dependents", None))
        )
        assert document["schemaVersion"] == 3
        (rows,) = document["matched"].values()
        snapshot = ModelSnapshot.from_state(physics_state)
        lines = format_impact("Speed_Kph", impact_of(snapshot, "Speed_Kph")).split("\n")[3:]
        assert [_report_line(row) for row in rows] == lines


# ---------------------------------------------------------------------------
# The forward direction
# ---------------------------------------------------------------------------


def test_the_forward_direction_walks_port_interface(wide_state) -> None:
    """The forward case: an SWC depends on the interfaces its ports name."""
    rows = _rows(dependencies(wide_state, "SpeedControl", depth=1))["swc"]
    assert rows[:2] == [
        ("interface", "ISpeed", "via port rIn"),
        ("interface", "IDiagnostics", "via port rDiag"),
    ]


def test_the_forward_direction_is_transitive(wide_state) -> None:
    """``SpeedControl -> ISpeed -> Speed -> float32_base``, in one unbounded call."""
    reached = {(row[0], row[1]) for row in _rows(dependencies(wide_state, "SpeedControl"))["swc"]}
    assert ("interface", "ISpeed") in reached
    assert ("implementation_type", "Speed") in reached
    assert ("base_type", "float32_base") in reached
    # And nothing that only DEPENDS on SpeedControl comes back.
    assert ("task", "Task_10ms") not in reached
    assert ("connector", "Sensor_to_Control") not in reached


def test_the_forward_direction_keeps_a_dangling_reference_visible(wide_state) -> None:
    """``ScratchPad`` is a memory budget key no memory answers to, and it is a node.

    Unchanged policy, asserted in the new direction: the traversal reports what
    the element *says*; the finding belongs to ``check_hardware_topology``.
    """
    rows = _rows(dependencies(wide_state, "SpeedSensor", depth=1))["swc"]
    assert ("hardware", "ScratchPad", "memory budget 512 bytes") in rows


def test_the_two_directions_are_not_each_others_answer(wide_state) -> None:
    """The mirror is of the EDGES, not of the result sets."""
    forward = _rows(dependencies(wide_state, "Task_10ms"))["task"]
    reverse = _rows(dependents(wide_state, "Task_10ms"))["task"]
    assert ("swc", "SpeedSensor", "runnable SpeedSensor_Run") in forward
    assert ("sequence", "speed_cycle", "") in reverse
    assert {row[1] for row in forward} & {row[1] for row in reverse} == set()


# ---------------------------------------------------------------------------
# The two walks and their two orders
# ---------------------------------------------------------------------------


def test_the_bounded_walk_is_breadth_first_level_order(wide_state) -> None:
    """``depth`` 1, 2 and 3 out from ``Speed``, pinned level by level.

    Each level extends the previous one — a bounded walk that reordered what it
    had already reported would fail the prefix assertions — and within a level
    the order is the frontier's, each node's edges in insertion order.
    """
    # The two composites name the PHYSICAL view (`SpeedPhys`), not `Speed`, so `Speed`'s one-hop
    # neighbourhood holds the mapping set that pairs the two. A mapping set's edge renders no detail:
    # a map is a composite member naming a type, which renders none.
    level_1 = [
        ("interface", "ISpeed", ""),
        ("implementation_type", "SpeedRef", ""),
        ("mapping_set", "SpeedMapping", ""),
    ]
    level_2 = level_1 + [
        ("swc", "SpeedControl", "via port rIn"),
        ("swc", "SpeedSensor", "via port pOut"),
        # A composition's boundary port names an interface exactly as a component's does, so
        # `Powertrain` is one hop from `ISpeed`.
        ("composition", "Powertrain", "via port pSpeedOut"),
    ]
    level_3_rows = [
        ("task", "Task_10ms", "runnables SpeedControl_Run, SpeedSensor_Run"),
        # A connector names, owner first, the endpoint of every level-2 component that reaches it.
        ("connector", "Sensor_to_Control", "via ports SpeedControl.rIn, SpeedSensor.pOut"),
        # No requirement row: the components name the requirement they satisfy, so it is what they
        # depend on, not their dependent.
        ("sequence", "speed_cycle", "steps: SpeedControl_Run, SpeedSensor_Run"),
        ("connector", "Diag_to_Powertrain", "via port Powertrain.rDiagIn"),
        ("composition", "VehicleTop", "member powertrain"),
    ]
    # Level 2's rows are not re-ordered by level 3 — but a row can GAIN details
    # from an edge between two elements of level 2: `Powertrain` places both
    # components, which a depth-3 walk expands and a depth-2 walk does not.
    level_3 = (
        level_2[:-1] + [("composition", "Powertrain", "via port pSpeedOut; members control, sensor")] + level_3_rows
    )
    assert _rows(dependents(wide_state, "Speed", depth=1))["implementation_type"] == level_1
    assert _rows(dependents(wide_state, "Speed", depth=2))["implementation_type"] == level_2
    assert _rows(dependents(wide_state, "Speed", depth=3))["implementation_type"] == level_3


def test_the_two_walks_differ_in_order_and_in_detail(wide_state) -> None:
    """The discriminating case: ``Task_10ms`` is three hops from ``Speed`` along two paths.

    The transitive walk reaches it through ``SpeedSensor`` first and the bounded walk through
    ``SpeedControl`` first; both rows carry both runnables in the order each walk met them, so serving
    ``depth: null`` with the bounded walk fails on a rendered string, not only on the sequence.
    """
    transitive = _rows(dependents(wide_state, "Speed"))["implementation_type"]
    bounded = _rows(dependents(wide_state, "Speed", depth=9))["implementation_type"]
    assert {row[:2] for row in transitive} == {row[:2] for row in bounded}
    assert transitive != bounded
    assert ("task", "Task_10ms", "runnables SpeedSensor_Run, SpeedControl_Run") in transitive
    assert ("task", "Task_10ms", "runnables SpeedControl_Run, SpeedSensor_Run") in bounded


def test_a_depth_beyond_the_graph_reaches_the_same_set_as_the_unbounded_walk(wide_state) -> None:
    """The bounded walk saturates; only the order and the details differ."""
    saturated = {row[:2] for row in _rows(dependents(wide_state, "Speed", depth=99))["implementation_type"]}
    unbounded = {row[:2] for row in _rows(dependents(wide_state, "Speed"))["implementation_type"]}
    assert saturated == unbounded


@pytest.mark.parametrize("depth", [0, -1])
@pytest.mark.parametrize("walk", [dependents, dependencies])
def test_a_depth_below_one_is_refused_in_both_directions(wide_state, walk, depth) -> None:
    """``depth: 0`` is not "no walk"; it is a question the vocabulary does not have.

    ``None`` is the transitive walk and ``1`` is the first level, so ``0`` can only be a caller's
    mistake, refused with the accepted values named.
    """
    with pytest.raises(ValueError) as excinfo:
        walk(wide_state, "Speed", depth=depth)
    assert str(depth) in str(excinfo.value)


@pytest.mark.parametrize("depth", [0, -1])
@pytest.mark.parametrize("walk", [dependents, dependencies])
def test_a_depth_below_one_is_refused_even_when_the_name_matches_nothing(wide_state, walk, depth) -> None:
    """An invalid argument is a property of the REQUEST, not of the model.

    Guards a defect where the check ran once per matched element, so a name no layer carries
    validated nothing and answered "not found" for a depth no walk could have run.
    """
    with pytest.raises(ValueError) as excinfo:
        walk(wide_state, "NoSuchElementAnywhere", depth=depth)
    assert "depth must be at least 1" in str(excinfo.value)
    assert str(depth) in str(excinfo.value)


@pytest.mark.parametrize("depth", [True, False, 1.0, 2.5, "1"])
@pytest.mark.parametrize("walk", [dependents, dependencies])
def test_a_depth_that_is_not_a_whole_number_is_refused_rather_than_walked(wide_state, walk, depth) -> None:
    """``depth < 1`` is not the whole guard: a bool, a float and a string are refused before any match.

    ``True`` is an ``int`` of value 1 and would walk one hop the caller never asked for; ``1.0`` and
    ``2.5`` would escape ``range()`` as a ``TypeError``; ``"1"`` fails the comparison itself.
    """
    with pytest.raises(ValueError, match="whole number or None"):
        walk(wide_state, "Speed", depth=depth)


@pytest.mark.parametrize("walk", [dependents, dependencies])
def test_a_name_that_matches_nothing_is_an_empty_answer_at_a_legal_depth(wide_state, walk) -> None:
    """The other half: refusing an invalid depth did not turn a miss into an error."""
    assert walk(wide_state, "NoSuchElementAnywhere") == {}
    assert walk(wide_state, "NoSuchElementAnywhere", depth=1) == {}


# ---------------------------------------------------------------------------
# The awkward shapes: cycles, composite identities, ambiguous names, empty layers
# ---------------------------------------------------------------------------


def test_a_cycle_terminates_and_reports_each_element_once() -> None:
    """Two records naming each other's type: the walk is cycle-safe both ways.

    The start element is in the visited set before the first pop, so it never
    reports itself — which is what a two-node cycle would otherwise produce.
    """
    a = RecordDataType(name="RecA", kind="record", fields=[RecordField(name="b", type="RecB")])
    b = RecordDataType(name="RecB", kind="record", fields=[RecordField(name="a", type="RecA")])
    state = _state(application_types=[a, b])

    assert _rows(dependents(state, "RecA")) == {"application_type": [("application_type", "RecB", "")]}
    assert _rows(dependencies(state, "RecA")) == {"application_type": [("application_type", "RecB", "")]}
    assert _rows(dependents(state, "RecA", depth=5)) == {"application_type": [("application_type", "RecB", "")]}


def test_the_member_walk_terminates_on_a_CYCLIC_member_of_graph() -> None:
    """``_members``' guard, at the cycle it is written for — a **kind** cycle, not a data one.

    Two kinds each declaring itself a member of the other: without ``seen`` the walk recurses into a
    ``RecursionError`` rather than a statement about the contract. The shipped ``member_of`` graph is
    acyclic, so the registry is stubbed rather than loaded.
    """

    class _Facts:
        def __init__(self, kind: str, member_of: tuple[tuple[str, str], ...]) -> None:
            self.kind = kind
            self.member_of = member_of

    class _CyclicRegistry:
        kinds = {"left": _Facts("left", (("right", "f"),)), "right": _Facts("right", (("left", "f"),))}

        def kind_of_model(self, _model: type) -> None:
            return None

    registry = _CyclicRegistry()
    owner = _Facts("owner", ())
    # Terminates, and answers nothing: no walk over this graph reaches an owner
    # of either kind, which is the honest answer for a contract that declares a
    # kind as a member of itself.
    assert list(query_edges._members(registry, object(), owner, "left")) == []


def test_a_composite_member_identity_yields_one_edge_per_target() -> None:
    """``task.runnables`` has a composite member identity; the graph node does not.

    Two runnable references on one task naming the SAME component are two members and one edge (a
    member is never a node); the edge carries both members' details, in emission order, and renders
    the plural.
    """
    swc = SWComponent(
        name="Multi",
        type="application",
        runnables=[Runnable(name="First"), Runnable(name="Second")],
    )
    task = Task(
        name="T",
        period=0.01,
        priority=1,
        partition="PARTITION0",
        runnables=[
            TaskRunnableRef(swc="Multi", runnable="First"),
            TaskRunnableRef(swc="Multi", runnable="Second"),
        ],
    )
    state = _state(
        swcs=[swc],
        tasks=[task],
        hardware=[Processor(name="CPU", kind="processor"), Core(name="CORE0", kind="core", threshold=0.7)],
    )

    assert _rows(dependents(state, "Multi")) == {"swc": [("task", "T", "runnables First, Second")]}


def test_a_name_two_layers_carry_is_reported_as_two_groups() -> None:
    """A bare name, no ``--type`` flag: each match is walked with its own visited set."""
    requirement = Requirement(
        id="Shared", title="A requirement", description="Links nothing", version=1, source="architecture"
    )
    swc = SWComponent(name="Shared", type="application", ports=[Port(name="p", direction="require", interface="I")])
    iface = SRInterface(
        name="I", kind="sender_receiver", data_elements=[DataElement(name="v", implementation_type="uint8")]
    )
    state = _state(requirements=[requirement], swcs=[swc], interfaces=[iface])

    result = _rows(dependencies(state, "Shared"))
    assert result == {
        "requirement": [],
        "swc": [("interface", "I", "via port p"), ("implementation_type", "uint8", "")],
    }
    assert list(result) == ["requirement", "swc"], "grouped out of the registry's display order"


def test_a_matched_element_with_no_edges_contributes_an_empty_list() -> None:
    """An empty list, not an absent key — the caller can tell "none" from "no such name"."""
    state = _state(base_types=[BaseDataType(name="uint8", kind="base_type", size=8, encoding="NONE")])
    assert dependents(state, "uint8") == {"base_type": []}
    assert dependencies(state, "uint8") == {"base_type": []}
    assert dependents(state, "NoSuchElement") == {}
    assert dependencies(state, "NoSuchElement") == {}


def test_an_entirely_empty_model_resolves_to_two_empty_maps() -> None:
    """Every layer empty: the key maps still cover every declared layer.

    The expected token set is derived from the registry, so a layer the contract adds belongs here
    without an edit. That the tokens are singular is ``test_model_relations.py``'s check.
    """
    registry = current_registry()
    graphs = build_graphs(_state())
    assert graphs.reverse == {}
    assert graphs.forward == {}
    assert set(graphs.key_maps) == {registry.layer(name).token for name in registry.display_order}
    assert len(graphs.key_maps) == len(registry.display_order)
    assert all(mapping == {} for mapping in graphs.key_maps.values())


def test_an_empty_layer_beside_a_populated_one_changes_no_answer() -> None:
    """The empty-layer case in the shape that could go wrong: a layer with no files.

    ``sequences``, ``connectors``, ``compositions`` and ``requirements`` are empty
    here while ``swc`` and ``interfaces`` are not, and the answer is exactly the
    populated layers' — an empty layer contributes no node and no group.
    """
    iface = SRInterface(
        name="ISpeed", kind="sender_receiver", data_elements=[DataElement(name="v", implementation_type="uint8")]
    )
    swc = SWComponent(
        name="Sensor",
        type="application",
        ports=[Port(name="pOut", direction="provide", interface="ISpeed")],
        memory={"RAM0": 128},
    )
    memory = Memory(name="RAM0", kind="memory", type="RAM", size=1024, accessible=["CORE0"], threshold=0.8)
    state = _state(
        swcs=[swc],
        interfaces=[iface],
        hardware=[Processor(name="CPU", kind="processor"), Core(name="CORE0", kind="core", threshold=0.7), memory],
    )

    assert _rows(dependents(state, "ISpeed")) == {"interface": [("swc", "Sensor", "via port pOut")]}
    assert list(dependents(state, "ISpeed")) == ["interface"]


# ---------------------------------------------------------------------------
# The delegation itself
# ---------------------------------------------------------------------------


def test_impact_publishes_the_engines_types_and_not_copies_of_them() -> None:
    """``impact.Dependent`` **is** ``query.Dependent``.

    A re-declared dataclass of the same shape would compare unequal to the
    engine's, which is the failure mode a caller that mixes the two imports
    would hit — and the one this assertion is cheaper than debugging.
    """
    assert impact.Dependent is Dependent
    assert impact.ElementRef is ElementRef


# ---------------------------------------------------------------------------
# The set projections — the filter, the member slice and the page
# ---------------------------------------------------------------------------
#
# These restate what `editing/read.py` does for the protocol, in the kernel's vocabulary, because
# the `cli` package may not import `editing/`. So two properties are asserted that one mechanism
# would not need: the canonical equality is the protocol's
# (`test_the_canonical_equality_is_the_protocols`), and the read order is the one every form
# publishes in, not the readers' order `swcraft list` answers. The end-to-end equality of the two
# backends is `test_cli_query.py`'s.


def test_the_slice_reads_in_the_read_order_and_not_the_readers_order(wide_state) -> None:
    """The slice answers in the read order and not in the readers' order.

    ``implementation_types`` discriminates: several of its kinds are populated and the readers'
    ordinal identity sort interleaves them. Ranking by declared kind — ``value_type``, then
    ``reference_type`` and on — is what ``swcraft query`` and every form of the editing protocol answer
    in, and what ``swcraft list`` deliberately does **not** do.
    """
    # Base and application types are other layers', asserted after this one.
    assert slice_layer(wide_state, "implementation_types").identities == (
        "ProbeGearValue",
        "Speed",
        "float32",
        "uint16",
        "uint8",  # the value types — `Probe…` and `Speed` first, because 'P' < 'S' < 'f' ordinally
        "ProbeGearRef",
        "SpeedRef",  # the reference types
        "SpeedFrame",  # the implementation record
        "SpeedFrames",  # the implementation array
        "SpeedFramesPtr",  # the pointer, last in the kind rank
    )
    readers_order = [element.name for element in wide_state["implementation_types"]]
    assert readers_order != list(slice_layer(wide_state, "implementation_types").identities)
    assert sorted(readers_order) == sorted(slice_layer(wide_state, "implementation_types").identities)
    assert slice_layer(wide_state, "base_types").identities == (
        "ProbeGearBase",
        "float32_base",
        "uint16_base",
        "uint8_base",
    )
    assert slice_layer(wide_state, "application_types").identities == (
        "ProbeGear",  # the enumeration naming a compu method
        "SpeedRecord",  # the record
        "SpeedHistory",  # the array
        "ProbeSpeedPhys",
        "Quality",
        "SpeedPhys",  # the application types
    )
    application_readers_order = [element.name for element in wide_state["application_types"]]
    assert application_readers_order != list(slice_layer(wide_state, "application_types").identities)
    assert sorted(application_readers_order) == sorted(slice_layer(wide_state, "application_types").identities)


def test_a_file_name_takes_no_part_in_the_slice_or_its_pages(tmp_path: Path) -> None:
    """Inside one kind, the read order's second key is identity, not file name.

    ``b.yaml`` declares ``A`` and ``a.yaml`` declares ``B``, both base types, so the kind rank ties and
    the reader's order decides. Read off disk through the one loader, because a state built in memory
    has no files to disagree with; the whole slice and a one-element page both start at ``A``.
    """
    model_root = tmp_path / "model"
    for filename, identity in (("b.yaml", "A"), ("a.yaml", "B")):
        path = model_root / "data_types" / "base" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"name: {identity}\nkind: base_type\nsize: 8\nencoding: unsigned\n", encoding="utf-8", newline="\n"
        )
    state = load_model_state(model_root)

    assert slice_layer(state, "base_types").identities == ("A", "B")
    first = slice_layer(state, "base_types", limit=1)
    assert first.identities == ("A",)
    assert slice_layer(state, "base_types", limit=1, after="A").identities == ("B",)


def test_the_canonical_equality_is_the_protocols(wide_state) -> None:
    """The two spellings of the canonical equality give one answer.

    ``query`` may not import ``editing`` (Rule 2 of ``test_editing_package_boundaries.py``), so the
    canonical equality a clause and a cursor are compared by is spelled twice. The values below are the
    rules that make it *not* Python's ``==``: a boolean is not an integer, and an integer and a float of
    the same value are one number.
    """
    pairs = [
        (1, 1.0),
        (True, True),
        (True, 1),
        (False, 0),
        ("a", "a"),
        ("1", 1),
        (None, None),
        (None, ""),
        ({"swc": "S", "runnable": "R"}, {"runnable": "R", "swc": "S"}),
        ({"swc": "S"}, {"swc": "T"}),
        ([1, 2], [1.0, 2.0]),
        ([1, 2], [2, 1]),
        # The third rule: an `int` too wide for a `float` is keyed as itself rather than raising
        # `OverflowError`; this pair crashes both surfaces if either spelling loses the arm.
        (10**400, 10**400),
        (10**400, 10**400 + 1),
        (10**400, 1),
        (10**400, 1.0),
        ({"x": 10**400}, {"x": 10**400}),
        # The same arm, narrower: an `int` a `float` ROUNDS rather than
        # refuses. Two 64-bit bounds one apart are two numbers, an integer a
        # float holds exactly is still that float, and a wide integer is not
        # the float it rounds to.
        (2**64 - 1, 2**64 - 1),
        (2**64 - 1, 2**64 - 2),
        (2**64 - 1, float(2**64)),
        (2**64, float(2**64)),
        (2**53 + 1, 2**53),
        (-(2**63), float(-(2**63))),
        ([2**63 - 1], [2**63]),
    ]
    for left, right in pairs:
        assert query.values_equal(left, right) is address.values_equal(left, right), (left, right)
    # The negative control: a table on which both answered the same way could
    # still be two tables agreeing by accident, so the two rules are named.
    assert not query.values_equal(True, 1)
    assert query.values_equal(1, 1.0)
    assert query.values_equal(10**400, 10**400) and not query.values_equal(10**400, 1)
    assert not query.values_equal(2**64 - 1, 2**64 - 2)
    assert query.values_equal(2**64, float(2**64))


def test_the_canonical_equality_is_the_protocols_for_a_model_and_for_an_object() -> None:
    """The model branch and the ``repr`` fallback, which the scalar table cannot reach.

    A clause composed from a parsed model value hands a ``BaseModel`` in, and any other object falls
    through to the repr. The impostor guards a divergence: one spelling dispatched on "has a
    ``model_dump`` attribute" and the other on ``isinstance(BaseModel)``, so such an object was reduced
    by one surface and compared whole by the other.
    """
    port = Port(name="pOut", direction="provide", interface="ISpeed")
    same = Port(name="pOut", direction="provide", interface="ISpeed")
    other = Port(name="pIn", direction="require", interface="ISpeed")

    for left, right in ((port, same), (port, other), (port, {"name": "pOut"})):
        assert query.values_equal(left, right) is address.values_equal(left, right), (left, right)
    assert query.values_equal(port, same)
    assert not query.values_equal(port, other)

    class _Impostor:
        """Not a ``BaseModel``, and carrying the method the old dispatch looked for."""

        def model_dump(self, **_kwargs: Any) -> dict[str, str]:
            return {"name": "pOut", "direction": "provide", "interface": "ISpeed"}

    impostor = _Impostor()
    assert query.values_equal(impostor, impostor) is address.values_equal(impostor, impostor)
    assert not query.values_equal(impostor, port)
    assert not address.values_equal(impostor, port)


class TestTheFilter:
    """``where`` at a layer and at a member slice."""

    def test_equals_selects_on_a_declared_scalar(self, wide_state) -> None:
        assert slice_layer(wide_state, "base_types", where=[Clause(field="kind", equals="base_type")]).identities == (
            "ProbeGearBase",
            "float32_base",
            "uint16_base",
            "uint8_base",
        )

    def test_clauses_are_and_composed(self, wide_state) -> None:
        result = slice_layer(
            wide_state,
            "base_types",
            where=[Clause(field="kind", equals="base_type"), Clause(field="size", equals=8)],
        )
        assert result.identities == ("uint8_base",)

    def test_an_integer_argument_reaches_a_number_field(self, wide_state) -> None:
        """``--where size=8`` is JSON's ``8`` and the field an ``int``; a bare ``==`` would match nothing."""
        assert slice_layer(wide_state, "base_types", where=[Clause(field="size", equals=8.0)]).identities == (
            "uint8_base",
        )
        assert slice_layer(wide_state, "base_types", where=[Clause(field="size", equals="8")]).identities == ()

    def test_exists_answers_both_ways_over_one_field(self, wide_state) -> None:
        """``unit`` is declared by ``value_type`` and ``application_type``, and each carries it.

        The partition is the assertion: every element is on exactly one side, so ``exists`` cannot read
        "declared by this kind" for one answer and "carried by this element" for the other. ``exists``
        asks whether a VALUE is carried and the empty string is none, so ``Quality``, carrying the
        default ``""``, is on the *absent* side like a value type with no unit.
        """
        # The two kinds are in two layers, so the partition is asserted in each.
        carried = slice_layer(wide_state, "implementation_types", where=[Clause(field="unit", exists=True)]).identities
        absent = slice_layer(wide_state, "implementation_types", where=[Clause(field="unit", exists=False)]).identities
        assert carried == ("Speed",)
        assert set(carried) | set(absent) == set(slice_layer(wide_state, "implementation_types").identities)
        assert not set(carried) & set(absent)
        carried = slice_layer(wide_state, "application_types", where=[Clause(field="unit", exists=True)]).identities
        absent = slice_layer(wide_state, "application_types", where=[Clause(field="unit", exists=False)]).identities
        assert "Quality" in absent
        assert "Quality" not in carried
        assert set(carried) | set(absent) == set(slice_layer(wide_state, "application_types").identities)
        assert not set(carried) & set(absent)

    def test_a_declared_field_holding_null_exists_and_a_field_no_kind_declares_does_not(self) -> None:
        """``--where wcet=null`` matches a task that does not state ``wcet``; ``--exists wcet`` does not.

        The cleared value is a value, but nothing is carried; a ``_row_matches`` that read the two the
        same way would answer both alike. The layer's other task carries the field, so the partition
        is the assertion.
        """
        state = _state(
            tasks=[
                Task(name="T_bare", period=0.01, priority=1, partition="PARTITION0"),
                Task(name="T_timed", period=0.02, priority=2, partition="PARTITION0", wcet="500us"),
            ]
        )
        assert slice_layer(state, "tasks", where=[Clause(field="wcet", exists=True)]).identities == ("T_timed",)
        assert slice_layer(state, "tasks", where=[Clause(field="wcet", exists=False)]).identities == ("T_bare",)
        assert slice_layer(state, "tasks", where=[Clause(field="wcet", equals=None)]).identities == ("T_bare",)
        assert slice_layer(state, "tasks", where=[Clause(field="wcet", equals="500us")]).identities == ("T_timed",)

    def test_a_placement_field_is_filterable(self, wide_state) -> None:
        """``group`` lives in the directory beside the element, not in it.

        A filter reading fields off the model alone would refuse the field ``swc`` is most likely sliced
        by, so the placement table is read exactly as ``editing.read._raw_placement`` reads it.
        """
        assert slice_layer(wide_state, "swc", where=[Clause(field="group", equals="control")]).identities == (
            "SpeedControl",
        )

    def test_an_empty_result_is_an_answer(self, wide_state) -> None:
        """An empty result is a fact about the model, which is why an undeclared field is refused."""
        result = slice_layer(wide_state, "implementation_types", where=[Clause(field="kind", equals="enum")])
        assert result.identities == ()
        assert result.where == (Clause(field="kind", equals="enum"),)

    def test_an_undeclared_field_is_refused_and_names_the_layer(self, wide_state) -> None:
        with pytest.raises(QueryError, match="'colour'.*layer 'implementation_types'"):
            slice_layer(wide_state, "implementation_types", where=[Clause(field="colour", equals="red")])

    def test_a_collection_field_is_refused_and_names_the_slice_that_answers_it(self, wide_state) -> None:
        with pytest.raises(QueryError, match="--members ports"):
            slice_layer(wide_state, "swc", where=[Clause(field="ports", equals="rIn")])

    def test_every_clause_is_checked_before_any_element_is_tested(self, wide_state) -> None:
        """A third clause naming an undeclared field refuses the request whole."""
        with pytest.raises(QueryError, match="'colour'"):
            slice_layer(
                wide_state,
                "base_types",
                where=[
                    Clause(field="kind", equals="base_type"),
                    Clause(field="size", equals=8),
                    Clause(field="colour", equals="red"),
                ],
            )

    def test_a_clause_carries_exactly_one_of_equals_and_exists(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            Clause(field="kind")
        with pytest.raises(ValueError, match="exactly one"):
            Clause(field="kind", equals="base_type", exists=True)


#: A field the tested thing does not carry at all, as distinct from every value.
_NOT_CARRIED = object()


@pytest.mark.parametrize(
    ("value", "carried"),
    [
        ("", False),
        (None, False),
        (_NOT_CARRIED, False),
        ("x", True),
        ("  ", True),
        (0, True),
        (False, True),
        ({}, True),
    ],
    ids=["empty-string", "null", "not-carried", "text", "whitespace", "zero", "false", "empty-map"],
)
def test_exists_is_one_answer_in_both_evaluators(value: Any, carried: bool) -> None:
    """The two statements of the ``exists`` rule give one answer.

    ``query.slicing._row_matches`` (this surface) and ``editing.read._clause_matches`` (the protocol)
    each test a not-carried sentinel first and then ask :func:`query.holds_no_value`. A field
    not carried, a ``null`` and the empty string are no value; whitespace, ``0``, ``false`` and an empty
    map are values. Asserted on the evaluators because no non-collection field of the contract holds a
    map, so ``{}`` reaches this rule nowhere else.
    """
    from swcraft_tools.editing import read as protocol_read

    dumped = {} if value is _NOT_CARRIED else {"field": value}
    protocol_value = protocol_read._MISSING if value is _NOT_CARRIED else value
    for exists in (True, False):
        kernel = query_slicing._row_matches(dumped, [Clause(field="field", exists=exists)], None)
        protocol = protocol_read._clause_matches(WhereClause(field="field", exists=exists), protocol_value)
        assert kernel == protocol == (carried == exists)
    if value is not _NOT_CARRIED:
        assert query.holds_no_value(value) is not carried


class TestTheDeclaredSafetyLevelIsTheOnlyOneQueryAnswers:
    """``query`` answers the declared safety level, never the derived one: a documented limit.

    ``query`` reads the **loaded element**, so ``--where asil=D`` names the components that *declare*
    ``D``. A component that declares nothing has an effective level, the maximum of the requirements
    allocated to it (what ``check_component_asil`` compares against), and **no clause of query reaches
    it**: the derived level is computed here through the validator's own function and shown to be a
    value the query does not answer.

    The field has **no default**, so three states are distinguishable: ``--where asil=QM`` answers the
    components that declare ``QM`` (no ASIL allocated), ``--where asil=D`` those that declare ``D``,
    and a component that declares nothing is answered by **neither**, because an omitted field dumps
    as ``None`` and ``_row_matches`` reads present-and-null as not existing. ``--exists asil`` is
    therefore "which components have taken a position".

    **What this does not reach**: the rule. Nothing on any read surface answers "what level does this
    component derive?", and no ``impact`` edge does.
    """

    _REQUIREMENT = Requirement(
        id="REQ_SAFETY_001",
        title="Braking",
        description="The system shall brake.",
        asil="D",
        version=1,
        source="architecture",
    )
    _SWCS = (
        SWComponent(name="DeclaresD", asil="D", satisfies=["REQ_SAFETY_001@1"]),
        SWComponent(name="DeclaresQM", asil="QM", satisfies=["REQ_SAFETY_001@1"]),
        SWComponent(name="DeclaresNothing", satisfies=["REQ_SAFETY_001@1"]),
    )

    @pytest.fixture
    def levelled(self) -> ModelState:
        return _state(swcs=list(self._SWCS), requirements=[self._REQUIREMENT])

    def test_a_component_that_only_derives_the_level_is_not_answered(self, levelled: ModelState) -> None:
        """The limit itself, against the derivation it does not see.

        ``DeclaresNothing`` really is at ASIL D — the validator says so — and
        ``--where asil=D`` still names only the component that wrote it down.
        """
        derived = effective_asil_levels(list(self._SWCS), [self._REQUIREMENT])
        assert derived["DeclaresNothing"] == "D"

        assert slice_layer(levelled, "swc", where=[Clause(field="asil", equals="D")]).identities == ("DeclaresD",)

    def test_a_declared_qm_is_answered_and_an_omission_is_not(self, levelled: ModelState) -> None:
        """``QM`` is a declaration: ``--where asil=QM`` does not name every component in the model."""
        assert slice_layer(levelled, "swc", where=[Clause(field="asil", equals="QM")]).identities == ("DeclaresQM",)

    def test_exists_partitions_the_layer_into_declared_and_omitted(self, levelled: ModelState) -> None:
        carried = slice_layer(levelled, "swc", where=[Clause(field="asil", exists=True)]).identities
        absent = slice_layer(levelled, "swc", where=[Clause(field="asil", exists=False)]).identities

        assert carried == ("DeclaresD", "DeclaresQM")
        assert absent == ("DeclaresNothing",)
        assert set(carried) | set(absent) == set(slice_layer(levelled, "swc").identities)

    def test_the_requirements_layer_filters_on_its_own_declaration(self, levelled: ModelState) -> None:
        """The field is declared on two kinds, and each layer answers for its own.

        Asserted because the rule reads both and a reader could take the
        component-side cases above for the whole story.
        """
        assert slice_layer(levelled, "requirements", where=[Clause(field="asil", equals="D")]).identities == (
            "REQ_SAFETY_001",
        )
        assert slice_layer(levelled, "requirements", where=[Clause(field="asil", equals="B")]).identities == ()


class TestTheMemberSlice:
    """``members``: one collection, sliced across the layer."""

    def test_a_list_collection_answers_rows_across_elements_with_their_owners(self, wide_state) -> None:
        """The question no address asks: every port of the layer, beside its owner.

        The rows span two elements, which is precisely what a collection address
        — one element's ``ports`` — cannot do.
        """
        assert slice_layer(wide_state, "swc", members="ports").identities == (
            {"element": "SpeedControl", "member": "rIn"},
            {"element": "SpeedControl", "member": "rDiag"},
            {"element": "SpeedSensor", "member": "pOut"},
        )

    def test_a_map_collection_answers_its_key_and_not_a_member(self, wide_state) -> None:
        """A map entry is reached by a ``key`` step and a list member by a ``member`` one.

        The row says the same word the address does, so a client composes the address from the row
        with no rule to learn.
        """
        rows = slice_layer(wide_state, "swc", members="memory").identities
        assert rows == (
            {"element": "SpeedControl", "key": "RAM0"},
            {"element": "SpeedSensor", "key": "RAM0"},
            {"element": "SpeedSensor", "key": "ScratchPad"},
            {"element": "Shared", "key": "RAM0"},
        )
        assert not any("member" in row for row in rows)

    def test_a_composite_member_identity_is_an_object(self, wide_state) -> None:
        """``task.runnables``: a composite member identity is an object, never an array."""
        assert slice_layer(wide_state, "tasks", members="runnables").identities[0] == {
            "element": "Task_10ms",
            # Three keys, the absent selector spelled `None`.
            "member": {"swc": "SpeedSensor", "instance": None, "runnable": "SpeedSensor_Run"},
        }

    def test_a_whole_value_collection_answers_its_members_verbatim(self, wide_state) -> None:
        """``composition.satisfies`` is a ``list[str]``: the member *is* its identity, version and all."""
        assert slice_layer(wide_state, "compositions", members="satisfies").identities == (
            {"element": "Powertrain", "member": "REQ_SPEED_001@1"},
            {"element": "Powertrain", "member": "REQ_SPEED_002@1"},
        )

    def test_a_clause_at_a_slice_tests_the_member(self, wide_state) -> None:
        """The filter is about the thing that was sliced, not about its owner."""
        assert slice_layer(
            wide_state, "swc", members="ports", where=[Clause(field="direction", equals="require")]
        ).identities == (
            {"element": "SpeedControl", "member": "rIn"},
            {"element": "SpeedControl", "member": "rDiag"},
        )

    def test_a_clause_the_member_kinds_do_not_declare_is_refused(self, wide_state) -> None:
        with pytest.raises(QueryError, match="'group'.*member kind of 'swc.ports'"):
            slice_layer(wide_state, "swc", members="ports", where=[Clause(field="group", equals="control")])

    def test_an_element_whose_kind_carries_no_such_collection_is_not_a_refusal(self, wide_state) -> None:
        """``Shared`` declares no ports, which is a selection and not an error.

        A layer holds several kinds and only some of them answer this question."""
        assert "Shared" not in {row["element"] for row in slice_layer(wide_state, "swc", members="ports").identities}

    def test_an_undeclared_collection_name_is_refused(self, wide_state) -> None:
        with pytest.raises(QueryError, match="--members 'sockets'"):
            slice_layer(wide_state, "swc", members="sockets")

    def test_a_declared_scalar_is_refused_and_names_the_filter_instead(self, wide_state) -> None:
        with pytest.raises(QueryError, match="--where type=VALUE"):
            slice_layer(wide_state, "swc", members="type")


class TestTheCompositionsLayerNeededNoCodeHere:
    """The ``compositions`` layer through the slice, with no code of its own.

    Three collections on one kind, two of them holding a member kind with a reference of its own, each
    answered by the projection that answers ``swc.ports``: the layer resolved through
    ``current_registry()``, the collection through the kind's declared fields and the member identity
    through the collection's declared ``identityField``. These cases are the observable half;
    :func:`test_the_slice_names_no_layer_of_its_own` reads the code.
    """

    def test_the_layer_slices_by_identity_in_the_read_order(self, wide_state) -> None:
        assert slice_layer(wide_state, "compositions").identities == ("Diagnostics", "Powertrain", "VehicleTop")

    def test_members_slices_the_hierarchy_across_the_layer_with_its_owners(self, wide_state) -> None:
        """``--members members``: every member of every composition, beside the one declaring it.

        A member is not a graph node, so this projection is the only surface that answers "which types
        are placed, and where".
        """
        assert slice_layer(wide_state, "compositions", members="members").identities == (
            {"element": "Diagnostics", "member": "shared"},
            {"element": "Powertrain", "member": "sensor"},
            {"element": "Powertrain", "member": "control"},
            {"element": "VehicleTop", "member": "powertrain"},
            {"element": "VehicleTop", "member": "diag"},
        )

    def test_the_other_two_collections_slice_the_same_way(self, wide_state) -> None:
        """``ports`` and ``delegations`` — one kind, three collections, one rule.

        Asserted beside ``members`` rather than instead of it because they
        differ in a way a single case would hide: ``delegations`` is declared on
        one of the three compositions only, so its rows span *fewer* elements
        than the layer has.
        """
        assert slice_layer(wide_state, "compositions", members="ports").identities == (
            {"element": "Diagnostics", "member": "pDiag"},
            {"element": "Powertrain", "member": "pSpeedOut"},
            {"element": "Powertrain", "member": "rDiagIn"},
        )
        assert slice_layer(wide_state, "compositions", members="delegations").identities == (
            {"element": "Powertrain", "member": "dSpeedOut"},
            {"element": "Powertrain", "member": "dDiagIn"},
        )

    def test_a_clause_filters_the_layer_on_a_field_of_its_own(self, wide_state) -> None:
        """``root`` is a declared boolean, and the filter reads it like any other.

        The negative half matters more than the positive one: two of the three
        compositions omit ``root`` on disk (``omitAtDefault``), so a filter that
        compared the *written* document rather than the loaded element would
        answer nothing at all.
        """
        assert slice_layer(wide_state, "compositions", where=[Clause(field="root", equals=True)]).identities == (
            "VehicleTop",
        )
        assert slice_layer(wide_state, "compositions", where=[Clause(field="root", equals=False)]).identities == (
            "Diagnostics",
            "Powertrain",
        )

    def test_a_member_clause_tests_the_member_and_not_its_owner(self, wide_state) -> None:
        """A composition-typed member declares ``composition``; a component-typed one ``type``."""
        assert slice_layer(
            wide_state, "compositions", members="members", where=[Clause(field="composition", exists=True)]
        ).identities == (
            {"element": "VehicleTop", "member": "powertrain"},
            {"element": "VehicleTop", "member": "diag"},
        )

    def test_a_collection_this_layer_does_not_declare_is_refused_by_name(self, wide_state) -> None:
        with pytest.raises(QueryError, match="--members 'runnables'"):
            slice_layer(wide_state, "compositions", members="runnables")

    def test_the_slice_names_no_layer_of_its_own(self) -> None:
        """The genericity claim, read off the code rather than off the answers.

        The ``query`` package may spell ``compositions`` only in field ids, the
        :data:`~swcraft_tools.query.edges._EDGE_DETAILS` rows. The scan is over the string literals of
        every module of the package, docstrings and the edge table's keys excluded, the keys asserted
        non-empty so the exclusion cannot be what makes the case pass.

        What it does **not** reach: a branch that named no layer but keyed on a layer's *shape* — a kind
        count, a nesting depth. The synthetic layer of ``test_model_relations.py`` and
        ``test_listing.py`` covers that.
        """
        package = Path(inspect.getfile(slice_layer)).parent
        modules = sorted(package.rglob("*.py"), key=lambda path: path.as_posix())
        assert len(modules) > 1, "the package scan found one module; the union below would be one file"
        literals: set[str] = set()
        for module in modules:
            tree = ast.parse(module.read_text(encoding="utf-8"))
            docstrings = {
                id(node.body[0].value)
                for node in ast.walk(tree)
                if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
                and node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            }
            literals |= {
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
            }
        assert literals, "the literal scan read nothing; the assertion below would be vacuous"

        edge_rows = {key for key in query_edges._EDGE_DETAILS if "compositions" in key}
        assert edge_rows, "the edge table carries no compositions row; the exclusion below is unearned"

        named = sorted(
            value
            for value in literals - set(query_edges._EDGE_DETAILS)
            if value in {layer.name for layer in current_registry().layers}
        )
        assert named == [], f"the query package spells a layer name outside the edge table: {named}"


def test_a_member_that_does_not_carry_its_declared_identity_has_none() -> None:
    """``_member_identity``'s fallback answers ``None``, as ``editing.address.member_identity_of`` does.

    The answer becomes a page **cursor**, so a wrong one silently mis-positions ``--after`` rather than
    refusing; ``None`` matches only another member that also has none.
    """
    registry = current_registry()
    collection = registry.layer("swc").top_level_kinds[0].get_field("ports").collection
    rule = collection_rule(registry.bound_class(collection.kind), collection.field, registry=registry)
    descriptor = reg.REGISTRY.layer("swc").top_level_kinds[0].get_field("ports").collection

    faceless = {"direction": "provide", "interface": "ISpeed"}
    assert query_slicing._member_identity(rule, faceless) is None
    assert address.member_identity_of(faceless, descriptor) is None
    # And the identity-carrying member answers the same value on both surfaces.
    named = {"name": "pOut", "direction": "provide", "interface": "ISpeed"}
    assert query_slicing._member_identity(rule, named) == address.member_identity_of(named, descriptor) == "pOut"


class TestThePage:
    """``limit`` / ``after``: a keyset window in the read order."""

    def test_a_window_carries_the_total_the_truncation_and_the_cursor(self, wide_state) -> None:
        # The layer's ten implementation types.
        result = slice_layer(wide_state, "implementation_types", limit=3)
        assert result.identities == ("ProbeGearValue", "Speed", "float32")
        assert result.page == QueryPage(total=10, returned=3, truncated=True, next="float32")

    def test_the_cursor_continues_the_walk_in_the_read_order(self, wide_state) -> None:
        first = slice_layer(wide_state, "implementation_types", limit=3)
        second = slice_layer(wide_state, "implementation_types", limit=3, after=first.page.next)
        assert second.identities == ("uint16", "uint8", "ProbeGearRef")
        assert first.identities + second.identities == slice_layer(wide_state, "implementation_types").identities[:6]

    def test_truncated_is_not_the_cursors_presence(self, wide_state) -> None:
        """The two come apart at both ends of a walk, and both ends are asserted.

        A ``limit`` above the set is complete with no cursor; the **last** window
        of a walk is truncated with no cursor; a window past the end is truncated
        and empty. Deriving the flag from ``next`` answers the first row wrongly
        in one direction and the second in the other.
        """
        whole = slice_layer(wide_state, "implementation_types", limit=99).page
        assert (whole.returned, whole.truncated, whole.next) == (10, False, ABSENT)
        last = slice_layer(wide_state, "implementation_types", limit=2, after="SpeedFrames").page
        assert (last.returned, last.truncated, last.next) == (1, True, ABSENT)
        past = slice_layer(wide_state, "implementation_types", limit=2, after="SpeedFramesPtr").page
        assert (past.returned, past.truncated, past.next) == (0, True, ABSENT)

    def test_the_filter_runs_before_the_page_and_the_total_is_the_filtered_set(self, wide_state) -> None:
        result = slice_layer(wide_state, "base_types", where=[Clause(field="kind", equals="base_type")], limit=2)
        assert result.identities == ("ProbeGearBase", "float32_base")
        assert result.page.total == 4

    def test_a_slice_pages_by_the_row_and_not_by_the_member(self, wide_state) -> None:
        """The cursor carries the owner because a member identity is not unique.

        ``RAM0`` is a key of three different components here, so a cursor naming
        only the member would continue past whichever row came first.
        """
        first = slice_layer(wide_state, "swc", members="memory", limit=2)
        assert first.page.next == {"element": "SpeedSensor", "key": "RAM0"}
        second = slice_layer(wide_state, "swc", members="memory", limit=2, after=first.page.next)
        assert second.identities == (
            {"element": "SpeedSensor", "key": "ScratchPad"},
            {"element": "Shared", "key": "RAM0"},
        )

    def test_a_composite_cursor_is_replayed_verbatim(self, wide_state) -> None:
        first = slice_layer(wide_state, "tasks", members="runnables", limit=1)
        second = slice_layer(wide_state, "tasks", members="runnables", limit=1, after=first.page.next)
        assert second.identities == (
            {
                "element": "Task_10ms",
                "member": {"swc": "SpeedControl", "instance": None, "runnable": "SpeedControl_Run"},
            },
        )
        # The cursor itself is the emitted three-key identity (QUERY_SCHEMA_VERSION 3).
        assert first.page.next == {
            "element": "Task_10ms",
            "member": {"swc": "SpeedSensor", "instance": None, "runnable": "SpeedSensor_Run"},
        }

    def test_a_composite_cursor_without_the_unset_selector_continues_the_same_page(self, wide_state) -> None:
        """The absent selector reads as ``null`` in a cursor, so a two-key identity continues the page."""
        three_key = slice_layer(wide_state, "tasks", members="runnables", limit=1).page.next
        two_key = {"element": three_key["element"], "member": {"swc": "SpeedSensor", "runnable": "SpeedSensor_Run"}}
        expected = slice_layer(wide_state, "tasks", members="runnables", limit=1, after=three_key).identities
        assert slice_layer(wide_state, "tasks", members="runnables", limit=1, after=two_key).identities == expected
        # A missing REQUIRED identity field is not completed, and the cursor is refused.
        with pytest.raises(QueryError, match="--after"):
            slice_layer(
                wide_state,
                "tasks",
                members="runnables",
                limit=1,
                after={"element": three_key["element"], "member": {"swc": "SpeedSensor"}},
            )

    def test_a_cursor_the_slice_does_not_carry_is_refused(self, wide_state) -> None:
        """A walk crossing an edit that removed the cursor element is refused.

        The keyset's whole argument: it is refused rather than answered with a different window."""
        with pytest.raises(QueryError, match="--after NoSuchType"):
            slice_layer(wide_state, "implementation_types", limit=2, after="NoSuchType")

    def test_a_limit_below_one_is_refused(self, wide_state) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            slice_layer(wide_state, "implementation_types", limit=0)

    @pytest.mark.parametrize("limit", [True, False, 1.0, 2.5, "2"])
    def test_a_limit_that_is_not_a_whole_number_is_refused_rather_than_read_or_escaped(self, wide_state, limit) -> None:
        """The bound alone was not the guard: the type is half of it.

        ``limit < 1`` admits ``True`` (``bool`` is an ``int`` subclass, so the page became one row) and
        ``1.0``, which escaped as an undocumented ``TypeError``. ``query.integral`` reads ``1.0`` as
        ``1`` on the JSON half, as JSON Schema does; this Python half keeps an exact ``int`` and raises
        ``ValueError`` so the CLI's one refusal clause catches it.
        """
        with pytest.raises(ValueError, match="whole number of rows"):
            slice_layer(wide_state, "implementation_types", limit=limit)

    def test_a_cursor_naming_an_element_the_filter_removed_is_refused(self, wide_state) -> None:
        """The interaction of the two keys, which neither key's own tests reach.

        ``uint8`` is in this layer and **not** in this slice, because the clause
        selected it out. The cursor is searched in the *post-filter* set, so it
        is refused — which is right, and is what fixes the order of the two
        operations in place: a page taken before the filter would find the
        cursor, window from it, and then filter, answering a different set from
        a position in another one.
        """
        with pytest.raises(QueryError, match="--after uint8"):
            slice_layer(
                wide_state,
                "implementation_types",
                where=[Clause(field="kind", equals="record")],
                limit=2,
                after="uint8",
            )

    def test_a_page_over_a_selection_that_matched_nothing_is_an_answer(self, wide_state) -> None:
        """Zero of zero, complete — never a refusal, and never an absent page.

        "No element carries that value" is a fact about the model and a page of
        it is an empty window, so the three figures are reported exactly as they
        would be for a non-empty set. A ``page`` omitted here would make a client
        that reads ``total`` branch on whether the answer was interesting.
        """
        empty = slice_layer(
            wide_state, "implementation_types", where=[Clause(field="kind", equals="nosuchkind")], limit=5
        )
        assert empty.identities == ()
        assert empty.page == QueryPage(total=0, returned=0, truncated=False, next=ABSENT)

    def test_a_cursor_carrying_a_non_ascii_identity_round_trips(self) -> None:
        """A cursor is a model identity, and a model identity is not ASCII by rule.

        ``format_query`` writes it with ``ensure_ascii=False`` and the CLI reads
        it back through ``json.loads``, so a name outside ASCII has to survive
        both. Nothing else in this suite carries one.
        """
        state = _state(
            requirements=[
                Requirement(id="REQ_ÜBER_001", title="a", description="a", version=1, source="architecture"),
                Requirement(id="REQ_ЖЮРИ_002", title="b", description="b", version=1, source="architecture"),
                Requirement(id="REQ_z_003", title="c", description="c", version=1, source="architecture"),
            ]
        )
        first = slice_layer(state, "requirements", limit=1)
        assert first.page.next == "REQ_ÜBER_001"
        second = slice_layer(state, "requirements", limit=1, after=first.page.next)
        assert second.identities == ("REQ_ЖЮРИ_002",)
        assert "REQ_ÜBER_001" in format_query(first)

    def test_a_cursor_without_a_limit_is_refused_here_too(self, wide_state) -> None:
        """The library refuses a cursor without a limit, as the wire, the schema and the terminal do.

        ``ReadRequest.__post_init__``, ``$defs/readRequest``'s ``dependentRequired`` and ``cmd_query``
        refuse it; guards a defect where the library answered the unpaged listing and dropped the cursor.
        """
        with pytest.raises(QueryError, match="`after` continues a page"):
            slice_layer(wide_state, "implementation_types", after="uint8")
        # And a cursor the slice does not carry is refused the same way, rather
        # than being answered because no `limit` made anyone look.
        with pytest.raises(QueryError, match="`after` continues a page"):
            slice_layer(wide_state, "implementation_types", after="NoSuchType")

    def test_an_unpaged_slice_reports_no_page_at_all(self, wide_state) -> None:
        assert slice_layer(wide_state, "implementation_types").page is None


class TestTheRenderers:
    """The bytes ``swcraft query`` prints, which are this module's and not the ``cli`` package's."""

    def test_an_unfiltered_slice_is_the_plain_identity_listing(self, wide_state) -> None:
        assert format_query(slice_layer(wide_state, "tasks")) == "tasks: Task_10ms\ntasks: Task_20ms\n"

    def test_the_scope_footer_names_only_what_was_narrowed(self, wide_state) -> None:
        rendered = format_query(
            slice_layer(wide_state, "base_types", where=[Clause(field="kind", equals="base_type")], limit=2)
        )
        # The base types are the `base_types` layer's.
        assert rendered.splitlines() == [
            "base_types: ProbeGearBase",
            "base_types: float32_base",
            "",
            "(where: kind=base_type; page: 2 of 4, truncated; next: after=float32_base)",
        ]

    def test_the_scope_footer_names_an_absent_clause_as_absent(self, wide_state) -> None:
        """``Clause.as_text``'s second existence branch, which no other test renders.

        ``exists`` and ``absent`` are two questions and the footer has to name
        which one was asked — a reader looking at ``(where: unit exists)`` above
        a one-line answer must be able to tell it from the five-line one. The
        branch is reachable from the protocol half today and from
        ``swcraft query`` only through a ``Clause`` composed in Python, which
        is the one filter question the CLI cannot ask.
        """
        absent = slice_layer(wide_state, "implementation_types", where=[Clause(field="unit", exists=False)])
        rendered = format_query(absent)
        assert "(where: unit absent)" in rendered
        assert "unit exists" not in rendered
        assert format_query(
            slice_layer(wide_state, "implementation_types", where=[Clause(field="unit", exists=True)])
        ).endswith("(where: unit exists)\n")

    def test_a_member_row_is_written_the_way_its_address_reads(self, wide_state) -> None:
        """``_rendered_identity``'s three branches, none of which a layer slice reaches.

        A list member is ``Owner.member``, a map entry is ``Owner[key]``, and a
        composite member identity falls back to its JSON object because there is
        no shorter spelling that is still replayable as ``--after``. All three
        are rendered through ``format_query`` rather than asserted on the helper,
        because the bytes are what a reader copies back onto the command line.
        """
        listed = format_query(slice_layer(wide_state, "swc", members="ports", limit=1))
        assert listed.splitlines()[0] == "swc: SpeedControl.rIn"

        mapped = format_query(slice_layer(wide_state, "swc", members="memory", limit=1))
        assert mapped.splitlines()[0] == "swc: SpeedControl[RAM0]"

        composite = format_query(slice_layer(wide_state, "tasks", members="runnables", limit=1))
        assert composite.splitlines()[0] == (
            'tasks: Task_10ms.{"instance": null, "runnable": "SpeedSensor_Run", "swc": "SpeedSensor"}'
        )

    def test_an_empty_answer_under_no_scope_prints_nothing(self) -> None:
        """``listing.format_listing``'s contract.

        An unfiltered ``query`` and a ``list`` differ in the order of the lines and in nothing else."""
        assert format_query(Slice(layer="tasks", identity_field="name", identities=())) == ""

    def test_an_empty_answer_under_a_scope_still_states_the_scope(self, wide_state) -> None:
        """Otherwise "nothing matched" and "there is nothing here" print the same."""
        assert (
            format_query(slice_layer(wide_state, "implementation_types", where=[Clause(field="kind", equals="enum")]))
            == "(where: kind=enum)\n"
        )

    def test_the_json_document_echoes_only_the_keys_the_request_carried(self, wide_state) -> None:
        document = json.loads(format_query_json(slice_layer(wide_state, "tasks")))
        assert document == {
            "schemaVersion": 3,
            "layer": "tasks",
            "identityField": "name",
            "elements": ["Task_10ms", "Task_20ms"],
        }

    def test_the_json_document_carries_the_echo_and_the_page_when_they_were_asked_for(self, wide_state) -> None:
        document = json.loads(
            format_query_json(
                slice_layer(wide_state, "swc", members="ports", where=[Clause(field="direction", exists=True)], limit=2)
            )
        )
        assert document["members"] == "ports"
        assert document["where"] == [{"field": "direction", "exists": True}]
        assert document["page"] == {
            "total": 3,
            "returned": 2,
            "truncated": True,
            "next": {"element": "SpeedControl", "member": "rDiag"},
        }

    def test_the_walk_renderer_is_not_format_impacts_bytes(self, wide_snapshot, wide_state) -> None:
        """The walk renderer does not print ``format_impact``'s byte-frozen report.

        One output printed by two commands would be both commands' contract. The two answer about
        the same set, which makes the difference a rendering choice rather than a different question.
        """
        walked = query.related(wide_state, "ISpeed", "dependents", None)
        rendered = format_related("ISpeed", walked)
        frozen = format_impact("ISpeed", impact_of(wide_snapshot, "ISpeed"))
        assert rendered.rstrip("\n") != frozen
        assert rendered.startswith("Dependents of 'ISpeed':")
        assert frozen.startswith("Impact of 'ISpeed':")
        names = {row.ref.name for rows in walked.values() for row in rows}
        assert all(name in rendered for name in names)

    def test_the_walk_renderer_names_the_direction_it_walked(self, wide_state) -> None:
        assert (
            format_related(
                "SpeedControl",
                query.related(wide_state, "SpeedControl", "dependencies", 1),
                direction="dependencies",
                depth=1,
            ).splitlines()[0]
            == "Dependencies of 'SpeedControl':"
        )

    def test_the_depth_is_a_footer_and_is_absent_at_the_default(self, wide_state) -> None:
        bounded = format_related("ISpeed", query.related(wide_state, "ISpeed", "dependents", 1), depth=1)
        unbounded = format_related("ISpeed", query.related(wide_state, "ISpeed", "dependents", None))
        assert bounded.rstrip("\n").endswith("(depth: 1)")
        assert "(depth" not in unbounded

    def test_a_matched_element_with_no_rows_says_none_and_not_no_dependents(self, wide_state) -> None:
        """``(none)``, because this renderer answers both directions.

        ``(no dependents)`` is the frozen report's word for one of them."""
        rendered = format_related("speed_cycle", query.related(wide_state, "speed_cycle", "dependents", None))
        assert "  (none)" in rendered

    def test_a_name_no_layer_carries_is_an_answer_and_not_a_refusal(self, wide_state) -> None:
        assert format_related("NoSuchThing", query.related(wide_state, "NoSuchThing", "dependents", None)) == (
            "No element named 'NoSuchThing' in any layer.\n"
        )
        assert json.loads(format_related_json("NoSuchThing", {}))["matched"] == {}

    def test_the_walk_json_document_carries_every_field_a_dependent_holds(self, wide_state) -> None:
        document = json.loads(
            format_related_json("ISpeed", query.related(wide_state, "ISpeed", "dependents", 1), depth=1)
        )
        assert document["direction"] == "dependents"
        assert document["depth"] == 1
        rows = document["matched"]["interface"]
        assert {"layer", "name", "detail", "details"} == set(rows[0])
        assert {
            "layer": "swc",
            "name": "SpeedControl",
            "detail": "via port rIn",
            "details": [{"kind": "port", "value": "rIn"}],
        } in rows

    def test_both_json_renderers_refuse_a_non_finite_value_rather_than_emit_one(self, wide_state) -> None:
        """`_json_document` renders with `allow_nan=False`, at both `return`s.

        Both documents echo the client's own request — a `--where` value reaches
        the slice document and `format_related_json` shares the renderer — and
        `_query_value` accepts `NaN` / `Infinity` / `-Infinity` because
        `json.loads` does. A bare `NaN` literal is not RFC 8259; the encoder
        raising is what turns it into the CLI's ordinary `Error:` refusal.
        """
        for value in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError, match="not JSON compliant"):
                format_query_json(slice_layer(wide_state, "tasks", where=[Clause(field="period", equals=value)]))
        with pytest.raises(ValueError, match="not JSON compliant"):
            format_related_json("ISpeed", query.related(wide_state, "ISpeed", "dependents", None), depth=float("nan"))

    def test_every_renderer_is_newline_terminated(self, wide_state) -> None:
        """The CLI writes them bare, so a missing terminator is a joined line."""
        for rendered in (
            format_query(slice_layer(wide_state, "tasks")),
            format_query_json(slice_layer(wide_state, "tasks")),
            format_related("ISpeed", query.related(wide_state, "ISpeed", "dependents", None)),
            format_related_json("ISpeed", query.related(wide_state, "ISpeed", "dependents", None)),
        ):
            assert rendered.endswith("\n")


def test_an_undeclared_layer_is_a_key_error(wide_state) -> None:
    """``registry.layer``'s own refusal, not a second one.

    The CLI never reaches it (argparse's ``choices`` come from the same registry), which is why this is asserted here
    rather than at the CLI boundary."""
    with pytest.raises(KeyError):
        slice_layer(wide_state, "nope")


def test_a_layer_the_state_does_not_carry_answers_an_empty_slice(wide_state) -> None:
    """A layer with no directory is an answer, and an empty one — never a miss.

    ``load_model_state`` gives such a layer an empty list, and the projection
    reads it through ``state.layers.get`` rather than through ``state[...]`` so
    that a state built without the key answers the same way.
    """
    empty = ModelState(layers={})
    assert slice_layer(empty, "tasks").identities == ()
    assert slice_layer(empty, "swc", members="ports").identities == ()
    assert slice_layer(empty, "tasks", limit=5).page == QueryPage(total=0, returned=0, truncated=False, next=ABSENT)


# ---------------------------------------------------------------------------
# What the read surfaces reach of the safety fields, and what they do not
#
# Measured over the acceptance fixture, and every one is a limit rather than a defect: the filter,
# the member slice and the existence test are keyed on a layer's TOP-LEVEL declared fields, and two
# of the three safety fields are not top-level on any layer element.
# ---------------------------------------------------------------------------


class TestWhatQueryReachesOfTheCapabilityField:
    """`asil_capability` is a TOP-LEVEL declared field of two `hardware` kinds: the filter reaches it.

    Unlike `coexistence`, the field has **no default**, so an unrated core does not carry it and
    `--exists` separates the rated elements from the unrated ones. Both halves are asserted.
    """

    def test_where_answers_the_rated_elements_of_the_acceptance_fixture(self) -> None:
        state = load_model_state(str(SAFETY_MODEL_ROOT))

        assert slice_layer(state, "hardware", where=(Clause("asil_capability", equals="B"),)).identities == (
            "CORE0",
            "RAM0",
        )

    def test_exists_answers_exactly_the_declaring_elements_because_the_field_has_no_default(self) -> None:
        state = _state(
            hardware=[
                Processor(name="CPU", kind="processor"),
                Core(name="CORE0", kind="core", threshold=0.7, asil_capability="B"),
                Core(name="CORE1", kind="core", threshold=0.7),
                Memory(
                    name="RAM0",
                    kind="memory",
                    type="RAM",
                    size=1,
                    accessible=["CORE0"],
                    threshold=1.0,
                    asil_capability="QM",
                ),
            ]
        )

        carried = slice_layer(state, "hardware", where=(Clause("asil_capability", exists=True),)).identities
        absent = slice_layer(state, "hardware", where=(Clause("asil_capability", exists=False),)).identities

        assert set(carried) == {"CORE0", "RAM0"}
        assert set(absent) == {"CPU", "CORE1"}

    def test_list_is_byte_identical_with_and_without_the_two_capabilities(self, tmp_path: Path) -> None:
        """`list` reads only each layer's identity field: the two fields add no read to it."""
        import shutil

        from swcraft_tools.listing import list_model
        from test_safety_byte_identity import strip_capability

        declared = tmp_path / "declared"
        bare = tmp_path / "bare"
        shutil.copytree(SAFETY_MODEL_ROOT, declared)
        shutil.copytree(SAFETY_MODEL_ROOT, bare)

        assert strip_capability(bare) == 2
        assert list_model(str(bare)) == list_model(str(declared))

    def test_the_cli_answers_the_rated_elements_by_filter_and_by_existence(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The two reads through ``swcraft query`` itself, over a copy of the acceptance fixture."""
        import shutil

        from swcraft_tools.cli import main

        shutil.copytree(SAFETY_MODEL_ROOT, tmp_path / "model")
        manifest = _write_manifest(tmp_path)

        for clause, footer in (
            (["--where", "asil_capability=B"], "(where: asil_capability=B)"),
            (["--exists", "asil_capability"], "(where: asil_capability exists)"),
        ):
            monkeypatch.setattr("sys.argv", ["swcraft", "query", "hardware", *clause, "--project", str(manifest)])
            main()  # a successful read: must not raise SystemExit
            assert capsys.readouterr().out.splitlines() == ["hardware: CORE0", "hardware: RAM0", "", footer]


class TestWhatQueryReachesOfTheCoexistenceFields:
    """Measured on `fixtures/safety/model`, the acceptance fixture.

    No read surface exposes a component's *effective* level. A client asking about the three declared
    fields meets three different answers depending on where the field sits.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def safety_state() -> ModelState:
        return load_model_state(str(SAFETY_MODEL_ROOT))

    def test_a_top_level_level_is_filterable(self, safety_state: ModelState) -> None:
        """`swc.asil` IS top-level, so the ordinary filter reaches it."""
        assert slice_layer(safety_state, "swc", where=(Clause("asil", equals="D"),)).identities == ("TorqueLimiter",)

    def test_a_ports_measures_map_is_not_filterable_and_the_refusal_says_why(self, safety_state: ModelState) -> None:
        """`measures` sits on a PORT, and the filter reads a layer element's own fields.

        This is the limit, stated rather than worked around: a client that wants
        the ports carrying a measure reads the elements and looks, or asks the
        editing protocol, whose member slice publishes a member's summary.
        """
        with pytest.raises(QueryError, match="not a declared field"):
            slice_layer(safety_state, "swc", where=(Clause("measures", equals="x"),))

    def test_a_data_elements_level_is_not_filterable_either(self, safety_state: ModelState) -> None:
        """`interfaces.data_element.asil` sits on a member of an interface."""
        with pytest.raises(QueryError, match="not a declared field"):
            slice_layer(safety_state, "interfaces", where=(Clause("asil", equals="D"),))

    def test_measures_is_not_a_sliceable_collection_because_it_is_not_top_level(self, safety_state: ModelState) -> None:
        """`--members` takes a layer element's own collection; `measures` is a port's."""
        with pytest.raises(QueryError, match="not a declared field"):
            slice_layer(safety_state, "swc", members="measures")

    def test_the_ports_slice_answers_every_port_with_its_owner_and_its_identity_alone(
        self, safety_state: ModelState
    ) -> None:
        """What `--members ports` really publishes, measured.

        Each row is `(owner, member identity)` — the port's NAME — and not the port's fields, so it
        does not show which ports carry a measure. That is the surface's shape: the same holds for a
        port's `description`.
        """
        answer = slice_layer(safety_state, "swc", members="ports")

        assert {"element": "DiagnosticsAgent", "member": "pDiag"} in answer.identities
        assert {"element": "DiagnosticsAgent", "member": "rSafety"} in answer.identities
        # And nothing else: each row is the owner plus the member's IDENTITY, so
        # a client cannot tell from this answer which ports carry a measure.
        assert {tuple(sorted(row)) for row in answer.identities} == {("element", "member")}

    def test_exists_on_coexistence_tells_an_argued_task_from_a_blank_one(self, safety_state: ModelState) -> None:
        """`--exists coexistence` tells an argued task from a blank one.

        `coexistence` defaults to `""`, and `exists` asks whether a value is carried, the empty string
        being none: `--exists coexistence` answers exactly the argued tasks and its negation the blank
        one — the set `--where coexistence=` finds, asserted beside it so the two spellings cannot
        drift apart.
        """
        existing = slice_layer(safety_state, "tasks", where=(Clause("coexistence", exists=True),)).identities
        absent = slice_layer(safety_state, "tasks", where=(Clause("coexistence", exists=False),)).identities
        blank = slice_layer(safety_state, "tasks", where=(Clause("coexistence", equals=""),)).identities

        assert set(existing) == {"Task_Argued", "Task_Homogeneous"}
        assert absent == blank == ("Task_Mixed",)

    def test_list_is_byte_identical_with_and_without_the_three_fields(self, tmp_path: Path) -> None:
        """`list` answers identities in reader order, so none of the three moves it.

        The bare tree is stripped by `test_safety_byte_identity.py`'s own helper, which removes **all
        three** fields and asserts it found at least six.
        """
        import shutil

        from test_safety_byte_identity import _strip_coexistence_fields

        declared = tmp_path / "declared"
        bare = tmp_path / "bare"
        shutil.copytree(SAFETY_MODEL_ROOT, declared)
        shutil.copytree(SAFETY_MODEL_ROOT, bare)

        assert _strip_coexistence_fields(bare) >= 6

        from swcraft_tools.listing import list_model

        assert list_model(str(bare)) == list_model(str(declared))


# ---------------------------------------------------------------------------
# Every detail of every edge that reached a dependent
#
# The adjacency map accumulates ``EdgeDetail(kind, value)`` tuples in emission order, deduplicated,
# and one rendering table turns a tuple into the parenthesis a report line carries. An edge into an
# element a walk already reached adds its details to the row. What that does not reach — an edge
# from an element at a bounded walk's bound — is asserted below rather than left to a docstring.
# ---------------------------------------------------------------------------


def _three_port_state() -> ModelState:
    """The showcase case, built in memory: three provide ports on one interface.

    ``packages/tools/tests/fixtures/showcase/model`` itself declares no connector, so the
    connector the case needs — one endpoint on the component's second port — is
    added here, beside a consumer to connect it to. The consumer's port is named
    like one of ``BrakeController``'s, so a connector line that named a bare port
    could be matched against the wrong component.
    """
    brake = SWComponent(
        name="BrakeController",
        type="application",
        ports=[
            Port(name="pBrakeRequest", direction="provide", interface="IBrakeRequest"),
            Port(name="pBrakeRequestAux", direction="provide", interface="IBrakeRequest"),
            Port(name="pBrakeRequestDiag", direction="provide", interface="IBrakeRequest"),
        ],
    )
    consumer = SWComponent(
        name="BrakeConsumer",
        type="application",
        ports=[Port(name="pBrakeRequest", direction="require", interface="IBrakeRequest")],
    )
    link = Connector.model_validate(
        {
            "name": "Brake_link",
            "from": {"swc": "BrakeController", "port": "pBrakeRequestAux"},
            "to": {"swc": "BrakeConsumer", "port": "pBrakeRequest"},
        }
    )
    iface = SRInterface(
        name="IBrakeRequest", kind="sender_receiver", data_elements=[DataElement(name="v", implementation_type="uint8")]
    )
    return _state(swcs=[brake, consumer], interfaces=[iface], connectors=[link])


_THREE_PORTS = (
    EdgeDetail("port", "pBrakeRequest"),
    EdgeDetail("port", "pBrakeRequestAux"),
    EdgeDetail("port", "pBrakeRequestDiag"),
)


class TestEveryDetailOfTheReachingEdges:
    """The accumulating map, in both directions; the merging walks and their one named limit."""

    def test_three_ports_on_one_interface_are_one_dependent_with_three_details(self) -> None:
        state = _three_port_state()
        rows = dependents(state, "IBrakeRequest")["interface"]
        assert rows[0] == Dependent(ElementRef("swc", "BrakeController"), _THREE_PORTS)
        assert [row.ref.name for row in rows].count("BrakeController") == 1, "one line per dependent"
        reverse = build_graphs(state).reverse
        assert reverse[ElementRef("interface", "IBrakeRequest")][ElementRef("swc", "BrakeController")] == _THREE_PORTS

    def test_a_port_reached_through_two_references_is_one_detail(self) -> None:
        """A connector whose two endpoints name the same port of one component."""
        loop = Connector.model_validate(
            {"name": "Loop", "from": {"swc": "A", "port": "p"}, "to": {"swc": "A", "port": "p"}}
        )
        state = _state(swcs=[SWComponent(name="A", type="application")], connectors=[loop])
        assert dependents(state, "A")["swc"] == [
            Dependent(ElementRef("connector", "Loop"), (EdgeDetail("endpoint", "A.p"),))
        ]

    def test_two_distinct_endpoint_ports_on_one_component_are_both_kept(self) -> None:
        loop = Connector.model_validate(
            {"name": "Loop", "from": {"swc": "A", "port": "pOut"}, "to": {"swc": "A", "port": "rIn"}}
        )
        state = _state(swcs=[SWComponent(name="A", type="application")], connectors=[loop])
        assert dependents(state, "A")["swc"] == [
            Dependent(
                ElementRef("connector", "Loop"), (EdgeDetail("endpoint", "A.pOut"), EdgeDetail("endpoint", "A.rIn"))
            )
        ]

    def test_dependencies_carries_the_same_tuples_read_the_other_way(self) -> None:
        state = _three_port_state()
        graphs = build_graphs(state)
        for node, edges in graphs.reverse.items():
            for reached, details in edges.items():
                assert graphs.forward[reached][node] == details, (node, reached)
        forward = dependencies(state, "BrakeController", depth=1)["swc"]
        assert Dependent(ElementRef("interface", "IBrakeRequest"), _THREE_PORTS) in forward

    @pytest.mark.parametrize(
        ("start", "reached", "kind", "value", "rendered"),
        [
            ("ISpeed", ElementRef("swc", "SpeedControl"), "port", "rIn", "via port rIn"),
            ("ISpeed", ElementRef("composition", "Powertrain"), "port", "pSpeedOut", "via port pSpeedOut"),
            ("SpeedSensor", ElementRef("task", "Task_10ms"), "runnable", "SpeedSensor_Run", "runnable SpeedSensor_Run"),
            ("SpeedSensor", ElementRef("composition", "Powertrain"), "member", "sensor", "member sensor"),
            ("Powertrain", ElementRef("composition", "VehicleTop"), "member", "powertrain", "member powertrain"),
            ("SpeedSensor", ElementRef("sequence", "speed_cycle"), "step", "SpeedSensor_Run", "step: SpeedSensor_Run"),
            ("RAM0", ElementRef("swc", "SpeedControl"), "budget", "1024", "memory budget 1024 bytes"),
            # A task names its partition and the partition its core.
            ("PARTITION0", ElementRef("task", "Task_10ms"), "partition", "PARTITION0", "partition PARTITION0"),
            ("CORE0", ElementRef("hardware", "PARTITION0"), "core", "CORE0", "core CORE0"),
            ("CORE0", ElementRef("hardware", "RAM0"), "access", "CORE0", "accessible from CORE0"),
        ],
    )
    def test_every_single_detail_case_is_a_one_element_tuple_of_the_single_detail_string(
        self, wide_state, start, reached, kind, value, rendered
    ) -> None:
        """Every single-detail row of ``_EDGE_DETAILS``, one case each."""
        graph = build_graphs(wide_state).reverse
        start_ref = next(node for node in graph if node.name == start)
        details = graph[start_ref][reached]
        assert details == (EdgeDetail(kind, value),)
        assert query_edges._render_details(details) == rendered

    def test_no_pair_of_the_frozen_fixture_holds_more_than_one_detail(self, wide_state) -> None:
        """``impact_model`` holds no pair with more than one detail."""
        for node, edges in build_graphs(wide_state).reverse.items():
            for reached, details in edges.items():
                assert len(details) <= 1, (node, reached, details)

    def test_an_element_reached_again_gains_the_later_parents_details(self, wide_state) -> None:
        """An element reached again gains the later parents' details, on both walks.

        From ``Speed``, ``Task_10ms`` is reachable through ``SpeedSensor`` and through
        ``SpeedControl``, and ``Sensor_to_Control`` is first reached from ``Powertrain`` through the
        connector's own ``composition`` field, which carries no detail. Dropping the later edges would
        make the connector that carries the interface read exactly like one that does not. Each row
        carries the details of every edge that reached it, in the order the walk met them, and the
        rows and their order do not move.
        """
        graph = build_graphs(wide_state).reverse
        task = ElementRef("task", "Task_10ms")
        connector = ElementRef("connector", "Sensor_to_Control")
        assert graph[ElementRef("composition", "Powertrain")][connector] == ()

        rows = {row.ref: row.details for row in dependents(wide_state, "Speed")["implementation_type"]}
        assert rows[task] == (EdgeDetail("runnable", "SpeedSensor_Run"), EdgeDetail("runnable", "SpeedControl_Run"))
        assert rows[connector] == (
            EdgeDetail("endpoint", "SpeedSensor.pOut"),
            EdgeDetail("endpoint", "SpeedControl.rIn"),
        )
        bounded = {row.ref: row.details for row in dependents(wide_state, "Speed", depth=3)["implementation_type"]}
        assert bounded[connector] == (
            EdgeDetail("endpoint", "SpeedControl.rIn"),
            EdgeDetail("endpoint", "SpeedSensor.pOut"),
        )

    def test_a_connector_reached_from_its_composition_first_names_its_endpoint(self) -> None:
        """``sub_link`` on the compositions fixture: reached through its composition, it names its endpoint.

        ``Sub`` has a boundary port on ``ISignal`` and is popped before ``Beta``
        on the transitive walk, so ``sub_link`` — drawn inside ``Sub`` — is first
        reached through its ``composition`` field. It still names its endpoint on
        ``Beta``, the reached component, and the port it names is not the one
        ``Beta``'s own line names: the read that tells this connector does not
        carry the interface.
        """
        snapshot = load_snapshot(str(_REPO_ROOT / "packages/tools/tests/fixtures/compositions/model"), rev=None)
        rows = query.dependents(snapshot.state, "ISignal")["interface"]
        order = [row.ref for row in rows]
        assert order.index(ElementRef("composition", "Sub")) < order.index(ElementRef("connector", "sub_link"))
        details = {row.ref: row.details for row in rows}
        assert details[ElementRef("connector", "sub_link")] == (EdgeDetail("endpoint", "Beta.pInner"),)
        assert details[ElementRef("swc", "Beta")] == (EdgeDetail("port", "pOut"),)

    def test_an_edge_from_an_element_at_the_bound_is_not_read(self, wide_state) -> None:
        """The bounded walk's named limit: it reads the edges of the elements it expands.

        At ``depth=2`` the two components and ``Powertrain`` are all two hops from
        ``Speed`` — the bound — so none of them is expanded, and ``Powertrain``'s
        row does not name the two members through which the components lead back
        to it. At ``depth=3`` the components are expanded and the row names both.
        """
        powertrain = ElementRef("composition", "Powertrain")
        at_two = {row.ref: row.details for row in dependents(wide_state, "Speed", depth=2)["implementation_type"]}
        at_three = {row.ref: row.details for row in dependents(wide_state, "Speed", depth=3)["implementation_type"]}
        assert at_two[powertrain] == (EdgeDetail("port", "pSpeedOut"),)
        assert at_three[powertrain] == (
            EdgeDetail("port", "pSpeedOut"),
            EdgeDetail("member", "control"),
            EdgeDetail("member", "sensor"),
        )

    def test_the_start_is_never_a_row_whatever_leads_back_to_it(self) -> None:
        """A cycle back to the matched element adds no row and no detail."""
        loop = Connector.model_validate(
            {"name": "Loop", "from": {"swc": "A", "port": "pOut"}, "to": {"swc": "A", "port": "rIn"}}
        )
        state = _state(swcs=[SWComponent(name="A", type="application")], connectors=[loop])
        assert [row.ref for row in dependencies(state, "Loop")["connector"]] == [ElementRef("swc", "A")]


class TestTheRenderingTable:
    """One detail renders the singular; several render the plural; kinds in table order."""

    @pytest.mark.parametrize(
        ("kind", "value", "rendered"),
        [
            ("port", "pA", "via port pA"),
            ("endpoint", "A.pA", "via port A.pA"),
            ("runnable", "R1", "runnable R1"),
            ("member", "M1", "member M1"),
            ("step", "R1", "step: R1"),
            ("budget", "512", "memory budget 512 bytes"),
            ("core", "C0", "core C0"),
            ("access", "C0", "accessible from C0"),
            ("partition", "P0", "partition P0"),
        ],
    )
    def test_one_detail_of_each_kind_renders_the_single_detail_bytes(self, kind, value, rendered) -> None:
        assert query_edges._render_details((EdgeDetail(kind, value),)) == rendered

    def test_the_table_has_exactly_the_nine_kinds_in_its_declared_order(self) -> None:
        """The nine kinds, in the declared order."""
        assert list(query_edges._DETAIL_RENDERING) == [
            "port",
            "endpoint",
            "runnable",
            "member",
            "step",
            "budget",
            "core",
            "access",
            "partition",
        ]

    @pytest.mark.parametrize(
        ("kind", "values", "rendered"),
        [
            ("port", ("pA", "pB", "pC"), "via ports pA, pB, pC"),
            ("endpoint", ("A.pOut", "B.rIn"), "via ports A.pOut, B.rIn"),
            ("runnable", ("R1", "R2"), "runnables R1, R2"),
            ("step", ("R1", "R2"), "steps: R1, R2"),
            # Reached on a walk row only, from several parents (see _DETAIL_RENDERING).
            ("member", ("M1", "M2"), "members M1, M2"),
            ("budget", ("512", "1024"), "memory budgets 512, 1024 bytes"),
            ("core", ("C0", "C1"), "cores C0, C1"),
            ("access", ("C0", "C1"), "accessible from C0, C1"),
            ("partition", ("P0", "P1"), "partitions P0, P1"),
        ],
    )
    def test_several_details_of_one_kind_render_the_plural(self, kind, values, rendered) -> None:
        assert query_edges._render_details(tuple(EdgeDetail(kind, value) for value in values)) == rendered

    def test_mixed_kinds_render_in_table_order_joined_by_a_semicolon(self) -> None:
        details = (EdgeDetail("runnable", "R1"), EdgeDetail("port", "pA"), EdgeDetail("port", "pB"))
        assert query_edges._render_details(details) == "via ports pA, pB; runnable R1"

    def test_values_keep_emission_order_and_are_never_sorted(self) -> None:
        details = (EdgeDetail("port", "pZ"), EdgeDetail("port", "pA"))
        assert query_edges._render_details(details) == "via ports pZ, pA"

    def test_a_qualified_detail_renders_its_owner_first(self) -> None:
        details = (EdgeDetail("budget", "2048", owner="A"), EdgeDetail("budget", "2048", owner="B"))
        assert query_edges._render_details(details) == "memory budgets A.2048, B.2048 bytes"
        assert EdgeDetail("port", "p").text == "p"

    def test_no_detail_renders_nothing(self) -> None:
        assert query_edges._render_details(()) == ""

    def test_a_kind_the_table_does_not_declare_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match="nonesuch"):
            EdgeDetail("nonesuch", "x")

    @pytest.mark.parametrize(
        "details",
        ["", "via port p", [EdgeDetail("port", "p")], ("via port p",)],
        ids=["empty-string", "rendered-string", "list", "tuple-of-strings"],
    )
    def test_anything_but_a_tuple_of_edge_details_is_refused(self, details) -> None:
        """A positional ``Dependent(ref, "")`` must not silently mean no detail."""
        with pytest.raises(TypeError):
            Dependent(ElementRef("swc", "S"), details)  # type: ignore[arg-type]

    def test_both_text_renderers_print_the_plural_line(self) -> None:
        walked = dependents(_three_port_state(), "IBrakeRequest")
        line = "  swc BrakeController (via ports pBrakeRequest, pBrakeRequestAux, pBrakeRequestDiag)"
        assert line in format_impact("IBrakeRequest", walked).splitlines()
        assert line in format_related("IBrakeRequest", walked).splitlines()


class TestTheConnectorEndpointDetail:
    """``field:connectors.connector_endpoint.swc`` / ``.composition`` name the endpoint, owner first."""

    def test_a_components_connectors_carry_the_endpoint(self, wide_state) -> None:
        rows = {row.ref: row.details for row in dependents(wide_state, "SpeedSensor", depth=1)["swc"]}
        assert rows[ElementRef("connector", "Sensor_to_Control")] == (EdgeDetail("endpoint", "SpeedSensor.pOut"),)

    def test_a_compositions_connectors_carry_the_endpoint(self, wide_state) -> None:
        rows = {row.ref: row.details for row in dependents(wide_state, "Diagnostics", depth=1)["composition"]}
        assert rows[ElementRef("connector", "Diag_to_Powertrain")] == (EdgeDetail("endpoint", "Diagnostics.pDiag"),)

    def test_the_connectors_own_composition_field_carries_none(self, wide_state) -> None:
        """``connector.composition`` names where the connector is drawn, not a port."""
        rows = {row.ref: row.details for row in dependents(wide_state, "Powertrain", depth=1)["composition"]}
        assert rows[ElementRef("connector", "Sensor_to_Control")] == ()
        assert rows[ElementRef("connector", "Diag_to_Powertrain")] == (EdgeDetail("endpoint", "Powertrain.rDiagIn"),)

    def test_the_showcase_cases_connector_carries_its_port(self) -> None:
        rendered = format_impact("BrakeController", dependents(_three_port_state(), "BrakeController"))
        assert "  connector Brake_link (via port BrakeController.pBrakeRequestAux)" in rendered.splitlines()

    def test_a_repeated_port_name_is_told_apart_by_its_owner(self) -> None:
        """Both components declare a ``pBrakeRequest``; the connector names the one it wires."""
        rendered = format_impact("IBrakeRequest", dependents(_three_port_state(), "IBrakeRequest")).splitlines()
        assert "  swc BrakeConsumer (via port pBrakeRequest)" in rendered
        assert (
            "  connector Brake_link (via ports BrakeConsumer.pBrakeRequest, BrakeController.pBrakeRequestAux)"
            in rendered
        )

    def test_the_forward_direction_names_the_same_endpoint(self, wide_state) -> None:
        rows = {row.ref: row.details for row in dependencies(wide_state, "Sensor_to_Control", depth=1)["connector"]}
        assert rows[ElementRef("swc", "SpeedSensor")] == (EdgeDetail("endpoint", "SpeedSensor.pOut"),)
        assert rows[ElementRef("swc", "SpeedControl")] == (EdgeDetail("endpoint", "SpeedControl.rIn"),)


def _equal_values_state(*, b_runnable: str = "Run", b_port: str = "pOut") -> ModelState:
    """Two components whose edges carry equal text: one runnable name, one port name, one budget.

    ``A`` and ``B`` each declare a runnable ``Run`` mapped into ``T``, a port
    ``pOut`` on ``ISig`` and a ``2048``-byte budget on ``RAM0`` — names a model
    may repeat across components. The keywords give ``B`` distinct names, the
    case in which nothing needs qualifying.
    """
    a = SWComponent(
        name="A",
        type="application",
        ports=[Port(name="pOut", direction="provide", interface="ISig")],
        runnables=[Runnable(name="Run")],
        memory={"RAM0": 2048},
    )
    b = SWComponent(
        name="B",
        type="application",
        ports=[Port(name=b_port, direction="require", interface="ISig")],
        runnables=[Runnable(name=b_runnable)],
        memory={"RAM0": 2048},
    )
    task = Task(
        name="T",
        period=0.01,
        priority=1,
        partition="PARTITION0",
        runnables=[TaskRunnableRef(swc="A", runnable="Run"), TaskRunnableRef(swc="B", runnable=b_runnable)],
    )
    iface = SRInterface(
        name="ISig", kind="sender_receiver", data_elements=[DataElement(name="v", implementation_type="uint8")]
    )
    hardware = [
        Processor(name="CPU", kind="processor"),
        Core(name="CORE0", kind="core", threshold=0.7),
        Memory(name="RAM0", kind="memory", type="RAM", size=8192, accessible=["CORE0"], threshold=0.8),
        Partition(name="PARTITION0", kind="partition", core="CORE0"),
    ]
    return _state(swcs=[a, b], interfaces=[iface], tasks=[task], hardware=hardware)


class TestADetailIsKeptPerEdgeNotPerText:
    """Equal values from two parents are two edges: both kept, and that kind qualified by parent."""

    def test_two_components_sharing_a_runnable_name_in_one_task(self) -> None:
        state = _equal_values_state()
        task = ElementRef("task", "T")
        transitive = {row.ref: row.details for row in dependents(state, "ISig")["interface"]}
        assert transitive[task] == (
            EdgeDetail("runnable", "Run", owner="B"),
            EdgeDetail("runnable", "Run", owner="A"),
        )
        bounded = {row.ref: row.details for row in dependents(state, "ISig", depth=2)["interface"]}
        assert bounded[task] == (
            EdgeDetail("runnable", "Run", owner="A"),
            EdgeDetail("runnable", "Run", owner="B"),
        )
        assert "  task T (runnables A.Run, B.Run)" in format_impact("ISig", dependents(state, "ISig", depth=2))

    def test_two_components_charging_equal_budgets_to_one_memory(self) -> None:
        """A consumer summing the budgets gets both, not half."""
        state = _equal_values_state()
        rows = {row.ref: row.details for row in dependencies(state, "T", depth=2)["task"]}
        budgets = rows[ElementRef("hardware", "RAM0")]
        assert budgets == (EdgeDetail("budget", "2048", owner="A"), EdgeDetail("budget", "2048", owner="B"))
        assert sum(int(detail.value) for detail in budgets) == 4096
        rendered = format_related("T", dependencies(state, "T", depth=2), direction="dependencies")
        assert "  hardware RAM0 (memory budgets A.2048, B.2048 bytes)" in rendered.splitlines()

    def test_the_dependencies_direction_port_names_its_component(self) -> None:
        rows = {row.ref: row.details for row in dependencies(_equal_values_state(), "T", depth=2)["task"]}
        assert rows[ElementRef("interface", "ISig")] == (
            EdgeDetail("port", "pOut", owner="A"),
            EdgeDetail("port", "pOut", owner="B"),
        )

    def test_only_the_kind_that_collided_is_qualified(self) -> None:
        """``T`` is reached from ``PARTITION0`` (one partition) and from both components (two ``Run``).

        The task names a partition and the partition the core, so the walk from ``CORE0`` reaches
        ``T`` through ``PARTITION0``.
        """
        rendered = format_impact("CORE0", dependents(_equal_values_state(), "CORE0")).splitlines()
        assert "  task T (runnables B.Run, A.Run; partition PARTITION0)" in rendered

    def test_distinct_values_from_several_parents_stay_bare(self) -> None:
        """No equal text, nothing to keep apart: the row stays bare."""
        state = _equal_values_state(b_runnable="Init", b_port="rIn")
        assert "  task T (runnables Init, Run)" in format_impact("ISig", dependents(state, "ISig")).splitlines()
        rows = {row.ref: row.details for row in dependencies(state, "T")["task"]}
        assert rows[ElementRef("interface", "ISig")] == (EdgeDetail("port", "rIn"), EdgeDetail("port", "pOut"))

    def test_a_row_reached_from_one_parent_is_never_qualified(self) -> None:
        state = _equal_values_state()
        for walked in (dependencies(state, "A", depth=1), dependents(state, "RAM0", depth=1)):
            for rows in walked.values():
                assert all(detail.owner is None for row in rows for detail in row.details), rows

    def test_the_walk_document_carries_the_owner_only_where_it_qualifies(self) -> None:
        state = _equal_values_state()
        document = json.loads(
            format_related_json("T", dependencies(state, "T", depth=2), direction="dependencies", depth=2)
        )
        rows = {row["name"]: row for row in document["matched"]["task"]}
        assert rows["RAM0"]["details"] == [
            {"kind": "budget", "value": "2048", "owner": "A"},
            {"kind": "budget", "value": "2048", "owner": "B"},
        ]
        assert rows["RAM0"]["detail"] == "memory budgets A.2048, B.2048 bytes"
        assert rows["PARTITION0"]["details"] == [{"kind": "partition", "value": "PARTITION0"}]

    def test_two_qualifying_parents_sharing_a_name_across_layers_are_told_apart_by_layer(self) -> None:
        """A component and a composition named ``X`` load (``check-model`` reports them): two owners ``X``.

        ``Root`` places both, each declares a port ``p`` on ``I``, and a connector
        wires the two ports — so the interface row (dependencies) and the
        connector row (dependents) each carry one value from two parents named
        ``X``, and only the layer tells them apart.
        """
        swc = SWComponent(name="X", ports=[Port(name="p", direction="provide", interface="I")])
        inner = Composition(name="X", title="X", ports=[CompositionPort(name="p", direction="require", interface="I")])
        root = Composition(
            name="Root",
            title="Root",
            root=True,
            members=[CompositionMember(name="a", type="X"), CompositionMember(name="b", composition="X")],
        )
        link = Connector.model_validate(
            {"name": "L", "from": {"swc": "X", "port": "p"}, "to": {"composition": "X", "port": "p"}}
        )
        iface = SRInterface(
            name="I", kind="sender_receiver", data_elements=[DataElement(name="v", implementation_type="uint8")]
        )
        state = _state(swcs=[swc], compositions=[inner, root], interfaces=[iface], connectors=[link])

        forward = format_related("Root", dependencies(state, "Root"), direction="dependencies").splitlines()
        assert "  interface I (via ports composition X.p, swc X.p)" in forward
        assert "  swc X (member a)" in forward
        reverse = format_impact("I", dependents(state, "I")).splitlines()
        assert "  connector L (via ports composition X.X.p, swc X.X.p)" in reverse
        assert "  swc X (via port p)" in reverse

        document = json.loads(format_related_json("I", dependents(state, "I")))
        connector = next(row for row in document["matched"]["interface"] if row["name"] == "L")
        assert connector["details"] == [
            {"kind": "endpoint", "value": "X.p", "owner": "X", "ownerLayer": "composition"},
            {"kind": "endpoint", "value": "X.p", "owner": "X", "ownerLayer": "swc"},
        ]

    def test_the_layer_is_named_only_beside_a_shared_name(self) -> None:
        """Owners ``A`` and ``B`` are distinct names, so the qualified values carry no layer."""
        rows = {row.ref: row.details for row in dependencies(_equal_values_state(), "T", depth=2)["task"]}
        assert all(detail.owner_layer is None for details in rows.values() for detail in details)


#: ``format_impact`` as recorded before the details accumulated. The only lines a report may change
#: are the ones :data:`_EXTENDED_LINES` names — a connector line gaining the endpoints it was
#: reached through, and a line gaining the details of an edge from a later parent. No graph pair of
#: these fixtures collapses.
_RECORDED_REPORTS: dict[tuple[str, str], str] = {
    ("examples/basic/model", "IExample"): (
        "Impact of 'IExample':\n\ninterface:\n"
        "  swc ExampleConsumerSWC (via port rIn)\n  swc ExampleSWC (via port pOut)\n"
        "  connector Example_to_Consumer\n  composition Root (member ExampleSWC)\n"
        "  requirement REQ_EXAMPLE_001\n  task Task_10ms (runnable ExampleConsumerRun)\n"
        "  sequence task10ms_consumer_cycle (step: ExampleConsumerRun)"
    ),
    ("examples/basic/model", "ExampleSWC"): (
        "Impact of 'ExampleSWC':\n\nswc:\n"
        "  connector Example_to_Consumer\n  composition Root (member ExampleSWC)\n  requirement REQ_EXAMPLE_001"
    ),
    ("examples/basic/model", "Task_10ms"): "Impact of 'Task_10ms':\n\ntask:\n  sequence task10ms_consumer_cycle",
    ("packages/tools/tests/fixtures/showcase/model", "IBrakeRequest"): (
        "Impact of 'IBrakeRequest':\n\ninterface:\n"
        "  swc BrakeController (via port pBrakeRequest)\n  task Task_50ms (runnable ProcessBrakeRequest)\n"
        "  composition VehicleControl (member BrakeController)\n  requirement REQ_SHOWCASE_BASELINE_001\n"
        "  sequence baseline_50ms_cycle (step: ProcessBrakeRequest)"
    ),
    ("packages/tools/tests/fixtures/showcase/model", "BrakeController"): (
        "Impact of 'BrakeController':\n\nswc:\n"
        "  task Task_50ms (runnable ProcessBrakeRequest)\n  composition VehicleControl (member BrakeController)\n"
        "  requirement REQ_SHOWCASE_BASELINE_001\n  sequence baseline_50ms_cycle (step: ProcessBrakeRequest)"
    ),
    ("packages/tools/tests/fixtures/showcase/model", "Task_50ms"): (
        "Impact of 'Task_50ms':\n\ntask:\n  sequence baseline_50ms_cycle"
    ),
    ("packages/tools/tests/fixtures/compositions/model", "ISignal"): (
        "Impact of 'ISignal':\n\ninterface:\n"
        "  swc Beta (via port pOut)\n  swc Consumer (via port rIn)\n  composition Sink (via port rSignalIn)\n"
        "  composition Sub (via port pSignalOut)\n  connector root_link\n  connector sub_link\n"
        "  composition Root (member Sub)\n  requirement REQ_COMPOSITIONS_001"
    ),
    ("packages/tools/tests/fixtures/compositions/model", "Consumer"): (
        "Impact of 'Consumer':\n\nswc:\n"
        "  composition Sink (member Consumer)\n  requirement REQ_COMPOSITIONS_001\n"
        "  connector root_link\n  composition Root (member Sink)"
    ),
    ("packages/tools/tests/fixtures/compositions/model", "Sub"): (
        "Impact of 'Sub':\n\ncomposition:\n  connector root_link\n  connector sub_link\n  composition Root (member Sub)"
    ),
}

#: The permitted extensions, per report. ``sub_link`` stays bare under ``Sub``:
#: the walk from ``Sub`` reaches neither ``Beta`` nor ``Gamma``, so the one edge
#: into it is its own ``composition`` field, which names no port. Under
#: ``ISignal`` it names ``Beta.pInner`` — reached from ``Sub`` first, it gains the
#: endpoint when ``Beta`` is expanded later — a port ``Beta``'s own line does not
#: carry, which is how a reader tells it does not carry the interface.
_EXTENDED_LINES: dict[tuple[str, str], dict[str, str]] = {
    ("examples/basic/model", "IExample"): {
        "  connector Example_to_Consumer": (
            "  connector Example_to_Consumer (via ports ExampleSWC.pOut, ExampleConsumerSWC.rIn)"
        ),
        "  composition Root (member ExampleSWC)": "  composition Root (members ExampleSWC, ExampleConsumerSWC)",
    },
    ("examples/basic/model", "ExampleSWC"): {
        "  connector Example_to_Consumer": "  connector Example_to_Consumer (via port ExampleSWC.pOut)"
    },
    ("packages/tools/tests/fixtures/compositions/model", "ISignal"): {
        "  composition Sink (via port rSignalIn)": "  composition Sink (via port rSignalIn; member Consumer)",
        "  composition Sub (via port pSignalOut)": "  composition Sub (via port pSignalOut; member Alpha)",
        "  connector root_link": "  connector root_link (via ports Sub.pSignalOut, Sink.rSignalIn)",
        "  connector sub_link": "  connector sub_link (via port Beta.pInner)",
        "  composition Root (member Sub)": "  composition Root (members Sub, Sink)",
    },
    ("packages/tools/tests/fixtures/compositions/model", "Consumer"): {
        "  connector root_link": "  connector root_link (via port Sink.rSignalIn)"
    },
    ("packages/tools/tests/fixtures/compositions/model", "Sub"): {
        "  connector root_link": "  connector root_link (via port Sub.pSignalOut)"
    },
}

_IMPACT_MODEL_ROOT = "packages/tools/tests/fixtures/recorded_oracles/impact_model"
#: ``format_impact_summary``'s text and ``page_impact``'s windows (``limit=2``, followed to the last
#: page), as SHA-256 prefixes. Neither reads a detail, so neither may move *for a detail*. A row is
#: re-recorded only when the model or a layer token under it moves, and only after the old pair is
#: reconstructed from today's result with that move undone.
_RECORDED_NARROW_FORMS: dict[tuple[str, str], tuple[str, str]] = {
    ("examples/basic/model", "IExample"): ("9f8e614575fee71b", "7bddfc4732d2b515"),
    ("examples/basic/model", "ExampleSWC"): ("5f373ed7c58dc144", "6b6122d976b2e3e8"),
    ("examples/basic/model", "Task_10ms"): ("8e495e69a83402dc", "498e5dc40450d51e"),
    ("packages/tools/tests/fixtures/showcase/model", "IBrakeRequest"): ("5929e1210d970ab2", "6f590f7d58826861"),
    ("packages/tools/tests/fixtures/showcase/model", "BrakeController"): ("62bad65601a7230e", "733d94f5d1e65b22"),
    ("packages/tools/tests/fixtures/showcase/model", "Task_50ms"): ("3fad846b12cd47ca", "c03e65c16f9cbd5b"),
    ("packages/tools/tests/fixtures/compositions/model", "ISignal"): ("c96954bfe0053455", "534e7077de67d63d"),
    ("packages/tools/tests/fixtures/compositions/model", "Consumer"): ("bb72564db4b5bdf0", "7627e5e340436cdd"),
    ("packages/tools/tests/fixtures/compositions/model", "Sub"): ("13a2a4c4e37dce88", "6fd35c1cbb182769"),
    (_IMPACT_MODEL_ROOT, "Speed"): ("0b9e4dab743f0e6d", "d80bb37d073f5191"),
    (_IMPACT_MODEL_ROOT, "SpeedSensor"): ("096dd52b82cfd50d", "f28adeed45e7467d"),
    (_IMPACT_MODEL_ROOT, "Shared"): ("225db5f3f186e104", "65e609fb3ee9e8c1"),
    (_IMPACT_MODEL_ROOT, "RAM0"): ("ac4154f36a2856ec", "616feac51a690b7e"),
    (_IMPACT_MODEL_ROOT, "CORE0"): ("1f648451be5ad5c3", "5b1d4c33b0600065"),
}

_REPO_ROOT = Path(__file__).resolve().parents[3]


#: The rendering table written out independently of ``query.edges._DETAIL_RENDERING``,
#: so the walk document's ``details`` can be checked against its ``detail``
#: without asking the renderer under test to grade itself.
_INDEPENDENT_TEMPLATES: dict[str, tuple[str, str]] = {
    "port": ("via port {}", "via ports {}"),
    "endpoint": ("via port {}", "via ports {}"),
    "runnable": ("runnable {}", "runnables {}"),
    "member": ("member {}", "members {}"),
    "step": ("step: {}", "steps: {}"),
    "budget": ("memory budget {} bytes", "memory budgets {} bytes"),
    "core": ("core {}", "cores {}"),
    "access": ("accessible from {}", "accessible from {}"),
    "partition": ("partition {}", "partitions {}"),
}


def _independent_rendering(details: list[dict[str, str]]) -> str:
    parts = []
    for kind, (one, several) in _INDEPENDENT_TEMPLATES.items():
        values = [
            f"{detail['owner']}.{detail['value']}" if "owner" in detail else detail["value"]
            for detail in details
            if detail["kind"] == kind
        ]
        if values:
            parts.append((one if len(values) == 1 else several).format(", ".join(values)))
    assert {detail["kind"] for detail in details} <= set(_INDEPENDENT_TEMPLATES), details
    return "; ".join(parts)


def _report_line(row: dict[str, Any]) -> str:
    rendered = _independent_rendering(row["details"])
    assert rendered == row["detail"], row
    suffix = f" ({rendered})" if rendered else ""
    return f"  {row['layer']} {row['name']}{suffix}"


class TestTheRecordedReportsMoveOnlyWhereTheLogSays:
    """The recorded byte-identity baseline, with every permitted delta named."""

    @pytest.mark.parametrize("case", sorted(_RECORDED_REPORTS))
    def test_every_recorded_line_is_unchanged_or_a_named_extension(self, case) -> None:
        root, name = case
        extensions = _EXTENDED_LINES.get(case, {})
        recorded = _RECORDED_REPORTS[case].split("\n")
        assert set(extensions) <= set(recorded), "an extension names a line the record does not carry"
        expected = "\n".join(extensions.get(line, line) for line in recorded)
        # Requirement rows left these reports when the link moved into the element; they are
        # restored where they stood (asserted absent), not re-recorded.
        result = restore_requirement_rows(impact_of(load_snapshot(str(_REPO_ROOT / root), rev=None), name), root, name)
        assert format_impact(name, result) == expected

    @pytest.mark.parametrize("case", sorted(_RECORDED_REPORTS))
    def test_the_walk_document_rows_say_what_the_report_lines_say(self, case) -> None:
        root, name = case
        snapshot = load_snapshot(str(_REPO_ROOT / root), rev=None)
        document = json.loads(format_related_json(name, query.related(snapshot.state, name, "dependents", None)))
        assert len(document["matched"]) == 1, "the line slice below assumes one matched layer"
        (rows,) = document["matched"].values()
        assert rows, "the line slice below assumes the report is not '(no dependents)'"
        lines = format_impact(name, impact_of(snapshot, name)).split("\n")[3:]
        assert [_report_line(row) for row in rows] == lines

    @pytest.mark.parametrize("case", sorted(_RECORDED_NARROW_FORMS))
    def test_the_summary_and_the_page_do_not_move(self, case) -> None:
        root, name = case
        # The requirement rows, restored where they stood so the recorded digests still grade every
        # other row.
        result = restore_requirement_rows(impact_of(load_snapshot(str(_REPO_ROOT / root), rev=None), name), root, name)
        summary = impact.format_impact_summary(name, result)
        windows = []
        after = None
        while True:
            window, page = impact.page_impact(result, 2, after)
            # The digests are a record and are not re-recorded: the digested
            # scope is the one recorded before a truncated page named its
            # per-kind totals, and the assertion below says the scope the page
            # now renders is that one with exactly the `in all:` part inserted.
            legacy = impact.format_impact_scope(page=dataclasses.replace(page, kinds=()))
            scope = impact.format_impact_scope(page=page)
            if page.truncated:
                in_all = "in all: " + ", ".join(f"{token} {count}" for token, count in page.kinds)
                figures = f"page: {page.returned} of {page.total}, truncated"
                assert scope == legacy.replace(figures, f"{figures}; {in_all}", 1), (legacy, scope)
                assert sum(count for _, count in page.kinds) == page.total
            else:
                assert scope == legacy
            windows.append(
                {
                    "rows": {layer: [[d.ref.layer, d.ref.name] for d in deps] for layer, deps in window.items()},
                    "page": [page.total, page.returned, page.truncated, page.next],
                    "scope": legacy,
                }
            )
            if page.next is None:
                break
            after = page.next
        digests = (
            hashlib.sha256(summary.encode("utf-8")).hexdigest()[:16],
            hashlib.sha256(json.dumps(windows, sort_keys=True).encode("utf-8")).hexdigest()[:16],
        )
        assert digests == _RECORDED_NARROW_FORMS[case]


class TestTheWalkDocumentAtVersionTwo:
    """The row gains ``details``; the one shared version constant moves to 2."""

    def test_the_walk_row_carries_the_rendered_detail_and_the_structured_details(self) -> None:
        document = json.loads(format_related_json("IBrakeRequest", dependents(_three_port_state(), "IBrakeRequest")))
        assert document["schemaVersion"] == 3
        assert document["matched"]["interface"][0] == {
            "layer": "swc",
            "name": "BrakeController",
            "detail": "via ports pBrakeRequest, pBrakeRequestAux, pBrakeRequestDiag",
            "details": [
                {"kind": "port", "value": "pBrakeRequest"},
                {"kind": "port", "value": "pBrakeRequestAux"},
                {"kind": "port", "value": "pBrakeRequestDiag"},
            ],
        }

    def test_a_row_with_no_detail_carries_an_empty_list(self, wide_state) -> None:
        document = json.loads(format_related_json("Task_10ms", dependents(wide_state, "Task_10ms")))
        assert document["matched"]["task"] == [
            {"layer": "sequence", "name": "speed_cycle", "detail": "", "details": []}
        ]

    def test_the_slice_document_moves_its_version_and_nothing_else(self, wide_state) -> None:
        # Version 3 carries the three-key member identity; over a layer without members the
        # document's shape is the earlier one.
        assert query.QUERY_SCHEMA_VERSION == 3
        assert json.loads(format_query_json(slice_layer(wide_state, "tasks"))) == {
            "schemaVersion": 3,
            "layer": "tasks",
            "identityField": "name",
            "elements": ["Task_10ms", "Task_20ms"],
        }


# ---------------------------------------------------------------------------
# The member path — `--members runnables.writes`
# ---------------------------------------------------------------------------
#
# The mirror of `test_editing_read.py`'s path section, at this surface: the
# kernel's resolution of a dotted `members` and the protocol's are two
# statements of one rule (Rule 2 keeps the `query` package out of `editing/`), so beside
# the answers each case asserts, `test_every_declared_path_answers_what_the_protocol_answers`
# holds the two to one answer over every path the live registry declares.

#: The shipped vector whose seed the path cases read: the provider writes over
#: two runnables, the consumer reads behind a delegation.
PATH_SEED = "read-swc-members-path-runnable-writes"


def _path_model_root(tmp_path: Path, vector: str = PATH_SEED) -> Path:
    root = fixtures.build_fixture(tmp_path / vector, vector)
    return Path(require_project(str(root / "swcraft.yaml")).resolved_model_root)


def _edit(path: Path, change: Any) -> None:
    """Load one seeded element file, let *change* edit it, and write it back."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(document)
    path.write_text(yaml.safe_dump(document, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n")


def _with_disabled_modes(model_root: Path) -> None:
    """The provider's first runnable gains a timing event with a disabled mode, and an init event."""

    def change(document: dict[str, Any]) -> None:
        document["runnables"][0]["events"] = [
            {
                "type": "timing",
                "period": 0.01,
                "disabled_modes": [{"port": "mMode", "mode_group_ref": "/Modes/Group", "mode_ref": "/Modes/Group/OFF"}],
            },
            {"type": "init"},
        ]
        document["ports"].append({"name": "mMode", "direction": "require", "interface": "IExample"})

    _edit(model_root / "swc" / "example" / "ExampleSWC.yaml", change)


def _with_port_details(model_root: Path) -> None:
    """The consumer's ``rIn`` gains two measures and a com spec, and a runnable a call through a new port.

    So every dotted path the shipped contract declares has rows on a seed: ``ports.measures`` (the one
    map-ending path, rows ``{element, through, key}``), ``ports.com_specs`` (a field identity on
    ``data_element``), ``runnables.calls`` (the composite identity ``(port, operation)``),
    ``ports.satisfies`` and ``runnables.satisfies`` (a whole-value string, the link itself),
    ``runnables.reads_variables`` and ``runnables.writes_variables`` (a field identity on
    ``variable``), and ``runnables.can_enter`` and ``runnables.runs_inside`` (a field identity on
    ``area``).
    """

    def change(document: dict[str, Any]) -> None:
        document["ports"][0]["measures"] = {"value": "e2e", "other": "crc"}
        document["ports"][0]["com_specs"] = [{"data_element": "value", "queue_length": 2}]
        document["ports"][0]["satisfies"] = ["REQ_EXAMPLE_001@1"]
        document["ports"].append({"name": "rDiag", "direction": "require", "interface": "IDiagnosticService"})
        document["runnables"][0]["calls"] = [
            {"port": "rDiag", "operation": "ReadDtc"},
            {"port": "rDiag", "operation": "ClearDtc"},
        ]
        document["runnables"][0]["satisfies"] = ["REQ_EXAMPLE_001@1"]
        document["variables"] = [{"name": "Filtered", "access": "implicit", "implementation_type": "uint8"}]
        document["runnables"][0]["reads_variables"] = [{"variable": "Filtered"}]
        document["runnables"][0]["writes_variables"] = [{"variable": "Filtered"}]
        document["exclusive_areas"] = [{"name": "FilterState"}]
        document["runnables"][0]["can_enter"] = [{"area": "FilterState"}]
        document["runnables"][0]["runs_inside"] = [{"area": "FilterState"}]

    _edit(model_root / "swc" / "example" / "ExampleConsumerSWC.yaml", change)


def _with_an_interface_stub(model_root: Path) -> None:
    """An external interface stub listing member NAMES under both shared collection names."""
    write_model_element(
        model_root,
        {
            "name": "IOutside",
            "kind": "external_interface",
            "external_ref": "/Outside/IOutside",
            "external_dest": "SENDER-RECEIVER-INTERFACE",
            "data_elements": ["outsideSpeed"],
        },
    )
    write_model_element(
        model_root,
        {
            "name": "IOutsideServer",
            "kind": "external_interface",
            "external_ref": "/Outside/IOutsideServer",
            "external_dest": "CLIENT-SERVER-INTERFACE",
            "operations": ["Fetch"],
        },
    )


def _declared_paths() -> list[tuple[str, str]]:
    """Every ``(layer, members)`` path the live registry declares, in the kernel's vocabulary.

    The first step is a collection of a layer's top-level kinds and each later
    step a collection of the member kinds the step before it holds, following
    the first declaration of each name; a map ends a path. Derived, never listed.
    """
    registry = current_registry()
    known = registry.kinds
    found: list[tuple[str, str]] = []
    for layer in registry.layers:
        pending: list[tuple[str, tuple[Any, ...]]] = [("", tuple(layer.top_level_kinds))]
        while pending:
            prefix, kinds = pending.pop(0)
            chosen: dict[str, Any] = {}
            for kind in kinds:
                for facts in kind.fields:
                    if facts.collection is not None and facts.name not in chosen:
                        chosen[facts.name] = facts
            for name, facts in chosen.items():
                found.append((layer.name, f"{prefix}{name}"))
                members = tuple(known[k] for k in facts.collection.member_kinds if k in known)
                if members and facts.collection.collection_kind != "map":
                    pending.append((f"{prefix}{name}.", members))
    return found


class TestTheMemberPath:
    """``slice_layer(..., members="runnables.writes")`` — rows with their owner chain."""

    def test_a_path_answers_every_write_and_read_with_its_owner_chain(self, tmp_path: Path) -> None:
        state = load_model_state(str(_path_model_root(tmp_path)))
        assert slice_layer(state, "swc", members="runnables.writes").identities == (
            {"element": "ExampleSWC", "through": ["ExampleRun"], "member": {"port": "pOut", "element": "value"}},
            {"element": "ExampleSWC", "through": ["ExampleRun"], "member": {"port": "pPhysical", "element": "speed"}},
            {
                "element": "ExampleSWC",
                "through": ["ExampleSlowRun"],
                "member": {"port": "pPhysical", "element": "speed"},
            },
        )
        assert slice_layer(state, "swc", members="runnables.reads").identities == (
            {
                "element": "ExampleConsumerSWC",
                "through": ["ExampleConsumerRun"],
                "member": {"port": "rIn", "element": "value"},
            },
            {
                "element": "ExampleConsumerSWC",
                "through": ["ExampleConsumerRun"],
                "member": {"port": "rPhysical", "element": "speed"},
            },
        )

    def test_a_one_step_row_carries_no_through_key(self, tmp_path: Path) -> None:
        state = load_model_state(str(_path_model_root(tmp_path)))
        rows = slice_layer(state, "swc", members="runnables").identities
        assert rows and all(list(row) == ["element", "member"] for row in rows)

    def test_the_filter_at_a_path_selects_the_final_member(self, tmp_path: Path) -> None:
        state = load_model_state(str(_path_model_root(tmp_path)))
        rows = slice_layer(
            state, "swc", members="runnables.writes", where=[Clause(field="port", equals="pPhysical")]
        ).identities
        assert [row["through"] for row in rows] == [["ExampleRun"], ["ExampleSlowRun"]]
        # A field only the intermediate member declares is not in that vocabulary.
        with pytest.raises(QueryError, match="'min_start_interval'.*member kind of 'swc.runnables.writes'"):
            slice_layer(
                state, "swc", members="runnables.writes", where=[Clause(field="min_start_interval", exists=True)]
            )

    def test_a_page_continues_past_a_cursor_that_carries_through(self, tmp_path: Path) -> None:
        state = load_model_state(str(_path_model_root(tmp_path)))
        first = slice_layer(state, "swc", members="runnables.writes", limit=2)
        assert first.page is not None
        cursor = first.page.next
        assert cursor == {
            "element": "ExampleSWC",
            "through": ["ExampleRun"],
            "member": {"port": "pPhysical", "element": "speed"},
        }
        second = slice_layer(state, "swc", members="runnables.writes", limit=2, after=cursor)
        assert second.identities == (
            {
                "element": "ExampleSWC",
                "through": ["ExampleSlowRun"],
                "member": {"port": "pPhysical", "element": "speed"},
            },
        )
        assert second.page is not None and second.page.next is ABSENT

        whole = slice_layer(state, "swc", members="runnables.writes").identities
        walked: list[Any] = []
        after: Any = ABSENT
        while True:
            page = slice_layer(state, "swc", members="runnables.writes", limit=1, after=after)
            walked.extend(page.identities)
            assert page.page is not None
            if page.page.next is ABSENT:
                break
            after = page.page.next
        assert tuple(walked) == whole

        with pytest.raises(QueryError, match="no element or member of this slice carries that identity"):
            slice_layer(state, "swc", members="runnables.writes", limit=1, after=dict(cursor, through=["NoSuchRun"]))

    def test_a_three_step_path_skips_the_sibling_kinds_and_reduces_a_whole_value_owner(self, tmp_path: Path) -> None:
        """``runnables.events.disabled_modes``: only ``timing_event`` declares the last step.

        The intermediate event is a ``whole_value`` member, so its identity in
        the chain is the event without the fields at their declared default —
        no ``name: null`` — which is the protocol's reduction.
        """
        model_root = _path_model_root(tmp_path)
        _with_disabled_modes(model_root)
        state = load_model_state(str(model_root))
        (row,) = slice_layer(state, "swc", members="runnables.events.disabled_modes").identities
        assert row == {
            "element": "ExampleSWC",
            "through": [
                "ExampleRun",
                {
                    "type": "timing",
                    "period": 0.01,
                    "disabled_modes": [
                        {"port": "mMode", "mode_group_ref": "/Modes/Group", "mode_ref": "/Modes/Group/OFF"}
                    ],
                },
            ],
            "member": {"port": "mMode", "mode_group_ref": "/Modes/Group", "mode_ref": "/Modes/Group/OFF"},
        }

    def test_a_legacy_slice_keeps_its_duplicate_identities(self, tmp_path: Path) -> None:
        """Two equal timing events are two members, so a path through them answers two equal rows."""
        model_root = _path_model_root(tmp_path)
        _edit(
            model_root / "swc" / "example" / "ExampleSWC.yaml",
            lambda document: document["runnables"][1].update(
                events=[{"type": "timing", "period": 0.1}, {"type": "timing", "period": 0.1}]
            ),
        )
        state = load_model_state(str(model_root))
        rows = slice_layer(state, "swc", members="runnables.events").identities
        slow = [row for row in rows if row["through"] == ["ExampleSlowRun"]]
        assert len(slow) == 2 and slow[0] == slow[1]

    @pytest.mark.parametrize(
        ("members", "says"),
        [
            ("runnables.writez", "'writez' is not a declared field of any member kind the members 'runnables'"),
            ("ports.nope", "'nope' is not a declared field of any member kind the members 'ports'"),
            ("runnables.name", "every step of a member path names a collection"),
            ("memory.anything", "a map may only be the last step"),
            ("memory.writes", "'memory' is a map collection"),
            ("accessible.anything", "not a declared field of any top-level element kind of layer 'swc'"),
            ("connections", "not a declared field of any top-level element kind of layer 'swc'"),
            ("foo-bar", "not a declared field of any top-level element kind of layer 'swc'"),
            ("type", "--where type=VALUE"),
        ],
        ids=(
            "undeclared-second-step",
            "undeclared-under-ports",
            "scalar-step",
            "map-mid-path",
            "map-mid-path-before-a-declared-name",
            "undeclared-first-step",
            "undeclared-one-step",
            "odd-one-step",
            "scalar-one-step",
        ),
    )
    def test_each_refusal_names_its_step(self, tmp_path: Path, members: str, says: str) -> None:
        state = load_model_state(str(_path_model_root(tmp_path)))
        with pytest.raises(QueryError) as refused:
            slice_layer(state, "swc", members=members)
        assert says in str(refused.value)
        assert str(refused.value).startswith(f"--members '{members}':")

    def test_every_declared_path_answers_what_the_protocol_answers(self, tmp_path: Path) -> None:
        """The one-answer property, over every path the registry declares, on two seeds.

        Each ``(layer, members)`` the live registry declares — every one-step
        collection and every dotted path below it — is answered by
        :func:`slice_layer` and by the protocol's ``read`` at ``form: "index"``,
        and the two identity lists must be equal. The path seed is widened with
        a disabled mode (so the three-step path and the whole-value reduction
        have rows) and with port measures, a com spec and calls (so the
        map-ending path, the field-identity path and the composite-identity path
        have rows); both seeds get two interface stubs (so the sibling skip is
        exercised at the first step). Every **dotted** path must answer rows on
        at least one seed, so no path's equality is only two empty lists.

        What it does **not** reach: a path the registry does not declare is
        compared by the refusal cases above, not here; and a one-step
        collection neither seed populates (``literals``, ``fields``,
        ``elements``, ``entries``, ``maps``, the compositions' two) is compared
        only while both surfaces answer nothing.
        """
        with_rows: set[tuple[str, str]] = set()
        for vector in ("read-swc-element", PATH_SEED):
            model_root = _path_model_root(tmp_path / vector, vector)
            if vector == PATH_SEED:
                _with_disabled_modes(model_root)
                _with_port_details(model_root)
            _with_an_interface_stub(model_root)
            state = load_model_state(str(model_root))
            snapshot = load_snapshot(str(model_root))
            for layer, members in _declared_paths():
                expected = read_value(snapshot, address.Address(layer=layer), form="index", members=members)
                assert list(slice_layer(state, layer, members=members).identities) == expected["identities"], (
                    vector,
                    layer,
                    members,
                )
                if expected["identities"]:
                    with_rows.add((layer, members))
        dotted = {path for path in _declared_paths() if "." in path[1]}
        assert dotted, "the shipped contract declares no dotted path"
        assert sorted(dotted - with_rows) == [], "a dotted path answered no rows on either seed"

    def test_a_map_ending_path_answers_through_and_key_on_both_surfaces(self, tmp_path: Path) -> None:
        """``ports.measures``: the one map-ending path, a row ``{element, through, key}``, its cursor and its text."""
        model_root = _path_model_root(tmp_path)
        _with_port_details(model_root)
        state = load_model_state(str(model_root))
        snapshot = load_snapshot(str(model_root))
        rows = [
            {"element": "ExampleConsumerSWC", "through": ["rIn"], "key": "value"},
            {"element": "ExampleConsumerSWC", "through": ["rIn"], "key": "other"},
        ]
        assert list(slice_layer(state, "swc", members="ports.measures").identities) == rows
        swc = address.Address(layer="swc")
        assert read_value(snapshot, swc, form="index", members="ports.measures") == {"count": 2, "identities": rows}
        for form in ("summary", "full"):
            assert read_value(snapshot, swc, form=form, members="ports.measures") == [
                {**rows[0], "member": "e2e"},
                {**rows[1], "member": "crc"},
            ]
        # The cursor carries both `through` and `key`, and both surfaces continue past it to the same row.
        first = slice_layer(state, "swc", members="ports.measures", limit=1)
        assert first.page is not None and first.page.next == rows[0]
        assert slice_layer(state, "swc", members="ports.measures", limit=1, after=rows[0]).identities == (rows[1],)
        second = read_value(snapshot, swc, form="index", members="ports.measures", limit=1, after=rows[0])
        assert second["identities"] == [rows[1]]
        # The text form writes the key in brackets after the owner chain.
        assert format_query(first).splitlines()[0] == "swc: ExampleConsumerSWC.rIn[value]"

    @pytest.mark.parametrize(
        "clause",
        [
            {"field": "period", "equals": 0.01},
            {"field": "period", "exists": True},
            {"field": "period", "exists": False},
            {"field": "type", "equals": "init"},
        ],
        ids=("period-equals", "period-exists", "period-absent", "type-equals"),
    )
    def test_a_filter_at_a_multi_kind_path_answers_what_the_protocol_answers(
        self, tmp_path: Path, clause: dict[str, Any]
    ) -> None:
        """``runnables.events`` holds a timing event (with ``period``) and an init event (without one).

        A clause on a field only some member kinds declare is a selection, never
        a refusal, at the end of a path as at a layer.
        """
        model_root = _path_model_root(tmp_path)
        _with_disabled_modes(model_root)
        state = load_model_state(str(model_root))
        snapshot = load_snapshot(str(model_root))
        kernel = slice_layer(state, "swc", members="runnables.events", where=[Clause(**clause)])
        protocol = read_value(
            snapshot,
            address.Address(layer="swc"),
            form="index",
            members="runnables.events",
            where=[WhereClause(**clause)],
        )
        assert list(kernel.identities) == protocol["identities"]
        types = [row["member"]["type"] for row in kernel.identities]
        if clause.get("exists") is False or clause.get("equals") == "init":
            assert types == ["init"]
        else:
            assert types and set(types) == {"timing"}

    @pytest.mark.parametrize("members", ["operations", "data_elements"])
    def test_an_interface_stubs_member_names_are_rows_on_neither_surface(self, tmp_path: Path, members: str) -> None:
        """An interface stub's member names are skipped here as the protocol skips them.

        The stub declares the name as a list of member NAMES, a collection of another shape, so it is never a row with a
        ``null`` member."""
        model_root = _path_model_root(tmp_path)
        _with_an_interface_stub(model_root)
        state = load_model_state(str(model_root))
        rows = slice_layer(state, "interfaces", members=members).identities
        assert not {row["element"] for row in rows} & {"IOutside", "IOutsideServer"}
        assert all(row["member"] is not None for row in rows)
        protocol = read_value(
            load_snapshot(str(model_root)), address.Address(layer="interfaces"), form="index", members=members
        )
        assert list(rows) == protocol["identities"]

    def test_both_renderers_print_the_owner_chain(self, tmp_path: Path) -> None:
        state = load_model_state(str(_path_model_root(tmp_path)))
        result = slice_layer(state, "swc", members="runnables.writes", limit=1)
        text = format_query(result)
        assert text.splitlines()[0] == 'swc: ExampleSWC.ExampleRun.{"element": "value", "port": "pOut"}'
        assert "(members: runnables.writes; page: 1 of 3, truncated; next: after=" in text
        document = json.loads(format_query_json(result))
        assert document["schemaVersion"] == query.QUERY_SCHEMA_VERSION
        assert document["members"] == "runnables.writes"
        assert document["elements"] == [
            {"element": "ExampleSWC", "through": ["ExampleRun"], "member": {"port": "pOut", "element": "value"}}
        ]


def test_dependents_does_not_follow_an_instance_selector() -> None:
    """A stated limitation: ``query --dependents`` walks declared references, and an ``instance`` value is not one.

    An ``instance`` value is a path. A task entry and a sequence step naming ``Ctrl``'s one occurrence by ``instance``
    are not its dependents; the same entries written with ``swc`` are. If the first half fails because they ARE listed,
    the limitation is lifted: say so in the YAML conventions and the guide, and delete that half."""

    def state(selector: dict[str, str]) -> ModelState:
        return _state(
            swcs=[SWComponent(name="Ctrl")],
            compositions=[
                Composition(name="Root", title="Root", root=True, members=[CompositionMember(name="c", type="Ctrl")])
            ],
            tasks=[
                Task(
                    name="T",
                    period=0.01,
                    priority=1,
                    partition="PARTITION0",
                    runnables=[TaskRunnableRef(runnable="Run", **selector)],
                )
            ],
            sequences=[Sequence(name="S", task="T", steps=[SequenceStep(runnable="Run", **selector)])],
        )

    def names(walked: Any) -> set[tuple[str, str]]:
        return {(row.ref.layer, row.ref.name) for rows in walked.values() for row in rows}

    by_instance = names(query.related(state({"instance": "c"}), "Ctrl", "dependents", None))
    assert ("task", "T") not in by_instance and ("sequence", "S") not in by_instance
    assert ("composition", "Root") in by_instance
    by_swc = names(query.related(state({"swc": "Ctrl"}), "Ctrl", "dependents", None))
    assert {("task", "T"), ("sequence", "S"), ("composition", "Root")} <= by_swc


class TestTheIdentityRowHelpers:
    """The enhanced-row branch's helpers, stated once for both surfaces.

    ``editing/read.py`` and :func:`query.answer_request` call the same four
    functions; the two-surface equality is ``test_cli_query.py``'s, and these pin
    each helper's own rule.
    """

    @staticmethod
    def _vocabulary() -> Any:
        from swcraft_tools.validator import DERIVED

        return DERIVED

    def test_a_name_applies_only_where_its_at_says(self) -> None:
        vocabulary = self._vocabulary()
        assert query.derived_refusal(vocabulary, ["connection_ends"], "connectors", None) is None
        assert query.derived_refusal(vocabulary, ["base_type"], "swc", "runnables.writes") is None
        assert query.derived_refusal(vocabulary, ["base_type"], "swc", "runnables.reads") is None
        unknown = query.derived_refusal(vocabulary, ["nope"], "swc", None)
        assert unknown is not None and unknown.kind == "unknown" and "`derived` names 'nope'" in unknown.message
        for name, layer, members in [
            ("connection_ends", "swc", None),
            ("connection_ends", "connectors", "from"),
            ("base_type", "swc", None),
            ("base_type", "swc", "runnables"),
            ("base_type", "connectors", "runnables.writes"),
        ]:
            refusal = query.derived_refusal(vocabulary, [name], layer, members)
            assert refusal is not None and refusal.kind == "not-applicable", (name, layer, members)

    def test_the_first_refused_name_is_the_one_named(self) -> None:
        refusal = query.derived_refusal(self._vocabulary(), ["connection_ends", "nope"], "connectors", None)
        assert refusal is not None and refusal.message.startswith("`derived` names 'nope'")

    def test_a_layer_identity_is_wrapped_and_a_member_row_copied(self) -> None:
        assert query.identity_rows(["A", "B"], sliced=False) == [{"element": "A"}, {"element": "B"}]
        member = {"element": "A", "through": ["Run"], "member": {"port": "p", "element": "e"}}
        rows = query.identity_rows([member], sliced=True)
        assert rows == [member] and rows[0] is not member

    def test_identical_rows_collapse_under_the_canonical_equality(self) -> None:
        rows = [{"element": "A", "derived": {"n": 1}}, {"element": "A", "derived": {"n": 1.0}}, {"element": "B"}]
        assert query.collapse_rows(rows) == [rows[0], rows[2]]
        assert query.collapse_rows([{"element": True}, {"element": 1}]) == [{"element": True}, {"element": 1}]

    def test_the_page_is_keyset_over_the_whole_row(self) -> None:
        rows = [{"element": "A"}, {"element": "B"}, {"element": "C"}]
        assert query.page_rows(rows, None) == (rows, None)
        window, page = query.page_rows(rows, 2)
        assert window == rows[:2]
        assert page == query.QueryPage(total=3, returned=2, truncated=True, next=rows[1])
        window, page = query.page_rows(rows, 2, rows[1])
        assert window == rows[2:]
        assert page == query.QueryPage(total=3, returned=1, truncated=True)
        assert query.page_rows(rows, 2, rows[2]) == ([], query.QueryPage(total=3, returned=0, truncated=True))
        with pytest.raises(query.RowCursorNotFound):
            query.page_rows(rows, 1, {"element": "Z"})
        with pytest.raises(ValueError):
            query.page_rows(rows, 0)

    def test_values_are_computed_then_collapsed_then_paged(self) -> None:
        state = _state()
        seen: list[Any] = []

        def derive(rows: Any, names: Any, given: Any) -> list[dict[str, Any]]:
            seen.append((list(rows), list(names), given))
            return [{**row, "derived": {"n": 1}} for row in rows for _ in range(2)]

        window, page = query.answer_rows(
            [{"element": "A"}, {"element": "B"}], ["n"], derive=derive, state=state, limit=1
        )
        assert seen == [([{"element": "A"}, {"element": "B"}], ["n"], state)]
        assert window == [{"element": "A", "derived": {"n": 1}}]
        assert page is not None and page.total == 2
        assert query.answer_rows([{"element": "A"}], [], derive=derive, state=state) == ([{"element": "A"}], None)

    def test_answer_request_answers_the_join_with_the_joined_reads_computed_values(self) -> None:
        from swcraft_tools.validator import derive_rows

        joined = query.parse_request(
            {
                "address": {"layer": "swc"},
                "form": "index",
                "join": [
                    {
                        "with": {"address": {"layer": "connectors"}, "derived": ["connection_ends"]},
                        "on": [{"left": "element", "right": "derived.connection_ends.requester.swc"}],
                        "as": "c",
                    }
                ],
            }
        )
        answered = query.answer_request(_three_port_state(), joined, vocabulary=self._vocabulary(), derive=derive_rows)
        assert answered.is_rows and answered.join == joined.join
        assert answered.identities == (
            {
                "element": "BrakeConsumer",
                "c": {
                    "element": "Brake_link",
                    "derived": {
                        "connection_ends": {
                            "provider": {"swc": "BrakeController", "port": "pBrakeRequestAux"},
                            "requester": {"swc": "BrakeConsumer", "port": "pBrakeRequest"},
                        }
                    },
                },
            },
        )

    def test_a_rows_answer_is_its_own_document(self) -> None:
        result = query.Slice(
            layer="connectors",
            identity_field="name",
            identities=({"element": "C", "derived": {"n": "x"}},),
            derived=("n",),
        )
        document = json.loads(query.format_query_json(result))
        assert document["schemaVersion"] == query.QUERY_ROWS_SCHEMA_VERSION
        assert list(document) == ["schemaVersion", "layer", "identityField", "derived", "rows"]
        assert query.format_query(result) == '{"element":"C","derived":{"n":"x"}}\n\n(derived: n)\n'
        legacy = query.Slice(layer="connectors", identity_field="name", identities=("C",))
        assert json.loads(query.format_query_json(legacy))["schemaVersion"] == query.QUERY_SCHEMA_VERSION


class TestTheJoin:
    """The join's kernel statement, stated once for both surfaces.

    ``row_shape``, ``join_shape`` and ``join_rows`` are generic over rows and
    read no field; the two-surface equality is ``test_cli_query.py``'s and the
    protocol's matrix ``test_editing_read.py``'s. These pin each helper's own
    rule.
    """

    WRITES = (query.RowStep("list", "field"), query.RowStep("list", "composite", ("port", "element")))

    @staticmethod
    def _vocabulary() -> Any:
        from swcraft_tools.validator import DERIVED

        return DERIVED

    def test_the_row_shape_follows_the_declarations(self) -> None:
        assert query.row_shape((), (), ()) == {"element": None}
        assert query.row_shape((query.RowStep("list", "field"),), (), ()) == {"element": None, "member": None}
        assert query.row_shape((query.RowStep("map", "whole_value"),), (), ()) == {"element": None, "key": None}
        assert query.row_shape(self.WRITES, ["base_type"], self._vocabulary()) == {
            "element": None,
            "through": {"0": None},
            "member": {"port": None, "element": None},
            "derived": {"base_type": {"implementation": None, "base": None}},
        }

    @pytest.mark.parametrize(
        "path",
        ["element", "through.0", "member.port", "member.element", "derived.base_type.base"],
    )
    def test_a_path_naming_one_value_is_accepted(self, path: str) -> None:
        left = query.row_shape(self.WRITES, ["base_type"], self._vocabulary())
        joined = query.join_shape(left, [(path, "element")], "other", {"element": None})
        assert joined["other"] == {"element": None}
        assert set(joined) == {"element", "through", "member", "derived", "other"}

    @pytest.mark.parametrize(
        "path",
        [
            "member",
            "through",
            "through.1",
            "through.0.name",
            "derived",
            "derived.base_type",
            "derived.nope",
            "key",
            "element.x",
            "member..port",
            "nope",
        ],
    )
    def test_a_path_naming_no_value_is_refused_on_either_side(self, path: str) -> None:
        shape = query.row_shape(self.WRITES, ["base_type"], self._vocabulary())
        with pytest.raises(query.JoinPathOutside, match="names no value of the row so far") as left:
            query.join_shape(shape, [(path, "element")], "a", {"element": None})
        assert (left.value.alias, left.value.side, left.value.path) == ("a", "left", path)
        with pytest.raises(query.JoinPathOutside, match="names no value of the joined read's row") as right:
            query.join_shape({"element": None}, [("element", path)], "a", shape)
        assert right.value.side == "right"
        assert isinstance(right.value, query.QueryError)

    def test_a_left_path_reaches_an_earlier_alias_and_prefix_sharing_aliases_are_two(self) -> None:
        first = query.join_shape({"element": None}, [("element", "element")], "reader", {"element": None})
        second = query.join_shape(first, [("reader.element", "element")], "reader2", {"element": None})
        assert set(second) == {"element", "reader", "reader2"}
        with pytest.raises(query.JoinPathOutside):
            query.join_shape(first, [("reader2.element", "element")], "reader3", {"element": None})

    def test_rows_match_in_outer_order_then_entry_order_and_a_non_match_is_dropped(self) -> None:
        outer = [{"element": "A"}, {"element": "B"}, {"element": "C"}]
        right = [
            {"element": "x", "derived": {"v": {"to": "B"}}},
            {"element": "y", "derived": {"v": {"to": "A"}}},
            {"element": "z", "derived": {"v": {"to": "B"}}},
        ]
        joined = query.join_rows(outer, [query.RowJoin(right, (("element", "derived.v.to"),), "j")])
        assert [(row["element"], row["j"]["element"]) for row in joined] == [("A", "y"), ("B", "x"), ("B", "z")]
        assert list(joined[0]) == ["element", "j"]

    def test_two_entries_compose_and_identical_rows_collapse_once_before_the_page(self) -> None:
        outer = [{"element": "A"}, {"element": "A"}]
        first = [{"element": "c1", "derived": {"e": {"p": "A", "r": "R"}}}]
        second = [{"element": "R", "member": "m1"}, {"element": "R", "member": "m2"}, {"element": "S", "member": "m3"}]
        joins = [
            query.RowJoin(first, (("element", "derived.e.p"),), "c"),
            query.RowJoin(second, (("c.derived.e.r", "element"),), "reader"),
        ]
        joined = query.join_rows(outer, joins)
        assert [row["reader"]["member"] for row in joined] == ["m1", "m2", "m1", "m2"]
        window, page = query.answer_rows(outer, [], derive=lambda *_: [], state=_state(), joins=joins)
        assert [row["reader"]["member"] for row in window] == ["m1", "m2"] and page is None

    @staticmethod
    def _counted(outer: Any, joins: Any) -> tuple[list[Any], list[query.JoinProduct]]:
        """The joined rows and every product the bound was handed, admitting each."""
        seen: list[query.JoinProduct] = []
        return query.join_rows(outer, joins, bound=seen.append), seen

    def test_one_entry_is_counted_exactly_before_it_is_built(self) -> None:
        """``sum(n_outer(key) * n_joined(key))`` over shared keys: 2 × 2 on ``A``, 1 × 1 on ``B``, ``C`` unmatched."""
        outer = [{"element": "A", "member": 1}, {"element": "A", "member": 2}, {"element": "B"}, {"element": "C"}]
        right = [{"element": "A", "member": 3}, {"element": "A", "member": 4}, {"element": "B"}, {"element": "D"}]
        joined, [product] = self._counted(outer, [query.RowJoin(right, (("element", "element"),), "j")])
        assert (product.position, product.alias, product.rows, product.built) == (0, "j", 5, 5)
        assert product.rows == len(joined)
        assert sorted((len(lefts), len(rights)) for lefts, rights in product.groups) == [(1, 1), (2, 2)]

    def test_a_fan_out_counts_every_pair(self) -> None:
        outer = [{"element": f"o{number}", "member": "k"} for number in range(30)]
        right = [{"element": f"r{number}", "member": "k"} for number in range(40)]
        joined, [product] = self._counted(outer, [query.RowJoin(right, (("member", "member"),), "j")])
        assert product.rows == len(joined) == 1_200

    def test_two_entries_are_counted_entry_by_entry_over_the_rows_the_last_one_built(self) -> None:
        """Entry 2's outer side is entry 1's product, so its count reads the rows entry 1 built."""
        outer = [{"element": "A"}, {"element": "B"}]
        first = [
            {"element": "c1", "derived": {"e": {"p": "A", "r": "R"}}},
            {"element": "c2", "derived": {"e": {"p": "A", "r": "S"}}},
            {"element": "c3", "derived": {"e": {"p": "B", "r": "R"}}},
        ]
        second = [{"element": "R", "member": "m1"}, {"element": "R", "member": "m2"}, {"element": "T", "member": "m3"}]
        joins = [
            query.RowJoin(first, (("element", "derived.e.p"),), "c"),
            query.RowJoin(second, (("c.derived.e.r", "element"),), "reader"),
        ]
        joined, products = self._counted(outer, joins)
        assert [(product.alias, product.rows) for product in products] == [("c", 3), ("reader", 4)]
        assert [product.built for product in products] == [3, 7], "the whole join's work so far, cumulative"
        assert len(joined) == 4

    def test_a_null_key_is_on_neither_side_of_the_count(self) -> None:
        outer = [{"element": "A", "member": None}, {"element": "B", "member": "k"}, {"element": "C"}]
        right = [{"element": "X", "member": None}, {"element": "Y", "member": "k"}, {"element": "Z"}]
        joined, [product] = self._counted(outer, [query.RowJoin(right, (("member", "member"),), "j")])
        assert product.rows == len(joined) == 1
        assert [(len(lefts), len(rights)) for lefts, rights in product.groups] == [(1, 1)]

    def test_a_refusing_bound_stops_the_entry_before_a_row_is_built(self) -> None:
        """The bound raises before the entry's product exists; nothing quadratic ran first."""

        class Refused(Exception):
            pass

        def refuse(product: query.JoinProduct) -> None:
            raise Refused(product.rows)

        outer = [{"element": "o", "member": "k"}] * 1_000
        right = [{"element": f"r{number}", "member": "k"} for number in range(1_000)]
        with pytest.raises(Refused) as refused:
            query.join_rows(outer, [query.RowJoin(right, (("member", "member"),), "j")], bound=refuse)
        assert refused.value.args == (1_000_000,)

    def test_the_count_is_the_answers_row_count_because_the_outer_rows_collapse_first(self) -> None:
        """Two identical outer rows are one before the join, so the product counted is the product answered."""
        outer = [{"element": "A"}, {"element": "A"}, {"element": "B"}]
        right = [{"element": "A", "member": 1}, {"element": "A", "member": 2}, {"element": "B", "member": 3}]
        seen: list[query.JoinProduct] = []
        window, page = query.answer_rows(
            outer,
            [],
            derive=lambda *_: [],
            state=_state(),
            joins=[query.RowJoin(right, (("element", "element"),), "j")],
            bound=seen.append,
        )
        assert [product.rows for product in seen] == [len(window)] == [3]
        assert page is None

    def test_a_join_on_a_through_step_indexes_the_list(self) -> None:
        outer = [
            {"element": "A", "through": ["Run1"], "member": "p"},
            {"element": "A", "through": ["Run2"], "member": "p"},
        ]
        right = [{"element": "T", "member": "Run2"}, {"element": "U", "member": "Run3"}]
        joined = query.join_rows(outer, [query.RowJoin(right, (("through.0", "member"),), "task")])
        assert joined == [{"element": "A", "through": ["Run2"], "member": "p", "task": right[0]}]

    def test_a_null_matches_nothing_on_either_side(self) -> None:
        outer = [
            {"element": "A", "derived": {"t": {"base": None}}},
            {"element": "B", "derived": {"t": {"base": "uint8"}}},
            {"element": "C"},
        ]
        right = [
            {"element": "X", "derived": {"t": {"base": None}}},
            {"element": "Y", "derived": {"t": {"base": "uint8"}}},
            {"element": "Z"},
        ]
        joined = query.join_rows(outer, [query.RowJoin(right, (("derived.t.base", "derived.t.base"),), "j")])
        assert [(row["element"], row["j"]["element"]) for row in joined] == [("B", "Y")]
        two_paths = query.join_rows(
            [{"element": "A", "member": None}],
            [query.RowJoin([{"element": "A", "member": None}], (("element", "element"), ("member", "member")), "j")],
        )
        assert two_paths == []

    def test_equality_is_values_equal_and_a_whole_key_must_match(self) -> None:
        outer = [{"element": "A", "member": {"port": "p", "element": 1}}]
        right = [
            {"element": "B", "member": {"port": "p", "element": 1.0}},
            {"element": "C", "member": {"port": "p", "element": True}},
        ]
        joined = query.join_rows(
            outer,
            [query.RowJoin(right, (("member.port", "member.port"), ("member.element", "member.element")), "j")],
        )
        assert [row["j"]["element"] for row in joined] == ["B"]

    def test_a_joined_reads_values_are_computed_by_the_outer_reads_rule_before_matching(self) -> None:
        state = _state()

        def derive(rows: Any, names: Any, _state: Any) -> list[dict[str, Any]]:
            return [{**row, "derived": {name: row["element"].lower() for name in names}} for row in rows]

        right = query.computed_rows([{"element": "A"}, {"element": "B"}], ["low"], derive=derive, state=state)
        assert query.computed_rows(right, [], derive=derive, state=state) == right
        window, page = query.answer_rows(
            [{"element": "a"}, {"element": "b"}],
            [],
            derive=derive,
            state=state,
            joins=[query.RowJoin(right, (("element", "derived.low"),), "j")],
            limit=1,
        )
        assert window == [{"element": "a", "j": {"element": "A", "derived": {"low": "a"}}}]
        assert page is not None and page.total == 2 and page.next == window[0]

    @staticmethod
    def _shaped_paths() -> list[tuple[str, str]]:
        """Every declared collection path, and every path the computed-value vocabulary's ``at`` names."""
        from swcraft_tools.validator import DERIVED

        named = [(entry.at["layer"], path) for entry in DERIVED for path in entry.at.get("members", ())]
        return sorted({*_declared_paths(), *named})

    def test_both_surfaces_read_one_row_shape_off_the_declarations(self) -> None:
        """``query.answer._row_steps`` (``CollectionFacts``) agrees with ``editing.read``'s (``CollectionDescriptor``).

        Each surface reads a step's container and identity rule off its own
        descriptor vocabulary, so a spelling drift in either would publish
        ``member`` on one surface where the other publishes ``key``. What it
        does not reach: a path no declaration names, which both refuse.
        """
        from swcraft_tools.editing.read import _row_steps as protocol_row_steps

        registry = current_registry()
        editing_registry = reg.or_current(None)
        paths = self._shaped_paths()
        assert any(len(members.split(".")) > 1 for _layer, members in paths)
        for layer, members in paths:
            kernel = query_answer._row_steps(registry.layer(layer), members, registry)
            protocol = protocol_row_steps(
                members,
                address.Address(layer=layer),
                layer=editing_registry.layer(layer),
                registry=editing_registry,
            )
            assert kernel == protocol, (layer, members)

    def test_a_bad_path_is_refused_on_an_empty_model(self) -> None:
        request = query.parse_request(
            {
                "address": {"layer": "swc"},
                "form": "index",
                "members": "runnables.writes",
                "join": [
                    {
                        "with": {"address": {"layer": "connectors"}, "derived": ["connection_ends"]},
                        "on": [{"left": "member", "right": "derived.connection_ends.provider.port"}],
                        "as": "connection",
                    }
                ],
            }
        )
        with pytest.raises(query.JoinPathOutside, match="join 'connection': `left` path 'member'"):
            query.answer_request(_state(), request, vocabulary=self._vocabulary(), derive=lambda *_: [])

    def test_a_joined_reads_own_refusal_names_its_alias(self) -> None:
        request = query.parse_request(
            {
                "address": {"layer": "swc"},
                "form": "index",
                "join": [
                    {
                        "with": {"address": {"layer": "swc"}, "members": "runnables.bogus"},
                        "on": [{"left": "element", "right": "element"}],
                        "as": "reader",
                    }
                ],
            }
        )
        with pytest.raises(query.QueryError, match="^join 'reader': `members` 'runnables.bogus'"):
            query.answer_request(_state(), request, vocabulary=self._vocabulary(), derive=lambda *_: [])
