"""Which characters an XML 1.0 document cannot carry, decided once.

XML 1.0's ``Char`` production (§ 2.2) admits tab, line feed, carriage return and the ranges ``#x20-#xD7FF``,
``#xE000-#xFFFD`` and ``#x10000-#x10FFFF`` -- nothing else, and no escape spells anything else either: a
character reference must name a ``Char`` too (the *Legal Character* well-formedness constraint). So a model
text holding a NUL, any other C0 control, a lone surrogate, ``U+FFFE`` or ``U+FFFF`` cannot be written as held,
and since SWCraft never alters a text, it reports it (``rule:text.xml-unrepresentable-character``) and refuses
to write it.

A leaf: the validator reports and the writer refuses by the same rule, and the validator must stay importable
without the writer, so this module imports nothing from ``swcraft_tools``.

What it does **not** decide: which model fields are free text a document carries, and anything about XML 1.1,
which SWCraft never writes.
"""

from __future__ import annotations

import re

#: Every character outside XML 1.0's ``Char`` production; a ``str`` can hold a lone surrogate, so the
#: surrogate block is outside the class too.
_UNREPRESENTABLE = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def xml_unrepresentable_character(text: str) -> str | None:
    """The first character of *text* that XML 1.0 cannot carry, or ``None`` when there is none."""
    found = _UNREPRESENTABLE.search(text)
    return None if found is None else found.group()


def code_point(character: str) -> str:
    """*character* spelled as its code point, ``U+0007``: the form a message can print for any character."""
    return f"U+{ord(character):04X}"


def unrepresentable_text_sentence(subject: str, character: str) -> str:
    """The one sentence said of a text XML 1.0 cannot carry: *subject* ``holds`` *character*, and what to do.

    *subject* names the text in the speaker's own terms; everything after it is the same words, so the same
    fault never reads as two.
    """
    return (
        f"{subject} holds {code_point(character)}, a character XML 1.0 cannot carry (a NUL, a control "
        "character other than tab, line feed and carriage return, a lone surrogate, U+FFFE or U+FFFF). SWCraft "
        "writes a text exactly as held and never alters it; remove the character from the model."
    )
