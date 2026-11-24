"""The guide's corpus producer (:mod:`swcraft_tools.guide_corpus`) and the reply composers.

On small fixture guides (``fixtures/guide_corpus/basic/`` and guides written into ``tmp_path``):
every cut, id, cap and split rule and every refusal, and the composers of
:mod:`swcraft_tools.guide_search` the producer measures with. On the real guide
(:class:`TestTheRealGuide`): a temporary repository holding a copy of ``docs/guide/`` with the
selected release notes written into it by the build script's own ``write_guide_release_notes``,
produced as the docs build produces it.

What this file does not reach: ``version.md``, which the docs build writes and the in-process
corpus lacks (the presence tests cover the built corpus); the ``detail`` reasons inside a
protocol error and the other tools' free ``ERROR:`` texts, which no ``explains`` entry names;
and whether a purpose, or an explanation, says anything true.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT, build_artifacts_module
from swcraft_tools import guide_corpus, guide_search
from swcraft_tools._version import __version__
from swcraft_tools.guide_corpus import GuideCorpusError, build_corpus, check_every_reply, produce
from swcraft_tools.guide_search import (
    GUIDE_REPLY_BUDGET_WIRE_BYTES,
    NO_MATCH_SENTENCE,
    PURPOSE_CAP_CHARACTERS,
    REFUSAL_CODES,
    REQUEST_CAP_CHARACTERS,
    GuideIndex,
    GuideRefusal,
)
from swcraft_tools.wire_unit import measure

FIXTURE = Path(__file__).parent / "fixtures" / "guide_corpus" / "basic"
FIXTURE_GUIDE = FIXTURE / "guide"


@pytest.fixture(scope="module")
def corpus() -> dict[str, Any]:
    return build_corpus(FIXTURE_GUIDE)


@pytest.fixture(scope="module")
def index(corpus: dict[str, Any]) -> GuideIndex:
    return GuideIndex.of(corpus)


def _sections(corpus: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {section["id"]: section for section in corpus["sections"]}


def _guide(tmp_path: Path, pages: dict[str, str]) -> Path:
    """A guide in *tmp_path* with the fixture's ``mkdocs.yml`` and *pages*; returns the guide directory."""
    shutil.copyfile(FIXTURE / "mkdocs.yml", tmp_path / "mkdocs.yml")
    guide = tmp_path / "guide"
    for name, text in pages.items():
        path = guide / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return guide


def _one_page(tmp_path: Path, text: str) -> dict[str, Any]:
    return build_corpus(_guide(tmp_path, {"page.md": text}))


# The cut, the ids and the authoring comments


class TestTheCut:
    def test_every_page_and_every_h2_and_h3_is_an_id_in_document_order(self, corpus: dict[str, Any]) -> None:
        assert [section["id"] for section in corpus["sections"]] == [
            "fenced",
            "fenced#same-heading",
            "fenced#below-it",
            "how-to/refusals",
            "how-to/refusals#stale-baseline",
            "how-to/refusals#unknown-element",
            "index",
            "index#reading",
            "index#pages",
            "index#sections",
            "index#where-to-go-next",
            "reference",
            "reference/rules",
            "reference/rules#checks-on-a-single-element",
        ]

    def test_page_names_are_the_sites_addresses_without_a_trailing_slash(self, corpus: dict[str, Any]) -> None:
        assert [page["page"] for page in corpus["pages"]] == [
            "fenced",
            "how-to/refusals",
            "index",
            "reference",
            "reference/rules",
        ]

    def test_an_explicit_anchor_is_the_id(self, corpus: dict[str, Any]) -> None:
        # `## Reading the guide { #reading }`: the attribute, not the slug of the title.
        assert "index#reading" in _sections(corpus)
        assert "index#reading-the-guide" not in _sections(corpus)

    def test_a_fenced_heading_is_no_heading_and_takes_no_slug(self, corpus: dict[str, Any]) -> None:
        # The page's code block holds `# Fenced headings` and `## Same heading`. Under
        # mkdocs.yml's own list (no `fenced_code`) they would read as an H1 and an H2,
        # and the real `## Same heading` would become `same-heading_1`.
        sections = _sections(corpus)
        assert "fenced#same-heading" in sections
        assert not any(section_id.endswith("_1") for section_id in sections)
        assert [s["level"] for s in corpus["sections"] if s["page"] == "fenced"] == [1, 2, 3]

    def test_the_effective_configuration_adds_fenced_code(self) -> None:
        extensions, configs = guide_corpus.effective_markdown_config(FIXTURE / "mkdocs.yml")
        assert "fenced_code" in extensions
        assert configs["toc"] == {"permalink": True}

    def test_the_levels_and_the_breadcrumbs(self, corpus: dict[str, Any]) -> None:
        sections = _sections(corpus)
        assert sections["index"]["breadcrumb"] == ["Start here"]
        assert sections["index#reading"]["breadcrumb"] == ["Start here", "Reading the guide"]
        assert sections["index#pages"]["breadcrumb"] == ["Start here", "Reading the guide", "Pages"]
        assert [sections[i]["level"] for i in ("index", "index#reading", "index#pages")] == [1, 2, 3]

    def test_a_page_id_reads_the_whole_page(self, corpus: dict[str, Any]) -> None:
        (text,) = _sections(corpus)["index"]["parts"]
        assert text.startswith("# Start here\n\nThe lead of the home page")
        assert "## Where to go next" in text
        assert "#### A deeper heading" in text

    def test_an_h2_reads_its_h3_children_and_stops_at_the_next_h2(self, corpus: dict[str, Any]) -> None:
        (text,) = _sections(corpus)["index#reading"]["parts"]
        assert text.startswith("## Reading the guide { #reading }")
        assert "### Pages" in text
        assert "### Sections" in text
        assert "Where to go next" not in text

    def test_an_h3_reads_its_own_text_and_its_deeper_headings(self, corpus: dict[str, Any]) -> None:
        (text,) = _sections(corpus)["index#pages"]["parts"]
        assert text == "### Pages\n\nA page is one Markdown file.\n\n#### A deeper heading\n\n" + (
            "A deeper heading stays inside its section."
        )

    def test_each_record_carries_its_own_body_without_its_heading_or_its_children(self, corpus: dict[str, Any]) -> None:
        sections = _sections(corpus)
        assert sections["index"]["own"] == ["The lead of the home page, before its first section."]
        assert sections["index#reading"]["own"] == ["The guide has pages, and pages have sections."]
        assert sections["index#pages"]["own"] == [
            "A page is one Markdown file.\n\n#### A deeper heading\n\nA deeper heading stays inside its section."
        ]
        assert sections["index#sections"]["own"] == ["A section has no purpose comment here."]

    def test_a_page_s_own_body_keeps_what_stands_above_its_h1_and_drops_the_h1_line_alone(self, tmp_path: Path) -> None:
        corpus = _one_page(tmp_path, "<!-- a note -->\n\n# Title\n\n```sh\n# Title again\n```\n\n## Child\n\nText.\n")
        assert _sections(corpus)["page"]["own"] == ["<!-- a note -->\n\n```sh\n# Title again\n```"]

    def test_a_section_with_no_text_of_its_own_has_an_empty_own_body(self, tmp_path: Path) -> None:
        corpus = _one_page(tmp_path, "# Title\n\n## Parent\n\n### Child\n\nText.\n")
        assert _sections(corpus)["page#parent"]["own"] == [""]
        assert _sections(corpus)["page"]["own"] == [""]

    def test_the_authoring_comments_are_read_and_removed(self, corpus: dict[str, Any]) -> None:
        sections = _sections(corpus)
        assert sections["index"]["purpose"] == "The fixture guide's home page."
        assert sections["index#reading"]["purpose"] == "How the guide is laid out."
        assert sections["index#sections"]["purpose"] == ""
        pages = {page["page"]: page for page in corpus["pages"]}
        assert pages["index"]["keywords"] == ["welcome", "first steps"]
        assert pages["fenced"]["keywords"] == []
        every_text = "\n".join(text for section in corpus["sections"] for text in section["parts"])
        assert "purpose:" not in every_text
        assert "keywords:" not in every_text
        assert "explains:" not in every_text

    def test_the_release_notes_section_is_not_read(self, corpus: dict[str, Any]) -> None:
        """The fixture holds the section's index and two notes; none is a page, an id or a word of the corpus."""
        assert sorted(path.name for path in (FIXTURE_GUIDE / "release-notes").glob("*.md")) == [
            "0.21.0.md",
            "0.9.0.md",
            "index.md",
        ]
        assert not [page for page in corpus["pages"] if page["page"].startswith(guide_corpus.RELEASE_NOTES_SUBTREE)]
        assert not [s for s in corpus["sections"] if s["id"].startswith(guide_corpus.RELEASE_NOTES_SUBTREE)]
        assert "Release notes of" not in json.dumps(corpus)

    @pytest.mark.parametrize(
        ("relative", "excluded"),
        [
            ("release-notes.md", True),
            ("release-notes/index.md", True),
            ("release-notes/0.21.0.md", True),
            ("release-notes/deeper/page.md", True),
            ("release-notes-and-more.md", False),
            ("upgrading.md", False),
            ("how-to/release-notes.md", False),
        ],
    )
    def test_the_release_notes_rule_takes_the_index_and_its_whole_subtree(self, relative: str, excluded: bool) -> None:
        assert guide_corpus.is_release_notes(relative) is excluded

    def test_a_release_notes_index_written_as_a_file_is_not_read(self, tmp_path: Path) -> None:
        corpus = build_corpus(
            _guide(
                tmp_path,
                {"index.md": "# Home\n\nText.\n", "release-notes.md": "# Release notes\n\n## Old { #old }\n\nx\n"},
            )
        )
        assert [section["id"] for section in corpus["sections"]] == ["index"]

    def test_explains_holds_each_code_and_each_rule_row_with_its_part(self, corpus: dict[str, Any]) -> None:
        assert corpus["explains"] == {
            "rule:swc.one": {"section": "reference/rules#checks-on-a-single-element", "part": 1},
            "rule:swc.two": {"section": "reference/rules#checks-on-a-single-element", "part": 1},
            "stale-baseline": {"section": "how-to/refusals#stale-baseline", "part": 1},
            "unknown-element": {"section": "how-to/refusals#unknown-element", "part": 1},
            "unknown-layer": {"section": "how-to/refusals#unknown-element", "part": 1},
        }

    def test_the_corpus_carries_its_schema_and_the_running_version(self, corpus: dict[str, Any]) -> None:
        assert corpus["corpus"] == "swcraft-guide"
        assert corpus["schema"] == 1
        assert corpus["swcraft_version"] == __version__
        assert set(corpus) == {"corpus", "schema", "swcraft_version", "pages", "sections", "explains"}
        assert set(corpus["sections"][0]) == {"id", "page", "breadcrumb", "level", "purpose", "parts", "own"}


# The split, measured on the composed reply


def _table(rows: int, *, rule_at: int | None = None) -> str:
    lines = ["| Check | What it requires |", "|---|---|"]
    for number in range(rows):
        if number == rule_at:
            lines.append("| `rule:late.row`{ #late.row } | The row a search must read at its part. |")
        else:
            lines.append(f"| check number {number:04d} | " + "a requirement that takes some room " * 3 + "|")
    return "\n".join(lines)


def _parts(corpus: dict[str, Any], section_id: str) -> list[str]:
    return _sections(corpus)[section_id]["parts"]


def _every_part_fits(corpus: dict[str, Any]) -> None:
    index = GuideIndex.of(corpus)
    for section_id, section in index.sections.items():
        for part in range(1, len(section["parts"]) + 1):
            assert measure(guide_search.compose_section(index, section_id, part)) <= GUIDE_REPLY_BUDGET_WIRE_BYTES


class TestTheSplit:
    def test_a_section_that_fits_is_one_part(self, corpus: dict[str, Any]) -> None:
        assert all(len(section["parts"]) == 1 for section in corpus["sections"])

    def test_blocks_are_packed_greedily_and_never_cut(self, tmp_path: Path) -> None:
        paragraphs = [f"Paragraph {n:03d} " + "with words that fill the part " * 6 for n in range(60)]
        corpus = _one_page(tmp_path, "# Long\n\n## Body\n\n" + "\n\n".join(paragraphs) + "\n")
        parts = _parts(corpus, "page#body")
        assert len(parts) > 1
        _every_part_fits(corpus)
        blocks = [block for part in parts for block in part.split("\n\n")]
        assert blocks == ["## Body", *paragraphs]
        # Greedy: the next part's first block would not have fitted the part before it.
        index = GuideIndex.of(corpus)
        first = guide_search.compose_section(index, "page#body", 1)
        first["text"] += "\n\n" + parts[1].split("\n\n")[0]
        assert measure(first) > GUIDE_REPLY_BUDGET_WIRE_BYTES

    def test_a_table_is_split_between_rows_with_its_header_repeated(self, tmp_path: Path) -> None:
        corpus = _one_page(tmp_path, "# Checks\n\n## All\n\n" + _table(300) + "\n")
        parts = _parts(corpus, "page#all")
        assert len(parts) > 1
        _every_part_fits(corpus)
        rows: list[str] = []
        for number, part in enumerate(parts):
            table = part.split("\n\n")[-1].split("\n")
            assert table[:2] == ["| Check | What it requires |", "|---|---|"], number
            rows += table[2:]
        assert rows == _table(300).split("\n")[2:]

    def test_a_rule_row_past_part_one_is_explained_at_its_part(self, tmp_path: Path) -> None:
        corpus = _one_page(tmp_path, "# Checks\n\n## All\n\n" + _table(300, rule_at=250) + "\n")
        entry = corpus["explains"]["rule:late.row"]
        assert entry["section"] == "page#all"
        assert entry["part"] > 1
        assert "`rule:late.row`{ #late.row }" in _parts(corpus, "page#all")[entry["part"] - 1]

    def test_a_fenced_block_is_split_between_lines_with_the_fence_reopened(self, tmp_path: Path) -> None:
        code = [f'    "line {n:04d}": "a value that takes some room in the block",' for n in range(400)]
        corpus = _one_page(tmp_path, "# Code\n\n## Sample\n\n```json\n" + "\n".join(code) + "\n```\n")
        parts = _parts(corpus, "page#sample")
        assert len(parts) > 1
        _every_part_fits(corpus)
        seen: list[str] = []
        for part in parts:
            fence = part.split("\n\n")[-1].split("\n")
            assert fence[0] == "```json"
            assert fence[-1] == "```"
            seen += fence[1:-1]
        assert seen == code

    def test_a_paragraph_is_split_at_a_sentence_end(self, tmp_path: Path) -> None:
        sentences = [f"Sentence {n:04d} says a thing that takes some room." for n in range(400)]
        corpus = _one_page(tmp_path, "# Prose\n\n## One paragraph\n\n" + " ".join(sentences) + "\n")
        parts = _parts(corpus, "page#one-paragraph")
        assert len(parts) > 1
        _every_part_fits(corpus)
        for part in parts:
            assert part.split("\n\n")[-1].endswith(".")
        assert " ".join(part.split("\n\n")[-1] for part in parts) == " ".join(sentences)

    def test_a_list_is_split_between_items(self, tmp_path: Path) -> None:
        items = [f"- item {n:04d} that takes some room in the list\n  and a continuation line" for n in range(300)]
        corpus = _one_page(tmp_path, "# List\n\n## Items\n\n" + "\n".join(items) + "\n")
        parts = _parts(corpus, "page#items")
        assert len(parts) > 1
        _every_part_fits(corpus)
        seen = "\n".join(part.split("\n\n")[-1] for part in parts)
        assert seen == "\n".join(items)

    def test_a_blank_line_before_an_indented_continuation_is_not_a_cut(self, tmp_path: Path) -> None:
        corpus = _one_page(tmp_path, "# P\n\n## S\n\n- an item\n\n  its second paragraph\n\nAfter the list.\n")
        (text,) = _parts(corpus, "page#s")
        assert text == "## S\n\n- an item\n\n  its second paragraph\n\nAfter the list."

    def test_an_own_body_is_cut_where_the_parts_are_and_stops_at_the_first_child(self, tmp_path: Path) -> None:
        paragraphs = [f"Paragraph {n:03d} " + "with words that fill the part " * 6 for n in range(60)]
        corpus = _one_page(
            tmp_path, "# Long\n\n## Body\n\n" + "\n\n".join(paragraphs) + "\n\n### Child\n\nChild text.\n"
        )
        record = _sections(corpus)["page#body"]
        parts, own = record["parts"], record["own"]
        assert len(parts) > 1 and len(own) == len(parts)
        assert own[0] == parts[0].removeprefix("## Body\n\n")
        assert own[1:-1] == parts[1:-1]
        assert own[-1] == parts[-1].split("\n\n### Child")[0]
        assert "\n\n".join(own) == "\n\n".join(paragraphs)
        # The page's own body is its H1's lead, empty here, in its first part alone.
        assert _sections(corpus)["page"]["own"] == [""]

    def test_the_page_id_and_its_sections_are_split_on_their_own(self, tmp_path: Path) -> None:
        corpus = _one_page(tmp_path, "# Checks\n\n## All\n\n" + _table(300) + "\n\n## After\n\nShort.\n")
        assert len(_parts(corpus, "page")) >= len(_parts(corpus, "page#all"))
        assert _parts(corpus, "page#after") == ["## After\n\nShort."]


# Every reply the corpus can produce, measured


class TestEveryReplyIsMeasured:
    def test_the_fixture_corpus_has_no_reply_over_the_budget(self, corpus: dict[str, Any]) -> None:
        assert check_every_reply(corpus) == []

    def test_the_worst_request_is_made_of_the_widest_escaping_character(self) -> None:
        widest = guide_corpus._widest(lambda query: measure({"query": query}))
        assert len(widest) == REQUEST_CAP_CHARACTERS
        # An ASCII control character escapes to six characters, and once more on the wire.
        assert set(widest) == {"\x01"}

    def test_an_over_budget_section_is_reported_by_name(self, corpus: dict[str, Any]) -> None:
        swollen = json.loads(json.dumps(corpus))
        _sections(swollen)["index#pages"]["parts"] = ['"' * 5_000]
        over = check_every_reply(swollen)
        assert [name for name, _ in over] == ["section `index#pages` part 1"]
        assert over[0][1] > GUIDE_REPLY_BUDGET_WIRE_BYTES

    def test_an_over_budget_hit_shows_in_the_worst_search(self, corpus: dict[str, Any]) -> None:
        swollen = json.loads(json.dumps(corpus))
        for section in swollen["sections"]:
            section["breadcrumb"] = [*section["breadcrumb"], "\x01" * 300]
        names = [name for name, _ in check_every_reply(swollen)]
        assert "the worst search" in names

    def test_a_toc_of_many_sections_is_paged_and_every_part_fits(self, tmp_path: Path) -> None:
        headings = "\n\n".join(f"## Section number {n:03d} with a title\n\nText." for n in range(250))
        corpus = _one_page(tmp_path, "# Many\n\n" + headings + "\n")
        assert check_every_reply(corpus) == []
        index = GuideIndex.of(corpus)
        parts = guide_search.toc_parts(index, "page")
        assert len(parts) > 1
        listed = [entry["id"] for part in parts for entry in part]
        assert listed == [section["id"] for section in corpus["sections"][1:]]
        first = guide_search.compose_toc(index, "page", 1)
        assert first["next"] == {"toc": True, "page": "page", "part": 2}
        assert guide_search.compose_toc(index, "page", len(parts))["next"] is None


# The composers


class TestTheComposers:
    def test_a_section_reply(self, index: GuideIndex) -> None:
        reply = guide_search.compose_section(index, "index#pages")
        assert reply == {
            "response": "guide",
            "mode": "section",
            "id": "index#pages",
            "page_title": "Start here",
            "breadcrumb": ["Start here", "Reading the guide", "Pages"],
            "purpose": "What a page is.",
            "link": guide_search.section_link("index#pages"),
            "part": 1,
            "parts": 1,
            "next": None,
            "text": index.sections["index#pages"]["parts"][0],
        }

    def test_a_section_reply_names_the_next_part(self, tmp_path: Path) -> None:
        index = GuideIndex.of(_one_page(tmp_path, "# Checks\n\n## All\n\n" + _table(300) + "\n"))
        assert guide_search.compose_section(index, "page#all", 1)["next"] == {"section": "page#all", "part": 2}

    def test_a_part_the_section_does_not_have_is_refused(self, index: GuideIndex) -> None:
        for part in (0, 2):
            with pytest.raises(GuideRefusal) as raised:
                guide_search.compose_section(index, "index#pages", part)
            assert raised.value.reply.startswith("ERROR: guide-unknown-part: `index#pages` has 1 part(s)")

    def test_an_unknown_id_is_left_to_the_reader(self, index: GuideIndex) -> None:
        with pytest.raises(KeyError):
            guide_search.compose_section(index, "index#nowhere")

    def test_a_search_reply_carries_the_ready_read_request(self, index: GuideIndex) -> None:
        reply = guide_search.compose_search(index, "pages", [("index#pages", 1), ("index", 1)], False)
        assert reply["response"] == "guide"
        assert reply["mode"] == "search"
        assert reply["query"] == "pages"
        assert reply["more"] is False
        assert "note" not in reply
        assert reply["hits"][0] == {
            "id": "index#pages",
            "page_title": "Start here",
            "breadcrumb": ["Start here", "Reading the guide", "Pages"],
            "purpose": "What a page is.",
            "link": guide_search.section_link("index#pages"),
            "parts": 1,
            "read": {"section": "index#pages", "part": 1},
        }

    def test_a_search_with_no_hit_says_so(self, index: GuideIndex) -> None:
        reply = guide_search.compose_search(index, "nothing", [], False)
        assert reply["hits"] == []
        assert reply["note"] == NO_MATCH_SENTENCE

    def test_a_search_reply_carries_at_most_five_hits(self, index: GuideIndex) -> None:
        with pytest.raises(ValueError):
            guide_search.compose_search(index, "x", [(section_id, 1) for section_id in list(index.sections)[:6]], True)

    def test_the_toc_lists_every_page(self, index: GuideIndex) -> None:
        reply = guide_search.compose_toc(index)
        assert reply["mode"] == "toc"
        assert "page" not in reply
        assert (reply["part"], reply["parts"], reply["next"]) == (1, 1, None)
        assert reply["pages"] == [
            {"page": "fenced", "title": "Fenced headings", "sections": 2},
            {"page": "how-to/refusals", "title": "Refusals", "sections": 2},
            {"page": "index", "title": "Start here", "sections": 4},
            {"page": "reference", "title": "Reference", "sections": 0},
            {"page": "reference/rules", "title": "The checks", "sections": 1},
        ]

    def test_the_toc_of_a_page_lists_its_sections(self, index: GuideIndex) -> None:
        reply = guide_search.compose_toc(index, "index")
        assert reply["page"] == "index"
        assert [entry["id"] for entry in reply["sections"]] == [
            "index#reading",
            "index#pages",
            "index#sections",
            "index#where-to-go-next",
        ]
        assert reply["sections"][1] == {
            "id": "index#pages",
            "title": "Pages",
            "purpose": "What a page is.",
            "level": 3,
            "parts": 1,
        }

    def test_the_toc_of_an_unknown_page_is_left_to_the_reader(self, index: GuideIndex) -> None:
        with pytest.raises(KeyError):
            guide_search.compose_toc(index, "nowhere")

    def test_a_toc_part_it_does_not_have_is_refused(self, index: GuideIndex) -> None:
        with pytest.raises(GuideRefusal) as raised:
            guide_search.compose_toc(index, None, 2)
        assert raised.value.reply.startswith("ERROR: guide-unknown-part: `toc` has 1 part(s)")


class TestTheRefusalTexts:
    def _every(self) -> dict[str, str]:
        return {
            "guide-missing": guide_search.missing_refusal(),
            "guide-version": guide_search.version_refusal("0.1.0", "0.2.0"),
            "guide-unreadable": guide_search.unreadable_refusal(),
            "guide-mode": guide_search.mode_refusal(),
            "guide-unknown-section": guide_search.unknown_section_refusal("index#x", ["index", "index#pages"]),
            "guide-unknown-part": guide_search.unknown_part_refusal("index", 3),
            "guide-request-too-long": guide_search.request_too_long_refusal(),
            "guide-over-budget": guide_search.over_budget_refusal("index"),
        }

    def test_every_closed_code_has_its_composer_and_the_error_shape(self) -> None:
        texts = self._every()
        assert tuple(texts) == REFUSAL_CODES
        for code, text in texts.items():
            assert text.startswith(f"ERROR: {code}: "), text
            assert "\n" not in text

    def test_no_refusal_names_a_location(self) -> None:
        for text in self._every().values():
            assert not guide_corpus.HOME_SPELLED.search(text)
            assert not guide_corpus.DRIVE_LETTER_PATH.search(text)
            assert not re.search(r"(?<![\w.~`-])/[A-Za-z]", text), text

    def test_the_version_refusal_names_both_versions(self) -> None:
        text = guide_search.version_refusal("0.1.0", "0.2.0")
        assert "0.1.0" in text
        assert "0.2.0" in text

    def test_the_unknown_section_refusal_echoes_the_id_and_the_nearest(self) -> None:
        text = guide_search.unknown_section_refusal("index#x", ["index", "index#pages"])
        assert text == (
            "ERROR: guide-unknown-section: the guide has no section `index#x`; the nearest are `index`, `index#pages`"
        )

    def test_the_too_long_refusal_echoes_nothing(self) -> None:
        assert guide_search.request_too_long_refusal() == guide_search.request_too_long_refusal()
        assert str(REQUEST_CAP_CHARACTERS) in guide_search.request_too_long_refusal()


# The producer's refusals


def _refused(tmp_path: Path, pages: dict[str, str], pattern: str) -> None:
    guide = _guide(tmp_path, pages)
    with pytest.raises(GuideCorpusError, match=pattern):
        build_corpus(guide)


class TestTheRefusals:
    def test_a_duplicate_explicit_anchor_is_refused(self, tmp_path: Path) -> None:
        page = "# P\n\n## One { #same }\n\nText.\n\n## Two { #same }\n\nText.\n"
        _refused(tmp_path, {"page.md": page}, r"the id `page#same` is also")

    def test_two_files_of_one_page_name_are_refused(self, tmp_path: Path) -> None:
        _refused(tmp_path, {"ref.md": "# A\n", "ref/index.md": "# B\n"}, r"the id `ref` is also")

    def test_a_page_with_no_h1_is_refused(self, tmp_path: Path) -> None:
        page = "## Only a section\n\nText.\n"
        _refused(tmp_path, {"page.md": page}, r"exactly one level-one heading, this one has 0")

    def test_a_page_with_two_h1s_is_refused(self, tmp_path: Path) -> None:
        _refused(tmp_path, {"page.md": "# One\n\n# Two\n"}, r"exactly one level-one heading, this one has 2")

    def test_a_section_before_the_h1_is_refused(self, tmp_path: Path) -> None:
        _refused(tmp_path, {"page.md": "## Early\n\n# Title\n"}, r"must come before every other heading")

    def test_a_heading_the_cut_cannot_see_is_refused(self, tmp_path: Path) -> None:
        _refused(tmp_path, {"page.md": "# Title\n\nSetext\n------\n\nText.\n"}, r"headings Markdown reads")

    @pytest.mark.parametrize(
        ("comment", "pattern"),
        [
            ("<!-- Purpose: Capitalised. -->", r"an authoring comment is one line"),
            ("<!-- purpose:  -->", r"an authoring comment is one line"),
            ("<!-- purpose: two\nlines -->", r"an authoring comment is one line"),
            ("<!-- purpose: " + "x" * (PURPOSE_CAP_CHARACTERS + 1) + " -->", r"at most 100 characters"),
            ("<!-- keywords: a, b -->", r"keywords stand under the page's level-one heading"),
            ("<!-- explains: a code -->", r"not a code"),
            ("<!-- purpose: one -->\n\n<!-- purpose: two -->", r"a heading has one purpose comment"),
        ],
    )
    def test_a_malformed_authoring_comment_under_a_section_is_refused(
        self, tmp_path: Path, comment: str, pattern: str
    ) -> None:
        _refused(tmp_path, {"page.md": f"# P\n\n## S\n\n{comment}\n\nText.\n"}, pattern)

    def test_an_authoring_comment_not_under_a_heading_is_refused(self, tmp_path: Path) -> None:
        page = "# P\n\n## S\n\nText first.\n\n<!-- purpose: Too late. -->\n"
        _refused(tmp_path, {"page.md": page}, r"must stand directly under a heading")

    def test_an_authoring_comment_sharing_its_block_is_refused(self, tmp_path: Path) -> None:
        page = "# P\n\n## S\n\n<!-- purpose: Glued. -->\nText.\n"
        _refused(tmp_path, {"page.md": page}, r"a block of its own")

    def test_explains_under_the_h1_is_refused(self, tmp_path: Path) -> None:
        _refused(tmp_path, {"page.md": "# P\n\n<!-- explains: code -->\n"}, r"explains stands under a `##` or `###`")

    def test_a_code_explained_twice_is_refused(self, tmp_path: Path) -> None:
        page = "# P\n\n## A\n\n<!-- explains: code -->\n\n## B\n\n<!-- explains: code -->\n"
        guide = _guide(tmp_path, {"page.md": page})
        with pytest.raises(GuideCorpusError, match=r"`code` is explained twice"):
            build_corpus(guide)

    def test_an_authoring_comment_inside_a_fence_is_text(self, tmp_path: Path) -> None:
        page = "# P\n\n## S\n\n```markdown\n<!-- purpose: Shown, not read. -->\n```\n"
        corpus = _one_page(tmp_path, page)
        assert _sections(corpus)["page#s"]["purpose"] == ""
        assert "<!-- purpose: Shown, not read. -->" in _parts(corpus, "page#s")[0]

    def test_a_single_line_that_cannot_fit_is_refused(self, tmp_path: Path) -> None:
        page = "# P\n\n## S\n\n" + "word " * 3_000 + "\n"
        _refused(tmp_path, {"page.md": page}, r"cannot fit one reply and cannot be split")

    def test_a_table_row_that_cannot_fit_is_refused(self, tmp_path: Path) -> None:
        rows = ["| a | b |", "|---|---|", "| " + "x" * 9_000 + " | y |", "| c | d |"]
        _refused(tmp_path, {"page.md": "# P\n\n## S\n\n" + "\n".join(rows) + "\n"}, r"a single row, line or sentence")

    def test_a_guide_with_no_page_is_refused(self, tmp_path: Path) -> None:
        guide = _guide(tmp_path, {})
        guide.mkdir()
        with pytest.raises(GuideCorpusError, match=r"no guide page"):
            build_corpus(guide)

    def test_a_configuration_mkdocs_cannot_read_is_refused(self, tmp_path: Path) -> None:
        # The fixture's configuration names `docs_dir: guide`, which is absent here.
        shutil.copyfile(FIXTURE / "mkdocs.yml", tmp_path / "mkdocs.yml")
        with pytest.raises(GuideCorpusError, match=r"MkDocs cannot read this configuration"):
            guide_corpus.effective_markdown_config(tmp_path / "mkdocs.yml")

    def test_a_guide_with_no_configuration_is_refused(self, tmp_path: Path) -> None:
        guide = tmp_path / "guide"
        guide.mkdir()
        with pytest.raises(GuideCorpusError, match=r"no mkdocs.yml"):
            guide_corpus.find_mkdocs_config(guide)


class TestTheBuildHostScan:
    @pytest.mark.parametrize(
        "line",
        [
            "Run `~/.swcraft/current/bin/swcraft`.",
            "Open `C:/work/model.yaml`.",
            "Open `D:\\work\\model.yaml`.",
        ],
    )
    def test_a_home_or_drive_spelling_is_refused_and_nothing_is_written(self, tmp_path: Path, line: str) -> None:
        guide = _guide(tmp_path, {"page.md": f"# P\n\n## S\n\n{line}\n"})
        out = tmp_path / "corpus.json"
        with pytest.raises(GuideCorpusError, match=r"the guide names a location"):
            produce(guide, out)
        assert not out.exists()

    def test_a_location_of_the_build_host_is_refused(self, tmp_path: Path) -> None:
        site = tmp_path / "site"
        site.mkdir()
        guide = _guide(tmp_path, {"page.md": f"# P\n\nThe site is built in {site.as_posix()}/index.html.\n"})
        with pytest.raises(GuideCorpusError, match=r"the guide names a location"):
            produce(guide, site / "corpus.json")

    def test_dollar_home_and_placeholders_pass(self, tmp_path: Path) -> None:
        page = "# P\n\nRun `$HOME/.swcraft/current/bin/swcraft`; it prints `<your home>/.swcraft`.\n"
        out = tmp_path / "corpus.json"
        produce(_guide(tmp_path, {"page.md": page}), out)
        assert out.is_file()

    def test_the_locations_skip_a_root_of_one_part(self) -> None:
        assert "/" not in guide_corpus.host_locations(Path("/"))

    @pytest.mark.parametrize(
        ("line", "token"),
        [
            # A character class with the slash before the backslash, as JSON escapes it.
            ('The class is `"[^@:/\\\\]"`.', "/\\\\"),
            ("The class is `[^@:/\\]`.", "/\\"),
            ("Two slashes: `a //` in a row.", "//"),
            # The bare URL schemes: with no authority, what follows the colon is a root spelling.
            ("A bare `file://` link.", "//"),
            ("A bare `file:///` link.", "///"),
            ("A bare `vscode://` link.", "//"),
            ("The root, /.", "/."),
        ],
    )
    def test_a_root_spelling_is_refused_on_every_host(self, tmp_path: Path, line: str, token: str) -> None:
        """A run of separators reads as a root on some host, so it is refused on all of them."""
        assert guide_corpus.root_spellings(line) == [token]
        guide = _guide(tmp_path, {"page.md": f"# P\n\n## S\n\n{line}\n"})
        out = tmp_path / "corpus.json"
        with pytest.raises(GuideCorpusError, match=r"the guide names a location: .*names '"):
            produce(guide, out)
        assert not out.exists()

    @pytest.mark.parametrize(
        "line",
        [
            "Ordinary text, with a slash in a/b and an AUTOSAR reference /Pkg/Swc.",
            "Open vscode://swcraft-local.swcraft-vscode/guide?section=index in the editor.",
            "The site is https://example.org/guide/ and its `file:` link.",
            "The class is `[^@:\\\\/]`, the slash written last.",
            "A slash alone / between words, and one at the end /",
        ],
    )
    def test_text_that_is_no_root_spelling_passes(self, tmp_path: Path, line: str) -> None:
        assert guide_corpus.root_spellings(line) == []
        out = tmp_path / "corpus.json"
        produce(_guide(tmp_path, {"page.md": f"# P\n\n## S\n\n{line}\n"}), out)
        assert out.is_file()


# The real guide, with the release notes the build carries written beside it

#: ``test_guide_surface.py``'s record of the hand-written pages' headings, read here for the page names.
ANCHOR_RECORD = Path(__file__).parent / "fixtures" / "guide_corpus" / "hand-written-anchors.json"

#: The protocol schema whose ``errorCode`` enum ``explains`` must cover, and the page that does.
PROTOCOL_SCHEMA = (
    Path("packages") / "tools" / "tests" / "fixtures" / "contracts" / "editing-protocol" / "protocol.schema.json"
)
REFUSALS_PAGE = "how-to/refusals"


@pytest.fixture(scope="module")
def real(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, Any], Path]:
    """The real guide's corpus, produced in a temporary repository as the docs build produces it.

    The build script writes its selected notes into the copy, so the producer meets the section as
    the docs build hands it over. The repository's own ``mkdocs.yml`` is named: the copy has none.
    """
    root = tmp_path_factory.mktemp("real-guide")
    shutil.copytree(
        REPO_ROOT / "docs" / "guide",
        root / "docs" / "guide",
        ignore=lambda directory, names: [
            name
            for name in names
            if Path(directory).resolve() == (REPO_ROOT / "docs" / "guide").resolve()
            and name in ("release-notes", "version.md")
        ],
    )
    shutil.copytree(REPO_ROOT / "docs" / "release-notes", root / "docs" / "release-notes")
    build_artifacts_module().write_guide_release_notes(root)
    site = root / "site"
    site.mkdir()
    out = site / guide_corpus.CORPUS_FILENAME
    return produce(root / "docs" / "guide", out, mkdocs_config=REPO_ROOT / "mkdocs.yml"), out


def _hand_written() -> set[str]:
    return {name[: -len(".md")] for name in json.loads(ANCHOR_RECORD.read_text(encoding="utf-8"))["pages"]}


class TestTheRealGuide:
    def test_it_is_produced_and_written(self, real: tuple[dict[str, Any], Path]) -> None:
        corpus, out = real
        assert json.loads(out.read_text(encoding="utf-8")) == corpus

    def test_every_page_has_one_h1_and_every_id_is_unique(self, real: tuple[dict[str, Any], Path]) -> None:
        corpus, _ = real
        pages = [page["page"] for page in corpus["pages"]]
        page_ids = [section["page"] for section in corpus["sections"] if section["level"] == 1]
        assert sorted(page_ids) == sorted(pages)
        ids = [section["id"] for section in corpus["sections"]]
        assert len(ids) == len(set(ids))

    def test_it_leaves_out_the_release_notes_the_build_writes_beside_it(
        self, real: tuple[dict[str, Any], Path]
    ) -> None:
        corpus, out = real
        written = list((out.parent.parent / "docs" / "guide" / "release-notes").glob("*.md"))
        assert len(written) == len(build_artifacts_module().guide_release_notes(REPO_ROOT)) + 1
        assert not [page["page"] for page in corpus["pages"] if guide_corpus.is_release_notes(page["page"] + ".md")]

    def test_every_hand_written_heading_has_an_anchor_id_and_a_purpose(self, real: tuple[dict[str, Any], Path]) -> None:
        corpus, _ = real
        record = json.loads(ANCHOR_RECORD.read_text(encoding="utf-8"))["pages"]
        hand_written = [section for section in corpus["sections"] if section["page"] in _hand_written()]
        assert {section["page"] for section in hand_written} == _hand_written()
        without = [section["id"] for section in hand_written if not section["purpose"]]
        assert without == []
        ids = {section["id"] for section in hand_written}
        for name, headings in record.items():
            page = name[: -len(".md")]
            for heading in headings:
                level, anchor = heading.split(" ")
                if len(level) in (2, 3):
                    assert f"{page}#{anchor}" in ids, f"{page}#{anchor}"

    def test_every_section_carries_an_authored_purpose(self, real: tuple[dict[str, Any], Path]) -> None:
        """The generated pages' purposes are their generators', the hand-written pages' their authors'."""
        corpus, _ = real
        without = [section["id"] for section in corpus["sections"] if not section["purpose"]]
        assert without == []

    def test_no_reply_is_over_the_budget(self, real: tuple[dict[str, Any], Path]) -> None:
        """:func:`produce` refuses otherwise; asserted again so a weakened producer is seen here."""
        corpus, _ = real
        assert check_every_reply(corpus) == []

    def test_the_build_host_scan_finds_nothing(self, real: tuple[dict[str, Any], Path]) -> None:
        corpus, out = real
        locations = guide_corpus.host_locations(out.parent.parent / "docs" / "guide", REPO_ROOT, out.parent)
        assert guide_corpus.scan_for_locations(corpus, locations) == []

    def test_explains_holds_every_protocol_code_once_on_the_refusals_page(
        self, real: tuple[dict[str, Any], Path]
    ) -> None:
        """Each ``errorCode`` of the protocol schema has exactly one ``explains`` section, on the *Refusals* page.

        The producer refuses a code explained twice; this holds that none is missing and nothing else is entered.
        """
        corpus, _ = real
        schema = json.loads((REPO_ROOT / PROTOCOL_SCHEMA).read_text(encoding="utf-8"))
        codes = schema["$defs"]["errorCode"]["enum"]
        assert len(codes) == 11
        explained = {key: entry for key, entry in corpus["explains"].items() if not key.startswith("rule:")}
        assert sorted(explained) == sorted(codes)
        for code, entry in explained.items():
            assert entry == {"section": f"{REFUSALS_PAGE}#{code}", "part": 1}, code


# The entry sections name their identifiers in prose

_HOW_TO = "# How-to\n\n- [Do it](do-it.md)\n"
_STEP = "*On the command line:*\n\n```sh\nswcraft check-model --project swcraft.yaml\n```\n"


class TestTheEntrySectionsNameTheirIdentifiers:
    """An entry section's code-block identifiers must stand in its prose too; planted both ways."""

    def test_the_narrow_patterns(self) -> None:
        code = "\n".join(
            [
                "swcraft generate needs --project swcraft.yaml --dry-run",
                '{"env": {"SWCRAFT_REVIEW": "panel"}, "satisfies": ["REQ_EXAMPLE_001@1"]}',
                'echo "$SWCRAFT_HOME" ${OTHER_VAR} $env:WIN_VAR %LOCAL_VAR%',
                "outputs.needs = swcraft.yaml model.arxml",
                "cp dist/needs.json ../reqs/build/needs.json .codex/hooks.json",
                "$HOME/.swcraft/current/bin/swcraft list",
            ]
        )
        assert guide_corpus.code_identifiers(code) == {
            "swcraft generate",
            "--project",
            "--dry-run",
            "SWCRAFT_REVIEW",
            "SWCRAFT_HOME",
            "OTHER_VAR",
            "WIN_VAR",
            "LOCAL_VAR",
            "outputs.needs",
            "dist/needs.json",
            ".codex/hooks.json",
        }

    def test_a_how_to_step_naming_its_command_only_in_code_is_refused(self, tmp_path: Path) -> None:
        guide = _guide(tmp_path, {"how-to/index.md": _HOW_TO, "how-to/do-it.md": "# Do it\n\n" + _STEP})
        corpus = build_corpus(guide)
        assert guide_corpus.entry_sections(corpus)["how-to/do-it"] == ["how-to link", "index"]
        assert guide_corpus.identifier_violations(corpus) == [("how-to/do-it", "swcraft check-model")]
        out = tmp_path / "corpus.json"
        with pytest.raises(GuideCorpusError, match=r"`how-to/do-it` names 'swcraft check-model' in a code block only"):
            produce(guide, out)
        assert not out.exists()

    def test_the_same_step_naming_it_in_prose_passes(self, tmp_path: Path) -> None:
        step = _STEP.replace("*On the command line:*", "*On the command line:* `swcraft check-model`:")
        guide = _guide(tmp_path, {"how-to/index.md": _HOW_TO, "how-to/do-it.md": "# Do it\n\n" + step})
        assert guide_corpus.identifier_violations(build_corpus(guide)) == []

    def test_a_section_no_pointer_names_is_not_held(self, tmp_path: Path) -> None:
        guide = _guide(tmp_path, {"page.md": "# Page\n\n## Step { #step }\n\n" + _STEP})
        corpus = build_corpus(guide)
        assert guide_corpus.entry_sections(corpus) == {}
        assert guide_corpus.identifier_violations(corpus) == []

    def test_a_section_a_how_to_page_links_to_is_held(self, tmp_path: Path) -> None:
        pages = {
            "how-to/index.md": "# How-to\n\nSee [the step](../cli.md#step).\n",
            "cli.md": "# CLI\n\n## Step { #step }\n\n```sh\nswcraft list --fit-bytes 1\n```\n",
        }
        corpus = build_corpus(_guide(tmp_path, pages))
        assert guide_corpus.entry_sections(corpus) == {"cli#step": ["how-to link"]}
        assert guide_corpus.identifier_violations(corpus) == [("cli#step", "--fit-bytes"), ("cli#step", "swcraft list")]

    def test_a_section_a_tool_points_to_and_one_explains_names_are_held(self, tmp_path: Path) -> None:
        pages = {
            "cli.md": "# CLI\n\n## Generating { #generating }\n\n```sh\nexport SWCRAFT_REVIEW=panel\n```\n",
            "codes.md": (
                "# Codes\n\n## Stale { #stale }\n\n<!-- explains: stale-baseline -->\n\n"
                '```json\n{"field": "outputs.needs"}\n```\n'
            ),
        }
        assert "cli#generating" in guide_corpus.TOOL_POINTER_SECTIONS
        corpus = build_corpus(_guide(tmp_path, pages))
        assert guide_corpus.entry_sections(corpus) == {"cli#generating": ["tool"], "codes#stale": ["explains"]}
        assert guide_corpus.identifier_violations(corpus) == [
            ("cli#generating", "SWCRAFT_REVIEW"),
            ("codes#stale", "outputs.needs"),
        ]

    def test_the_prose_must_hold_the_whole_spelling(self, tmp_path: Path) -> None:
        step = "`--fit-bytes-more` and `swcraft list`.\n\n```sh\nswcraft list --fit-bytes 1\n```\n"
        guide = _guide(tmp_path, {"how-to/index.md": _HOW_TO, "how-to/do-it.md": "# Do it\n\n" + step})
        assert guide_corpus.identifier_violations(build_corpus(guide)) == [("how-to/do-it", "--fit-bytes")]

    def test_every_allowance_states_why(self) -> None:
        assert guide_corpus.IDENTIFIER_ALLOWANCES
        assert all(len(reason.split()) >= 5 for reason in guide_corpus.IDENTIFIER_ALLOWANCES.values())

    def test_the_real_guide_s_entry_sections_name_their_identifiers(self, real: tuple[dict[str, Any], Path]) -> None:
        """:func:`produce` refuses otherwise; asserted again so a weakened producer is seen here."""
        corpus, _ = real
        entries = guide_corpus.entry_sections(corpus)
        assert set(guide_corpus.TOOL_POINTER_SECTIONS) <= set(entries)
        assert set(corpus["explains"][key]["section"] for key in corpus["explains"]) <= set(entries)
        assert guide_corpus.identifier_violations(corpus) == []


class TestWithoutMarkdown:
    @pytest.mark.parametrize("module", ["markdown", "mkdocs.config"])
    def test_a_missing_development_dependency_is_refused_in_one_sentence(
        self, monkeypatch: pytest.MonkeyPatch, module: str
    ) -> None:
        """Order-independent: every loaded ``markdown`` and ``mkdocs.config`` module is dropped for the duration.

        ``mkdocs.config.defaults`` imports ``markdown``, so a cached import from an earlier test hides a blocked one.
        """
        for loaded in [
            name for name in sys.modules if name.split(".")[0] == "markdown" or name.startswith("mkdocs.config")
        ]:
            monkeypatch.delitem(sys.modules, loaded, raising=False)
        monkeypatch.setitem(sys.modules, module, None)
        with pytest.raises(GuideCorpusError) as raised:
            build_corpus(FIXTURE_GUIDE)
        assert str(raised.value) == guide_corpus.MARKDOWN_ABSENT

    def test_importing_the_module_imports_neither(self) -> None:
        probe = "import sys, swcraft_tools.guide_corpus; print(sorted({'markdown', 'mkdocs'} & set(sys.modules)))"
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, encoding="utf-8", check=True
        )
        assert result.stdout.strip() == "[]"


# The document and the module form


class TestTheDocument:
    def test_it_is_written_sorted_utf8_and_reproducible(self, tmp_path: Path) -> None:
        first, second = tmp_path / "one.json", tmp_path / "two.json"
        produce(FIXTURE_GUIDE, first)
        produce(FIXTURE_GUIDE, second)
        data = first.read_bytes()
        assert data == second.read_bytes()
        assert data.endswith(b"\n")
        assert b"\r" not in data
        text = data.decode("utf-8")
        assert "\u2026" in text  # non-ASCII as it is
        loaded = json.loads(text)
        assert text == json.dumps(loaded, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        assert loaded == build_corpus(FIXTURE_GUIDE)

    def test_the_module_form_writes_the_corpus(self, tmp_path: Path) -> None:
        out = tmp_path / guide_corpus.CORPUS_FILENAME
        result = subprocess.run(
            [sys.executable, "-m", "swcraft_tools.guide_corpus", "--guide-dir", str(FIXTURE_GUIDE), "--out", str(out)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "guide-corpus: 5 pages, 14 ids"
        assert json.loads(out.read_text(encoding="utf-8"))["corpus"] == "swcraft-guide"

    def test_the_module_form_refuses_with_exit_1_and_writes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        guide = _guide(tmp_path, {"page.md": "# One\n\n# Two\n"})
        out = tmp_path / "corpus.json"
        assert guide_corpus.main(["--guide-dir", str(guide), "--out", str(out)]) == 1
        assert capsys.readouterr().err.startswith("guide-corpus: page.md: a page has exactly one level-one heading")
        assert not out.exists()

    def test_an_output_directory_that_does_not_exist_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(GuideCorpusError, match=r"does not exist"):
            produce(FIXTURE_GUIDE, tmp_path / "absent" / "corpus.json")
