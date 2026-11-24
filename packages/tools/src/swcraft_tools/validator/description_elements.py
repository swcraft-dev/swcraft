"""The data description elements: their layers, their packages and the type facts read from them.

Holds the class-to-layer table (:func:`data_type_layer`, :func:`data_description_package_path`),
the ``stated_*`` readers that answer what a type states through its inline fields or the
unit, compu method and data constraint it names, and the cross-element rules of the
``units``, ``compu_methods`` and ``data_constraints`` layers
(:func:`data_description_element_records`, :func:`check_data_description_elements`):
``unit.duplicate-name``, ``compu-method.duplicate-name``, ``data-constr.duplicate-name``,
``data-type.unknown-unit``, ``data-type.unknown-compu-method``, ``data-type.unknown-data-constr``,
``compu-method.unknown-unit``, ``data-type.compu-method-wrong-category``,
``data-type.compu-method-on-unresolved-reference-type`` and ``compu-method.table-entry-repeated``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import NamedTuple

from swcraft_tools.arxml_version import DATA_DESCRIPTION_PACKAGE_NAMES, ROOT_PACKAGE_NAMES
from swcraft_tools.data_description import DataDescription
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.schema import (
    ApplicationDataType,
    ArrayDataType,
    BaseDataType,
    DataConstr,
    DataDescriptionElement,
    DataType,
    EnumDataType,
    ExternalDataType,
    ImplementationArrayDataType,
    ImplementationRecordDataType,
    Linear,
    MappingSet,
    PointerDataType,
    RecordDataType,
    ReferenceDataType,
    TextTable,
    Unit,
    ValueDataType,
    ValueRange,
)
from swcraft_tools.validator.model_view import ModelInput, as_view
from swcraft_tools.validator.results import ConsistencyIssue, ConsistencyResult, consistency_result


def duplicates(names: Iterable[str]) -> list[str]:
    """The names appearing more than once, sorted."""
    seen: set[str] = set()
    duplicated: set[str] = set()
    for name in names:
        if name in seen:
            duplicated.add(name)
        seen.add(name)
    return sorted(duplicated)


def chain_text(chain: tuple[str, ...]) -> str:
    """*chain* as a message spells it: each name quoted, joined by ``->``."""
    return " -> ".join(f"'{name}'" for name in chain)


def article(noun: str) -> str:
    """``a record`` / ``an array`` / ``an implementation record``."""
    return f"{'an' if noun[0] in 'aeiou' else 'a'} {noun}"


def implementation_end(name: str, data_types: DataDescription) -> tuple[tuple[str, ...], DataType] | None:
    """The names from *name* down its reference chain, and the declaration it ends at — or ``None``.

    ``('A',)`` for a type that is not a reference type; ``('Alias', 'A')`` for a
    chain of one. ``None`` for a broken link or a cycle, each another rule's finding.
    *name* is an implementation type's.
    """
    names: list[str] = []
    current: DataType | None = data_types.implementation(name)
    while isinstance(current, ReferenceDataType):
        if current.name in names:
            return None
        names.append(current.name)
        current = data_types.implementation(current.base)
    if current is None:
        return None
    return (*names, current.name), current


# ---------------------------------------------------------------------------
# The data description layers' packages
# ---------------------------------------------------------------------------

#: The data description layer of each class, in the order the writer first emits into the
#: layer's package. Keyed on the class because an element carries no layer of its own and
#: every class is the kind of one layer alone.
DATA_DESCRIPTION_LAYER_OF_CLASS: tuple[tuple[tuple[type, ...], str], ...] = (
    ((BaseDataType,), "base_types"),
    (
        (
            ValueDataType,
            ReferenceDataType,
            ImplementationRecordDataType,
            ImplementationArrayDataType,
            PointerDataType,
            ExternalDataType,
        ),
        "implementation_types",
    ),
    ((ApplicationDataType, EnumDataType, RecordDataType, ArrayDataType), "application_types"),
    ((Unit,), "units"),
    ((TextTable, Linear), "compu_methods"),
    ((DataConstr,), "data_constraints"),
    ((MappingSet,), "mapping_sets"),
)


def data_description_package_path(element: DataType | MappingSet | DataDescriptionElement) -> str:
    """The package *element* is emitted in before its own ``package``: ``DataTypes/{LayerPackage}``.

    The one answer for the writer's and the importers' placement, so no element lands in
    two packages. It reads the element's class and nothing else.

    Raises:
        ValueError: for an element of no data description class -- a defect of the
            caller, never a model.
    """
    for classes, layer in DATA_DESCRIPTION_LAYER_OF_CLASS:
        if isinstance(element, classes):
            return f"{ROOT_PACKAGE_NAMES[0]}/{DATA_DESCRIPTION_PACKAGE_NAMES[layer]}"
    raise ValueError(f"{type(element).__name__} is no data description element")


def data_type_layer(element: DataType | DataDescriptionElement) -> str:
    """The model layer *element* is declared in: a type layer, ``units``, ``compu_methods`` or ``data_constraints``.

    The six implementation kinds answer ``implementation_types``; a mapping set answers
    ``mapping_sets``. It reads the element's class and nothing else.

    Raises:
        ValueError: for an element of no data description class -- a defect of
            the caller, never a model.
    """
    for classes, layer in DATA_DESCRIPTION_LAYER_OF_CLASS:
        if isinstance(element, classes):
            return layer
    raise ValueError(f"{type(element).__name__} is no data description element")


# ---------------------------------------------------------------------------
# The data description elements
#
# What a type states, whichever form states it, and the cross-element rules of
# the three layers of the forms a type names by reference.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NotResolved:
    """The answer of a ``stated_*`` function whose reference field names no element of the kind it needs."""


#: A ``stated_*`` answer for a reference field naming no element of the needed kind.
#: Distinct from ``None`` and ``""`` (no such property): callers abstain on it, because
#: :func:`data_description_element_records` already reports the cause.
NOT_RESOLVED = NotResolved()


def stated_unit_text(application_type: ApplicationDataType, description: DataDescription) -> str | NotResolved:
    """The unit *application_type* states, as the text the interface comparison compares: stripped, ``""`` for none.

    Its inline ``unit``, or the named unit's ``display_name`` (its ``name`` where that is
    blank). Units compare as text: the model holds no ``factorSiToUnit``,
    ``offsetSiToUnit`` or physical dimension. :data:`NOT_RESOLVED` where ``unit_ref``
    names no declared unit.
    """
    if application_type.unit_ref is None:
        return application_type.unit.strip()
    unit = description.unit(application_type.unit_ref)
    if unit is None:
        return NOT_RESOLVED
    return unit.display_name.strip() or unit.name


def stated_range(
    application_type: ApplicationDataType, description: DataDescription
) -> ValueRange | None | NotResolved:
    """The range *application_type* states: its inline ``range``, or the data constraint it names.

    Built with ``model_construct``: ``rule:data_constraints.data_constr.bounds-coherent``
    already refused incoherent bounds at parse time. The constraint's ``level`` is not
    part of a range. :data:`NOT_RESOLVED` where ``data_constr_ref`` names no declared
    data constraint.
    """
    if application_type.data_constr_ref is None:
        return application_type.range
    data_constr = description.data_constr(application_type.data_constr_ref)
    if data_constr is None:
        return NOT_RESOLVED
    return ValueRange.model_construct(min=data_constr.min, max=data_constr.max)


def stated_literals(
    declared: EnumDataType | ValueDataType | ReferenceDataType, description: DataDescription
) -> tuple[tuple[str, int], ...] | NotResolved:
    """The ``(name, value)`` pairs *declared* states as an ``init`` vocabulary.

    For an enumeration: its own ``literals``, or the entries of the texttable it names,
    in declaration order. For a value type or a reference type: the entries of
    :func:`implementation_table`.
    ``[constr_1225]`` (SWCT R4.2.2 ``:15421``, R4.3.0 ``:16544``) gives a data
    prototype typed by an implementation type that references a ``TEXTTABLE`` a
    text value matching one of its scales, which SWCraft holds as the entry's
    name -- the vocabulary ``[constr_1221]`` (``:21113`` / ``:23511``) gives an
    enumeration's literals.

    :data:`NOT_RESOLVED` where the enumeration's ``compu_method_ref`` names no
    declared compu method or a ``linear``, and for a value type or a reference
    type for which :func:`implementation_table` answers no table.
    """
    if isinstance(declared, EnumDataType):
        if declared.compu_method_ref is None:
            return tuple((literal.name, literal.value) for literal in declared.literals or ())
        compu_method = description.compu_method(declared.compu_method_ref)
        if not isinstance(compu_method, TextTable):
            return NOT_RESOLVED
        return tuple((entry.name, entry.value) for entry in compu_method.entries)
    stated = implementation_table(declared, description)
    if stated is None:
        return NOT_RESOLVED
    return tuple((entry.name, entry.value) for entry in stated.table.entries)


class StatedTable(NamedTuple):
    """The texttable an implementation type states: the type that states it, the reference, and the element."""

    #: The type itself or the first one down its chain that states a ``compu_method_ref``.
    type_name: str
    compu_method_ref: str
    table: TextTable


def implementation_table(
    declared: ValueDataType | ReferenceDataType, description: DataDescription
) -> StatedTable | None:
    """The texttable a value type or a reference type states, on the type or further along its chain.

    The implementation type ``[constr_1225]`` speaks of *references* a ``TEXTTABLE`` compu method; in the model a value
    type and a reference type state one by ``compu_method_ref``, and a reference
    type that states none takes its base's. The first reference down the chain decides.

    ``None`` where no ``compu_method_ref`` lies along the chain, where the chain does not
    end at a value type, breaks or closes on itself, and where the deciding reference
    names no declared compu method or a ``linear`` (``rule:data-type.unknown-compu-method``
    and ``rule:data-type.compu-method-wrong-category`` report those).
    """
    end = implementation_end(declared.name, description)
    if end is None or not isinstance(end[1], ValueDataType):
        return None
    for name in end[0]:
        stating = description.implementation(name)
        if isinstance(stating, (ValueDataType, ReferenceDataType)) and stating.compu_method_ref is not None:
            compu_method = description.compu_method(stating.compu_method_ref)
            if not isinstance(compu_method, TextTable):
                return None
            return StatedTable(stating.name, stating.compu_method_ref, compu_method)
    return None


#: The word a finding names each referring kind by.
_REFERRING_KIND_WORDS: dict[type, str] = {
    ApplicationDataType: "Application type",
    EnumDataType: "Enum",
    ValueDataType: "Value type",
    ReferenceDataType: "Reference type",
}


def data_description_element_records(description: DataDescription) -> list[ConsistencyIssue]:
    """The cross-element rules of the three layers of the forms a type names by reference.

    * a name declared twice inside one layer: ``rule:unit.duplicate-name``,
      ``rule:compu-method.duplicate-name`` (a ``texttable`` and a ``linear`` of
      one name included) and ``rule:data-constr.duplicate-name``;
    * a reference field naming no element of its layer:
      ``rule:data-type.unknown-unit`` (an application type's ``unit_ref``),
      ``rule:data-type.unknown-compu-method`` (an application type's, an
      enumeration's, a value type's and a reference type's
      ``compu_method_ref``), ``rule:data-type.unknown-data-constr`` (an
      application type's ``data_constr_ref``) and
      ``rule:compu-method.unknown-unit`` (a linear compu method's ``unit``);
    * the role a named compu method plays:
      ``rule:data-type.compu-method-wrong-category`` -- an application type
      naming a ``texttable``, or an enumeration, a value type or a reference type
      naming a ``linear`` (``[constr_1158]`` for the two implementation kinds);
      ``rule:data-type.compu-method-on-unresolved-reference-type`` -- a reference
      type stating a ``compu_method_ref`` whose chain, resolved, does not end in a
      value type (``[constr_1383]``); a chain ending at an external data type stub,
      breaking or closing on itself is not judged; and
      ``rule:compu-method.table-entry-repeated`` -- two entries of one texttable
      sharing a name or a value.

    Unit compatibility (``[constr_1026]``) is not judged: a ``unit`` element
    holds no factor, offset or dimension, so any two units are compatible. Nor
    are the elements' XML text and whether an element is referenced at all.
    """
    records = _duplicate_form_name_records(description)
    records.extend(_compu_method_records(description))
    for data_type in description.data_types:
        if isinstance(data_type, ApplicationDataType):
            records.extend(_application_reference_records(data_type, description))
        if isinstance(data_type, (ApplicationDataType, EnumDataType, ValueDataType, ReferenceDataType)):
            records.extend(_compu_method_reference_records(data_type, description))
    return records


def _duplicate_form_name_records(description: DataDescription) -> list[ConsistencyIssue]:
    """A name declared twice inside the ``units``, the ``compu_methods`` or the ``data_constraints`` layer."""
    records: list[ConsistencyIssue] = []
    for name in duplicates([unit.name for unit in description.units]):
        records.append(
            ConsistencyIssue(
                rule="unit.duplicate-name",
                message=f"Duplicate unit name '{name}' declared in multiple files.",
                subjects=(Subject("units", name),),
            )
        )
    for name in duplicates([compu_method.name for compu_method in description.compu_methods]):
        records.append(
            ConsistencyIssue(
                rule="compu-method.duplicate-name",
                message=f"Duplicate compu method name '{name}' declared in multiple files.",
                subjects=(Subject("compu_methods", name),),
            )
        )
    for name in duplicates([data_constr.name for data_constr in description.data_constraints]):
        records.append(
            ConsistencyIssue(
                rule="data-constr.duplicate-name",
                message=f"Duplicate data constraint name '{name}' declared in multiple files.",
                subjects=(Subject("data_constraints", name),),
            )
        )
    return records


def _compu_method_records(description: DataDescription) -> list[ConsistencyIssue]:
    """A linear's ``unit`` names a declared unit, and a texttable's entry names and values are each unique."""
    records: list[ConsistencyIssue] = []
    for compu_method in description.compu_methods:
        if isinstance(compu_method, Linear):
            if description.unit(compu_method.unit) is None:
                records.append(
                    ConsistencyIssue(
                        rule="compu-method.unknown-unit",
                        message=(
                            f"Linear compu method '{compu_method.name}' unit '{compu_method.unit}' is not declared "
                            "in the units layer."
                        ),
                        subjects=(Subject("compu_methods", compu_method.name, (field_step("unit"),)),),
                        target=compu_method.unit,
                    )
                )
            continue
        entry_names = [entry.name for entry in compu_method.entries]
        repeated_names = {name for name in entry_names if entry_names.count(name) > 1}
        if repeated_names:
            records.append(
                ConsistencyIssue(
                    rule="compu-method.table-entry-repeated",
                    message=(
                        f"Texttable compu method '{compu_method.name}' has duplicate entry name(s) "
                        f"{sorted(repeated_names)}; entry names must be unique within a texttable."
                    ),
                    subjects=tuple(
                        Subject("compu_methods", compu_method.name, (field_step("entries"), member_step(repeated_name)))
                        for repeated_name in sorted(repeated_names)
                    ),
                )
            )
        entry_values = [entry.value for entry in compu_method.entries]
        repeated_values = {value for value in entry_values if entry_values.count(value) > 1}
        if repeated_values:
            records.append(
                ConsistencyIssue(
                    rule="compu-method.table-entry-repeated",
                    message=(
                        f"Texttable compu method '{compu_method.name}' has duplicate entry value(s) "
                        f"{sorted(repeated_values)}; entry values must be unique within a texttable."
                    ),
                    subjects=tuple(
                        Subject("compu_methods", compu_method.name, (field_step("entries"), member_step(entry.name)))
                        for entry in compu_method.entries
                        if entry.value in repeated_values
                    ),
                )
            )
    return records


def _application_reference_records(
    data_type: ApplicationDataType, description: DataDescription
) -> list[ConsistencyIssue]:
    """An application type's ``unit_ref`` and ``data_constr_ref`` each name a declared element of their layer."""
    records: list[ConsistencyIssue] = []
    if data_type.unit_ref is not None and description.unit(data_type.unit_ref) is None:
        records.append(
            ConsistencyIssue(
                rule="data-type.unknown-unit",
                message=(
                    f"Application type '{data_type.name}' unit_ref '{data_type.unit_ref}' is not declared "
                    "in the units layer."
                ),
                subjects=(Subject("application_types", data_type.name, (field_step("unit_ref"),)),),
                target=data_type.unit_ref,
            )
        )
    if data_type.data_constr_ref is not None and description.data_constr(data_type.data_constr_ref) is None:
        records.append(
            ConsistencyIssue(
                rule="data-type.unknown-data-constr",
                message=(
                    f"Application type '{data_type.name}' data_constr_ref '{data_type.data_constr_ref}' is "
                    "not declared in the data_constraints layer."
                ),
                subjects=(Subject("application_types", data_type.name, (field_step("data_constr_ref"),)),),
                target=data_type.data_constr_ref,
            )
        )
    return records


def _compu_method_reference_records(
    data_type: ApplicationDataType | EnumDataType | ValueDataType | ReferenceDataType, description: DataDescription
) -> list[ConsistencyIssue]:
    """The compu method a type's ``compu_method_ref`` names: declared, of the right category, on a resolved chain."""
    records: list[ConsistencyIssue] = []
    reference = data_type.compu_method_ref
    if reference is None:
        return records
    word = _REFERRING_KIND_WORDS[type(data_type)]
    application_side = isinstance(data_type, (ApplicationDataType, EnumDataType))
    owner = Subject(
        "application_types" if application_side else "implementation_types",
        data_type.name,
        (field_step("compu_method_ref"),),
    )
    compu_method = description.compu_method(reference)
    if compu_method is None:
        records.append(
            ConsistencyIssue(
                rule="data-type.unknown-compu-method",
                message=(
                    f"{word} '{data_type.name}' compu_method_ref '{reference}' is not declared in the "
                    "compu_methods layer."
                ),
                subjects=(owner,),
                target=reference,
            )
        )
    elif isinstance(data_type, ApplicationDataType) and isinstance(compu_method, TextTable):
        records.append(
            ConsistencyIssue(
                rule="data-type.compu-method-wrong-category",
                message=(
                    f"Application type '{data_type.name}' compu_method_ref '{reference}' names a texttable compu "
                    "method; an application type's conversion is a linear compu method, and a texttable's "
                    "entries are an enum's values (name the texttable from an enum)."
                ),
                subjects=(owner, Subject("compu_methods", reference)),
            )
        )
    elif not isinstance(data_type, ApplicationDataType) and isinstance(compu_method, Linear):
        records.append(
            ConsistencyIssue(
                rule="data-type.compu-method-wrong-category",
                message=(
                    f"{word} '{data_type.name}' compu_method_ref '{reference}' names a linear compu method; "
                    + (
                        "an enum's values are the entries of a texttable compu method."
                        if application_side
                        else "an implementation type's compu method is restricted to the category "
                        "BITFIELD_TEXTTABLE or TEXTTABLE ([constr_1158]), so it names a texttable."
                    )
                ),
                subjects=(owner, Subject("compu_methods", reference)),
            )
        )
    if isinstance(data_type, ReferenceDataType):
        end = implementation_end(data_type.name, description)
        if end is not None and not isinstance(end[1], (ValueDataType, ExternalDataType)):
            chain, bottom = end
            records.append(
                ConsistencyIssue(
                    rule="data-type.compu-method-on-unresolved-reference-type",
                    message=(
                        f"Reference type '{data_type.name}' states compu_method_ref '{reference}', but its "
                        f"reference chain {chain_text(chain)} ends in {article(bottom.kind.replace('_', ' '))} "
                        "and not in a value type: a TYPE_REFERENCE type's compu method is allowed only where "
                        "the resolved chain ends in a VALUE type ([constr_1383])."
                    ),
                    subjects=(
                        Subject("implementation_types", data_type.name, (field_step("compu_method_ref"),)),
                        Subject("implementation_types", bottom.name),
                    ),
                )
            )
    return records


def check_data_description_elements(model_root: ModelInput) -> ConsistencyResult:
    """:func:`data_description_element_records` over the model at *model_root*."""
    view = as_view(model_root)
    return consistency_result(data_description_element_records(view.data_description))


def data_description_references(element: DataType | DataDescriptionElement) -> tuple[tuple[str, str], ...]:
    """The ``(layer, name)`` of every unit, compu method and data constraint *element* names directly.

    An application type's ``unit_ref``, ``compu_method_ref`` and ``data_constr_ref``,
    an enumeration's, a value type's and a reference type's ``compu_method_ref``, and a
    ``linear``'s ``unit``, in that order. Every other element names none.
    """
    found: tuple[tuple[str, str | None], ...]
    if isinstance(element, ApplicationDataType):
        found = (
            ("units", element.unit_ref),
            ("compu_methods", element.compu_method_ref),
            ("data_constraints", element.data_constr_ref),
        )
    elif isinstance(element, EnumDataType | ValueDataType | ReferenceDataType):
        found = (("compu_methods", element.compu_method_ref),)
    elif isinstance(element, Linear):
        compu_method = element
        found = (("units", compu_method.unit),)
    else:
        found = ()
    return tuple((layer, name) for layer, name in found if name is not None)
