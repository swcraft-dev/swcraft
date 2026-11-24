"""The trace link search's corpus, evidence and fingerprints, on synthetic models.

The oracles are computed in the tests: BM25 from its formula written out here, the four-target ranking
worked by hand in its docstring, never read back from the module under test.
"""

from __future__ import annotations

import math
from collections import Counter
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field
from swcraft_tools import trace_search
from swcraft_tools.model_state import load_model_state
from swcraft_tools.schema import Requirement, SWComponent
from swcraft_tools.trace_search import (
    BAND_MEDIUM,
    BAND_STRONG,
    BAND_WEAK,
    Corpus,
    ExistingLink,
    GroupKey,
    Ranking,
    Removal,
    TargetAddress,
    TraceConfig,
    bm25_scores,
    build_index,
    content_fingerprint,
    existing_links,
    parse_config,
    rank,
    requirement_fingerprint,
    target_fingerprint,
)
from swcraft_tools.trace_terms import STOP_WORDS, tokens
from trace_models import TraceModel, config_document, port, runnable


def _state(model: TraceModel, tmp_path: Path):
    return load_model_state(model.write(tmp_path / "model"))


def _rank(model: TraceModel, tmp_path: Path, identifier: str, *, candidates: int = 5, **keys: Any) -> Ranking:
    state = _state(model, tmp_path)
    config = parse_config(config_document(**keys)) if keys else TraceConfig()
    index = build_index(state, config)
    return rank(index, index.requirements[identifier], candidates)


def _rows(ranking: Ranking) -> dict[tuple[str, str], Any]:
    return {(row.address.kind, row.address.name): row for place in ranking.places for row in place.rows}


def _swc(name: str) -> GroupKey:
    return GroupKey("swc", name)


# ---------------------------------------------------------------------------
# The terms
# ---------------------------------------------------------------------------


class TestTheTerms:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("BrakeTemp2Sensor", ["brake", "temp", "sensor"]),
            ("brake_temp-sensor", ["brake", "temp", "sensor"]),
            ("ECU42Monitor", ["ecu", "42", "monitor"]),
            ("HTTPServer", ["httpserver"]),
            ("The brake shall report a fault", ["brake", "report", "fault"]),
            ("x y 7 ok", ["ok"]),
            ("", []),
        ],
    )
    def test_the_splits(self, text: str, expected: list[str]) -> None:
        assert tokens(text) == expected

    def test_the_stop_words_are_frozen(self) -> None:
        # 63 function words and 14 words of requirement boilerplate
        listed = (
            "an and any are as at be been being but by can could each for from has have if in into is it its "
            "may might must no not of on only or other shall should so such than that the their then there "
            "these they this those to under upon was were when where which while who will with within without "
            "would"
        ).split() + "all also both every one provide provided provides same use used uses using whether".split()
        assert frozenset(listed) == STOP_WORDS and len(listed) == len(STOP_WORDS) == 77
        assert {"shall", "should", "must", "will"} <= STOP_WORDS


# ---------------------------------------------------------------------------
# BM25, by its formula
# ---------------------------------------------------------------------------


def _formula(bags: dict[str, list[str]], query: list[str]) -> dict[str, float]:
    """BM25 written out: k1 1.2, b 0.75, idf ln(1 + (N - df + 0.5) / (df + 0.5)), distinct query terms."""
    size = len(bags)
    average = sum(len(bag) for bag in bags.values()) / size
    scores: dict[str, float] = {}
    for key, bag in bags.items():
        total = 0.0
        for term in sorted(set(query)):
            df = sum(1 for other in bags.values() if term in other)
            tf = bag.count(term)
            if tf == 0 or df == 0:
                continue
            idf = math.log(1 + (size - df + 0.5) / (df + 0.5))
            total += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(bag) / average))
        if total > 0:
            scores[key] = total
    return scores


def _corpus(bags: dict[str, list[str]]) -> Corpus:
    corpus = Corpus()
    for key, bag in bags.items():
        corpus.add(key, bag)
    return corpus


_BAGS = {
    "a": ["brake", "pad", "wear", "brake"],
    "b": ["brake", "temperature"],
    "c": ["door", "lock", "door", "handle", "light"],
}


class TestBm25:
    def test_three_targets_against_the_formula(self) -> None:
        query = ["brake", "wear", "door"]
        assert bm25_scores(_corpus(_BAGS), query) == pytest.approx(_formula(_BAGS, query), rel=1e-12)

    def test_a_repeated_query_term_counts_once(self) -> None:
        corpus = _corpus(_BAGS)
        assert bm25_scores(corpus, ["brake", "brake", "brake"]) == bm25_scores(corpus, ["brake"])

    def test_an_empty_corpus_and_an_empty_query_score_nothing(self) -> None:
        assert bm25_scores(Corpus(), ["brake"]) == {}
        assert bm25_scores(_corpus({"a": [], "b": []}), ["brake"]) == {}
        assert bm25_scores(_corpus(_BAGS), []) == {}

    def test_the_leave_one_out_equals_the_recomputation(self) -> None:
        """Removing one text from two bags (one left empty, so dropped) equals building the reduced corpus."""
        own = ["brake", "wear"]
        bags = {"a": [*_BAGS["a"], *own], "b": own, "c": _BAGS["c"]}
        removal = Removal(frozenset({"a", "b"}), Counter(own))
        reduced = {"a": _BAGS["a"], "c": _BAGS["c"]}
        query = ["brake", "wear", "door", "pad"]
        assert bm25_scores(_corpus(bags), query, removal) == pytest.approx(_formula(reduced, query), rel=1e-12)
        assert bm25_scores(_corpus(reduced), query) == pytest.approx(
            bm25_scores(_corpus(bags), query, removal), rel=1e-12
        )


# ---------------------------------------------------------------------------
# The links
# ---------------------------------------------------------------------------


class TestTheLinks:
    def test_the_seed_classification(self, tmp_path: Path) -> None:
        model = TraceModel()
        model.requirement("SR-1", "One", version=2)
        model.requirement("SR-2", "Two")
        model.component(
            "Ctl",
            satisfies=["SR-1@2", "SR-2"],
            ports=[port("pOut", satisfies=["SR-1@1"])],
            runnables=[runnable("Step", satisfies=["SR-9@1", "SR-1@x"])],
        )
        model.composition("Top", satisfies=["SR-1@2"])
        links = existing_links(_state(model, tmp_path))
        ctl, out, step = (
            TargetAddress("swc", None, "Ctl"),
            TargetAddress("port", "Ctl", "pOut"),
            TargetAddress("runnable", "Ctl", "Step"),
        )
        assert links["SR-1"] == [
            ExistingLink(ctl, 2, True, "SR-1@2"),
            ExistingLink(out, 1, False, "SR-1@1"),
            ExistingLink(step, None, False, "SR-1@x"),
            ExistingLink(TargetAddress("composition", None, "Top"), 2, True, "SR-1@2"),
        ]
        assert links["SR-2"] == [ExistingLink(ctl, None, False, "SR-2")]
        assert "SR-9" not in links

    def test_an_already_linked_target_is_never_a_row(self, tmp_path: Path) -> None:
        model = TraceModel()
        model.requirement("SR-1", "brake temperature", version=2)
        model.component("BrakeA", description="brake temperature", satisfies=["SR-1@2"])
        model.component("BrakeB", description="brake temperature", satisfies=["SR-1@1"])
        model.component("BrakeC", description="brake temperature")
        rows = _rows(_rank(model, tmp_path, "SR-1"))
        assert set(rows) == {("swc", "BrakeC")}


# ---------------------------------------------------------------------------
# E1, the name in the text
# ---------------------------------------------------------------------------


class TestTheName:
    def test_identifier_boundaries_and_the_minimum_length(self, tmp_path: Path) -> None:
        model = TraceModel()
        model.requirement("SR-1", "Report via BrakeCtl_Main and IO to Brake.")
        for name in ("Brake", "BrakeCtl", "BrakeCtl_Main", "IO"):
            model.component(name)
        rows = _rows(_rank(model, tmp_path, "SR-1"))
        assert rows[("swc", "Brake")].evidence[0] == {"kind": "name", "phrase": "Brake"}
        assert rows[("swc", "BrakeCtl_Main")].evidence[0]["kind"] == "name"
        # BrakeCtl and IO share text terms with the query, so they are rows, but neither name matches.
        assert all(record["kind"] != "name" for record in rows[("swc", "BrakeCtl")].evidence)
        assert all(record["kind"] != "name" for record in rows[("swc", "IO")].evidence)

    def test_an_e1_only_candidate_is_retrieved_stop_word_and_all(self, tmp_path: Path) -> None:
        model = TraceModel()
        model.requirement("SR-1", "Should report", "The report goes to Should.")
        model.component("Should")
        model.component("Other", description="nothing alike")
        ranking = _rank(model, tmp_path, "SR-1")
        assert [place.group for place in ranking.places] == [_swc("Should")]
        assert ranking.places[0].rows[0].band == BAND_STRONG
        assert ranking.places[0].score == 250_000

    def test_the_retrieved_ranking_equals_a_brute_force_scoring(self, tmp_path: Path, monkeypatch) -> None:
        model = _four_targets()
        model.component("Should")
        model.requirement("SR-X", "Should and Gamma", "gamma unit work", document="Spec")
        state = _state(model, tmp_path)
        index = build_index(state, TraceConfig())
        for identifier in sorted(index.requirements):
            retrieved = rank(index, index.requirements[identifier], 10)
            every_group = frozenset(index.groups)
            monkeypatch.setattr(trace_search, "_retrieve_every_group", every_group)
            brute = rank(index, index.requirements[identifier], 10)
            monkeypatch.setattr(trace_search, "_retrieve_every_group", None)
            assert retrieved == brute, identifier


# ---------------------------------------------------------------------------
# The ranking
# ---------------------------------------------------------------------------


def _four_targets() -> TraceModel:
    """Four components and a document of four other linked requirements whose texts share no term with the query."""
    model = TraceModel()
    model.component("Alpha", description="first unit")
    model.component("Beta", description="second unit")
    model.component("Gamma", description="third")
    model.component("Delta", description="fourth")
    for identifier, target in (("N-1", "Alpha"), ("N-2", "Alpha"), ("N-3", "Alpha"), ("N-4", "Beta")):
        model.requirement(identifier, "Neighbour", "zzz yyy", document="Spec")
        model.link(f"swc/app/{target}.yaml", f"{identifier}@1")
    model.requirement("SR-1", "Gamma", "goes on", document="Spec")
    return model


class TestTheRanking:
    def test_the_hand_worked_four_target_ranking(self, tmp_path: Path) -> None:
        """Worked by hand.

        ``n = 4`` other linked requirements in ``Spec``: three seeded on Alpha, one on Beta, so E3 weighs
        2 x min(1, 4/5) = 1.6. Their texts share no term with the query ``gamma goes``, so E5b is 0
        everywhere. Only Gamma's bag holds a query term (E2n 1), and its name is in the text (E1 = 1).

        - Gamma: E2n 1 + 0.25 x 1 = 1.25
        - Alpha: 1.6 x 3/4 = 1.2
        - Beta: 1.6 x 1/4 = 0.4
        - Delta: 0, no place.

        ``n = 4`` reaches ``MEDIUM_MIN_NEIGHBOURS``, so Alpha and Beta are *medium* by their neighbours alone.
        """
        ranking = _rank(_four_targets(), tmp_path, "SR-1")
        assert ranking.neighbours == 4
        assert [(place.group.name, place.score) for place in ranking.places] == [
            ("Gamma", 1_250_000),
            ("Alpha", 1_200_000),
            ("Beta", 400_000),
        ]
        assert ranking.margin == 50_000
        bands = {key[1]: row.band for key, row in _rows(ranking).items()}
        assert bands == {"Alpha": BAND_MEDIUM, "Gamma": BAND_STRONG, "Beta": BAND_MEDIUM}

    def test_the_neighbours_are_a_distribution(self, tmp_path: Path) -> None:
        """A neighbour linked to two groups counts for both: the shares sum above one.

        With ``n = 1`` E3 weighs 2 x 1/5 = 0.4, and one neighbour alone makes no *medium*.
        """
        model = TraceModel()
        model.component("Alpha")
        model.component("Beta")
        model.requirement("N-1", "Neighbour", document="Spec")
        model.link("swc/app/Alpha.yaml", "N-1@1")
        model.link("swc/app/Beta.yaml", "N-1@1")
        model.requirement("SR-1", "Query", document="Spec")
        ranking = _rank(model, tmp_path, "SR-1")
        assert [(place.group.name, place.score) for place in ranking.places] == [
            ("Alpha", 400_000),
            ("Beta", 400_000),
        ]
        assert {row.band for place in ranking.places for row in place.rows} == {BAND_WEAK}
        assert ranking.places[0].evidence[0] == {
            "kind": "neighbours",
            "share": 1,
            "seeded": 1,
            "of": 1,
            "examples": ["N-1"],
        }

    def test_below_three_neighbours_a_neighbour_alone_is_weak(self, tmp_path: Path) -> None:
        """Below ``MEDIUM_MIN_NEIGHBOURS`` a neighbour alone makes a weak row.

        One neighbour, seeded on Alpha and Beta. Alpha's text shares *pad* with the query, below
        Gamma's best text: medium by its neighbour and its text. Beta has the neighbour alone: weak.
        """
        model = TraceModel()
        model.component("Alpha", description="pad")
        model.component("Beta", description="unrelated")
        model.component("Gamma", description="pad wear")
        model.requirement("N-1", "Neighbour", "zzz", document="Spec")
        model.link("swc/app/Alpha.yaml", "N-1@1")
        model.link("swc/app/Beta.yaml", "N-1@1")
        model.requirement("SR-1", "pad wear", document="Spec")
        bands = {key[1]: row.band for key, row in _rows(_rank(model, tmp_path, "SR-1")).items()}
        assert bands == {"Alpha": BAND_MEDIUM, "Beta": BAND_WEAK, "Gamma": BAND_MEDIUM}

    def test_n_and_the_similar_weight(self, tmp_path: Path) -> None:
        """With n = 2 both document weights are 2 x 2/5 = 0.8; with no document there is none."""
        model = TraceModel()
        model.component("Alpha", description="unrelated")
        model.component("Beta", description="unrelated")
        for identifier, target in (("N-1", "Alpha"), ("N-2", "Beta")):
            model.requirement(identifier, "pad wear", document="Spec")
            model.link(f"swc/app/{target}.yaml", f"{identifier}@1")
        model.requirement("SR-1", "pad wear", document="Spec")
        model.requirement("SR-2", "pad wear")
        with_document = _rank(model, tmp_path, "SR-1")
        # Each group: E3 = 1/2 -> 0.4; E5bn = 1 -> 0.8; no text in the bags. The similarity
        # evidence keeps both rows medium although n is below MEDIUM_MIN_NEIGHBOURS.
        assert [place.score for place in with_document.places] == [1_200_000, 1_200_000]
        assert {row.band for place in with_document.places for row in place.rows} == {BAND_MEDIUM}
        without = _rank(model, tmp_path, "SR-2")
        assert without.neighbours == 0 and without.places == ()

    def test_the_owner_is_a_row_only_with_a_score(self, tmp_path: Path) -> None:
        model = TraceModel()
        model.requirement("SR-1", "temperature value")
        model.sr_interface("ISig", [("value", "")])
        model.component("Ctl", ports=[port("pTemperature", interface="ISig")])
        ranking = _rank(model, tmp_path, "SR-1")
        assert [row.address.kind for row in ranking.places[0].rows] == ["port"]

    def test_at_most_three_members_beside_the_owner(self, tmp_path: Path) -> None:
        model = TraceModel()
        model.requirement("SR-1", "brake signal")
        model.sr_interface("ISig", [("value", "")])
        model.component(
            "BrakeUnit",
            description="brake",
            ports=[port(f"pBrake{index}", interface="ISig", description="brake signal") for index in range(5)],
        )
        rows = _rank(model, tmp_path, "SR-1").places[0].rows
        assert [row.address.kind for row in rows].count("port") == trace_search.MEMBERS_PER_GROUP == 3
        assert any(row.address.kind == "swc" for row in rows)
        assert [row.score for row in rows] == sorted((row.score for row in rows), reverse=True)

    def test_a_group_with_no_eligible_row_takes_no_place(self, tmp_path: Path) -> None:
        model = TraceModel()
        model.requirement("SR-1", "brake temperature")
        model.component("BrakeA", description="brake temperature brake", satisfies=["SR-1@1"])
        model.component("BrakeB", description="brake")
        ranking = _rank(model, tmp_path, "SR-1", candidates=1)
        assert [place.group.name for place in ranking.places] == ["BrakeB"]

    def test_a_linked_targets_group_keeps_its_evidence(self, tmp_path: Path) -> None:
        """A linked port is never a row, while its text still counts for its component's group."""
        model = TraceModel()
        model.requirement("SR-1", "brake temperature")
        model.sr_interface("ISig", [("value", "")])
        model.component(
            "Ctl",
            description="brake",
            ports=[port("pTemperature", interface="ISig", satisfies=["SR-1@1"])],
        )
        model.component("Other", description="brake")
        ranking = _rank(model, tmp_path, "SR-1")
        places = {place.group.name: place for place in ranking.places}
        assert {row.address.kind for row in places["Ctl"].rows} == {"swc"}
        assert places["Ctl"].evidence[-1] == {"kind": "text", "terms": ["temperature", "brake"]}

    def test_stubs_are_never_targets(self, tmp_path: Path) -> None:
        model = TraceModel()
        model.requirement("SR-1", "brake signal")
        model.external_interface("IBrakeSignal", data_elements=["brake"])
        model.component("BrakeStub", external=True, ports=[port("pBrake", interface="IBrakeSignal")])
        model.component("BrakeUnit", ports=[port("pBrake", interface="IBrakeSignal")])
        rows = _rows(_rank(model, tmp_path, "SR-1"))
        assert set(rows) == {("port", "pBrake"), ("swc", "BrakeUnit")}
        assert all(row.address.owner == "BrakeUnit" for row in rows.values() if row.address.kind == "port")

    def test_the_maxima_never_depend_on_the_cut(self, tmp_path: Path, monkeypatch) -> None:
        model = TraceModel()
        model.requirement("SR-1", "brake signal value")
        model.sr_interface("ISig", [("value", "")])
        model.component(
            "BrakeUnit",
            description="brake",
            ports=[port(f"pSignal{index}", interface="ISig") for index in range(4)],
        )
        model.component("BrakeOther", description="brake signal")
        state = _state(model, tmp_path)
        index = build_index(state, TraceConfig())
        full = rank(index, index.requirements["SR-1"], 5)
        cut = rank(index, index.requirements["SR-1"], 1)
        # A place past the cut carries no evidence record; its score, rows and bands are the full run's.
        assert [(p.group, p.score, [(r.address, r.score, r.band) for r in p.rows]) for p in cut.ranked] == [
            (p.group, p.score, [(r.address, r.score, r.band) for r in p.rows]) for p in full.ranked
        ]
        assert cut.places == full.places[:1] and all(place.evidence == () for place in cut.ranked[1:])
        monkeypatch.setattr(trace_search, "MEMBERS_PER_GROUP", 1)
        fewer = rank(index, index.requirements["SR-1"], 5)
        assert {place.group: place.score for place in fewer.ranked} == {
            place.group: place.score for place in full.ranked
        }
        kept = {row.address: row.score for place in fewer.ranked for row in place.rows}
        assert kept.items() <= {row.address: row.score for place in full.ranked for row in place.rows}.items()


# ---------------------------------------------------------------------------
# The bands
# ---------------------------------------------------------------------------


def _neighbours_model(total: int, on_target: int) -> TraceModel:
    """A document of *total* other linked requirements, *on_target* of them on Alpha and the rest on Beta."""
    model = TraceModel()
    model.component("Alpha")
    model.component("Beta")
    for index in range(total):
        identifier = f"N-{index}"
        model.requirement(identifier, "Neighbour", document="Spec")
        model.link(f"swc/app/{'Alpha' if index < on_target else 'Beta'}.yaml", f"{identifier}@1")
    model.requirement("SR-1", "Query", document="Spec")
    return model


class TestTheBands:
    @pytest.mark.parametrize(
        ("total", "on_target", "band"),
        [(5, 5, BAND_STRONG), (4, 4, BAND_MEDIUM), (6, 3, BAND_STRONG), (6, 2, BAND_MEDIUM), (5, 2, BAND_MEDIUM)],
    )
    def test_the_neighbour_boundary(self, tmp_path: Path, total: int, on_target: int, band: str) -> None:
        rows = _rows(_rank(_neighbours_model(total, on_target), tmp_path, "SR-1"))
        assert rows[("swc", "Alpha")].band == band

    def test_every_target_tied_at_the_best_text_match_is_medium(self, tmp_path: Path) -> None:
        model = TraceModel()
        model.requirement("SR-1", "brake")
        model.component("UnitA", description="brake")
        model.component("UnitB", description="brake")
        model.component("UnitC", description="brake brake pad pad wear")
        bands = {name: row.band for (_kind, name), row in _rows(_rank(model, tmp_path, "SR-1")).items()}
        assert bands == {"UnitA": BAND_MEDIUM, "UnitB": BAND_MEDIUM, "UnitC": BAND_WEAK}


# ---------------------------------------------------------------------------
# The bags and the evidence records
# ---------------------------------------------------------------------------


class TestTheBags:
    def test_a_ports_interface_text_in_its_five_cases(self, tmp_path: Path) -> None:
        model = TraceModel()
        model.sr_interface("ISr", [("speedValue", "vehicle speed")])
        model.cs_interface("ICs", [("resetFault", "clear a fault")])
        model.external_interface("IExt", data_elements=["torqueValue"])
        model.component(
            "Ctl",
            ports=[
                port("pA", interface="ISr"),
                port("pB", interface="ICs"),
                port("pC", interface="IExt"),
                port("pD", interface="IMissing"),
                port("pE"),
            ],
            runnables=[
                runnable("Step", description="step", reads=[("pA", "speedValue")], calls=[("pB", "resetFault")])
            ],
        )
        index = build_index(_state(model, tmp_path), TraceConfig())
        bag = {address.name: index.targets[address].bag for address in index.targets}
        # A port named `pA` splits into two single letters, both too short to be terms.
        assert bag["pA"] == ("isr", "speed", "value", "vehicle", "speed")
        assert bag["pB"] == ("ics", "reset", "fault", "clear", "fault")
        assert bag["pC"] == ("iext", "torque", "value")
        assert bag["pD"] == ("imissing",)
        assert bag["pE"] == ()
        assert bag["Step"] == ("step", "step", "speed", "value", "reset", "fault")
        assert bag["Ctl"] == ("ctl",)
        members = index.groups[_swc("Ctl")]
        assert index.group_text.lengths[_swc("Ctl")] == sum(len(index.targets[address].bag) for address in members)

    def test_the_records_are_capped_and_ordered(self, tmp_path: Path) -> None:
        model = TraceModel()
        for index in range(5):
            model.requirement(f"N-{index}", "alpha beta gamma delta epsilon zeta", document="Spec")
        model.component(
            "AlphaUnit",
            description="alpha beta gamma delta epsilon zeta",
            satisfies=[f"N-{index}@1" for index in range(5)],
        )
        model.requirement("SR-1", "AlphaUnit alpha beta gamma delta epsilon zeta", document="Spec")
        row = _rank(model, tmp_path, "SR-1").places[0].rows[0]
        assert [record["kind"] for record in row.evidence] == ["name", "neighbours", "similar", "text"]
        assert row.evidence[1]["examples"] == ["N-0", "N-1", "N-2"]
        assert row.evidence[2] == {
            "kind": "similar",
            "score": {"n": 1_000_000, "d": 1_000_000},
            "count": 5,
            "examples": ["N-0", "N-1", "N-2"],
        }
        assert len(row.evidence[3]["terms"]) == trace_search.EVIDENCE_TERMS

    def test_the_own_text_never_raises_its_own_similarity(self, tmp_path: Path) -> None:
        """The leave-one-out: a requirement whose own link is hidden finds no similarity in its own text."""
        model = TraceModel()
        model.requirement("SR-1", "pad wear", document="Spec")
        model.requirement("N-1", "door lock", document="Spec")
        model.component("Alpha", satisfies=["SR-1@1"])
        model.component("Beta", satisfies=["N-1@1"])
        state = _state(model, tmp_path)
        index = build_index(state, TraceConfig())
        hidden = rank(index, index.requirements["SR-1"], 5, hide_own_links=True)
        assert [place.group.name for place in hidden.places] == ["Beta"]


# ---------------------------------------------------------------------------
# The fingerprints
# ---------------------------------------------------------------------------


class TestTheFingerprints:
    def _model(self) -> TraceModel:
        model = TraceModel()
        model.requirement("SR-1", "One", document="Spec")
        model.sr_interface("ISig", [("value", "")])
        model.component("Ctl", ports=[port("pOut", interface="ISig"), port("pLost", interface="IMissing")])
        return model

    def _prints(self, model: TraceModel, tmp_path: Path) -> dict[str, Any]:
        state = _state(model, tmp_path)
        return {
            "requirement": requirement_fingerprint(state["requirements"][0]),
            "port": target_fingerprint(state, TargetAddress("port", "Ctl", "pOut")),
            "lost": target_fingerprint(state, TargetAddress("port", "Ctl", "pLost")),
            "swc": target_fingerprint(state, TargetAddress("swc", None, "Ctl")),
        }

    def test_stable_under_a_satisfies_edit(self, tmp_path: Path) -> None:
        model = self._model()
        before = self._prints(model, tmp_path / "a")
        model.get("swc/app/Ctl.yaml")["satisfies"] = ["SR-1@1"]
        model.get("swc/app/Ctl.yaml")["ports"][0]["satisfies"] = ["SR-1@1"]
        model.get("interfaces/ISig.yaml")["satisfies"] = ["SR-1@1"]
        assert self._prints(model, tmp_path / "b") == before

    @pytest.mark.parametrize("edit", ["document", "description", "interface", "declare"])
    def test_a_content_edit_changes_it(self, tmp_path: Path, edit: str) -> None:
        model = self._model()
        before = self._prints(model, tmp_path / "a")
        if edit == "document":
            model.get("requirements/SR-1.yaml")["document"] = "Other"
            changed = {"requirement"}
        elif edit == "description":
            model.get("swc/app/Ctl.yaml")["ports"][0]["description"] = "now described"
            changed = {"port", "swc"}
        elif edit == "interface":
            model.get("interfaces/ISig.yaml")["data_elements"][0]["name"] = "renamed"
            changed = {"port", "swc"}
        else:
            model.sr_interface("IMissing", [("value", "")])
            changed = {"lost", "swc"}
        after = self._prints(model, tmp_path / "b")
        assert {key for key in before if before[key] != after[key]} == changed

    def test_the_envelope(self, tmp_path: Path) -> None:
        state = _state(self._model(), tmp_path)
        component = state["swc"][0]
        one = content_fingerprint(component, {"ISig": state["interfaces"][0], "IMissing": None})
        reordered = content_fingerprint(component, {"IMissing": None, "ISig": state["interfaces"][0]})
        assert one == reordered and one.startswith("sha256:")
        assert target_fingerprint(state, TargetAddress("swc", None, "Ctl")) == one
        assert target_fingerprint(state, TargetAddress("swc", None, "Gone")) is None

    def test_a_document_is_normalised_into_the_requirement_fingerprint(self, tmp_path: Path) -> None:
        a, b = self._model(), self._model()
        b.get("requirements/SR-1.yaml")["document"] = "  Spec  "
        assert self._prints(a, tmp_path / "a")["requirement"] == self._prints(b, tmp_path / "b")["requirement"]

    # A field at its default is left out of the dump, so a field the contract adds with a default
    # moves no fingerprint, and a value off its default does.

    def test_a_field_added_with_a_default_moves_no_fingerprint(self, tmp_path: Path) -> None:
        state = _state(self._model(), tmp_path)
        requirement, component = state["requirements"][0], state["swc"][0]
        interfaces = {"ISig": state["interfaces"][0]}

        class WiderRequirement(Requirement):
            extra: str = "default"

        class WiderComponent(SWComponent):
            tags: list[str] = Field(default_factory=list)

        wider_requirement = WiderRequirement.model_validate(requirement.model_dump())
        wider_component = WiderComponent.model_validate(component.model_dump())
        assert requirement_fingerprint(wider_requirement) == requirement_fingerprint(requirement)
        assert content_fingerprint(wider_component, interfaces) == content_fingerprint(component, interfaces)
        moved_requirement = WiderRequirement.model_validate({**requirement.model_dump(), "extra": "set"})
        moved_component = WiderComponent.model_validate({**component.model_dump(), "tags": ["t"]})
        assert requirement_fingerprint(moved_requirement) != requirement_fingerprint(requirement)
        assert content_fingerprint(moved_component, interfaces) != content_fingerprint(component, interfaces)

    def test_a_default_factory_field_at_its_default_is_absent_from_the_dump(self, tmp_path: Path) -> None:
        component = _state(self._model(), tmp_path)["swc"][0]

        class WiderComponent(SWComponent):
            tags: list[str] = Field(default_factory=list)

        assert "tags" not in trace_search._whole_dump(WiderComponent.model_validate(component.model_dump()))
        given = WiderComponent.model_validate({**component.model_dump(), "tags": []})
        assert "tags" not in trace_search._whole_dump(given)
        moved = WiderComponent.model_validate({**component.model_dump(), "tags": ["t"]})
        assert trace_search._whole_dump(moved)["tags"] == ["t"]

    def test_a_field_set_to_its_default_moves_no_fingerprint(self, tmp_path: Path) -> None:
        a, b = self._model(), self._model()
        b.get("requirements/SR-1.yaml")["stub"] = False
        b.get("requirements/SR-1.yaml")["derived_from"] = []
        assert self._prints(a, tmp_path / "a")["requirement"] == self._prints(b, tmp_path / "b")["requirement"]

    def test_the_document_normalisation_and_the_satisfies_removal_hold_under_the_defaults(self, tmp_path: Path) -> None:
        state = _state(self._model(), tmp_path)
        requirement, component = state["requirements"][0], state["swc"][0]
        spaced = Requirement.model_validate({**requirement.model_dump(), "document": "  Spec  "})
        assert requirement_fingerprint(spaced) == requirement_fingerprint(requirement)
        linked = SWComponent.model_validate({**component.model_dump(), "satisfies": ["SR-1@1"]})
        assert content_fingerprint(linked, {}) == content_fingerprint(component, {})


def test_the_share_figure_is_exact() -> None:
    assert trace_search.share_figure(Fraction(3, 6)) == {"n": 1, "d": 2}
    assert trace_search.share_figure(Fraction(4, 4)) == 1


def test_a_similar_record_that_rounds_to_zero_is_left_out() -> None:
    """The record appears only when it adds to the score, as rounded: E5bn below half a millionth adds nothing."""
    evidence = trace_search.Evidence(name=None, text=0.0, seeded=(), similar=1e-9)
    shown = trace_search._records(
        evidence, neighbours=3, e2_stats=None, key=None, similar_normalised=4e-7, similar_examples=[], similar_count=1
    )
    assert shown == ()
    kept = trace_search._records(
        evidence, neighbours=3, e2_stats=None, key=None, similar_normalised=6e-7, similar_examples=[], similar_count=1
    )
    assert kept == ({"kind": "similar", "score": {"n": 1, "d": 1_000_000}, "count": 1, "examples": []},)


class TestTheNameWords:
    """The project's and the baseline's words are no query terms."""

    def _model(self) -> TraceModel:
        model = TraceModel()
        model.requirement("SR-1", "Provide typed context signals", "Every signal is typed for the showcase.")
        model.get("requirements/SR-1.yaml")["baseline"] = "showcase-baseline-001"
        model.hardware("CPU", "processor", description="The showcase processor with two cores")
        model.hardware("RAM", "memory", description="Illustrative showcase memory budget")
        model.component("SignalHub", description="typed context signals")
        return model

    def test_the_baseline_words_leave_the_query(self, tmp_path: Path) -> None:
        state = _state(self._model(), tmp_path)
        requirement = next(item for item in state["requirements"] if item.id == "SR-1")
        assert trace_search.requirement_terms(requirement) == ["typed", "context", "signals", "signal", "typed"]
        rows = _rows(_rank(self._model(), tmp_path / "again", "SR-1"))
        assert set(rows) == {("swc", "SignalHub")}

    def test_the_project_name_words_leave_the_query(self, tmp_path: Path) -> None:
        state = _state(self._model(), tmp_path)
        requirement = next(item for item in state["requirements"] if item.id == "SR-1")
        requirement = requirement.model_copy(update={"baseline": None})
        assert "showcase" in trace_search.requirement_terms(requirement)
        assert "showcase" not in trace_search.requirement_terms(requirement, frozenset({"showcase"}))

    def test_the_project_name_is_read_from_the_manifest(self) -> None:
        class Project:
            name = "vehicle-control-showcase"

        assert trace_search.project_name_words(Project()) == frozenset({"vehicle", "control", "showcase"})  # type: ignore[arg-type]

    def test_the_boilerplate_is_stop_words(self) -> None:
        assert tokens("Every one shall provide the same signal") == ["signal"]
