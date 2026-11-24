"""The init value rules and the port communication specification rules.

:func:`check_init_conformance` judges every ``init`` -- a data element's, a port's ``com_specs``
entry's (:func:`port_init_records`) and an inter-runnable variable's (:func:`variable_init_records`)
-- against its declared type under the eight ``interface.init-*`` rules. :func:`port_com_spec_records`
reports ``swc.port.com-spec-unknown-element``, ``swc.port.com-spec-inapplicable`` and
``swc.port.com-spec-on-stub``. :func:`is_unwritable_integer` is the one detection the writer shares.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeGuard

from swcraft_tools.data_description import DataDescription
from swcraft_tools.diagnostics import Subject, SubjectStep, field_step, member_step
from swcraft_tools.rendering import render_model_scalar
from swcraft_tools.schema import (
    ApplicationDataType,
    ArrayDataType,
    BaseDataType,
    CSInterface,
    DataElement,
    DataType,
    EnumDataType,
    ExternalDataType,
    ExternalInterface,
    ImplementationArrayDataType,
    ImplementationRecordDataType,
    Interface,
    PointerDataType,
    Port,
    PortComSpec,
    RecordDataType,
    ReferenceDataType,
    SRInterface,
    SWComponent,
    ValueDataType,
)
from swcraft_tools.validator.compatibility import interface_index, is_queued
from swcraft_tools.validator.compositions import stub_member_miss, stub_member_names
from swcraft_tools.validator.data_types import declared_type, prototype_type_ref
from swcraft_tools.validator.description_elements import (
    NotResolved,
    StatedTable,
    article,
    chain_text,
    implementation_end,
    implementation_table,
    stated_literals,
    stated_range,
)
from swcraft_tools.validator.model_view import ModelInput, as_view
from swcraft_tools.validator.results import ConsistencyIssue, ConsistencyResult, consistency_result

# ---------------------------------------------------------------------------
# check_init_conformance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _InitViolation:
    """One way a value fails the type it was judged against, before it is a record.

    A violation is plain data until :func:`check_init_conformance` turns it into a :class:`ConsistencyIssue` at one
    literal site per rule, because the contract's gates read the emission site.

    Args:
        rule: the :data:`CONSISTENCY_RULES` member this is.
        path: where in the value it sits, in the reader's own spelling — ``init``, ``init.load``, ``init[1].x``. An
            address carries no ordinal, so this is the only place the depth is stated.
        detail: the sentence tail, complete with its full stop.
        type_name: the declared data type that refused the value; for a nested value, the nested type.
        steps: the path into that data type's own element the repair would take; empty when the repair is the type as a
            whole.
    """

    rule: str
    path: str
    detail: str
    type_name: str
    steps: tuple[SubjectStep, ...]


def _init_value_word(value: object) -> str:
    """``a list`` / ``a map`` / ``a scalar`` — what the value is, for a message."""
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "a map"
    return "a scalar"


def is_unwritable_integer(value: object) -> bool:
    """Whether *value* is an ``int`` no ``str()`` on this interpreter can spell.

    The only copy of this detection: the writer's guard calls it too, so the writer and ``check-model`` cannot disagree.
    It attempts ``str(value)`` and catches the ``ValueError``, because counting the digits is the conversion that
    raises. With the limit disabled (``sys.get_int_max_str_digits()`` answering ``0``) it answers ``False`` for every
    value. ``bool`` is excluded: the writer writes a boolean as ``1``/``0``.

    It does **not** answer whether the value belongs there at all: that is another rule's, and a non-``int`` of any kind
    is ``False`` here.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    try:
        str(value)
    except ValueError:
        return True
    return False


def _unwritable_integer_violation(value: object, type_name: str, path: str) -> _InitViolation | None:
    """``interface.init-unwritable-integer``: an ``int`` this interpreter cannot convert to decimal.

    The message names the value's bit length, never its digits, and the limit this interpreter reports.

    Returns:
        The violation, or ``None`` when the value is writable or is not an ``int`` at all.
    """
    if not is_unwritable_integer(value):
        return None
    return _InitViolation(
        rule="interface.init-unwritable-integer",
        path=path,
        detail=f"is {render_model_scalar(value)}: writing it into the ARXML means converting it to "
        f"its decimal string, which is above this interpreter's "
        f"{sys.get_int_max_str_digits()}-digit limit.",
        type_name=type_name,
        steps=(),
    )


def _is_init_number(value: object) -> TypeGuard[int | float]:
    """Whether a bound can be applied to *value* at all.

    ``bool`` is excluded: Pydantic's smart union keeps a boolean a ``bool``, and comparing ``True <= 100.0`` would
    silently accept the YAML 1.1 trap (``init: ON`` loads as ``True``).
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _init_violations(
    value: object,
    type_name: str,
    type_map: DataDescription,
    path: str,
    resolving: tuple[str, ...],
    *,
    application: bool,
) -> list[_InitViolation]:
    """How *value* fails the declared type *type_name*, recursively.

    The shape follows the declared type: a value type or an application type takes a scalar within the bounds it
    declares, an `enum` the **name** of one of its literals, a `record` a map keyed by exactly its declared fields, an
    `array` a list of exactly its declared length. A **reference type** has no shape and no range of its own: the value
    is judged at the end of its ``base`` chain. A value type or a reference type that states a table, on the type or
    along its chain (:func:`implementation_table`), takes the name of one of the table's entries, as an enumeration
    takes a literal's.

    **The walk abstains wherever the resolution it needs is itself a finding**, so no finding is reported twice: an
    undeclared type (``check_data_type_references``), a type in a place its kind may not stand (that check's
    reference-role findings), and a type already being resolved on this branch (``check_data_type_cycles``'
    ``data-type.cycle``, which also stops the recursion).

    Two scalars are refused under every type that carries a number: a ``str`` (``interface.init-string-scalar``) and an
    ``int`` this interpreter cannot convert to its decimal string (``interface.init-unwritable-integer``).

    What it does **not** reach: a scalar's *numeric domain*, except where a value type declares a bound. A base type's
    ``size`` and ``encoding`` state no value domain, so ``init: 999`` conforms to a value type over an 8-bit base type,
    and a number on a type that declares no range is admitted. A ``bool`` is refused under a bounded value type and
    accepted without a bound (:func:`_init_scalar_violations`).
    """
    declared = declared_type(type_map, type_name, application=application)
    if declared is None or type_name in resolving:
        return []
    resolving = (*resolving, type_name)

    if isinstance(declared, ReferenceDataType):
        # A reference type that states a table,
        # itself or along its chain, takes the name of one of the table's entries
        # (`[constr_1225]`), never a number.
        table = implementation_table(declared, type_map)
        if table is not None:
            return _init_table_violations(value, type_name, table, path)
        bottom = reference_chain_end(declared, type_map)
        if isinstance(bottom, ValueDataType):
            return _init_scalar_violations(value, bottom.name, bottom, type_map, path)
        if isinstance(bottom, (ImplementationRecordDataType, ImplementationArrayDataType, PointerDataType)):
            return _init_violations(value, bottom.name, type_map, path, resolving, application=False)
        return []
    if isinstance(declared, ImplementationRecordDataType):
        return _init_map_violations(
            value,
            type_name,
            "implementation record",
            "elements",
            {implementation_element.name: implementation_element.type for implementation_element in declared.elements},
            _init_member_violations,
            type_map,
            path,
            resolving,
        )
    if isinstance(declared, ImplementationArrayDataType):
        return _init_list_violations(
            value,
            type_name,
            "implementation array",
            declared.length,
            declared.element_type,
            _init_member_violations,
            type_map,
            path,
            resolving,
        )
    if isinstance(declared, PointerDataType):
        return [
            _InitViolation(
                rule="interface.init-shape-mismatch",
                path=path,
                detail=f"is {_init_value_word(value)}, but type '{type_name}' is a pointer, which takes no init "
                f"value: an address is the RTE's to supply, and no value specification can spell one.",
                type_name=type_name,
                steps=(),
            )
        ]
    if isinstance(declared, ValueDataType):
        table = implementation_table(declared, type_map)
        if table is not None:
            return _init_table_violations(value, type_name, table, path)
        return _init_scalar_violations(value, type_name, declared, type_map, path)
    if isinstance(declared, ApplicationDataType):
        return _init_application_violations(value, type_name, declared, type_map, path)
    if isinstance(declared, EnumDataType):
        return _init_enum_violations(value, type_name, declared, type_map, path)
    if isinstance(declared, RecordDataType):
        return _init_map_violations(
            value,
            type_name,
            "record",
            "fields",
            {record_field.name: record_field.type for record_field in declared.fields},
            _init_application_member_violations,
            type_map,
            path,
            resolving,
        )
    if isinstance(declared, ArrayDataType):
        return _init_list_violations(
            value,
            type_name,
            "array",
            declared.length,
            declared.element_type,
            _init_application_member_violations,
            type_map,
            path,
            resolving,
        )
    # A base type, reached only through a caller's own index, or a kind this function has no rule for.
    return []


def reference_chain_end(declared: ReferenceDataType, type_map: DataDescription) -> DataType | None:
    """The first declaration down *declared*'s reference chain that is not a reference type, or ``None``.

    ``None`` for a link that resolves to nothing and for a chain that closes on itself — each another check's finding.
    """
    end = implementation_end(declared.name, type_map)
    return None if end is None else end[1]


def external_type_chain(type_name: str, type_map: DataDescription, *, application: bool) -> tuple[str, ...] | None:
    """The names from *type_name* to the external data type stub it ends at, or ``None``.

    ``('Stub',)`` for a type that is a stub and ``('RefA', 'RefB', 'Stub')`` for a reference chain whose last ``base``
    is one — ``rule:interface.init-on-external-type``'s two shapes. ``None`` for every other type; for a chain that is
    undeclared, lands on anything but a stub, or closes on itself; and when *application* is true, since an application
    type ends no chain at a stub. A composite's member is not followed here: :func:`_init_member_violations` asks this
    of each member itself.
    """
    if application:
        return None
    end = implementation_end(type_name, type_map)
    return end[0] if end is not None and isinstance(end[1], ExternalDataType) else None


def _init_scalar_violations(
    value: object,
    type_name: str,
    declared: ValueDataType,
    type_map: DataDescription,
    path: str,
) -> list[_InitViolation]:
    """A value type takes a writable scalar, within its bounds where it declares them.

    Three refusals, in an order that keeps each value to one finding:

    1. a ``list`` or a ``dict`` where a scalar is declared — ``interface.init-shape-mismatch``;
    2. a ``str`` — ``interface.init-string-scalar``, under a bounded and an unbounded type alike: a value type that
       references no table carries a number, whatever its base type's ``encoding`` (one that references a table is
       :func:`_init_table_violations`', asked first);
    3. an ``int`` this interpreter cannot convert to its decimal string — ``interface.init-unwritable-integer``, before
       the bounds: moving such a value inside a range does not make it writable.

    Left for the bounds is a ``bool``: :func:`_is_init_number` excludes it, so it is a finding under a bounded value
    type and accepted under an unbounded one — a refusal about the bound, not about writability.

    *declared* may be the end of the element's reference chain; the finding's subject is then that value type, where the
    range is declared.
    """
    if not isinstance(type_map.base(declared.base), BaseDataType):
        # An undeclared base is already reported by `check_data_type_references`
        # (`data-type.value-type-undeclared-base`), and leaves no shape to judge.
        return []
    if isinstance(value, (list, dict)):
        return [
            _InitViolation(
                rule="interface.init-shape-mismatch",
                path=path,
                detail=f"is {_init_value_word(value)}, but type '{type_name}' is a {declared.kind}, "
                f"which takes a scalar.",
                type_name=type_name,
                steps=(),
            )
        ]
    if isinstance(value, str):
        return [
            _InitViolation(
                rule="interface.init-string-scalar",
                path=path,
                detail=f"is {render_model_scalar(value)}, a string, but type '{type_name}' is a "
                f"{declared.kind} that references no table, so its values are numbers; "
                f"a text names an enumeration's literal or a table's entry, and nothing else.",
                type_name=type_name,
                steps=(),
            )
        ]
    unwritable = _unwritable_integer_violation(value, type_name, path)
    if unwritable is not None:
        return [unwritable]
    bounds = (declared.value_min, declared.value_max)
    if all(bound is None for bound in bounds):
        return []
    if not _is_init_number(value):
        return [
            _InitViolation(
                rule="interface.init-shape-mismatch",
                path=path,
                detail=f"is {render_model_scalar(value)}, which is not a number, "
                f"but type '{type_name}' declares a numeric range.",
                type_name=type_name,
                steps=(),
            )
        ]
    if declared.value_min is not None and value < declared.value_min:
        return [
            _InitViolation(
                rule="interface.init-out-of-range",
                path=path,
                detail=f"is {render_model_scalar(value)}, which is below type '{type_name}'s "
                f"declared value_min {render_model_scalar(declared.value_min)}.",
                type_name=type_name,
                steps=(field_step("value_min"),),
            )
        ]
    if declared.value_max is not None and value > declared.value_max:
        return [
            _InitViolation(
                rule="interface.init-out-of-range",
                path=path,
                detail=f"is {render_model_scalar(value)}, which is above type '{type_name}'s "
                f"declared value_max {render_model_scalar(declared.value_max)}.",
                type_name=type_name,
                steps=(field_step("value_max"),),
            )
        ]
    return []


def _init_application_violations(
    value: object,
    type_name: str,
    declared: ApplicationDataType,
    type_map: DataDescription,
    path: str,
) -> list[_InitViolation]:
    """An application type takes a writable scalar, within the ``range`` it declares.

    The value type's three refusals in the same order, then the bounds, under the same rule identifiers. There is no
    ``base`` to resolve, a type declaring no ``range`` bounds nothing, and the ``bool`` asymmetry is the value type's.

    **The bounds are the ones the type states** (:func:`stated_range`): its inline ``range``, or the ``min`` and ``max``
    of the data constraint its ``data_constr_ref`` names; the finding's subject is then the reference field. A
    ``data_constr_ref`` naming nothing bounds nothing here -- ``rule:data-type.unknown-data-constr`` owns it. A
    conversion bounds nothing, whether inline or a referenced ``linear`` compu method.
    """
    if isinstance(value, (list, dict)):
        return [
            _InitViolation(
                rule="interface.init-shape-mismatch",
                path=path,
                detail=f"is {_init_value_word(value)}, but type '{type_name}' is a {declared.kind}, "
                f"which takes a scalar.",
                type_name=type_name,
                steps=(),
            )
        ]
    if isinstance(value, str):
        return [
            _InitViolation(
                rule="interface.init-string-scalar",
                path=path,
                detail=f"is {render_model_scalar(value)}, a string, but type '{type_name}' is an "
                f"{declared.kind} that is no enumeration, so its values are numbers; "
                f"a text names an enumeration's literal or a table's entry, and nothing else.",
                type_name=type_name,
                steps=(),
            )
        ]
    unwritable = _unwritable_integer_violation(value, type_name, path)
    if unwritable is not None:
        return [unwritable]
    bounds = stated_range(declared, type_map)
    if isinstance(bounds, NotResolved) or bounds is None or (bounds.min is None and bounds.max is None):
        return []
    # The repair's place is the inline bound, or the reference
    # field that names the data constraint stating it.
    referenced = declared.data_constr_ref is not None
    where = f"data constraint '{declared.data_constr_ref}'" if referenced else "range"
    if not _is_init_number(value):
        return [
            _InitViolation(
                rule="interface.init-shape-mismatch",
                path=path,
                detail=f"is {render_model_scalar(value)}, which is not a number, "
                f"but type '{type_name}' declares a numeric range.",
                type_name=type_name,
                steps=(),
            )
        ]
    if bounds.min is not None and value < bounds.min:
        return [
            _InitViolation(
                rule="interface.init-out-of-range",
                path=path,
                detail=f"is {render_model_scalar(value)}, which is below type '{type_name}'s "
                f"declared {where} min {render_model_scalar(bounds.min)}.",
                type_name=type_name,
                steps=(field_step("data_constr_ref"),) if referenced else (field_step("range"), field_step("min")),
            )
        ]
    if bounds.max is not None and value > bounds.max:
        return [
            _InitViolation(
                rule="interface.init-out-of-range",
                path=path,
                detail=f"is {render_model_scalar(value)}, which is above type '{type_name}'s "
                f"declared {where} max {render_model_scalar(bounds.max)}.",
                type_name=type_name,
                steps=(field_step("data_constr_ref"),) if referenced else (field_step("range"), field_step("max")),
            )
        ]
    return []


def _init_enum_violations(
    value: object,
    type_name: str,
    declared: EnumDataType,
    type_map: DataDescription,
    path: str,
) -> list[_InitViolation]:
    """An enumeration's ``init`` is a literal's **name**, and only the name — a numeric spelling is refused.

    The message always says to quote, because YAML 1.1 resolves literal names such as `ON`, `NO` and `0x10` to
    non-strings.

    **The vocabulary is what the enumeration states** (:func:`stated_literals`): its own ``literals``, or the entries of
    the texttable its ``compu_method_ref`` names; the finding's subject is then the reference field. A
    ``compu_method_ref`` naming no texttable judges nothing here -- ``rule:data-type.unknown-compu-method`` or
    ``rule:data-type.compu-method-wrong-category`` owns it.
    """
    stated = stated_literals(declared, type_map)
    if isinstance(stated, NotResolved):
        return []
    names = [name for name, _ in stated]
    referenced = declared.compu_method_ref is not None
    return _named_value_violations(
        value,
        path,
        names,
        subject=type_name,
        shape_detail=f"type '{type_name}' is an enum, which takes the name of one of its literals.",
        literal_detail=f"names no literal of enum '{type_name}'; "
        + (
            f"its compu method '{declared.compu_method_ref}' declares {names}. "
            if referenced
            else f"it declares {names}. "
        )
        + "A literal name YAML resolves to a non-string (ON, NO, 0x10) must be quoted.",
        steps=(field_step("compu_method_ref"),) if referenced else (field_step("literals"),),
    )


def _init_table_violations(
    value: object,
    type_name: str,
    stated: StatedTable,
    path: str,
) -> list[_InitViolation]:
    """A table-referencing type's ``init`` is the **name** of one of the table's entries, as under an enumeration.

    A value type or a reference type that references a ``texttable``, itself or along its chain
    (:func:`implementation_table`), is the implementation type ``[constr_1225]`` speaks of (SWCT R4.2.2 ``:15421``,
    R4.3.0 ``:16544``): its value is a text naming one of the table's scales, and SWCraft holds the entry's name. A
    number is refused. The finding's subject is the type that states the reference, at its ``compu_method_ref``.
    """
    names = [entry.name for entry in stated.table.entries]
    return _named_value_violations(
        value,
        path,
        names,
        subject=stated.type_name,
        shape_detail=f"type '{type_name}' references the table '{stated.compu_method_ref}', so it takes the name "
        "of one of its entries.",
        literal_detail=f"names no entry of the table '{stated.compu_method_ref}' that type '{stated.type_name}' "
        f"references; it declares {names}. A number names no entry, and an entry's name YAML resolves to a "
        "non-string (ON, NO, 0x10) must be quoted.",
        steps=(field_step("compu_method_ref"),),
    )


def _named_value_violations(
    value: object,
    path: str,
    names: Sequence[str],
    *,
    subject: str,
    shape_detail: str,
    literal_detail: str,
    steps: tuple[SubjectStep, ...],
) -> list[_InitViolation]:
    """The refusals of a value that names one of *names* -- an enumeration's literals or a table's entries.

    In this order, each reported once: an integer this interpreter cannot convert
    (``interface.init-unwritable-integer``, first, because no name would make it writable); a composite
    (``interface.init-shape-mismatch``, *shape_detail*); then anything but a ``str`` in *names*
    (``interface.init-unknown-enum-literal``, *literal_detail*, at *steps*). A literal's own declared ``value`` is not
    judged here. *subject* is the type each finding names.
    """
    unwritable = _unwritable_integer_violation(value, subject, path)
    if unwritable is not None:
        return [unwritable]
    if isinstance(value, (list, dict)):
        return [
            _InitViolation(
                rule="interface.init-shape-mismatch",
                path=path,
                detail=f"is {_init_value_word(value)}, but {shape_detail}",
                type_name=subject,
                steps=(),
            )
        ]
    if isinstance(value, str) and value in names:
        return []
    return [
        _InitViolation(
            rule="interface.init-unknown-enum-literal",
            path=path,
            detail=f"is {render_model_scalar(value)}, which {literal_detail}",
            type_name=subject,
            steps=steps,
        )
    ]


#: How one composite's member is walked: :func:`_init_violations` for an
#: application record's or array's, :func:`_init_member_violations` for an
#: implementation record's or array's.
_InitMemberWalker = Callable[[object, str, DataDescription, str, tuple[str, ...]], list[_InitViolation]]


def _init_map_violations(
    value: object,
    type_name: str,
    noun: str,
    collection: str,
    members: dict[str, str],
    walk: _InitMemberWalker,
    type_map: DataDescription,
    path: str,
    resolving: tuple[str, ...],
) -> list[_InitViolation]:
    """A map keyed by exactly the declared *members*, then each member's own value, walked by *walk*.

    For both record kinds: *noun* is ``record`` or ``implementation record``, *collection* the field declaring the
    members (``fields`` or ``elements``). A non-map is ``interface.init-shape-mismatch``, keys other than exactly the
    member names ``interface.init-record-field-mismatch``.
    """
    if not isinstance(value, dict):
        return [
            _InitViolation(
                rule="interface.init-shape-mismatch",
                path=path,
                detail=f"is {_init_value_word(value)}, but type '{type_name}' is {article(noun)}, "
                f"which takes a map keyed by its declared {collection}.",
                type_name=type_name,
                steps=(),
            )
        ]
    missing = sorted(set(members) - set(value))
    undeclared = sorted(set(value) - set(members))
    violations: list[_InitViolation] = []
    if missing or undeclared:
        violations.append(
            _InitViolation(
                rule="interface.init-record-field-mismatch",
                path=path,
                detail=f"does not name the {collection} {noun} '{type_name}' declares: "
                f"missing {missing}, undeclared {undeclared}.",
                type_name=type_name,
                steps=(field_step(collection),),
            )
        )
    for name in members:
        if name in value:
            violations.extend(walk(value[name], members[name], type_map, f"{path}.{name}", resolving))
    return violations


def _init_list_violations(
    value: object,
    type_name: str,
    noun: str,
    length: int,
    element_type: str,
    walk: _InitMemberWalker,
    type_map: DataDescription,
    path: str,
    resolving: tuple[str, ...],
) -> list[_InitViolation]:
    """A list of exactly the declared *length*, then each member's own value, walked by *walk*.

    One statement of the array shape for both array kinds (*noun* ``array`` or
    ``implementation array``).
    """
    if not isinstance(value, list):
        return [
            _InitViolation(
                rule="interface.init-shape-mismatch",
                path=path,
                detail=f"is {_init_value_word(value)}, but type '{type_name}' is {article(noun)}, "
                f"which takes a list of its declared length.",
                type_name=type_name,
                steps=(),
            )
        ]
    violations: list[_InitViolation] = []
    if len(value) != length:
        violations.append(
            _InitViolation(
                rule="interface.init-array-length-mismatch",
                path=path,
                detail=f"has {len(value)} member(s), but {noun} '{type_name}' declares length {length}.",
                type_name=type_name,
                steps=(field_step("length"),),
            )
        )
    for index, member in enumerate(value):
        violations.extend(walk(member, element_type, type_map, f"{path}[{index}]", resolving))
    return violations


def _init_member_violations(
    value: object,
    member_type: str,
    type_map: DataDescription,
    path: str,
    resolving: tuple[str, ...],
) -> list[_InitViolation]:
    """One member of an implementation composite's value, against the member's declared type.

    A member typed by an external data type stub, directly or through a chain of reference types, shapes no value and is
    refused under ``rule:interface.init-on-external-type`` with the stub as the second subject; every other member is
    walked by :func:`_init_violations`.
    """
    chain = external_type_chain(member_type, type_map, application=False)
    if chain is not None:
        reached = (
            f"'{chain[-1]}' is an external data type stub"
            if len(chain) == 1
            else f"'{member_type}' resolves through the reference chain {chain_text(chain)} to an external data "
            "type stub"
        )
        return [
            _InitViolation(
                rule="interface.init-on-external-type",
                path=path,
                detail=f"sits where {reached} whose content is outside the project: no value can be shaped against "
                "it. Remove the init, or type the member by a declared type.",
                type_name=chain[-1],
                steps=(),
            )
        ]
    return _init_violations(value, member_type, type_map, path, resolving, application=False)


def _init_application_member_violations(
    value: object,
    member_type: str,
    type_map: DataDescription,
    path: str,
    resolving: tuple[str, ...],
) -> list[_InitViolation]:
    """One member of a record's or an application array's value, against the member's application type."""
    return _init_violations(value, member_type, type_map, path, resolving, application=True)


def check_init_conformance(model_root: ModelInput) -> ConsistencyResult:
    """Check that every sender-receiver `init` fits the type its element declares.

    Three positions are judged: a data element's own `init`; a port's own `init` on a ``com_specs`` entry, against the
    type of the data element the entry names (:func:`port_init_records`); and an inter-runnable variable's `init`,
    against the variable's own type (:func:`variable_init_records`). Each finding has two subjects: the `init`, and the
    declaration that refused the value. The eight rules:

    1. ``interface.init-shape-mismatch``
    2. ``interface.init-unknown-enum-literal``
    3. ``interface.init-record-field-mismatch``
    4. ``interface.init-array-length-mismatch``
    5. ``interface.init-out-of-range``
    6. ``interface.init-string-scalar``
    7. ``interface.init-unwritable-integer``
    8. ``interface.init-on-external-type``

    The first five judge the value against its **declared type**; the next two whether the ARXML exporter can write the
    value at all. The eighth is decided first: a type that is an external data type stub, directly or through a
    reference chain, or a stub-typed member of an implementation record or array, refuses **any** value; its second
    subject is the stub. A finding inside a nested value addresses the element's `init` and states the position in its
    message. Not reached: a base type's value domain, and every case whose resolution is itself a reference or cycle
    finding (:func:`_init_violations`).

    Args:
        model_root: The model root, or a view of it.

    Returns:
        ConsistencyResult with consistent=True only when every declared `init` fits its type.
    """
    view = as_view(model_root)
    interfaces = view.interfaces
    type_map = view.data_description

    records: list[ConsistencyIssue] = []
    for iface in interfaces:
        if not isinstance(iface, SRInterface):
            continue
        for de in iface.data_elements:
            if de.init is None:
                continue
            where = Subject(
                "interfaces",
                iface.name,
                (field_step("data_elements"), member_step(de.name), field_step("init")),
            )
            # Decided before the walk, which would abstain on the stub and let the writer emit a value for a type it
            # cannot see.
            de_type, de_application = prototype_type_ref(de)
            if de_type is None:
                continue  # a prototype stating no type resolves to nothing: no chain, no walk
            chain = external_type_chain(de_type, type_map, application=de_application)
            if chain is not None:
                stub_name = chain[-1]
                reached = (
                    f"'{stub_name}' is an external data type stub"
                    if len(chain) == 1
                    else f"'{de_type}' resolves through the reference chain "
                    + chain_text(chain)
                    + " to an external data type stub"
                )
                records.append(
                    ConsistencyIssue(
                        rule="interface.init-on-external-type",
                        message=(
                            f"Interface '{iface.name}' element '{de.name}' declares an init value, but its type "
                            f"{reached} whose content is outside the project: no value can be shaped against "
                            "it. Remove the init, or type the element by a declared type."
                        ),
                        subjects=(where, Subject("implementation_types", stub_name)),
                    )
                )
                continue
            for violation in _init_violations(de.init, de_type, type_map, "init", (), application=de_application):
                message = (
                    f"Interface '{iface.name}' element '{de.name}' init value at '{violation.path}' {violation.detail}"
                )
                subjects = (
                    where,
                    Subject(
                        "application_types" if de_application else "implementation_types",
                        violation.type_name,
                        violation.steps,
                    ),
                )
                # The stub rule names its layer literally, for the emission scan; the literal rule takes `subjects`, of
                # either layer.
                stub_subjects = (where, Subject("implementation_types", violation.type_name, violation.steps))
                # One branch per rule rather than `rule=violation.rule`: the contract gates read each emission's rule
                # literal statically, and a table lookup would need a second `tableBinding`, which the conformance gate
                # allows only once.
                if violation.rule == "interface.init-shape-mismatch":
                    issue = ConsistencyIssue(rule="interface.init-shape-mismatch", message=message, subjects=subjects)
                elif violation.rule == "interface.init-unknown-enum-literal":
                    issue = ConsistencyIssue(
                        rule="interface.init-unknown-enum-literal", message=message, subjects=subjects
                    )
                elif violation.rule == "interface.init-record-field-mismatch":
                    issue = ConsistencyIssue(
                        rule="interface.init-record-field-mismatch", message=message, subjects=subjects
                    )
                elif violation.rule == "interface.init-array-length-mismatch":
                    issue = ConsistencyIssue(
                        rule="interface.init-array-length-mismatch", message=message, subjects=subjects
                    )
                elif violation.rule == "interface.init-out-of-range":
                    issue = ConsistencyIssue(rule="interface.init-out-of-range", message=message, subjects=subjects)
                elif violation.rule == "interface.init-string-scalar":
                    issue = ConsistencyIssue(rule="interface.init-string-scalar", message=message, subjects=subjects)
                elif violation.rule == "interface.init-unwritable-integer":
                    issue = ConsistencyIssue(
                        rule="interface.init-unwritable-integer", message=message, subjects=subjects
                    )
                elif violation.rule == "interface.init-on-external-type":
                    # Reached inside an implementation composite; the second subject is the stub.
                    issue = ConsistencyIssue(
                        rule="interface.init-on-external-type", message=message, subjects=stub_subjects
                    )
                else:
                    # Not a catch-all: a ninth rule, or a typo in a helper, would otherwise be emitted under the last
                    # branch's identifier, and no gate would notice.
                    raise AssertionError(f"unmapped init violation rule {violation.rule!r}")
                records.append(issue)

    # Ports after every element, then variables after every port.
    swcs = view.swcs
    records.extend(port_init_records(swcs, interfaces, type_map))
    records.extend(variable_init_records(swcs, type_map))
    return consistency_result(records)


def port_init_records(
    swcs: Sequence[SWComponent],
    interfaces: Sequence[Interface],
    type_map: DataDescription,
) -> list[ConsistencyIssue]:
    """The eight init rules over every port's own ``init``.

    A ``com_specs`` entry's ``init`` supersedes the element's own (``[TPS_SWCT_01182]``) and is judged against **that
    element's declared type**, with :func:`check_init_conformance`'s predicates and rules. The first subject is the
    entry's `init`, the second the declaration that refused the value.

    Skipped, each because another rule owns the entry: a component that is an external stub
    (``swc.external-with-ports``), an entry whose port's interface or element does not resolve
    (``swc.port.com-spec-unknown-element``), and an entry on a queued element, whose com-spec has no ``INIT-VALUE``
    (``swc.port.com-spec-inapplicable``). A duplicated port, interface or element name resolves to its first
    declaration.

    Returns:
        One record per finding, in port order and walk order.
    """
    declared = interface_index(list(interfaces))
    records: list[ConsistencyIssue] = []
    for swc in swcs:
        if swc.external:
            continue
        for port in swc.ports:
            interface = declared.get(port.interface) if port.interface is not None else None
            if not isinstance(interface, SRInterface):
                continue
            elements = first_elements(interface)
            for entry in port.com_specs:
                if entry.init is None:
                    continue
                element = elements.get(entry.data_element)
                if element is None or is_queued(element):
                    continue
                where = _com_spec_subject(swc, port, entry).at(field_step("init"))
                element_type, element_application = prototype_type_ref(element)
                if element_type is None:
                    continue  # a prototype stating no type resolves to nothing: no chain, no walk
                chain = external_type_chain(element_type, type_map, application=element_application)
                if chain is not None:
                    stub_name = chain[-1]
                    reached = (
                        f"'{stub_name}' is an external data type stub"
                        if len(chain) == 1
                        else f"'{element_type}' resolves through the reference chain "
                        + chain_text(chain)
                        + " to an external data type stub"
                    )
                    records.append(
                        ConsistencyIssue(
                            rule="interface.init-on-external-type",
                            message=(
                                f"SWC '{swc.name}' port '{port.name}' states an init value of its own for data "
                                f"element '{entry.data_element}', but the element's type {reached} whose content "
                                "is outside the project: no value can be shaped against it. Remove the port's "
                                "init, or type the element by a declared type."
                            ),
                            subjects=(where, Subject("implementation_types", stub_name)),
                        )
                    )
                    continue
                for violation in _init_violations(
                    entry.init, element_type, type_map, "init", (), application=element_application
                ):
                    message = (
                        f"SWC '{swc.name}' port '{port.name}' com_specs init value for data element "
                        f"'{entry.data_element}' at '{violation.path}' {violation.detail}"
                    )
                    subjects = (
                        where,
                        Subject(
                            "application_types" if element_application else "implementation_types",
                            violation.type_name,
                            violation.steps,
                        ),
                    )
                    # The stub rule names its layer literally, for the emission scan; the literal rule takes `subjects`,
                    # of either layer.
                    stub_subjects = (where, Subject("implementation_types", violation.type_name, violation.steps))
                    # One branch per rule: the contract gates read each emission's rule literal statically.
                    if violation.rule == "interface.init-shape-mismatch":
                        issue = ConsistencyIssue(
                            rule="interface.init-shape-mismatch", message=message, subjects=subjects
                        )
                    elif violation.rule == "interface.init-unknown-enum-literal":
                        issue = ConsistencyIssue(
                            rule="interface.init-unknown-enum-literal", message=message, subjects=subjects
                        )
                    elif violation.rule == "interface.init-record-field-mismatch":
                        issue = ConsistencyIssue(
                            rule="interface.init-record-field-mismatch", message=message, subjects=subjects
                        )
                    elif violation.rule == "interface.init-array-length-mismatch":
                        issue = ConsistencyIssue(
                            rule="interface.init-array-length-mismatch", message=message, subjects=subjects
                        )
                    elif violation.rule == "interface.init-out-of-range":
                        issue = ConsistencyIssue(rule="interface.init-out-of-range", message=message, subjects=subjects)
                    elif violation.rule == "interface.init-string-scalar":
                        issue = ConsistencyIssue(
                            rule="interface.init-string-scalar", message=message, subjects=subjects
                        )
                    elif violation.rule == "interface.init-unwritable-integer":
                        issue = ConsistencyIssue(
                            rule="interface.init-unwritable-integer", message=message, subjects=subjects
                        )
                    elif violation.rule == "interface.init-on-external-type":
                        issue = ConsistencyIssue(
                            rule="interface.init-on-external-type", message=message, subjects=stub_subjects
                        )
                    else:
                        raise AssertionError(f"unmapped init violation rule {violation.rule!r}")
                    records.append(issue)
    return records


def variable_init_records(swcs: Sequence[SWComponent], type_map: DataDescription) -> list[ConsistencyIssue]:
    """The eight init rules over every inter-runnable variable's ``init``.

    A variable's ``init`` is its ``INIT-VALUE`` (``[TPS_SWCT_01637]``), judged against the variable's own declared type
    with :func:`check_init_conformance`'s predicates and rules. The first subject is the variable's `init`, the second
    the declaration that refused the value.

    Skipped, each because another rule owns the variable: a component that is an external stub
    (``swc.external-with-ports``), and a variable whose type does not resolve (``swc.variable.undeclared-type``).

    Returns:
        One record per finding, in component order, variable order and walk order.
    """
    records: list[ConsistencyIssue] = []
    for swc in swcs:
        if swc.external:
            continue
        for variable in swc.variables:
            if variable.init is None:
                continue
            where = Subject("swc", swc.name, (field_step("variables"), member_step(variable.name), field_step("init")))
            variable_type, variable_application = prototype_type_ref(variable)
            if variable_type is None:
                continue  # a prototype stating no type resolves to nothing: no chain, no walk
            chain = external_type_chain(variable_type, type_map, application=variable_application)
            if chain is not None:
                stub_name = chain[-1]
                reached = (
                    f"'{stub_name}' is an external data type stub"
                    if len(chain) == 1
                    else f"'{variable_type}' resolves through the reference chain "
                    + chain_text(chain)
                    + " to an external data type stub"
                )
                records.append(
                    ConsistencyIssue(
                        rule="interface.init-on-external-type",
                        message=(
                            f"SWC '{swc.name}' variable '{variable.name}' declares an init value, but its type "
                            f"{reached} whose content is outside the project: no value can be shaped against it. "
                            "Remove the init, or type the variable by a declared type."
                        ),
                        subjects=(where, Subject("implementation_types", stub_name)),
                    )
                )
                continue
            for violation in _init_violations(
                variable.init, variable_type, type_map, "init", (), application=variable_application
            ):
                message = (
                    f"SWC '{swc.name}' variable '{variable.name}' init value at '{violation.path}' {violation.detail}"
                )
                subjects = (
                    where,
                    Subject(
                        "application_types" if variable_application else "implementation_types",
                        violation.type_name,
                        violation.steps,
                    ),
                )
                stub_subjects = (where, Subject("implementation_types", violation.type_name, violation.steps))
                # One branch per rule: the contract gates read each emission's rule literal statically.
                if violation.rule == "interface.init-shape-mismatch":
                    issue = ConsistencyIssue(rule="interface.init-shape-mismatch", message=message, subjects=subjects)
                elif violation.rule == "interface.init-unknown-enum-literal":
                    issue = ConsistencyIssue(
                        rule="interface.init-unknown-enum-literal", message=message, subjects=subjects
                    )
                elif violation.rule == "interface.init-record-field-mismatch":
                    issue = ConsistencyIssue(
                        rule="interface.init-record-field-mismatch", message=message, subjects=subjects
                    )
                elif violation.rule == "interface.init-array-length-mismatch":
                    issue = ConsistencyIssue(
                        rule="interface.init-array-length-mismatch", message=message, subjects=subjects
                    )
                elif violation.rule == "interface.init-out-of-range":
                    issue = ConsistencyIssue(rule="interface.init-out-of-range", message=message, subjects=subjects)
                elif violation.rule == "interface.init-string-scalar":
                    issue = ConsistencyIssue(rule="interface.init-string-scalar", message=message, subjects=subjects)
                elif violation.rule == "interface.init-unwritable-integer":
                    issue = ConsistencyIssue(
                        rule="interface.init-unwritable-integer", message=message, subjects=subjects
                    )
                elif violation.rule == "interface.init-on-external-type":
                    issue = ConsistencyIssue(
                        rule="interface.init-on-external-type", message=message, subjects=stub_subjects
                    )
                else:
                    raise AssertionError(f"unmapped init violation rule {violation.rule!r}")
                records.append(issue)
    return records


def quoted_list(names: Sequence[str]) -> str:
    """``'A'``, ``'A' and 'B'``, ``'A', 'B' and 'C'`` -- *names* quoted, in order, each once."""
    quoted = list(dict.fromkeys(f"'{name}'" for name in names))
    if len(quoted) == 1:
        return quoted[0]
    return f"{', '.join(quoted[:-1])} and {quoted[-1]}"


def first_elements(interface: SRInterface) -> dict[str, DataElement]:
    """*interface*'s data elements by name, **first** declaration winning (``name.duplicate-data-element``)."""
    elements: dict[str, DataElement] = {}
    for element in interface.data_elements:
        elements.setdefault(element.name, element)
    return elements


def _receiver_only_attributes(entry: PortComSpec) -> list[str]:
    """The receiver-only attributes *entry* states, in the kind's declaration order.

    Named attribute reads, never ``getattr``: the coverage field oracle binds a
    read to the attribute it names and abstains on a computed one.
    """
    return [
        name
        for name, stated in (
            ("enable_update", entry.enable_update is not None),
            ("alive_timeout", entry.alive_timeout is not None),
            ("handle_never_received", entry.handle_never_received is not None),
            ("handle_timeout_type", entry.handle_timeout_type is not None),
            ("queue_length", entry.queue_length is not None),
        )
        if stated
    ]


def _com_spec_subject(swc: SWComponent, port: Port, entry: PortComSpec) -> Subject:
    """One ``com_specs`` entry of *port*, addressed by its identity field ``data_element``."""
    return Subject(
        "swc",
        swc.name,
        (field_step("ports"), member_step(port.name), field_step("com_specs"), member_step(entry.data_element)),
    )


def _unresolvable_com_spec_reason(port: Port, interface: Interface | None) -> str | None:
    """Why no entry of *port*'s ``com_specs`` can name a data element, or ``None`` when one can."""
    if port.interface is None:
        return "the port declares no interface"
    if interface is None:
        return f"its interface '{port.interface}' is not declared"
    if isinstance(interface, ExternalInterface):
        # An entry resolves against the stub's `data_elements` in `port_com_spec_records`.
        return None
    if isinstance(interface, CSInterface):
        return (
            f"its interface '{port.interface}' is client/server, and a com-spec entry names a data element of a "
            "sender/receiver interface"
        )
    return None


#: Why a provide port's com-spec refuses a receiver-only attribute, for a declared interface's port and a stub's.
_SENDER_HAS_NO_RECEIVER_PLACE = "a provide port's sender com-spec has no place for a receiver attribute"


#: Why ``rule:swc.port.com-spec-on-stub`` refuses each attribute it names.
_ON_STUB_WHY = {
    "init": "an init value has no type to be shaped against",
    "queue_length": "the com-spec of a stub's member is never queued, so it has no queue length",
}


def port_com_spec_records(swcs: Sequence[SWComponent], interfaces: Sequence[Interface]) -> list[ConsistencyIssue]:
    """``rule:swc.port.com-spec-unknown-element``, ``…-inapplicable`` and ``rule:swc.port.com-spec-on-stub``.

    One finding per ``com_specs`` entry, in read order, on every component that is not an external stub (a stub's port
    with ``com_specs`` is ``swc.external-with-ports``' finding):

    * **unknown element** — the entry's ``data_element`` names no data element of the port's sender/receiver interface,
      or no name an external interface stub lists in its ``data_elements``, or the port's interface is client/server or
      undeclared;
    * **inapplicable** — the entry states an attribute with no place on the port's side or the element's policy: a
      receiver-only attribute on a ``provide`` port, ``queue_length`` on a non-queued element, or one of the four
      non-queued receiver attributes on a queued one (``[TPS_SWCT_01219]``; Tables 4.61 and 4.62 of R4.2.2) -- and an
      ``init`` on a queued element of either side, because neither queued com-spec carries an ``INIT-VALUE`` in either
      schema;
    * **on a stub** — an entry on a port typed by an external interface stub meets the direction half of the
      inapplicable rule, and ``swc.port.com-spec-on-stub`` refuses its ``init`` and, on a require port, its
      ``queue_length``.

    What this does not reach: ``handle_out_of_range`` and ``uses_end_to_end_protection``, which both directions and both
    policies carry, are never inapplicable; a duplicated port or element name resolves to the first declaration; whether
    a stated ``init`` fits its element's type is :func:`port_init_records`'.
    """
    declared = interface_index(list(interfaces))
    records: list[ConsistencyIssue] = []
    for swc in swcs:
        if swc.external:
            continue
        for port in swc.ports:
            if not port.com_specs:
                continue
            interface = declared.get(port.interface) if port.interface is not None else None
            unresolvable = _unresolvable_com_spec_reason(port, interface)
            stub = interface if isinstance(interface, ExternalInterface) else None
            elements = first_elements(interface) if isinstance(interface, SRInterface) else {}
            for entry in port.com_specs:
                records.extend(_com_spec_entry_records(swc, port, entry, stub, elements, unresolvable))
    return records


def _unknown_com_spec_element(
    port: Port,
    entry: PortComSpec,
    stub: ExternalInterface | None,
    element: DataElement | None,
    unresolvable: str | None,
) -> str | None:
    """Why *entry* of *port* names no data element it can resolve to, or ``None`` when it resolves."""
    # Through a stub the entry resolves against the stub's `data_elements` alone.
    if stub is not None:
        return (
            None
            if entry.data_element in stub_member_names(stub, "element")
            else stub_member_miss(stub, "element", entry.data_element)
        )
    if unresolvable is not None or element is None:
        return unresolvable or (
            f"sender/receiver interface '{port.interface}' declares no data element '{entry.data_element}'"
        )
    return None


def _declared_com_spec_misplacement(port: Port, entry: PortComSpec, element: DataElement) -> tuple[list[str], str]:
    """The attributes *entry* states with no place on *port*'s side or *element*'s policy, and why."""
    stated = _receiver_only_attributes(entry)
    # A port's own `init` has a place in a
    # non-queued com-spec of either side and in no queued one.
    init_on_queued = entry.init is not None and is_queued(element)
    if port.direction == "provide":
        misplaced = stated
        why = _SENDER_HAS_NO_RECEIVER_PLACE
        if init_on_queued:
            why = (
                f"{why}, and the element is queued, and a queued sender com-spec has no place for an init value"
                if misplaced
                else "the element is queued, and a queued sender com-spec has no place for an init value"
            )
            misplaced = [*misplaced, "init"]
    elif is_queued(element):
        misplaced = [name for name in stated if name != "queue_length"]
        if init_on_queued:
            misplaced = [*misplaced, "init"]
        why = "the element is queued, and a queued receiver com-spec has no place for a non-queued one"
    else:
        misplaced = [name for name in stated if name == "queue_length"]
        why = "the element is not queued, and a non-queued receiver com-spec has no queue length"
    return misplaced, why


def _com_spec_entry_records(
    swc: SWComponent,
    port: Port,
    entry: PortComSpec,
    stub: ExternalInterface | None,
    elements: Mapping[str, DataElement],
    unresolvable: str | None,
) -> list[ConsistencyIssue]:
    """The three rules of :func:`port_com_spec_records` over one ``com_specs`` entry of *port*.

    *stub* is the port's external interface stub, *elements* its declared
    interface's data elements (first declaration winning), and *unresolvable*
    why no entry of the port can name one (:func:`_unresolvable_com_spec_reason`).
    """
    records: list[ConsistencyIssue] = []
    subject = _com_spec_subject(swc, port, entry)
    element = elements.get(entry.data_element)
    unknown = _unknown_com_spec_element(port, entry, stub, element, unresolvable)
    if unknown is not None:
        records.append(
            ConsistencyIssue(
                rule="swc.port.com-spec-unknown-element",
                message=(
                    f"SWC '{swc.name}' port '{port.name}' declares com_specs for data element "
                    f"'{entry.data_element}', but {unknown}."
                ),
                subjects=(subject,),
            )
        )
        return records
    if stub is not None:
        # A listed element meets the direction half of the inapplicable rule; the policy half abstains, as a stub holds
        # no `sw_impl_policy`. On a provide port `queue_length` is the direction half's.
        misplaced = _receiver_only_attributes(entry) if port.direction == "provide" else []
        if misplaced:
            records.append(
                ConsistencyIssue(
                    rule="swc.port.com-spec-inapplicable",
                    message=(
                        f"SWC '{swc.name}' provide port '{port.name}' states {quoted_list(misplaced)} "
                        f"for data element '{entry.data_element}', but {_SENDER_HAS_NO_RECEIVER_PLACE}."
                    ),
                    subjects=(
                        subject,
                        Subject(
                            "swc",
                            swc.name,
                            (field_step("ports"), member_step(port.name), field_step("direction")),
                        ),
                    ),
                )
            )
        refused = [
            name
            for name, stated in (
                ("init", entry.init is not None),
                ("queue_length", entry.queue_length is not None and port.direction == "require"),
            )
            if stated
        ]
        if refused:
            records.append(
                ConsistencyIssue(
                    rule="swc.port.com-spec-on-stub",
                    message=(
                        f"SWC '{swc.name}' {port.direction} port '{port.name}' states "
                        f"{quoted_list(refused)} for data element '{entry.data_element}' of external "
                        f"interface stub '{stub.name}', which lists the element by name only: "
                        f"{' and '.join(_ON_STUB_WHY[name] for name in refused)}. Remove "
                        f"{'them' if len(refused) > 1 else 'it'} from the entry."
                    ),
                    subjects=tuple(subject.at(field_step(name)) for name in refused),
                )
            )
        return records
    if element is None:
        return records  # never: `_unknown_com_spec_element` names every entry of a declared interface without one
    misplaced, why = _declared_com_spec_misplacement(port, entry, element)
    if misplaced:
        records.append(
            ConsistencyIssue(
                rule="swc.port.com-spec-inapplicable",
                message=(
                    f"SWC '{swc.name}' {port.direction} port '{port.name}' states "
                    f"{quoted_list(misplaced)} for data element '{entry.data_element}', but {why}."
                ),
                subjects=(
                    subject,
                    Subject(
                        "interfaces",
                        port.interface,
                        (
                            field_step("data_elements"),
                            member_step(element.name),
                            field_step("sw_impl_policy"),
                        ),
                    ),
                ),
            )
        )
    return records
