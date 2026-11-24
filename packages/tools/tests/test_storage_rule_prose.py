"""The retired storage rule's own words, and the reading that finds them.

A model element's file name is derived from its identity and equals it only when nothing
collides (case-insensitively); a collision takes the nearest free ``~1``, ``~2``, … The old
rule -- *the filename is the element's name* -- is retired. This module holds its phrases,
:func:`stale_storage_sentences` and a self-test; the scan itself runs in the closed half of
this check.

The phrases are the old rule's own words, not a paraphrase detector: each is matched
case-insensitively with whitespace collapsed, so a rewrap is no escape, but other words pass.

**What this does not reach:** no test scans the open documents (README, guide, concept
documents, assistant-instruction copies, walkthrough pages), the decision records, the plans,
``docs/release-notes/``, code, docstrings, the contract's JSON documents or the protocol
schema's descriptions for the retired rule; nor the truth of the prose that replaced it.
"""

from __future__ import annotations

import re

#: The fragments of the retired rule, each as a live document spelled it.
STALE_STORAGE_PHRASES: tuple[str, ...] = (
    "the filename is the element's name",
    "the filename matches the element",
    "filename matches the element `name`",
    "the filename taken from the element's own name",
    "its name taken from the element's own identity",
    "because the file it lives in is named after it",
    "a file is named after the element in it",
    "identity and file name are not in bijection",
    "an identity and a file name are not in bijection",
    "the filename is the element's own",
    # The old assistant-instruction file's own sentence verbatim (line break collapsed), so a
    # copy-paste of that file fails by its own words and not only by the fragment above.
    "- Model YAML — one element per file, and the filename is the element's own identity field.",
)


def stale_storage_sentences(text: str) -> list[str]:
    """Every retired phrase *text* contains, in list order."""
    flat = re.sub(r"\s+", " ", text).lower()
    return [phrase for phrase in STALE_STORAGE_PHRASES if phrase.lower() in flat]


def test_the_scan_finds_a_retired_sentence_across_a_line_break() -> None:
    planted = "The rule: **one element per file, and the filename is the element's\nname.**"

    assert stale_storage_sentences(planted) == ["the filename is the element's name"]
    assert stale_storage_sentences("One file per element; a collision takes `~1` on the file alone.") == []
