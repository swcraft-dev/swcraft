"""The ASIL values and their comparison order — by ``X``, ``QM < A < B < C < D``.

An automotive safety integrity level is written in one of **eighteen**
spellings on the four *rated* fields (a requirement's, a component's, a data
element's and a partition's ``asil``) and one of **five** on the two
*capability* fields (a core's and a memory's ``asil_capability``, which are not
decomposed). The eighteen are the five plain levels and the thirteen decomposed
spellings ``X(Y)`` of AUTOSAR's Safety Extensions (:data:`ASIL_VALUES`), where
``Y`` — the value in parentheses — is the contextual ASIL before decomposition.
Every rule *ranks* by ``X`` alone: ``B`` and ``B(D)`` rank alike, ``QM(D)``
ranks with ``QM``. A rule that compares ``(Y)`` reads it through :func:`parse_asil`.

**What this module is not.** It does not own the *membership* of either value
set (the model contract's ``enum`` does, and ``schema.py`` refuses any other
value at load time); it repeats the spellings and holds the **order**. Nor does
it judge a decomposition: whether ``B(D)`` + ``B(D)`` realise one ``D``
requirement is evidence outside the model. ``QM`` is not an ASIL (ISO
26262-1:2018 3.6 Note 1): rank 0, :data:`NO_ASIL`, means "no ASIL is allocated
here", never "the lowest one". The levels are spelled in **capitals**.

A leaf importing nothing from ``swcraft_tools``: the validator and the editing
guard, on opposite sides of an existing dependency edge, both read it.
"""

from __future__ import annotations

from collections.abc import Iterable

#: The five plain levels, in **increasing** order: the capability fields'
#: whole vocabulary, and the spine of the order (a value ranks where its ``X``
#: stands here).
ASIL_LEVELS: tuple[str, ...] = ("QM", "A", "B", "C", "D")

#: The eighteen values a **rated** field (``asil`` on a requirement, a
#: component, a data element and a partition) may hold, in the standard's own
#: listing order — which is not the rank order; :func:`asil_rank` is.
#:
#: Transcribed from ``[TPS_SAFEX_00201]``, AUTOSAR_TPS_SafetyExtensions
#: (Document Identification No 671), Standard Release **R21-11**, § 5 *Safety
#: Integrity Levels*, p. 18 — the edition whose cover says *Document Status
#: obsolete*; the document is absent from R22-11 onward, so R21-11 is the last
#: text there is. p. 19 states that the value in parentheses is "the contextual
#: ASIL before decomposition", and ``[TPS_SAFEX_00202]`` gives the same values
#: to a safety-relevant element.
#:
#: **One spelling is left out on purpose — the standard's, not this module's:**
#: ``A(A)``. Of the fourteen ``X(Y)`` forms with ``X <= Y`` the list names
#: ``B(B)``, ``C(C)`` and ``D(D)`` but not ``A(A)``, so ``A(A)`` is not a value.
ASIL_VALUES: tuple[str, ...] = (
    "QM",
    "A",
    "B",
    "C",
    "D",
    "QM(A)",
    "QM(B)",
    "QM(C)",
    "QM(D)",
    "A(B)",
    "A(C)",
    "A(D)",
    "B(B)",
    "B(C)",
    "B(D)",
    "C(C)",
    "C(D)",
    "D(D)",
)

#: The value that allocates nothing: the bottom of :data:`ASIL_LEVELS`, rank 0,
#: with nothing below it. Deliberately **not** an ASIL (ISO 26262-1:2018 3.6
#: Note 1), so a bearer whose effective level is this one has none to withdraw
#: and a declaration compared against it can never come out below.
NO_ASIL: str = ASIL_LEVELS[0]


#: Each value's rank, taken once: :func:`asil_rank` runs per element in hot loops.
_RANKS: dict[str, int] = {value: ASIL_LEVELS.index(value.partition("(")[0]) for value in ASIL_VALUES}


def _refusal(value: str) -> ValueError:
    """The one refusal of a value outside :data:`ASIL_VALUES`, naming it."""
    return ValueError(f"{value!r} is not one of the ASIL values {', '.join(ASIL_VALUES)}")


def parse_asil(value: str) -> tuple[str, str | None]:
    """Split ``value`` into ``(X, Y)`` — ``Y`` is ``None`` for a plain level.

    ``"B(D)"`` is ``("B", "D")``, ``"QM(A)"`` is ``("QM", "A")`` and ``"C"`` is
    ``("C", None)``.

    **Raises** ``ValueError`` naming the value for anything outside
    :data:`ASIL_VALUES` — ``A(A)``, ``A(E)``, ``a(b)``, ``B (D)``: the loader
    refuses such a value, so reaching this function with one is a defect.
    """
    if value not in _RANKS:
        raise _refusal(value)
    level, _, context = value.partition("(")
    return level, (context[:-1] if context else None)


def asil_rank(level: str) -> int:
    """Return the position of ``level``'s ``X`` in :data:`ASIL_LEVELS` — ``QM`` is ``0``.

    A decomposed value ranks with its ``X``: ``B(D)`` is ``2``, like ``B``, and
    ``QM(D)`` is ``0``, like ``QM``. The ``(Y)`` never moves a rank.

    **Raises** ``ValueError`` for anything outside :data:`ASIL_VALUES`; a total
    function would silently rank a typo below ``QM``.
    """
    try:
        return _RANKS[level]
    except KeyError:
        raise _refusal(level) from None


def _max_key(value: str) -> tuple[int, bool, int]:
    """The tie rule as a key: ``X`` first, then plain over decomposed, then ``Y``."""
    rank = asil_rank(value)
    _, context = parse_asil(value)
    return (rank, context is None, ASIL_LEVELS.index(context) if context else rank)


def max_asil(levels: Iterable[str]) -> str:
    """Return the highest of ``levels`` by ``X``, carrying its ``(Y)``; ``"QM"`` if empty.

    The empty answer ``"QM"`` (rank 0) makes every "declared below the maximum"
    comparison come out false without a special case.

    **The tie rule**, among values with one ``X``: the **plain** spelling wins,
    and among decomposed spellings the **highest** ``Y`` wins (the conservative
    context) — so ``B`` beside ``B(D)`` answers ``B`` and ``B(C)`` beside
    ``B(D)`` answers ``B(D)``. The rule reads the values alone, never their
    order in the input. This is **not** ``max()`` over the raw strings:
    ``"QM"`` sorts lexicographically above ``"D"``.
    """
    return max(levels, key=_max_key, default=NO_ASIL)
