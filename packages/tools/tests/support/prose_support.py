"""Cutting Markdown prose into blocks, for the documentation checks.

A documentation check cuts a checklist into its items with
:func:`normalized_blocks` and :data:`CHECKLIST_ITEM`, and an inventory into its
bullets with :data:`BULLET`. The cut shapes are named here once, so two scans of
one document read it the same way.

**Why the second cut exists at all.** Markdown bullets are newline-separated, so
a blank-line-only split makes a whole list one block -- and a condition named in
a neighbouring bullet then exempts a claim three bullets away. Every caller that
scans a list needs the finer cut; the cut it needs depends on how its documents
write a list item, which is why the shapes are named constants here and chosen
by the caller rather than applied to everyone.

What this module does **not** reach: it is a text splitter, not a Markdown
parser. A fenced code block, a table and an indented continuation line are all
just text; a bullet inside a code fence starts a block; and normalisation
lower-cases and collapses whitespace, so nothing downstream can assert about
case or layout. A caller needing more than that needs a parser.
"""

from __future__ import annotations

import re

#: A blank line, tolerating trailing whitespace on it.
BLANK_LINE = r"\n\s*\n"

#: A Markdown bullet at the start of a line (``- `` / ``* ``).
BULLET = r"\n(?=\s*[-*] )"

#: A checklist item, ``- [ ]`` / ``- [x]``.
CHECKLIST_ITEM = r"\n(?=\s*- \[)"


def normalized_blocks(
    text: str,
    *cuts: str,
    separator: str = BLANK_LINE,
    drop: str = "",
) -> list[str]:
    """*text* as blocks, lower-cased and whitespace-collapsed.

    Split on *separator* first, then once more at each pattern in *cuts*, in the
    order given -- so ``normalized_blocks(text, BULLET, CHECKLIST_ITEM)`` cuts
    every bullet out of every paragraph and then every checklist item out of what
    is left.

    *drop* is a set of characters removed before normalisation, for the callers
    whose documents write the same word as ``` `pin` ``` and as ``**pin**``.
    """
    chunks = re.split(separator, text)
    for cut in cuts:
        chunks = [part for chunk in chunks for part in re.split(cut, chunk)]
    if drop:
        table = str.maketrans("", "", drop)
        chunks = [chunk.translate(table) for chunk in chunks]
    return [" ".join(chunk.lower().split()) for chunk in chunks]
