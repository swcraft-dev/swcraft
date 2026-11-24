"""Components: the port distribution, hubs, runnables and declared levels.

Each check has a refusal and an acceptance vector; the placement is held to the definitions of
SWCraft's pass (restated in `swcraft_specimens/passport.py`) by recomputing hubs, `top` and the
total from the counts it produces, and an exhaustive oracle over small configurations decides
independently which stated port leaves are feasible.
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import statistics
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from swcraft_specimens.contract import validate_contract
from swcraft_specimens.emit import build_file_map
from swcraft_specimens.errors import GenerationError
from swcraft_specimens.model import (
    BaseType,
    Component,
    Composition,
    CompositionMember,
    Connector,
    ConnectorEndpoint,
    Core,
    DataElement,
    Domain,
    Interface,
    Partition,
    Port,
    Processor,
    Project,
    Runnable,
    Task,
    ValueType,
    validate_project,
)
from swcraft_specimens.passport import read_passport
from swcraft_specimens.preset import ASIL
from swcraft_specimens.specimen.build import build_project
from swcraft_specimens.specimen.components import (
    ComponentShape,
    PortSpec,
    build_components,
    component_refusals,
    place_ports,
    port_placements,
    port_spec,
)
from swcraft_specimens.specimen.feasibility import check_feasibility
from swcraft_specimens.specimen.leaves import LEAVES, stated_leaves
from swcraft_specimens.specimen.preset import DEFAULT_NAME, SpecimenPreset
from swcraft_specimens.specimen.shape import PLACEMENTS_TRIED, Shape, specimen_shape
from swcraft_specimens.specimen.vocabulary import MET
from swcraft_specimens.validate import validate_file_map
from tests.oracle.specimen_helpers import load_specimen, neutral_passport

ROOT = Path(__file__).parents[1]


def stated(
    n: int,
    hubs: list[int],
    top: list[int] | None = None,
    total: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    leaves: dict[str, Any] = {"layers.swc": n, "components.hubs": hubs}
    if top is not None:
        leaves["components.ports.top"] = top
    if total is not None:
        leaves["components.ports.total"] = total
    leaves.update(extra or {})
    return leaves


def shapes_of(leaves: dict[str, Any]) -> tuple[ComponentShape, ...]:
    """The component shapes of stated port leaves, placed by the component placement alone."""
    return build_components(leaves, place_ports(port_spec(leaves)))


def refused(leaves: dict[str, Any]) -> list[tuple[str, str]]:
    return [(r.leaf, r.refusal_class) for r in component_refusals(leaves)]


def leaves_of(counts: list[int] | tuple[int, ...]) -> tuple[list[int], list[int], int]:
    """Hubs, top and total of per-component counts, by the pass's definitions."""
    threshold = 2 * statistics.median(counts) if counts else 0
    hubs = sorted((c for c in counts if c > threshold), reverse=True)
    return hubs, sorted(counts, reverse=True)[:8], sum(counts)


def assert_met(leaves: dict[str, Any]) -> tuple[int, ...]:
    assert component_refusals(leaves) == []
    counts = place_ports(port_spec(leaves))
    hubs, top, total = leaves_of(counts)
    assert len(counts) == leaves["layers.swc"]
    assert hubs == leaves["components.hubs"]
    if "components.ports.top" in leaves:
        assert top == leaves["components.ports.top"]
    if "components.ports.total" in leaves:
        assert total == leaves["components.ports.total"]
    assert list(counts) == sorted(counts, reverse=True)
    return counts


# --- one refusal and one acceptance vector per check ---------------------------------------------


class TestOrder:
    def test_top_not_descending_is_refused(self) -> None:
        assert refused(stated(3, [], [1, 2, 1], 4)) == [("components.ports.top", "order")]

    def test_top_descending_is_accepted(self) -> None:
        assert_met(stated(3, [], [2, 1, 1], 4))

    def test_top_of_the_wrong_length_is_refused(self) -> None:
        assert refused(stated(3, [], [2, 1], 4)) == [("components.ports.top", "order")]
        assert refused(stated(10, [], [2] * 7, 20)) == [("components.ports.top", "order")]

    def test_top_of_min_8_n_entries_is_accepted(self) -> None:
        assert_met(stated(10, [], [2] * 8, 20))

    def test_hubs_not_descending_are_refused(self) -> None:
        assert refused(stated(9, [5, 9], None, 20)) == [("components.hubs", "order")]

    def test_hubs_not_a_prefix_of_top_are_refused(self) -> None:
        assert refused(stated(5, [9], [8, 1, 1, 1, 1], 12)) == [("components.hubs", "order")]

    def test_hubs_a_prefix_of_top_are_accepted(self) -> None:
        assert_met(stated(5, [9], [9, 1, 1, 1, 1], 13))


class TestHubRule:
    def test_half_or_more_hubs_is_refused(self) -> None:
        assert refused(stated(4, [9, 9])) == [("components.hubs", "hub-rule")]
        assert refused(stated(3, [9, 9])) == [("components.hubs", "hub-rule")]

    def test_fewer_than_half_hubs_is_accepted(self) -> None:
        assert_met(stated(5, [9, 9]))

    def test_a_half_integer_median_is_an_acceptance_vector(self) -> None:
        # [4, 3, 0, 0]: median 1.5, so 3 is not a hub and 4 is.
        assert assert_met(stated(4, [4], [4, 3, 0, 0], 7)) == (4, 3, 0, 0)

    def test_two_central_values_are_constrained_together(self) -> None:
        # [10, 7, 6, 1, 0, 0]: median 3.5, so its only hub is 10; hubs [10, 7] are refused.
        assert refused(stated(6, [10, 7], [10, 7, 6, 1, 0, 0], 24)) == [("components.hubs", "hub-rule")]

    def test_no_hub_with_a_lone_port_is_refused(self) -> None:
        # One port among four components is always a hub: [1, 0, 0, 0] has median 0.
        assert refused(stated(4, [], None, 1)) == [("components.hubs", "hub-rule")]

    def test_a_hub_below_the_placed_top_is_refused(self) -> None:
        assert refused(stated(5, [4], [4, 4, 1, 1, 1], 11)) == [("components.hubs", "hub-rule")]


class TestPortTotal:
    def test_a_total_below_the_placed_ports_is_refused(self) -> None:
        assert refused(stated(5, [9], [9, 1, 1, 1, 1], 12)) == [("components.ports.total", "port-total")]

    def test_a_total_above_what_the_free_components_hold_is_refused(self) -> None:
        # Ten components, top eight at 2: the two free ones hold at most 2 each.
        assert refused(stated(10, [], [2] * 8, 21)) == [("components.ports.total", "port-total")]

    def test_a_total_with_no_free_component_is_exact(self) -> None:
        assert refused(stated(3, [], [2, 1, 1], 5)) == [("components.ports.total", "port-total")]
        assert_met(stated(3, [], [2, 1, 1], 4))

    def test_a_total_within_range_is_accepted(self) -> None:
        assert_met(stated(10, [], [2] * 8, 18))

    def test_ports_for_no_component_are_refused(self) -> None:
        assert refused(stated(0, [], [], 3)) == [("components.ports.total", "port-total")]
        assert refused(stated(0, [], [], 0)) == []


class TestHubsLongerThanEight:
    def test_ten_hubs_among_thirty_are_met(self) -> None:
        counts = assert_met(stated(30, [20] * 10, [20] * 8, 300))
        assert counts[:10] == (20,) * 10


class TestSum:
    def test_levels_above_n_are_refused_at_the_level_that_crosses(self) -> None:
        leaves = stated(
            3,
            [],
            extra={"components.by_declared_level.QM": 2, "components.by_declared_level.B": 2},
        )
        assert refused(leaves) == [("components.by_declared_level.B", "sum")]

    def test_levels_equal_to_n_are_accepted(self) -> None:
        leaves = stated(
            3,
            [],
            extra={"components.by_declared_level.QM": 2, "components.by_declared_level.D": 1},
        )
        assert refused(leaves) == []

    def test_runnables_for_no_component_are_refused(self) -> None:
        assert refused(stated(0, [], extra={"components.runnables": 2})) == [("components.runnables", "sum")]
        assert refused(stated(0, [], extra={"components.runnables": 0})) == []


# --- the placement against an exhaustive oracle --------------------------------------------------


def _all_counts(n: int, most: int) -> list[tuple[int, ...]]:
    return [
        tuple(sorted(values, reverse=True)) for values in itertools.combinations_with_replacement(range(most + 1), n)
    ]


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6])
def test_hubs_and_total_are_feasible_exactly_when_some_distribution_has_them(n: int) -> None:
    """Every (hubs, total) with total <= 9 is accepted iff some count vector produces it."""
    most = 9
    witnesses: set[tuple[tuple[int, ...], int]] = set()
    hub_lists: set[tuple[int, ...]] = set()
    for counts in _all_counts(n, most):
        hubs, _, total = leaves_of(counts)
        witnesses.add((tuple(hubs), total))
        hub_lists.add(tuple(hubs))
    for hub_list in sorted(hub_lists):
        for total in range(most + 1):
            leaves = stated(n, list(hub_list), None, total)
            if (hub_list, total) in witnesses:
                assert_met(leaves)
            else:
                assert refused(leaves), (n, hub_list, total)


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6, 9, 10])
def test_every_witnessed_hubs_top_and_total_is_met(n: int) -> None:
    most = 6 if n <= 6 else 3
    for counts in _all_counts(n, most):
        hubs, top, total = leaves_of(counts)
        assert_met(stated(n, hubs, top, total))
        assert_met(stated(n, hubs, top))
        assert_met(stated(n, hubs, None, total))


def test_two_large_totals_are_met_and_a_too_small_one_is_refused() -> None:
    assert assert_met(stated(2, [], None, 10_000)) == (5000, 5000)
    assert assert_met(stated(76, [428, 137, 100], None, 3000))[:3] == (428, 137, 100)
    assert refused(stated(4, [], None, 1)) == [("components.hubs", "hub-rule")]


# --- defaults ------------------------------------------------------------------------------------


class TestDefaults:
    def test_an_unstated_total_gives_two_ports_per_component(self) -> None:
        assert assert_met(stated(4, [])) == (2, 2, 2, 2)

    def test_an_unstated_total_beside_a_hub_is_the_smallest_with_two_per_component(self) -> None:
        assert assert_met(stated(5, [9])) == (9, 2, 2, 2, 2)

    def test_an_unstated_total_takes_less_when_the_hub_forbids_two(self) -> None:
        # A hub of 3 needs a median below 1.5, so the free components cannot all hold 2.
        counts = assert_met(stated(5, [3]))
        assert counts[0] == 3 and sum(counts[1:]) < 8

    def test_an_unstated_top_is_derived(self) -> None:
        counts = assert_met(stated(12, [], None, 30))
        assert sum(counts) == 30 and leaves_of(counts)[0] == []

    def test_a_specimen_without_components(self) -> None:
        assert shapes_of(stated(0, [], [], 0)) == ()


# --- runnables and declared levels ---------------------------------------------------------------


class TestRunnablesAndLevels:
    def test_runnables_are_spread_evenly_the_largest_first(self) -> None:
        shapes = shapes_of(stated(4, [], None, 10, extra={"components.runnables": 6}))
        assert [s.runnables for s in shapes] == [2, 2, 1, 1]

    def test_zero_runnable_components_are_allowed(self) -> None:
        shapes = shapes_of(stated(3, [], None, 6, extra={"components.runnables": 0}))
        assert [s.runnables for s in shapes] == [0, 0, 0]
        shapes = shapes_of(stated(3, [], None, 6, extra={"components.runnables": 1}))
        assert [s.runnables for s in shapes] == [1, 0, 0]

    def test_unstated_runnables_are_one_per_component(self) -> None:
        assert [s.runnables for s in shapes_of(stated(3, []))] == [1, 1, 1]

    def test_levels_go_from_the_smallest_component_up_in_order(self) -> None:
        leaves = stated(
            5,
            [9],
            None,
            None,
            extra={"components.by_declared_level.QM": 2, "components.by_declared_level.C": 1},
        )
        shapes = shapes_of(leaves)
        assert [s.level for s in shapes] == [ASIL.ABSENT, ASIL.ABSENT, ASIL.C, ASIL.QM, ASIL.QM]


# --- the feasibility check and the builder frame -------------------------------------------------


class TestFeasibilityFrame:
    def test_the_check_reports_the_component_refusals_at_their_leaves(self) -> None:
        passport = neutral_passport()
        passport["components"]["hubs"] = [5, 4]
        refusals = check_feasibility(passport)
        assert [(r.leaf, r.refusal_class) for r in refusals] == [("components.hubs", "order")]

    def test_the_neutral_passport_passes_the_component_checks(self) -> None:
        assert check_feasibility(neutral_passport()) == []

    def test_the_component_leaves_are_built(self) -> None:
        component_leaves = frozenset(
            {
                "layers.swc",
                "components.ports.total",
                "components.ports.top",
                "components.hubs",
                "components.runnables",
                "components.by_declared_level.QM",
                "components.by_declared_level.A",
                "components.by_declared_level.B",
                "components.by_declared_level.C",
                "components.by_declared_level.D",
            }
        )
        assert all(LEAVES[leaf].leaf_class == MET for leaf in component_leaves)
        refusals = check_feasibility(neutral_passport())
        assert not component_leaves & {r.leaf for r in refusals}


# --- the model and the emitter: no task, no level, no runnable event ----------------------------


def no_task_project(runnables: tuple[Runnable, ...] = ()) -> Project:
    """Two components, one connector, no task, no requirement, no memory; levels undeclared."""
    group = "specimen"
    first, second = "Swc0001", "Swc0002"
    return Project(
        DEFAULT_NAME,
        Processor("Cpu0001"),
        (Core("Core0001", Decimal("0.7"), ASIL.D),),
        (),
        (Domain(group, (first, second)),),
        (
            Component(first, group, ASIL.ABSENT, (Port("P1", "provide", "If0001"),), runnables, ()),
            Component(second, group, ASIL.QM, (Port("P1", "require", "If0001"),), (), ()),
        ),
        (BaseType("Bt0001", 8, "NONE", "uint8_t"), ValueType("Vt0001", "Bt0001")),
        (Interface("If0001", "sender_receiver", data_elements=(DataElement("D1", "Vt0001"),)),),
        (
            Connector(
                "Conn0001",
                "sender_receiver",
                ConnectorEndpoint(first, "P1"),
                ConnectorEndpoint(second, "P1"),
            ),
        ),
        (),
        (),
        (
            Composition(
                "Root",
                "Specimen root",
                True,
                (CompositionMember(first, first), CompositionMember(second, second)),
            ),
        ),
    )


class TestNoTaskProject:
    def test_the_no_task_project_validates_and_renders(self) -> None:
        project = no_task_project((Runnable("R1", None),))
        validate_project(project)
        file_map = build_file_map(project)
        validate_contract(file_map)
        validate_file_map(file_map, project, task_overhead_us=1)
        assert "model/tasks/.gitkeep" in file_map
        assert "model/requirements/.gitkeep" in file_map

    def test_a_runnable_without_event_renders_an_empty_event_list(self) -> None:
        project = no_task_project((Runnable("R1", None),))
        document = yaml.safe_load(build_file_map(project)["model/swc/specimen/Swc0001.yaml"])
        assert document["runnables"] == [{"name": "R1", "events": [], "reads": [], "writes": [], "calls": []}]

    def test_zero_runnable_components_validate(self) -> None:
        project = no_task_project()
        validate_project(project)
        file_map = build_file_map(project)
        validate_file_map(file_map, project, task_overhead_us=1)

    def test_a_component_without_a_level_omits_asil(self) -> None:
        file_map = build_file_map(no_task_project())
        assert "asil" not in yaml.safe_load(file_map["model/swc/specimen/Swc0001.yaml"])
        assert yaml.safe_load(file_map["model/swc/specimen/Swc0002.yaml"])["asil"] == "QM"

    def test_a_busy_task_keeps_the_one_task_rule(self) -> None:
        # A specimen's (explicit partitions) idle task maps no runnable; a busy one maps every
        # runnable exactly once.
        project = dataclasses.replace(
            no_task_project((Runnable("R1", None), Runnable("R2", None))),
            partitions=(Partition("Pa1", "Core0001"),),
        )
        idle_task = Task("Task0001", 1000, 1, "Core0001", 10, (), partition="Pa1")
        validate_project(dataclasses.replace(project, tasks=(idle_task,)))
        busy = dataclasses.replace(project, tasks=(dataclasses.replace(idle_task, runnables=(("Swc0001", "R1"),)),))
        with pytest.raises(GenerationError, match="exactly one task"):
            validate_project(busy)


class TestThePostCheck:
    def test_a_placement_that_misses_the_stated_leaves_is_a_generation_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from swcraft_specimens.specimen import components as components_module

        monkeypatch.setattr(components_module, "level_fill", lambda lows, highs, total: list(lows))
        with pytest.raises(GenerationError, match="does not reproduce"):
            place_ports(port_spec(stated(4, [], None, 10)))


# --- data accesses ------------------------------------------------------------------------------

#: The accesses each fixture gains.
FIXTURE_ACCESSES = {
    "minimal": 2,
    "topology": 360,
    "large-scale-topology": 3_000,
    "shapes": 300,
    "chain-empty": 2,
    "large-scale-shapes": 2_750,
}


def access_count(project: Project) -> int:
    return sum(
        len(runnable.reads) + len(runnable.writes)
        for component in project.components
        for runnable in component.runnables
    )


class TestDataAccesses:
    """Each component's `R1` writes its sender-receiver provide ports' data elements and reads its require ports'.

    Every data element of each port, in port order; nothing through a client-server port or a
    stub, nothing on a component with no runnable.
    """

    def test_r1_reads_and_writes_every_sender_receiver_element(self) -> None:
        project = build_project(load_specimen("shapes"))
        interfaces = {interface.name: interface for interface in project.interfaces}

        def elements(port: Port) -> list[tuple[str, str]]:
            interface = interfaces[port.interface]
            if not isinstance(interface, Interface) or interface.kind != "sender_receiver":
                return []
            return [(port.name, element.name) for element in interface.data_elements]

        for component in project.components:
            first, *rest = component.runnables
            assert list(first.writes) == [
                access for port in component.ports if port.direction == "provide" for access in elements(port)
            ]
            assert list(first.reads) == [
                access for port in component.ports if port.direction == "require" for access in elements(port)
            ]
            assert first.calls == ()
            assert all(r.reads == r.writes == () for r in rest)

    def test_the_neutral_specimen_s_accesses_by_name(self) -> None:
        # Written out by hand, not restated from the rule: the provider writes D1 through its
        # provide port P1, the requirer reads it through its require port P1.
        project = build_project(SpecimenPreset(DEFAULT_NAME, neutral_passport()))
        provider, requirer = project.components
        assert (provider.runnables[0].reads, provider.runnables[0].writes) == ((), (("P1", "D1"),))
        assert (requirer.runnables[0].reads, requirer.runnables[0].writes) == ((("P1", "D1"),), ())

    @pytest.mark.parametrize(("name", "reach"), [("shapes", "delegation-only"), ("topology", "unconnected")])
    def test_delegation_only_and_unconnected_sr_ports_carry_accesses(self, name: str, reach: str) -> None:
        # `shapes` holds 2 delegation-only SR ports and no unconnected one; `topology` 12
        # unconnected ones and no delegation.
        project = build_project(load_specimen(name))
        sender_receiver = {
            interface.name
            for interface in project.interfaces
            if isinstance(interface, Interface) and interface.kind == "sender_receiver"
        }
        ports = {
            (component.name, port.name)
            for component in project.components
            for port in component.ports
            if port.interface in sender_receiver
        }
        connected = {
            (end.swc, end.port) for connector in project.connectors for end in (connector.source, connector.target)
        }
        member_type = {
            (composition.name, member.name): member.type
            for composition in project.compositions
            for member in composition.members
            if member.composition is None
        }
        delegated = {
            (member_type[(composition.name, delegation.member)], delegation.member_port)
            for composition in project.compositions
            for delegation in composition.delegations
            if (composition.name, delegation.member) in member_type
        }
        accessed = {
            (component.name, port)
            for component in project.components
            for port, _ in (*component.runnables[0].reads, *component.runnables[0].writes)
        }
        reached = {
            "delegation-only": (ports & delegated) - connected,
            "unconnected": ports - connected - delegated,
        }[reach]
        assert reached
        assert reached <= accessed

    def test_no_access_through_a_client_server_port_or_a_stub(self) -> None:
        project = build_project(load_specimen("shapes"))
        kinds = {
            interface.name: interface.kind if isinstance(interface, Interface) else "external_interface"
            for interface in project.interfaces
        }
        port_kinds = [kinds[p.interface] for c in project.components for p in c.ports]
        # The fixture has both kinds the rule leaves out.
        assert {"client_server", "external_interface"} <= set(port_kinds)
        for component in project.components:
            port_kind = {port.name: kinds[port.interface] for port in component.ports}
            for runnable in component.runnables:
                for port_name, _ in (*runnable.reads, *runnable.writes):
                    assert port_kind[port_name] == "sender_receiver"

    def test_a_component_with_no_runnable_has_no_access(self) -> None:
        passport = neutral_passport()
        passport["components"]["runnables"] = 1
        project = build_project(SpecimenPreset(DEFAULT_NAME, passport))
        with_runnable, without = project.components
        assert without.runnables == ()
        assert access_count(project) == 1
        assert len(with_runnable.runnables[0].reads + with_runnable.runnables[0].writes) == 1

    @pytest.mark.parametrize(("name", "accesses"), sorted(FIXTURE_ACCESSES.items()))
    def test_the_access_counts_of_the_six_fixtures(self, name: str, accesses: int) -> None:
        assert access_count(build_project(load_specimen(name))) == accesses


#: The SHA-256 of each fixture's `port_placements` (`PLACEMENTS_TRIED` of them, `top` dropped, at
#: its built total; `json.dumps` of the counts lists), taken without the u1 candidate: every
#: fixture's chosen placement holds its u1, so the candidate is not tried.
EXPECTED_FIXTURE_PLACEMENTS_SHA256 = {
    "minimal": "9bae53be65fd7982530da27122b9d75690a7011d7435d3728f8a63ff1f9d312a",
    "topology": "c4dd3d30561e69af097b431243b8f13d33b230b96b7ce69a243796f3cc8700c6",
    "large-scale-topology": "be33192c5f086146751ca23e9cce860d4006b2640868e5b3f4d71c6e01130fff",
    "shapes": "c4dd3d30561e69af097b431243b8f13d33b230b96b7ce69a243796f3cc8700c6",
    "chain-empty": "9bae53be65fd7982530da27122b9d75690a7011d7435d3728f8a63ff1f9d312a",
    "large-scale-shapes": "18cf2f11b632da1e7fbabfd21eb2dc8d12cfd9fe698271994a749067d4e67786",
}
SCENARIO_UNCONNECTED_ONE_COMPONENT = (
    ROOT / "tests" / "fixtures" / "specimen-acceptance" / "scenario-unconnected-one-component.json"
)


def _one_component_spec(total: int) -> PortSpec:
    """`scenario-unconnected-one-component`'s port leaves at a trial total, as the shape asks.

    20 components, no hub, `top` and the total unstated.
    """
    passport = read_passport(SCENARIO_UNCONNECTED_ONE_COMPONENT.read_text(encoding="utf-8"))
    spec = port_spec(stated_leaves(passport))
    return PortSpec(spec.n, spec.hubs, None, total)


def _counts(spec: PortSpec, placements: list[Any]) -> list[tuple[int, ...]]:
    return [place_ports(spec, placement) for placement in placements]


class TestTheU1Candidate:
    """A candidate with one free component at exactly u1 ports replaces the chosen placement.

    It does when the chosen placement's largest count is below u1.
    """

    @pytest.mark.parametrize("total", [471, 474, 480, 561])
    def test_u1_above_the_chosen_largest_replaces_the_chosen_placement(self, total: int) -> None:
        spec = _one_component_spec(total)
        before = _counts(spec, port_placements(spec, PLACEMENTS_TRIED))
        assert max(before[0]) < 42
        after = _counts(spec, port_placements(spec, PLACEMENTS_TRIED, on_one=42))
        assert len(after) == len(before) == PLACEMENTS_TRIED
        first = after[0]
        assert 42 in first
        assert sum(first) == total
        assert len(first) == 20
        assert max(first) <= 2 * statistics.median(first)
        # The others level-filled: above the median and at or below it, each side level.
        median = statistics.median(first)
        rest = sorted(first, reverse=True)[1:]
        above = [count for count in rest if count > median]
        below = [count for count in rest if count <= median]
        assert max(above, default=0) - min(above, default=0) <= 1
        assert max(below) - min(below) <= 1
        assert after[1:] == [counts for counts in before[1:] if counts != first][: len(after) - 1]

    def test_the_u1_placement_of_480(self) -> None:
        spec = _one_component_spec(480)
        first = _counts(spec, port_placements(spec, PLACEMENTS_TRIED, on_one=42))[0]
        assert first == (42, *[26] * 7, 25, *[21] * 11)

    def test_u1_at_or_below_the_chosen_largest_leaves_the_list_unchanged(self) -> None:
        spec = _one_component_spec(480)
        before = port_placements(spec, PLACEMENTS_TRIED)
        largest = max(place_ports(spec, before[0]))
        for on_one in range(largest + 1):
            assert port_placements(spec, PLACEMENTS_TRIED, on_one=on_one) == before

    def test_no_u1_candidate_within_the_hub_rule_leaves_the_list_unchanged(self) -> None:
        # Two components of 4 ports: no non-hub holds 10, so the candidate does not exist.
        spec = PortSpec(2, (), None, 4)
        before = port_placements(spec, PLACEMENTS_TRIED)
        assert port_placements(spec, PLACEMENTS_TRIED, on_one=10) == before

    @pytest.mark.parametrize("name", sorted(EXPECTED_FIXTURE_PLACEMENTS_SHA256))
    def test_every_fixture_keeps_its_placements(self, name: str) -> None:
        leaves = stated_leaves(load_specimen(name).passport)
        shape = specimen_shape(leaves)
        assert isinstance(shape, Shape)
        spec = port_spec(leaves)
        trial = PortSpec(spec.n, spec.hubs, None, sum(shape.counts))
        on_one = int(leaves["components.unconnected.max_on_one"])
        counts = [list(counts) for counts in _counts(trial, port_placements(trial, PLACEMENTS_TRIED, on_one=on_one))]
        digest = hashlib.sha256(json.dumps(counts).encode("utf-8")).hexdigest()
        assert digest == EXPECTED_FIXTURE_PLACEMENTS_SHA256[name]
