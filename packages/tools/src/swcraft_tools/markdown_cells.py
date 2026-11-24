"""Safe Markdown for a model text in a table: a cell's text, a name's code span and a table.

A model text in a table cell has ``|`` escaped, a line break turned into a space and ``<``/``&``
spelled as entities, so the document holds no raw HTML and no text ends its cell; a name is set in a
code span whose fence is one backtick longer than the longest run of backticks it holds.
:func:`prose_code_span` is the code span for a name in a paragraph; :func:`inline_literal`,
:func:`paragraph_start` and :func:`heading_text` make a text literal in a paragraph, a list item or a
heading; :func:`code_fence` sets lines to keep as they are in a fenced code block.

A leaf module: it imports nothing of ``swcraft_tools``, so a report that renders tables loads no
other report's module.

**What it does not reach.** Markdown outside a table cell: a heading or a paragraph that carries a
model text is the caller's to keep on one line. A name holding a backslash right before a ``|`` keeps
its table intact, but is shown with that run of backslashes doubled: a code span processes no escape,
and cmark-gfm reads ``\\|`` in a row as an escaped backslash and a bare pipe where markdown-it (the VS
Code preview's) reads an escaped pipe, so only a doubled run escapes the pipe in both. markdown-it was
measured; cmark-gfm's reading is its source's, not run here; any other renderer is not reached.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

__all__ = [
    "cell_text",
    "code_fence",
    "code_span",
    "heading_text",
    "inline_literal",
    "paragraph_start",
    "prose_code_span",
    "table",
]


def cell_text(value: str) -> str:
    """A model text for a table cell: no raw HTML, no ``|`` that ends the cell, no line break.

    A backslash is doubled first, so a text holding ``\\|`` cannot turn the cell's escaped pipe
    into an escaped backslash and a bare pipe that ends the cell. A code span (:func:`code_span`)
    processes no backslash escape, so there only a run right before a pipe is doubled.
    """
    escaped = value.replace("\\", "\\\\").replace("&", "&amp;").replace("<", "&lt;").replace("|", "\\|")
    return " ".join(escaped.splitlines()) if ("\n" in escaped or "\r" in escaped) else escaped


def _fenced(flat: str) -> str:
    """*flat* in a code span whose fence is longer than any run of backticks in it, padded when it holds one."""
    longest = max((len(run) for run in re.findall(r"`+", flat)), default=0)
    if longest == 0:
        return f"`{flat}`"
    fence = "`" * (longest + 1)
    return f"{fence} {flat} {fence}"


def code_span(name: str) -> str:
    """A name in a code span (see :func:`_fenced`); ``|`` escaped and line breaks as in a cell.

    A run of backslashes right before a ``|`` is doubled, so it cannot turn the escaped pipe into an
    escaped backslash and a bare pipe that ends the cell.
    """
    flat = " ".join(name.splitlines())
    return _fenced(re.sub(r"(\\*)\|", lambda match: match.group(1) * 2 + "\\|", flat))


def prose_code_span(name: str) -> str:
    """:func:`code_span` for a paragraph: line breaks as in a cell, ``|`` kept as it is.

    GitHub Flavored Markdown removes a ``\\|`` escape inside a table row only; in a
    paragraph's code span it would be printed, so a paragraph takes this one.
    """
    return _fenced(" ".join(name.splitlines()))


def _row(*cells: str) -> str:
    """One table row, the cells given already escaped."""
    return "| " + " | ".join(cells) + " |"


def table(header: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> list[str]:
    """A table's lines: the header, the delimiter row and one line per row."""
    return [_row(*header), _row(*("---" for _ in header)), *(_row(*row) for row in rows)]


#: A paragraph's first characters that would open a heading, a list, a quote, a fence or a rule.
_LEADING_BLOCK_MARKER = re.compile(r"^(?:[#>+*\-_`~=]|\d{1,9}(?=[.)]))")
#: The characters that open an inline construct (a link, an image, emphasis, a code span,
#: strikethrough) and the four a bare URL or address needs to be linkified (``:``, ``.``, ``/``,
#: ``@``): an escaped one leaves markdown-it's linkify pass no scheme, domain or address to link.
_INLINE_MARKERS = re.compile(r"([\[\]`*_~:./@])")


def inline_literal(value: str) -> str:
    """*value* as literal text in a cell or a paragraph: :func:`cell_text`, then every inline marker escaped.

    A message quotes model-supplied names (``**X**``, ``example.com``), which would otherwise render
    as emphasis or a link; its own backticks are escaped too, so they read as the CLI prints them.
    What it does not reach: another renderer's autolinker that reads the text after its escapes are
    resolved.
    """
    return _INLINE_MARKERS.sub(r"\\\1", cell_text(value))


def paragraph_start(value: str) -> str:
    """*value* made safe as a paragraph's literal first words: a cell's text, inline and block markers escaped.

    Leading blanks are dropped (four would open a code block); a leading ``#``, ``>``, ``+``, ``-``
    or ``=`` gets a backslash, and a leading number's ``)`` does (its ``.`` already has one), so the
    text stays a paragraph. A bare URL, domain or e-mail address stays text in markdown-it 14 with
    ``linkify`` on, the VS Code preview's renderer. What it does not reach: another renderer's
    autolinker that reads the text after its escapes are resolved.
    """
    text = inline_literal(value).lstrip()
    match = _LEADING_BLOCK_MARKER.match(text)
    if match is None:
        return text
    if match.group(0)[0].isdigit():
        end = match.end()
        return f"{text[:end]}\\{text[end:]}"
    return f"\\{text}"


def heading_text(value: str) -> str:
    """*value* as a heading's literal text: :func:`inline_literal`, with every ``#`` escaped too.

    A heading's trailing run of ``#`` is its closing sequence and would be dropped, so a value
    ending in one keeps it only escaped.
    """
    return inline_literal(value).replace("#", "\\#")


def code_fence(lines: list[str]) -> list[str]:
    """*lines* in a fenced code block whose fence no backtick run of them can close.

    The fence is at least three backticks and one longer than the longest run in any line, so
    the lines are kept as they are.
    """
    longest = max((len(run) for line in lines for run in re.findall(r"`+", line)), default=0)
    fence = "`" * max(3, longest + 1)
    return [fence, *lines, fence]
