"""The guide's interchange schema page: the shipped schema, byte for byte, inside one fence.

The oracle reads the package file independently and compares it with the fenced text of the
*committed* page, not only of a fresh render.

Not reached: that the schema is valid JSON Schema or agrees with the reader
(``test_requirements_interchange.py``); the rendered site; the byte comparison of the committed
page with the generator (the drift gate, ``test_guide_reference_drift.py``).
"""

from __future__ import annotations

from importlib.resources import files as resource_files

import pytest
import yaml
from guide_fixtures import GUIDE_DIR, MKDOCS_YML, nav_paths
from swcraft_tools import requirements_interchange
from swcraft_tools.guide_reference import INDEX_PATH, all_artifacts, interchange
from swcraft_tools.guide_reference.catalogue import PAGE_SUMMARIES

_OPEN = "```json\n"
_CLOSE = "```\n"


def _shipped_schema() -> str:
    """The package file's text, read here rather than through the module under test."""
    resource = resource_files("swcraft_tools") / requirements_interchange.SCHEMA_RESOURCE
    return resource.read_text(encoding="utf-8")


def _fenced(page: str) -> str:
    """The text between the page's one ``json`` fence and its closing fence, the newline before it kept."""
    assert page.count(_OPEN) == 1, "the page carries not exactly one json fence"
    start = page.index(_OPEN) + len(_OPEN)
    assert page.endswith(_CLOSE), "the fence is not the page's last block"
    return page[start : len(page) - len(_CLOSE)]


def _committed_page() -> str:
    return (GUIDE_DIR / interchange.INTERCHANGE_PATH).read_text(encoding="utf-8")


class TestTheSchemaIsCarriedVerbatim:
    def test_the_committed_page_carries_the_shipped_schema_byte_for_byte(self) -> None:
        assert _fenced(_committed_page()) == _shipped_schema()

    def test_a_fresh_render_carries_it_too(self) -> None:
        assert _fenced(interchange.interchange_page()) == _shipped_schema()

    def test_the_text_is_not_reformatted(self) -> None:
        """No JSON round trip: spacing a re-serialization would normalize reaches the page as written."""
        text = '{ "a" :1,\n\t"b":[ 2 ]}\n'
        assert _fenced(interchange.interchange_page(text=text)) == text

    def test_a_text_with_no_final_newline_gets_one_and_nothing_else(self) -> None:
        assert _fenced(interchange.interchange_page(text="{}")) == "{}\n"

    def test_a_text_that_would_close_the_fence_is_refused(self) -> None:
        with pytest.raises(interchange.UnfenceableSchema):
            interchange.interchange_page(text='{"description": "```"}\n')


class TestThePageIsInTheSection:
    def test_the_heading_is_the_index_title(self) -> None:
        title, _summary = PAGE_SUMMARIES[interchange.INTERCHANGE_PATH]
        assert f"\n# {title}\n" in _committed_page()

    def test_the_index_carries_the_summary_row(self) -> None:
        title, summary = PAGE_SUMMARIES[interchange.INTERCHANGE_PATH]
        index = (GUIDE_DIR / INDEX_PATH).read_text(encoding="utf-8")
        assert f"| [{title}](requirements-interchange.md) | {summary} |" in index

    def test_the_drift_gate_population_includes_the_page(self) -> None:
        assert interchange.INTERCHANGE_PATH in all_artifacts()

    def test_the_navigation_names_the_page(self) -> None:
        config = yaml.safe_load(MKDOCS_YML.read_text(encoding="utf-8"))
        assert interchange.INTERCHANGE_PATH in nav_paths(config["nav"])

    def test_the_page_names_the_command_and_the_dry_run(self) -> None:
        page = _committed_page()
        assert "`swcraft import requirements --source FILE`" in page
        assert "`swcraft import requirements --dry-run`" in page
