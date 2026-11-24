"""Interfaces, the fan-out and the types.

Each check has a refusal and an acceptance vector; the built interfaces are held to the definitions
of SWCraft's pass (restated in `swcraft_specimens/passport.py`) by recomputing the fan-out from the
ports the assignment produces, and an exhaustive oracle over every fan-out list of a few ports
decides independently which stated fan-out leaves are feasible.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from swcraft_specimens.errors import GenerationError
from swcraft_specimens.model import BaseType, ExternalInterface, Interface, ValueType
from swcraft_specimens.passport import Refusal, compute_passport, read_passport, validate_passport
from swcraft_specimens.specimen import interfaces as interfaces_module
from swcraft_specimens.specimen.components import place_ports, port_spec
from swcraft_specimens.specimen.feasibility import check_feasibility
from swcraft_specimens.specimen.identifiers import identifier_counts, identifier_names
from swcraft_specimens.specimen.interfaces import (
    InterfacePlan,
    build_interfaces,
    interface_refusals,
    odd_fan_out_room,
    paired_split,
    split_fan_outs,
)
from swcraft_specimens.specimen.leaves import LEAVES, stated_leaves
from swcraft_specimens.specimen.shape import Shape, specimen_shape
from swcraft_specimens.specimen.vocabulary import MET
from tests.oracle.specimen_helpers import (
    descending_partitions,
    load_specimen,
    neutral_passport,
    shaped_witness,
    witness,
)

FAN_OUT_MAX = "interfaces.fan_out.max"
FAN_OUT_TOP = "interfaces.fan_out.top"


def stated(
    ports: int,
    sr: int,
    cs: int,
    fan_max: int,
    top: list[int] | None = None,
    unconnected: int = 0,
    *,
    n: int = 2,
    interfaces: int | None = None,
    implementation: int | None = None,
    base: int | None = None,
) -> dict[str, Any]:
    """Stated leaves: n components (no hub) holding `ports`, and the interface leaves."""
    leaves: dict[str, Any] = {
        "layers.swc": n,
        "components.hubs": [],
        "components.ports.total": ports,
        "components.unconnected.total": unconnected,
        "layers.interfaces": sr + cs if interfaces is None else interfaces,
        "interfaces.sender_receiver": sr,
        "interfaces.client_server": cs,
        "interfaces.fan_out.max": fan_max,
        "layers.implementation_types": (1 if sr + cs else 0) if implementation is None else implementation,
        "layers.base_types": (1 if sr + cs else 0) if base is None else base,
    }
    if top is not None:
        leaves[FAN_OUT_TOP] = top
    return leaves


def refused(leaves: dict[str, Any]) -> list[tuple[str, str]]:
    """The interface refusals, and the fan-out refusal of the fewest interfaces beyond the head.

    The fan-out refusal is for the stated port total, when the order is not refused.
    """
    refusals = interface_refusals(leaves)
    if not {r.leaf for r in refusals} & {FAN_OUT_MAX, FAN_OUT_TOP}:
        chosen = split_fan_outs(leaves, int(leaves["components.ports.total"]))
        if isinstance(chosen, Refusal):
            refusals.append(chosen)
    return [(r.leaf, r.refusal_class) for r in refusals]


#: The hardware leaves the identifier plan reads, at their plainest.
PLAIN_HARDWARE = {
    "hardware.cores": 1,
    "hardware.partitions": 0,
    "hardware.memories": 0,
    "hardware.rated_partitions": 0,
}


def plan_of(leaves: dict[str, Any]) -> InterfacePlan:
    """The interface plan of feasible leaves.

    The placed ports' fan-outs (the fewest interfaces beyond the head), named by the identifier
    plan.
    """
    counts = place_ports(port_spec(leaves))
    fan_outs = split_fan_outs(leaves, sum(counts))
    assert not isinstance(fan_outs, Refusal)
    full = {**PLAIN_HARDWARE, **leaves}
    names = identifier_names(full, identifier_counts(full, 0))
    return build_interfaces(leaves, fan_outs, names)


def fan_outs_of(plan: InterfacePlan) -> Counter[str]:
    """Per named interface, the ports it holds (the connectors assign them to components)."""
    return Counter({interface.name: fan_out for interface, fan_out in zip(plan.interfaces, plan.fan_outs)})


def assert_met(leaves: dict[str, Any]) -> InterfacePlan:
    assert refused(leaves) == []
    plan = plan_of(leaves)
    fan_out = fan_outs_of(plan)
    values = sorted(fan_out.values(), reverse=True)
    assert sum(plan.fan_outs) == leaves["components.ports.total"]
    assert list(plan.fan_outs) == sorted(plan.fan_outs, reverse=True)
    assert set(fan_out) <= {interface.name for interface in plan.interfaces}
    assert len(plan.interfaces) == leaves["layers.interfaces"]
    kinds = Counter(interface.kind for interface in plan.interfaces)
    assert kinds["sender_receiver"] == leaves["interfaces.sender_receiver"]
    assert kinds["client_server"] == leaves["interfaces.client_server"]
    assert (values[0] if values else 0) == leaves["interfaces.fan_out.max"]
    if FAN_OUT_TOP in leaves:
        assert values[:8] == leaves[FAN_OUT_TOP]
    value_types = [t for t in plan.data_types if isinstance(t, ValueType)]
    base_types = [t for t in plan.data_types if isinstance(t, BaseType)]
    assert len(value_types) == leaves["layers.implementation_types"]
    assert len(base_types) == leaves["layers.base_types"]
    return plan


# --- SR, CS, the fan-out max and top are met -----------------------------------------------------


class TestMet:
    def test_sr_cs_max_and_top_are_met(self) -> None:
        plan = assert_met(stated(10, 3, 1, 4, [4, 3, 2, 1], unconnected=1, n=4))
        assert sorted(fan_outs_of(plan).values(), reverse=True) == [4, 3, 2, 1]

    def test_the_interfaces_beyond_a_full_top_take_the_rest(self) -> None:
        # top sums to 24 of 30 ports: the six left go to three more interfaces of at most 2.
        plan = assert_met(stated(30, 11, 0, 5, [5, 4, 3, 3, 3, 2, 2, 2], n=10))
        assert sorted(fan_outs_of(plan).values(), reverse=True) == [5, 4, 3, 3, 3, 2, 2, 2, 2, 2, 2]

    def test_an_unstated_top_is_derived_from_the_max(self) -> None:
        plan = assert_met(stated(9, 4, 0, 3, n=3))
        assert sorted(fan_outs_of(plan).values(), reverse=True) == [3, 3, 3]

    def test_the_sender_receiver_interfaces_come_first(self) -> None:
        plan = assert_met(stated(6, 1, 2, 2, [2, 2, 2], n=3))
        assert [i.kind for i in plan.interfaces] == [
            "sender_receiver",
            "client_server",
            "client_server",
        ]
        sr, cs = plan.interfaces[0], plan.interfaces[1]
        assert isinstance(sr, Interface) and isinstance(cs, Interface)
        assert len(sr.data_elements) == 1 and not sr.operations
        assert len(cs.operations) == 1 and not cs.data_elements
        assert [a.direction for a in cs.operations[0].arguments] == ["in"]

    def test_no_port_and_no_interface(self) -> None:
        plan = assert_met(stated(0, 0, 0, 0, [], n=2))
        assert plan.interfaces == () and plan.data_types == ()

    def test_no_port_beside_declared_interfaces(self) -> None:
        plan = assert_met(stated(0, 2, 1, 0, n=2))
        assert len(plan.interfaces) == 3 and not fan_outs_of(plan)

    def test_a_large_scale_configuration_is_met_quickly(self) -> None:
        leaves = stated(3000, 600, 50, 597, unconnected=285, n=76)
        leaves["components.hubs"] = [428, 137, 100]
        plan = assert_met(leaves)
        assert max(fan_outs_of(plan).values()) == 597


class TestUnnamedInterfaces:
    def test_unnamed_interfaces_are_allowed(self) -> None:
        plan = assert_met(stated(4, 5, 3, 2, [2, 2], n=2))
        assert len(plan.interfaces) == 8
        assert len(fan_outs_of(plan)) == 2


# --- the fan-out refusals ------------------------------------------------------------------------


class TestFanOutOnes:
    def test_fan_out_one_interfaces_beyond_u_are_refused(self) -> None:
        assert refused(stated(4, 3, 0, 2, [2, 1, 1], unconnected=1)) == [(FAN_OUT_TOP, "fan-out")]

    def test_fan_out_one_interfaces_within_u_are_accepted(self) -> None:
        assert_met(stated(4, 3, 0, 2, [2, 1, 1], unconnected=2))

    def test_a_forced_fan_out_one_beyond_an_unstated_top_is_refused(self) -> None:
        # Five ports at most 2 per interface: 2 + 2 + 1, one interface of fan-out 1.
        assert refused(stated(5, 3, 0, 2, unconnected=0, n=3)) == [(FAN_OUT_MAX, "fan-out")]
        assert_met(stated(5, 3, 0, 2, unconnected=1, n=3))

    def test_a_forced_fan_out_one_beyond_a_full_top_is_refused(self) -> None:
        # top sums to 16 of 17: the one port left is an interface of fan-out 1.
        assert refused(stated(17, 9, 0, 2, [2] * 8, unconnected=0, n=9)) == [(FAN_OUT_TOP, "fan-out")]
        assert_met(stated(17, 9, 0, 2, [2] * 8, unconnected=1, n=9))


class TestFanOutSum:
    def test_a_short_top_summing_below_p_is_refused(self) -> None:
        assert refused(stated(6, 2, 0, 3, [3, 2])) == [(FAN_OUT_TOP, "fan-out")]

    def test_a_top_summing_above_p_is_refused(self) -> None:
        assert refused(stated(4, 2, 0, 3, [3, 2])) == [(FAN_OUT_TOP, "fan-out")]
        assert refused(stated(2, 2, 0, 3)) == [(FAN_OUT_MAX, "fan-out")]

    def test_a_short_top_summing_to_p_is_accepted(self) -> None:
        assert_met(stated(5, 2, 0, 3, [3, 2]))

    def test_ports_with_no_interface_are_refused(self) -> None:
        assert refused(stated(4, 2, 0, 0)) == [(FAN_OUT_MAX, "fan-out")]

    def test_a_max_with_no_port_is_refused(self) -> None:
        assert refused(stated(0, 2, 0, 1)) == [(FAN_OUT_MAX, "fan-out")]

    def test_a_named_interface_without_a_port_is_refused(self) -> None:
        assert refused(stated(2, 2, 0, 2, [2, 0])) == [(FAN_OUT_TOP, "fan-out")]

    def test_a_zero_fan_out_with_no_port_is_refused(self) -> None:
        # The zero-port path once returned before this check.
        leaves = stated(0, 1, 0, 0, [0])
        assert [(r.leaf, r.refusal_class) for r in interface_refusals(leaves)] == [(FAN_OUT_TOP, "fan-out")]
        chosen = split_fan_outs(leaves, 0)
        assert isinstance(chosen, Refusal) and chosen.leaf == FAN_OUT_TOP


class TestNamedWithinDeclared:
    def test_more_named_interfaces_than_declared_are_refused(self) -> None:
        assert refused(stated(4, 3, 0, 1, unconnected=4)) == [(FAN_OUT_MAX, "fan-out")]
        assert refused(stated(4, 1, 0, 2, [2, 2])) == [(FAN_OUT_TOP, "fan-out")]

    def test_a_head_naming_more_than_declared_is_refused_whatever_the_ports(self) -> None:
        # The stated leaves alone, before any port total.
        for leaves, leaf in (
            (stated(4, 1, 0, 2, [2, 2]), FAN_OUT_TOP),
            (stated(0, 0, 0, 1), FAN_OUT_MAX),
            (stated(3, 0, 0, 3, [3]), FAN_OUT_TOP),
        ):
            assert [(r.leaf, r.refusal_class) for r in interface_refusals(leaves)] == [(leaf, "fan-out")]

    def test_no_interface_and_no_fan_out_is_accepted(self) -> None:
        assert interface_refusals(stated(0, 0, 0, 0)) == []
        assert interface_refusals(stated(0, 0, 0, 0, [])) == []

    def test_named_interfaces_up_to_the_declared_are_accepted(self) -> None:
        assert_met(stated(4, 3, 1, 1, unconnected=4))
        assert_met(stated(4, 1, 1, 2, [2, 2]))


class TestFanOutOrder:
    def test_a_top_not_descending_is_refused(self) -> None:
        assert refused(stated(5, 2, 0, 2, [2, 3])) == [(FAN_OUT_TOP, "order")]

    def test_a_max_other_than_the_first_of_top_is_refused(self) -> None:
        assert refused(stated(5, 2, 0, 4, [3, 2])) == [(FAN_OUT_MAX, "order")]
        assert refused(stated(0, 2, 0, 1, [])) == [(FAN_OUT_MAX, "order")]

    def test_a_max_equal_to_the_first_of_top_is_accepted(self) -> None:
        assert_met(stated(5, 2, 0, 3, [3, 2]))


# --- layers.interfaces against SR + CS -----------------------------------------------------------


class TestInterfaceLayer:
    def test_above_sr_plus_cs_is_met_by_stubs(self) -> None:
        # The interfaces beyond SR + CS are external interface stubs.
        assert refused(stated(4, 2, 0, 2, [2, 2], interfaces=3)) == []
        plan = assert_met(stated(4, 2, 0, 2, [2, 2], interfaces=3))
        assert [i.kind for i in plan.interfaces] == [
            "sender_receiver",
            "sender_receiver",
            "external_interface",
        ]

    def test_below_sr_plus_cs_is_layer_total(self) -> None:
        assert refused(stated(4, 2, 0, 2, [2, 2], interfaces=1)) == [("layers.interfaces", "layer-total")]

    def test_equal_to_sr_plus_cs_is_met(self) -> None:
        assert_met(stated(4, 2, 0, 2, [2, 2], interfaces=2))


# --- the external interface stubs ----------------------------------------------------------------


def stubs_of(plan: InterfacePlan) -> list[ExternalInterface]:
    return [i for i in plan.interfaces if isinstance(i, ExternalInterface)]


class TestStubs:
    """X = layers.interfaces - SR - CS stubs after the client-server interfaces.

    Each has `external_dest: CLIENT-SERVER-INTERFACE` and `external_ref: /External/Interfaces/<name>`;
    the stubs take the X smallest fan-outs; the ports on stubs and on fan-out-1 interfaces number
    at most U + (R - R_c).
    """

    def test_x_stubs_follow_the_client_server_interfaces(self) -> None:
        plan = assert_met(stated(6, 1, 2, 2, [2, 2, 2], n=3, interfaces=5))
        assert [i.kind for i in plan.interfaces] == [
            "sender_receiver",
            "client_server",
            "client_server",
            "external_interface",
            "external_interface",
        ]
        stubs = stubs_of(plan)
        assert len(stubs) == 2
        for stub in stubs:
            assert stub.external_dest == "CLIENT-SERVER-INTERFACE"
            assert stub.external_ref == f"/External/Interfaces/{stub.name}"
        # Three named interfaces of fan-out 2, all SR + CS: the stubs are named by no port.
        assert set(fan_outs_of(plan)) == {i.name for i in plan.interfaces[:3]}

    def test_the_stubs_take_the_smallest_fan_outs(self) -> None:
        # Two SR interfaces and two stubs: the stubs hold the 2 and the 1 of [4, 3, 2, 1].
        plan = assert_met(stated(10, 2, 0, 4, [4, 3, 2, 1], unconnected=3, n=4, interfaces=4))
        fan_out = fan_outs_of(plan)
        assert [fan_out[stub.name] for stub in stubs_of(plan)] == [2, 1]
        assert [fan_out[i.name] for i in plan.interfaces[:2]] == [4, 3]

    def test_stub_and_fan_out_one_ports_beyond_u_are_refused_at_top(self) -> None:
        # The stubs' 2 + 1 ports and no fan-out-1 SR interface: 3 above U = 2.
        assert refused(stated(10, 2, 0, 4, [4, 3, 2, 1], unconnected=2, n=4, interfaces=4)) == [
            (FAN_OUT_TOP, "fan-out")
        ]
        # A stub of fan-out 1 is counted once: [3, 1, 1] with one stub needs U >= 2.
        assert refused(stated(5, 2, 0, 3, [3, 1, 1], unconnected=1, n=3, interfaces=3)) == [(FAN_OUT_TOP, "fan-out")]
        assert_met(stated(5, 2, 0, 3, [3, 1, 1], unconnected=2, n=3, interfaces=3))

    def test_stub_ports_beyond_u_are_admitted_up_to_u_plus_the_delegations(self) -> None:
        # The 3 stub ports need U + (R - R_c) >= 3, R - R_c at most D.
        leaves = stated(10, 2, 0, 4, [4, 3, 2, 1], unconnected=2, n=4, interfaces=4)
        leaves["connectors.delegation"] = 1
        assert split_fan_outs(leaves, 10) == (4, 3, 2, 1)
        leaves["components.unconnected.total"] = 1
        refusal = split_fan_outs(leaves, 10)
        assert isinstance(refusal, Refusal)
        assert (refusal.leaf, refusal.refusal_class) == (FAN_OUT_TOP, "fan-out")
        assert refusal.sentence == (
            "needs 3 ports on external interface stubs and fan-out-1 interfaces (3 on stubs), "
            "each a port no connector reaches; unconnected.total states 1, and at most 1 more "
            "are reached by a delegation alone"
        )

    def test_stub_ports_beyond_u_are_refused_at_the_layer_with_top_unstated(self) -> None:
        # Four ports at most 2 per interface, one SR interface: the second 2 is a stub's.
        assert refused(stated(4, 1, 0, 2, unconnected=1, n=2, interfaces=3)) == [("layers.interfaces", "fan-out")]
        plan = assert_met(stated(4, 1, 0, 2, unconnected=2, n=2, interfaces=3))
        fan_out = fan_outs_of(plan)
        assert sum(fan_out[stub.name] for stub in stubs_of(plan)) == 2

    def test_the_connectable_interfaces_hold_the_most_they_can(self) -> None:
        # The tail beyond a full top: 6 ports at most 2 each, one SR + CS slot left beyond the
        # head; the slot takes 2, the stubs the other 4 (the fewest stub ports any split has).
        leaves = stated(22, 9, 0, 2, [2] * 8, unconnected=4, n=11, interfaces=12)
        plan = assert_met(leaves)
        fan_out = fan_outs_of(plan)
        assert sum(fan_out.get(stub.name, 0) for stub in stubs_of(plan)) == 4
        assert refused({**leaves, "components.unconnected.total": 3}) == [(FAN_OUT_TOP, "fan-out")]

    def test_more_named_interfaces_than_the_layer_are_refused(self) -> None:
        assert refused(stated(4, 1, 0, 1, unconnected=4, interfaces=3)) == [(FAN_OUT_MAX, "fan-out")]
        assert_met(stated(4, 1, 0, 1, unconnected=4, interfaces=4))

    def test_stubs_need_no_value_type(self) -> None:
        plan = assert_met(stated(0, 0, 0, 0, interfaces=2, implementation=0, base=0))
        assert len(stubs_of(plan)) == 2 and plan.data_types == ()


# --- the types -----------------------------------------------------------------------------------


class TestTypes:
    def test_no_implementation_type_beside_an_interface_is_refused(self) -> None:
        assert refused(stated(4, 2, 0, 2, [2, 2], implementation=0, base=0)) == [
            ("layers.implementation_types", "layer-total")
        ]

    def test_an_implementation_type_without_a_base_type_is_refused(self) -> None:
        assert refused(stated(4, 2, 0, 2, [2, 2], implementation=1, base=0)) == [("layers.base_types", "layer-total")]

    def test_no_type_and_no_interface_is_accepted(self) -> None:
        assert_met(stated(0, 0, 0, 0, implementation=0, base=0))

    def test_base_types_without_an_implementation_type_are_accepted(self) -> None:
        plan = assert_met(stated(0, 0, 0, 0, implementation=0, base=2))
        assert [type(t) for t in plan.data_types] == [BaseType, BaseType]

    def test_the_type_counts_are_met_round_robin(self) -> None:
        plan = assert_met(stated(6, 2, 2, 2, [2, 2, 2], n=3, implementation=3, base=5))
        value_types = [t for t in plan.data_types if isinstance(t, ValueType)]
        base_names = [t.name for t in plan.data_types if isinstance(t, BaseType)]
        # Base types go round-robin to value types; the two left over stay unreferenced.
        assert [v.base for v in value_types] == base_names[:3]
        declared = [i for i in plan.interfaces if isinstance(i, Interface)]
        typed = [e.type for i in declared for e in i.data_elements] + [
            a.type for i in declared for o in i.operations for a in o.arguments
        ]
        # Value types go round-robin to the data elements and the arguments, in interface order.
        assert typed == [value_types[0].name, value_types[1].name] + [
            value_types[2].name,
            value_types[0].name,
        ]

    def test_more_value_types_than_typed_members_are_accepted(self) -> None:
        assert_met(stated(2, 1, 0, 2, [2], implementation=4, base=1))


# --- the plan is deterministic -------------------------------------------------------------------


class TestAssignment:
    """Which component holds which port is the connectors' (`test_specimen_connectors.py`)."""

    def test_the_assignment_is_deterministic(self) -> None:
        leaves = stated(30, 11, 0, 5, [5, 4, 3, 3, 3, 2, 2, 2], n=10)
        assert plan_of(leaves) == plan_of(leaves)


# --- the fan-out against an exhaustive oracle ----------------------------------------------------


#: The declared SR + CS interfaces and the stubs beside them the oracle walks.
DECLARED_AND_STUBS = [
    *((declared, 0) for declared in (0, 1, 2, 3, 4, 8, 9, 10)),
    (0, 2),
    (1, 1),
    (1, 3),
    (2, 2),
    (3, 6),
    (8, 2),
]


def forced(fan_outs: Sequence[int], declared: int) -> int:
    """The unconnected ports a descending fan-out list forces.

    Its stubs' (beyond the first `declared`) and its fan-out-1 SR + CS interfaces'.
    """
    return sum(fan_outs[declared:]) + list(fan_outs[:declared]).count(1)


@pytest.mark.parametrize("ports", list(range(11)))
def test_the_fan_out_is_feasible_exactly_when_some_fan_out_list_has_it(ports: int) -> None:
    """Every stated (max, top) is accepted iff some fan-out list has it within I, U and D."""
    candidates = {(p[0] if p else 0, p[:8]) for p in descending_partitions(ports)}
    for declared, stubs in DECLARED_AND_STUBS:
        layer = declared + stubs
        for unconnected, delegations in itertools.product((0, 1, 2, 3, 10), (0, 1, 3)):
            # The stubs take the smallest fan-outs, so a list's stub ports are those beyond the first
            # `declared`; they and the fan-out-1 SR + CS ones are reached by no connector: U, and at
            # most min(D, R) more reached by a delegation alone.
            room = unconnected + min(delegations, max(0, ports - unconnected))
            witnesses = {
                (p[0] if p else 0, p[:8])
                for p in descending_partitions(ports)
                if len(p) <= layer and forced(p, declared) <= room
            }
            for fan_max, top in sorted(candidates):
                for stated_top in (list(top), None):
                    leaves = stated(ports, declared, 0, fan_max, stated_top, unconnected, interfaces=layer)
                    leaves["connectors.delegation"] = delegations
                    feasible = (
                        (fan_max, top) in witnesses
                        if stated_top is not None
                        else any(w[0] == fan_max for w in witnesses)
                    )
                    assert (refused(leaves) == []) is feasible, (leaves, feasible)
                    if feasible:
                        values = split_fan_outs(leaves, ports)
                        assert not isinstance(values, Refusal)
                        assert sum(values) == ports and len(values) <= layer
                        assert forced(values, declared) <= room


def test_the_fewest_named_interfaces_are_chosen() -> None:
    values = split_fan_outs(stated(20, 20, 0, 6, unconnected=0, n=4), 20)
    assert not isinstance(values, Refusal)
    assert len(values) == math.ceil(20 / 6)


# --- the feasibility check and the builder frame -------------------------------------------------


class TestFeasibilityFrame:
    def test_the_check_reports_the_interface_refusals_at_their_leaves(self) -> None:
        passport = neutral_passport()
        passport["interfaces"]["fan_out"] = {"max": 2, "top": [1, 1]}
        refusals = check_feasibility(passport)
        assert [(r.leaf, r.refusal_class) for r in refusals] == [(FAN_OUT_MAX, "order")]

    def test_the_layer_and_type_refusals_reach_the_check(self) -> None:
        passport = neutral_passport()
        passport["layers"]["interfaces"] = 2
        passport["layers"]["base_types"] = 0
        # No base type has no name: its identifier lengths are 0.
        passport["identifiers"]["base_types"] = {"max_length": 0, "mean_length": 0}
        refusals = check_feasibility(passport)
        assert [(r.leaf, r.refusal_class) for r in refusals] == [
            ("layers.base_types", "layer-total"),
        ]

    def test_the_neutral_passport_passes_the_interface_checks(self) -> None:
        assert check_feasibility(neutral_passport()) == []

    def test_the_interface_leaves_are_built(self) -> None:
        interface_leaves = frozenset(
            {
                "layers.interfaces",
                "layers.implementation_types",
                "layers.base_types",
                "interfaces.sender_receiver",
                "interfaces.client_server",
                "interfaces.fan_out.max",
                "interfaces.fan_out.top",
            }
        )
        assert all(LEAVES[leaf].leaf_class == MET for leaf in interface_leaves)
        refusals = check_feasibility(neutral_passport())
        assert not interface_leaves & {r.leaf for r in refusals}


def test_fan_outs_that_miss_the_stated_leaves_are_a_generation_error() -> None:
    leaves = stated(30, 11, 0, 5, [5, 4, 3, 3, 3, 2, 2, 2], n=10)
    plan = plan_of(leaves)
    full = {**PLAIN_HARDWARE, **leaves}
    names = identifier_names(full, identifier_counts(full, 0))
    with pytest.raises(GenerationError, match="fan-out leaves"):
        build_interfaces(leaves, (6, *plan.fan_outs[1:]), names)


# --- the paired tail and the parity bound -------------------------------------------------------

ACCEPTANCE = Path(__file__).parent / "fixtures" / "specimen-acceptance"

#: The specimen fixtures (`swcraft_specimens/presets/specimens/`) whose fan-outs the paired split must not move.
FIXTURES = (
    "minimal",
    "topology",
    "large-scale-topology",
    "shapes",
    "chain-empty",
    "large-scale-shapes",
)

#: The sweep's cases (`test_specimen_fixtures.py`): the witnesses and the shaped witnesses.
WITNESS_CASES = [(seed, 2 + seed % 7) for seed in range(42)] + [(100 + seed, 9 + seed % 8) for seed in range(18)]
SHAPED_WITNESS_CASES = [(500 + seed, 2 + seed % 7) for seed in range(42)] + [
    (600 + seed, 9 + seed % 8) for seed in range(18)
]

#: The SHA-256 of each fixture's fan-outs (`json.dumps` of the list): the paired split replaces the
#: even one only where the even one cannot build, so none of them moves.
EXPECTED_FIXTURE_FAN_OUTS_SHA256 = {
    "minimal": "038966de9f6b9a901b20b4c6ca8b2a46009feebe031babc842d43690c0bc222b",
    "topology": "24f7392dc53359b3762017d0f1dd0b785c9e34afde9cb0a62f90b53e040ae30f",
    "large-scale-topology": "a1659f4dcfa218ea4ac9ee572890eeb4502b2e714378c9266ed862f913a2a871",
    "shapes": "24f7392dc53359b3762017d0f1dd0b785c9e34afde9cb0a62f90b53e040ae30f",
    "chain-empty": "038966de9f6b9a901b20b4c6ca8b2a46009feebe031babc842d43690c0bc222b",
    "large-scale-shapes": "d64a71592a19c7474621b9b536aaaaaa9b665e84082f92a2e116ba5f3e58e8a1",
}
#: The SHA-256 of every witness's and every shaped witness's fan-outs, in case order.
EXPECTED_WITNESS_FAN_OUTS_SHA256 = "cd7a8b7ce16ed20529c7e13c7e0b91cd4ab2cca0ae319e83c6a0933326dce1ec"
EXPECTED_SHAPED_WITNESS_FAN_OUTS_SHA256 = "4b927b1eb3128a310b364787ffd44a7ca31625f43436fd9ab2592eef9aa869ca"


def passport_small() -> dict[str, Any]:
    text = (ACCEPTANCE / "passport-small.json").read_text(encoding="utf-8")
    return stated_leaves(read_passport(text))


def odd(values: Sequence[int]) -> int:
    return sum(1 for value in values if value % 2)


def fan_outs_sha256(values: Sequence[Sequence[int]] | Sequence[int]) -> str:
    text = json.dumps([list(v) if isinstance(v, tuple) else v for v in values])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def witness_leaves(documents: Any) -> dict[str, Any]:
    return stated_leaves(validate_passport(compute_passport(documents)))


def shape_of(documents: Any) -> Shape:
    shape = specimen_shape(witness_leaves(documents))
    assert isinstance(shape, Shape), shape
    return shape


def fixture_leaves() -> dict[str, dict[str, Any]]:
    return {name: stated_leaves(load_specimen(name).passport) for name in FIXTURES}


class TestThePairedSplit:
    def test_190_ports_over_20_tails_have_no_odd_entry(self) -> None:
        paired = paired_split(190, 20, 15)
        assert paired is not None
        assert (sum(paired), len(paired), max(paired), min(paired)) == (190, 20, 10, 8)
        assert odd(paired) == 0
        assert paired == sorted(paired, reverse=True)

    def test_the_even_split_there_has_ten_odd_entries(self) -> None:
        assert odd([10] * 10 + [9] * 10) == 10
        values = split_fan_outs(passport_small(), 314, 20)
        assert not isinstance(values, Refusal)
        assert sorted(values[8:], reverse=True) == paired_split(190, 20, 15)

    def test_an_odd_rest_puts_its_odd_port_on_the_smallest_entry(self) -> None:
        assert paired_split(9, 3, 5) == [4, 3, 2]
        assert paired_split(7, 2, 5) == [4, 3]

    def test_a_pair_above_the_most_gives_no_paired_split(self) -> None:
        # 95 pairs over 13 tails: some take 8 pairs, 16 ports, above the head's last 15.
        assert paired_split(190, 13, 15) is None

    def test_an_empty_entry_gives_no_paired_split(self) -> None:
        assert paired_split(4, 3, 5) is None


class TestTheParityBound:
    def test_passport_small_admits_six_interfaces_of_odd_fan_out(self) -> None:
        # E = 2A - R_c = 0 extras, U = 6, no delegation: 0 + 6 + 0.
        assert odd_fan_out_room(passport_small(), 314) == 6

    def test_no_stated_count_gives_no_bound(self) -> None:
        leaves = passport_small()
        del leaves["connectors.assembly"], leaves["layers.connectors"]
        assert odd_fan_out_room(leaves, 314) is None

    def test_the_even_split_of_passport_small_breaks_the_bound(self) -> None:
        # The parity bound's trigger, through the module: the head [16, 16, 16, 16, 15, 15, 15, 15]
        # has 4 odd fan-outs, the even split of 20 tails 10, against a room of 6.
        leaves = passport_small()
        head = list(interfaces_module._head(leaves))
        slots = interfaces_module._declared(leaves) - len(head)
        even = interfaces_module._tail(314 - sum(head), 20, slots, head[-1])
        assert sorted(even, reverse=True) == [10] * 10 + [9] * 10
        assert odd_fan_out_room(leaves, 314) == 6
        assert odd([*head, *even]) == 14
        assert interfaces_module._parity_excess(leaves, 314, [*head, *even]) is not None

    def test_the_paired_tail_meets_it(self) -> None:
        values = split_fan_outs(passport_small(), 314, 20)
        assert not isinstance(values, Refusal)
        assert odd(values) == 4
        assert sum(values) == 314

    def test_a_tail_no_split_meets_is_refused_at_the_fan_out_leaf(self) -> None:
        # 13 tails: the paired split exceeds 15, the even one has 8 odd entries, 12 in all.
        refusal = split_fan_outs(passport_small(), 314, 13)
        assert refusal == Refusal(
            FAN_OUT_TOP,
            "fan-out",
            "needs 12 interfaces of odd fan-out for 314 ports; an interface of odd fan-out needs "
            "a port no connector reaches or an extra require port of a star, and "
            "components.unconnected.total, connectors.delegation and the 154 connectors leave "
            "at most 6",
        )

    def test_no_fixture_and_no_witness_breaks_it(self) -> None:
        cases = [
            *fixture_leaves().values(),
            *(witness_leaves(witness(seed, n)) for seed, n in WITNESS_CASES),
            *(witness_leaves(shaped_witness(seed, n)) for seed, n in SHAPED_WITNESS_CASES),
        ]
        bounded = 0
        for leaves in cases:
            shape = specimen_shape(leaves)
            assert isinstance(shape, Shape), shape
            room = odd_fan_out_room(leaves, sum(shape.counts))
            if room is not None:
                bounded += 1
                assert odd(shape.fan_outs) <= room, leaves
        assert bounded == len(cases)


class TestNoFanOutMoves:
    def test_every_fixture_keeps_its_fan_outs(self) -> None:
        for name, leaves in fixture_leaves().items():
            shape = specimen_shape(leaves)
            assert isinstance(shape, Shape), (name, shape)
            assert fan_outs_sha256(shape.fan_outs) == EXPECTED_FIXTURE_FAN_OUTS_SHA256[name], name

    def test_every_witness_keeps_its_fan_outs(self) -> None:
        fan_outs = [shape_of(witness(s, n)).fan_outs for s, n in WITNESS_CASES]
        assert fan_outs_sha256(fan_outs) == EXPECTED_WITNESS_FAN_OUTS_SHA256

    def test_every_shaped_witness_keeps_its_fan_outs(self) -> None:
        fan_outs = [shape_of(shaped_witness(s, n)).fan_outs for s, n in SHAPED_WITNESS_CASES]
        assert fan_outs_sha256(fan_outs) == EXPECTED_SHAPED_WITNESS_FAN_OUTS_SHA256
