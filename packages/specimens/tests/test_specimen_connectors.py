"""Connectors, links and unconnected ports.

Each inequality has a refusal and an acceptance vector; the built connectors are held to the
definitions of SWCraft's pass (restated in `swcraft_specimens/passport.py`) by recomputing the
unconnected ports, the connectors and the links from the ports and connectors the construction
produces. Two independent oracles decide which configurations are realisable: an exhaustive
enumeration of every connection structure of a few ports, built port by port, and models built at
random in that connection model, whose own leaves are fed back as the configuration.
"""

from __future__ import annotations

import itertools
import json
import random
import statistics
import tracemalloc
from collections import Counter
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from swcraft_specimens.errors import GenerationError
from swcraft_specimens.model import (
    AnyInterface,
    Argument,
    DataElement,
    ExternalInterface,
    Interface,
    Operation,
)
from swcraft_specimens.passport import Refusal
from swcraft_specimens.specimen import connectors as connectors_module
from swcraft_specimens.specimen import shape as shape_module
from swcraft_specimens.specimen.build import build_specimen_files, render_specimen
from swcraft_specimens.specimen.components import place_ports, port_spec
from swcraft_specimens.specimen.connectors import (
    PACK_BUDGET,
    ConnectorPlan,
    ConnectorSpec,
    Search,
    build_connectors,
    configuration_solution,
    connector_count,
    connector_refusals,
    connector_spec,
    solve_open,
    star_solution,
    unconnected_placements,
)
from swcraft_specimens.specimen.feasibility import check_feasibility
from swcraft_specimens.specimen.interfaces import split_fan_outs
from swcraft_specimens.specimen.leaves import LEAVES
from swcraft_specimens.specimen.preset import DEFAULT_NAME, SpecimenPreset
from swcraft_specimens.specimen.vocabulary import MET
from tests.oracle.specimen_helpers import (
    clear_search_caches,
    connection_structures,
    count_calls,
    descending_partitions,
    neutral_passport,
    packable,
)

ASSEMBLY = "connectors.assembly"
LAYER = "layers.connectors"
LINKS = "connectors.links"
TOTAL = "components.unconnected.total"
MAX_ON_ONE = "components.unconnected.max_on_one"
FAN_OUT_TOP = "interfaces.fan_out.top"
MINIMAL_PASSPORT = Path(__file__).parent / "fixtures" / "passport" / "minimal.swcraft-passport.json"


def spec(
    unconnected: int = 0,
    max_on_one: int | None = None,
    assembly: int | None = None,
    links: int = 0,
    *,
    layer: int | None = None,
) -> ConnectorSpec:
    """The connector leaves; `max_on_one` defaults to the plainest value for `unconnected`."""
    if max_on_one is None:
        max_on_one = 1 if unconnected else 0
    return ConnectorSpec(unconnected, max_on_one, assembly, layer, links)


def refused(counts: Sequence[int], fan_outs: Sequence[int], leaves: ConnectorSpec) -> list[tuple[str, str]]:
    return [(r.leaf, r.refusal_class) for r in connector_refusals(leaves, counts, fan_outs)]


def named_interfaces(fan_outs: Sequence[int]) -> tuple[Interface, ...]:
    """One interface per fan-out, alternating the two kinds."""
    interfaces: list[Interface] = []
    for index in range(len(fan_outs)):
        name = f"If{index + 1:04d}"
        if index % 2 == 0:
            interfaces.append(Interface(name, "sender_receiver", data_elements=(DataElement("D1", "Vt0001"),)))
        else:
            interfaces.append(
                Interface(
                    name,
                    "client_server",
                    operations=(Operation("O1", (Argument("A1", "in", "Vt0001"),)),),
                )
            )
    return tuple(interfaces)


def names(n: int) -> tuple[str, ...]:
    return tuple(f"Swc{index + 1:04d}" for index in range(n))


def hub_count(counts: Sequence[int]) -> int:
    """The hubs by the pass's definition: above twice the median of every component's count."""
    if not counts:
        return 0
    threshold = 2 * statistics.median(counts)
    return sum(1 for count in counts if count > threshold)


def build(counts: Sequence[int], fan_outs: Sequence[int], leaves: ConnectorSpec) -> ConnectorPlan:
    connectors = connector_count(leaves, counts, fan_outs)
    assert connectors is not None
    return build_connectors(
        leaves,
        counts,
        fan_outs,
        named_interfaces(fan_outs),
        names(len(counts)),
        tuple(f"Conn{index:04d}" for index in range(1, connectors + 1)),
    )


def assert_built(counts: Sequence[int], fan_outs: Sequence[int], leaves: ConnectorSpec) -> ConnectorPlan:
    """Build, then recompute every connector leaf from the ports and the connectors alone."""
    plan = build(counts, fan_outs, leaves)
    component_names = names(len(counts))
    interfaces = {i.name: i for i in named_interfaces(fan_outs)}
    ports = {
        (component, port.name): port
        for component, component_ports in zip(component_names, plan.ports, strict=True)
        for port in component_ports
    }
    assert [len(p) for p in plan.ports] == list(counts)
    fan_out = Counter(port.interface for port in ports.values())
    assert sorted(fan_out.values(), reverse=True) == sorted(fan_outs, reverse=True)
    for name, count in fan_out.items():
        assert count == fan_outs[list(interfaces).index(name)]
    # The connection model: provide -> require of one interface on two components.
    reached: Counter[tuple[str, str]] = Counter()
    for connector in plan.connectors:
        source, target = connector.source, connector.target
        assert source.swc is not None and target.swc is not None
        assert source.swc != target.swc
        provide, require = ports[(source.swc, source.port)], ports[(target.swc, target.port)]
        assert (provide.direction, require.direction) == ("provide", "require")
        assert provide.interface == require.interface
        assert connector.kind == interfaces[provide.interface].kind
        assert connector.composition is None
        reached[(source.swc, source.port)] += 1
        reached[(target.swc, target.port)] += 1
    for key, port in ports.items():
        if port.direction == "require":
            assert reached[key] <= 1
        else:
            assert reached[key] >= 1
    per_component = Counter(component for component, _ in ports if (component, _) not in reached)
    unconnected = sum(per_component.values())
    assert unconnected == leaves.unconnected
    assert max(per_component.values(), default=0) == leaves.max_on_one
    assembly = leaves.assembly if leaves.assembly is not None else leaves.layer
    if assembly is not None:
        assert len(plan.connectors) == assembly
    k = hub_count(counts)
    ordinary = set(component_names[k:])
    between = sum(1 for c in plan.connectors if c.source.swc in ordinary and c.target.swc in ordinary)
    assert max(0, between - len(ordinary) // 2) == leaves.links
    return plan


def large_scale() -> tuple[list[int], tuple[int, ...]]:
    """A large vector, placed by the component and interface builders.

    76 components, the largest 428, the heaviest interface 597, 1,375 connectors, 285 unconnected,
    137 on one component.
    """
    leaves: dict[str, Any] = {
        "layers.swc": 76,
        "components.hubs": [428, 137, 100],
        "components.ports.total": 3000,
        "components.unconnected.total": 285,
        "layers.interfaces": 650,
        "interfaces.sender_receiver": 600,
        "interfaces.client_server": 50,
        "interfaces.fan_out.max": 597,
        "layers.implementation_types": 1,
        "layers.base_types": 1,
    }
    counts = list(place_ports(port_spec(leaves)))
    fan_outs = split_fan_outs(leaves, sum(counts))
    assert not isinstance(fan_outs, Refusal)
    return counts, fan_outs


# --- the inequalities, one refusal and one acceptance vector each -----------------------------


class TestConnectorRange:
    """R = P - U reached ports: R >= 2 and ceil(R / 2) <= A <= R - 1."""

    def test_fewer_connectors_than_half_the_reached_ports_are_refused(self) -> None:
        # [2, 2], fan-outs [2, 2]: R = 4 needs A >= 2; A = 1 meets the links rule (A <= m // 2).
        assert refused([2, 2], [2, 2], spec(assembly=1)) == [(ASSEMBLY, "connectors")]

    def test_half_the_reached_ports_is_accepted(self) -> None:
        assert_built([2, 2], [2, 2], spec(assembly=2, links=1))

    def test_as_many_connectors_as_reached_ports_are_refused(self) -> None:
        # [3, 1], one interface of 4: A = 4 = R is one connector per port, no provide port left.
        assert refused([3, 1], [4], spec(assembly=4, links=3)) == [(ASSEMBLY, "connectors")]

    def test_one_fewer_than_the_reached_ports_is_accepted(self) -> None:
        # One provide port on the one-port component serves three require ports on the other.
        assert_built([3, 1], [4], spec(assembly=3, links=2))

    def test_one_reached_port_is_refused(self) -> None:
        assert refused([1, 1], [2], spec(1, 1, assembly=1)) == [(ASSEMBLY, "connectors")]


class TestNoConnectorExactlyWithoutReachedPorts:
    """A = 0 exactly when R = 0."""

    def test_no_connector_with_reached_ports_is_refused(self) -> None:
        assert refused([1, 1], [2], spec(assembly=0)) == [(ASSEMBLY, "connectors")]

    def test_connectors_without_reached_ports_are_refused(self) -> None:
        assert refused([1, 1], [1, 1], spec(2, 1, assembly=1)) == [(ASSEMBLY, "connectors")]

    def test_no_connector_and_no_reached_port_is_accepted(self) -> None:
        plan = assert_built([1, 1], [1, 1], spec(2, 1, assembly=0))
        assert plan.connectors == ()


class TestConnectorsWithinTheFanOut:
    """A <= sum(f - 1): each named interface needs a provide port of its own."""

    def test_the_plan_vector_is_refused(self) -> None:
        # Fan-outs [2, 2], P = 4, U = 0, A = 3 (four one-port components, links 1: 2 + 1 = 3).
        assert refused([1, 1, 1, 1], [2, 2], spec(assembly=3, links=1)) == [(ASSEMBLY, "connectors")]

    def test_within_the_fan_out_is_accepted(self) -> None:
        assert_built([1, 1, 1, 1], [2, 2], spec(assembly=2))


class TestUnconnected:
    """u1 <= U, u1 <= the largest component, U <= sum(min(p_j, u1)), u1 > 0 when U > 0."""

    def test_more_on_one_than_in_all_is_refused(self) -> None:
        assert refused([2, 2], [4], spec(1, 2, assembly=2, links=1)) == [(MAX_ON_ONE, "unconnected")]

    def test_more_on_one_than_the_largest_component_holds_is_refused(self) -> None:
        assert refused([1, 1], [1, 1], spec(2, 2, assembly=0)) == [(MAX_ON_ONE, "unconnected")]

    def test_the_plan_vector_beyond_the_spread_bound_is_refused(self) -> None:
        # Counts [6, 1, 1, 0], U = 5, u1 = 2: at most 2 + 1 + 1 + 0 = 4 unconnected.
        assert refused([6, 1, 1, 0], [8], spec(5, 2, assembly=2)) == [(MAX_ON_ONE, "unconnected")]

    def test_unconnected_ports_with_none_on_any_component_are_refused(self) -> None:
        assert refused([2, 2], [4], spec(1, 0, assembly=2, links=1)) == [(MAX_ON_ONE, "unconnected")]

    def test_the_spread_bound_is_accepted(self) -> None:
        # Counts [4, 2, 2, 2] (no hub), U = 4, u1 = 1: three pairs, all between ordinary ones.
        plan = assert_built([4, 2, 2, 2], [10], spec(4, 1, assembly=3, links=1))
        assert len(plan.connectors) == 3

    def test_unconnected_beside_connectors_are_accepted(self) -> None:
        assert_built([3, 3, 2], [4, 3, 1], spec(2, 1, assembly=3, links=2))


class TestLinks:
    """L > 0: C_oo = m // 2 + L; L = 0: C_oo <= m // 2; no hub: A = C_oo."""

    def test_no_hub_with_connectors_other_than_pairs_and_links_is_refused(self) -> None:
        assert refused([1, 1, 1, 1], [2, 2], spec(assembly=2, links=1)) == [(LINKS, "links")]

    def test_no_hub_with_pairs_and_links_is_accepted(self) -> None:
        assert_built([1, 1, 1, 1], [2, 2], spec(assembly=2, links=0))

    def test_four_components_one_assembly_no_hub_is_accepted(self) -> None:
        # No hub, L = 0, A = 1 below m // 2 = 2: only one pair is connected.
        assert_built([1, 1, 0, 0], [2], spec(assembly=1))

    def test_four_components_four_assemblies_no_hub_is_refused(self) -> None:
        assert refused([2, 2, 2, 2], [8], spec(assembly=4)) == [(LINKS, "links")]

    def test_four_components_four_assemblies_no_hub_with_two_links_is_accepted(self) -> None:
        assert_built([2, 2, 2, 2], [8], spec(assembly=4, links=2))

    def test_links_beyond_the_connectors_with_a_hub_are_refused(self) -> None:
        # One hub (6), m = 3: links 9 asks for C_oo = 10 of 9 connectors.
        assert refused([6, 2, 2, 2], [6, 3, 3], spec(assembly=9, links=9)) == [(LINKS, "links")]

    def test_links_with_a_hub_are_accepted(self) -> None:
        # C_oo = 1 + 1 = 2; the hub's six ports take the other seven connectors.
        assert_built([6, 2, 2, 2], [6, 3, 3], spec(assembly=9, links=1))


class TestHubConnectors:
    """C_hub = A - C_oo is decided by the construction, not by the hub's port count."""

    def test_the_plan_vector_beyond_the_hub_ports_is_accepted(self) -> None:
        # Counts [3, 1, 1, 1, 1], fan-outs [3, 2, 2], A = 4, U = 0, L = 0: three hub provide
        # ports serve four require ports, so C_hub = 4 exceeds the hub's 3 ports.
        assert hub_count([3, 1, 1, 1, 1]) == 1
        plan = assert_built([3, 1, 1, 1, 1], [3, 2, 2], spec(assembly=4))
        hub = names(5)[0]
        assert sum(1 for c in plan.connectors if hub in (c.source.swc, c.target.swc)) >= 2

    def test_a_hub_shortfall_is_refused_as_links(self) -> None:
        # [3, 1, 1] (one hub), U = 1, A = 3: one star of three require ports. With the
        # unconnected port on an ordinary component one ordinary port is left, too few for a hub
        # provider; so it starts at an ordinary component and needs four ports of one interface,
        # and the fan-outs [3, 2] hold at most three.
        assert hub_count([3, 1, 1]) == 1
        assert refused([3, 1, 1], [3, 2], spec(1, 1, assembly=3)) == [(ASSEMBLY, "links")]
        assert_built([3, 1, 1], [5], spec(1, 1, assembly=3))


class TestConnectorSum:
    def test_assembly_other_than_the_layer_count_is_refused(self) -> None:
        assert refused([1, 1], [2], spec(assembly=1, layer=2)) == [(ASSEMBLY, "sum")]

    def test_assembly_equal_to_the_layer_count_is_accepted(self) -> None:
        assert_built([1, 1], [2], spec(assembly=1, layer=1))

    def test_the_layer_count_alone_is_the_connector_count(self) -> None:
        assert_built([1, 1], [2], spec(layer=1))


class TestExtraNecessaryConditions:
    """The necessary conditions beyond the basic inequalities."""

    def test_a_component_with_more_reached_ports_than_connectors_is_refused(self) -> None:
        # [5, 1] (no hub), one interface of 6, A = 3, links 2 (A = 1 + 2): five reached ports
        # on one component each need a connector of their own, and there are three.
        assert refused([5, 1], [6], spec(assembly=3, links=2)) == [(ASSEMBLY, "connectors")]
        assert_built([5, 1], [6], spec(assembly=5, links=4))

    def test_links_beyond_the_ordinary_ports_are_refused(self) -> None:
        # [3, 1, 0, 0, 0]: two hubs, the three ordinary components hold no port, so no
        # connector joins two of them and L = 1 (C_oo = 2) cannot be met.
        assert hub_count([3, 1, 0, 0, 0]) == 2
        assert refused([3, 1, 0, 0, 0], [4], spec(assembly=3, links=1)) == [(LINKS, "links")]
        assert_built([3, 1, 0, 0, 0], [4], spec(assembly=3, links=0))

    def test_hub_ports_beyond_the_connectors_with_a_hub_end_are_refused(self) -> None:
        # [4, 4, 1, 1, 1, 1, 0]: two hubs, L = 1 puts 3 of A = 6 connectors between ordinary
        # components; the other 3 have at most 6 hub ends, for the hubs' 8 ports.
        assert hub_count([4, 4, 1, 1, 1, 1, 0]) == 2
        assert refused([4, 4, 1, 1, 1, 1, 0], [12], spec(assembly=6, links=1)) == [(LINKS, "links")]
        assert_built([4, 4, 1, 1, 1, 1, 0], [12], spec(assembly=7, links=1))

    def test_ordinary_ports_beyond_the_connector_ends_are_refused(self) -> None:
        # The large-scale configuration with links 40: 76 connectors between ordinary
        # components, whose 2,187 or more reached ports need more ends than 76 + 1,375.
        counts, fan_outs = large_scale()
        assert refused(counts, fan_outs, spec(285, 137, assembly=1375, links=40)) == [(LINKS, "links")]

    def test_unconnected_ports_the_interfaces_cannot_absorb_are_refused(self) -> None:
        # Fan-outs [3, 3], U = 2, A = 3: an interface's reached ports are 0 or at least 2, so
        # U = 2 leaves two interfaces of 2 reached ports, one connector each.
        assert refused([2, 2, 2], [3, 3], spec(2, 1, assembly=3, links=2)) == [(ASSEMBLY, "connectors")]
        assert_built([2, 2, 2], [3, 3], spec(2, 1, assembly=2, links=1))


# --- defaults ------------------------------------------------------------------------------------


class TestDefaults:
    def test_an_unstated_connector_count_is_the_smallest_feasible(self) -> None:
        plan = build([2, 2, 2, 2], [8], spec(links=2))
        assert len(plan.connectors) == 4

    def test_unstated_connectors_are_refused_when_none_is_feasible(self) -> None:
        assert refused([5, 1], [6], spec(links=0)) == [(LINKS, "links")]


# --- the construction --------------------------------------------------------------------------


class TestConstruction:
    def test_every_connector_joins_two_components_of_one_interface(self) -> None:
        plan = assert_built([5, 3, 2, 2], [5, 4, 3], spec(2, 2, assembly=5, links=3))
        assert len(plan.connectors) == 5

    def test_several_ports_of_one_interface_on_one_component_are_connected(self) -> None:
        # [5, 3, 2, 2] with one interface of 12: each component holds several of its ports.
        plan = assert_built([5, 3, 2, 2], [12], spec(assembly=6, links=4))
        assert len({c.source.port for c in plan.connectors}) >= 1

    def test_connector_and_port_names(self) -> None:
        plan = assert_built([2, 2], [2, 2], spec(assembly=2, links=1))
        assert [c.name for c in plan.connectors] == ["Conn0001", "Conn0002"]
        for ports in plan.ports:
            assert [p.name for p in ports] == [f"P{i}" for i in range(1, len(ports) + 1)]

    def test_the_construction_is_deterministic(self) -> None:
        leaves = spec(2, 2, assembly=5, links=3)
        assert build([5, 3, 2, 2], [5, 4, 3], leaves) == build([5, 3, 2, 2], [5, 4, 3], leaves)

    def test_no_component_and_no_port(self) -> None:
        plan = assert_built([], [], spec(assembly=0))
        assert plan.ports == () and plan.connectors == ()

    def test_components_without_ports(self) -> None:
        plan = assert_built([0, 0, 0], [], spec(assembly=0))
        assert plan.ports == ((), (), ())

    def test_a_large_scale_configuration_is_met_by_the_first_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The greedy packings meet this configuration at the first count and placement tried, so the
        # exact search never runs: the work is bounded by the counts below, not the wall clock.
        counts, fan_outs = large_scale()
        calls = count_calls(monkeypatch, connectors_module, "_solve_fixed", "_pack", "_exact_pack")
        clear_search_caches()
        assert_built(counts, fan_outs, spec(285, 137, assembly=1375, links=900))
        assert calls == Counter({"_solve_fixed": 1, "_pack": 1})


# --- the oracles ---------------------------------------------------------------------------------


def _achievable(counts: tuple[int, ...], fan_outs: tuple[int, ...]) -> set[tuple[int, ...]]:
    """(U, u1, A, L) of every connection structure whose stars pack, its open ports unconnected.

    The shared port-level enumeration, `tests/oracle/specimen_helpers.py`.
    """
    ordinary = len(counts) - hub_count(counts)
    found: set[tuple[int, ...]] = set()
    for reached, a, between, stars in connection_structures(counts):
        if packable(stars, fan_outs):
            unconnected = [count - r for count, r in zip(counts, reached, strict=True)]
            found.add((sum(unconnected), max(unconnected, default=0), a, max(0, between - ordinary // 2)))
    return found


def _configurations(max_ports: int, max_components: int) -> Iterator[tuple[Any, ...]]:
    for n in range(1, max_components + 1):
        for ports in range(max_ports + 1):
            for head in descending_partitions(ports):
                if len(head) > n:
                    continue
                counts = (*head, *(0,) * (n - len(head)))
                for fan_outs in descending_partitions(ports):
                    yield counts, fan_outs


@pytest.mark.parametrize("max_components", [1, 2, 3, 4])
def test_the_construction_meets_exactly_what_some_structure_has(max_components: int) -> None:
    """Every (U, u1, A, L) is accepted iff some connection structure of six ports has it."""
    for counts, fan_outs in _configurations(6, max_components):
        if len(counts) != max_components:
            continue
        witnesses = _achievable(counts, fan_outs)
        ports = sum(counts)
        for u in range(ports + 1):
            for u1 in range(u + 1):
                for a in range(ports + 1):
                    for links in range(4):
                        leaves = spec(u, u1, assembly=a, links=links)
                        accepted = refused(counts, fan_outs, leaves) == []
                        assert accepted is ((u, u1, a, links) in witnesses), (
                            counts,
                            fan_outs,
                            leaves,
                        )
                        if accepted:
                            assert_built(counts, fan_outs, leaves)


def _random_model(rng: random.Random) -> tuple[list[int], list[int], ConnectorSpec]:
    """A model built at random in the connection model, and its own leaves.

    Components get ports; stars (a provide port and require ports on other components) are
    drawn over them; the stars and the unconnected ports are grouped into interfaces.
    """
    n = rng.randint(2, 14)
    ports_of = [rng.randint(0, 9) for _ in range(n)]
    if rng.random() < 0.3:
        ports_of[0] += rng.randint(10, 40)
    free = [[c] * count for c, count in enumerate(ports_of)]
    slots = [port for ports in free for port in ports]
    rng.shuffle(slots)
    stars: list[list[int]] = []
    unconnected: list[int] = []
    while slots:
        provider = slots.pop()
        others = [s for s in slots if s != provider]
        if not others or rng.random() < 0.15:
            unconnected.append(provider)
            continue
        size = min(len(others), rng.choice([1, 1, 1, 2, 3, 6]))
        chosen: list[int] = []
        for _ in range(size):
            pick = next(s for s in slots if s != provider)
            slots.remove(pick)
            chosen.append(pick)
        stars.append([provider, *chosen])
    counts = sorted(ports_of, reverse=True)
    order = sorted(range(n), key=lambda c: (-ports_of[c], c))
    rank = {component: index for index, component in enumerate(order)}
    k = hub_count(counts)
    # Interfaces: each star alone or merged with the next; unconnected ports spread over them.
    groups: list[int] = []
    for star in stars:
        if groups and rng.random() < 0.4:
            groups[-1] += len(star)
        else:
            groups.append(len(star))
    for _ in unconnected:
        if groups and rng.random() < 0.7:
            groups[rng.randrange(len(groups))] += 1
        else:
            groups.append(1)
    per_component = Counter(rank[c] for c in unconnected)
    between = sum(1 for star in stars for target in star[1:] if rank[star[0]] >= k and rank[target] >= k)
    links = max(0, between - (n - k) // 2)
    leaves = ConnectorSpec(
        len(unconnected),
        max(per_component.values(), default=0),
        sum(len(star) - 1 for star in stars),
        None,
        links,
    )
    return counts, sorted(groups, reverse=True), leaves


@pytest.mark.parametrize("seed", range(120))
def test_a_random_model_s_own_leaves_are_met(seed: int) -> None:
    counts, fan_outs, leaves = _random_model(random.Random(seed))
    assert refused(counts, fan_outs, leaves) == []
    assert_built(counts, fan_outs, leaves)


@pytest.mark.parametrize("seed", range(0, 120, 4))
def test_the_greedy_matrix_builds_what_the_max_flow_builds(seed: int, monkeypatch: pytest.MonkeyPatch) -> None:
    # Above `MAX_FLOW_ARCS` the greedy lays the connectors; with the bound at 0 it lays every
    # random model's, and the post-check holds them to the leaves.
    monkeypatch.setattr(connectors_module, "MAX_FLOW_ARCS", 0)
    counts, fan_outs, leaves = _random_model(random.Random(seed))
    assert_built(counts, fan_outs, leaves)


def _matrix_sums(matrix: dict[tuple[int, int], int], size: int) -> tuple[list[int], list[int]]:
    rows, columns = [0] * size, [0] * size
    for (row, column), amount in matrix.items():
        assert row != column and amount > 0
        rows[row] += amount
        columns[column] += amount
    return rows, columns


@pytest.mark.parametrize("size", [2, 3, 4])
def test_the_greedy_matrix_exists_exactly_when_the_max_flow_one_does(size: int) -> None:
    # Every pair of sum vectors of up to three per node: the greedy lays a zero-diagonal matrix
    # with those sums exactly when Dinic's max-flow does.
    for outs in itertools.product(range(4), repeat=size):
        for ins in itertools.product(range(4), repeat=size):
            if sum(outs) != sum(ins):
                continue
            try:
                connectors_module._max_flow_matrix(outs, ins)
            except GenerationError:
                with pytest.raises(GenerationError):
                    connectors_module._greedy_matrix(outs, ins)
                continue
            greedy = connectors_module._greedy_matrix(outs, ins)
            assert _matrix_sums(greedy, size) == (list(outs), list(ins))


def test_a_side_of_ten_thousand_one_port_components_is_laid_in_linear_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 10,000 one-port components, 5,000 connectors in one interface of 10,000 ports: the max-flow
    # graph once had 25 million middle arcs and a dense 10,001 x 10,001 matrix (over 3 GB); the
    # greedy lays it in milliseconds. The work is bounded by the route (the greedy, never the
    # max-flow) and by the matrix holding one entry per connector.
    calls = count_calls(monkeypatch, connectors_module, "_max_flow_matrix", "_greedy_matrix")
    outs = [1] * 5000 + [0] * 5000 + [0]
    ins = [0] * 5000 + [1] * 5000 + [0]
    tracemalloc.start()
    try:
        matrix = connectors_module._matrix(outs, ins)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert _matrix_sums(matrix, len(outs)) == (outs, ins)
    assert calls == Counter({"_greedy_matrix": 1})
    assert len(matrix) == 5000
    assert peak < 64 * 2**20


@pytest.mark.parametrize(
    ("counts", "spec_", "expected"),
    [
        ((4,) + (1,) * 9996 + (0,) * 3, ConnectorSpec(0, 0, None, None, 1), 4998),
        ((0,) * 9000 + (10,) * 1000, ConnectorSpec(0, 0, None, None, 0), 4500),
    ],
)
def test_the_connector_candidates_of_ten_thousand_components_read_the_counts_once(
    counts: tuple[int, ...], spec_: ConnectorSpec, expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each derived A candidate once re-took the median and re-summed the counts (about 4 s for these
    # 10,000 components); the counts' terms are taken once per configuration.
    calls = count_calls(monkeypatch, connectors_module, "hub_count", "_terms", "_connector_refusal")
    candidates = connectors_module._connector_candidates(spec_, counts, (10_000,))
    assert len(candidates) == expected
    assert calls["_connector_refusal"] == 5000
    assert calls["_terms"] + calls["hub_count"] < 10


def test_a_feasibility_case_of_ten_thousand_components_reads_the_counts_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The same through check_feasibility: 10,000 components, 1,000 hubs of 10 ports, one interface
    # of fan-out 10,000, A unstated. The bound is on the scans of the counts per candidate, not on
    # the wall clock, which a loaded CI runner exceeds.
    scans = count_calls(monkeypatch, connectors_module, "hub_count", "_terms", "_connector_refusal")
    passport = json.loads(MINIMAL_PASSPORT.read_text(encoding="utf-8"))
    passport["layers"].update(swc=10_000, interfaces=1)
    del passport["layers"]["connectors"]
    components = passport["components"]
    components.update(hubs=[10] * 1000, runnables=10_000)
    components["ports"] = {key: value for key, value in components["ports"].items() if key != "top"} | {"total": 10_000}
    passport["interfaces"].update(sender_receiver=1, client_server=0, fan_out={"max": 10_000, "top": [10_000]})
    del passport["connectors"]["assembly"]
    passport["connectors"]["links"] = 0
    passport["identifiers"].update(
        max_length=8,
        swc={"max_length": 8, "mean_length": 8.0},
        connectors={"max_length": 8, "mean_length": 8.0},
    )
    shape_module._shape_cached.cache_clear()
    connectors_module._solve_cached.cache_clear()
    assert check_feasibility(passport) == []
    # Thousands of candidates are checked, so the bound below means something ...
    assert scans["_connector_refusal"] > 4000
    # ... over the counts' terms taken once per configuration, not once per candidate.
    assert scans["_terms"] + scans["hub_count"] < 10


# --- the feasibility check and the builder frame -------------------------------------------------


class TestFeasibilityFrame:
    def test_the_connector_leaves_are_read_from_the_passport(self) -> None:
        passport = neutral_passport()
        leaves = connector_spec(
            {
                TOTAL: 0,
                MAX_ON_ONE: 0,
                ASSEMBLY: 1,
                LAYER: 1,
                LINKS: 0,
            }
        )
        assert leaves == ConnectorSpec(0, 0, 1, 1, 0)
        assert check_feasibility(passport) == []

    def test_the_check_reports_the_connector_refusals_at_their_leaves(self) -> None:
        passport = neutral_passport()
        passport["connectors"]["links"] = 1
        passport["connectors"]["assembly"] = 2
        passport["layers"]["connectors"] = 2
        refusals = check_feasibility(passport)
        assert [(r.leaf, r.refusal_class) for r in refusals] == [(ASSEMBLY, "connectors")]

    def test_a_sum_refusal_reaches_the_check(self) -> None:
        passport = neutral_passport()
        passport["layers"]["connectors"] = 2
        refusals = check_feasibility(passport)
        assert [(r.leaf, r.refusal_class) for r in refusals] == [(ASSEMBLY, "sum")]

    def test_the_connector_leaves_are_built(self) -> None:
        connector_leaves = frozenset({LAYER, ASSEMBLY, LINKS, TOTAL, MAX_ON_ONE})
        assert all(LEAVES[leaf].leaf_class == MET for leaf in connector_leaves)
        refusals = check_feasibility(neutral_passport())
        assert not connector_leaves & {r.leaf for r in refusals}


# --- the external interface stubs: no connector names one --------------------------------------


def stub_passport(unconnected: int = 1) -> dict[str, Any]:
    """Two components, one SR interface with one connector, and one stub holding the third port.

    The components hold 2 and 1 ports, the interface has fan-out 2, and layers.interfaces =
    SR + CS + 1.
    """
    passport = neutral_passport()
    passport["layers"]["interfaces"] = 2
    passport["components"]["ports"] = {"total": 3, "top": [2, 1]}
    passport["components"]["unconnected"] = {
        "total": unconnected,
        "max_on_one": min(unconnected, 1),
    }
    passport["interfaces"]["fan_out"] = {"max": 2, "top": [2, 1]}
    return passport


class TestStubs:
    def test_no_connector_names_a_stub_and_its_ports_stay_unconnected(self) -> None:
        rendered = render_specimen(SpecimenPreset(DEFAULT_NAME, stub_passport()))
        project = rendered.project
        stubs = {i.name for i in project.interfaces if isinstance(i, ExternalInterface)}
        assert len(stubs) == 1
        stub_ports = {
            (component.name, port.name)
            for component in project.components
            for port in component.ports
            if port.interface in stubs
        }
        assert len(stub_ports) == 1
        ends = {(c.source.swc, c.source.port) for c in project.connectors} | {
            (c.target.swc, c.target.port) for c in project.connectors
        }
        assert not stub_ports & ends
        assert all(c.kind != "external_interface" for c in project.connectors)
        # The pass counts the stub's fan-out like any interface's.
        passport = rendered.passport
        assert passport["layers"]["interfaces"] == 2
        assert passport["interfaces"]["fan_out"] == {"max": 2, "top": [2, 1]}
        assert passport["components"]["unconnected"] == {"total": 1, "max_on_one": 1}
        assert passport["connectors"]["assembly"] == 1

    def test_the_specimen_is_built_and_self_checked(self) -> None:
        assert build_specimen_files(SpecimenPreset(DEFAULT_NAME, stub_passport()))

    def test_stub_ports_beyond_u_are_refused_before_the_derivation(self) -> None:
        # The derivation's stated conflicts (`shape._stated_refusals`): the port total is
        # unstated, the short top decides it, and its stub port needs an unconnected one.
        passport = stub_passport(unconnected=0)
        del passport["components"]["ports"]
        refusals = check_feasibility(passport)
        # With U = 0 all three ports are reached, which the one connector cannot join either.
        assert [(r.leaf, r.refusal_class) for r in refusals] == [
            (FAN_OUT_TOP, "fan-out"),
            (ASSEMBLY, "connectors"),
        ]
        assert "1 on stubs" in refusals[0].sentence
        passport = stub_passport(unconnected=1)
        del passport["components"]["ports"]
        assert check_feasibility(passport) == []

    def test_build_connectors_keeps_the_stub_fan_outs_out_of_the_stars(self) -> None:
        # Fan-outs [2, 1]: the first is connectable, the second a stub's (`connectable=1`).
        interfaces: tuple[AnyInterface, ...] = (
            *named_interfaces([2]),
            ExternalInterface("If2", "/External/If2", "X"),
        )
        plan = build_connectors(
            spec(1, 1, 1),
            [2, 1],
            [2, 1],
            interfaces,
            names(2),
            ("Conn1",),
            connectable=1,
        )
        stub_ports = [p for ports in plan.ports for p in ports if p.interface == "If2"]
        assert len(stub_ports) == 1
        assert all(c.kind == "sender_receiver" for c in plan.connectors)


# --- the closed classes with A unstated, the exact tier, the post-check ------------------------


class TestWithTheConnectorCountUnstated:
    def test_one_reached_port_is_refused_at_the_unconnected_total(self) -> None:
        refusals = connector_refusals(spec(1, 1), [1, 1], [2])
        assert [(r.leaf, r.refusal_class, r.sentence) for r in refusals] == [
            (TOTAL, "unconnected", "leaves one port reached; a connector joins two")
        ]

    def test_a_search_shortfall_names_the_links(self) -> None:
        refusals = connector_refusals(spec(links=0), [5, 1], [6])
        assert [(r.leaf, r.refusal_class) for r in refusals] == [(LINKS, "links")]
        assert spec(links=0).connector_leaf == LINKS


#: The configuration only the exact packing meets (found by disabling it over every
#: configuration of up to ten ports and six components: of 17 million, two need it, this one
#: and the same with the fan-outs (6, 3, 1)).
EXACT_ONLY = ((4, 3, 3, 0, 0, 0), (7, 3), spec(2, 2, assembly=6, links=2))


class TestTheExactPacking:
    def test_a_configuration_only_the_exact_packing_meets_is_built(self) -> None:
        counts, fan_outs, leaves = EXACT_ONLY
        assert refused(counts, fan_outs, leaves) == []
        assert_built(counts, fan_outs, leaves)

    def test_the_greedy_packings_alone_refuse_it(self) -> None:
        counts, fan_outs, leaves = EXACT_ONLY
        refusals = connector_refusals(leaves, counts, fan_outs, Search(PACK_BUDGET, 0))
        assert [(r.leaf, r.refusal_class) for r in refusals] == [(ASSEMBLY, "links")]

    def test_an_exhausted_search_is_a_refusal_not_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(connectors_module, "EXACT_STEPS", 1)
        counts, fan_outs, leaves = EXACT_ONLY
        refusals = connector_refusals(leaves, counts, fan_outs, Search(1, 1))
        assert [(r.leaf, r.refusal_class) for r in refusals] == [(ASSEMBLY, "links")]
        connectors_module._solve_cached.cache_clear()


class TestThePostCheck:
    def test_a_construction_missing_a_connector_is_a_generation_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original = connectors_module._edges

        def one_short(*arguments: Any) -> list[list[int]]:
            targets = original(*arguments)
            for row in targets:
                if row:
                    row.pop()
                    break
            return targets

        monkeypatch.setattr(connectors_module, "_edges", one_short)
        with pytest.raises(GenerationError):
            build([5, 3, 2, 2], [5, 4, 3], spec(2, 2, assembly=5, links=3))

    def test_a_plan_that_misses_a_leaf_is_a_generation_error(self) -> None:
        plan = build([2, 2], [2, 2], spec(assembly=2, links=1))
        with pytest.raises(GenerationError, match="connector leaves"):
            connectors_module._verify(
                plan,
                spec(assembly=3, links=1),
                [2, 2],
                [2, 2],
                named_interfaces([2, 2]),
                names(2),
                (0, 0),
                (0, 0),
            )


class TestOpenPlacements:
    """The placements of the unconnected ports the shape walks, and the construction on a fixed one.

    The fixed placement is of the ports no connector reaches (unconnected and delegation-only).
    """

    def test_the_placements_are_those_the_search_walks(self) -> None:
        leaves = spec(2, 1, assembly=5, links=2)
        counts = (2, 2, 2, 2, 2, 2)
        placements = list(unconnected_placements(leaves, counts))
        assert placements and len(set(placements)) == len(placements)
        for units in placements:
            assert sum(units) == 2 and max(units) == 1
        # The search with no delegation lands on one of them.
        landed = configuration_solution(leaves, counts, (4, 4, 4))
        assert landed is not None and landed.unconnected in placements

    def test_a_fixed_open_placement_is_built_with_its_open_ports_and_centres(self) -> None:
        # Two of the 12 ports unconnected and two delegation-only on the last component: R_c = 8.
        leaves = spec(2, 1, assembly=4, links=1)
        counts = (2, 2, 2, 2, 2, 2)
        units = (1, 1, 0, 0, 0, 2)
        solution = solve_open(leaves, counts, units, (4, 4, 4))
        assert solution is not None
        plan = build_connectors(
            leaves,
            counts,
            (4, 4, 4),
            named_interfaces((4, 4, 4)),
            names(6),
            tuple(f"Conn{index}" for index in range(1, 5)),
            unconnected=(1, 1, 0, 0, 0, 0),
            solution=solution,
        )
        assert [len(open_ports) for open_ports in plan.open_ports] == list(units)
        # The star centres: one provide port per star, R_c - A = 4.
        assert len(plan.centres) == 4
        ports = {(a, p.name): p for a, component in enumerate(plan.ports) for p in component}
        sources = {(c.source.swc, c.source.port) for c in plan.connectors}
        for a, port in plan.centres:
            assert ports[(a, port)].direction == "provide"
            assert (names(6)[a], port) in sources

    @pytest.mark.parametrize(
        "unconnected",
        [(2, 0, 0, 0, 0, 0), (1, 0, 0, 0, 0, 0), (1, 0, 1, 0, 0, 0), None],
        ids=["above-u1", "below-u", "beyond-the-open-ports", "missing"],
    )
    def test_a_wrong_unconnected_placement_under_delegations_is_a_generation_error(
        self, unconnected: tuple[int, ...] | None
    ) -> None:
        # The same open placement: U = 2, u1 = 1 must hold on the unconnected part, even though
        # the open ports (4) exceed U by the two delegation-only ones.
        leaves = spec(2, 1, assembly=4, links=1)
        solution = solve_open(leaves, (2, 2, 2, 2, 2, 2), (1, 1, 0, 0, 0, 2), (4, 4, 4))
        assert solution is not None
        with pytest.raises(GenerationError, match="stated connector leaves"):
            build_connectors(
                leaves,
                (2, 2, 2, 2, 2, 2),
                (4, 4, 4),
                named_interfaces((4, 4, 4)),
                names(6),
                tuple(f"Conn{index}" for index in range(1, 5)),
                unconnected=unconnected,
                solution=solution,
            )

    def test_a_placement_the_stars_cannot_take_is_refused(self) -> None:
        # Every reached port on one component: no connector joins two components.
        leaves = spec(0, 0, assembly=1)
        assert solve_open(leaves, (2, 2), (0, 2), (4,)) is None


# --- a solution from a layout -------------------------------------------------------------------

#: Seed 501's configuration: counts (12, 5, 2), one hub; U = 9, u1 = 7; R = 10, A = 3, L = 0; the
#: SR + CS fan-outs.
LAYOUT_COUNTS = (12, 5, 2)
LAYOUT_FAN_OUTS = (5, 3, 3, 3, 2, 1, 1, 1)
LAYOUT_SPEC = ConnectorSpec(9, 7, 3, None, 0)
#: A hand-made layout of it: the hub centres a star of two connectors on the first interface (to
#: both ordinary components, HO = 2); the five-port component centres one of one connector on the
#: second (to the hub, OH = 1). R_c = 5, so the open ports are 10, 3 and 1, the unconnected 7, 1
#: and 1 among them (the rest delegation-only).
LAYOUT_OPEN = (10, 3, 1)
LAYOUT_UNCONNECTED = (7, 1, 1)
LAYOUT_PIECES = ((0, 0, 2), (1, 1, 1))


def layout_solution(
    pieces: Sequence[tuple[int, int, int]] = LAYOUT_PIECES,
    providers: Sequence[int] = (1, 1, 0),
    extras: Sequence[int] = (1, 0, 0),
) -> Any:
    return star_solution(
        LAYOUT_COUNTS,
        LAYOUT_FAN_OUTS,
        LAYOUT_OPEN,
        providers,
        extras,
        pieces,
        ordinary=0,
        to_hubs=1,
        from_hubs=2,
        hub_to_hub=0,
    )


class TestStarSolution:
    """A solution the planner decided is laid as it is, with no search."""

    def test_a_layout_of_501_is_laid_with_no_search_and_passes_the_post_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_search(*args: object, **kwargs: object) -> None:
            raise AssertionError("a given solution is searched")

        monkeypatch.setattr(connectors_module, "_solve", no_search)
        monkeypatch.setattr(connectors_module, "solve_open", no_search)
        interfaces = named_interfaces(LAYOUT_FAN_OUTS)
        component_names = names(3)
        plan = build_connectors(
            LAYOUT_SPEC,
            LAYOUT_COUNTS,
            LAYOUT_FAN_OUTS,
            interfaces,
            component_names,
            ("Conn1", "Conn2", "Conn3"),
            solution=layout_solution(),
            unconnected=LAYOUT_UNCONNECTED,
        )
        assert [len(open_ports) for open_ports in plan.open_ports] == list(LAYOUT_OPEN)
        assert [a for a, _ in plan.centres] == [0, 1]
        ends = sorted(
            (c.source.swc, c.target.swc, interfaces_of(plan)[(c.source.swc, c.source.port)]) for c in plan.connectors
        )
        assert ends == [
            ("Swc0001", "Swc0002", "If0001"),
            ("Swc0001", "Swc0003", "If0001"),
            ("Swc0002", "Swc0001", "If0002"),
        ]
        connectors_module._verify(
            plan,
            LAYOUT_SPEC,
            LAYOUT_COUNTS,
            LAYOUT_FAN_OUTS,
            interfaces,
            component_names,
            LAYOUT_OPEN,
            LAYOUT_UNCONNECTED,
        )

    def test_pieces_that_overfill_an_interface_are_a_generation_error(self) -> None:
        # The hub's star of two connectors on the fifth interface takes three of its two ports.
        with pytest.raises(GenerationError, match="overfill"):
            layout_solution(pieces=((0, 4, 2), (1, 1, 1)))

    def test_pieces_other_than_the_providers_and_extras_are_a_generation_error(self) -> None:
        with pytest.raises(GenerationError, match="differ from its providers and extras"):
            layout_solution(extras=(0, 0, 0))
        with pytest.raises(GenerationError, match="differ from its providers and extras"):
            layout_solution(providers=(2, 1, 0))

    def test_blocks_other_than_the_stars_give_are_a_generation_error(self) -> None:
        with pytest.raises(GenerationError, match="blocks differ from its stars"):
            star_solution(
                LAYOUT_COUNTS,
                LAYOUT_FAN_OUTS,
                LAYOUT_OPEN,
                (1, 1, 0),
                (1, 0, 0),
                LAYOUT_PIECES,
                ordinary=1,
                to_hubs=0,
                from_hubs=2,
                hub_to_hub=0,
            )

    def test_a_star_on_an_interface_stub_is_a_generation_error(self) -> None:
        # Only the first interface holds stars; the second one's star lies on a stub.
        with pytest.raises(GenerationError, match="on an interface stub"):
            build_connectors(
                LAYOUT_SPEC,
                LAYOUT_COUNTS,
                LAYOUT_FAN_OUTS,
                named_interfaces(LAYOUT_FAN_OUTS),
                names(3),
                ("Conn1", "Conn2", "Conn3"),
                connectable=1,
                solution=layout_solution(),
                unconnected=LAYOUT_UNCONNECTED,
            )


def interfaces_of(plan: ConnectorPlan) -> dict[tuple[str | None, str], str]:
    """Each (component, port) of a plan's components named as `names` names them, its interface."""
    return {
        (component, port.name): port.interface
        for component, ports in zip(names(len(plan.ports)), plan.ports, strict=True)
        for port in ports
    }
