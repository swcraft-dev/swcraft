"""The release notes stay out of the agent's corpus, and no guide page leaves a step to them.

A release note tells how an earlier release worked, and an agent acting on it would follow
obsolete instructions; what a project must do to upgrade is the *Upgrading* page's. Two halves:

* **(a) The corpus.** The real guide, built in-process as the docs build hands it to the producer
  (the selected notes in its *Release notes* section): no page, section or ``explains`` entry
  comes from that section, and in none of the three modes is a note reachable.
* **(b) The pages.** Every committed hand-written guide page, read paragraph by paragraph outside
  fenced blocks: no sentence matches :data:`DEFERRAL_PATTERNS`. A link to a note that only
  points at history passes.

**What this does not reach.** A deferral worded outside the patterns (*the 0.6.0 note has
everything you need*, a note named without a link or an action word, a reference-style link or
an absolute URL); a deferral inside a fenced block, a comment or the generated pages; whether the
*Upgrading* page holds every step a note asks for, correctly -- that is the page's audit, not a
pattern's; and the built site's own ``guide-corpus.json`` -- this checks the producer that
writes it.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT, build_artifacts_module
from swcraft_tools import guide_corpus, guide_search
from swcraft_tools.guide_corpus import build_corpus
from swcraft_tools.guide_search import GuideIndex, GuideRefusal, SearchIndex, rank
from test_guide_surface import GENERATED_PREFIX, guide_pages

PAIRS = Path(__file__).parent / "fixtures" / "guide_retrieval" / "pairs.json"

#: The action a deferral asks the reader to take from a note.
_ACTION = r"(?:steps?|step-by-step|recipes?|checklists?|what to do|instructions?|the fix)"

#: A Markdown link into the *Release notes* section.
_NOTE_LINK = r"\]\([^)]*release-notes/[^)]*\)"

#: The sentences that leave a required step to a release note, each with its name.
DEFERRAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "a sentence linking a note asks for an action",
        re.compile(rf"(?:{_NOTE_LINK}.*\b{_ACTION}\b|\b{_ACTION}\b.*{_NOTE_LINK})", re.IGNORECASE),
    ),
    (
        "read the notes of the releases skipped",
        re.compile(r"\b(?:ones|notes?|releases?) (?:that )?you (?:are )?(?:skip|skipp(?:ed|ing))\b", re.IGNORECASE),
    ),
    (
        "see or read a note for an action",
        re.compile(
            rf"\b(?:see|read|re-read|consult|check|follow)\b.{{0,40}}\bnotes?\b.{{0,60}}\b{_ACTION}\b",
            re.IGNORECASE,
        ),
    ),
    (
        "a note named as the place of the steps",
        re.compile(
            rf"\bnotes?\b.{{0,30}}\b(?:is|are|carries|carry|gives|give|has|have|holds|hold)\b"
            rf".{{0,20}}\b{_ACTION}\b",
            re.IGNORECASE,
        ),
    ),
    ("a note's own section as the place of the fix", re.compile(r"\brelease note's own section\b", re.IGNORECASE)),
    ("follow a linked note", re.compile(r"\bfollows? \[[^\]]*\bnote\b[^\]]*\]", re.IGNORECASE)),
)

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z*\[`(])")


def hand_written_pages() -> dict[str, Path]:
    """The committed guide pages a person writes: everything but ``reference/`` and the notes."""
    return {name: path for name, path in guide_pages().items() if not name.startswith(GENERATED_PREFIX)}


def _paragraphs(text: str) -> list[str]:
    """*text*'s paragraphs outside fenced blocks and comments, each on one line."""
    paragraphs: list[str] = []
    current: list[str] = []
    fenced = False
    for line in text.split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced or line.strip().startswith("<!--"):
            continue
        if line.strip():
            current.append(line.strip())
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def deferrals(text: str) -> list[tuple[str, str]]:
    """Each ``(pattern name, sentence)`` of *text* that leaves a step to a release note."""
    found: list[tuple[str, str]] = []
    for paragraph in _paragraphs(text):
        for sentence in _SENTENCE_END.split(paragraph):
            found += [(name, sentence) for name, pattern in DEFERRAL_PATTERNS if pattern.search(sentence)]
    return found


# (b) No hand-written page defers a step to a note


class TestNoPageDefersAStepToANote:
    def test_the_pages_are_the_narrative_and_the_how_to_pages(self) -> None:
        pages = hand_written_pages()
        assert "upgrading.md" in pages and "how-to/index.md" in pages
        assert not [name for name in pages if name.startswith(("reference/", "release-notes/"))]

    @pytest.mark.parametrize("relative", sorted(hand_written_pages()))
    def test_no_sentence_leaves_a_step_to_a_note(self, relative: str) -> None:
        found = deferrals(hand_written_pages()[relative].read_text(encoding="utf-8"))
        assert found == [], f"docs/guide/{relative} leaves a step to a release note: {found}"

    @pytest.mark.parametrize(
        ("planted", "pattern"),
        [
            (
                "A project written before it needs three files, and the note is the step-by-step — "
                "[what changed, and what to do](release-notes/0.6.0.md).",
                "a sentence linking a note asks for an action",
            ),
            (
                "[Its note](release-notes/0.9.0.md) carries the recipe and the checklist.",
                "a sentence linking a note asks for an action",
            ),
            (
                "Read the release note for the version you are moving to, and the ones you are skipping over.",
                "read the notes of the releases skipped",
            ),
            ("Read every note you skipped before you start.", "read the notes of the releases skipped"),
            ("See the 0.14.0 note for the steps.", "see or read a note for an action"),
            ("The 0.14.0 note has the recipe.", "a note named as the place of the steps"),
            ("Re-read the release note's own section on it.", "a note's own section as the place of the fix"),
            ("A project on 0.20.0 follows [0.21.0's note](release-notes/0.21.0.md) first.", "follow a linked note"),
        ],
    )
    def test_each_pattern_fires_on_a_planted_sentence(self, planted: str, pattern: str) -> None:
        page = f"# A page\n\nA sentence before it. {planted} A sentence after it.\n"
        assert pattern in [name for name, _ in deferrals(page)]

    @pytest.mark.parametrize(
        "history",
        [
            "*History:* [the 0.6.0 release note](release-notes/0.6.0.md).",
            "| read the history of a release | [The release notes](release-notes/index.md) |",
            "A note is the history of one release: what it added, what it did not change.",
            "Every step a release asks of a project is on this page, never in a note.",
        ],
    )
    def test_a_history_link_and_a_statement_about_notes_pass(self, history: str) -> None:
        assert deferrals(f"# A page\n\n{history}\n") == []

    def test_a_deferral_in_a_fenced_block_or_a_comment_is_not_read(self) -> None:
        page = "# A page\n\n```text\nSee the 0.14.0 note for the steps.\n```\n\n<!-- See the note for the steps. -->\n"
        assert deferrals(page) == []


# (a) No section of the agent's corpus is a note


@pytest.fixture(scope="module")
def real(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, Any], Path]:
    """The real guide's corpus, built as the docs build hands the guide over: the selected notes beside it."""
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
    notes_dir = root / "docs" / "guide" / guide_corpus.RELEASE_NOTES_SUBTREE
    corpus = build_corpus(root / "docs" / "guide", mkdocs_config=REPO_ROOT / "mkdocs.yml")
    return corpus, notes_dir


def _note_ids(notes_dir: Path) -> list[str]:
    """The ids the notes would have had: each note's page name, and the section index's."""
    return sorted(
        guide_corpus.RELEASE_NOTES_SUBTREE
        if path.stem == "index"
        else f"{guide_corpus.RELEASE_NOTES_SUBTREE}/{path.stem}"
        for path in notes_dir.glob("*.md")
    )


def _from_notes(identifier: str) -> bool:
    return guide_corpus.is_release_notes(identifier.split("#", 1)[0] + ".md")


class TestTheCorpusHoldsNoNote:
    def test_the_build_wrote_the_notes_beside_the_guide(self, real: tuple[dict[str, Any], Path]) -> None:
        """The exclusion is exercised, not vacuous: the section is there for the producer to leave out."""
        _, notes_dir = real
        assert len(_note_ids(notes_dir)) == len(build_artifacts_module().guide_release_notes(REPO_ROOT)) + 1

    def test_no_page_section_or_explanation_comes_from_the_notes(self, real: tuple[dict[str, Any], Path]) -> None:
        corpus, _ = real
        assert not [page["page"] for page in corpus["pages"] if _from_notes(page["page"])]
        assert not [section["id"] for section in corpus["sections"] if _from_notes(section["id"])]
        assert not [key for key, entry in corpus["explains"].items() if _from_notes(entry["section"])]

    def test_a_notes_id_is_unknown_to_section(self, real: tuple[dict[str, Any], Path]) -> None:
        corpus, notes_dir = real
        index = SearchIndex.of(GuideIndex.of(corpus))
        for note_id in _note_ids(notes_dir):
            with pytest.raises(GuideRefusal) as raised:
                guide_search.section(index, note_id)
            assert raised.value.reply.startswith("ERROR: guide-unknown-section: "), note_id

    def test_the_toc_lists_no_note(self, real: tuple[dict[str, Any], Path]) -> None:
        corpus, _ = real
        index = SearchIndex.of(GuideIndex.of(corpus))
        listed = [entry["page"] for part in guide_search.toc_parts(index.guide, None) for entry in part]
        assert listed and not [page for page in listed if _from_notes(page)]
        with pytest.raises(GuideRefusal):
            guide_search.toc(index, guide_corpus.RELEASE_NOTES_SUBTREE)

    def test_no_search_hit_is_a_note(self, real: tuple[dict[str, Any], Path]) -> None:
        """The retrieval pairs' queries, and one made of each note's own title, find no note."""
        corpus, notes_dir = real
        index = SearchIndex.of(GuideIndex.of(corpus))
        queries = [pair["query"] for pair in json.loads(PAIRS.read_text(encoding="utf-8"))["pairs"]]
        queries += [path.read_text(encoding="utf-8").split("\n", 1)[0].lstrip("# ") for path in notes_dir.glob("*.md")]
        hits = {section_id for query in queries for section_id, _ in rank(index, query)}
        assert hits and not [section_id for section_id in hits if _from_notes(section_id)]
