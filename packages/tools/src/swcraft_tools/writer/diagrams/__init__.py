"""The diagram generator: the model drawn as UML/SysML views into ``views_root``.

:func:`generate_diagrams` is the one entry point; it owns the write discipline and draws nothing:

1. ``graph.build_graphs`` reads the model once and builds every view and every ``NotDrawn`` record;
2. every view's file names are checked (:data:`VIEW_ID_RE`, :data:`MAX_FILE_NAME_BYTES`, no name
   declared twice) before any layout;
3. ``layout.layout`` lays every ELK graph out in one Node process; every file's text is produced in memory;
4. only then are files written: an unchanged one is left untouched (bytes and mtime), a changed one
   is staged in a hidden sibling and renamed over its name, and every file of the generator's own
   naming (:data:`GENERATED_FILE_RE`) this run did not produce is removed.

A failed build leaves no outputs: a failure in steps 1-3 raises before the first write and ``views_root`` is not
created; a link or a directory at a written name is refused. A failure during step 4 (a full disk, a file held
open on Windows) leaves the files written before it: each file is replaced whole, but the set is not swapped in as
one. The write never writes through a name. Not reached: ``views_root`` being a link is followed; on Windows the
comparison may read through a link; a rewritten file takes a fresh mode; a killed process leaves its ``.tmp``
file; two concurrent runs on one ``views_root`` are not serialised.

Names are compared by ``str.casefold()``; a case variant is replaced by this run's spelling, and a stem equal to an
earlier one but for case takes a ``~<n>`` suffix, which can move when the earlier element is renamed. The result
names no path.
"""

from __future__ import annotations

import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Protocol

from swcraft_tools.exclusive_create import open_new
from swcraft_tools.writer.diagrams import deployment, graph, layout, nesting, page, report, sequence, svg
from swcraft_tools.writer.diagrams.layout import DiagramLayoutError

__all__ = [
    "GENERATED_FILE_RE",
    "MAX_FILE_NAME_BYTES",
    "VIEW_ID_RE",
    "DiagramGenerationError",
    "DiagramLayoutError",
    "DiagramsResult",
    "generate_diagrams",
]

#: The view ids the generator writes files for, with an optional case-collision suffix ``~<n>``.
VIEW_ID_RE = re.compile(r"[A-Za-z0-9_-]+(?:~[1-9][0-9]*)?")

#: The only file names a later run removes when it does not produce them.
GENERATED_FILE_RE = re.compile(r"(?:(?:ibd|overview|sequence)-.+|nesting|deployment)\.(?:svg|html)")

#: The longest file name the generator writes, in UTF-8 bytes: what every mainstream filesystem
#: caps one path component at. A view's names are ASCII, so here a byte is a character.
MAX_FILE_NAME_BYTES = 255

#: The staging file's prefix. Its length does not depend on the target's name (53 bytes in all),
#: so every name within :data:`MAX_FILE_NAME_BYTES` can be staged.
_STAGING_PREFIX = ".swcraft-diagram"

#: The characters XML 1.0's ``Char`` production excludes, apart from the surrogates UTF-8 refuses
#: first: the C0 controls but tab, line feed and carriage return, and U+FFFE and U+FFFF.
XML_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


class DiagramGenerationError(Exception):
    """The generator refused the model's views before the first write; nothing was written."""


@dataclass(frozen=True)
class DiagramsResult:
    """What one run did, by file name inside ``views_root``.

    ``written`` and ``kept`` list produced files in write order (each view's ``.svg`` then
    ``.html``, then the report, then the index); ``removed`` the stale files in name order.
    """

    views: tuple[str, ...]
    written: tuple[str, ...]
    kept: tuple[str, ...]
    removed: tuple[str, ...]
    not_drawn: tuple[graph.NotDrawn, ...]
    layout_seconds: float
    report_file: str = report.REPORT_FILE


def _refuse_file_names(view_ids: list[str]) -> None:
    illegal = [view for view in view_ids if VIEW_ID_RE.fullmatch(view) is None]
    if illegal:
        listed = ", ".join(repr(view) for view in illegal)
        raise DiagramGenerationError(
            f"no diagram was written: the view {listed} cannot be a file name, which takes ASCII letters, "
            "digits, '_' and '-' only. `swcraft check-model` reports the name that makes it."
        )
    too_long = [
        (view, longest)
        for view in view_ids
        if (longest := max(len(name.encode("utf-8")) for name in (f"{view}.svg", page.page_file(view))))
        > MAX_FILE_NAME_BYTES
    ]
    if too_long:
        listed = ", ".join(f"{view!r} ({longest} bytes)" for view, longest in too_long)
        raise DiagramGenerationError(
            f"no diagram was written: the view {listed} gives a file name longer than the {MAX_FILE_NAME_BYTES} "
            f"bytes every mainstream filesystem holds -- at most {MAX_FILE_NAME_BYTES} are allowed, its extension "
            "included. `swcraft check-model` holds a sequence name to no length, and a name equal to an earlier "
            "one but for case takes a '~<n>' suffix on top of it; shorten the name."
        )
    seen: set[str] = set()
    repeated: list[str] = []
    for view in view_ids:
        if view in seen and view not in repeated:
            repeated.append(view)
        seen.add(view)
    if repeated:
        listed = ", ".join(repr(view) for view in repeated)
        raise DiagramGenerationError(
            f"no diagram was written: the view {listed} is drawn from a name declared more than once, and "
            "both would be written into one file. `swcraft check-model` reports the name declared twice."
        )


class _Draw(Protocol):
    """A renderer with its view's inputs bound, collecting model entries into *sources*."""

    def __call__(self, *, sources: list[svg.ModelEntry] | None = None) -> str: ...


def _render_views(
    extracted: graph.DiagramGraphs, laid_out: list[dict]
) -> list[tuple[str, str, dict[str, svg.ModelEntry]]]:
    """Every view's id, SVG text (temporary source attributes stripped) and model block, in view order."""
    rendered: list[tuple[str, str, dict[str, svg.ModelEntry]]] = []

    def add(view: str, draw: _Draw) -> None:
        sources: list[svg.ModelEntry] = []
        text, entries = page.strip_sources(draw(sources=sources), sources)
        rendered.append((view, text, entries))

    answers = iter(laid_out)
    contents = extracted.interface_contents
    for item in extracted.compositions:
        ibd, overview = next(answers), next(answers)
        add(f"ibd-{item.stem}", partial(svg.render_ibd, ibd, contents))
        add(f"overview-{item.stem}", partial(svg.render_overview, overview))
    if extracted.nesting is not None:
        add(graph.NESTING_VIEW, partial(nesting.render_nesting, extracted.nesting))
    if extracted.deployment is not None:
        add(graph.DEPLOYMENT_VIEW, partial(deployment.render_deployment, extracted.deployment))
    for view, stem in zip(extracted.sequences, extracted.sequence_stems, strict=True):
        add(f"sequence-{stem}", partial(sequence.render_sequence, view, contents))
    return rendered


def _render_files(
    extracted: graph.DiagramGraphs, laid_out: list[dict], view_ids: list[str], project_name: str
) -> dict[str, str]:
    """Every file's name and text, in the order they are written: each view's two, the report, the index."""
    rendered = _render_views(extracted, laid_out)
    views = [(view, svg_text) for view, svg_text, _entries in rendered]
    if [view for view, _ in views] != view_ids:
        raise AssertionError("the rendered views are not in the extractor's view order")  # pragma: no cover
    report_text = report.render_report(project_name, views, extracted.not_drawn)
    stems = extracted.composition_stems()
    placements = extracted.placements
    files: dict[str, str] = {}
    for view, svg_text, entries in rendered:
        files[f"{view}.svg"] = svg_text
        files[page.page_file(view)] = page.render_view_page(
            project_name, view, svg_text, view_ids, stems, placements, model=entries
        )
    files[report.REPORT_FILE] = report_text
    elements = page.elements_index([(view, entries) for view, _svg_text, entries in rendered])
    files[page.INDEX_PAGE] = page.render_index(project_name, views, report_text, elements)
    return files


def _unencodable(error: UnicodeEncodeError) -> str:
    """The refusal for model text UTF-8 cannot hold, naming the first such character and no location."""
    character = error.object[error.start] if isinstance(error.object, str) else ""
    code = f"U+{ord(character):04X}" if character else "a character"
    return (
        f"no diagram was written: a text to draw holds {code}, which UTF-8 cannot hold -- a lone surrogate. "
        "No model file can hold one (the model read refuses it), so it came through the Python API, "
        "in an element's text or the project name; remove it from that text."
    )


def refuse_xml_illegal_text(files: dict[str, str]) -> None:
    """The refusal of a document holding a character XML 1.0 excludes, naming the first one and no location.

    The plain-text report is skipped; its text is quoted in the index page, which is checked.
    """
    for name, content in files.items():
        if not name.endswith((".svg", ".html")):
            continue
        found = XML_ILLEGAL_RE.search(content)
        if found is not None:
            raise DiagramGenerationError(
                f"no diagram was written: the model's text holds U+{ord(found.group()):04X}, which no XML "
                "document holds -- a control character or a noncharacter, which a double-quoted YAML string "
                "can spell as an escape such as \\x01. `swcraft check-model` does not report it; remove the "
                "escape from the element's text."
            )


def _refuse_entries_in_the_way(root: Path, files: dict[str, str]) -> None:
    """The refusal of a link or a non-regular file in *root* whose name is a produced file's but for case.

    A link would be replaced, discarding what the user made; a directory would fail the run half-way.
    A hard link passes: the write replaces its name and never writes into it.
    """
    if not root.is_dir():
        return
    produced = {name.casefold() for name in files}
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        if entry.name.casefold() not in produced:
            continue
        if entry.is_symlink():
            raise DiagramGenerationError(
                f"no diagram was written: views_root holds {entry.name!r} as a link, and the file of that name would "
                "replace it. Remove the link."
            )
        if not entry.is_file():
            raise DiagramGenerationError(
                f"no diagram was written: views_root holds {entry.name!r}, which is not a regular file, where the "
                "generator writes one. Remove it."
            )


#: A link at the name is not followed and a FIFO does not block the open. Neither flag exists on
#: Windows, where a link at the name is read through, and only read.
_READ_FLAGS = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _open_without_following(path: str, flags: int) -> int:
    return os.open(path, flags | _READ_FLAGS)


def _unchanged(path: Path, content: str) -> bool:
    """Whether *path* is a regular file already holding exactly *content*; any failure to read answers no.

    The read uses the write's encoding and newline, so no translation makes different bytes equal.
    """
    try:
        with open(path, encoding="utf-8", newline="\n", opener=_open_without_following) as existing:
            if not stat.S_ISREG(os.fstat(existing.fileno()).st_mode):
                return False
            return existing.read() == content
    except (OSError, UnicodeDecodeError):
        return False


def _staging_path(target: Path) -> Path:
    """A hidden sibling of *target* of fixed length, of no name the generator removes or writes."""
    return target.with_name(f"{_STAGING_PREFIX}-{uuid.uuid4().hex}.tmp")


def _write_by_replacing(target: Path, content: str, case_variants: list[Path]) -> None:
    """*content* written to a new file beside *target*, then renamed over *target*'s name.

    The new file is created exclusively and ``os.replace`` renames over the name, so a link or a
    hard-linked name found there is replaced, never written through. *case_variants* are unlinked
    after the content is on disk, because a rename over a surviving case variant keeps the
    variant's spelling. A failure after the create removes the staged file and propagates;
    ``open_new`` cleans up its own failed write.
    """
    staged = _staging_path(target)
    with open_new(staged) as handle:
        handle.write(content)
    try:
        for variant in case_variants:
            variant.unlink()
        os.replace(staged, target)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def generate_diagrams(
    model_root: str, views_root: str, project_name: str, *, node: str | None = None
) -> DiagramsResult:
    """The model under *model_root* drawn into *views_root*, titled by *project_name*.

    *node* overrides the layout bridge's Node resolution. Raises ``ElementParseError`` for an
    unreadable model, :class:`DiagramGenerationError` for a refused view, and
    :class:`DiagramLayoutError` for a failed layout -- each before the first write.
    """
    extracted = graph.build_graphs(model_root)
    view_ids = graph.view_ids(extracted)
    _refuse_file_names(view_ids)

    try:
        started = time.perf_counter()
        laid_out = layout.layout(extracted.elk_graphs(), node=node)
        layout_seconds = time.perf_counter() - started
        files = _render_files(extracted, laid_out, view_ids, project_name)
        # Encode every file once so text no UTF-8 holds is refused before the first write.
        for content in files.values():
            content.encode("utf-8")
    except UnicodeEncodeError as error:
        raise DiagramGenerationError(_unencodable(error)) from error
    refuse_xml_illegal_text(files)
    root = Path(views_root)
    _refuse_entries_in_the_way(root, files)

    # Everything is in memory and checked: from here on, and only from here on, the directory is touched.
    root.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    kept: list[str] = []
    listed = [entry.name for entry in root.iterdir()]
    for name, content in files.items():
        target = root / name
        # A case variant is replaced too: where the two are one file, it would keep the old spelling.
        variants = [
            root / other
            for other in listed
            if other != name
            and other.casefold() == name.casefold()
            and (root / other).is_file()
            and not (root / other).is_symlink()
        ]
        if not variants and _unchanged(target, content):
            kept.append(name)
            continue
        _write_by_replacing(target, content, variants)
        written.append(name)

    produced = {name.casefold() for name in files}
    removed: list[str] = []
    for name in sorted(entry.name for entry in root.iterdir()):
        if GENERATED_FILE_RE.fullmatch(name) is None or name.casefold() in produced:
            continue
        entry = root / name
        if entry.is_symlink() or not entry.is_file():
            continue
        entry.unlink()
        removed.append(name)

    return DiagramsResult(
        views=tuple(view_ids),
        written=tuple(written),
        kept=tuple(kept),
        removed=tuple(removed),
        not_drawn=extracted.not_drawn,
        layout_seconds=layout_seconds,
    )
