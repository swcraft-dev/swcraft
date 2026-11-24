"""The four relation resolvers ``binding.json`` names.

A *reference* is a field whose value is the target element's identity, resolved by a key lookup.
A *relation* names an element of another layer by something that is not that element's identity,
or is a collection whose keys or members do. ``model-contract.json`` declares each relation on the
field that carries it, with a ``resolution`` naming the shape of the source value; this module
holds one callable per shape, and ``binding.json``'s ``relations`` section ties a declared field to
its resolver. The contract itself names no Python.

**The resolver contract.** :func:`swcraft_tools.model_registry.ModelRegistry.relations` hands each
resolver out bound to its source field, as ``(source_element) -> Iterable[target key]``. A resolver
reports what the source element *says*, never what the model contains: a key naming no declared
element is still yielded, because that finding belongs to the validator's checks (filtering here
would put one complaint in two places), and filtering would need the whole model to answer a
question about one element.

**What a resolver does not do.** It does not decide which target element matches: the yielded key
is compared against the relation's declared target ``field``, so where that field is not the
target's identity two elements carrying the same value both take an edge. Every shipped relation
targets an identity field; the behaviour stands for a later contract version that declares
otherwise. Matching is the traversal's.

This module is a leaf of the generic core: it imports nothing from ``swcraft_tools``, because it
reads attributes off whatever element the registry hands it, including a class bound from outside
the package.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

__all__ = [
    "RelationResolutionError",
    "resolve_field_value",
    "resolve_list_values",
    "resolve_map_keys",
    "resolve_versioned_list_values",
]


class RelationResolutionError(TypeError):
    """A resolver was asked for a shape the source value does not have.

    A declared ``resolution`` disagrees with the bound field's type (``map-keys`` over a
    non-mapping): a defect in the contract or the binding. It is raised because an empty answer
    is indistinguishable from an element that names no target.
    """


def _value(element: Any, field: str) -> Any:
    """The source field's value; a missing attribute raises ``AttributeError``, a binding defect."""
    return getattr(element, field)


def resolve_field_value(element: Any, field: str) -> Iterable[Any]:
    """``field-value`` — the field's own value names one target.

    Yields at most one key; ``None`` yields none. The value is never coerced: it is compared
    against the target's declared field as it is. No shipped relation uses this shape; it stays
    so ``binding.json`` can bind it without a code change.
    """
    value = _value(element, field)
    return () if value is None else (value,)


def resolve_map_keys(element: Any, field: str) -> Iterable[Any]:
    """``map-keys`` — every key of a mapping field names a target; a ``None`` map names nothing."""
    value = _value(element, field)
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise RelationResolutionError(
            f"{type(element).__name__}.{field} is declared as a `map-keys` relation "
            f"but holds {type(value).__name__}, which is not a mapping"
        )
    return tuple(value)


def _members(element: Any, field: str, resolution: str) -> Sequence[Any]:
    """The list field's members, with a value that is not a list of members refused."""
    value = _value(element, field)
    if value is None:
        return ()
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise RelationResolutionError(
            f"{type(element).__name__}.{field} is declared as a `{resolution}` relation "
            f"but holds {type(value).__name__}, which is not a list of members"
        )
    return value


def resolve_list_values(element: Any, field: str) -> Iterable[Any]:
    """``list-values`` — every member of a list field is a target key.

    ``str`` and ``bytes`` are refused: iterating one would yield characters.
    """
    return tuple(_members(element, field, "list-values"))


def resolve_versioned_list_values(element: Any, field: str) -> Iterable[Any]:
    """``versioned-list-values`` — every ``<identity>@<version>`` member names a target by its identity.

    Members of a ``satisfies`` or ``derived_from`` list are ``REQ-12@3`` or a bare ``REQ-12``. The
    key is the text before the **first** ``@``, verbatim; a malformed suffix is the validator's to
    report. A member containing ``:`` is skipped: it is the element form ``<token>:<name>``, and a
    requirement ``id`` may not contain ``:``. ``str`` is refused as in :func:`resolve_list_values`.
    """
    members = _members(element, field, "versioned-list-values")
    return tuple(member.partition("@")[0] for member in members if ":" not in member)
