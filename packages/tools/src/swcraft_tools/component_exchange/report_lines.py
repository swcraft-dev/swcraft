"""The component import report's line primitives: a sentence, a headed list, and the regime word ``none``.

A leaf shared by ``report`` and ``capabilities``, so ``capabilities`` never imports ``report``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..capability_markers import ACCESS_POINTS
from ..markdown_cells import code_fence, heading_text, paragraph_start

#: The regime an import report names for a capability family when nothing decided anything
#: (a created, unchanged or conflicting component, or an update that keeps the project's
#: behaviour). Defined here so the report's field defaults can name it; ``capabilities.NONE``
#: is this object.
NONE = "none"


#: What an unrecognized marker says in a rendered import report. The export is treated as
#: unmarked (a value SWCraft did not write is no evidence about a runnable's access lists); the
#: line tells the reader why a marked-looking export behaved like an unmarked one.
def unrecognized_marker_sentence(value: str) -> str:
    return (
        f"{ACCESS_POINTS.sd_gid} marker present with value {value!r} -- not {ACCESS_POINTS.text!r}; "
        "the export is treated as unmarked"
    )


@dataclass(frozen=True)
class ReportLine:
    """One sentence of an import report: a line of the text form, a paragraph of the Markdown.

    *strong* sets it in bold in the Markdown (the component's line) and changes nothing in the
    text form.
    """

    text: str
    strong: bool = False

    def text_lines(self) -> list[str]:
        return [self.text]

    def markdown_lines(self) -> list[str]:
        text = paragraph_start(self.text)
        return [f"**{text}**" if self.strong else text]


@dataclass(frozen=True)
class ReportList:
    """A heading over its items.

    The text form writes its heading line and ``  - <item>`` lines; the Markdown, a ``##``
    heading (its trailing colon dropped) and a list.

    *verbatim* items are lines to keep as they are -- the update diff preview: indented by two
    in the text form, a fenced code block (a fence no backtick run of them can close) in the
    Markdown. A list with no item is its heading alone in both.
    """

    heading: str
    items: list[str]
    verbatim: bool = False

    def text_lines(self) -> list[str]:
        prefix = "  " if self.verbatim else "  - "
        return [self.heading, *(f"{prefix}{item}" for item in self.items)]

    def markdown_lines(self) -> list[str]:
        lines = [f"## {heading_text(self.heading.removesuffix(':'))}"]
        if not self.items:
            return lines
        if self.verbatim:
            return [*lines, "", *code_fence(self.items)]
        return [*lines, "", *(f"- {paragraph_start(item)}" for item in self.items)]


def counted(by_runnable: Mapping[str, Mapping[str, int]]) -> list[str]:
    """``<runnable>: <name>=<count>, ...`` per runnable, sorted by runnable: a preserved-counts list's items."""
    return [
        f"{runnable}: " + ", ".join(f"{name}={count}" for name, count in counts.items())
        for runnable, counts in sorted(by_runnable.items())
    ]
