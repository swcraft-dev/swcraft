"""The diagram report: ``report.txt``, what the views are and what they did not draw.

Nothing the model declares is left out of a view in silence: every item the extractor could not
place is a ``graph.NotDrawn`` record, printed grouped by kind (kinds alphabetical, records in reader
order). A self-connector drawn as a loop (:data:`LOOP_KIND`) is a section of its own. Before them,
every view with its pixel size and its size in UTF-8 bytes.

No timing is written, so two runs over one model write the same bytes and an unchanged
regeneration leaves the file untouched.

What it does not reach: whether the records are complete (the extractor's contract), and a view
whose SVG root carries no width or height, printed as ``?``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Protocol

#: The file the report is written to, beside the pages in ``views_root``.
REPORT_FILE = "report.txt"

#: The kind of the one record that names something drawn unusually rather than not drawn.
LOOP_KIND = "loop"

_ROOT = re.compile(r"<svg\b([^>]*)>")


class NotDrawnLike(Protocol):
    """What the report reads of one ``graph.NotDrawn``."""

    @property
    def view(self) -> str | None: ...
    @property
    def kind(self) -> str: ...
    @property
    def subject(self) -> str: ...
    @property
    def detail(self) -> str: ...


def _attribute(attributes: str, name: str) -> str:
    match = re.search(rf'\s{name}="([^"]*)"', attributes)
    return match.group(1) if match else "?"


def _size(svg_text: str) -> str:
    root = _ROOT.search(svg_text)
    attributes = root.group(1) if root else ""
    width, height = _attribute(attributes, "width"), _attribute(attributes, "height")
    return f"{width} x {height} px  {len(svg_text.encode('utf-8'))} bytes"


def _item(record: NotDrawnLike) -> str:
    view = record.view if record.view is not None else "(no view)"
    return f"{view}  {record.subject}: {record.detail}" if record.subject else f"{view}  {record.detail}"


def render_report(project_name: str, views: Sequence[tuple[str, str]], not_drawn: Sequence[NotDrawnLike]) -> str:
    """The report's text: *views* (id and SVG text) with their sizes, then *not_drawn* grouped."""
    lines = [f"Diagrams report: {project_name}", "", f"Views: {len(views)}"]
    width = max((len(view) for view, _ in views), default=0)
    lines.extend(f"  {view.ljust(width)}  {_size(svg_text)}" for view, svg_text in views)

    missing = [record for record in not_drawn if record.kind != LOOP_KIND]
    by_kind: dict[str, list[NotDrawnLike]] = {}
    for record in missing:
        by_kind.setdefault(record.kind, []).append(record)
    lines.append("")
    if missing:
        noun = "kind" if len(by_kind) == 1 else "kinds"
        lines.append(f"Not drawn: {len(missing)} in {len(by_kind)} {noun}")
        for kind in sorted(by_kind):
            lines.append(f"  {kind}: {len(by_kind[kind])}")
            lines.extend(f"    {_item(record)}" for record in by_kind[kind])
    else:
        lines.append("Not drawn: none")

    loops = [record for record in not_drawn if record.kind == LOOP_KIND]
    lines.append("")
    lines.append(f"Drawn as a loop: {len(loops)}" if loops else "Drawn as a loop: none")
    lines.extend(f"  {_item(record)}" for record in loops)
    return "\n".join(lines) + "\n"
