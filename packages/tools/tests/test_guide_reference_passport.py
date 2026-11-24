"""The guide's model passport page: the shipped schema, byte for byte, inside one fence.

The oracle reads the package file independently and compares it with the fenced text of the
*committed* page, not only of a fresh render.

Not reached: that the schema is valid JSON Schema or that the pass meets it
(``test_passport_schema.py``, ``test_passport.py``); the rendered site; the byte comparison of
the committed page with the generator (the drift gate); whether the preamble reads well -- its
four points are held only by the phrases each must carry.
"""

from __future__ import annotations

import re
from importlib.resources import files as resource_files

import pytest
import yaml
from guide_fixtures import GUIDE_DIR, MKDOCS_YML, nav_paths
from swcraft_tools import passport as passport_pass
from swcraft_tools.guide_reference import INDEX_PATH, all_artifacts, passport
from swcraft_tools.guide_reference.catalogue import PAGE_SUMMARIES

_OPEN = "```json\n"
_CLOSE = "```\n"


def _shipped_schema() -> str:
    """The package file's text, read here rather than through the module under test."""
    resource = resource_files("swcraft_tools") / passport_pass.SCHEMA_RESOURCE
    return resource.read_text(encoding="utf-8")


def _fenced(page: str) -> str:
    """The text between the page's one ``json`` fence and its closing fence, the newline before it kept."""
    assert page.count(_OPEN) == 1, "the page carries not exactly one json fence"
    start = page.index(_OPEN) + len(_OPEN)
    assert page.endswith(_CLOSE), "the fence is not the page's last block"
    return page[start : len(page) - len(_CLOSE)]


def _preamble(page: str) -> str:
    """The page's prose: everything before the fence."""
    return page[: page.index(_OPEN)]


def _committed_page() -> str:
    return (GUIDE_DIR / passport.PASSPORT_PATH).read_text(encoding="utf-8")


class TestTheSchemaIsCarriedVerbatim:
    def test_the_committed_page_carries_the_shipped_schema_byte_for_byte(self) -> None:
        assert _fenced(_committed_page()) == _shipped_schema()

    def test_a_fresh_render_carries_it_too(self) -> None:
        assert _fenced(passport.passport_page()) == _shipped_schema()

    def test_the_text_is_not_reformatted(self) -> None:
        """No JSON round trip: spacing a re-serialization would normalize reaches the page as written."""
        text = '{ "a" :1,\n\t"b":[ 2 ]}\n'
        assert _fenced(passport.passport_page(text=text)) == text

    def test_a_text_with_no_final_newline_gets_one_and_nothing_else(self) -> None:
        assert _fenced(passport.passport_page(text="{}")) == "{}\n"

    def test_a_text_that_would_close_the_fence_is_refused(self) -> None:
        with pytest.raises(passport.UnfenceableSchema):
            passport.passport_page(text='{"description": "```"}\n')


class TestThePreamble:
    """The four points the page owes its reader, each held by the words that carry it."""

    def test_it_says_what_a_passport_is_and_holds(self) -> None:
        prose = _preamble(_committed_page())
        assert "counts, distributions and lengths" in prose
        assert "no name, no value and no text" in prose

    def test_it_says_a_passport_is_sent_instead_of_the_model(self) -> None:
        prose = _preamble(_committed_page())
        assert "cannot leave your organisation" in prose
        assert "send the passport instead" in prose

    def test_it_names_the_command_and_what_the_file_does_not_carry(self) -> None:
        prose = _preamble(_committed_page())
        assert "`swcraft passport --project swcraft.yaml --output passport.json`" in prose
        assert "no path" in prose

    def test_it_promises_no_reproduction_it_cannot_make(self) -> None:
        """The page says generating from a passport meets only some passports, and promises nothing more."""
        prose = _preamble(_committed_page())
        assert "is not possible for every passport yet" in prose
        assert "refused by name, never approximated" in prose
        assert "delivered with the workstation archive" in prose and "not shipped" not in prose
        assert "regenerated from it" not in prose and "is reproduced on" not in prose

    def test_it_names_no_phase_number(self) -> None:
        assert not re.search(r"\bphase\s+\d", _committed_page(), re.IGNORECASE)


class TestThePageIsInTheSection:
    def test_the_heading_is_the_index_title(self) -> None:
        title, _summary = PAGE_SUMMARIES[passport.PASSPORT_PATH]
        assert f"\n# {title}\n" in _committed_page()

    def test_the_index_carries_the_summary_row(self) -> None:
        title, summary = PAGE_SUMMARIES[passport.PASSPORT_PATH]
        index = (GUIDE_DIR / INDEX_PATH).read_text(encoding="utf-8")
        assert f"| [{title}](model-passport.md) | {summary} |" in index

    def test_the_drift_gate_population_includes_the_page(self) -> None:
        assert passport.PASSPORT_PATH in all_artifacts()

    def test_the_navigation_names_the_page(self) -> None:
        config = yaml.safe_load(MKDOCS_YML.read_text(encoding="utf-8"))
        assert passport.PASSPORT_PATH in nav_paths(config["nav"])
