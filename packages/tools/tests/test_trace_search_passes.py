"""The trace link search's passes, documents and self-measurement, on synthetic models."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from swcraft_tools.model_state import load_model_state
from swcraft_tools.trace_search import (
    BAND_MEDIUM,
    BAND_STRONG,
    BAND_WEAK,
    PASS_LABELLING,
    PASS_REVIEW,
    GroupKey,
    Place,
    Ranking,
    Row,
    TargetAddress,
    TraceConfig,
    build_index,
    document_of,
    documents_table,
    format_score,
    labelling_budget,
    low_accuracy,
    parse_config,
    rank,
    recovery,
    rejected_earlier,
    requirements_in_scope,
    review_order,
    run_passes,
    select_for_labelling,
)
from trace_models import TraceModel, config_document, port


def _config(**keys: Any) -> TraceConfig:
    return parse_config(config_document(**keys)) if keys else TraceConfig()


def _run(model: TraceModel, tmp_path: Path, *, rejected: Sequence[tuple[str, int, TargetAddress]] = (), **keys: Any):
    state = load_model_state(model.write(tmp_path / "model"))
    config = _config(**keys)
    index = build_index(state, config, rejected)
    scope = requirements_in_scope(state, config)
    return state, config, index, scope


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


class TestTheDocuments:
    def test_the_normalisation(self, tmp_path: Path) -> None:
        model = TraceModel()
        for identifier, document in (("A", "  Spec A "), ("B", "Spec A"), ("C", ""), ("D", "spec a"), ("E", None)):
            model.requirement(identifier, "Text", document=document)
        state, config, index, scope = _run(model, tmp_path)
        assert [document_of(requirement) for requirement in state["requirements"]] == [
            "Spec A",
            "Spec A",
            None,
            "spec a",
            None,
        ]
        table = documents_table(state, index, scope, config)
        assert [(row["document"], row["inScope"]) for row in table] == [("Spec A", 2), ("spec a", 1), (None, 2)]

    def test_the_table_partitions_the_requirements_in_scope(self, tmp_path: Path) -> None:
        model = TraceModel()
        model.requirement("R1", "One", document="Spec", version=2)
        model.requirement("R2", "Two", document="Spec", version=2)
        model.requirement("R3", "Three", document="Spec")
        model.requirement("S1", "Stub", document="Spec", stub=True)
        model.component("Ctl", satisfies=["R1@2", "R2@1"])
        state, config, index, scope = _run(model, tmp_path)
        assert documents_table(state, index, scope, config) == [
            {
                "document": "Spec",
                "inScope": 3,
                "stubs": 1,
                "linked": 1,
                "awaitingOnly": 1,
                "withoutLink": 1,
                "rejectedEarlier": 0,
                "pass": PASS_REVIEW,
            }
        ]
        assert [requirement.id for requirement in scope] == ["R1", "R2", "R3"]

    def test_the_scope_filters(self, tmp_path: Path) -> None:
        model = TraceModel()
        model.requirement("R1", "One", document="Spec A")
        model.requirement("R2", "Two", document="Spec B")
        model.requirement("R3", "Three")
        _state, _config_, _index, scope = _run(model, tmp_path, documents=["Spec A"], without_document=False)
        assert [requirement.id for requirement in scope] == ["R1"]
        _state, _config_, _index, scope = _run(model, tmp_path, requirements=["R2", "R3"])
        assert [requirement.id for requirement in scope] == ["R2", "R3"]
        _state, _config_, _index, scope = _run(model, tmp_path, documents=["Spec A"], requirements=["R2"])
        assert scope == []

    def test_low_accuracy(self, tmp_path: Path) -> None:
        model = TraceModel()
        model.component("Ctl")
        model.requirement("R1", "One", document="Spec")
        model.requirement("R2", "Two", document="Spec")
        model.requirement("R3", "Three")
        model.requirement("R4", "Four", document="Other")
        model.link("swc/app/Ctl.yaml", "R1@1")
        _state, _config_, index, _scope = _run(model, tmp_path)

        def flag(identifier: str) -> bool:
            requirement = index.requirements[identifier]
            return low_accuracy(rank(index, requirement, 5), requirement)

        assert (flag("R1"), flag("R2"), flag("R3"), flag("R4")) == (True, False, True, True)


# ---------------------------------------------------------------------------
# The labelling budget and selection
# ---------------------------------------------------------------------------


class TestTheBudget:
    @pytest.mark.parametrize(
        ("unlinked", "share", "budget"),
        [(0, 0.05, 0), (1, 0.05, 1), (19, 0.05, 1), (20, 0.05, 1), (21, 0.05, 2), (7, 1.0, 7), (100, 0.07, 7)],
    )
    def test_the_budget(self, unlinked: int, share: float, budget: int) -> None:
        assert labelling_budget(unlinked, share) == budget


def _ranking(identifier: str, first: str | None, margin: int = 0, *, kind: str = "swc") -> Ranking:
    """A ranking whose first group is *first* with exactly *margin* over the second (no second for ``"only"``)."""
    if first is None:
        return Ranking(identifier, 0, (), 5)

    def place(group: GroupKey, score: int) -> Place:
        address = TargetAddress(group.kind, None, group.name)
        return Place(group, score, (), (Row(address, score, BAND_WEAK, ()),))

    head = GroupKey(kind, first)
    if margin < 0:
        return Ranking(identifier, 0, (place(head, -margin),), 5)
    return Ranking(identifier, 0, (place(head, 1_000_000 + margin), place(GroupKey("swc", "Zeta"), 1_000_000)), 5)


class TestTheSelection:
    def test_the_twelve_requirement_selection(self) -> None:
        """Listed requirement by requirement.

        - A is predicted by R01 (0.5), R02 (0.9), R03 (0.9) and R04 (0.1): four, so first; R02 and R03
          tie at the largest margin and R02 wins by the id order.
        - B by R05 (0.3), R06 (0.7) and R07 (0.2): R06.
        - C by R08 (0.4) and R09 (0.4): a tie, R08 by the id order.
        - the interface D by R10 alone, which has no second group (margin 0.6 over nothing).
        - R11 and R12 have no row: they come first among the least certain, then R04 (0.1), then R07 (0.2).
        """
        rankings = [
            _ranking("R01", "A", 500_000),
            _ranking("R02", "A", 900_000),
            _ranking("R03", "A", 900_000),
            _ranking("R04", "A", 100_000),
            _ranking("R05", "B", 300_000),
            _ranking("R06", "B", 700_000),
            _ranking("R07", "B", 200_000),
            _ranking("R08", "C", 400_000),
            _ranking("R09", "C", 400_000),
            _ranking("R10", "D", -600_000, kind="interface"),
            _ranking("R11", None),
            _ranking("R12", None),
        ]
        assert [(chosen.requirement, chosen.reason) for chosen in select_for_labelling(rankings, 8)] == [
            ("R02", "the clearest of 4 requirements whose first candidate is swc A"),
            ("R06", "the clearest of 3 requirements whose first candidate is swc B"),
            ("R08", "the clearest of 2 requirements whose first candidate is swc C"),
            ("R10", "the clearest of 1 requirement whose first candidate is interface D"),
            ("R11", "no candidate found"),
            ("R12", "no candidate found"),
            ("R04", "least certain: margin 0.1"),
            ("R07", "least certain: margin 0.2"),
        ]

    def test_a_small_budget_stops_in_step_one(self) -> None:
        rankings = [_ranking("R1", "A", 1), _ranking("R2", "B", 2), _ranking("R3", None)]
        assert [chosen.requirement for chosen in select_for_labelling(rankings, 1)] == ["R1"]
        assert select_for_labelling(rankings, 0) == []

    def test_equal_counts_follow_the_group_order(self) -> None:
        rankings = [_ranking("R1", "B", 1, kind="interface"), _ranking("R2", "A", 1)]
        assert [chosen.requirement for chosen in select_for_labelling(rankings, 2)] == ["R2", "R1"]

    def test_the_margin_is_taken_before_the_cut(self, tmp_path: Path) -> None:
        """With one place shown, a close runner-up still gives a small margin."""
        model = TraceModel()
        model.requirement("R1", "brake pad wear")
        model.component("BrakeA", description="brake pad wear")
        model.component("BrakeB", description="brake pad wear sensor")
        _state, _config_, index, _scope = _run(model, tmp_path, candidates=1)
        ranking = rank(index, index.requirements["R1"], 1)
        assert len(ranking.places) == 1 and len(ranking.ranked) == 2
        assert ranking.margin == ranking.ranked[0].score - ranking.ranked[1].score
        assert 0 < ranking.margin < ranking.ranked[0].score


# ---------------------------------------------------------------------------
# The passes on a model
# ---------------------------------------------------------------------------


def _labelling_model() -> TraceModel:
    model = TraceModel()
    for name, words in (("PadUnit", "pad wear"), ("DoorUnit", "door lock"), ("LampUnit", "lamp light")):
        model.component(name, description=words)
    for index, words in enumerate(("pad wear", "door lock", "lamp light", "pad wear check", "door lock check")):
        model.requirement(f"R{index}", words, document="Spec")
    model.requirement("L1", "pad wear linked", document="Spec")
    model.link("swc/app/PadUnit.yaml", "L1@1")
    model.requirement("N1", "lamp light")
    return model


class TestThePasses:
    def test_review_is_the_default_and_lists_every_requirement(self, tmp_path: Path) -> None:
        _state, config, index, scope = _run(_labelling_model(), tmp_path)
        outcomes = run_passes(index, scope, config)
        assert [(outcome.document, outcome.pass_) for outcome in outcomes] == [
            ("Spec", PASS_REVIEW),
            (None, PASS_REVIEW),
        ]
        listed = {ranking.requirement for outcome in outcomes for ranking, _reason in outcome.listed}
        nothing = {ranking.requirement for outcome in outcomes for ranking in outcome.nothing_found}
        assert listed | nothing == {"R0", "R1", "R2", "R3", "R4", "L1", "N1"}

    def test_a_labelled_document_lists_every_requirement_its_selection_first(self, tmp_path: Path) -> None:
        # the labelling share orders the list, it does not filter it
        _state, config, index, scope = _run(_labelling_model(), tmp_path, labelling=["Spec"], labelling_share=0.4)
        spec, no_document = run_passes(index, scope, config)
        assert (spec.pass_, no_document.pass_) == (PASS_LABELLING, PASS_REVIEW)
        assert spec.budget == 2 and spec.unselected == 3 and spec.chosen == 2
        listed = [ranking.requirement for ranking, _reason in spec.listed]
        in_spec = sorted(requirement.id for requirement in scope if requirement.document == "Spec")
        assert sorted(listed) == in_spec and listed[-1] == "L1"
        assert [reason is not None for _ranking, reason in spec.listed] == [True, True] + [False] * (len(listed) - 2)

    @pytest.mark.parametrize("budget", [0, 1, 2, 3, 4, 5])
    def test_the_full_selection_starts_with_every_smaller_one(self, tmp_path: Path, budget: int) -> None:
        _state, config, index, scope = _run(_labelling_model(), tmp_path, labelling=["Spec"])
        unlinked = [
            rank(index, requirement, 5)
            for requirement in scope
            if requirement.document == "Spec" and requirement.id != "L1"
        ]
        full = select_for_labelling(unlinked, len(unlinked))
        assert full[:budget] == select_for_labelling(unlinked, budget)

    def test_the_no_document_group_is_labelled_only_under_all(self, tmp_path: Path) -> None:
        _state, config, index, scope = _run(_labelling_model(), tmp_path, labelling="all")
        assert [outcome.pass_ for outcome in run_passes(index, scope, config)] == [PASS_LABELLING, PASS_LABELLING]

    def test_a_second_labelling_after_the_links_puts_new_requirements_first(self, tmp_path: Path) -> None:
        model = _labelling_model()
        _state, config, index, scope = _run(model, tmp_path / "a", labelling=["Spec"], labelling_share=0.4)
        first = [ranking.requirement for ranking, reason in run_passes(index, scope, config)[0].listed if reason]
        for identifier in first:
            model.link("swc/app/LampUnit.yaml", f"{identifier}@1")
        _state, config, index, scope = _run(model, tmp_path / "b", labelling=["Spec"], labelling_share=0.4)
        second = [ranking.requirement for ranking, reason in run_passes(index, scope, config)[0].listed if reason]
        assert second and not set(first) & set(second)

    def test_the_review_order(self) -> None:
        def ranking(identifier: str, band: str, score: int) -> Ranking:
            address = TargetAddress("swc", None, f"T{identifier}")
            return Ranking(identifier, 0, (Place(address.group, score, (), (Row(address, score, band, ()),)),), 5)

        rankings = [
            ranking("b", BAND_WEAK, 9),
            ranking("a", BAND_MEDIUM, 1),
            ranking("C", BAND_MEDIUM, 1),
            ranking("d", BAND_STRONG, 1),
            ranking("e", BAND_MEDIUM, 5),
            Ranking("f", 0, (), 5),
        ]
        found, nothing = review_order(rankings)
        assert [r.requirement for r in found] == ["d", "e", "a", "C", "b"]
        assert [r.requirement for r in nothing] == ["f"]


# ---------------------------------------------------------------------------
# The rejected pairs
# ---------------------------------------------------------------------------


def _swc(name: str) -> TargetAddress:
    return TargetAddress("swc", None, name)


def _four_targets(*, version: int = 1) -> TraceModel:
    """The evidence suite's hand-worked four-target model, the query ``SR-1`` at *version*."""
    model = TraceModel()
    model.component("Alpha", description="first unit")
    model.component("Beta", description="second unit")
    model.component("Gamma", description="third")
    model.component("Delta", description="fourth")
    for identifier, target in (("N-1", "Alpha"), ("N-2", "Alpha"), ("N-3", "Alpha"), ("N-4", "Beta")):
        model.requirement(identifier, "Neighbour", "zzz yyy", document="Spec")
        model.link(f"swc/app/{target}.yaml", f"{identifier}@1")
    model.requirement("SR-1", "Gamma", "goes on", document="Spec", version=version)
    return model


def _places(index, identifier: str) -> list[tuple[str, int]]:
    ranking = rank(index, index.requirements[identifier], 5)
    return [(place.group.name, place.score) for place in ranking.places]


class TestTheRejectedPairs:
    def test_the_hand_worked_ranking_with_one_member_rejected(self, tmp_path: Path) -> None:
        """Worked by hand from the evidence suite's four-target ranking (Gamma 1.25, Alpha 1.2, Beta 0.4).

        Gamma rejected for ``SR-1@1``: Gamma is no row, so its group, which has no
        other member, takes no place. The member maxima read the eligible targets
        alone, and no other member holds a query term, so E2n is 0 for each; the
        neighbour shares are untouched: Alpha 1.6 x 3/4 = 1.2, Beta 1.6 x 1/4 = 0.4.
        """
        _state, _config_, index, _scope = _run(_four_targets(), tmp_path, rejected=[("SR-1", 1, _swc("Gamma"))])
        assert _places(index, "SR-1") == [("Alpha", 1_200_000), ("Beta", 400_000)]
        assert rejected_earlier(index, "SR-1") == 1

    def test_a_rejected_pair_is_a_row_again_at_the_next_version(self, tmp_path: Path) -> None:
        _state, _config_, index, _scope = _run(
            _four_targets(version=2), tmp_path, rejected=[("SR-1", 1, _swc("Gamma"))]
        )
        assert _places(index, "SR-1") == [("Gamma", 1_250_000), ("Alpha", 1_200_000), ("Beta", 400_000)]
        assert index.rejected == {} and rejected_earlier(index, "SR-1") == 0

    def test_a_reject_of_another_requirement_moves_nothing(self, tmp_path: Path) -> None:
        _state, _config_, index, _scope = _run(_four_targets(), tmp_path, rejected=[("N-1", 1, _swc("Gamma"))])
        assert _places(index, "SR-1") == [("Gamma", 1_250_000), ("Alpha", 1_200_000), ("Beta", 400_000)]

    def test_a_rejected_targets_group_keeps_its_evidence(self, tmp_path: Path) -> None:
        """A rejected port is never a row, while its text still counts for its component's group."""
        model = TraceModel()
        model.requirement("SR-1", "brake temperature")
        model.sr_interface("ISig", [("value", "")])
        model.component("Ctl", description="brake", ports=[port("pTemperature", interface="ISig")])
        model.component("Other", description="brake")
        rejected = [("SR-1", 1, TargetAddress("port", "Ctl", "pTemperature"))]
        _state, _config_, index, _scope = _run(model, tmp_path, rejected=rejected)
        places = {place.group.name: place for place in rank(index, index.requirements["SR-1"], 5).places}
        assert {row.address.kind for row in places["Ctl"].rows} == {"swc"}
        assert places["Ctl"].evidence[-1] == {"kind": "text", "terms": ["temperature", "brake"]}

    def test_the_recovery_ignores_the_rejects(self, tmp_path: Path) -> None:
        model = TraceModel()
        model.component("Alpha", description="pad wear")
        model.requirement("R1", "pad wear", document="Spec")
        model.link("swc/app/Alpha.yaml", "R1@1")
        _s, config, index, scope = _run(model, tmp_path / "a")
        _s, config_r, index_r, scope_r = _run(model, tmp_path / "b", rejected=[("R1", 1, _swc("Alpha"))])
        assert recovery(index_r, scope_r, config_r) == recovery(index, scope, config)

    def test_the_counts(self, tmp_path: Path) -> None:
        """Counted: a target in scope; not one already linked, outside the scope, or at another version."""
        model = TraceModel()
        model.component("Alpha")
        model.component("Beta", satisfies=["R1@1"])
        model.component("Gamma")
        model.requirement("R1", "One", document="Spec")
        model.requirement("R2", "Two", document="Spec")
        model.requirement("R3", "Three")
        rejected = [
            ("R1", 1, _swc("Alpha")),
            ("R1", 1, _swc("Beta")),
            ("R1", 1, _swc("Gamma")),
            ("R1", 1, _swc("NoSuchUnit")),
            ("R2", 1, _swc("Alpha")),
            ("R2", 2, _swc("Gamma")),
            ("R3", 1, _swc("Alpha")),
        ]
        state, config, index, scope = _run(model, tmp_path, rejected=rejected, exclude=["Gamma"])
        assert [rejected_earlier(index, identifier) for identifier in ("R1", "R2", "R3")] == [1, 1, 1]
        table = documents_table(state, index, scope, config)
        assert [(row["document"], row["rejectedEarlier"]) for row in table] == [("Spec", 2), (None, 1)]


def test_format_score() -> None:
    assert [format_score(value) for value in (1_250_000, 500_000, 2_000_000, 1, 0, -100_000)] == [
        "1.25",
        "0.5",
        "2",
        "0.000001",
        "0",
        "-0.1",
    ]


# ---------------------------------------------------------------------------
# The self-measurement
# ---------------------------------------------------------------------------


class TestTheRecovery:
    def _model(self) -> TraceModel:
        """Counted by hand.

        In ``Spec``: R1, R2 and R3 on Alpha (R1 also by a stale link, R2 by two spellings of one
        seed), R4 on Beta's port pBeta, R5 on an external stub only. Every text is ``query``.

        - R1, R2, R3: hidden, each still has two neighbours on Alpha out of ``n = 4``, so Alpha is
          first and the Alpha row is shown: owner level and exact.
        - R4: no neighbour lands on Beta, and its own E5b bag is gone with its text (the
          leave-one-out), so Beta takes no place: missed at both levels.
        - R5: its one pair is outside the scope, counted apart.

        So 4 requirements: 3 at first place, 3 in the places; 4 pairs: 3 owner level, 3 exact;
        1 pair and 1 requirement outside the scope.
        """
        model = TraceModel()
        model.component("Alpha")
        model.component("Beta", ports=[port("pBeta")])
        model.component("Outside", external=True)
        for identifier in ("R1", "R2", "R3", "R4", "R5"):
            model.requirement(identifier, "query", document="Spec", version=2 if identifier == "R1" else 1)
        model.link("swc/app/Alpha.yaml", "R1@2", "R1@1", "R2@1", "R2@1", "R3@1")
        model.get("swc/app/Beta.yaml")["ports"][0]["satisfies"] = ["R4@1"]
        model.link("swc/app/Outside.yaml", "R5@1")
        return model

    def test_the_figures_counted_by_hand(self, tmp_path: Path) -> None:
        _state, config, index, scope = _run(self._model(), tmp_path)
        assert recovery(index, scope, config) == {
            "requirements": 4,
            "firstPlace": 3,
            "inThePlaces": 3,
            "pairs": 4,
            "pairsOwnerLevel": 3,
            "pairsExactTarget": 3,
            "outsideScopePairs": 1,
            "outsideScopeRequirements": 1,
        }

    def test_an_excluded_target_is_outside_the_scope(self, tmp_path: Path) -> None:
        _state, config, index, scope = _run(self._model(), tmp_path, exclude=["Alpha"])
        figures = recovery(index, scope, config)
        assert (figures["requirements"], figures["outsideScopePairs"], figures["outsideScopeRequirements"]) == (1, 4, 4)

    def test_no_linked_requirement_is_an_empty_population(self, tmp_path: Path) -> None:
        model = TraceModel()
        model.requirement("R1", "query")
        model.component("Alpha")
        _state, config, index, scope = _run(model, tmp_path)
        assert recovery(index, scope, config)["requirements"] == 0
