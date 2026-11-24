"""The ``guide`` tool's reader and ranking (:mod:`swcraft_tools.guide_search`).

What is pinned here: the reader (the installed corpus alone, its checks, loaded once per process,
a refusal never kept, no refusal naming a location); the three modes through
:func:`~swcraft_tools.guide_search.answer`, with their refusals, bounds and reply budget; the
ranking (tokeniser, exact lookup, overview rule, each weight constant, the order); the leaf's
imports; the measurement rankers (``RANKERS``), with the exact lookup and the overview rule in
front of every one; and the retrieval pairs of ``fixtures/guide_retrieval/pairs.json`` over the
real guide's corpus, built in-process with the release notes the build selects (and the producer
leaves out).

**Isolation from the locator.** The reader never reads ``SWCRAFT_GUIDE_SITE``, so every test that
reaches it patches the two site steps (:func:`installed`), and the reader's cache is reset around
every test (:func:`_fresh_reader`).

What this file does not reach: the MCP tool that wraps :func:`answer` (the server's tests), the built
site's corpus (the presence tests), ``version.md`` (absent from the in-process corpus), and whether a
retrieval pair's expected section is the *best* answer -- the pairs are judgements, not an oracle.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT, build_artifacts_module
from guide_fixtures import help_links_constant
from swcraft_tools import guide_search, guide_site
from swcraft_tools._version import __version__
from swcraft_tools.bm25 import Corpus, FieldCorpus, bm25_scores, bm25f_scores
from swcraft_tools.guide_corpus import build_corpus
from swcraft_tools.guide_search import (
    ANCESTOR_WEIGHT,
    AUXILIARY_STOP_WORDS,
    BM25F_BOOSTS,
    BM25F_FIELDS,
    BODY_COUNT_CAP,
    BODY_WEIGHT,
    CORPUS_FILENAME,
    CORPUS_VERSION_CAP_CHARACTERS,
    DEFAULT_RANKER,
    GUIDE_REPLY_BUDGET_WIRE_BYTES,
    HEADING_WEIGHT,
    NEAREST_LIMIT,
    NO_MATCH_SENTENCE,
    OVERVIEW_QUERIES,
    OVERVIEW_SECTION,
    PAGE_WEIGHT,
    PURPOSE_WEIGHT,
    RANKER_BM25,
    RANKER_BM25F,
    RANKER_WEIGHTS,
    RANKER_WEIGHTS_FIELDS,
    RANKER_WEIGHTS_FIELDS_NORM,
    RANKER_WEIGHTS_FIELDS_NORM_PARTS,
    RANKER_WEIGHTS_FIELDS_PARTS,
    RANKER_WEIGHTS_NORM,
    RANKER_WEIGHTS_NORM_PARTS,
    RANKER_WEIGHTS_PARTS,
    RANKERS,
    REQUEST_CAP_CHARACTERS,
    SEARCH_HIT_LIMIT,
    SECTION_LINK_PREFIX,
    STOP_WORDS,
    WEIGHTS_VARIANTS,
    GuideIndex,
    GuideRefusal,
    MeasurementIndex,
    SearchIndex,
    WeightsVariant,
    answer,
    load_index,
    rank,
    score,
    section_link,
    tokens,
)
from swcraft_tools.wire_unit import escaped_bytes, measure

FIXTURE_GUIDE = Path(__file__).parent / "fixtures" / "guide_corpus" / "basic" / "guide"
PAIRS = Path(__file__).parent / "fixtures" / "guide_retrieval" / "pairs.json"
ANCHOR_RECORD = Path(__file__).parent / "fixtures" / "guide_corpus" / "hand-written-anchors.json"
MODULE_PATH = Path(guide_search.__file__)


@pytest.fixture(autouse=True)
def _fresh_reader() -> Iterator[None]:
    guide_search.reset_cache()
    yield
    guide_search.reset_cache()


@pytest.fixture(scope="module")
def fixture_corpus() -> dict[str, Any]:
    return build_corpus(FIXTURE_GUIDE)


def _site(directory: Path, corpus: dict[str, Any] | str | None) -> Path:
    """A site in *directory*: an ``index.html`` and, unless *corpus* is ``None``, the corpus file."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text("<html></html>\n", encoding="utf-8", newline="\n")
    if corpus is not None:
        text = corpus if isinstance(corpus, str) else json.dumps(corpus, ensure_ascii=False)
        (directory / CORPUS_FILENAME).write_text(text, encoding="utf-8", newline="\n")
    return directory


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Point the reader's two steps at sites under *tmp_path*: ``installed(package=..., bundle=...)``.

    Each argument is a corpus (a mapping, or raw text), ``"site"`` for a site with no
    corpus, or ``None`` for no site at that step.
    """

    def install(package: Any = None, bundle: Any = None) -> None:
        def step(name: str, value: Any) -> Path:
            where = tmp_path / name
            if value is not None:
                _site(where, None if value == "site" else value)
            return where

        package_dir = step("package-site", package)
        bundle_dir = step("bundle-site", bundle)
        monkeypatch.setattr(guide_site, "package_site_dir", lambda: package_dir)
        monkeypatch.setattr(guide_site, "bundle_site_dir", lambda: bundle_dir)

    return install


def _index(corpus: dict[str, Any]) -> SearchIndex:
    return SearchIndex.of(GuideIndex.of(corpus))


def _measured(corpus: dict[str, Any], stop_words: frozenset[str] = STOP_WORDS) -> MeasurementIndex:
    """*corpus* indexed for every ranker: a whole-body or a BM25 ranker reads a measurement index."""
    return MeasurementIndex.over(_index(corpus), stop_words)


def _refusal_code(reply: dict[str, Any] | str) -> str:
    assert isinstance(reply, str), f"expected a refusal, got {reply!r}"
    assert reply.startswith("ERROR: ")
    return reply.split(": ", 2)[1]


# A small corpus written by hand, for the ranking's constants


def _section(
    page: str, title: str, anchor: str | None, level: int, crumbs: list[str], body: str, purpose: str = ""
) -> dict[str, Any]:
    heading = "#" * level + " " + crumbs[-1] + (f" {{ #{anchor} }}" if anchor else "")
    return {
        "id": page if anchor is None else f"{page}#{anchor}",
        "page": page,
        "level": level,
        "breadcrumb": [title, *crumbs[1:]] if level > 1 else [title],
        "purpose": purpose,
        "parts": [f"{heading}\n\n{body}"],
        "own": [body],
    }


def _hand_corpus(*pages: tuple[str, str, list[str], list[tuple[str | None, int, list[str], str]]]) -> dict[str, Any]:
    """A corpus of *pages*: ``(page, title, keywords, [(anchor, level, breadcrumb, body), ...])``.

    A page's first entry is its own section (anchor ``None``, level 1).
    """
    sections = []
    for page, title, _, entries in pages:
        for anchor, level, crumbs, body in entries:
            sections.append(_section(page, title, anchor, level, crumbs, body))
    # A page's subtree holds its sections: the page id's part is its whole text.
    for page, _, _, _ in pages:
        own = [section for section in sections if section["page"] == page]
        own[0]["parts"] = ["\n\n".join(section["parts"][0] for section in own)]
    return {
        "corpus": "swcraft-guide",
        "schema": 1,
        "swcraft_version": __version__,
        "pages": [{"page": page, "title": title, "keywords": keywords} for page, title, keywords, _ in pages],
        "sections": sections,
        "explains": {},
    }


def _one(
    body: str = "", heading: str = "Other", ancestor: str | None = None, purpose: str = "", **page: Any
) -> dict[str, Any]:
    """A one-page corpus whose section ``p#s`` has *heading*, an optional ancestor, *body* and *purpose*."""
    title = page.get("title", "Page")
    keywords = page.get("keywords", [])
    entries: list[tuple[str | None, int, list[str], str]] = [(None, 1, [title], "")]
    if ancestor is not None:
        entries.append(("a", 2, [title, ancestor], ""))
        entries.append(("s", 3, [title, ancestor, heading], body))
    else:
        entries.append(("s", 2, [title, heading], body))
    corpus = _hand_corpus(("p", title, keywords, entries))
    corpus["sections"][-1]["purpose"] = purpose
    return corpus


# The reader


class TestTheReader:
    def test_it_answers_from_the_package_payload(self, installed: Callable[..., None], fixture_corpus: dict) -> None:
        installed(package=fixture_corpus)
        reply = answer(section_id="index")
        assert isinstance(reply, dict) and reply["id"] == "index"

    def test_it_answers_from_the_bundle_when_the_package_holds_no_site(
        self, installed: Callable[..., None], fixture_corpus: dict
    ) -> None:
        installed(package=None, bundle=fixture_corpus)
        assert isinstance(answer(section_id="index"), dict)

    def test_the_package_payload_wins_over_the_bundle(
        self, installed: Callable[..., None], fixture_corpus: dict
    ) -> None:
        installed(package=fixture_corpus, bundle="{ not json")
        assert isinstance(answer(section_id="index"), dict)

    def test_the_first_site_wins_even_without_a_corpus(
        self, installed: Callable[..., None], fixture_corpus: dict
    ) -> None:
        # A package payload holding a site but no corpus is a broken install, not a
        # reason to read another location's corpus.
        installed(package="site", bundle=fixture_corpus)
        assert _refusal_code(answer(section_id="index")) == "guide-missing"

    def test_a_bundle_step_of_none_is_no_site(
        self, installed: Callable[..., None], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        installed()
        monkeypatch.setattr(guide_site, "bundle_site_dir", lambda: None)
        assert _refusal_code(answer(section_id="index")) == "guide-missing"

    def test_the_override_is_never_read(
        self, installed: Callable[..., None], fixture_corpus: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        forged = json.loads(json.dumps(fixture_corpus))
        forged["sections"][0]["purpose"] = "FORGED"
        monkeypatch.setenv(guide_site.GUIDE_SITE_ENV, str(_site(tmp_path / "forged", forged)))
        installed(package=fixture_corpus)
        reply = answer(section_id=fixture_corpus["sections"][0]["id"])
        assert isinstance(reply, dict) and reply["purpose"] != "FORGED"

    def test_the_override_alone_is_no_guide(
        self, installed: Callable[..., None], fixture_corpus: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(guide_site.GUIDE_SITE_ENV, str(_site(tmp_path / "forged", fixture_corpus)))
        installed()
        assert _refusal_code(answer(section_id="index")) == "guide-missing"

    def test_it_is_loaded_once(self, installed: Callable[..., None], fixture_corpus: dict, tmp_path: Path) -> None:
        installed(package=fixture_corpus)
        first = load_index()
        (tmp_path / "package-site" / CORPUS_FILENAME).unlink()
        assert load_index() is first
        guide_search.reset_cache()
        with pytest.raises(GuideRefusal):
            load_index()

    def test_a_refusal_is_never_kept(
        self, installed: Callable[..., None], fixture_corpus: dict, tmp_path: Path
    ) -> None:
        installed(package="site")
        assert _refusal_code(answer(section_id="index")) == "guide-missing"
        _site(tmp_path / "package-site", fixture_corpus)
        assert isinstance(answer(section_id="index"), dict)


class TestTheReadersRefusals:
    def test_no_site_at_either_step(self, installed: Callable[..., None], tmp_path: Path) -> None:
        installed()
        reply = answer(search_text="anything")
        assert _refusal_code(reply) == "guide-missing"
        assert str(tmp_path) not in reply and tmp_path.as_posix() not in reply
        assert "/" not in reply and "\\" not in reply

    def test_a_site_with_no_corpus(self, installed: Callable[..., None]) -> None:
        installed(package="site")
        assert _refusal_code(answer(toc_wanted=True)) == "guide-missing"

    @pytest.mark.parametrize(
        "text",
        [
            "{ not json",
            "[]",
            '{"corpus": "another", "schema": 1, "swcraft_version": "x", "pages": [], "sections": [], "explains": {}}',
        ],
        ids=["malformed", "not-a-mapping", "another-corpus"],
    )
    def test_an_unreadable_corpus(self, installed: Callable[..., None], text: str, tmp_path: Path) -> None:
        installed(package=text)
        reply = answer(toc_wanted=True)
        assert _refusal_code(reply) == "guide-unreadable"
        assert tmp_path.as_posix() not in reply

    @pytest.mark.parametrize(
        "spoil",
        [
            lambda corpus: corpus.update(schema=2),
            lambda corpus: corpus.update(swcraft_version=1),
            lambda corpus: corpus["sections"][0].pop("parts"),
            lambda corpus: corpus["sections"][0].update(parts=[]),
            lambda corpus: corpus["sections"][0].update(page="no-such-page"),
            lambda corpus: corpus["pages"][0].update(keywords="not-a-list"),
            lambda corpus: corpus.update(explains={"code": {"section": "nowhere", "part": 1}}),
            lambda corpus: corpus.update(explains={"code": {"section": corpus["sections"][0]["id"], "part": 99}}),
            lambda corpus: corpus.update(explains={"code": {"section": corpus["sections"][0]["id"], "part": True}}),
            lambda corpus: corpus["sections"][1].update(level=0),
            lambda corpus: corpus["sections"][1].update(level=4),
            lambda corpus: corpus["sections"][1].update(level=True),
            lambda corpus: corpus["sections"].append(dict(corpus["sections"][1])),
            lambda corpus: corpus["pages"].append(dict(corpus["pages"][0])),
            lambda corpus: corpus["sections"].pop(0),
            lambda corpus: corpus["sections"][1].update(level=1),
            lambda corpus: corpus["sections"][1].update(breadcrumb=[]),
            lambda corpus: corpus["sections"][1].update(id=7),
            lambda corpus: corpus["sections"][1].update(purpose=None),
            lambda corpus: corpus["sections"][1].pop("own"),
            lambda corpus: corpus["sections"][1].update(own=[]),
            lambda corpus: corpus["sections"][1].update(own=[1]),
            lambda corpus: corpus["sections"][1].update(own=["a"] * (len(corpus["sections"][1]["parts"]) + 1)),
            lambda corpus: corpus.update(swcraft_version="1" + ".1" * 4500),
            lambda corpus: corpus.update(swcraft_version="0.22.0/home/user/.swcraft"),
            lambda corpus: corpus.update(swcraft_version="C:\\Users\\someone"),
            lambda corpus: corpus.update(swcraft_version=""),
            lambda corpus: corpus["sections"][1].update(page=[corpus["sections"][1]["page"]]),
            lambda corpus: corpus.update(explains={"code": {"section": [corpus["sections"][0]["id"]], "part": 1}}),
        ],
        ids=[
            "unknown-schema",
            "version-not-text",
            "no-parts",
            "empty-parts",
            "unknown-page",
            "keywords-not-a-list",
            "explains-unknown-section",
            "explains-unknown-part",
            "explains-part-bool",
            "level-zero",
            "level-four",
            "level-bool",
            "duplicate-id",
            "duplicate-page",
            "page-without-own-section",
            "a-second-level-one-section",
            "empty-breadcrumb",
            "non-string-id",
            "purpose-not-text",
            "no-own",
            "empty-own",
            "own-not-text",
            "own-longer-than-parts",
            "version-over-its-cap",
            "version-with-a-path",
            "version-with-a-windows-path",
            "version-empty",
            "section-page-a-list",
            "explains-section-a-list",
        ],
    )
    def test_a_corpus_of_another_shape(
        self, installed: Callable[..., None], fixture_corpus: dict, spoil: Callable[[dict], Any]
    ) -> None:
        corpus = json.loads(json.dumps(fixture_corpus))
        assert corpus["sections"][0]["level"] == 1 and corpus["sections"][1]["level"] > 1
        spoil(corpus)
        installed(package=corpus)
        assert _refusal_code(answer(toc_wanted=True)) == "guide-unreadable"

    def test_an_index_that_cannot_be_built_is_unreadable_never_a_masked_error(
        self, installed: Callable[..., None], fixture_corpus: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A shape the check does not reach still answers ``guide-unreadable``: building the index is guarded."""

        def broken(cls: type, guide: GuideIndex) -> SearchIndex:
            raise TypeError("a shape the check did not reach")

        monkeypatch.setattr(SearchIndex, "of", classmethod(broken))
        installed(package=fixture_corpus)
        assert _refusal_code(answer(toc_wanted=True)) == "guide-unreadable"
        monkeypatch.undo()
        installed(package=fixture_corpus)
        assert isinstance(answer(toc_wanted=True), dict)  # the refusal was not kept

    def test_a_deeply_nested_corpus_is_unreadable_never_a_traceback(self, installed: Callable[..., None]) -> None:
        """A nesting the parser cannot descend raises ``RecursionError``; the parse guard refuses it."""
        depth = sys.getrecursionlimit() * 10
        installed(package='{"corpus": ' + "[" * depth + "]" * depth + "}")
        assert _refusal_code(answer(toc_wanted=True)) == "guide-unreadable"

    def test_a_defect_while_building_the_index_surfaces(
        self, installed: Callable[..., None], fixture_corpus: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only what a JSON-shaped value raises is a refusal: a ``RuntimeError`` (no ``RecursionError``) propagates."""
        reached: list[bool] = []

        def broken(cls: type, guide: GuideIndex) -> SearchIndex:
            reached.append(True)
            raise RuntimeError("a defect, not a shape")

        monkeypatch.setattr(SearchIndex, "of", classmethod(broken))
        installed(package=fixture_corpus)
        with pytest.raises(RuntimeError, match="a defect, not a shape"):
            answer(toc_wanted=True)
        assert reached == [True]

    @pytest.mark.parametrize("version", [__version__, "1!2.0rc1.post3.dev4+local.5", "9" * 64])
    def test_a_version_of_the_normalized_form_within_its_cap_is_read(
        self, installed: Callable[..., None], fixture_corpus: dict, version: str
    ) -> None:
        """The shape check accepts the normalized form up to the cap; another release is ``guide-version``."""
        assert len(version) <= CORPUS_VERSION_CAP_CHARACTERS
        installed(package={**fixture_corpus, "swcraft_version": version})
        reply = answer(toc_wanted=True)
        if version == __version__:
            assert isinstance(reply, dict)
        else:
            assert _refusal_code(reply) == "guide-version" and version in reply

    def test_another_version_is_refused_naming_both(self, installed: Callable[..., None], fixture_corpus: dict) -> None:
        installed(package={**fixture_corpus, "swcraft_version": "0.0.1"})
        reply = answer(toc_wanted=True)
        assert _refusal_code(reply) == "guide-version"
        assert "0.0.1" in reply and __version__ in reply

    def test_a_request_is_checked_before_the_corpus_is_read(self, installed: Callable[..., None]) -> None:
        installed()
        assert _refusal_code(answer()) == "guide-mode"
        assert _refusal_code(answer(search_text="x" * (REQUEST_CAP_CHARACTERS + 1))) == "guide-request-too-long"


# The three modes, through answer()


class TestTheModes:
    @pytest.fixture(autouse=True)
    def _guide(self, installed: Callable[..., None], fixture_corpus: dict) -> None:
        installed(package=fixture_corpus)

    def test_search(self) -> None:
        reply = answer(search_text="sections")
        assert isinstance(reply, dict)
        assert (reply["response"], reply["mode"], reply["query"]) == ("guide", "search", "sections")
        assert reply["hits"] and len(reply["hits"]) <= SEARCH_HIT_LIMIT
        assert reply["hits"][0]["id"] == "index#sections"
        assert reply["hits"][0]["read"] == {"section": "index#sections", "part": 1}
        assert "note" not in reply

    def test_a_search_with_more_hits_than_it_carries(self) -> None:
        reply = answer(search_text="guide page section reference release")
        assert isinstance(reply, dict)
        assert len(reply["hits"]) == SEARCH_HIT_LIMIT and reply["more"] is True

    def test_a_search_with_no_hit(self) -> None:
        reply = answer(search_text="zeppelin")
        assert isinstance(reply, dict)
        assert reply["hits"] == [] and reply["more"] is False and reply["note"] == NO_MATCH_SENTENCE

    @pytest.mark.parametrize("query", ["how do I", "swcraft", "what should it have"])
    def test_a_search_of_stop_words_alone_has_no_hit(self, query: str) -> None:
        reply = answer(search_text=query)
        assert isinstance(reply, dict)
        assert reply["hits"] == [] and reply["more"] is False and reply["note"] == NO_MATCH_SENTENCE

    def test_an_exact_code_lookup_reads_its_section(self) -> None:
        reply = answer(search_text="  stale-baseline ")
        assert isinstance(reply, dict)
        assert reply["hits"][0]["read"] == {"section": "how-to/refusals#stale-baseline", "part": 1}

    def test_the_toc(self) -> None:
        reply = answer(toc_wanted=True)
        assert isinstance(reply, dict)
        assert reply["mode"] == "toc" and reply["part"] == 1 and reply["next"] is None
        assert [entry["page"] for entry in reply["pages"]] == [
            "fenced",
            "how-to/refusals",
            "index",
            "reference",
            "reference/rules",
        ]

    def test_the_toc_of_a_page(self) -> None:
        reply = answer(toc_wanted=True, page="index")
        assert isinstance(reply, dict)
        assert [entry["id"] for entry in reply["sections"]][:2] == ["index#reading", "index#pages"]

    @pytest.mark.parametrize("page", ["index", "reference/rules", "reference/rules.md", "no-such-page"])
    def test_a_page_alone_is_the_toc_of_that_page(self, page: str) -> None:
        # A page alone answers as that page's toc: a known page, a `.md` spelling, and an unknown page (its
        # refusal) alike.
        assert answer(page=page) == answer(toc_wanted=True, page=page)
        assert answer(search_text="  ", page=page) == answer(toc_wanted=True, page=page)

    @pytest.mark.parametrize("part", [1, 2])
    def test_a_page_with_a_part_is_that_part_of_the_page_s_toc(self, part: int) -> None:
        reply = answer(page="index", part=part)
        assert reply == answer(toc_wanted=True, page="index", part=part)
        if part == 1:
            assert isinstance(reply, dict) and reply["mode"] == "toc" and reply["page"] == "index"
        else:
            assert _refusal_code(reply) == "guide-unknown-part"

    def test_the_release_notes_are_unknown_to_every_mode(self) -> None:
        """The fixture guide holds the section's index and two notes; the corpus holds none of them."""
        assert _refusal_code(answer(toc_wanted=True, page="release-notes")) == "guide-unknown-section"
        for note_id in ("release-notes", "release-notes/0.9.0", "release-notes/0.9.0#upgrading"):
            assert _refusal_code(answer(section_id=note_id)) == "guide-unknown-section", note_id
        reply = answer(search_text="release notes upgrading 0.9.0")
        assert isinstance(reply, dict)
        assert not [hit["id"] for hit in reply["hits"] if hit["id"].startswith("release-notes")]

    def test_the_toc_of_an_unknown_page_names_the_nearest_pages(self) -> None:
        reply = answer(toc_wanted=True, page="references")
        assert _refusal_code(reply) == "guide-unknown-section"
        assert "`references`" in reply and "`reference`" in reply

    def test_a_toc_part_it_does_not_have(self) -> None:
        assert answer(toc_wanted=True, part=2) == (
            "ERROR: guide-unknown-part: `toc` has 1 part(s); ask for a part from 1 to 1"
        )

    def test_a_section_reads_its_subtree(self) -> None:
        reply = answer(section_id="index#reading")
        assert isinstance(reply, dict)
        assert (reply["id"], reply["part"], reply["parts"], reply["next"]) == ("index#reading", 1, 1, None)
        assert "### Pages" in reply["text"] and "## Where to go next" not in reply["text"]

    def test_part_resolves_to_one_when_omitted(self) -> None:
        assert answer(section_id="index") == answer(section_id="index", part=1)

    @pytest.mark.parametrize("part", [0, -1, 2, 10**30])
    def test_a_part_the_section_does_not_have(self, part: int) -> None:
        reply = answer(section_id="index", part=part)
        assert reply == "ERROR: guide-unknown-part: `index` has 1 part(s); ask for a part from 1 to 1"
        assert str(10**30) not in reply  # the part asked is never echoed

    def test_an_unknown_section_names_the_nearest(self) -> None:
        reply = answer(section_id="index#where-next")
        assert _refusal_code(reply) == "guide-unknown-section"
        assert "`index#where-next`" in reply and "`index#where-to-go-next`" in reply
        assert reply.count("`") <= 2 * (1 + NEAREST_LIMIT)

    def test_the_nearest_are_at_most_three(self) -> None:
        reply = answer(section_id="guide-page-section-reference-release")
        assert _refusal_code(reply) == "guide-unknown-section"
        assert reply.count("`") == 2 * (1 + NEAREST_LIMIT)

    @pytest.mark.parametrize(
        "arguments",
        [
            {},
            {"search_text": "x", "toc_wanted": True},
            {"search_text": "x", "section_id": "index"},
            {"toc_wanted": True, "section_id": "index"},
            {"search_text": "x", "page": "index"},
            {"section_id": "index", "page": "index"},
            {"search_text": "x", "toc_wanted": True, "page": "index"},
            {"search_text": "x", "part": 1},
            {"part": 1},
            {"search_text": "   "},
            {"search_text": " \t\n"},
        ],
        ids=[
            "none",
            "search-and-toc",
            "search-and-section",
            "toc-and-section",
            "page-with-search",
            "page-with-section",
            "page-with-search-and-toc",
            "part-with-search",
            "part-alone",
            "blank-search",
            "whitespace-search",
        ],
    )
    def test_not_exactly_one_mode_is_refused(self, arguments: dict[str, Any]) -> None:
        assert _refusal_code(answer(**arguments)) == "guide-mode"


class TestTheSourceFileSuffix:
    """A page or an id spelled as the page's source file (``.md``) is that page or id."""

    @pytest.fixture(autouse=True)
    def _guide(self, installed: Callable[..., None], fixture_corpus: dict) -> None:
        installed(package=fixture_corpus)

    @pytest.mark.parametrize(
        ("asked", "canonical"),
        [("reference/rules.md", "reference/rules"), ("index.md#reading", "index#reading"), ("index.md", "index")],
    )
    def test_a_suffixed_id_reads_the_canonical_section(self, asked: str, canonical: str) -> None:
        reply = answer(section_id=asked)
        assert reply == answer(section_id=canonical)
        assert isinstance(reply, dict)
        assert reply["id"] == canonical and reply["link"].endswith("=" + canonical)

    def test_a_suffixed_toc_page_lists_the_canonical_page(self) -> None:
        reply = answer(toc_wanted=True, page="reference/rules.md")
        assert reply == answer(toc_wanted=True, page="reference/rules")
        assert isinstance(reply, dict) and reply["page"] == "reference/rules"

    @pytest.mark.parametrize("asked", ["no-such-page.md", "no-such-page.md#reading", "index.md.md", "index#reading.md"])
    def test_a_suffix_naming_no_page_is_still_refused_as_asked(self, asked: str) -> None:
        reply = answer(section_id=asked)
        assert _refusal_code(reply) == "guide-unknown-section"
        assert f"`{asked}`" in reply

    def test_a_suffixed_toc_page_naming_no_page_is_still_refused(self) -> None:
        assert _refusal_code(answer(toc_wanted=True, page="no-such-page.md")) == "guide-unknown-section"

    @pytest.mark.parametrize("asked", ["index.md#nonexistent", "index#nonexistent"])
    def test_an_unknown_anchor_on_a_known_page_names_the_page_and_its_sections(self, asked: str) -> None:
        reply = answer(section_id=asked)
        assert _refusal_code(reply) == "guide-unknown-section"
        named = re.findall(r"`([^`]*)`", reply)
        assert named[0] == asked
        assert named[1] == "index"
        assert len(named) == 1 + NEAREST_LIMIT
        assert all(section_id.startswith("index#") for section_id in named[2:])

    def test_the_page_nearest_rank_the_anchor_words_first(self) -> None:
        reply = answer(section_id="index.md#where-next")
        assert re.findall(r"`([^`]*)`", reply)[1:3] == ["index", "index#where-to-go-next"]

    def test_a_suffixed_id_at_the_bound_is_answered_within_the_budget(self) -> None:
        requested = "index.md#" + '"\\' * ((REQUEST_CAP_CHARACTERS - len("index.md#")) // 2)
        reply = answer(section_id=requested)
        assert _refusal_code(reply) == "guide-unknown-section"
        assert escaped_bytes(reply) <= GUIDE_REPLY_BUDGET_WIRE_BYTES

    def test_the_bound_is_checked_on_the_id_as_asked(self) -> None:
        assert _refusal_code(answer(section_id="index.md#" + "x" * REQUEST_CAP_CHARACTERS)) == "guide-request-too-long"


class TestTheRequestBounds:
    @pytest.fixture(autouse=True)
    def _guide(self, installed: Callable[..., None], fixture_corpus: dict) -> None:
        installed(package=fixture_corpus)

    @pytest.mark.parametrize("field", ["search_text", "section_id", "page"])
    def test_a_value_over_the_bound_is_refused_echoing_nothing(self, field: str) -> None:
        value = "\U0001f600" * (REQUEST_CAP_CHARACTERS + 1)
        arguments: dict[str, Any] = {field: value}
        if field == "page":
            arguments["toc_wanted"] = True
        reply = answer(**arguments)
        assert _refusal_code(reply) == "guide-request-too-long"
        assert "\U0001f600" not in reply

    @pytest.mark.parametrize("character", ['"', "\\", "\x01", "\U0001f600", "é"])
    def test_a_query_at_the_bound_of_wide_characters_is_answered_within_the_budget(self, character: str) -> None:
        query = character * REQUEST_CAP_CHARACTERS
        reply = answer(search_text=query)
        assert isinstance(reply, dict) and reply["query"] == query
        assert measure(reply) <= GUIDE_REPLY_BUDGET_WIRE_BYTES

    def test_an_unknown_id_at_the_bound_is_echoed(self) -> None:
        requested = '"\\' * (REQUEST_CAP_CHARACTERS // 2)
        reply = answer(section_id=requested)
        assert _refusal_code(reply) == "guide-unknown-section"
        assert f"`{requested}`" in reply
        assert escaped_bytes(reply) <= GUIDE_REPLY_BUDGET_WIRE_BYTES


class TestTheReplyBudget:
    @pytest.mark.parametrize(
        ("request_", "target"),
        [
            ({"section_id": "fenced"}, "fenced"),
            ({"search_text": "swollen"}, "search"),
            ({"toc_wanted": True}, "toc"),
            ({"toc_wanted": True, "page": "fenced"}, "fenced"),
        ],
        ids=["section", "search", "toc", "toc-of-a-page"],
    )
    def test_a_reply_over_the_budget_is_refused_naming_its_target(
        self, installed: Callable[..., None], fixture_corpus: dict, request_: dict[str, Any], target: str
    ) -> None:
        """A page title over the budget swells every mode's reply: each is refused, naming its target."""
        corpus = json.loads(json.dumps(fixture_corpus))
        page = next(page for page in corpus["pages"] if page["page"] == "fenced")
        page["title"] = "swollen " * (GUIDE_REPLY_BUDGET_WIRE_BYTES // 8 + 1)
        for record in corpus["sections"]:
            if record["page"] == "fenced" and record["level"] > 1:
                record["breadcrumb"][-1] = page["title"]
        installed(package=corpus)
        reply = answer(**request_)
        assert reply == guide_search.over_budget_refusal(target)

    @pytest.mark.parametrize("requested", ["p#zebra", "q#zebra"], ids=["on-a-known-page", "on-no-page"])
    def test_a_refusal_over_the_budget_gives_way_to_the_constant_sentence(
        self, installed: Callable[..., None], requested: str
    ) -> None:
        """An unknown id whose nearest are a corpus's long ids would swell the refusal: it echoes nothing instead.

        Both nearest rules are reached: the page and its own sections (``p#…``), the whole guide's (``q#…``).
        """
        long = "zebra-" + "x" * 4_500
        entries = [(None, 1, ["Page"], "")] + [(f"{long}{n}", 2, ["Page", "Zebra"], "zebra") for n in range(3)]
        installed(package=_hand_corpus(("p", "Page", [], entries)))
        reply = answer(section_id=requested)
        assert reply == guide_search.over_budget_refusal()
        assert "zebra" not in reply and escaped_bytes(reply) <= GUIDE_REPLY_BUDGET_WIRE_BYTES

    def test_every_refusal_at_its_longest_echo_is_within_the_budget(self) -> None:
        """Each composer at the longest text it may echo: a request's cap, a version's cap, the nearest ids."""
        request = '"\\' * (REQUEST_CAP_CHARACTERS // 2)
        version = "9" * CORPUS_VERSION_CAP_CHARACTERS
        refusals = {
            "guide-missing": guide_search.missing_refusal(),
            "guide-version": guide_search.version_refusal(version, __version__),
            "guide-unreadable": guide_search.unreadable_refusal(),
            "guide-mode": guide_search.mode_refusal(),
            "guide-unknown-section": guide_search.unknown_section_refusal(request, [request] * NEAREST_LIMIT),
            "guide-unknown-part": guide_search.unknown_part_refusal(request, 10**6),
            "guide-request-too-long": guide_search.request_too_long_refusal(),
            "guide-over-budget": guide_search.over_budget_refusal(request),
        }
        assert set(refusals) == set(guide_search.REFUSAL_CODES)
        for code, reply in refusals.items():
            assert _refusal_code(reply) == code
            assert escaped_bytes(reply) <= GUIDE_REPLY_BUDGET_WIRE_BYTES, code
        assert escaped_bytes(guide_search.over_budget_refusal()) <= GUIDE_REPLY_BUDGET_WIRE_BYTES


# The ranking


class TestTheTokens:
    def test_lowercased_runs_of_letters_and_digits(self) -> None:
        assert tokens("Check-Model: ARXML 4.3.0") == ["check", "model", "arxml", "4", "3", "0"]

    def test_nfkc_first(self) -> None:
        # Fullwidth letters and a ligature fold to their plain forms.
        assert tokens("ＡＲＸＭＬ ﬁle") == ["arxml", "file"]

    def test_a_plural_s_is_dropped_from_a_token_longer_than_three(self) -> None:
        assert tokens("ports bus runnables its") == ["port", "bus", "runnable"]

    def test_a_stop_word_is_recognised_before_the_s_is_dropped(self) -> None:
        assert tokens("does this") == []

    def test_the_stop_words_are_forty_six_pinned_words(self) -> None:
        assert len(STOP_WORDS) == 46
        assert all(word.isascii() and word.isalpha() and word == word.lower() for word in STOP_WORDS)
        assert {"the", "how", "what", "is"} <= STOP_WORDS

    def test_the_auxiliaries_and_the_product_name_are_stop_words(self) -> None:
        """Five auxiliaries and ``swcraft`` are stop words; ``had`` is not one."""
        assert {"has", "have", "will", "shall", "should", "swcraft"} <= STOP_WORDS
        assert "had" not in STOP_WORDS
        assert tokens("SWCraft has, will and should have shall") == []


class TestTheWeights:
    def test_the_constants(self) -> None:
        assert (HEADING_WEIGHT, ANCESTOR_WEIGHT, PURPOSE_WEIGHT, PAGE_WEIGHT, BODY_WEIGHT, BODY_COUNT_CAP) == (
            8,
            4,
            4,
            3,
            1,
            3,
        )

    def test_the_purpose_weighs_as_an_ancestor_heading(self) -> None:
        assert PURPOSE_WEIGHT == ANCESTOR_WEIGHT
        assert (SEARCH_HIT_LIMIT, NEAREST_LIMIT) == (5, 3)

    def test_the_sections_own_heading(self) -> None:
        assert score(_index(_one(heading="Connector")), "p#s", "connector") == (1, HEADING_WEIGHT)

    def test_an_ancestor_heading(self) -> None:
        corpus = _one(heading="Other", ancestor="Connector")
        assert score(_index(corpus), "p#s", "connector") == (1, ANCESTOR_WEIGHT)

    def test_the_page_title(self) -> None:
        assert score(_index(_one(title="Connector page")), "p#s", "connector") == (1, PAGE_WEIGHT)

    def test_a_page_keyword(self) -> None:
        assert score(_index(_one(keywords=["wiring, link"])), "p#s", "wiring") == (1, PAGE_WEIGHT)

    def test_a_purpose_alone(self) -> None:
        """A word the section's purpose holds and nothing else of the section does."""
        corpus = _one(purpose="When two connectors must be wired")
        assert score(_index(corpus), "p#s", "wiring wired") == (1, PURPOSE_WEIGHT)
        assert [section_id for section_id, _ in rank(_index(corpus), "wired")] == ["p#s"]

    def test_a_purpose_word_counts_once_however_often_it_stands(self) -> None:
        corpus = _one(purpose="wired, wired and wired again")
        assert score(_index(corpus), "p#s", "wired") == (1, PURPOSE_WEIGHT)

    def test_a_purpose_adds_to_the_heading_and_the_body(self) -> None:
        corpus = _one(heading="Wired", body="wired", purpose="When it is wired")
        assert score(_index(corpus), "p#s", "wired") == (1, HEADING_WEIGHT + PURPOSE_WEIGHT + BODY_WEIGHT)

    def test_a_purpose_is_its_own_sections_alone(self) -> None:
        """A section's purpose does not reach its page or its sibling."""
        corpus = _one(purpose="wired")
        index = _index(corpus)
        assert score(index, "p", "wired") == (0, 0)

    def test_a_keyword_and_the_title_count_once(self) -> None:
        corpus = _one(title="Wiring", keywords=["wiring"])
        assert score(_index(corpus), "p#s", "wiring") == (1, PAGE_WEIGHT)

    @pytest.mark.parametrize("count", [1, 2, 3, 4, 9])
    def test_the_body_up_to_its_cap(self, count: int) -> None:
        """The body scores up to its cap; under the default it adds nothing to the coverage."""
        corpus = _one(body=" ".join(["connector"] * count))
        assert score(_index(corpus), "p#s", "connector") == (0, BODY_WEIGHT * min(count, BODY_COUNT_CAP))

    def test_the_weights_add_up_per_distinct_token(self) -> None:
        corpus = _one(
            heading="Connector",
            ancestor="Connector",
            title="Connector",
            body="connector connector",
            purpose="A connector",
        )
        expected = HEADING_WEIGHT + ANCESTOR_WEIGHT + PURPOSE_WEIGHT + PAGE_WEIGHT + 2 * BODY_WEIGHT
        assert score(_index(corpus), "p#s", "connector connectors") == (1, expected)

    def test_the_body_is_the_records_own_body(self) -> None:
        """The body scored is ``own``, never the subtree's parts: a child's words are not the parent's body."""
        corpus = _hand_corpus(
            (
                "p",
                "Page",
                [],
                [
                    (None, 1, ["Page"], "lead"),
                    ("a", 2, ["Page", "Parent"], "own words"),
                    ("b", 3, ["Page", "Parent", "Child"], "connector"),
                ],
            )
        )
        corpus["sections"][1]["parts"] = [
            "## Parent { #a }\n\nown words\n\n### Child { #b }\n\nconnector",
        ]
        index = _index(corpus)
        assert score(index, "p#a", "connector") == (0, 0)
        assert score(index, "p#a", "parent") == (1, HEADING_WEIGHT)
        assert score(index, "p#a", "own words") == (0, 2 * BODY_WEIGHT)
        assert score(index, "p", "connector") == (0, 0)
        assert score(index, "p", "lead") == (0, BODY_WEIGHT)


class TestTheOrder:
    def test_more_distinct_tokens_first(self) -> None:
        corpus = _hand_corpus(
            (
                "p",
                "Page",
                [],
                [
                    (None, 1, ["Page"], ""),
                    ("strong", 2, ["Page", "Connector connector"], "connector connector connector"),
                    ("broad", 2, ["Page", "Other"], "connector port"),
                ],
            )
        )
        # The whole-body weights count a body match toward the coverage.
        ranked = rank(_measured(corpus), "connector port", ranker=RANKER_WEIGHTS)
        assert [section_id for section_id, _ in ranked] == ["p#broad", "p#strong"]

    def test_the_default_counts_the_coverage_over_the_non_body_fields(self) -> None:
        """Under the default the coverage is the heading, ancestor, purpose and page fields; the body scores."""
        corpus = _hand_corpus(
            (
                "p",
                "Page",
                [],
                [
                    (None, 1, ["Page"], ""),
                    ("strong", 2, ["Page", "Connector connector"], "connector connector connector"),
                    ("broad", 2, ["Page", "Connector port"], ""),
                    ("body", 2, ["Page", "Other"], "connector port"),
                ],
            )
        )
        assert [section_id for section_id, _ in rank(_index(corpus), "connector port")] == [
            "p#broad",
            "p#strong",
            "p#body",
        ]

    def test_then_the_score(self) -> None:
        corpus = _hand_corpus(
            (
                "p",
                "Page",
                [],
                [(None, 1, ["Page"], ""), ("a", 2, ["Page", "Other"], "port"), ("b", 2, ["Page", "Port"], "")],
            )
        )
        assert [section_id for section_id, _ in rank(_index(corpus), "port")] == ["p#b", "p#a"]

    def test_no_page_name_is_ordered_apart(self) -> None:
        """A page named like the retired release notes is ranked as any other: by coverage, score and id."""
        corpus = _hand_corpus(
            ("a-page", "A", [], [(None, 1, ["A"], ""), ("x", 2, ["A", "Other"], "port")]),
            ("release-notes", "Notes", [], [(None, 1, ["Notes"], ""), ("x", 2, ["Notes", "Other"], "port")]),
            ("z-page", "Z", [], [(None, 1, ["Z"], ""), ("x", 2, ["Z", "Other"], "port")]),
        )
        assert [section_id for section_id, _ in rank(_index(corpus), "port")] == [
            "a-page#x",
            "release-notes#x",
            "z-page#x",
        ]

    def test_then_the_id_ascending(self) -> None:
        corpus = _hand_corpus(
            (
                "p",
                "Page",
                [],
                [(None, 1, ["Page"], ""), ("b", 2, ["Page", "Other"], "port"), ("a", 2, ["Page", "Else"], "port")],
            )
        )
        assert [section_id for section_id, _ in rank(_index(corpus), "port")] == ["p#a", "p#b"]

    def test_a_section_matching_no_token_is_never_a_hit(self) -> None:
        assert rank(_index(_one(body="port")), "zeppelin") == []


class TestTheExactLookup:
    @pytest.fixture
    def index(self, fixture_corpus: dict) -> SearchIndex:
        return _index(fixture_corpus)

    def test_a_refusal_code_is_the_first_hit(self, index: SearchIndex) -> None:
        assert rank(index, "unknown-layer")[0] == ("how-to/refusals#unknown-element", 1)

    @pytest.mark.parametrize("prefix", ["", "rule:"])
    def test_a_rule_id_with_or_without_its_prefix(self, index: SearchIndex, prefix: str) -> None:
        key, entry = next(
            (key, entry) for key, entry in index.guide.corpus["explains"].items() if key.startswith("rule:")
        )
        assert rank(index, prefix + key.removeprefix("rule:"))[0] == (entry["section"], entry["part"])

    def test_a_section_id_is_the_first_hit_at_part_one(self, index: SearchIndex) -> None:
        assert rank(index, "index#where-to-go-next")[0] == ("index#where-to-go-next", 1)

    def test_the_exact_hit_is_not_ranked_again(self, index: SearchIndex) -> None:
        ids = [section_id for section_id, _ in rank(index, "index#sections")]
        assert ids.count("index#sections") == 1


class TestTheOverviewRule:
    """An explicit *what is SWCraft* query ranks the home page first; nothing else is routed."""

    @pytest.fixture
    def index(self) -> SearchIndex:
        return _index(
            _hand_corpus(
                ("index", "SWCraft", [], [(None, 1, ["SWCraft"], "a tool")]),
                (
                    "install",
                    "Install SWCraft",
                    [],
                    [(None, 1, ["Install SWCraft"], "install swcraft from its archive")],
                ),
                ("cli", "SWCraft commands", [], [(None, 1, ["SWCraft commands"], "swcraft validate, swcraft build")]),
            )
        )

    def test_the_constants(self) -> None:
        assert OVERVIEW_SECTION == "index"
        assert OVERVIEW_QUERIES == {
            "what is swcraft",
            "what s swcraft",
            "what is swcraft for",
            "what is swcraft used for",
            "what does swcraft do",
            "what can swcraft do",
            "overview of swcraft",
            "an overview of swcraft",
            "swcraft overview",
            "introduction to swcraft",
            "about swcraft",
        }

    @pytest.mark.parametrize(
        "query",
        ["what is swcraft", "What is SWCraft?", "  what's SWCraft ", "What does SWCraft do?", "overview of SWCraft"],
    )
    def test_an_overview_query_ranks_the_home_page_first(self, index: SearchIndex, query: str) -> None:
        ranked = [section_id for section_id, _ in rank(index, query)]
        assert ranked[0] == "index"
        assert ranked.count("index") == 1

    @pytest.mark.parametrize(
        "query",
        ["install swcraft", "swcraft validate command", "swcraft build", "what is swcraft validate", "swcraft"],
    )
    def test_an_installation_or_a_command_query_is_not_routed(self, index: SearchIndex, query: str) -> None:
        """Negative controls: each is ranked by its words alone, as if the rule did not exist."""
        by_words = sorted(
            (section_id for section_id in index.scored if score(index, section_id, query) != (0, 0)),
            key=lambda section_id: (*(-value for value in score(index, section_id, query)), section_id),
        )
        assert [section_id for section_id, _ in rank(index, query)] == by_words

    def test_the_rule_needs_the_home_page(self) -> None:
        """Without the home page an overview query is its words alone: stop words, no hit."""
        index = _index(_hand_corpus(("other", "Other", [], [(None, 1, ["Other"], "swcraft")])))
        assert [section_id for section_id, _ in rank(index, "what is swcraft")] == []

    def test_the_rule_reads_the_query_before_its_stop_words_go(self, index: SearchIndex) -> None:
        """Every accepted form is stop words or nearly: the rule still ranks the home page first."""
        assert tokens("what is swcraft") == []
        for query in sorted(OVERVIEW_QUERIES):
            assert rank(index, query)[0] == ("index", 1), query

    @pytest.mark.parametrize("query", ["swcraft", "SWCraft?", "what should it have", "how will I"])
    def test_stop_words_the_rule_does_not_cover_rank_nothing(self, index: SearchIndex, query: str) -> None:
        """Negative controls: a query of stop words alone that is no overview form ranks no section."""
        assert rank(index, query) == []

    def test_the_rule_does_not_apply_among_a_subset(self, index: SearchIndex) -> None:
        """Among a subset the query is its words alone: here stop words, so not even the home page."""
        assert rank(index, "what is swcraft", ["index", "cli"]) == []
        assert [section_id for section_id, _ in rank(index, "what is swcraft validate", ["index", "cli"])] == ["cli"]


# The measurement rankers


def _ids(ranked: list[tuple[str, int]]) -> list[str]:
    return [section_id for section_id, _ in ranked]


class TestTheRankers:
    def test_the_constants(self) -> None:
        assert RANKERS == (
            RANKER_WEIGHTS,
            RANKER_WEIGHTS_FIELDS,
            RANKER_WEIGHTS_NORM,
            RANKER_WEIGHTS_PARTS,
            RANKER_WEIGHTS_FIELDS_NORM,
            RANKER_WEIGHTS_FIELDS_PARTS,
            RANKER_WEIGHTS_NORM_PARTS,
            RANKER_WEIGHTS_FIELDS_NORM_PARTS,
            RANKER_BM25,
            RANKER_BM25F,
        )
        assert (RANKERS[0], *RANKERS[-2:]) == ("weights", "bm25", "bm25f")
        assert DEFAULT_RANKER == RANKER_WEIGHTS_FIELDS_PARTS == "weights-fields-parts"
        assert BM25F_FIELDS == ("heading", "ancestors", "purpose", "page", "body")
        assert AUXILIARY_STOP_WORDS == {"has", "have", "will", "shall", "should", "swcraft"} <= STOP_WORDS

    def test_the_weights_variants_are_the_three_structural_changes_and_their_combinations(self) -> None:
        settings = {
            name: (variant.body_in_coverage, variant.body_normalised, variant.per_part)
            for name, variant in WEIGHTS_VARIANTS.items()
        }
        assert settings == {
            "weights": (True, False, False),
            "weights-fields": (False, False, False),
            "weights-norm": (True, True, False),
            "weights-parts": (True, False, True),
            "weights-fields-norm": (False, True, False),
            "weights-fields-parts": (False, False, True),
            "weights-norm-parts": (True, True, True),
            "weights-fields-norm-parts": (False, True, True),
        }
        assert all(isinstance(variant, WeightsVariant) for variant in WEIGHTS_VARIANTS.values())
        assert set(WEIGHTS_VARIANTS) <= set(RANKERS)
        assert BM25F_FIELDS == ("heading", "ancestors", "purpose", "page", "body")
        assert AUXILIARY_STOP_WORDS == {"has", "have", "will", "shall", "should", "swcraft"} <= STOP_WORDS

    def test_bm25fs_boosts_are_the_default_rankers_weights(self) -> None:
        assert dict(BM25F_BOOSTS) == {
            "heading": HEADING_WEIGHT,
            "ancestors": ANCESTOR_WEIGHT,
            "purpose": PURPOSE_WEIGHT,
            "page": PAGE_WEIGHT,
            "body": BODY_WEIGHT,
        }

    def test_a_measurement_index_takes_the_tool_s_stop_words_unless_it_names_others(self) -> None:
        """Both BM25 rankers measured better with the tool's stop words on the agent's queries."""
        assert _measured(_one(body="port")).stop_words == STOP_WORDS

    def test_the_default_is_the_per_part_fields_weights_and_the_tool_answers_with_it(
        self, installed: Callable[..., None], fixture_corpus: dict
    ) -> None:
        installed(package=fixture_corpus)
        index = _index(fixture_corpus)
        assert type(load_index()) is SearchIndex  # the tool never builds a measurement index
        assert rank(index, "page section") == rank(index, "page section", ranker=RANKER_WEIGHTS_FIELDS_PARTS)
        assert answer(search_text="page section") == guide_search.search(
            index, "page section", ranker=RANKER_WEIGHTS_FIELDS_PARTS
        )

    def test_an_unknown_ranker_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown ranker"):
            rank(_index(_one(body="port")), "port", ranker="fts5")

    @pytest.mark.parametrize("ranker", list(WEIGHTS_VARIANTS))
    def test_a_weights_ranker_takes_no_other_stop_words(self, ranker: str) -> None:
        with pytest.raises(ValueError, match="STOP_WORDS"):
            rank(_measured(_one(body="port"), frozenset()), "port", ranker=ranker)

    def test_plain_bm25_is_the_shared_bm25_over_one_bag_of_every_field(self) -> None:
        corpus = _hand_corpus(
            (
                "p",
                "Wiring page",
                ["harness"],
                [
                    (None, 1, ["Wiring page"], ""),
                    ("a", 2, ["Wiring page", "Port"], "port port connector"),
                    ("b", 2, ["Wiring page", "Other"], "connector"),
                ],
            )
        )
        index = _measured(corpus)
        bags = {
            "p": ["wiring", "page", "harness", "wiring", "page"],
            "p#a": ["port", "wiring", "page", "harness", "port", "port", "connector"],
            "p#b": ["other", "wiring", "page", "harness", "connector"],
        }
        expected = Corpus()
        for key, bag in bags.items():
            expected.add(key, bag)
        found = guide_search.bm25_ranking_scores(index, "port connector harness", RANKER_BM25)
        assert found == bm25_scores(expected, ["connector", "harness", "port"])
        assert _ids(rank(index, "port connector harness", ranker=RANKER_BM25)) == sorted(
            found, key=lambda key: (-found[key], key)
        )

    def test_bm25f_is_the_shared_bm25f_over_the_five_fields_with_the_boosts(self) -> None:
        corpus = _one(body="port port", heading="Port", ancestor="Wiring", purpose="when a port moves", title="Ports")
        index = _measured(corpus)
        found = guide_search.bm25_ranking_scores(index, "port wiring", RANKER_BM25F)
        expected = FieldCorpus(BM25F_FIELDS)
        page = ["port"]
        expected.add("p", {"heading": ["port"], "page": page})
        expected.add("p#a", {"heading": ["wiring"], "page": page})
        expected.add(
            "p#s",
            {
                "heading": ["port"],
                "ancestors": ["wiring"],
                "purpose": ["port", "move"],
                "page": page,
                "body": ["port", "port"],
            },
        )
        assert found == bm25f_scores(expected, ["port", "wiring"], BM25F_BOOSTS)

    def test_a_heading_outweighs_the_same_word_in_the_body_under_bm25f(self) -> None:
        corpus = _hand_corpus(
            (
                "p",
                "Page",
                [],
                [(None, 1, ["Page"], ""), ("a", 2, ["Page", "Other"], "port"), ("b", 2, ["Page", "Port"], "other")],
            )
        )
        assert _ids(rank(_measured(corpus), "port", ranker=RANKER_BM25F)) == ["p#b", "p#a"]

    @pytest.mark.parametrize("ranker", [RANKER_BM25, RANKER_BM25F])
    def test_a_tie_is_broken_by_the_id(self, ranker: str) -> None:
        corpus = _hand_corpus(
            (
                "p",
                "Page",
                [],
                [(None, 1, ["Page"], ""), ("b", 2, ["Page", "Other"], "port"), ("a", 2, ["Page", "Other"], "port")],
            )
        )
        assert _ids(rank(_measured(corpus), "port", ranker=ranker)) == ["p#a", "p#b"]

    @pytest.mark.parametrize("ranker", RANKERS)
    def test_deterministic(self, ranker: str, fixture_corpus: dict) -> None:
        first = rank(_measured(fixture_corpus), "page section guide", ranker=ranker)
        assert first == rank(_measured(fixture_corpus), "page section guide", ranker=ranker)
        assert first

    @pytest.mark.parametrize("ranker", [RANKER_BM25, RANKER_BM25F])
    def test_a_section_no_token_reaches_is_never_a_hit(self, ranker: str) -> None:
        assert rank(_measured(_one(body="port")), "zeppelin", ranker=ranker) == []

    @pytest.mark.parametrize("ranker", RANKERS)
    def test_the_exact_lookup_stands_in_front(self, ranker: str, fixture_corpus: dict) -> None:
        index = _measured(fixture_corpus)
        assert rank(index, "unknown-layer", ranker=ranker)[0] == ("how-to/refusals#unknown-element", 1)
        ids = _ids(rank(index, "index#sections", ranker=ranker))
        assert ids[0] == "index#sections" and ids.count("index#sections") == 1

    @pytest.mark.parametrize("ranker", RANKERS)
    def test_the_overview_rule_stands_in_front(self, ranker: str) -> None:
        index = _measured(
            _hand_corpus(
                ("index", "SWCraft", [], [(None, 1, ["SWCraft"], "a tool")]),
                ("cli", "SWCraft commands", [], [(None, 1, ["SWCraft commands"], "swcraft validate")]),
            )
        )
        for query in sorted(OVERVIEW_QUERIES):
            assert rank(index, query, ranker=ranker)[0] == ("index", 1), query

    def test_a_stop_word_override_reaches_the_bm25_rankers_alone(self) -> None:
        corpus = _one(body="swcraft port")
        assert rank(_measured(corpus), "swcraft", ranker=RANKER_BM25) == []
        without = STOP_WORDS - AUXILIARY_STOP_WORDS
        assert _ids(rank(_measured(corpus, without), "swcraft", ranker=RANKER_BM25)) == ["p#s"]

    @pytest.mark.parametrize("ranker", [RANKER_BM25, RANKER_BM25F, RANKER_WEIGHTS, RANKER_WEIGHTS_FIELDS_NORM])
    def test_a_whole_body_or_bm25_ranker_reads_a_measurement_index_alone(self, ranker: str) -> None:
        """The index the tool loads holds the per-part units alone; the rest is built by a measurement."""
        with pytest.raises(ValueError, match="MeasurementIndex"):
            rank(_index(_one(body="port")), "port", ranker=ranker)

    @pytest.mark.parametrize("ranker", [name for name, variant in WEIGHTS_VARIANTS.items() if variant.per_part])
    def test_a_per_part_ranker_reads_the_tool_s_index(self, ranker: str) -> None:
        assert _ids(rank(_index(_one(body="port")), "port", ranker=ranker)) == ["p#s"]

    def test_the_indexes_are_immutable(self, fixture_corpus: dict) -> None:
        """Every structure is built at once; nothing is filled in on first use."""
        index = _measured(fixture_corpus)
        for value in (index.scored, index.units, index.whole_units, index.guide.sections, index.guide.toc):
            with pytest.raises(TypeError):
                value["x"] = None  # type: ignore[index]
        with pytest.raises(AttributeError):
            index.units = {}  # type: ignore[misc]
        unit = index.units["index"][0]
        with pytest.raises(TypeError):
            unit.body["x"] = 1  # type: ignore[index]


def _long_section_corpus() -> dict[str, Any]:
    """A page with a two-part section ``p#long`` and a one-part section ``p#short``.

    ``p#long``'s own body runs to two parts (*alpha* in the first, *beta* in the second); ``p#short``'s heading
    says *Alpha* and its body is short.
    """
    corpus = _hand_corpus(
        (
            "p",
            "Page",
            [],
            [
                (None, 1, ["Page"], ""),
                ("long", 2, ["Page", "Long"], "alpha"),
                ("short", 2, ["Page", "Alpha"], "gamma"),
            ],
        )
    )
    long = next(section for section in corpus["sections"] if section["id"] == "p#long")
    long["parts"] = [long["parts"][0] + "\n\nfiller words", "beta beta beta and more filler words"]
    long["own"] = ["alpha\n\nfiller words", "beta beta beta and more filler words"]
    return corpus


class TestTheWeightsVariants:
    def test_a_per_part_ranker_ranks_one_part_bodies_as_its_whole_body_twin(self, fixture_corpus: dict) -> None:
        """Where every own body fits one part, the units are the sections and the two rankings are one."""
        assert all(len(section["own"]) == 1 for section in fixture_corpus["sections"])
        index = _measured(fixture_corpus)
        for query in ("page section", "guide", "unknown layer refusal", "sections of the index"):
            assert rank(index, query, ranker=RANKER_WEIGHTS_PARTS) == rank(index, query, ranker=RANKER_WEIGHTS), query
            # The default is the per-part twin of the fields-coverage ranker.
            assert rank(index, query) == rank(index, query, ranker=RANKER_WEIGHTS_FIELDS), query

    def test_a_section_has_one_unit_per_part_its_own_body_reaches(self) -> None:
        index = _measured(_long_section_corpus())
        assert [unit.part for unit in index.units["p#long"]] == [1, 2]
        assert [unit.part for unit in index.whole_units["p#long"]] == [1]
        assert index.whole_units["p#long"][0].body == {"alpha": 1, "filler": 2, "word": 2, "beta": 3, "more": 1}

    def test_the_default_ranks_a_long_section_by_its_best_part_and_reads_that_part(self) -> None:
        index = _measured(_long_section_corpus())
        # The whole body covers both words, so the long section leads the heading match.
        assert _ids(rank(index, "alpha beta", ranker=RANKER_WEIGHTS))[:2] == ["p#long", "p#short"]
        # Per part, no part of it covers both: the heading match leads.
        assert rank(index, "alpha beta")[:2] == [("p#short", 1), ("p#long", 2)]
        assert rank(index, "beta") == [("p#long", 2)]
        reply = guide_search.search(index, "beta")
        assert {"section": "p#long", "part": 2} in [hit["read"] for hit in reply["hits"]]

    def test_a_tie_between_parts_reads_the_first(self) -> None:
        corpus = _long_section_corpus()
        long = next(section for section in corpus["sections"] if section["id"] == "p#long")
        long["parts"][1] = long["own"][1] = "alpha and more filler words"
        assert ("p#long", 1) in rank(_index(corpus), "alpha")

    @pytest.mark.parametrize("ranker", [RANKER_WEIGHTS_FIELDS, RANKER_WEIGHTS_FIELDS_NORM])
    def test_without_the_body_in_the_coverage_a_heading_match_leads_a_body_covering_more(self, ranker: str) -> None:
        corpus = _hand_corpus(
            (
                "p",
                "Page",
                [],
                [(None, 1, ["Page"], ""), ("a", 2, ["Page", "Other"], "port wire"), ("b", 2, ["Page", "Port"], "x")],
            )
        )
        index = _measured(corpus)
        assert _ids(rank(index, "port wire", ranker=RANKER_WEIGHTS)) == ["p#a", "p#b"]
        # The body-only section is still a hit, after the heading match.
        assert _ids(rank(index, "port wire", ranker=ranker)) == ["p#b", "p#a"]

    @pytest.mark.parametrize("ranker", [RANKER_WEIGHTS_NORM, RANKER_WEIGHTS_FIELDS_NORM])
    def test_a_normalised_body_credits_a_word_in_a_short_body_above_three_in_a_long_one(self, ranker: str) -> None:
        filler = " ".join(f"filler{number}" for number in range(97))
        corpus = _hand_corpus(
            (
                "p",
                "Page",
                [],
                [
                    (None, 1, ["Page"], ""),
                    ("a", 2, ["Page", "Long"], f"port port port {filler}"),
                    ("b", 2, ["Page", "Short"], "port"),
                ],
            )
        )
        index = _measured(corpus)
        assert _ids(rank(index, "port", ranker=RANKER_WEIGHTS)) == ["p#a", "p#b"]
        assert _ids(rank(index, "port", ranker=ranker)) == ["p#b", "p#a"]


# The leaf

ALLOWED_PACKAGE_IMPORTS = {
    "swcraft_tools._version",
    "swcraft_tools.bm25",
    "swcraft_tools.guide_site",
    "swcraft_tools.wire_unit",
}


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module == "swcraft_tools":
                modules.update(f"swcraft_tools.{alias.name}" for alias in node.names)
            else:
                modules.add(node.module)
    return modules


class TestTheModuleIsALeaf:
    def test_it_imports_the_standard_library_bm25_guide_site_version_and_wire_unit_alone(self) -> None:
        modules = _imported_modules(ast.parse(MODULE_PATH.read_text(encoding="utf-8")))
        foreign = sorted(
            module
            for module in modules
            if module.split(".")[0] not in sys.stdlib_module_names
            and module != "__future__"
            and module not in ALLOWED_PACKAGE_IMPORTS
        )
        assert foreign == [], (
            f"guide_search.py may import the stdlib, bm25, guide_site, _version and wire_unit; found {foreign}"
        )

    def test_the_scan_sees_a_planted_package_import(self) -> None:
        modules = _imported_modules(ast.parse("from swcraft_tools import project, guide_site\n"))
        assert sorted(modules - ALLOWED_PACKAGE_IMPORTS) == ["swcraft_tools.project"]

    def test_it_never_reaches_the_override_or_the_search_that_honours_it(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        assert not attributes & {"env_site_dir", "site_root", "GUIDE_SITE_ENV", "environ"}


# The retrieval pairs, over the real guide


@pytest.fixture(scope="module")
def real_index(tmp_path_factory: pytest.TempPathFactory) -> SearchIndex:
    """The real guide's corpus, built in-process with the release notes the build selects."""
    root = tmp_path_factory.mktemp("real-guide")
    guide = (REPO_ROOT / "docs" / "guide").resolve()
    shutil.copytree(
        guide,
        root / "docs" / "guide",
        ignore=lambda directory, names: [
            name for name in names if Path(directory).resolve() == guide and name in ("release-notes", "version.md")
        ],
    )
    shutil.copytree(REPO_ROOT / "docs" / "release-notes", root / "docs" / "release-notes")
    build_artifacts_module().write_guide_release_notes(root)
    return _index(build_corpus(root / "docs" / "guide", mkdocs_config=REPO_ROOT / "mkdocs.yml"))


def _editor_pattern(name: str) -> re.Pattern[str]:
    """The body of ``helpLinks.ts``' anchored literal *name* (``/^…$/``), compiled for ``fullmatch``."""
    literal = help_links_constant(name)
    found = re.fullmatch(r"/\^(.+)\$/", literal)
    assert found is not None, f"helpLinks.ts' {name} is one anchored literal: {literal}"
    return re.compile(found.group(1))


class TestTheSectionLink:
    """The ``link`` a section reply and a search hit carry, and the editor handler that opens it.

    **What this does not reach:** whether the editor's chat renders the link clickable and hands it to the
    handler (a manual check), and the two regex dialects, which read the character classes alike.
    """

    def test_the_prefix_names_the_extension_of_this_repository(self) -> None:
        """The authority is ``<publisher>.<name>`` of the extension's ``package.json``, read, never transcribed."""
        manifest = json.loads((REPO_ROOT / "vscode_extension" / "package.json").read_text(encoding="utf-8"))
        assert SECTION_LINK_PREFIX == f"vscode://{manifest['publisher']}.{manifest['name']}/guide?section="

    def test_the_link_is_the_prefix_and_the_id(self) -> None:
        assert section_link("how-to/connect-components#before-you-start") == (
            "vscode://swcraft-local.swcraft-vscode/guide?section=how-to/connect-components#before-you-start"
        )

    def test_a_section_reply_and_every_search_hit_carry_their_link(
        self, installed: Callable[..., None], fixture_corpus: dict
    ) -> None:
        installed(package=fixture_corpus)
        reply = answer(section_id="index#sections")
        assert isinstance(reply, dict)
        assert reply["link"] == section_link("index#sections")
        found = answer(search_text="guide page section reference release")
        assert isinstance(found, dict)
        assert found["hits"]
        assert all(hit["link"] == section_link(hit["id"]) for hit in found["hits"])
        toc = answer(toc_wanted=True)
        assert isinstance(toc, dict)
        assert all("link" not in entry for entry in toc["pages"])

    def test_every_id_of_the_real_guide_is_one_the_editor_opens(self, real_index: SearchIndex) -> None:
        """Each id's page and anchor pass the handler's patterns, within its bound, so no link is refused there."""
        page_pattern = _editor_pattern("SECTION_PAGE_PATTERN")
        anchor_pattern = _editor_pattern("SECTION_ANCHOR_PATTERN")
        limit = int(help_links_constant("SECTION_ID_LIMIT"))
        refused = []
        for section_id in real_index.guide.sections:
            page, _, anchor = section_id.partition("#")
            if (
                page_pattern.fullmatch(page) is None
                or (anchor and anchor_pattern.fullmatch(anchor) is None)
                or len(section_id) > limit
            ):
                refused.append(section_id)
        assert refused == []
        assert limit == REQUEST_CAP_CHARACTERS


def _pairs() -> list[dict[str, Any]]:
    return json.loads(PAIRS.read_text(encoding="utf-8"))["pairs"]


class TestTheRetrievalPairs:
    @pytest.mark.parametrize("pair", _pairs(), ids=lambda pair: f"{pair['kind']}:{pair['query']}")
    def test_the_pair(self, real_index: SearchIndex, pair: dict[str, Any]) -> None:
        ids = [section_id for section_id, _ in rank(real_index, pair["query"])][:SEARCH_HIT_LIMIT]
        if pair["kind"] in ("paraphrase", "synonym", "observed"):
            assert set(pair["expect"]) & set(ids[:3]), f"none of {pair['expect']} in the first 3: {ids[:3]}"
        elif pair["kind"] == "ambiguous":
            assert set(pair["expect"]) <= set(ids), f"{pair['expect']} not all in the first 5: {ids}"
        else:
            assert pair["kind"] == "no-match"
            assert not set(pair["wrong"]) & set(ids), f"a wrong id among {ids}"

    def test_every_expected_id_is_in_the_corpus(self, real_index: SearchIndex) -> None:
        missing = sorted(
            {section_id for pair in _pairs() for section_id in pair.get("expect", []) + pair.get("wrong", [])}
            - set(real_index.guide.sections)
        )
        assert missing == []

    def test_every_narrative_page_has_two_paraphrases_and_a_synonym(self) -> None:
        pages = {name[: -len(".md")] for name in json.loads(ANCHOR_RECORD.read_text(encoding="utf-8"))["pages"]}
        for page in sorted(pages):
            kinds = [pair["kind"] for pair in _pairs() if pair["page"] == page]
            assert kinds.count("paraphrase") >= 2 and kinds.count("synonym") >= 1, page

    def test_every_how_to_page_has_two_paraphrases_and_a_synonym(self) -> None:
        """The How-to pages are held by the same rule as the narrative pages, read off the disk."""
        how_to = REPO_ROOT / "docs" / "guide" / "how-to"
        pages = {
            "how-to" if path.stem == "index" else f"how-to/{path.stem}"
            for path in sorted(how_to.glob("*.md"), key=lambda path: path.as_posix())
        }
        assert pages
        for page in sorted(pages):
            kinds = [pair["kind"] for pair in _pairs() if pair["page"] == page]
            assert kinds.count("paraphrase") >= 2 and kinds.count("synonym") >= 1, page

    def test_every_protocol_code_is_found_first_by_exact_lookup(self, real_index: SearchIndex) -> None:
        """A refusal code typed as the query lands on its *Refusals* section, first, at its part."""
        explained = {key: entry for key, entry in real_index.guide.corpus["explains"].items() if ":" not in key}
        assert len(explained) == 11
        for code, entry in explained.items():
            assert rank(real_index, code)[0] == (entry["section"], entry["part"]), code
            assert entry["section"] == f"how-to/refusals#{code}", code

    def test_an_exact_rule_lookup_lands_on_its_part_past_the_first(self, real_index: SearchIndex) -> None:
        key, entry = next(
            (key, entry)
            for key, entry in sorted(real_index.guide.corpus["explains"].items())
            if entry["section"] == "reference/rules#checks-between-elements" and entry["part"] > 1
        )
        assert rank(real_index, key.removeprefix("rule:"))[0] == (entry["section"], entry["part"])
        reply = guide_search.section(real_index, entry["section"], entry["part"])
        assert f"`{key}`" in reply["text"]

    def test_every_overview_form_ranks_the_home_page_first(self, real_index: SearchIndex) -> None:
        """On the real guide, ``swcraft`` a stop word: the rule still answers every accepted form."""
        for query in sorted(OVERVIEW_QUERIES):
            assert rank(real_index, query)[0] == (OVERVIEW_SECTION, 1), query

    @pytest.mark.parametrize("query", ["swcraft", "what should it have", "how will I"])
    def test_stop_words_no_overview_form_covers_get_the_no_match_answer(
        self, real_index: SearchIndex, query: str
    ) -> None:
        reply = guide_search.search(real_index, query)
        assert reply["hits"] == [] and reply["more"] is False and reply["note"] == NO_MATCH_SENTENCE

    def test_every_search_reply_of_the_pairs_fits_the_budget(self, real_index: SearchIndex) -> None:
        for pair in _pairs():
            assert measure(guide_search.search(real_index, pair["query"])) <= GUIDE_REPLY_BUDGET_WIRE_BYTES


def test_the_refusals_are_raised_as_guide_refusals(fixture_corpus: dict) -> None:
    index = _index(fixture_corpus)
    with pytest.raises(GuideRefusal) as raised:
        guide_search.section(index, "nowhere")
    assert raised.value.reply.startswith("ERROR: guide-unknown-section: ")
    with pytest.raises(GuideRefusal):
        guide_search.toc(index, "nowhere")
