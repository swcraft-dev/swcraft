"""Tests for import component's {X}_Timing refusal: reserved exactly where the component writes an SWC-TIMING."""

from __future__ import annotations

import pytest
from swcraft_tools.schema import Runnable, SWComponent


def _budgeted(name: str, *runnables: str) -> SWComponent:
    return SWComponent(name=name, runnables=[Runnable(name=r, wcet="1us", wcet_type="gross") for r in runnables])


def _unbudgeted(name: str, *runnables: str) -> SWComponent:
    return SWComponent(name=name, runnables=[Runnable(name=r) for r in runnables])


class TestImportComponentReservesTheTimingNameWhereItIsEmitted:
    """``import component``'s exact ``{X}_Timing`` refusal (``reserved_name_violations``).

    ``X`` is a base when the component **as written** emits an ``SWC-TIMING``: its
    own budget, the target's carried onto a runnable that states none, or the
    target's own runnables when the behaviour is kept -- never the target's copy
    alone, which a component dropping every budgeted runnable does not write. The
    carry cases compose ``plan_component_import``'s own functions
    (``behaviour_overlay``, ``carry_target_costs``); the end-to-end refusal is
    ``test_component_import.py``'s.
    """

    @staticmethod
    def _violations(
        swc: SWComponent,
        target_swcs: dict[str, SWComponent],
        compositions: frozenset[str] = frozenset(),
    ) -> list[str]:
        from swcraft_tools.component_exchange.name_policies import reserved_name_violations

        return reserved_name_violations(swc, [], target_swcs, set(compositions), {}, [], {})

    @staticmethod
    def _written(target: SWComponent, incoming: SWComponent, *, behaviour_kept: bool = False) -> SWComponent:
        """The component as ``plan_component_import`` writes it: the overlay, then the budget carry."""
        from swcraft_tools.component_exchange.resulting_behaviour import behaviour_overlay
        from swcraft_tools.component_exchange.target_carry import carry_target_costs

        written = behaviour_overlay(target, incoming) if behaviour_kept else incoming
        return carry_target_costs(target, written)[0]

    @pytest.mark.parametrize(
        ("target_swcs", "compositions", "what"),
        [
            pytest.param({"X_Timing": _unbudgeted("X_Timing")}, frozenset(), "SWC", id="a-target-component"),
            pytest.param({}, frozenset({"X_Timing"}), "composition", id="a-target-composition"),
        ],
    )
    def test_a_budgeted_incoming_component_refuses_the_exact_claim(
        self, target_swcs: dict[str, SWComponent], compositions: frozenset[str], what: str
    ) -> None:
        assert self._violations(_budgeted("X", "Run"), target_swcs, compositions) == [
            f"{what} name 'X_Timing' collides with the SWC-TIMING name auto-generated for 'X'"
        ]

    def test_a_budget_the_target_carries_onto_an_unbudgeted_incoming_runnable_claims_the_name(self) -> None:
        """An export with no budget on a runnable the target budgets: the target's pair is carried."""
        target = _budgeted("X", "Run")
        written = self._written(target, _unbudgeted("X", "Run"))
        assert self._violations(written, {"X": target}, frozenset({"X_Timing"})) == [
            "composition name 'X_Timing' collides with the SWC-TIMING name auto-generated for 'X'"
        ]

    def test_a_target_budget_on_a_runnable_the_file_drops_claims_nothing(self) -> None:
        """Nothing is carried onto a runnable the file does not have, so the written component emits no timing."""
        target = _budgeted("X", "Gone")
        written = self._written(target, _unbudgeted("X", "Run"))
        assert self._violations(written, {"X": target}, frozenset({"X_Timing"})) == []

    @pytest.mark.parametrize("kept", [True, False], ids=["behaviour-kept", "behaviour-written"])
    def test_a_kept_behaviour_decides_by_the_targets_runnables(self, kept: bool) -> None:
        """A withheld behaviour (``runnables=[]``) over a budgeted target component keeps the target's runnables."""
        target = _budgeted("X", "Run")
        written = self._written(target, _unbudgeted("X"), behaviour_kept=kept)
        violations = self._violations(written, {"X": target}, frozenset({"X_Timing"}))
        assert bool(violations) is kept, violations

    def test_unbudgeted_on_both_sides_leaves_the_name_free(self) -> None:
        assert self._violations(_unbudgeted("X", "Run"), {"X": _unbudgeted("X", "Run")}, frozenset({"X_Timing"})) == []

    def test_a_pair_wholly_inside_the_target_is_not_this_imports(self) -> None:
        assert (
            self._violations(_unbudgeted("Other", "Run"), {"Y": _budgeted("Y", "Run")}, frozenset({"Y_Timing"})) == []
        )
