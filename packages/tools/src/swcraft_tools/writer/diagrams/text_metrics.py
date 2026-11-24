"""The one estimate of a label's width, shared by the extractor and the renderers.

``graph.py`` sizes every label box ELK lays out with it and ``svg.py`` (through ``text_width``)
every box Python lays out. It is one leaf module because both sides must agree to the hundredth of
a pixel and neither may import the other.

The estimate is per character class (the constants below, Helvetica's proportions): a flat
0.6 em undersized long all-capitals names enough to overrun the port square beside them. The sum
is kept in integer thousandths of an em so it does not depend on the order floats are added in.

What it does not reach: real text metrics. A different font or an unusually wide character still
lands off its box.
"""

from __future__ import annotations

#: The width of a capital letter, in thousandths of the font size.
CAPITAL_WIDTH = 670
#: The width of a lower-case letter, a digit and ``_``, in thousandths of the font size.
NARROW_WIDTH = 550
#: The width of every other character, in thousandths of the font size.
OTHER_WIDTH = 600
#: The padding around a label, two pixels a side.
PADDING = 4


def character_width(character: str) -> int:
    """*character*'s estimated width in thousandths of the font size."""
    if character.isupper():
        return CAPITAL_WIDTH
    if character.islower() or character.isdigit() or character == "_":
        return NARROW_WIDTH
    return OTHER_WIDTH


def text_width(text: str, font_size: float) -> float:
    """The estimated width of *text* at *font_size*, two pixels a side, rounded to two decimals."""
    thousandths = sum(character_width(character) for character in text)
    return round(thousandths * font_size / 1000 + PADDING, 2)
