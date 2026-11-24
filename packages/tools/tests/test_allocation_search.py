"""The allocation experiment's search (``swcraft_tools.allocation.solve_curve``).

Every model is synthetic (``carma_models.py``). The curve on fixture (a) is compared with an optimum
computed by hand (the move sets as literals below), and every optimum's move set is run through
``check_cross_layer_consistency`` here, independently of the search's own check.

A status other than ``optimal`` is forced by monkeypatching the solver's answer, never by a short time
limit: where a limit falls depends on the machine and its load. Determinism is asserted only on completed
searches.
"""

from __future__ import annotations

import dataclasses
import filecmp
import itertools
import json
import re
import sys
import urllib.parse
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
import scipy.optimize
import scipy.sparse
import validator_package
from carma_models import (
    FORMULATION_EXPERIMENT,
    FORMULATION_LINKS,
    THREE_CORES_LAYOUT,
    UNLINKED_FREE_IDLE,
    UNREACHABLE_TIME_LIMIT_S,
    CarmaModel,
    awkward_periods,
    budget_cap,
    config_document,
    formulation,
    full_run_document,
    init_runnable,
    mixed_task,
    mixed_task_with_idle,
    three_cores,
    tolerance_exact,
    tolerance_scale,
    unlinked_free,
    unloadable_solver,
    with_unmapped_server,
    write_project,
    zero_rule,
)
from swcraft_tools import allocation
from swcraft_tools.allocation import (
    BUDGET_CAP_REASON,
    CURVE_STATUSES,
    ENUMERATION_AT_K,
    ENUMERATION_EXHAUSTED,
    NOT_REPRESENTABLE,
    STATUS_FEASIBLE,
    STATUS_INFEASIBLE,
    STATUS_OPTIMAL,
    STATUS_UNKNOWN,
    TABLE_MISSED_REASON,
    ZERO_RULE_REASON,
    BudgetCapExceeded,
    Curve,
    Move,
    NotRepresentableError,
    Projection,
    SearchModel,
    SolverExecutionError,
    SolverMissingError,
    link_sets,
    moves_of,
    parse_config,
    read_projection,
    solve_curve,
    write_move_set,
)
from swcraft_tools.diagnostics import Subject
from swcraft_tools.project import require_project
from swcraft_tools.resource_analysis import analyze_resource_budgets
from swcraft_tools.validator import ConsistencyIssue, check_cross_layer_consistency
from swcraft_tools.yaml_load import load_yaml_document


def _setup(model: CarmaModel, tmp_path: Path, **experiment: Any) -> tuple[Path, Projection, Any]:
    root = model.write(tmp_path / "model")
    parsed = parse_config(full_run_document(**experiment))
    return root, read_projection(root, parsed), parsed


class TestTheHelpersDeadline:
    """The wall clock stays out of what a test asserts: a project's deadline is the helper's, not each call's."""

    def test_a_project_written_with_no_deadline_carries_the_unreachable_one(self, tmp_path: Path) -> None:
        _manifest, file = write_project(three_cores(), tmp_path)
        written = parse_config(load_yaml_document(file.read_text(encoding="utf-8")))
        assert written.search.time_limit_s == UNREACHABLE_TIME_LIMIT_S

    def test_a_named_deadline_wins(self, tmp_path: Path) -> None:
        _manifest, file = write_project(three_cores(), tmp_path, time_limit_s=45)
        written = parse_config(load_yaml_document(file.read_text(encoding="utf-8")))
        assert written.search.time_limit_s == 45


def _run(model: CarmaModel, tmp_path: Path, **experiment: Any) -> tuple[Path, Projection, Curve]:
    root, projection, parsed = _setup(model, tmp_path, **experiment)
    return root, projection, solve_curve(root, projection, parsed)


def _names(moves: tuple[Move, ...]) -> frozenset[tuple[str, str, str]]:
    return frozenset((move.component, move.source, move.target) for move in moves)


def _brute_force(projection: Projection, assignment: dict[str, str]) -> Fraction:
    """``sum cost x [partitions differ]`` over every costed link -- no CONSTANT/VARIABLE split, no C0."""
    current = {row.placement.component: row.placement.partition for row in projection.table}
    where = {**current, **assignment}
    return sum(
        (link.cost for link in projection.links if link.costed and where[link.sender] != where[link.receiver]),
        Fraction(0),
    )


def _apply_and_check(root: Path, projection: Projection, moves: tuple[Move, ...], destination: Path):
    write_move_set(root, destination, projection, moves)
    return check_cross_layer_consistency(str(destination))


# Fixture (a): the hand-computed optimum

#: The links of ``three_cores`` that cross at the start, each 400 bytes x Hz:
#: L0 B1 (P2) -> B5 (P3), L1 B2 (P2) -> B6 (P3), L2 Q1 (P1) -> B7 (P3). One move
#: joins at most one of them; L2 only by Q1 -> P3 (B7 may not enter the QM P1).
_L0 = (("B1", "P2", "P3"), ("B5", "P3", "P2"))
_L1 = (("B2", "P2", "P3"), ("B6", "P3", "P2"))
_L2 = (("Q1", "P1", "P3"),)
HAND_OPTIMUM: dict[int, tuple[Fraction, frozenset[frozenset[tuple[str, str, str]]]]] = {
    1: (Fraction(800), frozenset(frozenset({move}) for move in (*_L0, *_L1, *_L2))),
    2: (
        Fraction(400),
        frozenset(
            frozenset({a, b}) for first, second in ((_L0, _L1), (_L0, _L2), (_L1, _L2)) for a in first for b in second
        ),
    ),
    3: (Fraction(0), frozenset(frozenset({a, b, c}) for a in _L0 for b in _L1 for c in _L2)),
}


class TestThreeCoresCurve:
    def test_the_curve_is_the_hand_computed_optimum(self, tmp_path: Path) -> None:
        _root, projection, curve = _run(three_cores(), tmp_path)
        assert curve.current == Fraction(1200) == projection.current_volume
        assert curve.constant == 0
        for n, (value, move_sets) in HAND_OPTIMUM.items():
            point = curve.point(n)
            assert point.status == STATUS_OPTIMAL and not point.cut_short
            assert point.solution is not None
            assert point.solution.objective == value
            assert _names(point.solution.moves) in move_sets

    def test_every_hand_optimum_passes_check_model_on_a_copy(self, tmp_path: Path) -> None:
        root, projection, _parsed = _setup(three_cores(), tmp_path)
        sets = link_sets(projection)
        for n, (value, move_sets) in HAND_OPTIMUM.items():
            for index, move_set in enumerate(sorted(move_sets, key=sorted)):
                assignment = {component: target for component, _source, target in move_set}
                assert _brute_force(projection, assignment) == value
                moves = moves_of(projection, sets, assignment)
                result = _apply_and_check(root, projection, moves, tmp_path / f"copy-{n}-{index}")
                assert result.consistent, result.issues

    def test_a_locked_component_never_moves(self, tmp_path: Path) -> None:
        _root, _projection, curve = _run(three_cores(), tmp_path, locked=["Q1"], candidates=10)
        moved = {move.component for point in curve.points if point.solution for move in point.solution.moves}
        moved |= {move.component for candidate in curve.candidates for move in candidate.solution.moves}
        assert "Q1" not in moved
        # L2 can no longer be joined: the best at N = 3 leaves its 400 bytes x Hz.
        assert curve.point(3).solution is not None and curve.point(3).solution.objective == 400

    def test_the_candidates_are_k_in_the_order_of_objective_then_move_set_names(self, tmp_path: Path) -> None:
        _root, _projection, curve = _run(three_cores(), tmp_path)
        assert curve.enumeration == ENUMERATION_AT_K
        assert len(curve.candidates) == 3
        # The point's proved optimum first, then the enumerated ones by objective and move-set names.
        assert curve.candidates[0].solution == curve.point(3).solution
        keys = [(c.solution.objective, allocation.move_set_key(c.solution.moves)) for c in curve.candidates[1:]]
        assert keys == sorted(keys)
        assert all(c.within_tolerance for c in curve.candidates)
        assert all(_names(c.solution.moves) in HAND_OPTIMUM[3][1] for c in curve.candidates)

    def test_the_enumeration_exhausts_and_that_is_not_the_curve_status_infeasible(self, tmp_path: Path) -> None:
        _root, _projection, curve = _run(three_cores(), tmp_path, candidates=10)
        assert curve.enumeration == ENUMERATION_EXHAUSTED
        assert curve.enumeration not in CURVE_STATUSES
        assert curve.point(3).status == STATUS_OPTIMAL
        # Every proved optimum of N = 3, and nothing else: four move sets.
        assert {_names(c.solution.moves) for c in curve.candidates} == HAND_OPTIMUM[3][1]

    def test_every_candidate_is_within_the_tolerance_exactly(self, tmp_path: Path) -> None:
        _root, projection, curve = _run(three_cores(), tmp_path, candidates_at=2, candidates=6, tolerance=1.0)
        optimum = curve.point(2).solution
        assert optimum is not None and curve.candidates[0].solution == optimum
        for candidate in curve.candidates:
            assert candidate.solution.objective <= optimum.objective * 2
            assert candidate.solution.objective == _brute_force(projection, dict(candidate.solution.assignment))

    @pytest.mark.parametrize("tolerance", [0.5, 1.0])
    def test_no_candidate_fails_to_improve_on_the_current_state(self, tmp_path: Path, tolerance: float) -> None:
        """A tolerance reaching the current volume never makes a set that buys nothing, or worse, a candidate."""
        _root, projection, curve = _run(three_cores(), tmp_path, candidates_at=1, candidates=20, tolerance=tolerance)
        assert curve.current == 1200
        assert len(curve.candidates) > 1
        assert all(candidate.solution.objective < curve.current for candidate in curve.candidates)
        # The bound read in result.json is the capped one: s x (current - C0) - 1 at most.
        search = SearchModel(scipy, projection, projection.sets, 1)
        cap = search.objective_scale * (curve.current - projection.sets.c0) - 1
        assert curve.tolerance_bound is not None and curve.tolerance_bound <= cap

    def test_no_later_candidate_contains_an_earlier_candidates_moves(self, tmp_path: Path) -> None:
        """The superset no-good is one-directional: a later subset of an earlier, better candidate is kept.

        At N = 2 with tolerance 1.0 the optimum's volume is not 0, so the bound admits worse sets and
        nesting actually occurs: the 1-move sets come after the 2-move sets that contain them.
        """
        _root, _projection, curve = _run(three_cores(), tmp_path, candidates_at=2, candidates=20, tolerance=1.0)
        move_sets = [_names(candidate.solution.moves) for candidate in curve.candidates]
        assert len(move_sets) > 1
        for index, earlier in enumerate(move_sets):
            for later in move_sets[index + 1 :]:
                assert not earlier <= later
        # The case the no-good deliberately does not reach occurs here: fewer moves for less gain.
        nested = [
            (earlier, later)
            for index, earlier in enumerate(move_sets)
            for later in move_sets[index + 1 :]
            if later < earlier
        ]
        assert nested
        volumes = {_names(candidate.solution.moves): candidate.solution.objective for candidate in curve.candidates}
        assert all(volumes[later] > volumes[earlier] for earlier, later in nested)

    def test_two_completed_runs_give_equal_results_up_to_the_timing_fields(self, tmp_path: Path) -> None:
        first = _run(three_cores(), tmp_path / "one", candidates=10)[2]
        second = _run(three_cores(), tmp_path / "two", candidates=10)[2]
        assert all(point.status == STATUS_OPTIMAL for point in first.points)
        assert first.enumeration == ENUMERATION_EXHAUSTED
        assert _without_timing(dataclasses.asdict(first)) == _without_timing(dataclasses.asdict(second))


def _without_timing(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_timing(item) for key, item in value.items() if key != "seconds"}
    if isinstance(value, (list, tuple)):
        return [_without_timing(item) for item in value]
    return value


# The per-candidate check


class TestThePerCandidateCheck:
    def test_a_set_the_table_admits_and_a_rule_refuses_is_dropped_and_listed(self, tmp_path: Path) -> None:
        _root, projection, curve = _run(mixed_task(), tmp_path, max_moves=2)
        assert projection.row("Q").target("P4").admissible and projection.row("B").target("P4").admissible
        point = curve.point(2)
        (refused,) = point.refused
        assert _names(refused.moves) == {("B", "P2", "P4"), ("Q", "P1", "P4")}
        assert refused.rules == ("rule:safety.task-mixed-asil",)
        assert refused.reason == TABLE_MISSED_REASON
        # The search goes on without it: the point still has a checked answer.
        assert point.status == STATUS_OPTIMAL and point.solution is not None
        assert point.solution.objective == 400

    def test_the_zero_rule_drop_is_listed_as_the_kept_source_budget(self, tmp_path: Path) -> None:
        _root, _projection, curve = _run(zero_rule(), tmp_path, locked=["Y", "W"], max_moves=2, candidates=1)
        (refused,) = curve.point(1).refused
        assert _names(refused.moves) == {("X", "PA", "PB")}
        assert refused.rules == ("rule:hardware.threshold-breach",)
        assert refused.reason == ZERO_RULE_REASON
        assert refused.reason != TABLE_MISSED_REASON

    def test_the_exact_no_good_keeps_the_legal_better_superset(self, tmp_path: Path) -> None:
        _root, _projection, curve = _run(zero_rule(), tmp_path, locked=["Y", "W"], max_moves=2, candidates=1)
        # {X -> PB} was refused at N = 1; the superset {X -> PB, Z -> PC} is legal, better, and found at N = 2.
        superset = curve.point(2).solution
        assert superset is not None
        assert _names(superset.moves) == {("X", "PA", "PB"), ("Z", "PA", "PC")}
        assert superset.objective == 0

    def test_a_kept_candidates_loads_are_the_copys_not_the_solvers(self, tmp_path: Path) -> None:
        root, projection, curve = _run(zero_rule(), tmp_path, locked=["Y", "W"], max_moves=2, candidates=1)
        (candidate,) = curve.candidates
        solution = candidate.solution
        copy = tmp_path / "copy"
        write_move_set(root, copy, projection, solution.moves)
        expected = {usage.name: usage.consumption for usage in analyze_resource_budgets(str(copy)).resources}
        loads = {load.core: load.load for load in solution.loads}
        assert loads == {name: value for name, value in expected.items() if name in loads}
        # The copy keeps TA's budget (zero rule), so C1 is 0.77 where the solver counted 0.76.
        assert loads["C1"] == Fraction(77, 100)
        solver_figure = projection.loads[0].load - projection.placements["Z"].share
        assert solver_figure == Fraction(76, 100) != loads["C1"]
        assert solution.touched_cores == ("C1", "C2")

    def test_the_implied_edits_state_the_budget_the_zero_rule_frees(self, tmp_path: Path) -> None:
        _root, _projection, curve = _run(zero_rule(), tmp_path, locked=["Y", "W"], max_moves=2, candidates=1)
        edits = {edit.task: edit for edit in curve.candidates[0].solution.edits}
        assert list(edits) == ["TA", "TB", "TC", "TZ"]
        assert edits["TA"].kept_by_zero_rule and edits["TA"].written == "100us" and edits["TA"].frees == "100us"
        assert (edits["TB"].written, edits["TC"].written, edits["TZ"].written) == ("7100us", "1500us", "500us")
        assert not any(edits[name].kept_by_zero_rule for name in ("TB", "TC", "TZ"))

    def test_an_advisory_core_constrains_nothing_and_its_breach_is_reported(self, tmp_path: Path) -> None:
        model = three_cores()
        model.core("C3", threshold=0.1, enforcement="advisory", asil_capability="B")
        root, projection, parsed = _setup(model, tmp_path)
        assert check_cross_layer_consistency(str(root)).consistent
        assert "C3" not in SearchModel(scipy, projection, projection.sets, 3).constrained_cores
        curve = solve_curve(root, projection, parsed)
        solution = curve.point(3).solution
        assert solution is not None and solution.objective == 0  # the moves into P3 are not held back
        assert solution.advisory_breaches == ("C3",)
        c3 = next(load for load in solution.loads if load.core == "C3")
        assert c3.load > c3.threshold


# A warning refuses nothing

#: The rule the fixture below breaks alone (the projection reads no requirement). A warning
#: (``validator.WARNING_RULES``); each case's control makes it an error by monkeypatch.
_WARNING_RULE = "trace.requirement-unallocated"

#: The requirement the fixture adds and nothing satisfies.
_UNSATISFIED = "REQ_CARMA_UNSATISFIED"


def _with_an_unsatisfied_requirement() -> CarmaModel:
    """``three_cores`` with one more requirement no component satisfies: the warning's finding, no other."""
    model = three_cores()
    model.unsatisfied_requirement(_UNSATISFIED)
    return model


class TestAWarningRefusesNothing:
    """The experiment refuses a model with error findings; a warning neither refuses it nor cuts a candidate.

    Each case runs the model with the rule as shipped (a warning, which
    `test_the_warning_rules_are_the_contracts_own` holds), then made an error as the control, so the class
    is the only thing between the model and the refusal.
    """

    def test_a_model_whose_only_finding_is_a_warning_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        model = _with_an_unsatisfied_requirement()
        manifest, file = write_project(model, tmp_path)
        gate = check_cross_layer_consistency(str(tmp_path / "model"))
        assert gate.consistent and gate.issues == []
        assert [(record.rule, record.subjects) for record in gate.warnings] == [
            (_WARNING_RULE, (Subject("requirements", _UNSATISFIED),))
        ]

        run = allocation.run_allocation(require_project(str(manifest)), file)

        assert run.result_path.exists() and run.report_path.exists()
        assert run.document["curve"][0]["status"] == STATUS_OPTIMAL
        # The control: the same model with the rule an error is refused, the count naming that one finding.
        validator_package.patch_everywhere(monkeypatch, "WARNING_RULES", frozenset())
        with pytest.raises(allocation.ModelRefusedError) as refused:
            allocation.run_allocation(require_project(str(manifest)), file)
        assert refused.value.count == 1
        assert str(refused.value).startswith("the model is refused: check-model reports 1 error ")

    def test_a_candidate_whose_copy_carries_the_warning_alone_is_a_solution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, projection, _parsed = _setup(_with_an_unsatisfied_requirement(), tmp_path)
        assignment = {"B1": "P3"}  # one move, joining L0 on P3

        answer = allocation._Checker(root, projection)(assignment)

        assert isinstance(answer, allocation.Solution)
        assert _names(answer.moves) == {("B1", "P2", "P3")}
        # The copy the checker judged really carried the warning: the same move set, applied and checked here.
        copy = check_cross_layer_consistency(str(_apply(root, projection, answer.moves, tmp_path / "copy")))
        assert copy.consistent and copy.issues == []
        assert [(record.rule, record.subjects) for record in copy.warnings] == [
            (_WARNING_RULE, (Subject("requirements", _UNSATISFIED),))
        ]
        # The control: with the rule an error the same copy is a RefusedSet under that rule, the table missed.
        validator_package.patch_everywhere(monkeypatch, "WARNING_RULES", frozenset())
        refused = allocation._Checker(root, projection)(assignment)
        assert isinstance(refused, allocation.RefusedSet)
        assert refused.rules == (f"rule:{_WARNING_RULE}",)
        assert refused.reason == TABLE_MISSED_REASON

    def test_the_curve_is_the_unwarned_models_curve(self, tmp_path: Path) -> None:
        """A warning on every copy changes no verdict: the curve equals the unwarned fixture's."""
        _root, _projection, warned = _run(_with_an_unsatisfied_requirement(), tmp_path / "warned", max_moves=3)
        _root, _projection, unwarned = _run(three_cores(), tmp_path / "unwarned", max_moves=3)
        assert _without_timing(dataclasses.asdict(warned)) == _without_timing(dataclasses.asdict(unwarned))
        assert all(point.refused == () for point in warned.points)


def _apply(root: Path, projection: Projection, moves: tuple[Move, ...], destination: Path) -> Path:
    write_move_set(root, destination, projection, moves)
    return destination


# The implied edits, aggregated per touched task


def _edits(model: CarmaModel, tmp_path: Path, assignment: dict[str, str], **experiment: Any):
    root, projection, _parsed = _setup(model, tmp_path, **experiment)
    moves = moves_of(projection, link_sets(projection), assignment)
    edits = write_move_set(root, tmp_path / "copy", projection, moves)
    return {edit.task: edit for edit in edits}


class TestTheAggregatedEdits:
    def test_a_swap_is_one_edit_per_task_and_unchanged_where_the_costs_cancel(self, tmp_path: Path) -> None:
        edits = _edits(three_cores(), tmp_path, {"B1": "P3", "B5": "P2"})
        assert list(edits) == ["T_P2_10ms", "T_P3_10ms"]
        for edit in edits.values():
            assert (edit.outgoing, edit.incoming) == (Fraction(100, 10**6), Fraction(100, 10**6))
            assert (edit.outgoing_text, edit.incoming_text) == ("100us", "100us")
            assert edit.unchanged and edit.written == edit.original == "1000us"

    def test_several_moves_sharing_a_task_are_one_edit_each_side(self, tmp_path: Path) -> None:
        edits = _edits(three_cores(), tmp_path, {"B1": "P3", "B2": "P3"})
        assert list(edits) == ["T_P2_10ms", "T_P3_10ms"]
        assert (edits["T_P2_10ms"].outgoing_text, edits["T_P2_10ms"].written) == ("200us", "800us")
        assert (edits["T_P3_10ms"].incoming_text, edits["T_P3_10ms"].written) == ("200us", "1200us")

    def test_the_zero_rule_is_judged_on_the_final_value_not_per_move(self, tmp_path: Path) -> None:
        # X leaves TA (its only cost) while Y joins it: per move TA would reach zero, in total it does not.
        edits = _edits(zero_rule(), tmp_path, {"X": "PB", "Y": "PA"})
        assert edits["TA"].computed == Fraction(100, 10**6)
        assert not edits["TA"].kept_by_zero_rule and edits["TA"].unchanged and edits["TA"].written == "100us"

    def test_a_task_left_at_exactly_zero_keeps_its_budget(self, tmp_path: Path) -> None:
        edits = _edits(zero_rule(), tmp_path, {"X": "PB"}, locked=["Y", "W"])
        assert edits["TA"].computed == 0 and edits["TA"].kept_by_zero_rule
        assert edits["TA"].written == "100us" and edits["TA"].frees == "100us"
        written = load_yaml_document((tmp_path / "copy" / "tasks" / "TA.yaml").read_text(encoding="utf-8"))
        assert written["wcet"] == "100us" and written["runnables"] == []

    def test_the_copy_differs_only_in_the_touched_tasks_runnables_and_wcet(self, tmp_path: Path) -> None:
        root, projection, _parsed = _setup(three_cores(), tmp_path)
        moves = moves_of(projection, link_sets(projection), {"B1": "P3", "B6": "P2", "Q1": "P3"})
        copy = tmp_path / "copy"
        edits = write_move_set(root, copy, projection, moves)
        touched = {f"tasks/{edit.task}.yaml" for edit in edits}
        assert touched == {"tasks/T_P1_10ms.yaml", "tasks/T_P2_10ms.yaml", "tasks/T_P3_10ms.yaml"}
        sources = sorted((path for path in root.rglob("*") if path.is_file()), key=Path.as_posix)
        copies = sorted((path for path in copy.rglob("*") if path.is_file()), key=Path.as_posix)
        assert [p.relative_to(root).as_posix() for p in sources] == [p.relative_to(copy).as_posix() for p in copies]
        for source in sources:
            relative = source.relative_to(root).as_posix()
            if relative not in touched:
                assert filecmp.cmp(source, copy / relative, shallow=False), relative
                continue
            before = load_yaml_document(source.read_text(encoding="utf-8"))
            after = load_yaml_document((copy / relative).read_text(encoding="utf-8"))
            assert list(before) == list(after), relative
            differing = {key for key in before if before[key] != after[key]}
            assert differing <= {"runnables", "wcet"}, (relative, differing)
            assert "runnables" in differing


# The edits are exact, in the finest unit involved


def _respelled(model: CarmaModel, **budgets: str) -> CarmaModel:
    """*model* with each named task's ``wcet`` or component's one runnable ``wcet`` spelled as given."""
    for name, wcet in budgets.items():
        if f"tasks/{name}.yaml" in model.files:
            model.get(f"tasks/{name}.yaml")["wcet"] = wcet
        else:
            model.get(f"swc/carma/{name}.yaml")["runnables"][0]["wcet"] = wcet
    return model


class TestTheEditsInTheFinestUnit:
    def test_an_edit_is_written_in_the_finest_unit_among_the_task_and_the_moved_budgets(self, tmp_path: Path) -> None:
        model = _respelled(zero_rule(), X="100000ns", TB="7ms")
        edits = _edits(model, tmp_path, {"X": "PB"}, locked=["Y", "W"])
        tb = edits["TB"]
        assert (tb.original, tb.unit, tb.outgoing_text, tb.incoming_text) == ("7ms", "ns", "0ns", "100000ns")
        assert tb.written == "7100000ns" and not tb.unchanged and not tb.kept_by_zero_rule
        written = load_yaml_document((tmp_path / "copy" / "tasks" / "TB.yaml").read_text(encoding="utf-8"))
        assert written["wcet"] == "7100000ns"

    def test_the_zero_rule_keeps_the_task_s_own_text_and_states_the_freed_budget_in_the_finest_unit(
        self, tmp_path: Path
    ) -> None:
        model = _respelled(zero_rule(), X="100000ns")
        edits = _edits(model, tmp_path, {"X": "PB"}, locked=["Y", "W"])
        ta = edits["TA"]
        assert ta.kept_by_zero_rule and (ta.original, ta.written, ta.frees) == ("100us", "100us", "100000ns")
        written = load_yaml_document((tmp_path / "copy" / "tasks" / "TA.yaml").read_text(encoding="utf-8"))
        assert written["wcet"] == "100us"

    def test_an_unchanged_task_keeps_its_own_text_whatever_the_units_moved(self, tmp_path: Path) -> None:
        model = _respelled(three_cores(), B5="100000ns", T_P3_10ms="1ms")
        edits = _edits(model, tmp_path, {"B1": "P3", "B5": "P2"})
        p3 = edits["T_P3_10ms"]
        assert p3.unchanged and p3.unit == "ns" and p3.written == p3.original == "1ms"
        assert (p3.outgoing_text, p3.incoming_text) == ("100000ns", "100000ns")

    def test_the_report_and_result_json_state_every_budget_figure_as_a_text(self, tmp_path: Path) -> None:
        model = _respelled(zero_rule(), X="100000ns", TB="7ms")
        experiment = {"locked": ["Y", "W"], "max_moves": 2, "candidates": 1}
        _root, projection, curve = _run(model, tmp_path, **experiment)
        document = _document(projection, curve, **experiment)
        assert document["schemaVersion"] == 1
        edits = {edit["task"]: edit for edit in document["candidates"]["items"][0]["edits"]}
        assert {key: edits["TB"][key] for key in ("original", "outgoing", "incoming", "written", "frees")} == {
            "original": "7ms",
            "outgoing": "0ns",
            "incoming": "100000ns",
            "written": "7100000ns",
            "frees": "0ns",
        }
        markdown = _one_content(document)
        assert "- task TB (partition PB): wcet 7ms -> 7100000ns (-0ns +100000ns)" in markdown
        assert (
            "- task TA (partition PA): wcet keep 100us (the zero rule: no declared runnable budget is left in it, "
            "and the moves free 100000ns)"
        ) in markdown


class TestTheNetBudgetsAreNamed:
    def test_the_data_gaps_list_the_net_budgets_the_loads_sum(self, tmp_path: Path) -> None:
        model = zero_rule()
        model.get("swc/carma/X.yaml")["runnables"][0]["wcet_type"] = "net"
        model.get("swc/carma/Z.yaml")["runnables"][0]["wcet_type"] = "net"
        experiment = {"locked": ["Y", "W"], "max_moves": 1, "candidates": 1}
        _root, projection, curve = _run(model, tmp_path, **experiment)
        document = _document(projection, curve, **experiment)
        assert "budgets declared net, whose external calls no load counts: X.X_Run, Z.Z_Run" in document["dataGaps"]

    def test_no_net_budget_states_no_line(self, tmp_path: Path) -> None:
        experiment = {"locked": ["Y", "W"], "max_moves": 1, "candidates": 1}
        _root, projection, curve = _run(zero_rule(), tmp_path, **experiment)
        assert not [gap for gap in _document(projection, curve, **experiment)["dataGaps"] if "net" in gap]


class TestTheFactorCap:
    """An implied budget past 18 digits of its finest unit refuses its candidate, never the experiment."""

    def test_the_over_cap_move_set_writes_nothing_and_raises_with_its_edits(self, tmp_path: Path) -> None:
        root, projection, _parsed = _setup(budget_cap(), tmp_path)
        moves = moves_of(projection, link_sets(projection), {"A": "PB"})
        copy = tmp_path / "copy"
        with pytest.raises(BudgetCapExceeded) as caught:
            write_move_set(root, copy, projection, moves)
        assert not copy.exists(), "no file of the scratch copy is written for a refused candidate"
        edits = {edit.task: edit for edit in caught.value.edits}
        assert edits["TB"].written == "1000000000000000001ns"

    def test_the_candidate_is_refused_and_listed_and_the_run_goes_on(self, tmp_path: Path) -> None:
        experiment = {"locked": ["B"], "max_moves": 1, "candidates": 1}
        _root, projection, curve = _run(budget_cap(), tmp_path, **experiment)
        (refused,) = curve.point(1).refused
        assert _names(refused.moves) == {("A", "PA", "PB")}
        assert (refused.reason, refused.rules) == (BUDGET_CAP_REASON, ())
        assert refused.messages == ("task 'TB' would need wcet 1000000000000000001ns, more than 18 digits",)
        markdown = _one_content(_document(projection, curve, **experiment))
        assert (
            f"- at N = 1: A: PA -> PB; {BUDGET_CAP_REASON} (task 'TB' would need wcet 1000000000000000001ns, "
            "more than 18 digits)"
        ) in markdown

    def test_the_search_still_returns_the_other_candidate_whose_budget_is_exactly_eighteen_digits(
        self, tmp_path: Path
    ) -> None:
        experiment = {"max_moves": 1, "candidates": 1}
        root, projection, curve = _run(budget_cap(), tmp_path, **experiment)
        solution = curve.point(1).solution
        assert solution is not None and _names(solution.moves) == {("B", "PB", "PA")}
        assert all(refused.reason == BUDGET_CAP_REASON for refused in curve.point(1).refused)
        edits = {edit.task: edit for edit in solution.edits}
        assert edits["TB"].written == "999999999999999999ns"
        copy = tmp_path / "checked"
        write_move_set(root, copy, projection, solution.moves)
        assert check_cross_layer_consistency(str(copy)).consistent


# Fixture (e): the formulation


def _formulation(tmp_path: Path, **experiment: Any) -> tuple[Path, Projection, Any]:
    return _setup(formulation(), tmp_path, **{**FORMULATION_EXPERIMENT, "max_moves": 3, **experiment})


def _link_name(link) -> str:
    return link.connector


class TestTheFormulation:
    def test_the_two_sets_are_disjoint_and_every_costed_link(self, tmp_path: Path) -> None:
        _root, projection, _parsed = _formulation(tmp_path)
        sets = link_sets(projection)
        assert dict(sets.domains) == {"A": ("P1", "P2"), "D": ("P3", "P4"), "E": ("P2", "P1")}
        constant = {_link_name(link) for link in sets.constant}
        variable = {_link_name(link) for link in sets.variable}
        assert constant == {"AH", "ED", "GH"}
        assert variable == {"AE", "AG"}
        assert not constant & variable
        assert constant | variable == {_link_name(link) for link in projection.links if link.costed}
        assert set(FORMULATION_LINKS) == constant | variable

    def test_c0_counts_each_constant_crossing_link_once(self, tmp_path: Path) -> None:
        _root, projection, _parsed = _formulation(tmp_path)
        assert link_sets(projection).c0 == 3 * 400

    def test_the_constant_links_are_absent_from_the_solvers_objective(self, tmp_path: Path) -> None:
        _root, projection, _parsed = _formulation(tmp_path)
        search = SearchModel(scipy, projection, projection.sets, 3)
        names = {search.names[variable] for variable, _coefficient in search.objective_terms}
        # AG is 1 - x[A,P2]; AE is 1 - same[AE,P1] - same[AE,P2]. Nothing of AH, ED or GH.
        assert names == {"x[A,P2]", "same[0,P1]", "same[0,P2]"}
        assert not any("D" in name for name in names)
        # The objective adds the tie-break, sum moved = |F| - sum x[c, current(c)]: one term per search component.
        objective = {search.names[variable] for variable, coefficient in search.objective.items() if coefficient}
        assert objective == names | {"x[A,P1]", "x[D,P3]", "x[E,P2]"}

    def test_each_variable_link_crosses_exactly_when_its_partitions_differ(self, tmp_path: Path) -> None:
        _root, projection, _parsed = _formulation(tmp_path)
        sets = link_sets(projection)
        components = sorted(sets.domains)
        for chosen in itertools.product(*(sets.domains[name] for name in components)):
            assignment = dict(zip(components, chosen, strict=True))
            search = SearchModel(scipy, projection, projection.sets, 3)
            for name, partition in assignment.items():
                search.rows.append(allocation._Row(((search.x[(name, partition)], 1),), 1, 1))
            result = search.solve(UNREACHABLE_TIME_LIMIT_S)
            assert result.status == 0, result.message
            values = search.exact_values(result.x)
            assert values is not None
            for index, link in enumerate(sets.variable):
                differ = sets.partition_of(link.sender, assignment) != sets.partition_of(link.receiver, assignment)
                assert search.cross_value(index, values) == int(differ), (assignment, link.connector)

    def test_every_reported_objective_is_the_brute_force_over_every_costed_link(self, tmp_path: Path) -> None:
        root, projection, parsed = _formulation(tmp_path, candidates=10, tolerance=1.0)
        curve = solve_curve(root, projection, parsed)
        assert curve.current == _brute_force(projection, {}) == 2000
        solutions = [point.solution for point in curve.points if point.solution is not None]
        solutions += [candidate.solution for candidate in curve.candidates]
        assert len(solutions) > 3
        for solution in solutions:
            assert solution.objective == _brute_force(projection, dict(solution.assignment))


class TestTheToleranceBoundsScale:
    def test_c0_is_moved_right_before_the_floor(self, tmp_path: Path) -> None:
        _root, projection, curve = _run(tolerance_scale(), tmp_path, locked=["R", "S", "G"], max_moves=1)
        sets = link_sets(projection)
        assert sets.c0 == 1
        assert [link.cost for link in sets.variable] == [Fraction(2), Fraction(8)]
        search = SearchModel(scipy, projection, projection.sets, 1)
        assert search.objective_scale == Fraction(1, 2)
        optimum = curve.point(1).solution
        assert optimum is not None and optimum.objective == 3
        assert _names(optimum.moves) == {("M", "P1", "P3")}
        # floor(1/2 x (3 - 1)) = 1; a left side that kept C0 would be 3/2 at the optimum and exclude it.
        assert curve.tolerance_bound == 1
        assert search.objective_scale * sets.variable_sum(dict(optimum.assignment)) == 1
        assert curve.candidates[0].solution == optimum
        for candidate in curve.candidates:
            assert sets.c0 + sets.variable_sum(dict(candidate.solution.assignment)) <= optimum.objective


class TestTheIntegersRange:
    @pytest.mark.parametrize(("links", "refused"), [(True, "the objective"), (False, "core 'C1'")])
    def test_the_awkward_periods_are_refused_before_any_solve(
        self, tmp_path: Path, monkeypatch, links: bool, refused: str
    ) -> None:
        """The first part of the model whose integers exceed ``2**53`` refuses, by name, before any solve.

        With the links the objective, built first, refuses; without them C1's strict-core constraint does.
        """
        root, projection, parsed = _setup(awkward_periods(links=links), tmp_path, locked=["RA", "RB"])
        assert check_cross_layer_consistency(str(root)).consistent
        before = sorted(path.as_posix() for path in root.rglob("*"))

        def never(*_args: object) -> object:
            raise AssertionError("no solve may run")

        monkeypatch.setattr(allocation, "_run_solver", never)
        with pytest.raises(NotRepresentableError, match=NOT_REPRESENTABLE) as raised:
            solve_curve(root, projection, parsed)
        assert refused in str(raised.value)
        assert sorted(path.as_posix() for path in root.rglob("*")) == before


# The statuses, forced


def _forcing(monkeypatch, answer):
    """Replace the solver's status by *answer(name, assignment, search)*, keeping its real solve underneath."""
    real = allocation._run_solver

    def fake(search, max_time_in_seconds):
        name, assignment = real(search, max_time_in_seconds)
        return answer(name, assignment, search)

    monkeypatch.setattr(allocation, "_run_solver", fake)


def _answering(monkeypatch, answer):
    """Replace ``milp``'s result by *answer(result, search)*, keeping the real solve underneath."""
    real = allocation.SearchModel.solve

    def fake(search, time_limit):
        return answer(real(search, time_limit), search)

    monkeypatch.setattr(allocation.SearchModel, "solve", fake)


def _result(status: int, message: str, x: Any) -> Any:
    return scipy.optimize.OptimizeResult(status=status, message=message, x=x)


#: ``milp``'s message for HiGHS's model error, which shares status 2 with a proved infeasibility.
_MODEL_ERROR = "(HiGHS Status 2: Model error)"


class TestTheSolversAnswersAreMapped:
    """``milp``'s statuses and solutions, forced at the solver's result: the mapping ``_run_solver`` owns."""

    @pytest.mark.parametrize(
        ("forced", "match"),
        [
            (lambda result, _search: _result(4, "(HiGHS Status 13: Solve error)", None), "status 4"),
            (lambda result, _search: _result(3, "The problem is unbounded.", None), "status 3"),
            (lambda result, _search: _result(2, _MODEL_ERROR, None), "without a proof of infeasibility"),
            (lambda result, _search: _result(0, "", None), "status 0 without a solution"),
            (lambda result, _search: _result(0, "", [0.5] * len(result.x)), "not an exact 0/1"),
            (lambda result, _search: _result(0, "", [float("nan")] * len(result.x)), "not an exact 0/1"),
            (lambda result, _search: _result(0, "", [1e-3, *result.x[1:]]), "not an exact 0/1"),
            # Integral, but every placement row is violated: the exact re-check refuses what rounding accepts.
            (lambda result, _search: _result(0, "", [0.0] * len(result.x)), "not an exact 0/1"),
            (lambda result, _search: _result(1, "", [1.0] * len(result.x)), "not an exact 0/1"),
        ],
    )
    def test_an_error_status_or_an_inexact_solution_is_an_execution_failure(
        self, tmp_path: Path, monkeypatch, forced, match: str
    ) -> None:
        _answering(monkeypatch, forced)
        root, projection, parsed = _setup(three_cores(), tmp_path)
        with pytest.raises(SolverExecutionError, match=match):
            solve_curve(root, projection, parsed)

    def test_a_proved_infeasibility_is_the_infeasible_status(self, tmp_path: Path, monkeypatch) -> None:
        _answering(
            monkeypatch, lambda _result_, _search: _result(2, "The problem is infeasible. (HiGHS Status 8)", None)
        )
        _root, _projection, curve = _run(three_cores(), tmp_path)
        assert all(point.status == STATUS_INFEASIBLE and not point.cut_short for point in curve.points)

    def test_a_limit_without_a_solution_is_unknown(self, tmp_path: Path, monkeypatch) -> None:
        _answering(monkeypatch, lambda _result_, _search: _result(1, "Time limit reached.", None))
        _root, _projection, curve = _run(three_cores(), tmp_path)
        assert all(point.status == STATUS_UNKNOWN and point.solution is None for point in curve.points)

    def test_a_limit_with_a_solution_is_feasible(self, tmp_path: Path, monkeypatch) -> None:
        _answering(monkeypatch, lambda result, _search: _result(1, "Time limit reached.", result.x))
        _root, _projection, curve = _run(three_cores(), tmp_path)
        assert all(point.status == STATUS_FEASIBLE and point.cut_short for point in curve.points)

    def test_a_value_within_the_tolerance_is_read_as_its_integer(self, tmp_path: Path, monkeypatch) -> None:
        def nudged(result, _search):
            return _result(0, "", [value + 1e-7 if value < 0.5 else value - 1e-7 for value in result.x])

        _answering(monkeypatch, nudged)
        _root, _projection, curve = _run(three_cores(), tmp_path)
        for n, (value, _sets) in HAND_OPTIMUM.items():
            assert curve.point(n).status == STATUS_OPTIMAL
            assert curve.point(n).solution is not None and curve.point(n).solution.objective == value

    def test_milp_is_handed_the_fixed_options_and_the_remaining_time(self, tmp_path: Path, monkeypatch) -> None:
        received: list[dict[str, Any]] = []
        real = scipy.optimize.milp

        def recording(*args: Any, **kwargs: Any) -> Any:
            received.append(dict(kwargs["options"]))
            return real(*args, **kwargs)

        monkeypatch.setattr(scipy.optimize, "milp", recording)
        _run(three_cores(), tmp_path, time_limit_s=45)
        assert received and all(set(options) == {"mip_rel_gap", "disp", "time_limit"} for options in received)
        assert all(options["mip_rel_gap"] == 0 and options["disp"] is False for options in received)
        assert all(0 < options["time_limit"] <= 45 for options in received)


class TestTheStatuses:
    def test_an_answer_that_is_no_status_is_an_execution_failure(self, tmp_path: Path, monkeypatch) -> None:
        _forcing(monkeypatch, lambda _name, _assignment, _search: ("status 4 (Solve error)", None))
        root, projection, parsed = _setup(three_cores(), tmp_path)
        with pytest.raises(SolverExecutionError, match="status 4"):
            solve_curve(root, projection, parsed)
        assert "status 4" not in CURVE_STATUSES

    def test_unknown_has_no_move_set_and_no_candidates(self, tmp_path: Path, monkeypatch) -> None:
        _forcing(monkeypatch, lambda _name, _assignment, _search: ("UNKNOWN", None))
        _root, _projection, curve = _run(three_cores(), tmp_path)
        assert all(point.status == STATUS_UNKNOWN for point in curve.points)
        assert all(point.solution is None and point.cut_short for point in curve.points)
        assert curve.candidates == () and curve.enumeration is None

    def test_feasible_is_an_upper_bound_with_one_candidate_and_no_tolerance_claim(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _forcing(monkeypatch, lambda _name, assignment, _search: ("FEASIBLE", assignment))
        _root, _projection, curve = _run(three_cores(), tmp_path / "forced")
        assert all(point.status == STATUS_FEASIBLE and point.cut_short for point in curve.points)
        for n, (value, _sets) in HAND_OPTIMUM.items():
            assert curve.point(n).solution is not None and curve.point(n).solution.objective >= value
        (candidate,) = curve.candidates
        assert candidate.solution == curve.point(3).solution
        assert not candidate.within_tolerance
        assert curve.enumeration is None and curve.tolerance_bound is None

    def test_infeasible_is_a_curve_status(self, tmp_path: Path, monkeypatch) -> None:
        _forcing(monkeypatch, lambda _name, _assignment, _search: ("INFEASIBLE", None))
        _root, _projection, curve = _run(three_cores(), tmp_path)
        assert all(point.status == STATUS_INFEASIBLE for point in curve.points)
        assert all(point.solution is None and not point.cut_short for point in curve.points)
        assert curve.candidates == ()

    def test_an_error_while_enumerating_is_an_execution_failure(self, tmp_path: Path, monkeypatch) -> None:
        """The chosen point's own solve answers; the enumeration's first re-solve is the one that fails."""
        real = allocation._run_solver
        calls: list[int] = []

        def fake(search, max_time_in_seconds):
            calls.append(search.n)
            if search.n == 3 and calls.count(3) > 1:
                return "status 4 (Solve error)", None
            return real(search, max_time_in_seconds)

        monkeypatch.setattr(allocation, "_run_solver", fake)
        root, projection, parsed = _setup(three_cores(), tmp_path)
        with pytest.raises(SolverExecutionError, match="enumerating the candidates at N = 3"):
            solve_curve(root, projection, parsed)

    def test_every_solve_of_a_point_gets_the_remaining_time_not_a_fresh_limit(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Each solve costs one second on a clock the test owns: the limits it receives count down from the point's."""
        now = [0.0]
        monkeypatch.setattr(allocation, "_monotonic", lambda: now[0])
        received: dict[int, list[float]] = {}
        real = allocation._run_solver

        def recording(search, max_time_in_seconds):
            received.setdefault(search.n, []).append(max_time_in_seconds)
            answer = real(search, max_time_in_seconds)
            now[0] += 1
            return answer

        monkeypatch.setattr(allocation, "_run_solver", recording)
        _run(three_cores(), tmp_path, candidates=10, time_limit_s=1000)
        chosen = received[3]
        assert len(chosen) >= 5  # the point's solve and the enumeration's
        assert chosen == [1000 - index for index in range(len(chosen))]

    def test_a_clock_that_has_not_moved_never_hands_the_solver_more_than_the_limit(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A clock that has not moved never hands the solver more than the limit.

        Windows' ``time.monotonic`` ticks about every 16 ms, so a fast solve can read the point's start, and
        ``(started + 45) - started`` rounds above 45.
        """
        frozen = 8388564.178766354
        assert (frozen + 45) - frozen > 45  # the rounding this case stands on
        monkeypatch.setattr(allocation, "_monotonic", lambda: frozen)
        received: list[float] = []
        real = allocation._run_solver
        made = [0]

        def recording(search, max_time_in_seconds):
            received.append(max_time_in_seconds)
            made[0] += 1
            if made[0] > 20:  # the frozen clock never spends the deadline: end the point here
                monkeypatch.setattr(allocation, "_monotonic", lambda: frozen + 1_000)
            return real(search, max_time_in_seconds)

        monkeypatch.setattr(allocation, "_run_solver", recording)
        _run(three_cores(), tmp_path, time_limit_s=45)
        assert received and all(0 < limit <= 45 for limit in received)

    @pytest.mark.parametrize("solves", [1, 2, 3])
    def test_a_spent_deadline_cuts_the_enumeration_short(self, tmp_path: Path, monkeypatch, solves: int) -> None:
        """The clock jumps past the point's deadline after the *solves*-th solve: one candidate per earlier solve."""
        now = [0.0]
        monkeypatch.setattr(allocation, "_monotonic", lambda: now[0])
        real = allocation._run_solver
        made: list[int] = []

        def fake(search, max_time_in_seconds):
            answer = real(search, max_time_in_seconds)
            if search.n == 3:
                made.append(3)
                if len(made) == solves:
                    now[0] += 10_000
            return answer

        monkeypatch.setattr(allocation, "_run_solver", fake)
        _root, projection, curve = _run(three_cores(), tmp_path, candidates=10, time_limit_s=45)
        assert curve.point(3).status == STATUS_OPTIMAL
        assert curve.enumeration == allocation.ENUMERATION_CUT_SHORT
        assert len(curve.candidates) == solves
        gaps = _document(projection, curve)["dataGaps"]
        assert any(gap.startswith("a time limit cut the candidate enumeration at N = 3 short") for gap in gaps)

    @pytest.mark.parametrize(("feasible_at", "candidates"), [(3, 3), (2, 10)])
    def test_a_feasible_answer_ends_the_enumeration_cut_short(
        self, tmp_path: Path, monkeypatch, feasible_at: int, candidates: int
    ) -> None:
        """A ``FEASIBLE`` answer while time remains is kept and ends the enumeration.

        No further solve runs and no superset no-good is drawn from it.
        """
        real = allocation._run_solver
        made: list[int] = []

        def fake(search, max_time_in_seconds):
            name, assignment = real(search, max_time_in_seconds)
            if search.n != 3:
                return name, assignment
            made.append(3)
            if len(made) == feasible_at:
                assert name == "OPTIMAL" and assignment is not None
                return "FEASIBLE", assignment
            return name, assignment

        supersets: list[dict[str, str]] = []
        real_superset = allocation.SearchModel.add_superset_no_good

        def recording(search, assignment):
            supersets.append(dict(assignment))
            return real_superset(search, assignment)

        monkeypatch.setattr(allocation, "_run_solver", fake)
        monkeypatch.setattr(allocation.SearchModel, "add_superset_no_good", recording)
        _root, projection, curve = _run(three_cores(), tmp_path, candidates=candidates, time_limit_s=1000)
        assert curve.point(3).status == STATUS_OPTIMAL
        assert len(made) == feasible_at  # no solve after the feasible answer
        assert len(curve.candidates) == feasible_at
        assert all(candidate.within_tolerance for candidate in curve.candidates)
        assert len(supersets) == feasible_at - 1  # the optimum's and the proved ones', never the forced set's
        assert curve.enumeration == allocation.ENUMERATION_CUT_SHORT
        gaps = _document(projection, curve, candidates=candidates, time_limit_s=1000)["dataGaps"]
        assert any(gap.startswith("a time limit cut the candidate enumeration at N = 3 short") for gap in gaps)


class TestTheMissingSolver:
    def test_the_refusal_names_the_solver(self, tmp_path: Path, monkeypatch) -> None:
        root, projection, parsed = _setup(three_cores(), tmp_path)
        monkeypatch.setitem(sys.modules, "scipy", None)
        with pytest.raises(SolverMissingError, match="'scipy'") as raised:
            solve_curve(root, projection, parsed)
        assert not isinstance(raised.value, allocation.SolverUnloadableError)
        assert "pip install 'scipy>=1.17.1'" in str(raised.value)

    @pytest.mark.parametrize(
        "error",
        [
            ImportError("DLL load failed while importing _core: The specified module could not be found."),
            OSError("[WinError 126] The specified module could not be found: 'C:/somewhere/scipy/_core.pyd'"),
        ],
    )
    def test_a_solver_that_will_not_load_is_refused_in_one_sentence_without_a_path(
        self, tmp_path: Path, monkeypatch, error: BaseException
    ) -> None:
        root, projection, parsed = _setup(three_cores(), tmp_path)
        unloadable_solver(monkeypatch, error)
        with pytest.raises(allocation.SolverUnloadableError) as raised:
            solve_curve(root, projection, parsed)
        sentence = str(raised.value)
        assert isinstance(raised.value, SolverMissingError)
        assert "'scipy'" in sentence and "cannot be loaded" in sentence
        assert (
            "\n" not in sentence and "somewhere" not in sentence and "/" not in sentence.replace("'scipy>=1.17.1'", "")
        )


# The lexicographic objective: the volume, then the fewest moves, in one solve


def _moved(curve: Curve) -> list[frozenset[tuple[str, str, str]]]:
    return [_names(point.solution.moves) for point in curve.points if point.solution is not None]


class TestTheLexicographicObjective:
    def test_a_move_that_buys_no_volume_is_never_part_of_a_point(self, tmp_path: Path) -> None:
        _root, projection, curve = _run(unlinked_free(), tmp_path, locked=["X"], max_moves=4)
        assert len(projection.sets.domains) == 1 + len(UNLINKED_FREE_IDLE)
        assert curve.current == 400
        assert [point.status for point in curve.points] == [STATUS_OPTIMAL] * 4
        assert _moved(curve) == [frozenset({("A", "P1", "P2")})] * 4
        assert all(point.solution is not None and point.solution.objective == 0 for point in curve.points)

    def test_no_link_that_can_be_joined_is_no_improvement(self, tmp_path: Path) -> None:
        _root, projection, curve = _run(unlinked_free(), tmp_path, locked=["X", "A"], max_moves=3)
        assert _moved(curve) == [frozenset()] * 3
        assert all(point.solution is not None and point.solution.objective == 400 for point in curve.points)
        assert curve.candidates == () and curve.enumeration is None
        parsed = parse_config(config_document(name="relieve", locked=["X", "A"], max_moves=3))
        assert "no improvement at any N" in allocation.summary_line(parsed, projection, curve)

    def test_a_model_without_volume_moves_nothing_at_any_n(self, tmp_path: Path) -> None:
        _root, projection, curve = _run(unlinked_free(connector=False), tmp_path, locked=["X"], max_moves=3)
        assert curve.current == 0 and projection.links == ()
        assert _moved(curve) == [frozenset()] * 3
        assert curve.candidates == () and curve.enumeration is None

    def test_a_superset_padded_by_volume_neutral_moves_is_never_a_candidate(self, tmp_path: Path) -> None:
        """``[A]`` alone: ``[A, U2]`` and every other padding of the optimum is excluded by the superset no-good."""
        _root, _projection, curve = _run(unlinked_free(), tmp_path, locked=["X"], max_moves=2, candidates=4)
        assert [_names(candidate.solution.moves) for candidate in curve.candidates] == [{("A", "P1", "P2")}]
        assert curve.enumeration == ENUMERATION_EXHAUSTED

    def test_the_superset_no_good_excludes_the_padded_sets_the_exact_one_lets_through(self, tmp_path: Path) -> None:
        _root, projection, _parsed = _setup(unlinked_free(), tmp_path, locked=["X"], max_moves=2)
        only_a = {**SearchModel(scipy, projection, projection.sets, 2).current, "A": "P2"}
        exact = SearchModel(scipy, projection, projection.sets, 2)
        exact.add_no_good(only_a)
        _status, padded = allocation._run_solver(exact, UNREACHABLE_TIME_LIMIT_S)
        assert padded is not None and padded["A"] == "P2" and padded != only_a
        superset = SearchModel(scipy, projection, projection.sets, 2)
        superset.add_superset_no_good(only_a)
        _status, answer = allocation._run_solver(superset, UNREACHABLE_TIME_LIMIT_S)
        assert answer is not None and answer["A"] == "P1"

    def test_the_candidates_are_ordered_by_volume_then_by_move_count(self, tmp_path: Path) -> None:
        _root, _projection, curve = _run(three_cores(), tmp_path, candidates_at=2, candidates=20, tolerance=0.5)
        keys = [allocation.candidate_order(candidate.solution) for candidate in curve.candidates[1:]]
        assert len(keys) > 1 and keys == sorted(keys)

    def test_a_feasible_incumbent_that_buys_nothing_counts_as_moving_nothing(self, tmp_path: Path, monkeypatch) -> None:
        """A forced ``feasible`` answer that moves ``U0`` and changes no volume is the empty move set."""
        _forcing(monkeypatch, lambda _name, assignment, _search: ("FEASIBLE", {**assignment, "U0": "P2"}))
        _root, _projection, curve = _run(unlinked_free(), tmp_path, locked=["X", "A"], max_moves=1)
        point = curve.point(1)
        assert point.status == STATUS_FEASIBLE and point.solution is not None
        assert point.solution.moves == () and point.solution.objective == curve.current
        assert curve.candidates == ()


class TestThePointsAboveTheSearchComponents:
    def test_they_are_neither_solved_nor_built(self, tmp_path: Path, monkeypatch) -> None:
        real = allocation._run_solver
        solved: list[int] = []
        built: list[int] = []

        def recording(search, max_time_in_seconds):
            solved.append(search.n)
            return real(search, max_time_in_seconds)

        real_point = allocation.CurvePoint

        def counting(*args, **kwargs):
            point = real_point(*args, **kwargs)
            built.append(point.n)
            return point

        monkeypatch.setattr(allocation, "_run_solver", recording)
        monkeypatch.setattr(allocation, "CurvePoint", counting)
        _root, projection, curve = _run(tolerance_exact(), tmp_path, locked=["X", "Y"], max_moves=6, candidates=5)
        assert len(projection.sets.domains) == 2
        assert set(solved) == {1, 2} and built == [1, 2]
        assert [point.n for point in curve.points] == [1, 2]
        assert (curve.max_moves, curve.last_solved) == (6, 2)
        with pytest.raises(IndexError):
            curve.point(3)
        # The candidates asked for at N = 6 are enumerated at N = 2, the same model, and say so.
        assert curve.candidates_at == 2 and curve.enumeration == ENUMERATION_EXHAUSTED
        assert curve.candidates[0].solution == curve.point(2).solution

    def test_a_max_moves_far_above_the_components_costs_only_its_sentence(self, tmp_path: Path, monkeypatch) -> None:
        """``max_moves: 10**9`` gives the report of ``max_moves: |F|`` plus the parameters and one sentence."""
        experiment = {"locked": ["X", "Y"], "candidates": 5}
        _root, projection, bounded = _run(tolerance_exact(), tmp_path / "bounded", max_moves=2, **experiment)
        solved: list[int] = []
        real = allocation._run_solver

        def recording(search, max_time_in_seconds):
            solved.append(search.n)
            return real(search, max_time_in_seconds)

        monkeypatch.setattr(allocation, "_run_solver", recording)
        _root, projection, huge = _run(tolerance_exact(), tmp_path / "huge", max_moves=10**9, **experiment)
        assert set(solved) == {1, 2} and len(huge.points) == 2
        small = _document(projection, bounded, max_moves=2, **experiment)
        large = _document(projection, huge, max_moves=10**9, **experiment)
        assert (small["lastSolved"], large["lastSolved"]) == (2, 2)
        assert [point["n"] for point in large["curve"]] == [1, 2]
        assert large["fingerprint"]["parameters"]["search"]["max_moves"] == 10**9
        assert large["candidates"]["at"] == 2 and large["summary"] == small["summary"]
        sentence = (
            "N = 3 … 1000000000: the same as N = 2 (no component left to move: a move set never moves more "
            "components than the search may move, so a larger N allows nothing new)."
        )
        expected = (
            _one_content(small)
            .replace("- search.max_moves: 2\n", "- search.max_moves: 1000000000\n")
            .replace("- search.candidates_at: 2\n", "- search.candidates_at: 1000000000\n")
            .replace("\n## Candidates\n", f"\n{sentence}\n\n## Candidates\n")
        )
        assert _one_content(large) == expected
        assert sentence not in _one_content(small)

    def test_one_point_above_is_named_alone(self, tmp_path: Path) -> None:
        experiment = {"locked": ["X", "Y"], "max_moves": 3, "candidates": 1}
        _root, projection, curve = _run(tolerance_exact(), tmp_path, **experiment)
        assert "\nN = 3: the same as N = 2 (no component left to move" in _one_content(
            _document(projection, curve, **experiment)
        )


# A refused set, carried forward


class TestARefusedSetAtEveryLaterN:
    def test_it_is_never_proposed_again(self, tmp_path: Path) -> None:
        _root, _projection, curve = _run(mixed_task_with_idle(), tmp_path, max_moves=3)
        assert [_names(refused.moves) for refused in curve.point(2).refused] == [{("B", "P2", "P4"), ("Q", "P1", "P4")}]
        # Without the carried no-good N = 3 would propose {Q, B} first and list it again.
        assert [_names(refused.moves) for refused in curve.point(3).refused] == [
            {("B", "P2", "P4"), ("Q", "P1", "P4"), ("V", "P1", "P4")}
        ]
        assert curve.point(3).solution is not None and curve.point(3).solution.moves == ()


# The zero rule's classification


def _refusal_with(monkeypatch, tmp_path: Path, extra) -> allocation.RefusedSet:
    """``{X -> PB}`` on :func:`zero_rule`, the gate's real answer edited by *extra(records)*."""
    real = allocation.check_cross_layer_consistency

    def edited(model_root: str, arxml_version: str | None = None):
        result = real(model_root, arxml_version)
        records = extra(result.records)
        return dataclasses.replace(result, consistent=not records, records=tuple(records))

    monkeypatch.setattr(allocation, "check_cross_layer_consistency", edited)
    root, projection, _parsed = _setup(zero_rule(), tmp_path, locked=["Y", "W"])
    refused = allocation._Checker(root, projection)({"X": "PB", "Z": "PA"})
    assert isinstance(refused, allocation.RefusedSet)
    return refused


class TestTheZeroRuleClassification:
    def test_the_pure_case_is_the_zero_rule(self, tmp_path: Path, monkeypatch) -> None:
        refused = _refusal_with(monkeypatch, tmp_path, list)
        assert refused.reason == ZERO_RULE_REASON

    def test_a_zero_rule_breach_beside_another_rule_is_not_the_zero_rule(self, tmp_path: Path, monkeypatch) -> None:
        other = ConsistencyIssue("safety.task-mixed-asil", "synthetic", (Subject("tasks", "TB"),))
        refused = _refusal_with(monkeypatch, tmp_path, lambda records: [*records, other])
        assert refused.reason == TABLE_MISSED_REASON
        assert refused.rules == ("rule:hardware.threshold-breach", "rule:safety.task-mixed-asil")

    def test_a_breach_on_a_core_holding_no_zero_rule_task_is_not_the_zero_rule(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        def elsewhere(records):
            return [dataclasses.replace(record, subjects=(Subject("hardware", "C2"),)) for record in records]

        refused = _refusal_with(monkeypatch, tmp_path, elsewhere)
        assert refused.rules == ("rule:hardware.threshold-breach",)
        assert refused.reason == TABLE_MISSED_REASON


# The tolerance, taken exactly


class TestTheToleranceTakenExactly:
    def test_a_candidate_on_the_bound_of_a_tolerance_no_float_spells_is_kept(self, tmp_path: Path) -> None:
        experiment = {"locked": ["X", "Y"], "max_moves": 1, "candidates": 5, "tolerance": 0.3}
        _root, projection, curve = _run(tolerance_exact(), tmp_path, **experiment)
        assert Fraction(0.3) < Fraction(3, 10)  # the float is below the decimal the file wrote
        assert [candidate.solution.objective for candidate in curve.candidates] == [100, 130]
        assert curve.candidates[1].solution.objective == curve.candidates[0].solution.objective * Fraction(13, 10)
        # s = 1/10 (the coefficients 130 and 100 over their gcd): floor(1/10 x 100 x 13/10) = 13, exactly.
        assert curve.tolerance_bound == 13
        parameters = _document(projection, curve, **experiment)["fingerprint"]["parameters"]
        assert parameters["search"]["tolerance"] == {"n": 3, "d": 10}


# Task entries spelled by instance


def _by_instance() -> CarmaModel:
    """Fixture (a), ``B1`` placed as the member ``b1_main`` and named by it in its task."""
    model = three_cores()
    model.root_members["B1"] = {"name": "b1_main", "type": "B1"}
    entries = model.get("tasks/T_P2_10ms.yaml")["runnables"]
    entries[entries.index({"swc": "B1", "runnable": "B1_Run"})] = {"instance": "b1_main", "runnable": "B1_Run"}
    return model


class TestTaskEntriesSpelledByInstance:
    def test_the_placement_is_derived_through_the_instance(self, tmp_path: Path) -> None:
        root, projection, _parsed = _setup(_by_instance(), tmp_path)
        assert check_cross_layer_consistency(str(root)).consistent
        placement = projection.placements["B1"]
        assert placement.free and placement.partition == "P2" and placement.core == "C2"

    def test_a_move_carries_the_entry_in_its_own_form_to_a_legal_copy(self, tmp_path: Path) -> None:
        root, projection, _parsed = _setup(_by_instance(), tmp_path)
        moves = moves_of(projection, projection.sets, {"B1": "P3"})
        copy = tmp_path / "copy"
        write_move_set(root, copy, projection, moves)
        source = load_yaml_document((copy / "tasks" / "T_P2_10ms.yaml").read_text(encoding="utf-8"))
        target = load_yaml_document((copy / "tasks" / "T_P3_10ms.yaml").read_text(encoding="utf-8"))
        assert {"instance": "b1_main", "runnable": "B1_Run"} not in source["runnables"]
        assert {"instance": "b1_main", "runnable": "B1_Run"} in target["runnables"]
        assert check_cross_layer_consistency(str(copy)).consistent

    def test_the_target_tasks_levels_read_the_instance_entries(self, tmp_path: Path) -> None:
        """``Q2`` (QM) may not join ``T_P2_10ms``, whose B-rated ``B1`` is named by its instance."""
        projection = _setup(_by_instance(), tmp_path)[1]
        target = projection.row("Q2").target("P2")
        assert "rule:safety.task-mixed-asil" in [reason.rule for reason in target.reasons]
        assert any("'B1' (ASIL B)" in reason.text for reason in target.reasons)


# Initialization and server runnables in no task

#: The fixture (a) components other than ``B5``: locked, so ``B5 -> P2`` is the one move that buys volume at N = 1.
_ALL_BUT_B5 = sorted(name for names in THREE_CORES_LAYOUT.values() for name in names if name != "B5")
_B5_INIT_ASSUMPTION = (
    "initialization runnable 'B5_Init' of 'B5' runs in no task: activated once at start "
    "(`Rte_Init` or an initialization task), outside the steady-state load"
)


def _with_unmapped_init() -> CarmaModel:
    """Fixture (a), ``B5`` given an initialization runnable in no task and without ``wcet``."""
    model = three_cores()
    model.get("swc/carma/B5.yaml")["runnables"].append(init_runnable("B5_Init", wcet=None))
    return model


class TestInitializationAndServerRunnables:
    def test_a_component_with_an_unmapped_initialization_runnable_moves_to_a_legal_copy(self, tmp_path: Path) -> None:
        experiment = {"locked": _ALL_BUT_B5, "max_moves": 1, "candidates": 1}
        root, projection, curve = _run(_with_unmapped_init(), tmp_path, **experiment)
        assert check_cross_layer_consistency(str(root)).consistent
        placement = projection.placements["B5"]
        assert placement.free and placement.partition == "P3"
        assert [each.runnable for each in placement.runnables] == ["B5_Run"]
        point = curve.point(1)
        assert point.status == STATUS_OPTIMAL and point.solution is not None
        assert _names(point.solution.moves) == {("B5", "P3", "P2")}
        assert point.solution.objective == Fraction(800) < curve.current == Fraction(1200)
        result = _apply_and_check(root, projection, point.solution.moves, tmp_path / "copy")
        assert result.consistent, result.issues
        copied = load_yaml_document((tmp_path / "copy" / "tasks" / "T_P2_10ms.yaml").read_text(encoding="utf-8"))
        assert {"swc": "B5", "runnable": "B5_Run"} in copied["runnables"]
        assert not any(entry["runnable"] == "B5_Init" for entry in copied["runnables"])
        document = _document(projection, curve, **experiment)
        assert _B5_INIT_ASSUMPTION in document["dataGaps"]
        markdown = _one_content(document)
        gaps = markdown[markdown.index(f"## {allocation.SECTION_DATA_GAPS}") :]
        assert f"- {_B5_INIT_ASSUMPTION}" in gaps
        assert "declares no wcet" not in markdown

    def test_a_component_with_an_unmapped_server_runnable_never_moves_and_its_call_is_counted(
        self, tmp_path: Path
    ) -> None:
        root, projection, curve = _run(with_unmapped_server(wcet=None), tmp_path)
        assert check_cross_layer_consistency(str(root)).consistent
        placement = projection.placements["B1"]
        assert not placement.free and placement.partition == "P2"
        assert projection.row("B1").domain == ()
        assert projection.current_calls == 1
        for n in (1, 2, 3):
            point = curve.point(n)
            assert point.solution is not None
            assert "B1" not in {move.component for move in point.solution.moves}
        markdown = _one_content(_document(projection, curve))
        assert "Inter-partition calls: 1." in markdown

    def test_a_free_caller_of_an_unmapped_server_may_still_move(self, tmp_path: Path) -> None:
        """What the unmapped-runnable rule does not reach: the caller ``Q1`` stays free and may move away from ``B1``.

        Its call is then counted as crossing and nothing more -- its legality is not judged, and the
        server's cost in the caller's task is added to no share.
        """
        projection = _setup(with_unmapped_server(wcet="100us"), tmp_path)[1]
        caller = projection.placements["Q1"]
        assert caller.free and caller.partition == "P1"
        assert projection.row("Q1").domain == ("P1", "P3")
        assert [each.runnable for each in caller.runnables] == ["Q1_Run"]
        assert caller.share == projection.placements["Q2"].share
        assert allocation._crossing_calls(projection, projection.sets, {"Q1": "P3"}) == 1


# The report's document, beyond the all-optimal run


def _document(projection: Projection, curve: Curve, **experiment: Any) -> dict[str, Any]:
    parsed = parse_config(config_document(name="relieve", **experiment))
    fingerprint = allocation.Fingerprint("sha256:0", 0, "0", curve.solver_version)
    return allocation.result_document(
        parsed, projection, curve, fingerprint, output="dist/experiments/relieve", configuration=None
    )


def _one_content(document: dict[str, Any]) -> str:
    """``report.md`` rendered from the document, and equal to the one rendered from its JSON text."""
    markdown = allocation.render_markdown(document)
    assert allocation.render_markdown(json.loads(allocation.result_json(document))) == markdown
    return markdown


class TestTheReportDocument:
    def test_unknown_points_have_no_move_set_and_their_note(self, tmp_path: Path, monkeypatch) -> None:
        _forcing(monkeypatch, lambda _name, _assignment, _search: ("UNKNOWN", None))
        _root, projection, curve = _run(three_cores(), tmp_path)
        document = _document(projection, curve)
        assert document["summary"].endswith(
            "no checked move set at any N (N = 1: no move set (unknown); N = 2: no move set (unknown); "
            "N = 3: no move set (unknown))."
        )
        markdown = _one_content(document)
        assert "| 1 | unknown | — | — | cut short by the time limit: may differ on another run |" in markdown
        assert "None at N = 3." in markdown
        assert any(gap.startswith("a time limit cut the search at N = 2 short") for gap in document["dataGaps"])

    def test_a_feasible_candidate_carries_no_tolerance_claim(self, tmp_path: Path, monkeypatch) -> None:
        _forcing(monkeypatch, lambda _name, assignment, _search: ("FEASIBLE", assignment))
        _root, projection, curve = _run(three_cores(), tmp_path)
        document = _document(projection, curve)
        (item,) = document["candidates"]["items"]
        assert item["tolerance"] == allocation.NO_TOLERANCE_CLAIM
        assert f"### Candidate 1: 0 bytes x Hz ({allocation.NO_TOLERANCE_CLAIM})" in _one_content(document)

    def test_a_dropped_set_is_listed_with_its_rule(self, tmp_path: Path) -> None:
        _root, projection, curve = _run(mixed_task(), tmp_path, max_moves=2)
        document = _document(projection, curve, max_moves=2)
        (dropped,) = document["dropped"]
        assert (dropped["foundAt"], dropped["rules"]) == (2, ["rule:safety.task-mixed-asil"])
        assert (
            "- at N = 2: B: P2 -> P4; Q: P1 -> P4; a rule the freedom table did not foresee "
            "(rule:safety.task-mixed-asil)"
        ) in _one_content(document)

    def test_a_dropped_set_is_listed_once_wherever_it_was_found(self, tmp_path: Path) -> None:
        _root, _projection, curve = _run(mixed_task(), tmp_path, max_moves=2)
        doubled = dataclasses.replace(curve, refused=curve.point(2).refused)
        assert len(allocation._dropped(doubled)) == 1

    def test_the_zero_rule_edit_is_stated_as_the_budget_it_frees(self, tmp_path: Path) -> None:
        experiment = {"locked": ["Y", "W"], "max_moves": 2, "candidates": 1}
        _root, projection, curve = _run(zero_rule(), tmp_path, **experiment)
        markdown = _one_content(_document(projection, curve, **experiment))
        assert (
            "- task TA (partition PA): wcet keep 100us (the zero rule: no declared runnable budget is left in it, "
            "and the moves free 100us)"
        ) in markdown
        dropped = "- at N = 1: X: PA -> PB; the source budget is kept (zero rule) (rule:hardware.threshold-breach)"
        assert dropped in markdown

    def test_an_advisory_breach_is_a_line_of_the_candidate_and_of_the_gaps(self, tmp_path: Path) -> None:
        model = three_cores()
        model.core("C3", threshold=0.1, enforcement="advisory", asil_capability="B")
        _root, projection, curve = _run(model, tmp_path)
        document = _document(projection, curve)
        markdown = _one_content(document)
        assert "Advisory cores over their threshold after the moves (reported, not constrained): C3" in markdown
        assert (
            "advisory core 'C3' is over its threshold in a state this report states: reported, not constrained"
        ) in document["dataGaps"]

    def test_the_parameters_render_by_their_key_whatever_the_component_names(self, tmp_path: Path) -> None:
        """An ``allowed_partitions`` keyed by components named ``n`` and ``d`` is a mapping, not a rational."""
        _root, projection, curve = _run(three_cores(), tmp_path, max_moves=1)
        document = _document(projection, curve, max_moves=1, tolerance=0.25)
        document["fingerprint"]["parameters"]["constraints"]["allowed_partitions"] = {"n": ["P1"], "d": ["P2", "P3"]}
        markdown = _one_content(document)
        assert "- constraints.allowed_partitions: n: P1; d: P2, P3" in markdown
        assert "- search.tolerance: 0.25" in markdown

    def test_a_figure_is_shown_by_the_resource_layers_rule_and_never_reads_higher(self, tmp_path: Path) -> None:
        """Loads as ``format_percent``, volumes as ``format_decimal``: truncated downward, ``~`` when not exact.

        A rounded-up figure (``0.67`` for ``2/3``) would read higher than the exact value in ``result.json``.
        """
        _root, projection, curve = _run(three_cores(), tmp_path, max_moves=1)
        document = _document(projection, curve, max_moves=1)
        document["currentState"]["volume"] = {"n": 2, "d": 3}
        document["currentState"]["loads"][0]["load"] = {"n": 2, "d": 3}
        document["currentState"]["loads"][0]["threshold"] = {"n": 4, "d": 5}
        markdown = _one_content(document)
        assert "Inter-partition volume: ~0.66 bytes x Hz." in markdown
        load = document["currentState"]["loads"][0]
        assert f"| {load['core']} | {load['enforcement']} | ~66.66% | 80.00% |" in markdown
        assert "≈" not in markdown
        assert document["currentState"]["volume"] == {"n": 2, "d": 3}


# The links from report.md's names to their model files

#: A Markdown link ``[name](target)``: the report's names never hold a bracket and its targets never a parenthesis.
_LINK = re.compile(r"\[([^\]\[]+)\]\(([^)\s]+)\)")


def _unlinked(markdown: str) -> str:
    """*markdown* with every ``[name](target)`` reduced to ``name``."""
    return _LINK.sub(lambda match: match.group(1), markdown)


def _targets(markdown: str) -> dict[str, set[str]]:
    """``name -> every target`` a link of *markdown* gives it."""
    found: dict[str, set[str]] = {}
    for name, target in _LINK.findall(markdown):
        found.setdefault(name, set()).add(target)
    return found


def _resolve(directory: Path, target: str) -> Path:
    return (directory / urllib.parse.unquote(target)).resolve()


def _project_run(tmp_path: Path, model: CarmaModel, **experiment: Any) -> tuple[Path, allocation.AllocationRun]:
    """*model* as a project under *tmp_path*, run with *experiment*'s configuration: ``(model root, the run)``."""
    manifest, file = write_project(model, tmp_path, **experiment)
    return tmp_path / "model", allocation.run_allocation(require_project(str(manifest)), file)


class TestTheReportLinks:
    def test_without_links_the_report_is_todays_text(self, tmp_path: Path) -> None:
        """No ``report_links``, or empty ones: no link anywhere, and every linked site in its plain form."""
        _root, projection, curve = _run(zero_rule(), tmp_path, locked=["Y", "W"], max_moves=2, candidates=1)
        document = _document(projection, curve, locked=["Y", "W"], max_moves=2, candidates=1)
        markdown = _one_content(document)
        assert allocation.render_markdown(document, allocation.ReportLinks()) == markdown
        assert "](" not in markdown
        assert "- **X**: partition PA (core C1) -> partition PB (core C1)" in markdown
        assert "  - runnable X_Run: task TA -> task TB" in markdown
        assert "- task TB (partition PB): wcet 7000us -> 7100us (-0us +100us)" in markdown
        assert "\n| X | " in markdown
        # Against a committed text too, not only against the same renderer's second call.
        golden = Path(__file__).parent / "fixtures" / "allocation_report_golden" / "report_without_links.txt"
        expected = golden.read_text(encoding="utf-8").replace("SciPy <version>", f"SciPy {curve.solver_version}")
        assert markdown == expected

    def test_every_target_resolves_to_the_elements_own_file(self, tmp_path: Path) -> None:
        """A component under its group directory, a task and a partition, each linked at every site it appears."""
        root, run = _project_run(tmp_path, zero_rule(), locked=["Y", "W"], max_moves=2, candidates=1)
        markdown = run.report_path.read_text(encoding="utf-8")
        directory = run.report_path.parent
        targets = _targets(markdown)
        expected = {
            "X": root / "swc" / "carma" / "X.yaml",
            "Z": root / "swc" / "carma" / "Z.yaml",
            "TA": root / "tasks" / "TA.yaml",
            "TB": root / "tasks" / "TB.yaml",
            "PA": root / "hardware" / "PA.yaml",
            "PB": root / "hardware" / "PB.yaml",
        }
        for name, file in expected.items():
            assert {_resolve(directory, target) for target in targets[name]} == {file.resolve()}, name
        for name, found in targets.items():
            assert all(not Path(target).is_absolute() and "\\" not in target for target in found), name
            assert all(_resolve(directory, target).is_file() for target in found), name
        assert "- **[X](../../../model/swc/carma/X.yaml)**: partition [PA](../../../model/hardware/PA.yaml)" in markdown
        assert "  - runnable X_Run: task [TA](../../../model/tasks/TA.yaml) -> task [TB](" in markdown
        assert "- task [TB](../../../model/tasks/TB.yaml) (partition PB): wcet 7000us -> 7100us" in markdown
        assert "\n| [X](../../../model/swc/carma/X.yaml) | " in markdown
        # The compound cells stay plain: the curve's Moves column and the dropped sets.
        assert "- at N = 1: X: PA -> PB; the source budget is kept" in markdown
        assert _unlinked(markdown) == allocation.render_markdown(
            json.loads(run.result_path.read_text(encoding="utf-8"))
        )

    def test_a_link_survives_an_output_outside_dist(self, tmp_path: Path) -> None:
        root, run = _project_run(tmp_path, three_cores(), max_moves=1, output="reports/allocation")
        assert run.report_path.parent == (tmp_path / "reports" / "allocation").resolve()
        markdown = run.report_path.read_text(encoding="utf-8")
        targets = _targets(markdown)
        assert targets, "no link was written"
        assert {_resolve(run.report_path.parent, target) for target in targets["B1"]} == {
            (root / "swc" / "carma" / "B1.yaml").resolve()
        }
        assert "\n| [B1](../../model/swc/carma/B1.yaml) | " in markdown

    def test_a_collision_file_links_to_its_own_file(self, tmp_path: Path) -> None:
        """``t_p2_10ms`` stored as ``t_p2_10ms~1.yaml`` beside ``T_P2_10ms.yaml``: each name its own file."""
        model = three_cores()
        root = model.write(tmp_path / "model")
        collision = root / "tasks" / "t_p2_10ms~1.yaml"
        collision.write_text(
            (root / "tasks" / "T_P2_10ms.yaml").read_text(encoding="utf-8").replace("T_P2_10ms", "t_p2_10ms"),
            encoding="utf-8",
            newline="\n",
        )
        directory = tmp_path / "dist" / "experiments" / "allocation"
        tasks = allocation.read_report_links(root, directory).tasks
        assert tasks["t_p2_10ms"] == "../../../model/tasks/t_p2_10ms~1.yaml"
        assert tasks["T_P2_10ms"] == "../../../model/tasks/T_P2_10ms.yaml"
        assert _resolve(directory, tasks["t_p2_10ms"]) == collision.resolve()

    def test_each_segment_is_percent_encoded(self, tmp_path: Path) -> None:
        root = three_cores().write(tmp_path / "a b#c" / "model")
        directory = tmp_path / "out"
        assert allocation.read_report_links(root, directory).partitions["P1"] == "../a%20b%23c/model/hardware/P1.yaml"
        assert _resolve(directory, "../a%20b%23c/model/hardware/P1.yaml") == (root / "hardware" / "P1.yaml").resolve()

    def test_a_name_two_files_declare_maps_to_nothing(self, tmp_path: Path) -> None:
        """The defensive branch: ``check-model`` refuses such a model first, so only the helper meets it."""
        root = three_cores().write(tmp_path / "model")
        (root / "tasks" / "Twin.yaml").write_text(
            (root / "tasks" / "T_P2_10ms.yaml").read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
        )
        tasks = allocation.read_report_links(root, tmp_path).tasks
        assert "T_P2_10ms" not in tasks
        assert tasks["T_P2_20ms"] == "model/tasks/T_P2_20ms.yaml"
