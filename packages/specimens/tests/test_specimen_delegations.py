"""Delegations, and the connector range over R_c.

The ports no connector end reaches are reached by delegations: the components are entered whole in
ascending reached ports until R_c, the connector-free ones sit in the deepest composition of the
chain, and D is met from three sources in order (reach, chain steps, repeats). Every acceptance is
judged by the passport pass over the rendered documents; the admitted R_c interval is held to an
exhaustive brute force over the chain shape with the ascending entry, candidate by candidate. The
ascending split is the preferred candidate alone: when it does not build, the entry planner lays
the configuration out (`layout.py`).
"""

from __future__ import annotations

import itertools
import json
from collections import Counter
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from swcraft_specimens.model import Composition, CompositionPort, Port, Project
from swcraft_specimens.passport import Refusal, compute_passport, validate_passport
from swcraft_specimens.specimen import build as build_module
from swcraft_specimens.specimen import layout as layout_module
from swcraft_specimens.specimen import shape as shape_module
from swcraft_specimens.specimen.build import build_project, build_specimen_files, render_specimen
from swcraft_specimens.specimen.connectors import (
    FULL_SEARCH,
    ConnectorSpec,
    Search,
    solve_open,
    star_solution,
    unconnected_placements,
)
from swcraft_specimens.specimen.delegations import (
    EntryOrder,
    admitted_reach,
    build_delegations,
    delegation_count,
    delegation_refusal,
)
from swcraft_specimens.specimen.feasibility import check_feasibility
from swcraft_specimens.specimen.layout import SOLVER, Layout, Planned, Trace, layout_bounds
from swcraft_specimens.specimen.leaves import LEAVES, stated_leaves
from swcraft_specimens.specimen.preset import DEFAULT_NAME, SpecimenPreset
from swcraft_specimens.specimen.shape import Shape, specimen_shape
from swcraft_specimens.specimen.vocabulary import DELEGATIONS, MET
from tests.oracle.specimen_helpers import neutral_passport, shaped_witness

DELEGATION = "connectors.delegation"


def six(delegations: int, compositions: int = 3, connectors: int = 3) -> dict[str, Any]:
    """Six components of two ports, U = 2, three SR interfaces of fan-out 4.

    U is one on each of two components: R = 10 reached ports, A = 3, so R_c is 4 to 6 and the
    rest are reached by delegation.
    """
    passport = neutral_passport()
    passport["identifiers"] = {"max_length": 8}
    passport["layers"].update(swc=6, interfaces=3, connectors=connectors, compositions=compositions)
    passport["components"]["ports"] = {"total": 12, "top": [2, 2, 2, 2, 2, 2]}
    passport["components"]["unconnected"] = {"total": 2, "max_on_one": 1}
    passport["components"]["runnables"] = 6
    passport["interfaces"] = {
        "sender_receiver": 3,
        "client_server": 0,
        "fan_out": {"max": 4, "top": [4, 4, 4]},
    }
    passport["connectors"] = {"assembly": connectors, "delegation": delegations, "links": 0}
    return passport


def with_stub(delegations: int) -> dict[str, Any]:
    """Six components of two ports, U = 0, two SR interfaces of fan-out 5 and one stub.

    The stub holds the other 2 ports; they exceed U, so they need R - R_c >= 2.
    """
    passport = neutral_passport()
    passport["identifiers"] = {"max_length": 8}
    passport["layers"].update(swc=6, interfaces=3, connectors=5)
    passport["components"]["ports"] = {"total": 12, "top": [2, 2, 2, 2, 2, 2]}
    passport["components"]["unconnected"] = {"total": 0, "max_on_one": 0}
    passport["components"]["runnables"] = 6
    passport["interfaces"] = {
        "sender_receiver": 2,
        "client_server": 0,
        "fan_out": {"max": 5, "top": [5, 5, 2]},
    }
    passport["connectors"] = {"assembly": 5, "delegation": delegations, "links": 2}
    return passport


def hubs_list(count: int, total: int, largest: int) -> list[int]:
    """`count` descending hub counts summing to `total`, the first `largest`, the rest even."""
    rest, others = total - largest, count - 1
    values = [largest] + [rest // others] * others
    for index in range(rest - (rest // others) * others):
        values[1 + index] += 1
    return sorted(values, reverse=True)


def large_scale() -> dict[str, Any]:
    """The large-scale shape: P = 3,083, U = 107, A = 1,448, D = 4,192, C = 13.

    Its 64-entry `hubs` list carries 3,007 ports (one hub of 400).
    """
    passport = neutral_passport()
    passport["identifiers"] = {"max_length": 8}
    passport["layers"].update(
        swc=140,
        interfaces=300,
        connectors=1448,
        compositions=13,
        implementation_types=1,
        base_types=1,
    )
    passport["components"]["ports"] = {"total": 3083}
    passport["components"]["hubs"] = hubs_list(64, 3007, 400)
    passport["components"]["unconnected"] = {"total": 107, "max_on_one": 20}
    passport["components"]["runnables"] = 140
    passport["interfaces"] = {"sender_receiver": 300, "client_server": 0, "fan_out": {"max": 30}}
    passport["connectors"] = {"assembly": 1448, "delegation": 4192, "links": 0}
    return passport


def refusals(passport: dict[str, Any]) -> list[Refusal]:
    return check_feasibility(passport)


def refused(passport: dict[str, Any]) -> list[tuple[str, str]]:
    return [(r.leaf, r.refusal_class) for r in refusals(passport)]


def the_shape(passport: dict[str, Any]) -> Shape:
    shape = specimen_shape(stated_leaves(passport))
    assert isinstance(shape, Shape)
    return shape


def built(passport: dict[str, Any]) -> tuple[Project, dict[str, Any]]:
    """The project, through the full pipeline and the self-check, and its passport."""
    files = build_specimen_files(SpecimenPreset(DEFAULT_NAME, passport))
    assert "generation.json" in files
    rendered = render_specimen(SpecimenPreset(DEFAULT_NAME, passport))
    return rendered.project, rendered.passport


# --- the sources, classified from the built project -----------------------------------------------


def sources(project: Project, only: Composition | None = None) -> list[str]:
    """Each delegation's source, in emission order per composition (of `only` alone when given).

    `reach` (a component port no connector reaches), `step` (a child composition's boundary port),
    `repeat` (a provide port a connector reaches).
    """
    connected = {(c.source.swc, c.source.port) for c in project.connectors} | {
        (c.target.swc, c.target.port) for c in project.connectors
    }
    kinds: list[str] = []
    for composition in project.compositions if only is None else (only,):
        members = {m.name: m for m in composition.members}
        for delegation in composition.delegations:
            member = members[delegation.member]
            if member.composition is not None:
                kinds.append("step")
            elif (member.type, delegation.member_port) in connected:
                kinds.append("repeat")
            else:
                kinds.append("reach")
    return kinds


def reached_counts(project: Project) -> Counter[tuple[str, str]]:
    """How often each component port is reached.

    Once when any connector end reaches it (a provide port carries every connector of its star),
    and once per delegation onto a component member.
    """
    reached: Counter[tuple[str, str]] = Counter()
    for connector in project.connectors:
        assert connector.source.swc is not None and connector.target.swc is not None
    for key in {(c.source.swc, c.source.port) for c in project.connectors} | {
        (c.target.swc, c.target.port) for c in project.connectors
    }:
        assert key[0] is not None
        reached[(key[0], key[1])] += 1
    for composition in project.compositions:
        types = {m.name: m.type for m in composition.members if m.type is not None}
        for delegation in composition.delegations:
            if delegation.member in types:
                reached[(types[delegation.member], delegation.member_port)] += 1
    return reached


class TestTheLeaf:
    def test_connectors_delegation_is_met(self) -> None:
        assert LEAVES[DELEGATION].leaf_class == MET

    def test_an_unstated_delegation_count_is_zero(self) -> None:
        passport = neutral_passport()
        del passport["connectors"]["delegation"]
        assert delegation_count(stated_leaves(passport)) == 0
        assert refused(passport) == []


class TestTheInequality:
    """R - R_c <= D <= R - A + (C - 1) x D_deep(R_c), at its least and its most.

    With R = 10, A = 3, C = 3 the least is 10 - 6 = 4 (R_c at its most, 2A) and the most
    10 - 3 + 2 x 6 = 19 (R_c = 4: two components entered, four connector-free holding 6 reached
    ports).
    """

    def test_below_the_least_is_refused_naming_both_bounds(self) -> None:
        found = refusals(six(3))
        assert [(r.leaf, r.refusal_class) for r in found] == [(DELEGATION, DELEGATIONS)]
        assert found[0].sentence == (
            "states 3; with 10 reached ports, 3 connectors and 3 compositions the delegations "
            "number at least 4 and at most 19"
        )

    def test_above_the_most_is_refused_naming_both_bounds(self) -> None:
        found = refusals(six(20))
        assert [(r.leaf, r.refusal_class) for r in found] == [(DELEGATION, DELEGATIONS)]
        assert "at least 4 and at most 19" in found[0].sentence

    @pytest.mark.parametrize("delegations", [4, 19])
    def test_both_bounds_are_accepted_and_met(self, delegations: int) -> None:
        assert refused(six(delegations)) == []
        _, passport = built(six(delegations))
        assert passport["connectors"]["delegation"] == delegations
        assert passport["components"]["unconnected"] == {"total": 2, "max_on_one": 1}

    def test_one_composition_bounds_the_most_by_the_repeats(self) -> None:
        # C = 1: no chain step, so at most R - A = 7.
        assert refused(six(7, compositions=1)) == []
        assert refused(six(8, compositions=1)) == [(DELEGATION, DELEGATIONS)]

    def test_no_connector_needs_every_reached_port_delegated(self) -> None:
        # A = 0: R_c = 0, so D is R to C x R: 10 to 30 here.
        passport = six(9, connectors=0)
        passport["connectors"]["links"] = 0
        assert refused(passport) == [(DELEGATION, DELEGATIONS)]
        passport["connectors"]["delegation"] = 10
        assert refused(passport) == []


class TestTheAdmittedInterval:
    """The admitted R_c interval's closed form on hand-computed cases."""

    def test_no_connector_admits_only_zero(self) -> None:
        entry = EntryOrder.of([2, 3])
        assert admitted_reach(entry, 0, 5, 1, 5) == [0]
        assert admitted_reach(entry, 0, 4, 1, 5) == []
        assert admitted_reach(entry, 0, 10, 2, 5) == [0]

    def test_no_delegation_admits_only_r(self) -> None:
        entry = EntryOrder.of([1, 2, 3, 4])
        assert admitted_reach(entry, 5, 0, 3, 10) == [10]
        assert admitted_reach(entry, 3, 0, 3, 10) == []

    def test_the_interval_from_the_top_down(self) -> None:
        # R = 10, A = 3: R_c in [4, 6]; D_deep is 4 at each (the 4-port component left), so the
        # capacity is 10 - 3 + 4 = 11 with C = 2.
        entry = EntryOrder.of([1, 2, 3, 4])
        assert admitted_reach(entry, 3, 6, 2, 10) == [6, 5, 4]
        assert admitted_reach(entry, 3, 11, 2, 10) == [6, 5, 4]
        assert admitted_reach(entry, 3, 12, 2, 10) == []
        # The lower bound R - D cuts from below.
        assert admitted_reach(entry, 3, 5, 2, 10) == [6, 5]

    def test_the_capacity_cuts_from_the_top(self) -> None:
        # R = 10, A = 4: R_c in [5, 8]; D_deep is 4 up to 6 and 0 from 7 (every one entered).
        entry = EntryOrder.of([1, 1, 4, 4])
        assert [entry.deep(c) for c in range(5, 9)] == [4, 4, 0, 0]
        assert admitted_reach(entry, 4, 14, 3, 10) == [6, 5]

    def test_the_sr_and_cs_ports_bound_r_c(self) -> None:
        entry = EntryOrder.of([1, 2, 3, 4])
        assert admitted_reach(entry, 5, 3, 1, 8) == [8, 7]


class TestTheRefusalsOfTheConstruction:
    """The `_try_delegated` refusals no passport of the other vectors reaches."""

    def test_an_unstated_count_names_the_connectors_chosen(self) -> None:
        # The least D with A unstated is R less the most R_c, the SR + CS ports.
        bounds = layout_bounds(12, None, 1, 10, (0, 6, 7))
        assert bounds == (2, 12)
        assert delegation_refusal(1, 12, None, 1, bounds).sentence == (
            "states 1; with 12 reached ports, the connectors chosen and 1 compositions the "
            "delegations number at least 2 and at most 12"
        )

    def test_a_count_no_r_c_admits_is_refused_at_the_connector_leaf(self) -> None:
        # Six components of two ports, U = 4: R = 8 reached ports, so A = 8 leaves no R_c above
        # it (a provide port carries no require port's connector); L = 5 makes the links
        # inequality admit A = 3 + 5.
        passport = six(4, compositions=2, connectors=8)
        passport["components"]["unconnected"] = {"total": 4, "max_on_one": 1}
        passport["interfaces"]["fan_out"] = {"max": 6, "top": [6, 6]}
        passport["interfaces"]["sender_receiver"] = 2
        passport["layers"]["interfaces"] = 2
        passport["connectors"]["links"] = 5
        found = refusals(passport)
        assert [(r.leaf, r.refusal_class) for r in found] == [("connectors.assembly", "connectors")]
        assert found[0].sentence == (
            "states 8; the 8 reached ports on the 12 ports of the sender-receiver and "
            "client-server interfaces allow at most 7, since a provide port carries no require "
            "port's connector"
        )

    def test_the_placements_are_made_one_at_a_time(self) -> None:
        """`unconnected_placements` is a generator: drawing the first placement makes that one alone.

        The case is ceiling-size: 5 hubs of 1,000 ports, 4,995 components of one, U = 3,000 at most
        1,000 on one, 8,000 placements of 5,000 entries each.
        """
        counts = (1000,) * 5 + (1,) * 4995
        placements = unconnected_placements(ConnectorSpec(3000, 1000, None, None, 0), counts)
        assert isinstance(placements, Iterator)
        first = next(placements)
        assert len(first) == len(counts) and sum(first) == 3000

    def test_an_unstated_count_is_capped_at_the_most_the_capacity_holds(self) -> None:
        # Two unconnected ports and two delegation-only ones on six components of two: R_c = 8,
        # so A is 4 to 7, and L = 1 with no hub fixes it at 3 + 1 = 4.
        spec = ConnectorSpec(2, 1, None, None, 1)
        counts = (2, 2, 2, 2, 2, 2)
        units = (1, 1, 0, 0, 0, 2)
        assert solve_open(spec, counts, units, (4, 4, 4)) is not None
        assert solve_open(spec, counts, units, (4, 4, 4), most=4) is not None
        assert solve_open(spec, counts, units, (4, 4, 4), most=3) is None


class TestTheSplit:
    def test_the_entered_set_is_the_ascending_prefix(self) -> None:
        # Reached ports 4, 1, 3, 2 (index order): entered ascending 1 (j=1), 2 (j=3), 3 (j=2).
        split = EntryOrder.of([4, 1, 3, 2]).split(5)
        assert split.entered == (1, 2, 3)
        assert split.connector_free == (0,)
        # The last entered (j=2, 3 ports) is partial: 1 + 2 + 3 = 6, one beyond R_c = 5.
        assert split.delegated == (4, 0, 1, 0)

    def test_ties_enter_by_index_and_zero_enters_none(self) -> None:
        assert EntryOrder.of([2, 2, 2]).split(2).entered == (0,)
        assert EntryOrder.of([2, 2, 2]).split(0).entered == ()
        assert EntryOrder.of([2, 2, 2]).split(0).delegated == (2, 2, 2)

    def test_d_deep_is_the_connector_free_reached_ports(self) -> None:
        reached = [4, 1, 3, 2, 5]
        entry = EntryOrder.of(reached)
        for connected in range(sum(reached) + 1):
            split = EntryOrder.of(reached).split(connected)
            free = sum(reached[j] for j in split.connector_free)
            assert entry.deep(connected) == free
            assert sum(split.delegated) == sum(reached) - connected


class TestTheConstruction:
    def test_no_delegation_builds_exactly_as_before(self) -> None:
        # A = 5 (with two links) reaches all 10 reached ports: R_c = R.
        passport = six(0, connectors=5)
        passport["connectors"]["links"] = 2
        shape = the_shape(passport)
        assert shape.layout.delegations == 0 and shape.layout.connector_reached == 10
        project = build_project(SpecimenPreset(DEFAULT_NAME, passport))
        assert all(c.delegations == () and c.ports == () for c in project.compositions)
        root = project.compositions[0]
        assert [m.type for m in root.members if m.type] == [c.name for c in project.components]

    def test_one_composition_uses_reach_then_repeats(self) -> None:
        # R_c = 6 (2A): 4 delegation-only ports, then one repeat.
        project, passport = built(six(5, compositions=1))
        assert sources(project) == ["reach"] * 4 + ["repeat"]
        assert passport["connectors"]["delegation"] == 5

    def test_three_compositions_use_reach_chain_steps_then_repeats(self) -> None:
        # R_c = 6: 4 reach, 4 x 2 chain steps, then 3 repeats (the star centres): 15.
        project, passport = built(six(15))
        root, middle, deepest = project.compositions
        assert Counter(sources(project)) == Counter({"reach": 4, "step": 8, "repeat": 3})
        assert [sources(project, c) for c in project.compositions] == [
            ["step"] * 4 + ["repeat"] * 3,
            ["step"] * 4,
            ["reach"] * 4,
        ]
        assert passport["connectors"]["delegation"] == 15
        assert len(root.delegations) == 7 and len(middle.delegations) == 4
        assert len(deepest.delegations) == 4

    def test_chain_steps_go_port_by_port_up_to_the_root(self) -> None:
        # D = 6: 4 reach, then 2 chain steps: the first deep port's both steps.
        project, _ = built(six(6))
        root, middle, deepest = project.compositions
        assert [d.member_port for d in middle.delegations] == [deepest.delegations[0].port]
        assert [d.member_port for d in root.delegations] == [middle.delegations[0].port]
        assert [d.member for d in root.delegations] == [middle.name]

    @pytest.mark.parametrize(
        "passport",
        [six(4), six(10), six(19), six(5, compositions=1), with_stub(3), with_stub(4)],
        ids=["d4", "d10", "d19", "d5-c1", "stub-d3", "stub-d4"],
    )
    def test_the_built_delegations_meet_the_stated_count(self, passport: dict[str, Any]) -> None:
        project, generated = built(passport)
        stated = passport["connectors"]["delegation"]
        # D met exactly, U unchanged, by the pass.
        assert generated["connectors"]["delegation"] == stated
        assert generated["components"]["unconnected"] == passport["components"]["unconnected"]
        unconnected = passport["components"]["unconnected"]["total"]
        reached = reached_counts(project)
        ports = {(component.name, port.name): port for component in project.components for port in component.ports}
        # Every port but the U unconnected ones is reached.
        assert sum(1 for key in ports if key not in reached) == unconnected
        # No port twice, but a repeat's provide port: a connector's source (its star's centre)
        # and the target of exactly one delegation.
        sources_of = {(c.source.swc, c.source.port) for c in project.connectors}
        delegated_to: Counter[tuple[str, str]] = Counter()
        for composition in project.compositions:
            types = {m.name: m.type for m in composition.members if m.type is not None}
            for delegation in composition.delegations:
                if delegation.member in types:
                    delegated_to[(types[delegation.member], delegation.member_port)] += 1
        for key, times in reached.items():
            if times > 1:
                assert times == 2 and ports[key].direction == "provide"
                assert key in sources_of and delegated_to[key] == 1
        compositions = {c.name: c for c in project.compositions}
        for composition in project.compositions:
            boundary = {p.name: p for p in composition.ports}
            members = {m.name: m for m in composition.members}
            for delegation in composition.delegations:
                outer = boundary[delegation.port]
                member = members[delegation.member]
                inner_ports: dict[str, Port | CompositionPort]
                if member.type is not None:
                    inner_ports = {p.name: p for c in project.components if c.name == member.type for p in c.ports}
                else:
                    assert member.composition is not None
                    inner_ports = {p.name: p for p in compositions[member.composition].ports}
                inner = inner_ports[delegation.member_port]
                assert (outer.direction, outer.interface) == (inner.direction, inner.interface)
            # Every delegation has a boundary port of its own.
            assert len({d.port for d in composition.delegations}) == len(composition.delegations)
            # Members, boundary ports, delegations and connectors: one name space ignoring case.
            names = [
                *(m.name for m in composition.members),
                *(p.name for p in composition.ports),
                *(d.name for d in composition.delegations),
                *(c.name for c in project.connectors if composition.root),
            ]
            assert len({name.casefold() for name in names}) == len(names)
            assert all(p.name.startswith("BP") for p in composition.ports)
            assert all(d.name.startswith("Dlg") for d in composition.delegations)

    def test_the_entered_components_are_the_root_members(self) -> None:
        passport = six(10)
        shape = the_shape(passport)
        project = build_project(SpecimenPreset(DEFAULT_NAME, passport))
        reached = [c - u for c, u in zip(shape.counts, shape.layout.unconnected, strict=True)]
        split = EntryOrder.of(reached).split(shape.layout.connector_reached)
        assert (shape.layout.root, shape.layout.deep) == (split.entered, split.connector_free)
        names = [c.name for c in project.components]
        root, deepest = project.compositions[0], project.compositions[-1]
        assert [m.type for m in root.members if m.type] == [names[j] for j in split.entered]
        assert [m.type for m in deepest.members if m.type] == [names[j] for j in split.connector_free]
        # D_deep is the connector-free components' reached ports: each reached by a delegation
        # from the deepest composition.
        deep = sum(reached[j] for j in split.connector_free)
        assert deep == EntryOrder.of(reached).deep(shape.layout.connector_reached)
        assert sources(project, deepest) == ["reach"] * deep
        # Every connector joins two root members.
        root_components = {m.type for m in root.members}
        assert all(c.source.swc in root_components and c.target.swc in root_components for c in project.connectors)

    def test_with_one_composition_the_root_holds_the_entered_first(self) -> None:
        passport = six(5, compositions=1)
        shape = the_shape(passport)
        assert shape.layout.deep
        project = build_project(SpecimenPreset(DEFAULT_NAME, passport))
        names = [c.name for c in project.components]
        (root,) = project.compositions
        assert [m.type for m in root.members] == [
            *(names[j] for j in shape.layout.root),
            *(names[j] for j in shape.layout.deep),
        ]
        assert [m.name for m in root.members] == [f"M{i:04d}" for i in range(1, 7)]

    def test_a_stub_port_is_reached_by_a_delegation_naming_the_stub(self) -> None:
        # D = 3, R_c = 9: one spare SR port and the stub's two are reached by delegation alone,
        # the stub's ports taken first.
        project, _ = built(with_stub(3))
        stub = project.interfaces[-1]
        assert stub.kind == "external_interface"
        root = project.compositions[0]
        boundary = {p.name: p for p in root.ports}
        on_stub = [d for d in root.delegations if boundary[d.port].interface == stub.name]
        assert len(on_stub) == 2
        ports = {(c.name, p.name): p for c in project.components for p in c.ports}
        types = {m.name: m.type for m in root.members}
        for delegation in on_stub:
            inner = ports[(str(types[delegation.member]), delegation.member_port)]
            assert inner.interface == stub.name


# --- the construction executes the layout -------------------------------------------------------


@pytest.fixture
def fresh_shapes() -> Iterator[None]:
    """The shape cache cleared before and after a test that monkeypatches what it computes."""
    shape_module._shape_cached.cache_clear()
    yield
    shape_module._shape_cached.cache_clear()


def two_partly_entered() -> Layout:
    """A solver layout of `six(15)` no entry order makes.

    One star of three connectors centred on component 0 (interface 0), its require ends on
    components 1, 2 and 3; components 2 and 3 hold the two unconnected ports; components 0 and 1
    are root members **partly** entered (one port each reached by a delegation alone,
    D_root = 2), components 4 and 5 deep (D_deep = 4). So reach 6, chain steps
    min(15 - 6, 2 x 4) = 8, repeats 1.
    """
    counts = (2, 2, 2, 2, 2, 2)
    solution = star_solution(
        counts,
        (4, 4, 4),
        (1, 1, 1, 1, 2, 2),
        (1, 0, 0, 0, 0, 0),
        (2, 0, 0, 0, 0, 0),
        ((0, 0, 3),),
        ordinary=3,
        to_hubs=0,
        from_hubs=0,
        hub_to_hub=0,
    )
    return Layout.of_solution(counts, (0, 0, 1, 1, 0, 0), solution, 15, 3, source=SOLVER, step=2)


class TestTheLayoutIsExecuted:
    def test_a_solver_layout_with_two_partly_entered_components_is_built_as_decided(
        self, monkeypatch: pytest.MonkeyPatch, fresh_shapes: None
    ) -> None:
        decided = two_partly_entered()
        assert (decided.root, decided.deep) == ((0, 1, 2, 3), (4, 5))
        assert (decided.reach, decided.steps, decided.repeats) == (6, 8, 1)
        assert (decided.root_delegated, decided.deep_delegated) == (2, 4)

        def preferred_fails(*_: Any, **__: Any) -> None:
            return None

        def planned(*_: Any, **__: Any) -> Planned:
            return Planned(decided, Trace(relaxed=True, builder="first-fit"))

        received: list[Any] = []
        original = build_delegations

        def spied(*args: Any, **kwargs: Any) -> Any:
            received.append([*args, *kwargs.values()])
            return original(*args, **kwargs)

        monkeypatch.setattr(shape_module, "solve_open", preferred_fails)
        monkeypatch.setattr(layout_module, "plan", planned)
        monkeypatch.setattr(build_module, "build_delegations", spied)
        passport = six(15)
        assert the_shape(passport).layout == decided
        project, generated = built(passport)
        # The builder handed the delegations the layout itself, not a split of its own.
        assert received and all(decided in arguments for arguments in received)
        assert generated["connectors"]["delegation"] == 15
        names = [c.name for c in project.components]
        root, _, deepest = project.compositions
        assert [m.type for m in root.members if m.type] == names[:4]
        assert [m.type for m in deepest.members if m.type] == names[4:]

        def reach_from(composition: Composition) -> Counter[str]:
            types = {m.name: m.type for m in composition.members if m.type is not None}
            kinds = sources(project, composition)
            return Counter(
                str(types[d.member]) for d, kind in zip(composition.delegations, kinds, strict=True) if kind == "reach"
            )

        # D_root from both partly entered components, in the root; D_deep from the deep ones.
        assert reach_from(root) == Counter({names[0]: 1, names[1]: 1})
        assert reach_from(deepest) == Counter({names[4]: 2, names[5]: 2})
        assert Counter(sources(project)) == Counter({"reach": 6, "step": 8, "repeat": 1})

    def test_a_step_three_layout_is_realised_through_star_solution_and_meets_every_leaf(
        self, monkeypatch: pytest.MonkeyPatch, fresh_shapes: None
    ) -> None:
        def not_built(*_: Any, **__: Any) -> None:
            return None

        laid: list[tuple[Any, ...]] = []
        original = star_solution

        def spied(*args: Any, **kwargs: Any) -> Any:
            laid.append(tuple(args[5]))
            return original(*args, **kwargs)

        monkeypatch.setattr(layout_module, "solve_open", not_built)
        monkeypatch.setattr(layout_module, "first_fit", not_built)
        monkeypatch.setattr(layout_module, "star_solution", spied)
        passport = validate_passport(compute_passport(shaped_witness(501, 3)))
        # Every stated leaf met: the self-check of generation refuses a miss.
        files = build_specimen_files(SpecimenPreset("SweepSpecimen", passport))
        assert json.loads(files["generation.json"])["passport"]["connectors"]["delegation"] == 6
        shape = the_shape(passport)
        assert (shape.layout.source, shape.layout.step) == ("solver", 3)
        # Realised from the exact program's star placement, and laid from the same pieces.
        assert laid and all(pieces == shape.layout.pieces for pieces in laid)


class TestTheStubBound:
    """The ports on stubs and fan-out-1 interfaces number at most U + (R - R_c)."""

    def test_stub_ports_beyond_u_are_refused_without_delegations(self) -> None:
        assert refused(with_stub(0)) == [("interfaces.fan_out.top", "fan-out")]

    def test_stub_ports_beyond_u_are_admitted_when_delegations_reach_them(self) -> None:
        # R_c = 9 (R_c = 10 would fill the two SR interfaces of 5 with stars of two exactly).
        assert refused(with_stub(3)) == []
        shape = the_shape(with_stub(3))
        assert sum(shape.counts) - sum(shape.layout.unconnected) - shape.layout.connector_reached == 3

    def test_one_delegation_is_too_few_for_two_stub_ports(self) -> None:
        found = refusals(with_stub(1))
        assert [(r.leaf, r.refusal_class) for r in found] == [("interfaces.fan_out.top", "fan-out")]
        assert "at most 1 more are reached by a delegation alone" in found[0].sentence


class TestTheLargeScale:
    def test_the_large_scale_shape_is_admitted_with_a_large_hub_connector_free(self) -> None:
        passport = large_scale()
        assert refused(passport) == []
        shape = the_shape(passport)
        assert shape.layout.delegations == 4192 and shape.connectors == 1448
        # The 400-port hub is left connector-free, in the deepest composition.
        assert 0 in shape.layout.deep and shape.counts[0] == 400
        assert sum(shape.counts) == 3083 and sum(shape.layout.unconnected) == 107


# --- the exhaustive small oracle ----------------------------------------------------------------


def brute_admits(
    reached: Sequence[int],
    connected: int,
    connectors: int,
    delegations: int,
    compositions: int,
) -> bool:
    """Whether the ascending whole-component entry at R_c = `connected` meets D, by enumeration.

    The chain's three sources: the entry walked port by port, every number of chain steps per deep
    port, every number of repeats.
    """
    order = sorted(range(len(reached)), key=lambda j: (reached[j], j))
    taken, entered = 0, []
    for j in order:
        if taken >= connected:
            break
        entered.append(j)
        taken += reached[j]
    deep = sum(reached[j] for j in range(len(reached)) if j not in entered)
    reach = sum(reached) - connected
    steps = {sum(choice) for choice in itertools.product(range(compositions), repeat=deep)}
    repeats = range(connected - connectors + 1) if connectors else range(1)
    return any(reach + s + r == delegations for s in steps for r in repeats)


def brute_reach(
    reached: Sequence[int],
    connectors: int | None,
    delegations: int,
    compositions: int,
    ports: int,
) -> list[int]:
    """The R_c the brute force admits, from the top down.

    For a stated A those with A + 1 <= R_c <= min(2A, the SR + CS `ports`) (R_c = 0 alone for
    A = 0); for an unstated one every R_c of at most `ports` at which some A of ceil(R_c / 2) to
    R_c - 1 (or none, for R_c = 0) is admitted, tried one by one.
    """
    total = sum(reached)
    admitted = []
    for connected in range(total, -1, -1):
        if connectors is None:
            counts = [0] if connected == 0 else range(-(-connected // 2), connected)
            choices = [a for a in counts if connected <= ports or a == 0]
        elif connectors == 0:
            choices = [0] if connected == 0 else []
        elif connectors + 1 <= connected <= min(2 * connectors, ports):
            choices = [connectors]
        else:
            choices = []
        if any(brute_admits(reached, connected, a, delegations, compositions) for a in choices):
            admitted.append(connected)
    return admitted


Case = tuple[tuple[int, ...], int, int, int | None, int, int, int]


def small_box() -> list[Case]:
    """(counts, U, u1, A, D, C, the SR + CS ports) over a small box.

    Two to four components of at most three ports, at most seven in all, U at most 2, A stated or
    unstated (`None`), D from 0, and the SR + CS ports all the ports or one fewer (a stub holding
    the other).
    """
    cases = []
    for n in (2, 3, 4):
        for counts in itertools.product(range(4), repeat=n):
            if list(counts) != sorted(counts, reverse=True) or not 2 <= sum(counts) <= 7:
                continue
            for unconnected in range(3):
                for on_one in range(1, unconnected + 1) if unconnected else (0,):
                    reached = sum(counts) - unconnected
                    if reached < 0:
                        continue
                    stated: list[int | None] = [*range(0, max(1, reached)), None]
                    for connectors in stated:
                        for compositions in (1, 2, 3):
                            for ports in (sum(counts), sum(counts) - 1):
                                for delegations in range(0, compositions * reached + 2):
                                    cases.append(
                                        (
                                            counts,
                                            unconnected,
                                            on_one,
                                            connectors,
                                            delegations,
                                            compositions,
                                            ports,
                                        )
                                    )
    return cases


class TestTheExhaustiveOracle:
    def test_the_closed_form_interval_equals_the_brute_force_candidate_by_candidate(
        self,
    ) -> None:
        checked = unstated = 0
        for case in small_box():
            counts, unconnected, on_one, connectors, delegations, compositions, ports = case
            spec = ConnectorSpec(unconnected, on_one, connectors, None, 0)
            for units in unconnected_placements(spec, counts):
                reached = [c - u for c, u in zip(counts, units, strict=True)]
                closed = admitted_reach(EntryOrder.of(reached), connectors, delegations, compositions, ports)
                brute = brute_reach(reached, connectors, delegations, compositions, ports)
                assert closed == brute, (case, units)
                checked += 1
                unstated += connectors is None
        assert checked > 10000 and unstated > 2000

    def test_the_shape_keeps_the_preferred_candidate_exactly_when_it_builds(self) -> None:
        """Over a smaller box and several fan-out lists, the shape agrees with the brute force.

        The brute force's candidates in order (placements in the search's order, R_c from the top
        down): the preferred candidate, the first placement's top R_c, built with the whole search,
        is the shape exactly when it builds (`preferred`); otherwise the entry planner lays the
        configuration out (`solver`), and a refusal leaves no candidate built by any search.
        """
        checked = preferred = solver = 0
        for case in small_box():
            counts, unconnected, on_one, connectors, delegations, compositions, ports = case
            if len(counts) > 3 or compositions > 2 or connectors is None or ports < sum(counts):
                continue
            spec = ConnectorSpec(unconnected, on_one, connectors, None, 0)
            placements = list(unconnected_placements(spec, counts))
            for fan_outs in fan_out_lists(sum(counts)):
                candidates = [
                    (units, connected)
                    for units in placements
                    for connected in brute_reach(
                        [c - u for c, u in zip(counts, units, strict=True)],
                        connectors,
                        delegations,
                        compositions,
                        sum(fan_outs),
                    )
                ]

                def builds(candidate: tuple[tuple[int, ...], int], search: Search) -> bool:
                    units, connected = candidate
                    reached = [c - u for c, u in zip(counts, units, strict=True)]
                    split = EntryOrder.of(reached).split(connected)
                    open_units = tuple(u + d for u, d in zip(units, split.delegated, strict=True))
                    return solve_open(spec, counts, open_units, fan_outs, search) is not None

                stated = {
                    "connectors.delegation": delegations,
                    "layers.compositions": compositions,
                }
                result = shape_module._try_delegated(stated, spec, counts, fan_outs, fan_outs, FULL_SEARCH)
                first = bool(candidates) and candidates[0][0] == placements[0]
                first_builds = first and builds(candidates[0], FULL_SEARCH)
                if isinstance(result, Shape):
                    assert (result.layout.source == "preferred") == first_builds, case
                    if first_builds:
                        assert (
                            result.layout.unconnected,
                            result.layout.connector_reached,
                        ) == candidates[0], case
                        preferred += 1
                    else:
                        assert result.layout.source == "solver", case
                        solver += 1
                else:
                    assert not any(builds(c, FULL_SEARCH) for c in candidates), case
                checked += 1
        assert checked > 1000 and preferred > 100 and solver > 0


def fan_out_lists(ports: int) -> list[tuple[int, ...]]:
    """Descending fan-out lists of `ports` ports in one, two or three interfaces.

    Each is at least 2 (an interface of fan-out 1 is a forced unconnected port).
    """
    lists = []
    for parts in (1, 2, 3):
        for fan_outs in itertools.product(range(2, ports + 1), repeat=parts):
            if sum(fan_outs) == ports and list(fan_outs) == sorted(fan_outs, reverse=True):
                lists.append(fan_outs)
    return lists
