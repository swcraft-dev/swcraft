"""The entry planner's programs, canonical sequence and oracle.

The two integer programs of one configuration (the relaxed one of step 1 and the exact one of
step 3) are held to an independent port-level enumeration of every connection structure of a few
ports (`tests/oracle/specimen_helpers.py`); each constraint family has an acceptance and a refusal
vector; the canonical sequence is held equal under two variable orders and with presolve on and
off, and to layouts recorded on macOS arm64 (`tests/fixtures/layout/canonical.json`), which CI
asserts on Linux x86_64.
"""

from __future__ import annotations

import copy
import json
import logging
import subprocess
import sys
import traceback
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from swcraft_specimens.errors import GenerationError
from swcraft_specimens.passport import PassportRefused, Refusal, compute_passport, validate_passport
from swcraft_specimens.specimen import build as build_module
from swcraft_specimens.specimen import layout as layout_module
from swcraft_specimens.specimen import shape as shape_module
from swcraft_specimens.specimen.build import build_specimen_files
from swcraft_specimens.specimen.components import place_ports, port_spec
from swcraft_specimens.specimen.connectors import star_solution
from swcraft_specimens.specimen.interfaces import split_fan_outs
from swcraft_specimens.specimen.layout import (
    BLOCKS,
    CONNECTORS,
    DELEGATIONS,
    FIRST_FIT,
    LINKS,
    PORTS,
    STARS,
    UNCONNECTED,
    Canonical,
    Configuration,
    Layout,
    Program,
    SolverOptions,
    Tally,
    Trace,
    canonical,
    check_solution,
    derived_total_clause,
    exact_program,
    first_fit,
    layout_bounds,
    no_layout_refusal,
    relaxed_program,
    solve,
    star_pieces,
    star_search,
)
from swcraft_specimens.specimen.leaves import stated_leaves
from swcraft_specimens.specimen.preset import SpecimenPreset
from swcraft_specimens.specimen.shape import Shape, specimen_shape
from swcraft_specimens.specimen.vocabulary import DELEGATION, NO_LAYOUT
from tests.oracle.ceiling_solutions import (
    CEILING_CASES,
    CEILING_FIXTURE,
    case_key,
    ceiling_configuration,
    program_digest,
)
from tests.oracle.specimen_helpers import (
    compare_layout_box,
    configuration_of,
    layout_box,
    load_specimen,
    shaped_witness,
)

CANONICAL = Path(__file__).parent / "fixtures" / "layout" / "canonical.json"

#: Seed 501's configuration: counts (12, 5, 2), one hub; U = 9, u1 = 7; R = 10, A = 3, L = 0,
#: D = 6, C = 4; the SR + CS fan-outs.
SEED_501 = Configuration(
    counts=(12, 5, 2),
    fan_outs=(5, 3, 3, 3, 2, 1, 1, 1),
    unconnected=9,
    max_on_one=7,
    connectors=3,
    links=0,
    delegations=6,
    compositions=4,
)

#: Two stars of three ports cannot share fan-outs (4, 2).
COUNTEREXAMPLE = Configuration(
    counts=(3, 3),
    fan_outs=(4, 2),
    unconnected=0,
    max_on_one=0,
    connectors=4,
    links=3,
    delegations=1,
    compositions=1,
)

#: The 12 shaped witnesses the preferred candidate does not build, as (seed, n).
SOLVER_WITNESSES = (
    (501, 3),
    (502, 4),
    (509, 4),
    (517, 5),
    (518, 6),
    (529, 3),
    (537, 4),
    (538, 5),
    (605, 14),
    (606, 15),
    (608, 9),
    (617, 10),
)


def witness_configuration(seed: int, n: int) -> Configuration:
    """A shaped witness's first configuration: its placed counts and fan-outs."""
    stated = stated_leaves(validate_passport(compute_passport(shaped_witness(seed, n))))
    return first_configuration(stated)


def first_configuration(stated: dict[str, Any]) -> Configuration:
    counts = place_ports(port_spec(stated))
    fan_outs = split_fan_outs(stated, sum(counts), None)
    assert not isinstance(fan_outs, Refusal)
    return configuration_of(stated, counts, fan_outs)


def topology_variant() -> Configuration:
    """The `topology` preset with D = 30, C = 2 and the total 289."""
    passport = copy.deepcopy(load_specimen("topology").passport)
    passport["connectors"]["delegation"] = 30
    passport["layers"]["compositions"] = 2
    passport["components"]["ports"]["total"] = 289
    return first_configuration(stated_leaves(validate_passport(passport)))


def shapes_configuration() -> Configuration:
    return first_configuration(stated_leaves(load_specimen("shapes").passport))


def feasible(configuration: Configuration, exact: bool) -> bool:
    program = exact_program(configuration) if exact else relaxed_program(configuration)
    return solve(program) is not None


def summary(found: Canonical) -> dict[str, Any]:
    """A canonical layout as the recorded data holds it."""
    return {
        "connectors": found.connectors,
        "connected": list(found.connected),
        "unconnected": list(found.unconnected),
    }


def star_summary(found: Canonical) -> dict[str, Any]:
    """A canonical layout's stars, extras and blocks as the recorded data holds them."""
    assert found.providers is not None and found.extras is not None and found.blocks is not None
    return {
        "providers": list(found.providers),
        "extras": list(found.extras),
        "blocks": list(found.blocks),
    }


def configuration_record(configuration: Configuration) -> dict[str, Any]:
    return {
        "counts": list(configuration.counts),
        "fan_outs": list(configuration.fan_outs),
        "unconnected": configuration.unconnected,
        "max_on_one": configuration.max_on_one,
        "connectors": configuration.connectors,
        "links": configuration.links,
        "delegations": configuration.delegations,
        "compositions": configuration.compositions,
    }


# --- seed 501 and the counterexample ------------------------------------------------------------


def test_seed_501_s_configuration_is_the_witness_s_first() -> None:
    assert witness_configuration(501, 3) == SEED_501


def test_seed_501_is_feasible_in_both_programs() -> None:
    assert feasible(SEED_501, exact=False)
    assert feasible(SEED_501, exact=True)


def test_seed_501_s_canonical_layout_passes_the_exact_re_check() -> None:
    program = exact_program(SEED_501)
    found = canonical(program, SEED_501, stars=True)
    assert found is not None
    check_solution(program, found.values)
    assert found.providers is not None and found.extras is not None
    assert found.blocks is not None and found.pieces is not None
    ordinary, to_hubs, from_hubs, hub_to_hub = found.blocks
    # The connector module's own integer check of the decided construction.
    star_solution(
        SEED_501.counts,
        SEED_501.fan_outs,
        [p - c for p, c in zip(SEED_501.counts, found.connected, strict=True)],
        found.providers,
        found.extras,
        found.pieces,
        ordinary=ordinary,
        to_hubs=to_hubs,
        from_hubs=from_hubs,
        hub_to_hub=hub_to_hub,
    )
    assert sum(found.unconnected) == SEED_501.unconnected
    assert max(found.unconnected) == SEED_501.max_on_one
    assert sum(piece[2] for piece in found.pieces) == SEED_501.connectors


def test_seed_501_meets_nineteen_delegations_and_not_twenty() -> None:
    assert feasible(replace(SEED_501, delegations=19), exact=True)
    assert feasible(replace(SEED_501, delegations=19), exact=False)
    assert not feasible(replace(SEED_501, delegations=20), exact=True)
    assert not feasible(replace(SEED_501, delegations=20), exact=False)


def test_the_no_layout_sentence_carries_the_figures_and_the_scope() -> None:
    refusal = no_layout_refusal(replace(SEED_501, delegations=20), 3)
    assert (refusal.leaf, refusal.refusal_class) == (DELEGATION, NO_LAYOUT)
    assert refusal.sentence == (
        "states 20; with 19 ports on 3 components (1 hubs), 9 unconnected (at most 7 on one), "
        "3 connectors, 0 links, 4 compositions and 19 ports on the sender-receiver and "
        "client-server interfaces, no layout of the connectors and the delegations exists in "
        "the specimen generator's chain, for the fan-out tails and counts beyond top that the specimen generator "
        "derives (3 tried)"
    )


def test_the_no_layout_sentence_names_chosen_connectors_when_unstated() -> None:
    sentence = no_layout_refusal(replace(SEED_501, connectors=None), 1).sentence
    assert ", the connectors chosen, 0 links," in sentence
    assert sentence.endswith("(1 tried)")


def test_the_derived_total_clause_states_the_budget_and_the_way_out() -> None:
    assert derived_total_clause(64) == (
        "the derivation tried up to 64 totals, each with its own layout program (at the largest "
        "sizes up to about an hour each); stating components.ports.total makes it one"
    )


def test_the_counterexample_is_infeasible_in_the_exact_program_alone() -> None:
    assert feasible(COUNTEREXAMPLE, exact=False)
    assert not feasible(COUNTEREXAMPLE, exact=True)


# --- one acceptance and one refusal vector per constraint family --------------------------------


def _vector(
    counts: tuple[int, ...], fan_outs: tuple[int, ...], *figures: int, **accepted: Any
) -> tuple[Configuration, Configuration]:
    """A refusal (U, u1, A, L, D, C) and its acceptance, the one figure named changed."""
    refused = Configuration(counts, fan_outs, *figures)
    return refused, replace(refused, **accepted)


#: Per family (L1-L7): a refusal infeasible by that family's rows (feasible without them) and an
#: acceptance one figure away from it. Each was found by a search over small configurations;
#: all but L4's are refused by their own family alone (omitting any other family keeps them
#: infeasible). No configuration searched (up to seven ports on at most three interfaces) is
#: refused by L4 alone: without the stars' rows the extras are free, which relieves the blocks
#: too.
FAMILY_VECTORS: dict[str, tuple[Configuration, Configuration]] = {
    # One connector needs a reached port on each of two components; with one unconnected port
    # on each, the one-port component has none left (u + c <= p).
    PORTS: _vector((3, 1), (4,), 2, 1, 1, 0, 2, 3, connectors=0),
    # Three unconnected ports, at most one on each of two components.
    UNCONNECTED: _vector((2, 1), (3,), 3, 1, 0, 0, 1, 3, unconnected=2),
    # Five connectors on five reached ports: the stars' provide ports come on top of them.
    CONNECTORS: _vector((3, 1, 1), (5,), 0, 0, 5, 0, 1, 3, connectors=4),
    # The zero diagonal: the one connector joins both components, so neither is deep, and
    # D = 3 needs a deep one.
    BLOCKS: _vector((2, 1), (3,), 0, 0, 1, 0, 3, 3, delegations=2),
    # A link and no connector.
    LINKS: _vector((1, 1), (2,), 0, 0, 0, 1, 2, 1, links=0),
    # A connector on interfaces of fan-out 1.
    STARS: _vector((1, 1), (1, 1), 0, 0, 1, 0, 2, 3, connectors=0),
    # One delegation where both reached ports, with no connector, need one each.
    DELEGATIONS: _vector((1, 1), (2,), 0, 0, 0, 0, 1, 1, delegations=2),
}


@pytest.mark.parametrize("family", [PORTS, UNCONNECTED, CONNECTORS, BLOCKS, LINKS, STARS, DELEGATIONS])
def test_each_family_has_an_acceptance_and_a_refusal_vector(family: str) -> None:
    refused, accepted = FAMILY_VECTORS[family]
    assert solve(exact_program(accepted)) is not None
    assert solve(exact_program(refused)) is None
    assert solve(exact_program(refused, omit=frozenset({family}))) is not None
    if family != BLOCKS:
        for other in FAMILY_VECTORS:
            if other != family:
                assert solve(exact_program(refused, omit=frozenset({other}))) is None, other


# --- the solve wrapper --------------------------------------------------------------------------


def test_the_exact_re_check_rejects_a_perturbed_solution() -> None:
    program = exact_program(SEED_501)
    values = solve(program)
    assert values is not None
    check_solution(program, values)
    perturbed = list(values)
    perturbed[program.connected[0]] += 1
    with pytest.raises(GenerationError, match="integer check"):
        check_solution(program, perturbed)


class _Result:
    def __init__(self, status: int, x: Any) -> None:
        self.status = status
        self.x = x
        self.message = f"status {status}"


def test_a_solver_status_other_than_feasible_or_infeasible_is_a_generation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scipy.optimize

    monkeypatch.setattr(scipy.optimize, "milp", lambda *args, **kwargs: _Result(1, None))
    with pytest.raises(GenerationError, match="status 1"):
        solve(exact_program(SEED_501))


def test_a_feasible_verdict_failing_the_integer_check_is_a_generation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scipy.optimize

    program = exact_program(SEED_501)
    zeros = [0.0] * len(program.names)
    monkeypatch.setattr(scipy.optimize, "milp", lambda *args, **kwargs: _Result(0, zeros))
    with pytest.raises(GenerationError, match="integer check"):
        solve(program)


def test_a_no_closed_by_integer_propagation_is_certified() -> None:
    # Four unconnected ports, at most one on each of two components: the domain empties.
    case = Configuration((2, 2), (2, 2), 4, 1, 1, 0, 1, 1)
    tally = Tally()
    assert solve(exact_program(case), tally=tally) is None
    assert (tally.certified, tally.uncertified) == (1, 0)


def test_a_no_propagation_does_not_close_is_counted_as_the_solver_s() -> None:
    tally = Tally()
    assert solve(exact_program(COUNTEREXAMPLE), tally=tally) is None
    assert (tally.certified, tally.uncertified) == (0, 1)


@pytest.mark.parametrize("module", ["swcraft_specimens.specimen.build", "swcraft_specimens.specimen.layout"])
def test_scipy_is_imported_only_by_a_solve(module: str) -> None:
    code = f"import sys, {module}\nprint(sorted(m for m in sys.modules if m.split('.')[0] in ('scipy', 'numpy')))\n"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        cwd=Path(__file__).parents[1],
    )
    assert result.stdout.strip() == "[]"


# --- an unstated connector count ----------------------------------------------------------------

#: Seed 501 with A unstated: the smallest count with a layout, and the next one.
SMALLEST_UNSTATED = 2
NEXT_UNSTATED = 3


def test_an_unstated_count_takes_the_smallest_feasible_choosable_one() -> None:
    unstated = replace(SEED_501, connectors=None)
    found = canonical(relaxed_program(unstated), unstated, choosable=tuple(range(0, 20)))
    assert found is not None and found.connectors == SMALLEST_UNSTATED


def test_an_unstated_count_skips_a_refused_one() -> None:
    unstated = replace(SEED_501, connectors=None)
    choosable = tuple(a for a in range(0, 20) if a != SMALLEST_UNSTATED)
    found = canonical(relaxed_program(unstated), unstated, choosable=choosable)
    assert found is not None and found.connectors == NEXT_UNSTATED


def test_an_unstated_count_with_nothing_choosable_has_no_layout() -> None:
    unstated = replace(SEED_501, connectors=None)
    assert canonical(relaxed_program(unstated), unstated, choosable=()) is None


def test_an_unstated_count_with_no_choosable_one_above_the_smallest_feasible_has_no_layout() -> None:
    # The smallest feasible count is 2: neither 0 nor 1 is feasible, and none is choosable above.
    unstated = replace(SEED_501, connectors=None)
    tally = Tally()
    assert canonical(relaxed_program(unstated), unstated, choosable=(0, 1), tally=tally) is None
    assert tally.solves > 0


# --- the delegation bounds of every layout ------------------------------------------------------


@pytest.mark.parametrize(
    ("reached", "connectors", "compositions", "connectable", "choosable", "bounds"),
    [
        # TestTheInequality's six components: R = 10, A = 3, the SR + CS ports 12.
        (10, 3, 3, 12, (), (4, 19)),
        (10, 3, 1, 12, (), (4, 7)),
        (10, 0, 3, 12, (), (10, 30)),
        # Seed 501: R = 10, A = 3, C = 4, the SR + CS ports 19.
        (10, 3, 4, 19, (), (4, 25)),
        # A unstated: with 0 choosable C x R, else from the least positive count.
        (10, None, 3, 12, (0, 5, 6), (0, 30)),
        (10, None, 3, 12, (5, 6), (0, 3 * (10 - 5) - 2)),
        (10, None, 3, 8, (5, 6), (2, 13)),
        (10, None, 3, 12, (0,), (10, 30)),
        (10, None, 3, 12, (), (10, 9)),
    ],
)
def test_the_bounds_of_every_layout(
    reached: int,
    connectors: int | None,
    compositions: int,
    connectable: int,
    choosable: tuple[int, ...],
    bounds: tuple[int, int],
) -> None:
    assert layout_bounds(reached, connectors, compositions, connectable, choosable) == bounds


# --- canonicity ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["501", "605", "shapes"])
def test_the_canonical_layout_is_one_under_two_variable_orders_and_presolve(name: str) -> None:
    configuration = {
        "501": lambda: SEED_501,
        "605": lambda: witness_configuration(605, 14),
        "shapes": shapes_configuration,
    }[name]()
    program = relaxed_program(configuration)
    found = [
        canonical(program, configuration, options=options)
        for options in (
            SolverOptions(),
            SolverOptions(reversed_columns=True),
            SolverOptions(presolve=False),
        )
    ]
    assert found[0] is not None
    assert found[1] == found[0] and found[2] == found[0]


def test_seed_501_s_exact_canonical_layout_is_one_under_two_variable_orders_and_presolve() -> None:
    program = exact_program(SEED_501)
    first = canonical(program, SEED_501, stars=True)
    second = canonical(program, SEED_501, stars=True, options=SolverOptions(reversed_columns=True))
    third = canonical(program, SEED_501, stars=True, options=SolverOptions(presolve=False))
    assert first is not None and first == second and first == third


def recorded_cases() -> list[tuple[str, Configuration]]:
    cases = [
        ("topology variant", topology_variant()),
        ("shapes", shapes_configuration()),
    ]
    cases.extend((f"seed {seed}", witness_configuration(seed, n)) for seed, n in SOLVER_WITNESSES)
    return cases


def test_the_canonical_layouts_equal_those_recorded_on_macos_arm64() -> None:
    """A floating-point verdict that differs between platforms fails here on the CI runner.

    The runner is Linux x86_64. Each case's sequence runs on to the stars, extras and blocks step
    2's first-fit builder reads (A and (c, u) stay as fixed).
    """
    recorded = json.loads(CANONICAL.read_text(encoding="utf-8"))
    assert recorded["recorded_on"] == "macOS arm64"
    cases = recorded_cases()
    assert [name for name, _ in cases] == [case["name"] for case in recorded["cases"]]
    assert [case["name"] for case in recorded["stars"]["cases"]] == [name for name, _ in cases]
    for (name, configuration), case, stars in zip(cases, recorded["cases"], recorded["stars"]["cases"], strict=True):
        assert configuration_record(configuration) == case["configuration"], name
        found = canonical(relaxed_program(configuration), configuration, stars=True)
        assert found is not None, name
        assert summary(found) == case["canonical"], name
        assert star_summary(found) == stars["stars"], name


@pytest.mark.parametrize(
    ("section", "exact", "configuration"),
    [
        ("exact", True, SEED_501),
        ("unstated", False, replace(SEED_501, connectors=None)),
    ],
    ids=["seed-501-exact", "seed-501-unstated"],
)
def test_the_exact_and_the_unstated_canonical_layouts_equal_those_recorded(
    section: str, exact: bool, configuration: Configuration
) -> None:
    """Step 3's canonical layout with its star placement, and the smallest admitted A of an unstated count.

    As recorded on macOS arm64.
    """
    (case,) = json.loads(CANONICAL.read_text(encoding="utf-8"))[section]["cases"]
    assert configuration_record(configuration) == case["configuration"]
    program = exact_program(configuration) if exact else relaxed_program(configuration)
    found = canonical(program, configuration, stars=True)
    assert found is not None
    record = {**summary(found), **star_summary(found)}
    if found.pieces is not None:
        record["pieces"] = [list(piece) for piece in found.pieces]
    assert record == case["canonical"]


# --- the builders -------------------------------------------------------------------------------

#: Seed 501's star placement, (component, interface, stars, connectors): the hub centres two
#: stars of one connector each on the first interface (fan-out 5) and one on the second.
PINNED_PLACEMENT = ((0, 0, 2, 2), (0, 1, 1, 1))


def test_the_star_search_on_seed_501_s_exact_counts_finds_the_pinned_placement() -> None:
    found = canonical(exact_program(SEED_501), SEED_501, stars=True)
    assert found is not None and found.providers is not None and found.extras is not None
    outs = [p + e for p, e in zip(found.providers, found.extras, strict=True)]
    placement = star_search(SEED_501.fan_outs, found.providers, outs)
    assert placement == PINNED_PLACEMENT
    assert found.pieces == star_pieces(PINNED_PLACEMENT)


def test_the_star_search_refuses_counts_no_placement_has() -> None:
    # Two stars of three ports on (4, 2): the counterexample's counts.
    assert star_search((4, 2), (1, 1), (2, 2)) is None
    assert star_search((4, 2), (0, 1), (1, 1)) is None


def test_the_star_search_refuses_stars_with_no_interface_to_take_them() -> None:
    # No interface of fan-out 2 or more: a star has nowhere to go, and no star needs none.
    assert star_search((1, 1), (1, 0), (1, 0)) is None
    assert star_search((1, 1), (0, 0), (0, 0)) == ()


def test_the_star_pieces_split_the_connectors_evenly() -> None:
    assert star_pieces(((0, 1, 2, 5), (1, 0, 1, 1))) == ((0, 1, 3), (0, 1, 2), (1, 0, 1))


def test_the_first_fit_builder_takes_the_least_room_that_holds_a_star() -> None:
    # Component 0: one star with 2 extras (4 ports); component 1: two stars and one extra, on
    # its first (3 and 2 ports). Into (5, 4, 3): 4 -> the 4, 3 -> the 3, 2 -> the 5.
    assert first_fit((5, 4, 3), (1, 2), (2, 1)) == ((0, 1, 3), (1, 2, 2), (1, 0, 1))
    assert first_fit((3, 3), (1,), (2,)) is None
    # Extras on a component with no star.
    assert first_fit((4,), (0,), (1,)) is None


# --- the independent oracle ---------------------------------------------------------------------


def test_the_boxes_have_the_measured_configurations() -> None:
    assert [len(layout_box(ports)) for ports in (4, 5, 6)] == [28, 63, 151]


def _oracle(max_ports: int, cases: int) -> None:
    checked, disagreements, misses, failures = compare_layout_box(max_ports)
    assert disagreements == []
    assert misses == []
    assert failures == []
    assert checked == cases


def test_the_exact_program_equals_the_oracle_on_the_box_of_five_ports() -> None:
    """Every case of at most five ports: the exact program's verdict is the oracle's.

    And the relaxed program is never infeasible where the oracle has a layout.
    """
    _oracle(5, 78_990)


@pytest.mark.slow
def test_the_exact_program_equals_the_oracle_on_the_box_of_six_ports() -> None:
    _oracle(6, 363_714)


# --- the notice ---------------------------------------------------------------------------------

#: The step-3 notice, for a preset named `SweepSpecimen`.
NOTICE_LINE = (
    "swcraft-specimens: specimen SweepSpecimen: the layout needs the specimen generator's exact program, which may "
    "take long at this size; stating components.ports.total and interfaces.fan_out.top shortens "
    "it"
)


@pytest.fixture
def fresh_shapes() -> Iterator[None]:
    """The shape cache cleared before and after a test that counts what the shape computes."""
    shape_module._shape_cached.cache_clear()
    yield
    shape_module._shape_cached.cache_clear()


def seed_501_preset() -> SpecimenPreset:
    passport = validate_passport(compute_passport(shaped_witness(501, 3)))
    return SpecimenPreset("SweepSpecimen", passport)


def step_two_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither step-2 builder builds, so the planner reaches step 3."""

    def not_built(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(layout_module, "solve_open", not_built)
    monkeypatch.setattr(layout_module, "first_fit", not_built)


def test_the_notice_is_given_once_from_the_shape_inside_the_feasibility_check(
    monkeypatch: pytest.MonkeyPatch, fresh_shapes: None
) -> None:
    step_two_fails(monkeypatch)
    calls: list[tuple[str, list[str]]] = []
    exact_programs: list[Configuration] = []
    real_exact_program = layout_module.exact_program

    def counted(configuration: Configuration, *args: Any, **kwargs: Any) -> Any:
        exact_programs.append(configuration)
        return real_exact_program(configuration, *args, **kwargs)

    def notify(line: str) -> None:
        calls.append((line, [frame.name for frame in traceback.extract_stack()]))

    monkeypatch.setattr(layout_module, "exact_program", counted)
    files = build_specimen_files(seed_501_preset(), notify=notify)
    assert "generation.json" in files
    assert [line for line, _ in calls] == [NOTICE_LINE]
    # Step 3 ran once: the build read the cached shape (the wrapper's dedupe hid nothing).
    assert len(exact_programs) == 1
    # Given while the feasibility check computed the shape, not again from the cached build.
    stack = calls[0][1]
    assert "check_feasibility" in stack and "specimen_shape" in stack
    assert "build_project" not in stack
    assert layout_module.NOTICE.get() is None


def test_the_notice_is_not_given_without_step_three(fresh_shapes: None) -> None:
    calls: list[str] = []
    build_specimen_files(seed_501_preset(), notify=calls.append)
    assert calls == []
    shape = specimen_shape(stated_leaves(seed_501_preset().passport))
    assert isinstance(shape, Shape) and shape.layout.step == 2


# --- step 3 not reached at the ceiling ----------------------------------------------------------

#: The fastest of the five feasible ceiling cases (19.5 s), the one the `slow` test still solves.
SOLVED_CEILING_CASE = (3500, 100, 4, 0)


def _step_two_at_the_ceiling(configuration: Configuration, program: Program, raw: tuple[int, ...]) -> None:
    """The first-fit builder builds this raw solution of the relaxed program, so step 3 is not reached."""
    connectors = configuration.connectors
    assert connectors is not None
    assert (configuration.hubs, configuration.ports, configuration.phi) == (5, 9995, 9995)
    providers = tuple(raw[v] for v in program.providers)
    extras = tuple(raw[v] for v in program.extras)
    pieces = first_fit(configuration.fan_outs, providers, extras)
    assert pieces is not None
    ordinary, to_hubs, from_hubs, hub_to_hub = (raw[v] for v in program.blocks)
    connected = tuple(raw[v] for v in program.connected)
    solution = star_solution(
        configuration.counts,
        configuration.fan_outs,
        tuple(p - c for p, c in zip(configuration.counts, connected, strict=True)),
        providers,
        extras,
        pieces,
        ordinary=ordinary,
        to_hubs=to_hubs,
        from_hubs=from_hubs,
        hub_to_hub=hub_to_hub,
    )
    assert sum(solution.providers) == sum(connected) - connectors


@pytest.mark.parametrize("case", CEILING_CASES, ids=[case_key(case) for case in CEILING_CASES])
def test_step_two_builds_the_recorded_raw_solution_at_the_ceiling(case: tuple[int, int, int, int]) -> None:
    """The first-fit builder builds the relaxed program's recorded raw solution of each feasible ceiling case.

    So step 3 is not reached on it.

    Each recorded solution (`tests/oracle/ceiling_solutions.py`) is re-checked in Python integers
    against the program built today, whose digest is the recorded one. Not reached: the canonical
    (c, u) at the ceiling (20-60 minutes each), so step 3 is not shown avoided on the canonical
    solution; nor is the exact program run here."""
    recorded = {entry["case"]: entry for entry in json.loads(CEILING_FIXTURE.read_text(encoding="utf-8"))["cases"]}
    entry = recorded[case_key(case)]
    configuration = ceiling_configuration(*case)
    program = relaxed_program(configuration)
    assert program_digest(program) == entry["program_sha256"]
    values = [0] * len(program.names)
    for index, value in entry["nonzero"]:
        values[index] = value
    raw = tuple(values)
    check_solution(program, raw)
    _step_two_at_the_ceiling(configuration, program, raw)


def test_the_ceiling_cases_are_recorded_once_each() -> None:
    recorded = [entry["case"] for entry in json.loads(CEILING_FIXTURE.read_text(encoding="utf-8"))["cases"]]
    assert recorded == [case_key(case) for case in CEILING_CASES]


def test_every_solve_runs_on_one_long_lived_solver_thread_and_the_default_stack_is_restored() -> None:
    """One thread for the process: a fresh thread per solve crashed HiGHS on Windows."""
    import threading

    before = threading.stack_size()
    seen: list[tuple[str, int]] = []

    def call() -> int:
        seen.append((threading.current_thread().name, threading.get_ident()))
        return 7

    assert layout_module._on_the_solver_stack(call) == 7
    assert layout_module._on_the_solver_stack(call) == 7
    assert seen[0] == seen[1]
    assert seen[0][0].startswith(layout_module.SOLVER_THREAD_NAME)
    assert seen[0][1] != threading.get_ident()
    assert threading.stack_size() == before
    assert layout_module.SOLVER_STACK_BYTES == 128 * 1024 * 1024


#: CPython's ``THREAD_MAX_STACKSIZE`` on Windows (``Python/thread_nt.h``, 0x10000000): a requested size
#: at or above it is refused with ``ValueError: size not valid``.
WINDOWS_THREAD_MAX_STACKSIZE = 0x10000000


def test_the_solver_stack_is_below_the_windows_thread_limit() -> None:
    assert layout_module.SOLVER_STACK_BYTES < WINDOWS_THREAD_MAX_STACKSIZE


def test_a_solve_s_exception_is_raised_on_the_caller_s_thread() -> None:
    def call() -> int:
        raise GenerationError("from the solve")

    with pytest.raises(GenerationError, match="from the solve"):
        layout_module._on_the_solver_stack(call)


@pytest.mark.slow
def test_the_relaxed_program_is_solved_at_the_ceiling() -> None:
    """One ceiling case solved for real, on every platform the stage runs on.

    The solve answers on the solver's own stack (a ceiling-size program overflowed a Windows main
    thread's), its solution passes the integer check, and step 2 builds it.
    """
    configuration = ceiling_configuration(*SOLVED_CEILING_CASE)
    program = relaxed_program(configuration)
    raw = solve(program)
    assert raw is not None
    _step_two_at_the_ceiling(configuration, program, raw)


def test_the_notice_wrapper_tells_at_most_once_per_build(monkeypatch: pytest.MonkeyPatch) -> None:
    def twice(preset: SpecimenPreset) -> dict[str, bytes]:
        notice = layout_module.NOTICE.get()
        assert notice is not None
        notice("first")
        notice("second")
        return {}

    monkeypatch.setattr(build_module, "_build_specimen_files", twice)
    lines: list[str] = []
    assert build_specimen_files(seed_501_preset(), notify=lines.append) == {}
    assert lines == ["swcraft-specimens: specimen SweepSpecimen: first"]
    assert layout_module.NOTICE.get() is None


def test_the_notice_is_reset_after_a_refused_build(fresh_shapes: None) -> None:
    passport = seed_501_preset().passport
    passport["connectors"]["delegation"] = 20
    lines: list[str] = []
    with pytest.raises(PassportRefused):
        build_specimen_files(SpecimenPreset("SweepSpecimen", passport), notify=lines.append)
    assert lines == []
    assert layout_module.NOTICE.get() is None


# --- the planner's answers ----------------------------------------------------------------------


def test_seed_501_at_twenty_delegations_is_refused_at_step_one_with_its_tally(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=layout_module.LOG.name)
    planned = layout_module.plan(replace(SEED_501, delegations=20))
    assert planned.layout is None and planned.trace == Trace(relaxed=False)
    # The "no" a refusal rests on is counted, not dropped.
    assert planned.tally.solves == planned.tally.certified + planned.tally.uncertified >= 1
    (record,) = caplog.records
    assert record.getMessage().endswith(
        f"{planned.tally.solves} solves: 0 feasible, {planned.tally.certified} certified no, "
        f"{planned.tally.uncertified} uncertified no"
    )


def test_the_counterexample_s_step_three_refusal_keeps_its_uncertified_no() -> None:
    planned = layout_module.plan(COUNTEREXAMPLE)
    assert planned.layout is None
    assert planned.trace == Trace(relaxed=True, builder=None, exact=False)
    assert planned.tally.feasible > 0 and planned.tally.uncertified >= 1


def test_without_solve_open_seed_501_is_built_by_first_fit_from_the_continued_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(layout_module, "solve_open", lambda *_: None)
    planned = layout_module.plan(SEED_501)
    assert planned.trace == Trace(relaxed=True, builder=FIRST_FIT)
    layout = planned.layout
    assert layout is not None and (layout.source, layout.step) == ("solver", 2)
    # The stars are the relaxed program's canonical ones, (A, c, u) as step 2 fixed them.
    found = canonical(relaxed_program(SEED_501), SEED_501, stars=True)
    assert found is not None and found.providers is not None and found.extras is not None
    assert (layout.connectors, layout.connected, layout.unconnected) == (
        found.connectors,
        found.connected,
        found.unconnected,
    )
    assert (layout.providers, layout.extras) == (found.providers, found.extras)
    assert layout.pieces == first_fit(SEED_501.fan_outs, found.providers, found.extras)
    assert planned.tally.feasible > 0


def test_a_first_fit_layout_is_built_into_a_project_that_holds_it(
    monkeypatch: pytest.MonkeyPatch, fresh_shapes: None
) -> None:
    monkeypatch.setattr(layout_module, "solve_open", lambda *_: None)
    real_plan = layout_module.plan
    traces: list[Trace] = []

    def spied(*args: Any, **kwargs: Any) -> Any:
        planned = real_plan(*args, **kwargs)
        traces.append(planned.trace)
        return planned

    monkeypatch.setattr(layout_module, "plan", spied)
    preset = seed_501_preset()
    files = build_specimen_files(preset)
    assert "generation.json" in files
    assert traces == [Trace(relaxed=True, builder=FIRST_FIT)]
    shape = specimen_shape(stated_leaves(preset.passport))
    assert isinstance(shape, Shape) and (shape.layout.source, shape.layout.step) == ("solver", 2)


@pytest.mark.parametrize(
    ("unconnected", "delegations", "match"),
    [
        ((7, 2), 6, "does not give every component"),
        ((7, 2, 5), 6, "do not add up"),
        (None, 0, "do not reproduce D"),
    ],
    ids=["short", "beyond-the-open-ports", "below-the-reach"],
)
def test_a_layout_its_construction_does_not_give_is_a_generation_error(
    unconnected: tuple[int, ...] | None, delegations: int, match: str
) -> None:
    planned = layout_module.plan(SEED_501)
    layout = planned.layout
    assert layout is not None
    solution = layout.solution(SEED_501.counts, SEED_501.fan_outs)
    with pytest.raises(GenerationError, match=match):
        Layout.of_solution(
            SEED_501.counts,
            layout.unconnected if unconnected is None else unconnected,
            solution,
            delegations,
            SEED_501.compositions,
            source=layout_module.SOLVER,
            step=2,
        )


@pytest.mark.parametrize(
    "command",
    [
        ["generate", "small"],
        ["generate", "chain-empty"],
    ],
    ids=["waters", "preferred-specimen"],
)
def test_a_build_with_no_solver_built_layout_loads_neither_scipy_nor_numpy(tmp_path: Path, command: list[str]) -> None:
    code = (
        "import sys\n"
        "from swcraft_specimens.cli import run\n"
        f"assert run({[*command, '--out', str(tmp_path / 'out')]!r}) == 0\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in ('scipy', 'numpy')))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        cwd=Path(__file__).parents[1],
    )
    assert result.stdout.strip().splitlines()[-1] == "[]"
