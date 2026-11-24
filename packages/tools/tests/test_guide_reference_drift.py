"""The drift and navigation gates for the guide's committed reference pages.

``docs/guide/reference/`` holds the committed rendered output of
:func:`swcraft_tools.guide_reference.all_artifacts`. It can go stale both ways: the generator ahead of
the tree (every artifact must exist on disk, byte-identical) and the tree ahead of the generator (a
page nothing produces any more).

**The exemption.** ``reference/tools.md`` is generated in ``packages/mcp/`` (``swcraft_tools`` may
not import ``swcraft_mcp``), so it is exempted from the orphan direction **by name**, and no
assertion here requires it to exist; its own drift gate is
``packages/mcp/tests/test_tool_reference.py``'s. For the same reason the navigation gate is two
inclusions, not one equality: every generated page is in ``mkdocs.yml``'s reference section, and
every row there names a generated page or the exempted one.

**What this module does not reach.** Whether the bytes are *right* (the content oracles in
``test_guide_reference_model.py``, ``test_guide_reference_rules.py`` and
``test_guide_reference_cli.py``); a narrative page missing from the navigation (the guide surface
gate's); and whether a link inside a page resolves (``mkdocs build --strict``, in
``python scripts/dev.py docs``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest
import yaml
from guide_fixtures import GUIDE_DIR, MKDOCS_YML, REFERENCE_DIR, committed_pages, nav_paths
from swcraft_tools import __version__
from swcraft_tools.guide_reference import (
    FOREIGN_PAGES,
    INDEX_PATH,
    NAV_SECTION_TITLE,
    UntitledPage,
    all_artifacts,
    nav_entries,
)
from swcraft_tools.guide_reference.__main__ import (
    UnreadableNavSection,
    _row,
    main,
    write_nav,
    write_pages,
)
from swcraft_tools.guide_reference.catalogue import MODEL_PAGE_PREFIX, PAGE_SUMMARIES, title_of

REGENERATE = "regenerate with `python scripts/dev.py docs-reference` and commit the result"

#: The pages under ``docs/guide/reference/`` that another package generates, which this gate
#: neither requires nor rejects.
EXEMPT_PAGES = frozenset({"reference/tools.md"})

#: The prefix that scopes the navigation gate. The narrative pages beside
#: ``reference/`` are hand-written and are the guide surface gate's business.
_REFERENCE_PREFIX = "reference/"


def _committed_pages() -> dict[str, Path]:
    """Every committed file under the reference directory, keyed by its path relative to ``docs/guide/``."""
    assert REFERENCE_DIR.is_dir(), f"{REFERENCE_DIR} is missing — {REGENERATE}"
    return committed_pages(REFERENCE_DIR)


def _referencenav_paths() -> list[str]:
    """The document paths the committed ``mkdocs.yml`` navigation names under ``reference/``."""
    config = yaml.safe_load(MKDOCS_YML.read_text(encoding="utf-8"))
    return [path for path in nav_paths(config["nav"]) if path.startswith(_REFERENCE_PREFIX)]


# The rules, as functions: every gate and every case of ``TestTheGatesFire`` calls the same one (a
# fire-test that re-derives the set expression by hand passes an emptied gate). A ``None`` population
# reads the real tree.


def stale_pages(
    artifacts: Mapping[str, str] | None = None,
    committed: Mapping[str, Path] | None = None,
) -> list[str]:
    """Generated pages whose committed bytes are not the bytes the generator renders."""
    artifacts = all_artifacts() if artifacts is None else artifacts
    committed = _committed_pages() if committed is None else committed
    return [
        relative
        for relative, text in artifacts.items()
        if relative in committed and committed[relative].read_bytes() != text.encode("utf-8")
    ]


def uncommitted_pages(
    artifacts: Mapping[str, str] | None = None,
    committed: Mapping[str, Path] | None = None,
) -> list[str]:
    """Generated pages that are not on disk at all."""
    artifacts = all_artifacts() if artifacts is None else artifacts
    committed = _committed_pages() if committed is None else committed
    return [relative for relative in artifacts if relative not in committed]


def orphan_pages(
    artifacts: Mapping[str, str] | None = None,
    committed: Iterable[str] | None = None,
) -> list[str]:
    """Committed pages under ``reference/`` that no generator produces and no exemption covers."""
    artifacts = all_artifacts() if artifacts is None else artifacts
    committed = _committed_pages() if committed is None else committed
    return sorted(set(committed) - set(artifacts) - EXEMPT_PAGES)


def unnavigated_pages(
    artifacts: Mapping[str, str] | None = None,
    navigated: Iterable[str] | None = None,
) -> list[str]:
    """Generated pages no navigation row reaches."""
    artifacts = all_artifacts() if artifacts is None else artifacts
    navigated = _referencenav_paths() if navigated is None else navigated
    return sorted(set(artifacts) - set(navigated))


def unbacked_nav_rows(
    artifacts: Mapping[str, str] | None = None,
    navigated: Iterable[str] | None = None,
) -> list[str]:
    """Navigation rows under ``reference/`` that name a page nothing produces."""
    artifacts = all_artifacts() if artifacts is None else artifacts
    navigated = _referencenav_paths() if navigated is None else navigated
    return sorted(set(navigated) - set(artifacts) - EXEMPT_PAGES)


def version_carriers(committed: Mapping[str, Path] | None = None) -> list[str]:
    """Committed pages whose text embeds the product version string."""
    committed = _committed_pages() if committed is None else committed
    return [relative for relative, path in committed.items() if __version__ in path.read_text(encoding="utf-8")]


# The gates


def test_every_generated_page_is_committed_byte_identically() -> None:
    """Generator ahead of the tree: a missing or differing page is drift, and the message names the fix."""
    missing = uncommitted_pages()
    assert not missing, f"not committed under {GUIDE_DIR}: {missing} — {REGENERATE}"
    stale = stale_pages()
    assert not stale, f"stale under {GUIDE_DIR} (the generator renders different bytes): {stale} — {REGENERATE}"


def test_every_committed_page_is_one_the_generator_produces() -> None:
    """Tree ahead of the generator: an orphan left by a renamed or removed layer is drift too."""
    orphans = orphan_pages()
    assert not orphans, f"pages under {REFERENCE_DIR} that no generator produces: {orphans} — delete them, {REGENERATE}"


def test_the_exemption_set_is_exactly_the_mcp_owned_page() -> None:
    """One name: the tool page is generated across a boundary this package may not cross.

    A second entry would be a second generator writing into ``docs/guide/reference/`` with no gate of its own.
    """
    assert EXEMPT_PAGES == frozenset({"reference/tools.md"})


def test_the_exemption_is_not_asserted_to_exist() -> None:
    """The gate is green with the exempted page present and with it absent: nothing here reads it off disk."""
    committed = set(_committed_pages())
    for state in (committed | EXEMPT_PAGES, committed - EXEMPT_PAGES):
        assert orphan_pages(committed=state) == []


def test_every_generated_page_is_in_the_navigation() -> None:
    """First inclusion: a generated page absent from ``nav`` is published and unreachable from the menu."""
    missing = unnavigated_pages()
    assert not missing, (
        f"generated pages missing from {MKDOCS_YML.name}'s {NAV_SECTION_TITLE} nav: {missing} — {REGENERATE}"
    )


def test_every_navigated_reference_page_is_generated_or_exempt() -> None:
    """Second inclusion: a ``nav`` row naming no page fails ``mkdocs build --strict``."""
    unknown = unbacked_nav_rows()
    assert not unknown, f"{MKDOCS_YML.name} navigates reference pages nothing generates: {unknown} — {REGENERATE}"


def test_the_navigation_rows_are_in_the_generators_order() -> None:
    """The committed navigation is the generator's own row order, not an order somebody sorted."""
    generated = [path for _, path in nav_entries(all_artifacts())]
    assert [path for path in _referencenav_paths() if path in set(generated)] == generated


def test_no_committed_page_carries_the_product_version() -> None:
    """The guide is not a sixth lockstep site: no committed page carries the version string.

    The likely offender is the command page: ``cli.dispatch.build_parser()``'s ``--version`` carries the version.
    """
    carriers = version_carriers()
    assert not carriers, f"committed guide pages embedding the product version {__version__!r}: {carriers}"


def test_the_gates_read_a_non_empty_corpus() -> None:
    """A gate over nothing passes vacuously; pin that each population has something in it."""
    assert all_artifacts(), "the generator produces no page at all"
    assert _committed_pages(), f"nothing is committed under {REFERENCE_DIR}"
    assert _referencenav_paths(), f"{MKDOCS_YML.name} navigates no page under {_REFERENCE_PREFIX}"


class TestTheGatesFire:
    """Every gate above, shown failing against a mutated copy through the same function the gate calls."""

    def test_a_differing_page_is_found(self) -> None:
        artifacts = dict(all_artifacts())
        relative = next(iter(artifacts))
        artifacts[relative] += "\nan edit nobody regenerated\n"
        assert stale_pages(artifacts=artifacts) == [relative]

    def test_a_missing_page_is_found(self) -> None:
        # The page removed is one the generator produces: removing the other package's page would
        # leave the expression nothing to find.
        relative = next(iter(all_artifacts()))
        committed = {name: path for name, path in _committed_pages().items() if name != relative}
        assert uncommitted_pages(committed=committed) == [relative]

    def test_an_orphan_page_is_found(self) -> None:
        committed = set(_committed_pages()) | {"reference/model/a_removed_layer.md"}
        assert orphan_pages(committed=committed) == ["reference/model/a_removed_layer.md"]

    def test_a_page_missing_from_the_navigation_is_found(self) -> None:
        navigated = set(_referencenav_paths()) - {"reference/rules.md"}
        assert unnavigated_pages(navigated=navigated) == ["reference/rules.md"]

    def test_a_navigation_row_with_no_page_is_found(self) -> None:
        navigated = set(_referencenav_paths()) | {"reference/invented.md"}
        assert unbacked_nav_rows(navigated=navigated) == ["reference/invented.md"]

    def test_a_page_carrying_the_version_is_found(self, tmp_path: Path) -> None:
        """The version gate itself, over a corpus of two pages this case writes."""
        carrier = tmp_path / "carrier.md"
        carrier.write_text(f"pip install swcraft-tools=={__version__}\n", encoding="utf-8")
        clean = tmp_path / "clean.md"
        clean.write_text("Install the current release.\n", encoding="utf-8")
        assert version_carriers(committed={"carrier.md": carrier, "clean.md": clean}) == ["carrier.md"]


class TestTheWriter:
    """``guide_reference.__main__`` — the writing half, exercised in a temporary tree.

    The command has no ``--check`` mode (the gates above are the comparison); these cases witness that
    what it writes is what the gates read, that it keeps a row it does not own, and that it is idempotent.
    """

    def test_writing_then_reading_round_trips_every_page(self, tmp_path: Path) -> None:
        artifacts = all_artifacts()
        written, orphans = write_pages(artifacts, tmp_path)
        assert (written, orphans) == (len(artifacts), ())
        for relative, text in artifacts.items():
            assert (tmp_path / relative).read_text(encoding="utf-8") == text

    def test_an_orphan_layer_page_is_pruned(self, tmp_path: Path) -> None:
        artifacts = all_artifacts()
        write_pages(artifacts, tmp_path)
        orphan = tmp_path / "reference" / "model" / "a_removed_layer.md"
        orphan.write_text("# A removed layer\n", encoding="utf-8")
        _, pruned = write_pages(artifacts, tmp_path)
        assert pruned == (orphan,)
        assert not orphan.exists()

    def test_a_page_outside_the_model_subtree_is_not_pruned(self, tmp_path: Path) -> None:
        """The exempted page lives beside ``rules.md``: a wider sweep would delete the other generator's output."""
        artifacts = all_artifacts()
        write_pages(artifacts, tmp_path)
        foreign = tmp_path / "reference" / "tools.md"
        foreign.write_text("# The assistant's tools\n", encoding="utf-8")
        _, pruned = write_pages(artifacts, tmp_path)
        assert pruned == ()
        assert foreign.exists()

    def test_the_navigation_rewrite_keeps_a_row_it_does_not_own(self, tmp_path: Path) -> None:
        config = tmp_path / "mkdocs.yml"
        config.write_text(MKDOCS_YML.read_text(encoding="utf-8"), encoding="utf-8")
        text = config.read_text(encoding="utf-8").replace(
            "  - Reference:\n", "  - Reference:\n      - The assistant's tools: reference/tools.md\n"
        )
        config.write_text(text, encoding="utf-8")
        assert write_nav(config, nav_entries(all_artifacts()))
        rewritten = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert "reference/tools.md" in nav_paths(rewritten["nav"])

    def test_the_navigation_rewrite_drops_a_row_for_a_pruned_layer_page(self, tmp_path: Path) -> None:
        """The model subtree is owned in full: a removed layer's row is deleted, never kept as foreign."""
        config = tmp_path / "mkdocs.yml"
        config.write_text(MKDOCS_YML.read_text(encoding="utf-8"), encoding="utf-8")
        text = config.read_text(encoding="utf-8").replace(
            "  - Reference:\n",
            "  - Reference:\n      - A removed layer: reference/model/a_removed_layer.md\n",
        )
        config.write_text(text, encoding="utf-8")
        assert write_nav(config, nav_entries(all_artifacts()))
        rewritten = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert "reference/model/a_removed_layer.md" not in nav_paths(rewritten["nav"])
        assert config.read_text(encoding="utf-8") == MKDOCS_YML.read_text(encoding="utf-8")

    def test_the_navigation_rewrite_is_idempotent(self, tmp_path: Path) -> None:
        config = tmp_path / "mkdocs.yml"
        config.write_text(MKDOCS_YML.read_text(encoding="utf-8"), encoding="utf-8")
        entries = nav_entries(all_artifacts())
        assert not write_nav(config, entries), "the committed nav is already what the generator writes"
        assert config.read_text(encoding="utf-8") == MKDOCS_YML.read_text(encoding="utf-8")

    def test_the_navigation_rewrite_adds_the_section_when_it_is_absent(self, tmp_path: Path) -> None:
        config = tmp_path / "mkdocs.yml"
        config.write_text("site_name: T\nnav:\n  - Start here: index.md\n", encoding="utf-8")
        assert write_nav(config, nav_entries(all_artifacts()))
        parsed = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert nav_paths(parsed["nav"]) == ["index.md", *[path for _, path in nav_entries(all_artifacts())]]

    def test_a_configuration_with_no_nav_block_is_refused(self, tmp_path: Path) -> None:
        """Appending to an implicit navigation would produce a site ``--strict`` never checks against one."""
        config = tmp_path / "mkdocs.yml"
        config.write_text("site_name: T\n", encoding="utf-8")
        with pytest.raises(ValueError, match="no `nav:` block"):
            write_nav(config, nav_entries(all_artifacts()))

    def test_every_generated_page_has_a_navigation_label(self) -> None:
        """The label is the page's own heading; a page with none is refused rather than labelled by path."""
        labels = dict((path, label) for label, path in nav_entries(all_artifacts()))
        assert set(labels) == set(all_artifacts())
        assert all(label and not label.endswith(".md") for label in labels.values())

    def test_a_page_with_no_heading_is_refused(self) -> None:
        with pytest.raises(UntitledPage):
            nav_entries({"reference/index.md": "# Reference\n", "reference/nameless.md": "no heading here\n"})

    def test_a_label_that_would_be_an_ambiguous_scalar_is_quoted(self) -> None:
        """``_label``'s guard: ``Model: layers`` would otherwise parse as a different YAML mapping."""
        assert _row("Model: layers", "reference/model/index.md") == (
            '      - "Model: layers": reference/model/index.md'
        )
        assert _row('The "assistant"', "reference/tools.md") == ('      - "The \\"assistant\\"": reference/tools.md')
        assert _row("Plain title", "reference/index.md") == "      - Plain title: reference/index.md"

    def test_a_nested_sub_group_in_the_section_is_refused(self, tmp_path: Path) -> None:
        """A line the parse cannot classify is a line the rewrite would delete, so it raises.

        ``      - Model:`` is a label with no path; a rewrite that misread it would drop it and every row under it.
        """
        config = tmp_path / "mkdocs.yml"
        text = MKDOCS_YML.read_text(encoding="utf-8").replace(
            "  - Reference:\n", "  - Reference:\n      - Model:\n          - Layers: reference/model/index.md\n"
        )
        config.write_text(text, encoding="utf-8")
        with pytest.raises(UnreadableNavSection, match="cannot classify"):
            write_nav(config, nav_entries(all_artifacts()))
        assert config.read_text(encoding="utf-8") == text, "the file was rewritten before the refusal"

    def test_a_blank_line_after_the_section_stays_below_it(self, tmp_path: Path) -> None:
        """The section ends at its last row, so a following blank line is not absorbed and re-emitted above."""
        config = tmp_path / "mkdocs.yml"
        entries = nav_entries(all_artifacts())
        rows = "\n".join(f"      - {label}: {path}" for label, path in entries)
        original = f"site_name: T\nnav:\n  - Start here: index.md\n  - Reference:\n{rows}\n\ntheme:\n  name: material\n"
        config.write_text(original, encoding="utf-8")

        assert not write_nav(config, entries), "the section was already exactly what the generator writes"
        assert config.read_text(encoding="utf-8") == original


class TestTheIndex:
    """The section index against the two other statements of a page's identity."""

    def test_every_index_title_is_the_pages_own_heading(self) -> None:
        """`PAGE_SUMMARIES` carries a title and `title_of` reads one off the page — pin them equal."""
        artifacts = all_artifacts()
        for path, (title, _) in PAGE_SUMMARIES.items():
            assert title_of(path, artifacts[path]) == title, (
                f"{path}: the index calls it {title!r} and the page's own heading says "
                f"{title_of(path, artifacts[path])!r}"
            )

    def test_the_index_lists_every_page_of_the_section(self) -> None:
        """The index's first sentence says "exhaustively"; the layer pages are folded into the model index's row."""
        artifacts = all_artifacts()
        listed = set(re.findall(r"\]\(([^)]+)\)", artifacts[INDEX_PATH]))
        expected = {
            path[len("reference/") :]
            for path in [*artifacts, *FOREIGN_PAGES]
            if path != INDEX_PATH and not (path.startswith(MODEL_PAGE_PREFIX) and path != "reference/model/index.md")
        }
        assert listed == expected, f"missing from the index: {sorted(expected - listed)}"

    def test_the_foreign_page_set_is_exactly_the_exempted_one(self) -> None:
        """`FOREIGN_PAGES` (what the index lists) and `EXEMPT_PAGES` (what the orphan direction skips) agree."""
        assert set(FOREIGN_PAGES) == EXEMPT_PAGES


class TestTheCommand:
    """``main()`` itself — the entry point ``python scripts/dev.py docs-reference`` runs.

    The pages land where the drift gate reads them, the navigation is rewritten only when asked, and a
    second run changes nothing.
    """

    def test_it_writes_every_page_where_the_gate_reads_it(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:

        assert main(["--output-dir", str(tmp_path)]) == 0
        for relative, text in all_artifacts().items():
            assert (tmp_path / relative).read_text(encoding="utf-8") == text
        assert "wrote" in capsys.readouterr().out

    def test_a_second_run_is_a_no_op(self, tmp_path: Path) -> None:
        config = tmp_path / "mkdocs.yml"
        config.write_text(MKDOCS_YML.read_text(encoding="utf-8"), encoding="utf-8")
        argv = ["--output-dir", str(tmp_path), "--mkdocs-yml", str(config)]
        assert main(argv) == 0
        first = {
            path: path.read_bytes()
            for path in sorted(tmp_path.rglob("*"), key=lambda path: path.as_posix())
            if path.is_file()
        }
        assert main(argv) == 0
        second = {
            path: path.read_bytes()
            for path in sorted(tmp_path.rglob("*"), key=lambda path: path.as_posix())
            if path.is_file()
        }
        assert first == second

    def test_it_leaves_the_navigation_alone_when_it_is_not_given_one(self, tmp_path: Path) -> None:
        config = tmp_path / "mkdocs.yml"
        config.write_text("site_name: T\nnav:\n  - Start here: index.md\n", encoding="utf-8")
        assert main(["--output-dir", str(tmp_path)]) == 0
        assert config.read_text(encoding="utf-8") == "site_name: T\nnav:\n  - Start here: index.md\n"

    def test_it_reports_the_pages_it_pruned(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        main(["--output-dir", str(tmp_path)])
        orphan = tmp_path / "reference" / "model" / "a_removed_layer.md"
        orphan.write_text("# A removed layer\n", encoding="utf-8")
        assert main(["--output-dir", str(tmp_path)]) == 0
        assert "a_removed_layer.md" in capsys.readouterr().out
        assert not orphan.exists()

    def test_an_output_directory_is_required(self) -> None:
        with pytest.raises(SystemExit):
            main([])
