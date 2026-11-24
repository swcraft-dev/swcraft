"""The architecture metrics, each expected figure worked by hand from a small model in the test.

No figure here was copied from the code's output: the comment beside it is the arithmetic.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT
from scratch_git import git as _git
from single_read_support import armed
from swcraft_tools import change_report as change_report_module
from swcraft_tools import metrics as metrics_module
from swcraft_tools import model_state as model_state_module
from swcraft_tools.change_report import RevisionParseError
from swcraft_tools.cli import main
from swcraft_tools.metrics import (
    CONNECTED_UNMEASURED,
    DELEGATED_TO_NO_ONE,
    HEAD_DOES_NOT_PARSE,
    INCOMPLETE,
    LOOK_AT_KEYS,
    LOOK_ERRORS,
    LOOK_HUBS,
    LOOK_INTRODUCED,
    LOOK_LOW_COHESION,
    LOOK_NEW_HUBS,
    LOOK_NO_LINK,
    LOOK_ROSE,
    LOOK_UNSCHEDULED,
    NO_PORT,
    ArchitectureState,
    AsilCoverage,
    Cohesion,
    ComponentRow,
    Coupling,
    CtChange,
    Integrity,
    LookAtEntry,
    LookAtLine,
    MemberRow,
    MetricsReport,
    Movement,
    NotAvailable,
    RequirementCoverage,
    RunnableCoverage,
    TaskCoverage,
    _head_cause,
    component_cohesion,
    composition_tables,
    compute_metrics,
    look_at,
    measure,
    measure_cohesion,
    measure_coupling,
    measure_coverage,
    measure_integrity,
    measure_links,
    measure_size,
    top_components,
)
from swcraft_tools.model_registry import current_registry
from swcraft_tools.model_state import (
    ElementParseError,
    ModelState,
    RetiredStorageError,
    RevisionReadError,
    load_model_state,
)
from swcraft_tools.project import require_project
from swcraft_tools.schema import (
    Composition,
    Connector,
    CSInterface,
    ExternalInterface,
    Partition,
    Requirement,
    SRInterface,
    SWComponent,
    Task,
)
from swcraft_tools.validator import ConsistencyIssue

P = "provide"
R = "require"


def swc(name: str, *ports: str, external: bool = False, runnables: int = 0) -> SWComponent:
    """A component; a port is ``p<name>`` for provide and ``r<name>`` for require."""
    return SWComponent.model_validate(
        {
            "name": name,
            "external": external,
            "ports": [{"name": port, "direction": P if port[0] == "p" else R, "interface": "I"} for port in ports],
            "runnables": [{"name": f"run{index}"} for index in range(runnables)],
        }
    )


def stub(name: str, *ports: str) -> SWComponent:
    return swc(name, *ports, external=True)


def composition(
    name: str,
    *,
    root: bool = False,
    members: tuple[tuple[str, str], ...] = (),
    boxes: tuple[tuple[str, str], ...] = (),
    ports: tuple[str, ...] = (),
    delegations: tuple[tuple[str, str, str], ...] = (),
) -> Composition:
    """*members* and *boxes* are ``(member name, type)``; a delegation is ``(port, member, member port)``."""
    return Composition.model_validate(
        {
            "name": name,
            "title": name,
            "root": root,
            "members": [{"name": member, "type": type_} for member, type_ in members]
            + [{"name": member, "composition": type_} for member, type_ in boxes],
            "ports": [{"name": port, "direction": P if port[0] == "p" else R, "interface": "I"} for port in ports],
            "delegations": [
                {"name": f"d{index}", "port": port, "member": member, "member_port": member_port}
                for index, (port, member, member_port) in enumerate(delegations)
            ],
        }
    )


def end(address: str) -> dict[str, str]:
    """``A.p`` is a component's port; ``box:B.p`` a composition's boundary port."""
    owner, port = address.split(".")
    if owner.startswith("box:"):
        return {"composition": owner.removeprefix("box:"), "port": port}
    return {"swc": owner, "port": port}


def connector(name: str, from_: str, to: str, owner: str | None = None) -> Connector:
    return Connector.model_validate({"name": name, "from": end(from_), "to": end(to), "composition": owner})


def model(*elements: object, **layers: list[object]) -> ModelState:
    """A state from elements, sorted into their layers by type; the keywords hold the rest."""
    by_type = {SWComponent: "swc", Composition: "compositions", Connector: "connectors"}
    sorted_layers: dict[str, list[object]] = {"swc": [], "compositions": [], "connectors": [], **layers}
    for element in elements:
        sorted_layers[by_type[type(element)]].append(element)
    return ModelState(layers=sorted_layers)


def row(state: ModelState, name: str) -> ComponentRow:
    return next(entry for entry in measure_coupling(state)[0] if entry.name == name)


def figures(state: ModelState) -> dict[str, tuple[int, int]]:
    return {entry.name: (entry.ca, entry.ce) for entry in measure_coupling(state)[0]}


def coupling(state: ModelState) -> Coupling:
    answer = measure_coupling(state)[1]
    assert isinstance(answer, Coupling)
    return answer


def fillers(count: int) -> list[SWComponent]:
    """*count* components with no port: each Ct = 0."""
    return [swc(f"Z{index}") for index in range(count)]


def sr_interface() -> SRInterface:
    return SRInterface.model_validate(
        {"name": "I", "kind": "sender_receiver", "data_elements": [{"name": "d", "implementation_type": "T"}]}
    )


def cs_interface() -> CSInterface:
    return CSInterface.model_validate({"name": "I", "kind": "client_server", "operations": [{"name": "op"}]})


def external_interface() -> ExternalInterface:
    return ExternalInterface.model_validate(
        {
            "name": "I",
            "kind": "external_interface",
            "external_ref": "/X/I",
            "external_dest": "SENDER-RECEIVER-INTERFACE",
        }
    )


class TestSize:
    def test_counts_every_layer_in_display_order_and_the_members(self) -> None:
        state = model(
            swc("A", "pa", "rb", runnables=2),
            stub("S", "ps"),
            composition("Root", root=True, ports=("pOut",), delegations=(("pOut", "a", "pa"),), members=(("a", "A"),)),
            connector("c1", "S.ps", "A.rb"),
        )
        size = measure_size(state, current_registry())
        assert [name for name, _ in size.layers] == list(current_registry().display_order)
        counts = dict(size.layers)
        assert (counts["swc"], counts["compositions"], counts["connectors"], counts["tasks"]) == (2, 1, 1, 0)
        assert (size.components, size.external_stubs, size.compositions) == (1, 1, 1)
        # Ports: 2 on A, 1 on S, 1 on the boundary. Connectors and delegations 1 each, runnables 2.
        assert (size.ports, size.runnables, size.connectors, size.delegations) == (4, 2, 1, 1)
        assert (size.component_ports, size.stub_ports, size.boundary_ports) == (2, 1, 1)

    def test_no_composition_is_depth_zero(self) -> None:
        assert measure_size(model(swc("A")), current_registry()).nesting_depth == 0

    def test_one_flat_composition_is_depth_one(self) -> None:
        state = model(swc("A"), composition("Root", root=True, members=(("a", "A"),)))
        assert measure_size(state, current_registry()).nesting_depth == 1

    def test_the_deepest_chain_is_the_depth(self) -> None:
        # Root > Mid > Leaf is three levels; Root > Side is two.
        state = model(
            composition("Root", root=True, boxes=(("m", "Mid"), ("s", "Side"))),
            composition("Mid", boxes=(("l", "Leaf"),)),
            composition("Leaf"),
            composition("Side"),
        )
        assert measure_size(state, current_registry()).nesting_depth == 3

    def test_a_composition_cycle_still_answers(self) -> None:
        state = model(composition("A", boxes=(("b", "B"),)), composition("B", boxes=(("a", "A"),)))
        assert measure_size(state, current_registry()).nesting_depth == 2

    def test_a_cycle_does_not_shorten_the_chain_entered_elsewhere(self) -> None:
        # A holds B and C, B holds A: B > A > C is three deep, though B reached from A is cut at 1.
        state = model(
            composition("A", boxes=(("b", "B"), ("c", "C"))),
            composition("B", boxes=(("a", "A"),)),
            composition("C"),
        )
        assert measure_size(state, current_registry()).nesting_depth == 3

    def test_the_depth_depends_on_no_name_order_on_a_cycle(self) -> None:
        # The same shape under names that sort the other way round: Z > Y > X is three deep.
        state = model(
            composition("Y", boxes=(("z", "Z"), ("x", "X"))),
            composition("Z", boxes=(("y", "Y"),)),
            composition("X"),
        )
        assert measure_size(state, current_registry()).nesting_depth == 3

    def test_a_cycle_group_counts_as_many_levels_as_it_holds(self) -> None:
        # X holds L1, L2 and L3, each of which holds X: one group of four, so 4 deep, where the longest
        # chain with no repeat, L1 > X > L2, is 3. The definition states the rule for a cycle.
        state = model(
            composition("X", boxes=(("a", "L1"), ("b", "L2"), ("c", "L3"))),
            *(composition(name, boxes=(("x", "X"),)) for name in ("L1", "L2", "L3")),
        )
        assert measure_size(state, current_registry()).nesting_depth == 4

    def test_mutually_containing_compositions_answer_in_bounded_time(self) -> None:
        # Forty compositions, each holding every other one: every simple path through them is far
        # beyond reach, while the groups are counted in one pass. Depth: one group of forty.
        names = [f"K{index:02d}" for index in range(40)]
        state = model(
            *(
                composition(name, boxes=tuple((other.lower(), other) for other in names if other != name))
                for name in names
            )
        )
        assert measure_size(state, current_registry()).nesting_depth == 40

    def test_a_long_chain_reaches_no_recursion_limit(self) -> None:
        names = [f"N{index:04d}" for index in range(3000)]
        state = model(
            *(composition(name, boxes=((f"c{index}", names[index + 1]),)) for index, name in enumerate(names[:-1])),
            composition(names[-1]),
        )
        assert measure_size(state, current_registry()).nesting_depth == 3000

    def test_a_composition_containing_itself_still_answers(self) -> None:
        state = model(composition("A", boxes=(("a", "A"),)))
        assert measure_size(state, current_registry()).nesting_depth == 1

    def test_a_nested_type_nobody_declares_adds_no_level(self) -> None:
        state = model(composition("A", boxes=(("g", "Ghost"),)))
        assert measure_size(state, current_registry()).nesting_depth == 1

    def test_a_repeated_component_name_counts_both_files_and_one_row(self) -> None:
        state = model(swc("A"), swc("A"), swc("B"))
        assert measure_size(state, current_registry()).components == 3
        assert coupling(state).n == 2


class TestDirection:
    @pytest.mark.parametrize("interface", [sr_interface, cs_interface, external_interface])
    def test_the_requirer_depends_on_the_provider_for_every_kind(self, interface) -> None:
        # Sender or server A, receiver or client B: B needs A.
        state = model(swc("A", "pa"), swc("B", "rb"), connector("c", "A.pa", "B.rb"), interfaces=[interface()])
        assert figures(state) == {"A": (1, 0), "B": (0, 1)}

    @pytest.mark.parametrize("interface", [sr_interface, cs_interface, external_interface])
    def test_a_reversed_connector_states_the_same_dependency(self, interface) -> None:
        state = model(swc("A", "pa"), swc("B", "rb"), connector("c", "B.rb", "A.pa"), interfaces=[interface()])
        assert figures(state) == {"A": (1, 0), "B": (0, 1)}
        assert measure_links(state).not_measured == 0

    @pytest.mark.parametrize(("left", "right"), [("pa", "pb"), ("ra", "rb")])
    def test_two_ends_of_one_direction_draw_no_link_and_are_counted(self, left, right) -> None:
        state = model(swc("A", left), swc("B", right), connector("c", f"A.{left}", f"B.{right}"))
        assert figures(state) == {"A": (0, 0), "B": (0, 0)}
        assert coupling(state).links_not_measured == 1
        assert coupling(state).not_measured_by_reason == (("one direction", 1),)

    def test_a_mutual_pair_counts_twice(self) -> None:
        # A needs B and B needs A: Ca 1 + Ce 1 = Ct 2 on each side, and two dependencies in the model,
        # one per directed pair, so the model's count is half the sum of Ct.
        state = model(
            swc("A", "pa", "ra"),
            swc("B", "pb", "rb"),
            connector("c1", "A.pa", "B.rb"),
            connector("c2", "B.pb", "A.ra"),
        )
        assert (row(state, "A").ct, row(state, "B").ct) == (2, 2)
        assert coupling(state).dependencies == 2

    def test_one_more_connector_is_one_more_dependency_stubs_included(self) -> None:
        # B needs A, A needs the stub S, and the stub T needs B: three directed pairs, each with a
        # declared end. The sum of Ct is 4 and the sum of Ce 2; neither is the count of dependencies.
        state = model(
            swc("A", "pa", "rs"),
            swc("B", "rb", "pb"),
            stub("S", "ps"),
            stub("T", "rt"),
            connector("c1", "A.pa", "B.rb"),
            connector("c2", "S.ps", "A.rs"),
            connector("c3", "B.pb", "T.rt"),
        )
        assert figures(state) == {"A": (1, 1), "B": (1, 1)}
        assert coupling(state).dependencies == 3

    def test_a_dependency_stated_twice_counts_once(self) -> None:
        state = model(
            swc("A", "pa", "pb"),
            swc("B", "ra", "rb"),
            connector("c1", "A.pa", "B.ra"),
            connector("c2", "A.pb", "B.rb"),
        )
        assert figures(state) == {"A": (1, 0), "B": (0, 1)}


class TestNotMeasured:
    def test_an_end_that_does_not_resolve_is_counted(self) -> None:
        state = model(swc("A", "pa"), connector("c", "A.pa", "Ghost.r"))
        assert coupling(state).links_not_measured == 1
        assert coupling(state).not_measured_by_reason == (("unreached end", 1),)

    def test_a_boundary_port_no_delegation_pulls_is_counted(self) -> None:
        state = model(
            swc("A", "pa"),
            swc("B", "rb"),
            composition("Box", members=(("b", "B"),), ports=("rIn",)),
            connector("c", "A.pa", "box:Box.rIn"),
        )
        assert coupling(state).links_not_measured == 1
        assert coupling(state).not_measured_by_reason == (("unreached end", 1),)
        assert figures(state) == {"A": (0, 0), "B": (0, 0)}

    def test_a_repeat_of_a_connector_name_is_counted_and_reads_the_first(self) -> None:
        state = model(
            swc("A", "pa"),
            swc("B", "rb"),
            swc("C", "rc"),
            connector("c", "A.pa", "B.rb"),
            connector("c", "A.pa", "C.rc"),
        )
        assert figures(state) == {"A": (1, 0), "B": (0, 1), "C": (0, 0)}
        assert coupling(state).links_not_measured == 1
        assert coupling(state).not_measured_by_reason == (("repeated name", 1),)


class TestDelegation:
    @staticmethod
    def wired(*wrappers: Composition, sender: str, receiver: str) -> ModelState:
        return model(
            swc("A", "pa"),
            swc("B", "rb"),
            swc("C", "rc"),
            *wrappers,
            connector("c", sender, receiver),
        )

    def test_a_link_follows_two_levels_of_delegation_with_fan_out(self) -> None:
        # A's receiver side fans out inside Outer > Inner to B and C: B needs A and C needs A.
        inner = composition(
            "Inner",
            members=(("b", "B"), ("c", "C")),
            ports=("rIn",),
            delegations=(("rIn", "b", "rb"), ("rIn", "c", "rc")),
        )
        outer = composition("Outer", boxes=(("i", "Inner"),), ports=("rIn",), delegations=(("rIn", "i", "rIn"),))
        wrapped = self.wired(inner, outer, sender="A.pa", receiver="box:Outer.rIn")
        assert figures(wrapped) == {"A": (2, 0), "B": (0, 1), "C": (0, 1)}

    def test_wrapping_moves_no_figure(self) -> None:
        direct = model(
            swc("A", "pa"),
            swc("B", "rb"),
            swc("C", "rc"),
            connector("c1", "A.pa", "B.rb"),
            connector("c2", "A.pa", "C.rc"),
        )
        inner = composition(
            "Inner",
            members=(("b", "B"), ("c", "C")),
            ports=("rIn",),
            delegations=(("rIn", "b", "rb"), ("rIn", "c", "rc")),
        )
        outer = composition("Outer", boxes=(("i", "Inner"),), ports=("rIn",), delegations=(("rIn", "i", "rIn"),))
        wrapped = self.wired(inner, outer, sender="A.pa", receiver="box:Outer.rIn")
        assert measure_coupling(wrapped) == measure_coupling(direct)

    def test_a_composition_is_never_a_node(self) -> None:
        inner = composition("Inner", members=(("b", "B"),), ports=("rIn",), delegations=(("rIn", "b", "rb"),))
        state = self.wired(inner, sender="A.pa", receiver="box:Inner.rIn")
        assert "Inner" not in figures(state)
        assert coupling(state).n == 3


class TestStubsAreNeighbours:
    def test_a_link_to_a_stub_counts_for_the_declared_side_only(self) -> None:
        state = model(swc("A", "ra", "pa"), stub("S", "ps", "rs"), connector("c", "S.ps", "A.ra"))
        assert figures(state) == {"A": (0, 1)}
        entry = row(state, "A")
        assert entry.external_neighbours == 1
        # n = 1: the stub is out of the population, so the mean is A's own 1.
        assert (coupling(state).n, coupling(state).mean) == (1, Fraction(1))

    def test_both_directions_to_one_stub_are_two_dependencies_and_one_neighbour(self) -> None:
        state = model(
            swc("A", "ra", "pa"),
            stub("S", "ps", "rs"),
            connector("c1", "S.ps", "A.ra"),
            connector("c2", "A.pa", "S.rs"),
        )
        entry = row(state, "A")
        assert (entry.ca, entry.ce, entry.external_neighbours) == (1, 1, 1)

    def test_a_stub_to_stub_connector_is_counted_nowhere_but_the_size(self) -> None:
        state = model(swc("A"), stub("S", "ps"), stub("T", "rt"), connector("c", "S.ps", "T.rt"))
        assert figures(state) == {"A": (0, 0)}
        assert coupling(state).links_not_measured == 0
        assert measure_size(state, current_registry()).connectors == 1

    def test_a_pair_of_stubs_of_one_direction_is_counted_nowhere(self) -> None:
        state = model(swc("A"), stub("S", "ps"), stub("T", "pt"), connector("c", "S.ps", "T.pt"))
        assert coupling(state).links_not_measured == 0

    def test_the_stub_line_counts_and_orders_by_declared_dependents(self) -> None:
        state = model(
            swc("A", "ra", "rb", "pa"),
            swc("B", "ra"),
            stub("Com", "ps"),
            stub("Dem", "ps"),
            stub("Sink", "rs"),
            connector("c1", "Dem.ps", "A.ra"),
            connector("c2", "Dem.ps", "B.ra"),
            connector("c3", "Com.ps", "A.rb"),
            connector("c4", "A.pa", "Sink.rs"),
        )
        answer = coupling(state)
        # Three stubs; Sink depends on A and nobody declared depends on it, so it is not listed.
        assert answer.external_stubs == 3
        assert answer.stubs_depended_on == (("Dem", 2), ("Com", 1))

    def test_the_stub_list_stops_at_five_ties_by_name(self) -> None:
        names = ["S1", "S2", "S3", "S4", "S5", "S6"]
        ports = [f"r{name}" for name in names]
        state = model(
            swc("A", *ports),
            *[stub(name, "ps") for name in names],
            *[connector(f"c{name}", f"{name}.ps", f"A.r{name}") for name in names],
        )
        assert coupling(state).stubs_depended_on == tuple((name, 1) for name in names[:5])

    def test_a_stub_turned_declared_keeps_its_neighbours_ct(self) -> None:
        wiring = connector("c", "S.ps", "A.ra")
        as_stub = model(swc("A", "ra"), stub("S", "ps"), wiring)
        as_declared = model(swc("A", "ra"), swc("S", "ps"), wiring)
        assert row(as_stub, "A").ct == row(as_declared, "A").ct == 1
        assert (coupling(as_stub).n, coupling(as_declared).n) == (1, 2)


class TestOneTypeCountsOnce:
    def test_a_type_placed_twice_is_one_node_with_the_union_of_its_links(self) -> None:
        state = model(
            swc("Twice", "ra", "rb"),
            swc("A", "pa"),
            swc("B", "pb"),
            composition("Root", root=True, members=(("t1", "Twice"), ("t2", "Twice"), ("a", "A"), ("b", "B"))),
            connector("c1", "A.pa", "Twice.ra"),
            connector("c2", "B.pb", "Twice.rb"),
        )
        assert figures(state) == {"A": (1, 0), "B": (1, 0), "Twice": (0, 2)}

    def test_a_connector_between_two_prototypes_of_one_type_is_dropped_uncounted(self) -> None:
        state = model(swc("T", "pa", "ra"), connector("c", "T.pa", "T.ra"))
        assert figures(state) == {"T": (0, 0)}
        assert coupling(state).links_not_measured == 0


class TestStatistics:
    def test_no_component_makes_the_block_not_available(self) -> None:
        assert measure_coupling(model())[1] == NotAvailable("the model declares no component")

    def test_stubs_alone_make_the_block_not_available(self) -> None:
        assert measure_coupling(model(stub("S")))[1] == NotAvailable("the model declares no component")

    def test_five_components_at_the_extreme_shape_have_no_outliers_to_show(self) -> None:
        # Ct = (0, 0, 0, 0, 1): only a stub link gives the odd sum. mu = 1/5; the variance is
        # (4 * 1/25 + 16/25) / 5 = 4/25, so the deviation is exactly 2/5 and 1 lies exactly two
        # deviations out: the strict test could never fire, which is the case for n/a.
        state = model(swc("A", "ra"), *fillers(4), stub("S", "ps"), connector("c", "S.ps", "A.ra"))
        answer = coupling(state)
        assert (answer.mean, answer.variance) == (Fraction(1, 5), Fraction(4, 25))
        reason = "fewer than six components: no value can lie more than two deviations out"
        assert answer.hubs == NotAvailable(reason)
        assert answer.isolated == NotAvailable(reason)

    def test_with_hubs_not_available_the_trimmed_figures_read_everyone(self) -> None:
        state = model(swc("A", "ra"), *fillers(4), stub("S", "ps"), connector("c", "S.ps", "A.ra"))
        answer = coupling(state)
        assert (answer.trimmed_mean, answer.trimmed_max, answer.trimmed_over_all) == (Fraction(1, 5), 1, True)

    def test_six_components_can_hold_a_hub(self) -> None:
        # Ct = (5, 0, 0, 0, 0, 0): mu = 5/6; variance = (5 * 25/36 + 625/36) / 6 = 125/36.
        # (5 - 5/6)^2 = 625/36 > 4 * 125/36 = 500/36: a hub.
        names = [f"S{index}" for index in range(5)]
        state = model(
            swc("Hub", *[f"r{name}" for name in names]),
            *fillers(5),
            *[stub(name, "ps") for name in names],
            *[connector(f"c{name}", f"{name}.ps", f"Hub.r{name}") for name in names],
        )
        answer = coupling(state)
        assert (answer.mean, answer.variance) == (Fraction(5, 6), Fraction(125, 36))
        assert answer.hubs == ("Hub",)
        # mu^2 = 25/36 < 500/36: the lower threshold is below zero.
        assert answer.isolated == NotAvailable("the lower threshold is below zero")
        # Without the hub five zeroes remain, and the line reads them, not everyone.
        assert (answer.trimmed_mean, answer.trimmed_max, answer.trimmed_over_all) == (Fraction(0), 0, False)

    def test_exactly_on_the_threshold_is_no_hub(self) -> None:
        # (0 x 8, 1 x 2) at n = 10: mu = 1/5; variance = (8 * 1/25 + 2 * 16/25) / 10 = 4/25,
        # so mu + 2 deviations = 1/5 + 4/5 = 1, and Ct = 1 is on the threshold.
        state = model(swc("A", "pa"), swc("B", "rb"), *fillers(8), connector("c", "A.pa", "B.rb"))
        answer = coupling(state)
        assert (answer.mean, answer.variance) == (Fraction(1, 5), Fraction(4, 25))
        assert answer.hubs == ()
        assert answer.isolated == NotAvailable("the lower threshold is below zero")
        assert (answer.trimmed_mean, answer.trimmed_max, answer.trimmed_over_all) == (Fraction(1, 5), 1, False)

    def test_an_isolated_component_where_the_lower_threshold_stands(self) -> None:
        # A ring of five, each Ct = 2, and Z with 0: mu = 10/6 = 5/3;
        # variance = (5 * 1/9 + 25/9) / 6 = 5/9. mu^2 = 25/9 >= 20/9, so the threshold stands;
        # (5/3)^2 = 25/9 > 20/9: Z is isolated, and (2 - 5/3)^2 = 1/9 < 20/9: nobody is a hub.
        names = ["A", "B", "C", "D", "E"]
        ring = [
            connector(f"c{name}", f"{name}.pout", f"{names[(index + 1) % 5]}.rin") for index, name in enumerate(names)
        ]
        state = model(*[swc(name, "pout", "rin") for name in names], swc("Z"), *ring)
        answer = coupling(state)
        assert (answer.mean, answer.variance) == (Fraction(5, 3), Fraction(5, 9))
        assert (answer.hubs, answer.isolated) == ((), ("Z",))
        # Over the five left: mean 2, highest 2.
        assert (answer.trimmed_mean, answer.trimmed_max, answer.trimmed_over_all) == (Fraction(2), 2, False)

    def test_equal_figures_have_no_outlier(self) -> None:
        answer = coupling(model(*fillers(6)))
        assert (answer.variance, answer.hubs, answer.isolated) == (Fraction(0), (), ())

    def test_hubs_run_from_the_highest_ties_by_name(self) -> None:
        # Big 7, Hub1 and Hub2 6, forty zeroes: n = 43, mu = 19/43, the mean of squares is 121/43,
        # so the variance is 121/43 - 361/1849 = 4842/1849 and 4 * variance = 19368/1849.
        # (6 - 19/43)^2 = (239/43)^2 = 57121/1849, above it: all three are hubs.
        stubs = [f"S{index}" for index in range(7)]
        state = model(
            swc("Hub2", *[f"r{name}" for name in stubs[:6]]),
            swc("Hub1", *[f"r{name}" for name in stubs[:6]]),
            swc("Big", *[f"r{name}" for name in stubs]),
            *fillers(40),
            *[stub(name, "ps") for name in stubs],
            *[connector(f"a{name}", f"{name}.ps", f"Hub2.r{name}") for name in stubs[:6]],
            *[connector(f"b{name}", f"{name}.ps", f"Hub1.r{name}") for name in stubs[:6]],
            *[connector(f"c{name}", f"{name}.ps", f"Big.r{name}") for name in stubs],
        )
        assert coupling(state).hubs == ("Big", "Hub1", "Hub2")

    def test_the_top_list_stops_at_ten_highest_first_ties_by_name(self) -> None:
        rows = [
            ComponentRow(f"C{index:02}", "application", 0, index % 3, 0, 0, NotAvailable("no runnable"), 0)
            for index in range(12)
        ]
        top = top_components(rows)
        assert len(top) == 10
        # Ct 2 for indexes 2, 5, 8, 11; then Ct 1 for 1, 4, 7, 10; then Ct 0 for 0, 3.
        assert [entry.name for entry in top[:5]] == ["C02", "C05", "C08", "C11", "C01"]


class TestCompositionTables:
    @staticmethod
    def nested() -> ModelState:
        """Root holds Chassis, Power and the stub Com; Chassis fans its boundary out to A and B."""
        return model(
            swc("A", "pa"),
            swc("B", "pb"),
            swc("C", "rc", "rd"),
            stub("Com", "ps"),
            composition(
                "Root", root=True, boxes=(("Chassis", "ChassisT"), ("Power", "PowerT")), members=(("Com", "Com"),)
            ),
            composition(
                "ChassisT",
                members=(("a", "A"), ("b", "B")),
                ports=("pOut",),
                delegations=(("pOut", "a", "pa"), ("pOut", "b", "pb")),
            ),
            composition(
                "PowerT",
                members=(("c", "C"),),
                ports=("rIn", "rCom"),
                delegations=(("rIn", "c", "rc"), ("rCom", "c", "rd")),
            ),
            connector("wire", "box:ChassisT.pOut", "box:PowerT.rIn"),
            connector("com", "Com.ps", "box:PowerT.rCom"),
        )

    @staticmethod
    def table(state: ModelState, name: str):
        tables = composition_tables(state)[0]
        return next(table for table in tables if table.composition == name)

    def test_a_fan_out_below_a_box_is_one_link_at_the_outer_level(self) -> None:
        table = self.table(self.nested(), "Root")
        # Power needs Chassis and needs Com: two links, Power Ce 2; Chassis and Com Ca 1.
        assert table.rows == (
            MemberRow("Power", "composition", 0, 2, "PowerT"),
            MemberRow("Chassis", "composition", 1, 0, "ChassisT"),
            MemberRow("Com", "stub", 1, 0, "Com"),
        )
        assert table.links == 2

    def test_a_member_wired_only_through_the_boundary_shows_zero(self) -> None:
        table = self.table(self.nested(), "ChassisT")
        assert table.rows == (MemberRow("a", "component", 0, 0, "A"), MemberRow("b", "component", 0, 0, "B"))
        assert table.links == 0

    def test_the_atomic_figures_do_walk_the_same_links_down(self) -> None:
        state = self.nested()
        assert figures(state) == {"A": (1, 0), "B": (1, 0), "C": (0, 3)}

    def test_a_link_inside_a_box_is_in_that_box_alone(self) -> None:
        state = model(
            swc("A", "pa"),
            swc("B", "rb"),
            composition("Root", root=True, boxes=(("box", "Box"),)),
            composition("Box", members=(("a", "A"), ("b", "B"))),
            connector("c", "A.pa", "B.rb", owner="Box"),
        )
        assert self.table(state, "Box").links == 1
        assert self.table(state, "Root").links == 0
        assert composition_tables(state)[1] == ()

    def test_a_self_link_is_dropped_uncounted(self) -> None:
        state = model(
            swc("T", "pa", "ra"),
            composition("Root", root=True, members=(("t1", "T"), ("t2", "T"))),
            connector("c", "T.pa", "T.ra"),
        )
        assert self.table(state, "Root").rows == (MemberRow("t1", "component", 0, 0, "T"),)
        assert composition_tables(state)[1] == ()

    def test_a_repeated_connector_name_is_a_link_per_file_in_the_table_and_one_in_the_figures(self) -> None:
        # The figures read a connector name once (the first file, the repeat counted under the limit);
        # a composition table reads every connector file, so two files sharing a name and joining
        # different pairs are two links there, as the Size definition says.
        state = model(
            swc("A", "pa"),
            swc("B", "rb"),
            swc("C", "rc"),
            composition("Root", root=True, members=(("a", "A"), ("b", "B"), ("c", "C"))),
            connector("c", "A.pa", "B.rb"),
            connector("c", "A.pa", "C.rc"),
        )
        assert figures(state) == {"A": (1, 0), "B": (0, 1), "C": (0, 0)}
        assert coupling(state).not_measured_by_reason == (("repeated name", 1),)
        assert self.table(state, "Root").links == 2
        assert composition_tables(state)[1] == ()

    def test_a_repeated_type_is_one_row_under_its_first_member(self) -> None:
        state = model(
            swc("T", "ra"),
            swc("A", "pa"),
            composition("Root", root=True, members=(("second", "A"), ("t1", "T"), ("t2", "T"))),
            connector("c", "A.pa", "T.ra"),
        )
        assert self.table(state, "Root").rows == (
            MemberRow("second", "component", 1, 0, "A"),
            MemberRow("t1", "component", 0, 1, "T"),
        )

    def test_peers_count_once_per_direction_and_ct_sums_them(self) -> None:
        state = model(
            swc("A", "pa", "pb", "ra"),
            swc("B", "rb", "rc", "pc"),
            composition("Root", root=True, members=(("a", "A"), ("b", "B"))),
            connector("c1", "A.pa", "B.rb"),
            connector("c2", "A.pb", "B.rc"),
            connector("c3", "B.pc", "A.ra"),
        )
        table = self.table(state, "Root")
        assert table.rows == (MemberRow("a", "component", 1, 1, "A"), MemberRow("b", "component", 1, 1, "B"))
        assert table.links == 2

    def test_an_unknown_owner_is_skipped_and_counted(self) -> None:
        state = model(
            swc("A", "pa"),
            swc("B", "rb"),
            composition("Root", root=True, members=(("a", "A"), ("b", "B"))),
            connector("c", "A.pa", "B.rb", owner="Ghost"),
        )
        assert self.table(state, "Root").links == 0
        assert composition_tables(state)[1] == (("unresolved composition", 1),)

    @pytest.mark.parametrize("roots", [0, 2])
    def test_no_single_root_skips_a_rootless_connector_and_counts_it(self, roots) -> None:
        state = model(
            swc("A", "pa"),
            swc("B", "rb"),
            composition("One", root=roots == 2, members=(("a", "A"), ("b", "B"))),
            composition("Two", root=roots == 2, members=(("a", "A"), ("b", "B"))),
            connector("c", "A.pa", "B.rb"),
        )
        tables, not_placed = composition_tables(state)
        assert [table.links for table in tables] == [0, 0]
        assert not_placed == (("unresolved composition", 1),)

    def test_an_end_that_is_no_member_is_skipped_and_counted(self) -> None:
        state = model(
            swc("A", "pa"),
            swc("B", "rb"),
            composition("Root", root=True, members=(("a", "A"),)),
            connector("c", "A.pa", "B.rb"),
        )
        assert composition_tables(state)[1] == (("end not a member", 1),)

    def test_an_unresolved_port_is_counted(self) -> None:
        state = model(
            swc("A", "pa"),
            swc("B", "rb"),
            composition("Root", root=True, members=(("a", "A"), ("b", "B"))),
            connector("c", "A.pa", "B.gone"),
        )
        assert composition_tables(state)[1] == (("unresolved port", 1),)

    def test_a_pair_of_one_direction_is_counted(self) -> None:
        state = model(
            swc("A", "pa"),
            swc("B", "pb"),
            composition("Root", root=True, members=(("a", "A"), ("b", "B"))),
            connector("c", "A.pa", "B.pb"),
        )
        assert composition_tables(state)[1] == (("one direction", 1),)
        assert self.table(state, "Root").links == 0

    def test_a_reversed_connector_keeps_its_direction(self) -> None:
        state = model(
            swc("A", "pa"),
            swc("B", "rb"),
            composition("Root", root=True, members=(("a", "A"), ("b", "B"))),
            connector("c", "B.rb", "A.pa"),
        )
        assert self.table(state, "Root").rows == (
            MemberRow("a", "component", 1, 0, "A"),
            MemberRow("b", "component", 0, 1, "B"),
        )

    def test_an_empty_composition_is_not_available(self) -> None:
        table = self.table(model(composition("Shell")), "Shell")
        assert table.rows == NotAvailable("the composition has no member")

    def test_a_composition_placed_twice_is_one_table(self) -> None:
        state = model(
            swc("A"),
            composition("Root", root=True, boxes=(("x", "Box"), ("y", "Box"))),
            composition("Box", members=(("a", "A"),)),
        )
        assert [table.composition for table in composition_tables(state)[0]] == ["Root", "Box"]
        assert self.table(state, "Root").rows == (MemberRow("x", "composition", 0, 0, "Box"),)

    def test_an_unreferenced_composition_is_still_a_table(self) -> None:
        state = model(
            swc("A"),
            composition("Root", root=True, members=(("a", "A"),)),
            composition("Orphan", members=(("a", "A"),)),
        )
        assert [table.composition for table in composition_tables(state)[0]] == ["Root", "Orphan"]

    def test_the_root_comes_first_and_the_rest_by_name_three_levels_deep(self) -> None:
        state = model(
            composition("Beta", boxes=(("g", "Gamma"),)),
            composition("Gamma", members=(("a", "A"), ("b", "B"))),
            composition("Zroot", root=True, boxes=(("b", "Beta"),)),
            swc("A", "pa"),
            swc("B", "rb"),
            connector("c", "A.pa", "B.rb", owner="Gamma"),
        )
        tables = composition_tables(state)[0]
        assert [table.composition for table in tables] == ["Zroot", "Beta", "Gamma"]
        assert tables[2].links == 1

    def test_a_member_type_no_file_declares_is_still_a_row(self) -> None:
        state = model(composition("Root", root=True, members=(("g", "Ghost"),)))
        assert self.table(state, "Root").rows == (MemberRow("g", "component", 0, 0, "Ghost"),)

    def test_a_connector_moved_out_of_every_table_moves_no_atomic_figure(self) -> None:
        def wired(owner: str | None) -> ModelState:
            return model(
                swc("A", "pa"),
                swc("B", "rb"),
                composition("Root", root=True, members=(("a", "A"), ("b", "B"))),
                connector("c", "A.pa", "B.rb", owner=owner),
            )

        placed, unplaced = wired(None), wired("Ghost")
        assert composition_tables(placed)[1] != composition_tables(unplaced)[1]
        assert measure_coupling(placed) == measure_coupling(unplaced)

    def test_a_link_between_two_stubs_is_a_link_of_their_table(self) -> None:
        # The coupling draws nothing between two stubs; the table they are members of does.
        state = model(
            swc("A"),
            stub("S", "ps"),
            stub("T", "rt"),
            composition("Root", root=True, members=(("a", "A"), ("s", "S"), ("t", "T"))),
            connector("c", "S.ps", "T.rt"),
        )
        table = self.table(state, "Root")
        assert table.links == 1
        assert table.rows == (
            MemberRow("s", "stub", 1, 0, "S"),
            MemberRow("t", "stub", 0, 1, "T"),
            MemberRow("a", "component", 0, 0, "A"),
        )
        assert coupling(state).dependencies == 0

    def test_a_nested_box_s_boundary_port_that_does_not_resolve_is_counted(self) -> None:
        state = model(
            swc("A", "pa"),
            composition("Root", root=True, members=(("a", "A"),), boxes=(("box", "Box"),)),
            composition("Box", ports=("rIn",)),
            connector("c", "A.pa", "box:Box.rGone"),
        )
        assert composition_tables(state)[1] == (("unresolved port", 1),)
        assert self.table(state, "Root").links == 0

    def test_a_connector_is_counted_under_the_first_reason_that_holds(self) -> None:
        # The end B is no member of Root and B.gone does not resolve: counted once, as no member.
        state = model(
            swc("A", "pa"),
            swc("B", "rb"),
            composition("Root", root=True, members=(("a", "A"),)),
            connector("c", "A.pa", "B.gone"),
        )
        assert composition_tables(state)[1] == (("end not a member", 1),)

    def test_a_self_link_with_a_port_that_does_not_resolve_is_counted_as_that(self) -> None:
        # The port check comes before the self-link check, so this one is counted and not dropped.
        state = model(
            swc("T", "pa"),
            composition("Root", root=True, members=(("t1", "T"), ("t2", "T"))),
            connector("c", "T.pa", "T.gone"),
        )
        assert composition_tables(state)[1] == (("unresolved port", 1),)
        assert self.table(state, "Root").rows == (MemberRow("t1", "component", 0, 0, "T"),)

    def test_a_table_carries_no_statistics(self) -> None:
        fields = set(self.table(self.nested(), "Root").__dataclass_fields__)
        assert fields == {"composition", "rows", "links"}


class TestMeasure:
    def test_measure_holds_every_block_of_one_side(self, tmp_path: Path) -> None:
        state = TestCompositionTables.nested()
        side = measure(state, str(tmp_path), "working tree", current_registry())
        assert isinstance(side, ArchitectureState)
        assert side.label == "working tree"
        assert side.size == measure_size(state, current_registry())
        assert (side.components, side.coupling) == measure_coupling(state)
        assert (side.composition_tables, side.links_not_placed_by_reason) == composition_tables(state)
        assert side.links_not_placed == sum(count for _, count in side.links_not_placed_by_reason)
        assert side.coverage == measure_coverage(state)
        assert side.cohesion == measure_cohesion(side.components, 0)

    def test_a_model_with_no_composition_has_no_table_and_answers(self, tmp_path: Path) -> None:
        side = measure(model(swc("A")), str(tmp_path), "working tree", current_registry())
        assert side.composition_tables == ()
        assert side.links_not_placed == 0


def issue(rule: str) -> ConsistencyIssue:
    return ConsistencyIssue(rule, f"finding of {rule}")


def component(name: str, *runnables: dict[str, Any], external: bool = False, **fields: Any) -> SWComponent:
    return SWComponent.model_validate({"name": name, "external": external, "runnables": list(runnables), **fields})


def periodic(name: str, **fields: Any) -> dict[str, Any]:
    return {"name": name, "events": [{"type": "timing", "period": 0.01}], **fields}


def initial(name: str) -> dict[str, Any]:
    return {"name": name, "events": [{"type": "init"}]}


def server(name: str) -> dict[str, Any]:
    return {"name": name, "events": [{"type": "operation", "port": "pS", "operation": "Op"}]}


def data_started(name: str, port: str = "rD") -> dict[str, Any]:
    """A runnable started by a data-received event on *port* and reading nothing."""
    return {"name": name, "events": [{"type": "data", "port": port, "element": "d"}]}


def call_returned(name: str, port: str = "rC") -> dict[str, Any]:
    """A runnable started by a ``call_returned`` event on *port* and calling nothing."""
    return {"name": name, "events": [{"type": "call_returned", "port": port, "operation": "Op"}]}


def task(name: str, *entries: dict[str, str]) -> Task:
    return Task.model_validate(
        {"name": name, "period": 0.01, "priority": 1, "partition": "P", "runnables": list(entries)}
    )


def requirement(identifier: str, **fields: Any) -> Requirement:
    return Requirement.model_validate(
        {"id": identifier, "title": identifier, "description": "d", "version": 1, "source": "architecture", **fields}
    )


class TestIntegrityFindings:
    def test_findings_count_by_severity_and_family_most_first_ties_by_name(self) -> None:
        # Errors: connector 2, composition 1, name 1; the two ones tie and go by name. One warning, trace.
        findings = [
            issue("connector.from-not-provide"),
            issue("name.duplicate-swc"),
            issue("connector.endpoint-unknown"),
            issue("composition.no-root"),
            issue("trace.requirement-unallocated"),
        ]
        integrity = measure_integrity(model(), findings)
        assert (integrity.errors, integrity.warnings) == (4, 1)
        assert integrity.errors_by_family == (("connector", 2), ("composition", 1), ("name", 1))
        assert integrity.warnings_by_family == (("trace", 1),)

    def test_a_rule_repeated_counts_per_finding_by_rule(self) -> None:
        findings = [issue("connector.b"), issue("connector.a"), issue("connector.b")]
        assert measure_integrity(model(), findings).by_rule == (("connector.a", 1), ("connector.b", 2))

    def test_no_finding_is_two_zeros_and_no_family(self) -> None:
        integrity = measure_integrity(model(), [])
        assert integrity == Integrity(0, 0, (), (), (), 0, ())


class TestUnconnectedPorts:
    def test_only_the_ports_nothing_reaches_are_named_sorted(self) -> None:
        # A has 3 ports, B 1: 4 in all. The connector reaches A.pOut and B.rIn; A.rZ and A.rA are left.
        state = model(swc("A", "pOut", "rZ", "rA"), swc("B", "rIn"), connector("c", "A.pOut", "B.rIn"))
        integrity = measure_integrity(state, [])
        assert integrity.ports == 4
        assert integrity.unconnected == ("A.rA", "A.rZ")
        assert row(state, "A").unconnected_ports == 2
        assert row(state, "B").unconnected_ports == 0

    def test_a_stubs_ports_are_left_out_of_both_figures(self) -> None:
        # The stub's rNobody is unconnected and is in neither the count of ports nor the list: one port in all.
        state = model(swc("A", "pOut"), stub("Ext", "rIn", "rNobody"), connector("c", "A.pOut", "Ext.rIn"))
        integrity = measure_integrity(state, [])
        assert (integrity.ports, integrity.unconnected) == (1, ())

    def test_a_port_delegated_to_an_unwired_boundary_port_counts_as_connected(self) -> None:
        state = model(
            swc("A", "pOut"),
            composition("Root", root=True, members=(("a", "A"),), ports=("pB",), delegations=(("pB", "a", "pOut"),)),
        )
        assert measure_integrity(state, []).unconnected == ()

    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        project = tmp_path / "project"
        shutil.copytree(REPO_ROOT / "examples" / "basic", project, ignore=shutil.ignore_patterns("dist", "views"))
        consumer = project / "model" / "swc" / "example" / "ExampleConsumerSWC.yaml"
        text = consumer.read_text(encoding="utf-8")
        spare = "ports:\n  - name: rSpare\n    direction: require\n    interface: IExample\n"
        consumer.write_text(text.replace("ports:\n", spare), encoding="utf-8", newline="\n")
        return project

    def test_the_count_agrees_with_the_query_of_the_computed_value(
        self, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        request = tmp_path / "request.json"
        document = {"address": {"layer": "swc"}, "members": "ports", "form": "index", "derived": ["unconnected"]}
        request.write_text(json.dumps(document), encoding="utf-8", newline="\n")
        argv = ["swcraft", "query", "--request", str(request), "--project", str(project / "swcraft.yaml")]
        monkeypatch.setattr("sys.argv", [*argv, "--format", "json"])
        main()
        answered = json.loads(capsys.readouterr().out)["rows"]

        integrity = measure_integrity(load_model_state(str(project / "model")), [])
        assert [f"{entry['element']}.{entry['member']}" for entry in answered] == list(integrity.unconnected)
        assert integrity.unconnected == ("ExampleConsumerSWC.rSpare",)


class TestMeasuredFindings:
    @staticmethod
    def basic(tmp_path: Path) -> Path:
        model_root = tmp_path / "model"
        shutil.copytree(REPO_ROOT / "examples" / "basic" / "model", model_root)
        return model_root

    def measured(self, model_root: Path) -> Integrity:
        state = load_model_state(str(model_root))
        return measure(state, str(model_root), "working tree", current_registry()).integrity

    def test_the_clean_example_has_no_finding(self, tmp_path: Path) -> None:
        integrity = self.measured(self.basic(tmp_path))
        assert (integrity.errors, integrity.warnings) == (0, 0)

    def test_a_warning_counts_as_a_warning_and_never_as_an_error(self, tmp_path: Path) -> None:
        # A requirement nothing satisfies is `trace.requirement-unallocated`, a warning, and the only finding.
        model_root = self.basic(tmp_path)
        (model_root / "requirements" / "REQ_LONELY.yaml").write_text(
            "id: REQ_LONELY\ntitle: t\ndescription: d\nreq_type: req\nversion: 1\nsource: architecture\n",
            encoding="utf-8",
            newline="\n",
        )
        integrity = self.measured(model_root)
        assert (integrity.errors, integrity.warnings) == (0, 1)
        assert integrity.errors_by_family == ()
        assert integrity.warnings_by_family == (("trace", 1),)
        assert integrity.by_rule == (("trace.requirement-unallocated", 1),)

    def test_an_error_counts_as_an_error(self, tmp_path: Path) -> None:
        # Removing an interface the ports name leaves findings of class error; the families add up to them.
        model_root = self.basic(tmp_path)
        (model_root / "interfaces" / "IExample.yaml").unlink()
        integrity = self.measured(model_root)
        assert integrity.errors > 0
        assert integrity.errors == sum(count for _, count in integrity.errors_by_family)


class TestAsilCoverage:
    def test_no_level_anywhere_is_not_available(self) -> None:
        state = model(component("A"), requirements=[requirement("R1")])
        assert measure_coverage(state).asil == NotAvailable(
            "no declared component or requirement states a safety level"
        )

    def test_a_stubs_level_alone_states_none(self) -> None:
        state = model(component("A"), component("Ext", external=True, asil="D"))
        assert measure_coverage(state).asil == NotAvailable(
            "no declared component or requirement states a safety level"
        )

    def test_a_partitions_level_alone_states_none(self) -> None:
        # A partition is rated, but no component's level is read off it: the reason names what is read.
        partition = Partition.model_validate({"name": "P0", "kind": "partition", "core": "C0", "asil": "D"})
        state = model(component("A"), hardware=[partition])
        assert measure_coverage(state).asil == NotAvailable(
            "no declared component or requirement states a safety level"
        )

    def test_a_component_level_makes_it_a_number(self) -> None:
        # A declares; B is silent and nothing rates it, so it is neither: 2 components, 1, 0, B.
        state = model(component("A", asil="B"), component("B"))
        assert measure_coverage(state).asil == AsilCoverage(2, 1, 0, ("B",))

    def test_a_requirement_level_alone_makes_it_a_number(self) -> None:
        # Nothing declares; A satisfies a requirement of level C, so A inherits; B is neither.
        state = model(component("A", satisfies=["R1@1"]), component("B"), requirements=[requirement("R1", asil="C")])
        assert measure_coverage(state).asil == AsilCoverage(2, 0, 1, ("B",))

    def test_qm_declared_is_declared(self) -> None:
        assert measure_coverage(model(component("A", asil="QM"))).asil == AsilCoverage(1, 1, 0, ())

    def test_qm_of_d_inherits_as_qm_so_it_is_neither(self) -> None:
        # The effective level is QM(D), whose rating is QM: not above QM, so not inherited.
        state = model(component("A", satisfies=["R1@1"]), requirements=[requirement("R1", asil="QM(D)")])
        assert measure_coverage(state).asil == AsilCoverage(1, 0, 0, ("A",))

    def test_a_stub_is_outside_the_population(self) -> None:
        state = model(component("A", asil="A"), component("Ext", external=True))
        assert measure_coverage(state).asil == AsilCoverage(1, 1, 0, ())

    @pytest.mark.parametrize(
        ("first", "last", "expected"),
        [
            # One rule for a repeated name: the last declared file is the component, the one effective_level
            # rates too. It states none, so no declared component states a level; the first file is read by none.
            ({"asil": "D"}, {}, NotAvailable("no declared component or requirement states a safety level")),
            # The last declares B: declared, never "inherited".
            ({}, {"asil": "B"}, AsilCoverage(1, 1, 0, ())),
        ],
    )
    def test_a_name_two_files_share_is_declared_and_rated_by_one_file(
        self, first: dict[str, str], last: dict[str, str], expected: AsilCoverage | NotAvailable
    ) -> None:
        state = model(component("A", **first), component("A", **last))
        assert measure_coverage(state).asil == expected

    def test_a_level_in_the_middle_file_of_three_is_read_by_no_figure(self) -> None:
        # None, D, None: the last file is the component and states none; the middle file is read by no figure.
        state = model(component("A"), component("A", asil="D"), component("A"))
        assert measure_coverage(state).asil == NotAvailable(
            "no declared component or requirement states a safety level"
        )

    @pytest.mark.parametrize(
        ("files", "expected"),
        [
            # A declared file wins over a stub of its name wherever the stub stands: the name is a declared
            # component in the shares, read off its last declared file.
            ((("D", False), (None, True)), AsilCoverage(1, 1, 0, ())),
            ((("D", False), (None, True), (None, True)), AsilCoverage(1, 1, 0, ())),
            (((None, True), ("D", False)), AsilCoverage(1, 1, 0, ())),
            (((None, True), (None, False), ("D", False)), AsilCoverage(1, 1, 0, ())),
            # A stub's level is never a statement, first, last or between declared files that state none.
            ((("D", True), (None, False)), NotAvailable("no declared component or requirement states a safety level")),
            (((None, False), ("D", True)), NotAvailable("no declared component or requirement states a safety level")),
        ],
    )
    def test_a_declared_files_level_is_seen_whatever_stub_shares_its_name(
        self, files: tuple[tuple[str | None, bool], ...], expected: AsilCoverage | NotAvailable
    ) -> None:
        state = model(*(component("A", external=external, asil=level) for level, external in files))
        assert measure_coverage(state).asil == expected

    def test_a_stubs_level_never_makes_a_declared_component_of_its_name_declare_one(self) -> None:
        # The stub first states D, the declared A none, and an unrelated requirement's level makes the
        # coverage a number: A neither declares nor inherits a level, whichever file is first.
        for files in ((("D", True), (None, False)), ((None, False), ("D", True))):
            state = model(
                *(component("A", external=external, asil=level) for level, external in files),
                requirements=[requirement("R1", asil="B")],
            )
            assert measure_coverage(state).asil == AsilCoverage(1, 0, 0, ("A",))

    def test_a_stubs_level_under_a_declared_name_states_none(self) -> None:
        state = model(component("A"), component("A", external=True, asil="D"), component("A"))
        assert measure_coverage(state).asil == NotAvailable(
            "no declared component or requirement states a safety level"
        )


class TestRunnableCoverage:
    def test_no_runnable_is_not_available(self) -> None:
        state = model(component("A"), component("Ext", periodic("r"), external=True))
        assert measure_coverage(state).runnable == NotAvailable("no component declares a runnable")

    def test_no_component_is_not_available(self) -> None:
        assert measure_coverage(model()).runnable == NotAvailable("no component declares a runnable")

    def test_the_share_names_what_has_none(self) -> None:
        state = model(component("A", periodic("r")), component("C"), component("B"))
        assert measure_coverage(state).runnable == RunnableCoverage(3, 1, ("B", "C"))


class TestTaskCoverage:
    def test_no_task_is_not_available(self) -> None:
        assert measure_coverage(model(component("A", periodic("r")))).task == NotAvailable("the model has no task")

    def test_no_runnable_is_not_available(self) -> None:
        state = model(component("A"), tasks=[task("T")])
        assert measure_coverage(state).task == NotAvailable("no component declares a runnable")

    def test_a_swc_entry_covers_and_the_rest_are_named(self) -> None:
        # Of A.r1, A.r2 (initialization) and B.r0 only A.r1 is on a task: 1 of 3.
        state = model(
            component("A", periodic("r1"), initial("r2")),
            component("B", {"name": "r0"}),
            tasks=[task("T", {"swc": "A", "runnable": "r1"})],
        )
        assert measure_coverage(state).task == TaskCoverage(3, 1, 0, ("A.r2", "B.r0"))

    def test_zero_on_a_task_is_a_figure_on_a_model_that_has_tasks(self) -> None:
        state = model(component("A", periodic("r")), tasks=[task("T")])
        assert measure_coverage(state).task == TaskCoverage(1, 0, 0, ("A.r",))

    def test_an_instance_entry_covers_the_runnable_for_its_whole_type(self) -> None:
        # A is placed twice; one entry through `a2` marks A.r covered.
        state = model(
            component("A", periodic("r")),
            composition("Root", root=True, members=(("a1", "A"), ("a2", "A"))),
            tasks=[task("T", {"instance": "a2", "runnable": "r"})],
        )
        assert measure_coverage(state).task == TaskCoverage(1, 1, 0, ())

    def test_a_server_runnable_on_no_task_is_apart(self) -> None:
        # Two runnables, one a server: the figure is 1 of 1, and the server is no gap.
        state = model(
            component("A", periodic("r"), server("s")),
            tasks=[task("T", {"swc": "A", "runnable": "r"})],
        )
        assert measure_coverage(state).task == TaskCoverage(1, 1, 1, ())

    @pytest.mark.parametrize(
        "entry",
        [
            {"instance": "nobody", "runnable": "r"},
            {"swc": "Nobody", "runnable": "r"},
            {"swc": "A", "runnable": "nothing"},
        ],
    )
    def test_an_entry_that_resolves_to_no_runnable_counts_none(self, entry: dict[str, str]) -> None:
        state = model(
            component("A", periodic("r")),
            composition("Root", root=True, members=(("a", "A"),)),
            tasks=[task("T", entry)],
        )
        assert measure_coverage(state).task == TaskCoverage(1, 0, 0, ("A.r",))

    def test_a_stubs_runnables_are_outside(self) -> None:
        state = model(
            component("A", periodic("r")),
            component("Ext", periodic("x"), external=True),
            tasks=[task("T", {"swc": "A", "runnable": "r"})],
        )
        assert measure_coverage(state).task == TaskCoverage(1, 1, 0, ())

    def test_a_data_received_runnable_on_no_task_is_listed(self) -> None:
        # Unlike a server, a data-received runnable needs a task, so it is counted.
        state = model(
            component("A", periodic("r"), data_started("d")),
            tasks=[task("T", {"swc": "A", "runnable": "r"})],
        )
        assert measure_coverage(state).task == TaskCoverage(2, 1, 0, ("A.d",))

    def test_a_data_received_runnable_on_a_task_is_covered(self) -> None:
        state = model(
            component("A", periodic("r"), data_started("d")),
            tasks=[task("T", {"swc": "A", "runnable": "r"}, {"swc": "A", "runnable": "d"})],
        )
        assert measure_coverage(state).task == TaskCoverage(2, 2, 0, ())

    def test_a_call_returned_runnable_on_no_task_is_listed(self) -> None:
        # Nobody calls it, so it needs a task.
        state = model(
            component("A", periodic("r"), call_returned("b")),
            tasks=[task("T", {"swc": "A", "runnable": "r"})],
        )
        assert measure_coverage(state).task == TaskCoverage(2, 1, 0, ("A.b",))

    def test_a_call_returned_runnable_on_a_task_is_covered(self) -> None:
        state = model(
            component("A", periodic("r"), call_returned("b")),
            tasks=[task("T", {"swc": "A", "runnable": "r"}, {"swc": "A", "runnable": "b"})],
        )
        assert measure_coverage(state).task == TaskCoverage(2, 2, 0, ())


class TestRequirementCoverage:
    def test_no_requirement_is_not_available(self) -> None:
        assert measure_coverage(model(component("A"))).requirement == NotAvailable("the model declares no requirement")

    def test_allocated_and_linked_with_what_is_missing_named(self) -> None:
        # R1 is satisfied by A, R2 by nothing: 1 of 2. A is linked, B is not: 1 of 2.
        state = model(
            component("A", satisfies=["R1@1"]),
            component("B"),
            requirements=[requirement("R1"), requirement("R2")],
        )
        assert measure_coverage(state).requirement == RequirementCoverage(2, 1, 0, ("R2",), 2, 1, ("B",))

    def test_a_stub_requirement_is_apart(self) -> None:
        # R0 is a stub: allocation has no answer for it, so 1 of 1 with one apart.
        state = model(
            component("A", satisfies=["R1@1"]),
            requirements=[requirement("R1"), requirement("R0", stub=True)],
        )
        assert measure_coverage(state).requirement == RequirementCoverage(1, 1, 1, (), 1, 1, ())

    def test_a_link_on_a_member_counts_for_its_component(self) -> None:
        # A's link sits on a runnable, B's on a port; both components are linked, and R1 allocated.
        state = model(
            component("A", periodic("r", satisfies=["R1@1"])),
            component("B", ports=[{"name": "pX", "direction": "provide", "interface": "I", "satisfies": ["R2@1"]}]),
            requirements=[requirement("R1"), requirement("R2")],
        )
        assert measure_coverage(state).requirement == RequirementCoverage(2, 2, 0, (), 2, 2, ())

    def test_a_link_to_an_undeclared_requirement_counts_for_no_component(self) -> None:
        state = model(component("A", satisfies=["GHOST@1"]), requirements=[requirement("R1")])
        assert measure_coverage(state).requirement == RequirementCoverage(1, 0, 0, ("R1",), 1, 0, ("A",))

    def test_a_link_to_a_stub_requirement_links_its_component(self) -> None:
        # A stub requirement is declared, so A's link to it counts; allocation has no answer for R0.
        state = model(
            component("A", satisfies=["R0@1"]),
            component("B"),
            requirements=[requirement("R1"), requirement("R0", stub=True)],
        )
        assert measure_coverage(state).requirement == RequirementCoverage(1, 0, 1, ("R1",), 2, 1, ("B",))

    def test_a_stub_components_link_allocates_and_links_no_declared_component(self) -> None:
        # The stub's link allocates R1, the rule's own reading, and adds nothing to the components' figure.
        state = model(
            component("A"),
            component("Ext", external=True, satisfies=["R1@1"]),
            requirements=[requirement("R1")],
        )
        assert measure_coverage(state).requirement == RequirementCoverage(1, 1, 0, (), 1, 0, ("A",))

    def test_a_stub_components_link_to_a_stub_requirement_allocates_nothing(self) -> None:
        # R0 is a placeholder: the stub's link to it leaves allocation without an answer, so no
        # requirement is allocated and R0 is counted apart, as the External stub definition says.
        state = model(
            component("A"),
            component("Ext", external=True, satisfies=["R0@1"]),
            requirements=[requirement("R1"), requirement("R0", stub=True)],
        )
        assert measure_coverage(state).requirement == RequirementCoverage(1, 0, 1, ("R1",), 1, 0, ("A",))

    def test_a_stub_components_link_to_an_id_a_placeholder_shares_allocates_nothing(self) -> None:
        # A real R1 and a placeholder R1 share the id: allocation is answered per id, and an id a stub
        # requirement holds has no answer, so the stub's link allocates nothing, as the External stub
        # definition says ("a declared requirement id no stub requirement holds").
        state = model(
            component("A"),
            component("Ext", external=True, satisfies=["R1@1"]),
            requirements=[requirement("R1"), requirement("R1", stub=True)],
        )
        assert measure_coverage(state).requirement == RequirementCoverage(0, 0, 1, (), 1, 0, ("A",))


def test_a_fresh_shape_is_not_available_in_every_coverage_and_never_zero() -> None:
    coverage = measure_coverage(model(component("A")))
    assert all(isinstance(value, NotAvailable) for value in vars(coverage).values())


def accessing(name: str, *ports: str, kind: str = "reads") -> dict[str, Any]:
    """A runnable with one access per port in *kind*.

    *kind* is ``reads``, ``writes``, ``calls``, ``operation``, ``data`` or ``call_returned``; with
    ``reads_variables`` or ``writes_variables`` the names are inter-runnable variables.
    """
    if kind == "operation":
        return {"name": name, "events": [{"type": "operation", "port": port, "operation": "Op"} for port in ports]}
    if kind == "data":
        return {"name": name, "events": [{"type": "data", "port": port, "element": "d"} for port in ports]}
    if kind == "call_returned":
        return {"name": name, "events": [{"type": "call_returned", "port": port, "operation": "Op"} for port in ports]}
    if kind in ("reads_variables", "writes_variables"):
        return {"name": name, kind: [{"variable": variable} for variable in ports]}
    member = "operation" if kind == "calls" else "element"
    return {"name": name, kind: [{"port": port, member: "d"} for port in ports]}


#: The one-item reason: the one item may be a port or a variable.
ONE_REASON = "one port or variable: every runnable that uses one uses only it"


def cohesion_of(*runnables: dict[str, Any]) -> tuple[Fraction | NotAvailable, int]:
    return component_cohesion(component("C", *runnables))


def cohesion_block(*components: SWComponent) -> Cohesion | NotAvailable:
    state = model(*components)
    rows = measure_coupling(state)[0]
    return measure_cohesion(rows, sum(len(each.runnables) for each in components if not each.external))


class TestComponentCohesion:
    """The cohesion definition, case by case; each figure is the shared pairs over all pairs of measured runnables."""

    def test_no_runnable_is_not_available(self) -> None:
        assert cohesion_of() == (NotAvailable("no runnable"), 0)

    def test_no_runnable_declaring_an_access_is_not_available(self) -> None:
        assert cohesion_of(periodic("a"), initial("b")) == (NotAvailable("no runnable declares an access"), 0)

    def test_exactly_one_runnable_measured_is_one(self) -> None:
        assert cohesion_of(accessing("a", "rX", "rY")) == (Fraction(1), 1)

    def test_several_runnables_one_measured_is_not_available(self) -> None:
        # Five runnables, one declaring an access: a 1.0 here would have nothing behind it.
        answer = cohesion_of(accessing("a", "rX"), *(periodic(f"p{index}") for index in range(4)))
        assert answer == (NotAvailable("only one runnable declares an access"), 1)

    def test_measured_runnables_on_one_port_are_not_available(self) -> None:
        assert cohesion_of(accessing("a", "rX"), accessing("b", "rX"), accessing("c", "rX")) == (
            NotAvailable(ONE_REASON),
            3,
        )

    def test_one_port_leaves_out_a_runnable_that_uses_no_port(self) -> None:
        # Two runnables on rX and an initialization runnable on none: the reason speaks of the runnables
        # that use a port, as the renderer's words do.
        answer = cohesion_of(accessing("a", "rX"), accessing("b", "rX"), initial("init"))
        assert answer == (NotAvailable(ONE_REASON), 2)

    def test_one_port_reads_the_ports_used_not_the_ports_declared(self) -> None:
        ports = [{"name": f"r{index}", "direction": R, "interface": "I"} for index in range(5)]
        swc_ = component("C", accessing("a", "r0"), accessing("b", "r0"), ports=ports)
        assert component_cohesion(swc_) == (NotAvailable(ONE_REASON), 2)

    def test_otherwise_the_shared_pairs_over_all_pairs(self) -> None:
        # a {rX, rY}, b {rY}, c {rZ}: pairs ab share rY, ac and bc share nothing: 1 of 3.
        answer = cohesion_of(accessing("a", "rX", "rY"), accessing("b", "rY"), accessing("c", "rZ"))
        assert answer == (Fraction(1, 3), 3)

    def test_two_runnables_sharing_nothing_give_zero(self) -> None:
        assert cohesion_of(accessing("a", "rX"), accessing("b", "rY")) == (Fraction(0), 2)

    def test_all_runnables_sharing_one_of_several_ports_give_one(self) -> None:
        # a {rX, rY}, b {rX}, c {rX, rZ}: the union is three ports, and every pair shares rX: 3 of 3.
        answer = cohesion_of(accessing("a", "rX", "rY"), accessing("b", "rX"), accessing("c", "rX", "rZ"))
        assert answer == (Fraction(1), 3)

    def test_an_init_runnable_with_no_access_is_left_out_and_counted(self) -> None:
        # init says nothing: the pair a-b shares rX, so 1 of 1, and two runnables are measured, not three.
        answer = cohesion_of(initial("init"), accessing("a", "rX"), accessing("b", "rX", "rY"))
        assert answer == (Fraction(1), 2)

    def test_a_server_runnable_shares_its_provide_port_with_a_reader_of_nothing_else(self) -> None:
        # srv is started on pS; rd touches pS alone; other touches rZ. Pairs: srv-rd share pS,
        # srv-other and rd-other share nothing: 1 of 3. Without the event, srv would be unmeasured and
        # rd-other would give 0.
        answer = cohesion_of(server("srv"), accessing("rd", "pS"), accessing("other", "rZ"))
        assert answer == (Fraction(1, 3), 3)

    @pytest.mark.parametrize("kind", ["reads", "writes", "calls", "operation", "data", "call_returned"])
    def test_every_list_and_the_starting_events_contribute(self, kind: str) -> None:
        # x touches q through *kind*; y touches q and z; w touches z. Pairs xy and yw share, xw does not:
        # 2 of 3. Were *kind* not read, x would be unmeasured and y-w alone would give 1.
        answer = cohesion_of(accessing("x", "q", kind=kind), accessing("y", "q", "z"), accessing("w", "z"))
        assert answer == (Fraction(2, 3), 3)

    def test_a_runnable_started_by_a_port_it_does_not_read_counts_that_port(self) -> None:
        # d is started on rD and reads nothing; rd reads rD; other reads rZ.
        # Pairs d-rd share rD, the others nothing: 1 of 3. Without the event, d would be unmeasured and rd-other
        # would give 0.
        answer = cohesion_of(data_started("d"), accessing("rd", "rD"), accessing("other", "rZ"))
        assert answer == (Fraction(1, 3), 3)

    def test_a_runnable_that_also_reads_its_starting_element_scores_as_without_the_event(self) -> None:
        # The usual case: the started runnable reads the element too, so the port was already in its touched set.
        started = {**accessing("d", "rD"), **data_started("d")}
        assert cohesion_of(started, accessing("b", "rD", "rX")) == cohesion_of(
            accessing("d", "rD"), accessing("b", "rD", "rX")
        )

    def test_a_runnable_started_by_a_call_returning_counts_that_port(self) -> None:
        # b is started on rC when its call returns and declares nothing else;
        # rd reads rC; other reads rZ. Pairs b-rd share rC, the others nothing: 1 of 3. Without the event, b would
        # be unmeasured and rd-other would give 0.
        answer = cohesion_of(call_returned("b"), accessing("rd", "rC"), accessing("other", "rZ"))
        assert answer == (Fraction(1, 3), 3)

    def test_two_runnables_sharing_only_the_call_returned_events_port_share(self) -> None:
        # The caller calls rC and reads rX; the callback is started on rC and writes rY. Their one item in common
        # is the event's port: 1 of 1. Passing the event over would give 0.
        caller = {
            "name": "caller",
            "calls": [{"port": "rC", "operation": "Op"}],
            "reads": [{"port": "rX", "element": "d"}],
        }
        back = {**call_returned("back"), "writes": [{"port": "rY", "element": "d"}]}
        assert cohesion_of(caller, back) == (Fraction(1), 2)

    def test_two_runnables_sharing_only_a_variable_share(self) -> None:
        # a reads rX and writes V; b reads rY and reads V. No port in common, the variable V in common:
        # 1 of 1. Counting ports alone would give 0.
        a = {"name": "a", "reads": [{"port": "rX", "element": "d"}], "writes_variables": [{"variable": "V"}]}
        b = {"name": "b", "reads": [{"port": "rY", "element": "d"}], "reads_variables": [{"variable": "V"}]}
        assert cohesion_of(a, b) == (Fraction(1), 2)

    def test_a_port_and_a_variable_of_one_name_are_not_shared(self) -> None:
        # a reads the port Speed, b reads the variable Speed: two items of one name, nothing in common: 0 of 1,
        # and the union is two items, so the one-item reason does not apply either.
        assert cohesion_of(accessing("a", "Speed"), accessing("b", "Speed", kind="reads_variables")) == (Fraction(0), 2)

    @pytest.mark.parametrize("kind", ["reads_variables", "writes_variables"])
    def test_every_variable_list_contributes(self, kind: str) -> None:
        # x touches the variable q through *kind*; y reads q and writes z; w writes z. Pairs xy and yw share,
        # xw does not: 2 of 3. Were *kind* not read, x would be unmeasured and y-w alone would give 1.
        y = {"name": "y", "reads_variables": [{"variable": "q"}], "writes_variables": [{"variable": "z"}]}
        answer = cohesion_of(accessing("x", "q", kind=kind), y, accessing("w", "z", kind="writes_variables"))
        assert answer == (Fraction(2, 3), 3)

    def test_a_runnable_with_variable_accesses_only_is_measured(self) -> None:
        # The component's only runnable writes one variable: measured, so 1, never "no runnable declares an access".
        assert cohesion_of(accessing("a", "V", kind="writes_variables")) == (Fraction(1), 1)

    def test_measured_runnables_on_one_variable_are_not_available(self) -> None:
        # The writer and the reader of V touch V alone: the figure would be 1 by construction.
        answer = cohesion_of(accessing("w", "V", kind="writes_variables"), accessing("r", "V", kind="reads_variables"))
        assert answer == (NotAvailable(ONE_REASON), 2)

    def test_the_shared_fixture_scores_by_its_variables(self) -> None:
        # SpeedEstimator: FilterSpeed writes FilteredSpeed, LogSpeed reads it, MonitorSpeed reads it and reads and
        # writes PeakSpeed. Every pair shares FilteredSpeed: 3 of 3, over two items, so a figure. No port is used.
        state = load_model_state(
            REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "inter_runnable_variables" / "model"
        )
        entry = row(state, "SpeedEstimator")
        assert (entry.cohesion, entry.measured_runnables) == (Fraction(1), 3)

    def test_the_row_carries_the_figure_and_the_measured_count(self) -> None:
        state = model(component("C", initial("init"), accessing("a", "rX"), accessing("b", "rY")))
        entry = row(state, "C")
        assert (entry.cohesion, entry.measured_runnables) == (Fraction(0), 2)


class TestCohesionBlock:
    def test_no_component_declaring_an_access_is_not_available(self) -> None:
        answer = cohesion_block(component("A", periodic("a")), component("B"))
        assert answer == NotAvailable("no component's runnables declare an access")

    def test_no_component_is_not_available(self) -> None:
        assert cohesion_block() == NotAvailable("no component's runnables declare an access")

    def test_accesses_that_give_no_component_a_figure_are_not_available(self) -> None:
        # A has one measured runnable of two, B two runnables on one port: neither has a figure.
        answer = cohesion_block(
            component("A", accessing("a", "rX"), periodic("p")),
            component("B", accessing("a", "rX"), accessing("b", "rX")),
        )
        assert answer == NotAvailable("no component's runnables give a figure")

    def test_the_aggregates(self) -> None:
        # A {rX, rY} {rY} {rZ}: 1 of 3. B {rX} {rY}: 0 of 1. C {rX} {rX} {rX, rY} {rZ}: the pairs among the
        # first three share rX and the three with {rZ} share nothing, 3 of 6 = 1/2, exactly the threshold
        # and not below. D one runnable: 1. E no runnable, F no access, G one port, H one measured of two.
        # Mean over A, B, C, D: (1/3 + 0 + 1/2 + 1) / 4 = (2/6 + 3/6 + 6/6) / 4 = 11/24.
        # Below 0.5, lowest first: B (0), A (1/3). The stub S is out of every figure.
        # Runnables: A 3, B 2, C 4, D 1, E 0, F 1, G 2, H 2 = 15. Used, the figured ones': 3 + 2 + 4 + 1 = 10.
        answer = cohesion_block(
            component("A", accessing("a", "rX", "rY"), accessing("b", "rY"), accessing("c", "rZ")),
            component("B", accessing("a", "rX"), accessing("b", "rY")),
            component(
                "C", accessing("a", "rX"), accessing("b", "rX"), accessing("c", "rX", "rY"), accessing("d", "rZ")
            ),
            component("D", accessing("a", "rX")),
            component("E"),
            component("F", periodic("p")),
            component("G", accessing("a", "rX"), accessing("b", "rX")),
            component("H", accessing("a", "rX"), periodic("p")),
            component("S", accessing("a", "rX"), accessing("b", "rY"), external=True),
        )
        assert isinstance(answer, Cohesion)
        assert answer.components == 4
        assert answer.mean == Fraction(11, 24)
        assert answer.below == (("B", Fraction(0)), ("A", Fraction(1, 3)))
        assert answer.not_available == (
            ("no runnable", 1),
            ("no runnable declares an access", 1),
            ("only one runnable declares an access", 1),
            (ONE_REASON, 1),
        )
        assert (answer.runnables_used, answer.runnables) == (10, 15)

    def test_below_ties_go_by_name_and_reasons_without_a_component_are_left_out(self) -> None:
        answer = cohesion_block(
            component("Zed", accessing("a", "rX"), accessing("b", "rY")),
            component("Abe", accessing("a", "rX"), accessing("b", "rY")),
            component("Mid", accessing("a", "rX")),
            component("Off"),
        )
        assert isinstance(answer, Cohesion)
        assert answer.below == (("Abe", Fraction(0)), ("Zed", Fraction(0)))
        assert answer.not_available == (("no runnable", 1),)

    def test_a_stub_is_outside_the_population(self) -> None:
        answer = cohesion_block(
            component("A", accessing("a", "rX")),
            component("S", accessing("a", "rX"), accessing("b", "rY"), external=True),
        )
        assert isinstance(answer, Cohesion)
        assert (answer.components, answer.mean, answer.below) == (1, Fraction(1), ())

    def test_measure_carries_the_block(self, tmp_path: Path) -> None:
        state = model(component("A", accessing("a", "rX"), accessing("b", "rY")), component("S", external=True))
        side = measure(state, str(tmp_path), "working tree", current_registry())
        assert isinstance(side.cohesion, Cohesion)
        assert (side.cohesion.mean, side.cohesion.runnables, side.cohesion.runnables_used) == (Fraction(0), 2, 2)


# ---------------------------------------------------------------------------
# What to look at: the four facts of Ct = 0, and the lines
# ---------------------------------------------------------------------------


class TestNoLinkWords:
    def test_no_port(self) -> None:
        assert row(model(swc("A")), "A").no_link == NO_PORT

    def test_an_unconnected_port_is_incomplete(self) -> None:
        assert row(model(swc("A", "pX")), "A").no_link == INCOMPLETE

    def test_a_discarded_self_link_is_connected_with_no_measured_dependency(self) -> None:
        # One type placed twice, wired to itself: the pair is a self-link, both ports connected.
        state = model(
            swc("T", "pOut", "rIn"),
            composition("Root", root=True, members=(("t1", "T"), ("t2", "T"))),
            connector("c", "T.pOut", "T.rIn"),
        )
        assert (row(state, "T").ct, row(state, "T").no_link) == (0, CONNECTED_UNMEASURED)

    def test_a_pair_of_one_direction_is_connected_with_no_measured_dependency(self) -> None:
        state = model(swc("X", "pA"), swc("Y", "pB"), connector("c", "X.pA", "Y.pB"))
        assert (row(state, "X").no_link, row(state, "Y").no_link) == (CONNECTED_UNMEASURED, CONNECTED_UNMEASURED)

    def test_a_port_delegated_to_an_unwired_boundary_port_reaches_no_one(self) -> None:
        state = model(
            swc("A", "pA"),
            composition("Box", root=True, members=(("a", "A"),), ports=("pB",), delegations=(("pB", "a", "pA"),)),
        )
        assert (row(state, "A").unconnected_ports, row(state, "A").no_link) == (0, DELEGATED_TO_NO_ONE)

    def test_a_linked_component_has_no_word(self) -> None:
        state = model(swc("A", "pX"), swc("B", "rX", "pY"), connector("c", "A.pX", "B.rX"))
        # B's pY is unconnected, yet B has a link: the word is for Ct = 0 alone.
        assert (row(state, "A").no_link, row(state, "B").no_link) == (None, None)


def side_of(state: ModelState, tmp_path: Path) -> ArchitectureState:
    """*state* measured, with no finding: ``check-model`` reads an empty directory and has its own."""
    side = measure(state, str(tmp_path), "working tree", current_registry())
    return dataclasses.replace(side, integrity=measure_integrity(state, ()), findings=())


class TestLookAt:
    def test_a_line_is_present_only_with_content(self, tmp_path: Path) -> None:
        # A to B is one link; both Ct 1, no port is unconnected, no task, no cohesion figure.
        state = model(swc("A", "pX"), swc("B", "rX"), connector("c", "A.pX", "B.rX"))
        assert look_at(side_of(state, tmp_path), None) == ()

    def test_hubs_no_link_and_low_cohesion_in_the_fixed_order(self, tmp_path: Path) -> None:
        # Hub provides five ports, each wired to one reader: Hub Ct 5, R0..R4 Ct 1, Z0..Z3 Ct 0;
        # n = 10, mu = 1, variance (16 + 4) / 10 = 2, (5 - 1)^2 = 16 > 8: Hub is the one hub.
        # mu^2 = 1 < 8: isolated is n/a, so its line is absent though four components have Ct 0.
        readers = [swc(f"R{index}", f"r{index}") for index in range(5)]
        hub = component(
            "Hub",
            accessing("a", "p0"),
            accessing("b", "p1"),
            ports=[{"name": f"p{i}", "direction": P, "interface": "I"} for i in range(5)],
        )
        state = model(
            hub,
            *readers,
            *fillers(4),
            *[connector(f"c{index}", f"Hub.p{index}", f"R{index}.r{index}") for index in range(5)],
        )
        lines = look_at(side_of(state, tmp_path), None)
        assert [line.key for line in lines] == [LOOK_HUBS, LOOK_NO_LINK, LOOK_LOW_COHESION]
        assert lines[0] == LookAtLine(LOOK_HUBS, (LookAtEntry("Hub", 5),), 0)
        assert lines[1].entries == tuple(LookAtEntry(f"Z{index}", word=NO_PORT) for index in range(4))
        # Hub's two runnables touch p0 and p1, sharing nothing: 0 of 1 pairs.
        assert lines[2] == LookAtLine(LOOK_LOW_COHESION, (LookAtEntry("Hub", Fraction(0)),), 0)

    def test_a_list_stops_at_ten_and_counts_the_rest(self, tmp_path: Path) -> None:
        state = model(*[swc(f"A{index:02}", "pX") for index in range(13)])
        (line,) = look_at(side_of(state, tmp_path), None)
        assert line.key == LOOK_NO_LINK
        assert [entry.name for entry in line.entries] == [f"A{index:02}" for index in range(10)]
        assert line.more == 3
        assert line.entries[0] == LookAtEntry("A00", word=INCOMPLETE, unconnected=1)

    def test_unscheduled_runnables_only_when_task_coverage_is_a_number(self, tmp_path: Path) -> None:
        a = component("A", periodic("run"), initial("init"), server("serve"))
        assert look_at(side_of(model(a), tmp_path), None)[-1].key == LOOK_NO_LINK
        scheduled = model(a, tasks=[task("T", {"swc": "A", "runnable": "run"})])
        unscheduled = look_at(side_of(scheduled, tmp_path), None)[-1]
        # The server runnable is apart: a direct call is its legal state.
        assert unscheduled == LookAtLine(LOOK_UNSCHEDULED, (LookAtEntry("A.init"),), 0)

    def test_a_call_returned_runnable_on_no_task_is_unscheduled(self, tmp_path: Path) -> None:
        # A call-returned runnable needs a task, as a periodic one does.
        a = component("A", periodic("run"), call_returned("back"))
        scheduled = model(a, tasks=[task("T", {"swc": "A", "runnable": "run"})])
        unscheduled = look_at(side_of(scheduled, tmp_path), None)[-1]
        assert unscheduled == LookAtLine(LOOK_UNSCHEDULED, (LookAtEntry("A.back"),), 0)

    def test_the_keys_are_the_fixed_order(self) -> None:
        assert LOOK_AT_KEYS[:3] == (LOOK_ERRORS, LOOK_HUBS, LOOK_NO_LINK)
        assert LOOK_AT_KEYS[-3:] == (LOOK_NEW_HUBS, LOOK_ROSE, LOOK_INTRODUCED)


# ---------------------------------------------------------------------------
# Revisions and movement, on temporary git repositories
# ---------------------------------------------------------------------------

IEXAMPLE_PORT = "  - name: {name}\n    direction: {direction}\n    interface: IExample\n"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def component_yaml(name: str, *ports: str) -> str:
    """A component; a port is ``p<name>`` for provide and ``r<name>`` for require, each on ``IExample``."""
    text = f"name: {name}\ntype: application\n"
    if ports:
        text += "ports:\n" + "".join(
            IEXAMPLE_PORT.format(name=port, direction=P if port[0] == "p" else R) for port in ports
        )
    return text


def connector_yaml(name: str, from_: str, to: str) -> str:
    (from_swc, from_port), (to_swc, to_port) = from_.split("."), to.split(".")
    return f"name: {name}\nfrom:\n  swc: {from_swc}\n  port: {from_port}\nto:\n  swc: {to_swc}\n  port: {to_port}\n"


def bad_connector(name: str) -> str:
    """A connector from a port ExampleSWC does not have, named after the connector."""
    return connector_yaml(name, f"ExampleSWC.pNo{name}", "ExampleConsumerSWC.rIn")


def requirement_yaml(identifier: str) -> str:
    return f"id: {identifier}\ntitle: t\ndescription: d\nreq_type: req\nversion: 1\nsource: architecture\n"


def basic_project(directory: Path) -> Path:
    """A copy of ``examples/basic``: ExampleSWC provides to ExampleConsumerSWC, so each has Ct 1."""
    project = directory / "project"
    shutil.copytree(REPO_ROOT / "examples" / "basic", project, ignore=shutil.ignore_patterns("dist", "views"))
    return project


def bare_project(directory: Path) -> Path:
    """The basic manifest and its one interface, and no other element."""
    project = directory / "project"
    write(project / "swcraft.yaml", (REPO_ROOT / "examples" / "basic" / "swcraft.yaml").read_text(encoding="utf-8"))
    interface = REPO_ROOT / "examples" / "basic" / "model" / "interfaces" / "IExample.yaml"
    write(project / "model" / "interfaces" / "IExample.yaml", interface.read_text(encoding="utf-8"))
    return project


def commit(project: Path, message: str = "a commit") -> None:
    if not (project / ".git").exists():
        _git("init", "-q", cwd=project)
    _git("add", "-A", cwd=project)
    _git("commit", "-q", "-m", message, cwd=project)


def short_sha(project: Path, revision: str = "HEAD") -> str:
    return _git("rev-parse", "--short", revision, cwd=project).stdout.strip()


def metrics_of(project: Path, *since: str, since_head: bool = False) -> MetricsReport:
    return compute_metrics(require_project(str(project / "swcraft.yaml")), since, since_head=since_head)


def figure(movement: Movement, name: str) -> tuple[object, ...]:
    entry = next(entry for entry in movement.figures if entry.figure == name)
    return entry.values, entry.change, entry.not_available_at


def no_location(text: str, project: Path) -> bool:
    places = {tempfile.gettempdir(), os.path.realpath(tempfile.gettempdir()), str(project), project.as_posix()}
    return not any(place in text for place in places)


class TestSeries:
    def test_without_a_base_there_is_the_working_tree_alone(self, tmp_path: Path) -> None:
        report = metrics_of(basic_project(tmp_path))
        assert [side.label for side in report.series] == ["working tree"]
        assert (report.movement, report.no_base, report.project_name) == (None, None, "swcraft-basic-example")

    def test_one_since_is_the_base(self, tmp_path: Path) -> None:
        project = basic_project(tmp_path)
        commit(project)
        report = metrics_of(project, "HEAD")
        assert [side.label for side in report.series] == [f"HEAD ({short_sha(project)})", "working tree"]
        assert report.movement is not None
        assert (report.movement.base, report.movement.current) == (f"HEAD ({short_sha(project)})", "working tree")

    def test_two_since_are_three_columns_the_first_named_the_base(self, tmp_path: Path) -> None:
        # Three commits: the basic model (2 components), Extra added (3), Other added (4); then the
        # working tree adds Last (5). Named HEAD~1 then HEAD~2: the base is HEAD~1, as named, not the older.
        project = basic_project(tmp_path)
        swc_dir = project / "model" / "swc" / "example"
        commit(project)
        write(swc_dir / "Extra.yaml", component_yaml("Extra"))
        commit(project)
        write(swc_dir / "Other.yaml", component_yaml("Other"))
        commit(project)
        write(swc_dir / "Last.yaml", component_yaml("Last"))
        report = metrics_of(project, "HEAD~1", "HEAD~2")
        assert [side.label for side in report.series] == [
            f"HEAD~1 ({short_sha(project, 'HEAD~1')})",
            f"HEAD~2 ({short_sha(project, 'HEAD~2')})",
            "working tree",
        ]
        assert report.movement is not None
        assert report.movement.base == f"HEAD~1 ({short_sha(project, 'HEAD~1')})"
        # Columns: HEAD~1 holds 3, HEAD~2 holds 2, the working tree 5; the change is base to working tree.
        assert figure(report.movement, "Components") == ((3, 2, 5), 2, ())
        assert report.movement.added == ("Last", "Other")

    def test_an_added_and_a_removed_component(self, tmp_path: Path) -> None:
        # Base: the basic model and Old (no port). Working tree: Old removed, New added with a require
        # port ExampleSWC provides to. ExampleSWC: Ct 1 -> 2; New: none -> 1; Old: 0 -> none, no move.
        project = basic_project(tmp_path)
        swc_dir = project / "model" / "swc" / "example"
        write(swc_dir / "Old.yaml", component_yaml("Old"))
        commit(project)
        (swc_dir / "Old.yaml").unlink()
        write(swc_dir / "New.yaml", component_yaml("New", "rNew"))
        write(project / "model" / "connectors" / "ToNew.yaml", connector_yaml("ToNew", "ExampleSWC.pOut", "New.rNew"))
        movement = metrics_of(project, "HEAD").movement
        assert movement is not None
        assert (movement.added, movement.removed) == (("New",), ("Old",))
        assert movement.ct_changes == (CtChange("ExampleSWC", 1, 2), CtChange("New", None, 1))
        # Dependencies: 1 at the base (consumer on ExampleSWC), 2 now (New on ExampleSWC too): one
        # connector added, one dependency. The Ct moves sum to twice it, each link counted at both ends.
        assert figure(movement, "Dependencies") == ((1, 2), 1, ())
        assert sum(change.change for change in movement.ct_changes) == 2
        rose = next(line for line in metrics_of(project, "HEAD").look_at if line.key == LOOK_ROSE)
        # Only a component at both sides rose: New is in the added list, not a rise.
        assert rose == LookAtLine(LOOK_ROSE, (LookAtEntry("ExampleSWC", 2, before=1),), 0)

    def test_a_hub_gained(self, tmp_path: Path) -> None:
        # Ten components: Hub, C1..C5, F1..F4. Base: Hub wired to C1 alone, so Hub 1, C1 1, the rest 0:
        # mu = 1/5, variance (2 * 16/25 + 8 * 1/25) / 10 = 4/25, (1 - 1/5)^2 = 16/25 is not above
        # 4 * 4/25: no hub. Working tree: Hub wired to all five: Hub 5, C1..C5 1, F 0: mu = 1,
        # variance (16 + 4) / 10 = 2, (5 - 1)^2 = 16 > 8: Hub is a hub.
        project = bare_project(tmp_path)
        model_root = project / "model"
        write(model_root / "swc" / "Hub.yaml", component_yaml("Hub", *[f"p{index}" for index in range(1, 6)]))
        for index in range(1, 6):
            write(model_root / "swc" / f"C{index}.yaml", component_yaml(f"C{index}", f"r{index}"))
        for index in range(1, 5):
            write(model_root / "swc" / f"F{index}.yaml", component_yaml(f"F{index}"))
        write(model_root / "connectors" / "w1.yaml", connector_yaml("w1", "Hub.p1", "C1.r1"))
        commit(project)
        for index in range(2, 6):
            write(
                model_root / "connectors" / f"w{index}.yaml",
                connector_yaml(f"w{index}", f"Hub.p{index}", f"C{index}.r{index}"),
            )
        report = metrics_of(project, "HEAD")
        movement = report.movement
        assert movement is not None
        assert (movement.hubs_gained, movement.hubs_lost) == (("Hub",), ())
        assert figure(movement, "Hubs") == ((0, 1), 1, ())
        # One dependency per wire: 1 at the base, 5 now.
        assert figure(movement, "Dependencies") == ((1, 5), 4, ())
        # Mean Ct: 1/5 -> 1.
        assert figure(movement, "Mean Ct") == ((Fraction(1, 5), Fraction(1)), Fraction(4, 5), ())
        lines = {line.key: line for line in report.look_at}
        assert lines[LOOK_NEW_HUBS] == LookAtLine(LOOK_NEW_HUBS, (LookAtEntry("Hub", 5),), 0)
        # Hub rose by 4, C2..C5 by 1 each: five rises, the largest first, ties by name.
        assert lines[LOOK_ROSE].entries == (
            LookAtEntry("Hub", 5, before=1),
            *(LookAtEntry(f"C{index}", 1, before=0) for index in range(2, 6)),
        )

    def test_findings_introduced_resolved_and_still_open(self, tmp_path: Path) -> None:
        # A connector to a port that does not exist is one error (each its own port, so no two are a
        # duplicate link); a requirement nothing satisfies is one
        # warning. Base: errors Bad1, Bad2, warnings REQ_A, REQ_B: 4 findings. Working tree: Bad1 and
        # REQ_B gone, Bad3 and REQ_C added. Introduced: Bad3, REQ_C; resolved: Bad1, REQ_B, errors first;
        # still open: 4 - 2 = 2 (Bad2, REQ_A).
        project = basic_project(tmp_path)
        model_root = project / "model"
        for name in ("Bad1", "Bad2"):
            write(model_root / "connectors" / f"{name}.yaml", bad_connector(name))
        for identifier in ("REQ_A", "REQ_B"):
            write(model_root / "requirements" / f"{identifier}.yaml", requirement_yaml(identifier))
        commit(project)
        (model_root / "connectors" / "Bad1.yaml").unlink()
        (model_root / "requirements" / "REQ_B.yaml").unlink()
        write(model_root / "connectors" / "Bad3.yaml", bad_connector("Bad3"))
        write(model_root / "requirements" / "REQ_C.yaml", requirement_yaml("REQ_C"))
        report = metrics_of(project, "HEAD")
        movement = report.movement
        assert movement is not None
        unknown_port, unallocated = "connector.endpoint-unknown-port", "trace.requirement-unallocated"
        assert [(finding.rule, finding.severity) for finding in movement.introduced] == [
            (unknown_port, "error"),
            (unallocated, "warning"),
        ]
        assert "Bad3" in movement.introduced[0].message and "REQ_C" in movement.introduced[1].message
        assert [finding.rule for finding in movement.resolved] == [unknown_port, unallocated]
        assert "Bad1" in movement.resolved[0].message and "REQ_B" in movement.resolved[1].message
        assert (movement.base_findings, movement.still_open) == (4, 2)
        assert figure(movement, "Errors") == ((2, 2), 0, ())
        assert figure(movement, "Warnings") == ((2, 2), 0, ())
        assert not movement.unchanged
        lines = {line.key: line for line in report.look_at}
        assert lines[LOOK_ERRORS] == LookAtLine(LOOK_ERRORS, (LookAtEntry("connector", 2),), 0)
        assert [entry.name for entry in lines[LOOK_INTRODUCED].entries] == [unknown_port, unallocated]

    def test_a_figure_not_available_at_one_side_has_no_change_and_names_the_side(self, tmp_path: Path) -> None:
        # The base holds no task; the working tree adds the basic task, which holds ExampleConsumerRun:
        # 1 of the 2 periodic runnables on a task.
        project = basic_project(tmp_path)
        tasks = project / "model" / "tasks"
        shutil.move(str(tasks), str(tmp_path / "tasks"))
        commit(project)
        shutil.move(str(tmp_path / "tasks"), str(tasks))
        movement = metrics_of(project, "HEAD").movement
        assert movement is not None
        base = f"HEAD ({short_sha(project)})"
        assert figure(movement, "Runnables on a task") == ((NotAvailable("the model has no task"), 1), None, (base,))

    def test_a_readable_head_with_no_change(self, tmp_path: Path) -> None:
        project = basic_project(tmp_path)
        commit(project)
        report = metrics_of(project, since_head=True)
        assert report.no_base is None
        assert report.movement is not None
        assert report.movement.base == f"HEAD ({short_sha(project)})"
        assert report.movement.unchanged

    def test_a_readable_head_with_a_change(self, tmp_path: Path) -> None:
        project = basic_project(tmp_path)
        commit(project)
        write(project / "model" / "swc" / "example" / "Extra.yaml", component_yaml("Extra"))
        report = metrics_of(project, since_head=True)
        assert report.movement is not None
        assert not report.movement.unchanged
        assert report.movement.added == ("Extra",)

    def test_since_and_since_head_together_are_refused(self, tmp_path: Path) -> None:
        project = basic_project(tmp_path)
        commit(project)
        with pytest.raises(ValueError, match="never by both"):
            metrics_of(project, "HEAD", since_head=True)


class TestRefusals:
    def test_an_unknown_revision_is_refused(self, tmp_path: Path) -> None:
        project = basic_project(tmp_path)
        commit(project)
        with pytest.raises(ValueError):
            metrics_of(project, "no-such-revision")

    def test_a_revision_on_a_retired_layout_is_refused(self, tmp_path: Path) -> None:
        project = basic_project(tmp_path)
        write(project / "model" / "mapping_sets" / "SyntheticSet.yaml", "name: SyntheticSet\nmaps: []\n")
        commit(project)
        shutil.rmtree(project / "model" / "mapping_sets")
        with pytest.raises(RetiredStorageError, match="in revision 'HEAD'"):
            metrics_of(project, "HEAD")

    def test_a_revision_whose_element_file_does_not_parse_is_refused(self, tmp_path: Path) -> None:
        project = basic_project(tmp_path)
        task = project / "model" / "tasks" / "Task_10ms.yaml"
        good = task.read_text(encoding="utf-8")
        write(task, "name: [unclosed\n")
        commit(project)
        write(task, good)
        with pytest.raises(RevisionParseError):
            metrics_of(project, "HEAD")

    def test_an_option_shaped_revision_is_refused_before_any_git_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = basic_project(tmp_path)
        commit(project)
        manifest = require_project(str(project / "swcraft.yaml"))
        started: list[object] = []
        real_run = subprocess.run

        def recording(*args: Any, **kwargs: Any) -> Any:
            started.append(args)
            return real_run(*args, **kwargs)

        monkeypatch.setattr(subprocess, "run", recording)
        with pytest.raises(ValueError):
            compute_metrics(manifest, ["-x"])
        assert started == []

    def test_a_working_tree_that_cannot_be_read_refuses_under_since_head(self, tmp_path: Path) -> None:
        project = basic_project(tmp_path)
        commit(project)
        write(project / "model" / "tasks" / "Task_10ms.yaml", "name: [unclosed\n")
        with pytest.raises(ElementParseError):
            metrics_of(project, since_head=True)


@pytest.fixture
def made_trees(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every temporary tree a revision is materialised into, recorded as it is made."""
    made: list[str] = []
    materialize = change_report_module.materialize_revision

    def recording(model_root: str, revision: str) -> Any:
        tmpdir, extracted = materialize(model_root, revision)
        made.append(tmpdir.name)
        return tmpdir, extracted

    monkeypatch.setattr(change_report_module, "materialize_revision", recording)
    return made


class TestTemporaryTrees:
    def test_the_trees_are_gone_after_success_and_after_a_refusal(self, tmp_path: Path, made_trees: list[str]) -> None:
        project = basic_project(tmp_path)
        commit(project)
        metrics_of(project, "HEAD", "HEAD")
        with pytest.raises(ValueError):
            metrics_of(project, "HEAD", "no-such-revision")
        assert len(made_trees) == 3
        assert not any(Path(name).exists() for name in made_trees)

    def test_each_side_is_measured_in_its_own_block_closed_before_its_tree_goes(
        self, tmp_path: Path, made_trees: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The tripwire re-reads, at each block's exit, what the block held. A historical, non-empty
        # model whose tree was already gone would read as a difference and fail this test at teardown.
        assert armed()
        project = basic_project(tmp_path)
        commit(project)
        exits: list[bool] = []
        real = metrics_module.single_read

        @contextlib.contextmanager
        def watched() -> Iterator[None]:
            with real():
                yield
            exits.append(all(Path(name).exists() for name in made_trees))

        monkeypatch.setattr(metrics_module, "single_read", watched)
        report = metrics_of(project, "HEAD")
        # One block per side, the revision's and the working tree's; each closed with the tree on disk.
        assert exits == [True, True]
        assert report.series[0].size.components == 2
        assert not any(Path(name).exists() for name in made_trees)


class TestOneReadPerSide:
    def test_the_findings_read_the_files_the_figures_were_measured_from(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An interface the ports name goes away between the side's read and check-model's, and is
        # back before the block closes (the tripwire compares the held layers with the disk then). Read
        # once, the findings see the interface the figures saw: no error. Read twice, they would not.
        project = basic_project(tmp_path)
        interface = project / "model" / "interfaces" / "IExample.yaml"
        aside = tmp_path / "IExample.yaml"
        real = metrics_module.all_findings

        def saved_meanwhile(root: str, arxml_version: str | None = None) -> Any:
            shutil.move(str(interface), str(aside))
            try:
                return real(root, arxml_version)
            finally:
                shutil.move(str(aside), str(interface))

        monkeypatch.setattr(metrics_module, "all_findings", saved_meanwhile)
        report = metrics_of(project)
        assert report.series[-1].integrity.errors == 0


def head_state(kind: str, directory: Path) -> Path:
    """A project whose HEAD cannot be read, in one of five ways; the working tree reads."""
    project = basic_project(directory)
    if kind == "no git repository":
        return project
    _git("init", "-q", cwd=project)
    if kind == "no commit":
        return project
    if kind == "no model at the commit":
        model_root = project / "model"
        shutil.move(str(model_root), str(directory / "aside"))
        write(project / "notes" / "README.md", "before the model\n")
        commit(project)
        shutil.move(str(directory / "aside"), str(model_root))
    elif kind == "a retired layout at the commit":
        write(project / "model" / "mapping_sets" / "SyntheticSet.yaml", "name: SyntheticSet\nmaps: []\n")
        commit(project)
        shutil.rmtree(project / "model" / "mapping_sets")
    else:
        task = project / "model" / "tasks" / "Task_10ms.yaml"
        good = task.read_text(encoding="utf-8")
        write(task, "name: [unclosed\n")
        commit(project)
        write(task, good)
    return project


HEAD_STATES = (
    "no git repository",
    "no commit",
    "no model at the commit",
    "a retired layout at the commit",
    "a file at the commit that does not parse",
)


class TestSinceHead:
    @pytest.mark.parametrize("kind", HEAD_STATES)
    def test_an_unreadable_head_leaves_the_state_and_says_why(self, tmp_path: Path, kind: str) -> None:
        project = head_state(kind, tmp_path)
        # A component with no port, so what to look at has a line to keep.
        write(project / "model" / "swc" / "example" / "Lonely.yaml", component_yaml("Lonely"))
        report = metrics_of(project, since_head=True)
        alone = metrics_of(project)
        assert [side.label for side in report.series] == ["working tree"]
        assert report.series == alone.series
        assert report.series[0].size.components == 3
        assert report.look_at == alone.look_at
        assert LOOK_NO_LINK in [line.key for line in report.look_at]
        assert report.movement is None
        assert report.no_base
        assert no_location(report.no_base, project)

    @pytest.mark.parametrize(
        "refusal",
        [
            FileNotFoundError(2, "No such file or directory", "git"),
            # A git on the path without the right to execute it: the process start fails with EACCES.
            PermissionError(13, "Permission denied", "git"),
        ],
        ids=["not-installed", "not-executable"],
    )
    def test_git_that_cannot_be_started_is_said_in_fixed_words(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: OSError
    ) -> None:
        # The refusal is raised where the process start raises it, inside `subprocess`, on every platform.
        project = basic_project(tmp_path)
        commit(project)

        def start_fails(*args: Any, **kwargs: Any) -> None:
            raise refusal

        monkeypatch.setattr(subprocess.Popen, "_execute_child", start_fails)
        report = metrics_of(project, since_head=True)
        assert report.no_base == "git could not be run"
        monkeypatch.undo()
        assert report.series == metrics_of(project).series

    @pytest.mark.parametrize(
        "refusal",
        [
            FileNotFoundError(2, "No usable temporary directory found"),
            PermissionError(13, "Permission denied", "tmp"),
            OSError(errno.ENOSPC, "No space left on device", "tmp"),
        ],
        ids=["no-temporary-folder", "temporary-folder-not-writable", "disk-full"],
    )
    def test_a_tree_that_cannot_be_unpacked_after_git_ran_is_not_called_git_not_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: OSError
    ) -> None:
        # Both git calls succeed; the temporary tree HEAD's files go into cannot be made.
        project = basic_project(tmp_path)
        commit(project)

        def no_temporary_tree(*args: Any, **kwargs: Any) -> None:
            raise refusal

        monkeypatch.setattr(model_state_module.tempfile, "TemporaryDirectory", no_temporary_tree)
        report = metrics_of(project, since_head=True)
        assert report.no_base == "HEAD's files could not be unpacked"
        assert no_location(report.no_base, project)
        monkeypatch.undo()
        assert report.series == metrics_of(project).series

    def test_a_full_disk_while_unpacking_is_a_cause_and_not_a_failed_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Both git calls succeed and the temporary tree exists; writing HEAD's files into it fills the
        # disk. The run still measures the working tree and says no errno text and no path.
        project = basic_project(tmp_path)
        commit(project)

        def disk_full(*args: Any, **kwargs: Any) -> None:
            raise OSError(errno.ENOSPC, "No space left on device", str(tmp_path / "unpacked"))

        monkeypatch.setattr(model_state_module.tarfile.TarFile, "extractall", disk_full)
        report = metrics_of(project, since_head=True)
        assert report.no_base == "HEAD's files could not be unpacked"
        assert no_location(report.no_base, project)
        assert report.movement is None
        monkeypatch.undo()
        assert report.series == metrics_of(project).series

    def test_a_full_disk_still_refuses_a_revision_the_user_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only `--since-head`'s HEAD is a base that may be missing; a revision named is refused as before.
        project = basic_project(tmp_path)
        commit(project)

        def disk_full(*args: Any, **kwargs: Any) -> None:
            raise OSError(errno.ENOSPC, "No space left on device", "tmp")

        monkeypatch.setattr(model_state_module.tempfile, "TemporaryDirectory", disk_full)
        with pytest.raises(OSError, match="No space left"):
            metrics_of(project, "HEAD")

    def test_a_repository_git_refuses_is_not_called_no_repository(self, tmp_path: Path) -> None:
        # A `.git` file git cannot read: git finds a repository and refuses it ("invalid gitfile format"),
        # as it refuses one owned by another user; neither is "not in a git repository".
        project = basic_project(tmp_path)
        write(project / ".git", "not a gitdir line\n")
        report = metrics_of(project, since_head=True)
        assert report.no_base == "git refuses to read the repository the project is in"
        assert no_location(report.no_base, project)

    def test_a_git_file_that_points_nowhere_is_no_repository(self, tmp_path: Path) -> None:
        # Git calls a `.git` file naming a directory that does not exist "not a git repository" (up to 2.54)
        # or "gitfile does not point to a valid repository" (2.56): no repository either way.
        project = basic_project(tmp_path)
        write(project / ".git", f"gitdir: {(tmp_path / 'nowhere').as_posix()}\n")
        report = metrics_of(project, since_head=True)
        assert report.no_base == "the project is not in a git repository"

    def test_dubious_ownership_is_a_refusal_and_not_no_repository(self) -> None:
        refusal = RevisionReadError(
            "not a git repository",
            "the model folder is in no git repository this process can read",
            "fatal: detected dubious ownership in repository at '/srv/project'\n",
        )
        assert _head_cause(refusal) == "git refuses to read the repository the project is in"
        missing = RevisionReadError(
            "not a git repository",
            "the model folder is in no git repository this process can read",
            "fatal: not a git repository (or any of the parent directories): .git\n",
        )
        assert _head_cause(missing) == "the project is not in a git repository"
        # git 2.56's wording of a `.git` file naming a missing directory.
        pointing_nowhere = RevisionReadError(
            "not a git repository",
            "the model folder is in no git repository this process can read",
            "fatal: gitfile does not point to a valid repository: /srv/project/.git\n",
        )
        assert _head_cause(pointing_nowhere) == "the project is not in a git repository"
        malformed = RevisionReadError(
            "not a git repository",
            "the model folder is in no git repository this process can read",
            "fatal: invalid gitfile format: /srv/project/.git\n",
        )
        assert _head_cause(malformed) == "git refuses to read the repository the project is in"

    def test_no_commit_is_recognised_under_a_translated_git(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Git's own words are read in the C locale, whatever language the user's git speaks.
        project = head_state("no commit", tmp_path)
        for name, value in (("LANGUAGE", "de"), ("LANG", "de_DE.UTF-8"), ("LC_ALL", "de_DE.UTF-8")):
            monkeypatch.setenv(name, value)
        assert metrics_of(project, since_head=True).no_base == "the current branch has no commit yet"

    def test_an_orphan_branch_in_a_repository_with_commits_says_the_branch_has_none(self, tmp_path: Path) -> None:
        # HEAD is unborn although the repository holds commits, so the cause speaks of the branch.
        project = basic_project(tmp_path)
        commit(project)
        _git("checkout", "-q", "--orphan", "fresh", cwd=project)
        assert metrics_of(project, since_head=True).no_base == "the current branch has no commit yet"

    @pytest.mark.parametrize("kind", HEAD_STATES)
    def test_head_named_by_since_still_refuses(self, tmp_path: Path, kind: str) -> None:
        project = head_state(kind, tmp_path)
        with pytest.raises(ValueError):
            metrics_of(project, "HEAD")

    @pytest.mark.parametrize(
        ("kind", "cause"),
        [
            ("no git repository", "the project is not in a git repository"),
            ("no commit", "the current branch has no commit yet"),
            ("no model at the commit", "HEAD holds no model yet"),
            ("a retired layout at the commit", "HEAD keeps the model in a retired layout"),
        ],
    )
    def test_each_cause_is_recognised_and_said_in_fixed_words(self, tmp_path: Path, kind: str, cause: str) -> None:
        assert metrics_of(head_state(kind, tmp_path), since_head=True).no_base == cause

    def test_a_file_that_does_not_parse_is_named_by_its_storage_name(self, tmp_path: Path) -> None:
        report = metrics_of(head_state("a file at the commit that does not parse", tmp_path), since_head=True)
        assert report.no_base is not None
        assert report.no_base.startswith(
            "a model file at HEAD does not parse: Invalid task YAML in tasks/Task_10ms.yaml: "
        )

    def test_a_file_with_two_field_errors_is_one_line_under_the_sentence(self, tmp_path: Path) -> None:
        """``HEAD_DOES_NOT_PARSE`` keeps one line, the details joined by ``; ``."""
        project = basic_project(tmp_path)
        _git("init", "-q", cwd=project)
        task = project / "model" / "tasks" / "Task_10ms.yaml"
        good = task.read_text(encoding="utf-8")
        write(task, "name: Task_10ms\nperiod: .nan\npriority: x\npartition: PARTITION0\n")
        commit(project)
        write(task, good)
        report = metrics_of(project, since_head=True)
        assert report.no_base == (
            f"{HEAD_DOES_NOT_PARSE}: Invalid task YAML in tasks/Task_10ms.yaml: period: Input should be a finite "
            "number; priority: Input should be a valid integer, unable to parse string as an integer"
        )

    def test_an_unrecognised_refusal_keeps_its_shared_form(self) -> None:
        refusal = RevisionReadError(
            "git archive failed for revision 'HEAD'", "git could not read it", "fatal: bad object"
        )
        assert _head_cause(refusal) == "git archive failed for revision 'HEAD': git could not read it"
        assert _head_cause(ValueError("some other refusal")) == "some other refusal"
