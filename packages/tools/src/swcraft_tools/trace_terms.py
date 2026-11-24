"""The terms of a text, shared by the trace link search and the requirements import's report.

A leaf: it imports nothing from :mod:`swcraft_tools`, so ``swcraft import requirements`` names terms exactly as
the search counts them without loading the search.

- :func:`tokens` and :data:`STOP_WORDS` are the search's tokeniser;
- :func:`words` is the same split with nothing dropped, so a ``not`` or a ``shall`` that moved is named;
- :func:`is_reported_term` is which of those words the import's report names when it moved;
- :func:`numbers` is the numbers a text writes, as written.

**What it does not reach:** no stemming and no lemmatisation; a decimal comma is not a decimal mark (``1,5`` is
two numbers, ``1`` and ``5``); a number glued to a preceding letter is not a number (``SR2`` gives none), though a
decimal part after it is (``SR2.5`` gives ``5``); a dotted run of three or more parts gives its pieces (``1.5.3``
gives ``1.5`` and ``3``); an exponent, a thousands separator and a unit are not read (``10ms`` gives ``10``).
"""

from __future__ import annotations

import re
from collections.abc import Iterator

#: The stop words, requirement boilerplate included: dropped from every bag and every query, never from the
#: exact name match.
STOP_WORDS: frozenset[str] = frozenset(
    (
        "all also an and any are as at be been being both but by can could each every for from has have if "
        "in into is it its may might must no not of on one only or other provide provided provides same "
        "shall should so such than that the their then there these they this those to under upon use used "
        "uses using was were when where whether which while who will with within without would"
    ).split()
)

#: The stop words the import's report still names when they moved: a negation, a modal or ``only`` changes what
#: a requirement says (``shall`` -> ``shall not``, ``shall`` -> ``may``).
REPORTED_STOP_WORDS: frozenset[str] = frozenset(
    "can could may might must no not only shall should will without would".split()
)
#: The shortest token kept: a single letter or digit is dropped.
MIN_TOKEN_LENGTH = 2

_LETTER_DIGIT_RUNS = re.compile(r"[^\W_]+")


def _split_run(run: str) -> Iterator[str]:
    """*run* (letters and digits only) split at every lower-to-upper and letter-digit boundary."""
    start = 0
    for index in range(1, len(run)):
        previous, current = run[index - 1], run[index]
        if (previous.islower() and current.isupper()) or (previous.isdigit() != current.isdigit()):
            yield run[start:index]
            start = index
    yield run[start:]


def tokens(text: str) -> list[str]:
    """The terms of *text*, in order and with repeats: split, lower-cased, short tokens and stop words dropped.

    ``BrakeTemp2Sensor`` -> ``brake``, ``temp``, ``sensor`` (the ``2`` is too
    short). No stemming and no lemmatisation.
    """
    terms: list[str] = []
    for run in _LETTER_DIGIT_RUNS.findall(text):
        for part in _split_run(run):
            term = part.lower()
            if len(term) >= MIN_TOKEN_LENGTH and term not in STOP_WORDS:
                terms.append(term)
    return terms


def words(text: str) -> list[str]:
    """Every word of *text*, in order and with repeats: :func:`tokens`' split, lower-cased, nothing dropped.

    ``The brake shall not be applied`` -> ``the``, ``brake``, ``shall``,
    ``not``, ``be``, ``applied``; ``SR600`` -> ``sr``, ``600``.
    """
    return [part.lower() for run in _LETTER_DIGIT_RUNS.findall(text) for part in _split_run(run)]


def is_reported_term(word: str) -> bool:
    """Whether the import's report names *word*, one of :func:`words`, when its count moved.

    Every word is, but a stop word outside :data:`REPORTED_STOP_WORDS` and a single letter: ``not``, ``shall``
    and ``most`` are named, ``at``, ``for`` and ``s`` are not. A single digit is named (``SR2`` -> ``SR3`` is a
    change); a contraction splits (``can't`` gives ``can`` and an unnamed ``t``).
    """
    if word in STOP_WORDS:
        return word in REPORTED_STOP_WORDS
    return not (len(word) == 1 and word.isalpha())


#: A number: a maximal run of digits with at most
#: one ``.digits`` part, not preceded by a letter, a digit or ``_``; a leading
#: ``-`` belongs to it when that ``-`` is preceded by a non-word character or
#: the text's start.
_NUMBER = re.compile(r"(?:(?<!\w)-)?(?<!\w)\d+(?:\.\d+)?")


def numbers(text: str) -> list[str]:
    """The numbers *text* writes, in order and with repeats, each spelled as written.

    ``600``, ``-40`` and ``1.5`` are kept as they are; ``a-40`` gives ``40``,
    ``10ms`` gives ``10``, ``SR2`` gives none, ``SR2.5`` gives ``5``, ``1.5.3``
    gives ``1.5`` and ``3``, and ``1,5`` gives ``1`` and ``5``.
    """
    return _NUMBER.findall(text)
