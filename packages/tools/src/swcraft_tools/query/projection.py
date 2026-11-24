"""The ``full`` projection: one element or member, every field its own kind declares, in the declaration order.

``read`` at ``full`` in the editing protocol and ``swcraft query --form full`` both publish through these two
functions, so the two surfaces publish one value per element. A nested model is published off its own kind; a
value that is not a declared model is its JSON form.

A **placement** field (``swc.group``) lives in the layer's placement table, not in the element, so the caller
supplies its value (:data:`PlacementValue`): the protocol refuses an element whose table holds no value
(``placement_unavailable``), and the CLI prints ``null``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from pydantic import BaseModel

from swcraft_tools.query.edges import field_attribute
from swcraft_tools.query.request import canonical


class _Field(Protocol):
    """One declared field, as the projection reads it: its published name and whether it is placement."""

    @property
    def name(self) -> str: ...

    @property
    def placement(self) -> bool: ...


class _Kind(Protocol):
    """One declared element kind, as the projection reads it: its fields in the declaration order."""

    @property
    def fields(self) -> Sequence[_Field]: ...


class KindLookup(Protocol):
    """A registry the projection can publish through: the declared kind of a model class, or ``None``."""

    def kind_of_model(self, model: type) -> _Kind | None: ...


#: The caller's answer for one placement field: ``(field, kind, placement values or None)`` to the value.
PlacementValue = Callable[[Any, Any, Mapping[str, str] | None], Any]


def full_element(
    element: Any, registry: KindLookup, *, placement: Mapping[str, str] | None, placement_value: PlacementValue
) -> Any:
    """One element or nested member, field by field, in the order its kind declares.

    A placement field's value is ``placement_value(field, kind, placement)``; every other field is the
    element's own value, published by :func:`full_value`. A value whose class no kind declares is its JSON
    form, whole.
    """
    kind = registry.kind_of_model(type(element))
    if kind is None:
        return canonical(element)
    published: dict[str, Any] = {}
    for field in kind.fields:
        if field.placement:
            published[field.name] = placement_value(field, kind, placement)
            continue
        attribute = field_attribute(type(element), field.name)  # type: ignore[arg-type]  # mypy limitation: type[Any] is not Hashable for lru_cache
        if attribute not in type(element).model_fields:  # pragma: no cover - the registry derives from model_fields
            continue
        published[field.name] = full_value(getattr(element, attribute), registry, placement_value)
    return published


def full_value(value: Any, registry: KindLookup, placement_value: PlacementValue) -> Any:
    """Any value at ``full``: a model off its own kind, a map or a list item by item, anything else its JSON form.

    A nested model carries no placement values, so a placement field its kind declared would be asked for
    with ``None``.
    """
    if isinstance(value, BaseModel):
        return full_element(value, registry, placement=None, placement_value=placement_value)
    if isinstance(value, Mapping):
        return {key: full_value(item, registry, placement_value) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [full_value(item, registry, placement_value) for item in value]
    return canonical(value)
