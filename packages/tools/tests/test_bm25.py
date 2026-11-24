"""The shared BM25 leaf (:mod:`swcraft_tools.bm25`): a leaf, the trace link search's own, and BM25F.

* **the leaf** -- the module imports the standard library alone, so the guide's reader and the
  trace link search score with one implementation;
* **the shared objects** -- ``trace_search``'s BM25 names are this module's objects, not copies;
  the search's own oracles stay its suites (``test_trace_search_evidence.py``,
  ``test_trace_search_showcase_shape.py``);
* **BM25F** -- against its formula computed by hand, equal to BM25 with one field at boost 1.

What this file does not reach: what a score is worth -- that is the callers' measurements.
"""

from __future__ import annotations

import ast
import math
import sys
from collections import Counter
from pathlib import Path

import pytest
from swcraft_tools import bm25, trace_search
from swcraft_tools.bm25 import BM25_B, BM25_K1, Corpus, FieldCorpus, bm25_scores, bm25f_scores, idf

MODULE_PATH = Path(bm25.__file__)


def _imported_modules() -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(MODULE_PATH.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "bm25.py must use no relative import"
            modules.add(node.module or "")
    return modules


class TestTheLeaf:
    def test_it_imports_the_standard_library_alone(self) -> None:
        roots = {module.split(".")[0] for module in _imported_modules()}
        foreign = sorted(root for root in roots if root not in sys.stdlib_module_names and root != "__future__")
        assert foreign == [], f"bm25.py must import the standard library alone; found {foreign}"

    def test_the_scan_sees_a_planted_package_import(self) -> None:
        tree = ast.parse("from swcraft_tools import schema\nimport math\n")
        roots = {
            (node.module if isinstance(node, ast.ImportFrom) else node.names[0].name).split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import | ast.ImportFrom)
        }
        assert sorted(root for root in roots if root not in sys.stdlib_module_names) == ["swcraft_tools"]


class TestTheMoveFromTheTraceSearch:
    @pytest.mark.parametrize(
        "name", ["Bm25Stats", "Corpus", "Removal", "bm25_scores", "bm25_stats", "scores_of", "term_contributions"]
    )
    def test_the_trace_searchs_names_are_the_leafs_objects(self, name: str) -> None:
        assert getattr(trace_search, name) is getattr(bm25, name)

    def test_the_constants(self) -> None:
        assert (BM25_K1, BM25_B) == (1.2, 0.75)

    def test_idf_is_the_formula(self) -> None:
        assert idf(10, 3) == math.log(1 + (10 - 3 + 0.5) / (3 + 0.5))
        assert idf(1, 1) > 0


_FIELDS = ("title", "text")


def _fielded(documents: dict[str, dict[str, list[str]]]) -> FieldCorpus:
    corpus = FieldCorpus(_FIELDS)
    for key, bags in documents.items():
        corpus.add(key, bags)
    return corpus


def _formula(documents: dict[str, dict[str, list[str]]], query: list[str], boosts: dict[str, float]) -> dict:
    """BM25F written out, independently of the module."""
    size = len(documents)
    averages = {name: sum(len(bags.get(name, [])) for bags in documents.values()) / size for name in _FIELDS}
    scores: dict[str, float] = {}
    for term in sorted(set(query)):
        holding = [key for key, bags in documents.items() if any(term in bag for bag in bags.values())]
        if not holding:
            continue
        weight = math.log(1 + (size - len(holding) + 0.5) / (len(holding) + 0.5))
        for key in holding:
            pseudo = 0.0
            for name in _FIELDS:
                bag = documents[key].get(name, [])
                if bag.count(term) and boosts.get(name):
                    pseudo += boosts[name] * bag.count(term) / (1 - BM25_B + BM25_B * len(bag) / averages[name])
            if pseudo:
                scores[key] = scores.get(key, 0.0) + weight * pseudo * (BM25_K1 + 1) / (pseudo + BM25_K1)
    return scores


_DOCUMENTS = {
    "a": {"title": ["brake", "pedal"], "text": ["the", "brake", "pedal", "signal", "brake"]},
    "b": {"title": ["wheel"], "text": ["wheel", "speed", "sensor"]},
    "c": {"title": ["brake", "light"], "text": ["light"]},
    "d": {"text": ["speed", "limit", "brake"]},
}


class TestBm25f:
    @pytest.mark.parametrize("query", [["brake"], ["brake", "speed"], ["wheel", "light", "absent"]])
    def test_the_formula(self, query: list[str]) -> None:
        boosts = {"title": 3.0, "text": 1.0}
        assert bm25f_scores(_fielded(_DOCUMENTS), query, boosts) == pytest.approx(
            _formula(_DOCUMENTS, query, boosts), rel=1e-12
        )

    @pytest.mark.parametrize("query", [["brake"], ["brake", "speed", "wheel"]])
    def test_one_field_at_boost_one_is_bm25(self, query: list[str]) -> None:
        plain = Corpus()
        for key, bags in _DOCUMENTS.items():
            plain.add(key, bags.get("text", []))
        fielded = _fielded({key: {"text": bags.get("text", [])} for key, bags in _DOCUMENTS.items()})
        assert bm25f_scores(fielded, query, {"text": 1.0}) == pytest.approx(bm25_scores(plain, query), rel=1e-12)

    def test_a_boost_scales_its_field(self) -> None:
        corpus = _fielded(_DOCUMENTS)
        low = bm25f_scores(corpus, ["light"], {"title": 1.0, "text": 1.0})
        high = bm25f_scores(corpus, ["light"], {"title": 8.0, "text": 1.0})
        assert high["c"] > low["c"]

    def test_a_field_outside_the_boosts_weighs_nothing(self) -> None:
        assert bm25f_scores(_fielded(_DOCUMENTS), ["pedal"], {"text": 1.0}) == pytest.approx(
            _formula(_DOCUMENTS, ["pedal"], {"text": 1.0})
        )
        assert bm25f_scores(_fielded({"x": {"title": ["pedal"]}, "y": {"text": ["z"]}}), ["pedal"], {"text": 1.0}) == {}

    def test_a_longer_field_scores_lower_at_equal_frequency(self) -> None:
        corpus = _fielded({"short": {"text": ["port"]}, "long": {"text": ["port", "a", "b", "c", "d"]}})
        scores = bm25f_scores(corpus, ["port"], {"text": 1.0})
        assert scores["short"] > scores["long"]

    def test_the_term_order_never_reaches_a_sum(self) -> None:
        corpus = _fielded(_DOCUMENTS)
        boosts = {"title": 3.0, "text": 1.0}
        assert bm25f_scores(corpus, ["speed", "brake", "brake"], boosts) == bm25f_scores(
            corpus, ["brake", "speed"], boosts
        )

    def test_an_empty_corpus_or_query(self) -> None:
        assert bm25f_scores(FieldCorpus(_FIELDS), ["brake"], {"text": 1.0}) == {}
        assert bm25f_scores(_fielded(_DOCUMENTS), [], {"text": 1.0}) == {}

    def test_a_field_not_of_the_corpus_is_refused(self) -> None:
        with pytest.raises(ValueError, match="fields not of this corpus"):
            FieldCorpus(_FIELDS).add("x", {"heading": ["port"]})

    def test_the_postings_hold_each_document_once_per_term(self) -> None:
        corpus = _fielded({"a": {"title": ["port"], "text": ["port", "port"]}})
        assert corpus.postings == {"port": ["a"]}
        assert corpus.frequencies["a"]["text"] == Counter({"port": 2})
