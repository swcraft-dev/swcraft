"""What a rename of a whole element is: one definition, for the safety verdict and for staging.

A rename is a client's ``remove X`` and ``create Y`` in one change set;
:func:`element_rename_pairs` says which pairs are one. The rule:

* X and Y are in the same **layer**, and their **published** values (placement
  ``group`` included, so a ``group`` change is not a rename) are equal once the
  identity fields are removed (:func:`identity_stripped`). The comparison is a
  JSON rendering, so it is type-strict: ``false`` and ``0``, ``1`` and ``1.0``,
  ``0.0`` and ``-0.0`` differ.
* The pairing is **unique** for that content within the change set
  (:func:`unique_pairing`). Where two or more identical elements are removed and
  created together, none is a rename (the fail-safe direction).

A rename bundled with any edit is **not** a rename. A leaf: it imports nothing of
``swcraft_tools``.
"""

from __future__ import annotations

import json
from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

__all__ = [
    "IDENTITY_FIELDS",
    "AmbiguousRename",
    "ElementRenames",
    "element_rename_pairs",
    "identity_stripped",
    "unique_pairing",
]

#: The fields a rename may differ in. A fixed pair rather than resolved per layer
#: because the same test serves a collection member, whose identity is its own
#: ``name`` and which no layer declaration names.
IDENTITY_FIELDS: frozenset[str] = frozenset({"name", "id"})

_Shape = TypeVar("_Shape", bound=Hashable)


def identity_stripped(value: Any) -> str | None:
    """*value* rendered without its identity, or ``None`` where it cannot be one.

    A stable string, so two published values compare by a dictionary lookup.
    ``default=str`` lets an exotic value compare as its text rather than raise
    while the guard renders an approval prompt.
    """
    if not isinstance(value, Mapping):
        return None
    stripped = {key: held for key, held in value.items() if key not in IDENTITY_FIELDS}
    return json.dumps(stripped, sort_keys=True, default=str)


def unique_pairing(
    removed: Mapping[_Shape, Sequence[str]], created: Mapping[_Shape, Sequence[str]]
) -> tuple[list[tuple[_Shape, str, str]], list[tuple[_Shape, tuple[str, ...], tuple[str, ...]]]]:
    """The pairs one content shape makes, and the shapes whose pairing is not unique.

    Args:
        removed: content shape -> the names removed under it, in delta order.
        created: content shape -> the names created under it, in delta order.

    Returns:
        ``(pairs, ambiguities)``: each pair ``(shape, was, now)``, each ambiguity
        ``(shape, removals, creations)``, both in *created*'s order. A shape with
        no removal is neither.
    """
    pairs: list[tuple[_Shape, str, str]] = []
    ambiguities: list[tuple[_Shape, tuple[str, ...], tuple[str, ...]]] = []
    for shape, creations in created.items():
        removals = removed.get(shape, ())
        if not removals:
            continue
        if len(removals) > 1 or len(creations) > 1:
            ambiguities.append((shape, tuple(removals), tuple(creations)))
            continue
        was, now = removals[0], creations[0]
        if was == now:
            continue  # pragma: no cover - the differ never reports an unchanged identity
        pairs.append((shape, was, now))
    return pairs, ambiguities


@dataclass(frozen=True)
class AmbiguousRename:
    """One element content shape whose ``remove``/``create`` pairing was not unique."""

    layer: str
    #: The content, as :func:`identity_stripped` rendered it.
    content: str
    #: The removed identities carrying it, in their given order.
    removals: tuple[str, ...]
    #: The created identities carrying it, in their given order.
    creations: tuple[str, ...]


@dataclass(frozen=True)
class ElementRenames:
    """Which whole-element removals and creations of one change set are renames.

    ``pairs`` and ``ambiguities`` are in the order of first creation per content
    shape. ``unpaired_removals`` and ``unpaired_creations`` are every removal and
    every creation no pair took — an ambiguous one included, and one whose value
    is not a mapping — in their given order, as ``(layer, identity)``.
    """

    #: ``(layer, was, now)``.
    pairs: tuple[tuple[str, str, str], ...]
    ambiguities: tuple[AmbiguousRename, ...]
    unpaired_removals: tuple[tuple[str, str], ...]
    unpaired_creations: tuple[tuple[str, str], ...]


def element_rename_pairs(
    removed: Iterable[tuple[str, str, Any]], created: Iterable[tuple[str, str, Any]]
) -> ElementRenames:
    """The whole-element renames among *removed* and *created*.

    Args:
        removed: ``(layer, identity, published value)`` of every removed element,
            in delta order.
        created: the same, of every created element.

    The published value is the delta's (:func:`swcraft_tools.editing.read.publish_element`),
    never a writer dictionary: the two differ where a field's JSON spelling does
    (an ``init`` of ``false`` against ``0`` dumps alike and publishes apart).
    """
    removed = list(removed)
    created = list(created)
    removed_by: dict[tuple[str, str], list[str]] = {}
    created_by: dict[tuple[str, str], list[str]] = {}
    for layer, identity, value in removed:
        content = identity_stripped(value)
        if content is not None:
            removed_by.setdefault((layer, content), []).append(identity)
    for layer, identity, value in created:
        content = identity_stripped(value)
        if content is not None:
            created_by.setdefault((layer, content), []).append(identity)
    pairs, ambiguous = unique_pairing(removed_by, created_by)
    paired_removals = {(shape[0], was) for shape, was, _now in pairs}
    paired_creations = {(shape[0], now) for shape, _was, now in pairs}
    return ElementRenames(
        pairs=tuple((shape[0], was, now) for shape, was, now in pairs),
        ambiguities=tuple(
            AmbiguousRename(shape[0], shape[1], removals, creations) for shape, removals, creations in ambiguous
        ),
        unpaired_removals=tuple(
            (layer, identity) for layer, identity, _ in removed if (layer, identity) not in paired_removals
        ),
        unpaired_creations=tuple(
            (layer, identity) for layer, identity, _ in created if (layer, identity) not in paired_creations
        ),
    )
