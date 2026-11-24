"""``python -m swcraft_tools.guide_reference``: write the generated guide pages.

Puts the rendered pages on disk and keeps ``mkdocs.yml``'s reference navigation section pointing at
exactly them. ``python scripts/dev.py docs-reference`` runs it; it is the only place in
``packages/tools/`` that writes into ``docs/guide/``.

There is no ``--check`` mode: the byte comparison lives in the drift test, and a second
implementation would be a second place to disagree.

The navigation rewrite keeps rows it does not own: ``reference/tools.md`` is generated in
``packages/mcp/``, and deleting its row would make the two steps of one command fight over one file.
Only ``reference/model/`` is pruned of orphans, because one generator renders that subtree in full (a
removed layer leaves a file nothing produces); an orphan elsewhere is reported by the drift gate
instead of deleted on a guess.

**What this module does not reach:** whether the pages are right. The generators' content oracles
witness that, and ``mkdocs build --strict`` that the navigation resolves.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from swcraft_tools.guide_reference.catalogue import NAV_SECTION_TITLE, all_artifacts, nav_entries

__all__ = ["UnreadableNavSection", "main", "write_nav", "write_pages"]

#: The subtree :func:`write_pages` sweeps for orphans, relative to the output directory.
PRUNED_SUBTREE = "reference/model"

#: The indentation of ``mkdocs.yml``'s ``nav:`` block: two spaces for a top-level row, six inside a
#: section, so the rewrite matches the file it edits.
_SECTION_INDENT = "  "
_ENTRY_INDENT = "      "

#: The characters that make a bare YAML scalar ambiguous; a label carrying one is quoted.
_NEEDS_QUOTING = set(":#{}[]&*!|>'\"%@`,")


def write_pages(artifacts: Mapping[str, str], output_dir: Path) -> tuple[int, tuple[Path, ...]]:
    """Every artifact written under *output_dir*, then :data:`PRUNED_SUBTREE`'s orphans pruned.

    Returns the number of files written and the orphans removed. The text is written UTF-8 with no
    newline translation, so the bytes on disk are the bytes the drift gate compares.
    """
    for relative, text in artifacts.items():
        path = output_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    swept = output_dir / PRUNED_SUBTREE
    orphans = tuple(
        path
        for path in sorted(swept.rglob("*.md"), key=lambda path: path.as_posix())
        if path.relative_to(output_dir).as_posix() not in artifacts
    )
    for path in orphans:
        path.unlink()
    return len(artifacts), orphans


def _label(text: str) -> str:
    """One navigation label, quoted only when a bare scalar would be ambiguous."""
    if not text or text != text.strip() or _NEEDS_QUOTING & set(text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _row(label: str, path: str) -> str:
    """One navigation row inside the section."""
    return f"{_ENTRY_INDENT}- {_label(label)}: {path}"


class UnreadableNavSection(ValueError):
    """A line inside the navigation section that this rewrite cannot classify.

    Raised rather than skipped, because a line the parse does not return is a line the rewrite
    **deletes** -- a nested sub-group (``      - Model:``) would vanish with every row under it.
    """

    def __init__(self, line: str) -> None:
        self.line = line
        super().__init__(
            f"the {NAV_SECTION_TITLE} navigation section holds a line this rewrite cannot classify "
            f"as a `- <label>: <path>` row and would therefore delete: {line.strip()!r}. Teach "
            "`_existing_rows` about the shape, or take the line out of the section."
        )


def _existing_rows(body: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """The ``(row, path)`` pairs a section body already holds, in file order.

    Split on the last ``": "``, so a label carrying a colon keeps its path. Blank lines are passed
    over; anything else that is not a ``- <label>: <path>`` row raises :class:`UnreadableNavSection`.
    """
    rows = []
    for line in body:
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("- ") or ": " not in stripped:
            raise UnreadableNavSection(line)
        rows.append((line, stripped.rsplit(": ", 1)[1].strip()))
    return tuple(rows)


def _section_bounds(lines: Sequence[str]) -> tuple[int, int] | None:
    """The half-open line range of the navigation section, or ``None`` when it has none yet.

    Trailing blank lines are not part of the section; otherwise the rewrite would re-emit a following
    blank line above the rows instead of below them.
    """
    header = f"{_SECTION_INDENT}- {NAV_SECTION_TITLE}:"
    for start, line in enumerate(lines):
        if line.rstrip() != header:
            continue
        end = start + 1
        while end < len(lines) and (not lines[end].strip() or lines[end].startswith(_SECTION_INDENT + " ")):
            end += 1
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        return start, end
    return None


def _nav_block_end(lines: Sequence[str]) -> int:
    """The line index just past the last row of the top-level ``nav:`` block.

    Raises ``ValueError`` when the file declares no ``nav:``: appending a section there would silently
    produce a second, implicit navigation, and ``mkdocs build --strict`` checks the explicit one.
    """
    for start, line in enumerate(lines):
        if line.rstrip() != "nav:":
            continue
        end = start + 1
        last = start + 1
        while end < len(lines) and (not lines[end].strip() or lines[end].startswith(" ")):
            if lines[end].strip():
                last = end + 1
            end += 1
        return last
    raise ValueError("mkdocs.yml declares no `nav:` block to add the reference section to")


def write_nav(mkdocs_yml: Path, entries: Iterable[tuple[str, str]]) -> bool:
    """``mkdocs.yml``'s navigation section rewritten from *entries*, keeping unowned rows.

    Returns whether the file changed. The rewrite is line-based and touches only the section's rows,
    so the file's comments survive, which a parse-and-dump would not. A row under
    :data:`PRUNED_SUBTREE` is never kept as unowned: it would point at a pruned layer page.
    """
    original = mkdocs_yml.read_text(encoding="utf-8")
    lines = original.splitlines()
    owned = list(entries)
    owned_paths = {path for _, path in owned}
    bounds = _section_bounds(lines)
    kept: list[str] = []
    if bounds is None:
        start = end = _nav_block_end(lines)
    else:
        start, end = bounds
        kept = [
            row
            for row, path in _existing_rows(lines[start + 1 : end])
            if path not in owned_paths and not path.startswith(PRUNED_SUBTREE + "/")
        ]
    section = [f"{_SECTION_INDENT}- {NAV_SECTION_TITLE}:", *(_row(label, path) for label, path in owned), *kept]
    rewritten = "\n".join([*lines[:start], *section, *lines[end:]]) + "\n"
    if rewritten != original:
        mkdocs_yml.write_text(rewritten, encoding="utf-8", newline="\n")
    return rewritten != original


def main(argv: list[str] | None = None) -> int:
    """``python -m swcraft_tools.guide_reference --output-dir DIR [--mkdocs-yml FILE]``.

    ``--output-dir`` is the guide's source tree (``docs/guide``), the root the artifact keys and the
    navigation rows are both relative to.
    """
    parser = argparse.ArgumentParser(
        prog="python -m swcraft_tools.guide_reference",
        description="Generate the user guide's reference pages from SWCraft's own definitions.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="the guide's source tree (docs/guide); pages are written at their path relative to it",
    )
    parser.add_argument(
        "--mkdocs-yml",
        type=Path,
        default=None,
        help="the mkdocs configuration whose reference navigation section is rewritten (optional)",
    )
    args = parser.parse_args(argv)
    output_dir: Path = args.output_dir
    artifacts = all_artifacts()
    written, orphans = write_pages(artifacts, output_dir)
    summary = f"wrote {written} reference pages to {output_dir}, removed {len(orphans)} stale"
    if orphans:
        summary += f": {', '.join(path.relative_to(output_dir).as_posix() for path in orphans)}"
    if args.mkdocs_yml is not None:
        verb = "rewrote" if write_nav(args.mkdocs_yml, nav_entries(artifacts)) else "confirmed"
        summary += f"; {verb} the {NAV_SECTION_TITLE} nav section in {args.mkdocs_yml}"
    print(summary)
    return 0


if __name__ == "__main__":  # pragma: no cover - the one line only `python -m ...` reaches
    raise SystemExit(main())
