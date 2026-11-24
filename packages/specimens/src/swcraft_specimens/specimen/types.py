"""The type layers: units, compu methods, data constraints, application types and mapping sets.

Each layer is written in the count the passport states (none when the leaf is unstated), every
reference names a declared element and every element is valid under SWCraft's rules:

- **units** (`Un...`): a name only, no display name;
- **compu methods** (`Cm...`): with at least one unit every one is `linear`, its unit
  round-robin over the units, factor 1 and offset 0; with no unit every one is a `texttable`
  with the two entries `E0: 0` and `E1: 1`, since a linear method must name a unit;
- **data constraints** (`Dc...`): `min: 0`, `max: 100`;
- **application types** (`At...`): a `unit_ref`, a `compu_method_ref` (only when the methods are
  linear: SWCraft refuses a texttable from an application type as `compu-method-wrong-category`)
  and a `data_constr_ref`, each round-robin when there are any; nothing inline;
- **mapping sets** (`Ms...`): with application types and value types both present, every
  application type is mapped exactly once to a value type (round-robin), the rows spread over the
  sets as evenly as possible, the first sets taking one more; otherwise the sets carry no rows.

No port names an application type, so SWCraft's `application-type-unmapped` rule stays silent.
Every count is buildable: the layers add no `layer-total` refusal. The names are the identifier
plan's (`identifiers.identifier_names`).

What this does not reach: the passport states counts and identifier lengths of these layers, not
their content. "Valid under SWCraft's rules" is checked against SWCraft only on the recorded
fixtures; no unit, empty mapping sets and unmapped application types are held by the specimen
generator's own pass and self-check alone.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..errors import GenerationError
from ..model import (
    ApplicationType,
    CompuMethod,
    DataConstr,
    LinearCompuMethod,
    MappingSet,
    TextTableCompuMethod,
    Unit,
    ValueType,
)
from .common import stated_count
from .vocabulary import layer_leaf

#: The five type layers in the passport's layer order, each with its name stem.
TYPE_STEMS: dict[str, str] = {
    "application_types": "At",
    "mapping_sets": "Ms",
    "units": "Un",
    "compu_methods": "Cm",
    "data_constraints": "Dc",
}
TYPE_LAYERS = tuple(TYPE_STEMS)

#: A texttable compu method's entries when no unit exists.
TEXTTABLE_ENTRIES: tuple[tuple[str, int], ...] = (("E0", 0), ("E1", 1))
#: Every data constraint's bounds.
CONSTRAINT_MIN = 0
CONSTRAINT_MAX = 100


@dataclass(frozen=True)
class TypeLayers:
    """The five type layers of a specimen, each in emission order."""

    units: tuple[Unit, ...]
    compu_methods: tuple[CompuMethod, ...]
    data_constraints: tuple[DataConstr, ...]
    application_types: tuple[ApplicationType, ...]
    mapping_sets: tuple[MappingSet, ...]


def type_count(stated: Mapping[str, Any], layer: str) -> int:
    """The stated count of a type layer; none when the leaf is unstated."""
    return stated_count(stated, layer_leaf(layer))


def _cycle(names: Sequence[str], index: int) -> str:
    """The `index`-th of `names` round-robin (`names` not empty)."""
    return names[index % len(names)]


def _round_robin(names: Sequence[str], index: int) -> str | None:
    """`_cycle`, or `None` when there is no name to refer to."""
    return _cycle(names, index) if names else None


def _mapping_sets(names: Sequence[str], applications: Sequence[str], values: Sequence[str]) -> tuple[MappingSet, ...]:
    rows = [(name, _cycle(values, index)) for index, name in enumerate(applications)] if applications and values else []
    sets: list[MappingSet] = []
    start = 0
    for index, name in enumerate(names):
        size = len(rows) // len(names) + (1 if index < len(rows) % len(names) else 0)
        sets.append(MappingSet(name, tuple(rows[start : start + size])))
        start += size
    return tuple(sets)


def build_types(
    stated: Mapping[str, Any],
    value_types: Sequence[ValueType],
    names: Mapping[str, Sequence[str]],
) -> TypeLayers:
    """The five type layers of a stated passport, named by `names`.

    The mapping rows pair the application types with `value_types`.
    """
    for layer in TYPE_LAYERS:
        if len(names[layer]) != type_count(stated, layer):
            raise GenerationError(
                f"the identifier plan names {len(names[layer])} elements of the layer {layer}, "
                f"which states {type_count(stated, layer)}"
            )
    unit_names = list(names["units"])
    method_names = list(names["compu_methods"])
    constraint_names = list(names["data_constraints"])
    application_names = list(names["application_types"])
    units = tuple(Unit(name) for name in unit_names)
    compu_methods: tuple[CompuMethod, ...]
    if unit_names:
        compu_methods = tuple(
            LinearCompuMethod(name, _cycle(unit_names, index)) for index, name in enumerate(method_names)
        )
    else:
        compu_methods = tuple(TextTableCompuMethod(name, TEXTTABLE_ENTRIES) for name in method_names)
    # Only a linear method may be referenced from an application type.
    referable_methods = method_names if unit_names else []
    data_constraints = tuple(DataConstr(name, CONSTRAINT_MIN, CONSTRAINT_MAX) for name in constraint_names)
    application_types = tuple(
        ApplicationType(
            name,
            _round_robin(unit_names, index),
            _round_robin(referable_methods, index),
            _round_robin(constraint_names, index),
        )
        for index, name in enumerate(application_names)
    )
    mapping_sets = _mapping_sets(names["mapping_sets"], application_names, [value.name for value in value_types])
    return TypeLayers(units, compu_methods, data_constraints, application_types, mapping_sets)
