"""The AUTOSAR ``Identifier`` rule every ARXML-bound model name must satisfy.

Both licensed schemas constrain a ``SHORT-NAME`` — and every ``*-REF`` path
segment — to ``[a-zA-Z][a-zA-Z0-9_]*`` with a maximum of 128 characters
(``IDENTIFIER--SIMPLE``). The validator reports every offender and the writer
refuses before writing, so this leaf imports nothing from ``swcraft_tools``.

**The pattern is stored unanchored and matched with** ``fullmatch()``: it is the
schema's own string (XSD patterns are implicitly whole-value), and
``re.match(r"^…$")`` would let a trailing newline through (``"A\\n"``). It is
distinct from ``schema.py``'s ``_SEQUENCE_NAME_RE``; the two must not be unified.

**Diagnostics only.** :func:`check_identifier` returns a reason, never raises and
never rewrites a name.

**A second, weaker rule: the C identifier.** A runnable's stated ``symbol`` is
AUTOSAR's ``RunnableEntity.symbol``, typed by the ``CIdentifier`` primitive, whose
class table gives it the pattern ``[a-zA-Z_][a-zA-Z0-9_]*`` and no length (GST
R4.2.2 Table 4.31, ``:3203-3218``; R4.3.0 Table 4.49, ``:3910-3925``), and both
licensed schemas carry that pattern as ``C-IDENTIFIER--SIMPLE`` (4.2.2 ``:8295``,
4.3.0 ``:9281``). It is stored and matched as the Identifier's is. Every
Identifier is a C identifier, so a runnable's name -- what the writer writes when
no symbol is stated -- always passes it.
"""

from __future__ import annotations

import re

#: The ``IDENTIFIER--SIMPLE`` ``pattern`` facet, verbatim from both licensed
#: schemas (4.2.2 and 4.3.0 agree), unanchored.
IDENTIFIER_PATTERN = "[a-zA-Z][a-zA-Z0-9_]*"

#: :data:`IDENTIFIER_PATTERN`, compiled. Use it with ``fullmatch()`` — a bare
#: ``match()`` accepts any name with a conforming *prefix* (``"A B"``).
IDENTIFIER_RE = re.compile(IDENTIFIER_PATTERN)

#: The ``IDENTIFIER--SIMPLE`` ``maxLength`` facet, in characters of the name
#: actually **emitted** (a generated form can exceed it).
MAX_IDENTIFIER_LENGTH = 128

#: The reason a name fails the pattern half of the rule.
REASON_PATTERN = (
    f"must be an AUTOSAR Identifier matching {IDENTIFIER_PATTERN} "
    "(a letter first, then letters, digits or underscores only)"
)

#: The ``C-IDENTIFIER--SIMPLE`` ``pattern`` facet, verbatim from both licensed
#: schemas (4.2.2 and 4.3.0 agree), and the ``CIdentifier`` primitive's
#: ``xml.xsd.pattern`` tag, unanchored. The type declares no ``maxLength``.
C_IDENTIFIER_PATTERN = "[a-zA-Z_][a-zA-Z0-9_]*"

#: :data:`C_IDENTIFIER_PATTERN`, compiled. Use it with ``fullmatch()``.
C_IDENTIFIER_RE = re.compile(C_IDENTIFIER_PATTERN)

#: The reason a text fails the C identifier rule.
REASON_C_IDENTIFIER = (
    f"must be a C identifier matching {C_IDENTIFIER_PATTERN} "
    "(a letter or an underscore first, then letters, digits or underscores only)"
)

#: The reason a name fails the length half; the advice differs from the pattern's.
REASON_LENGTH = f"must be at most {MAX_IDENTIFIER_LENGTH} characters"


def check_identifier(name: str) -> str | None:
    """Return why *name* is not a legal AUTOSAR ``Identifier``, or ``None``.

    Args:
        name: the name as it will be **emitted** (a generated form such as
            ``{swc}_InternalBehavior`` in its final shape).

    Returns:
        :data:`REASON_PATTERN` or :data:`REASON_LENGTH` when *name* is not
        conforming, and ``None`` when it is. The pattern failure wins when a name
        fails both.
    """
    if not IDENTIFIER_RE.fullmatch(name):
        return REASON_PATTERN
    if len(name) > MAX_IDENTIFIER_LENGTH:
        return REASON_LENGTH
    return None


def check_c_identifier(text: str) -> str | None:
    """Return why *text* is not a C identifier, or ``None``.

    *text* (a runnable's stated ``symbol``) is judged **as written**: nothing is
    stripped first, and an empty text fails.
    """
    if not C_IDENTIFIER_RE.fullmatch(text):
        return REASON_C_IDENTIFIER
    return None


def short_name_key(name: str) -> str:
    """Return the key two ``SHORT-NAME`` s are compared by for uniqueness: ``name.casefold()``.

    ``[constr_2508]`` requires a ``SHORT-NAME`` to be unique **ignoring case** within
    its name space (GST R4.2.2 ``:2635``, R4.3.0 ``:2711``; ``[TPS_GST_00021]``, R4.2.2
    ``:2624``: *"checked for uniqueness as not case sensitive"*). R22-11 defines *case
    insensitive* over the two ASCII letter sets (GST R22-11 ``:2989–2997``), and over the Identifier grammar
    :data:`IDENTIFIER_PATTERN` ``casefold()`` is exactly that definition.

    Every name-space comparison for ``[constr_2508]`` folds through this
    function. What this does **not** reach: only the validator's name-space and
    runnable-event comparisons are held to it by an AST check
    (``TestRunnableEventCollisionsOnTheKey``); other case folds (storage and schema
    file names, the root-package-name comparison) are not this function's.

    Args:
        name: a ``SHORT-NAME`` as it is (or will be) emitted.

    Returns:
        The comparison key; two names are one name to AUTOSAR exactly when equal.
    """
    return name.casefold()
