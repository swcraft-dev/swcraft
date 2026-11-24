"""The data type rules: references, base types, cycles, mapping sets and application types.

Holds the checks over the data type layers and the interfaces that type their prototypes:
reference resolution and reference roles, duplicate names and generated-name collisions,
external data type stubs (:func:`check_data_type_references`), base types, reference cycles,
mapping sets, and the comparisons of application types, together with the shared helpers
(the stub reference-pair grammar, a prototype's stated type).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import NamedTuple

from swcraft_tools.arxml_version import ROOT_PACKAGE_NAMES
from swcraft_tools.data_description import APPLICATION_DATA_TYPE_CLASSES, DataDescription
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.schema import (
    ApplicationDataType,
    ArrayDataType,
    BaseDataType,
    CSArgument,
    CSInterface,
    DataElement,
    DataType,
    EnumDataType,
    ExternalDataType,
    ImplementationArrayDataType,
    ImplementationRecordDataType,
    Interface,
    MappingSet,
    PointerDataType,
    RecordDataType,
    ReferenceDataType,
    SRInterface,
    SWComponent,
    ValueDataType,
    Variable,
)
from swcraft_tools.validator.description_elements import (
    chain_text,
    data_description_package_path,
    data_type_layer,
    duplicates,
    implementation_end,
)
from swcraft_tools.validator.generated_names import (
    RESERVED_COMPU_SUFFIX,
    RESERVED_RANGE_SUFFIX,
    RESERVED_UNIT_SUFFIX,
    generated_data_type_forms,
    generates_compu_method,
)
from swcraft_tools.validator.model_view import ModelInput, as_view
from swcraft_tools.validator.results import ConsistencyIssue, ConsistencyResult, consistency_result, deduplicated

#: The ``DEST`` values an ``external_type`` stub may carry: ``IMPLEMENTATION-DATA-TYPE``
#: alone, the one value the reference-base role's XSD type allows and the one concrete
#: implementation value of a data prototype's ``TYPE-TREF``. An application type is never stubbed,
#: because nothing could supply the data type map ``[TPS_SWCT_01570]`` / ``[constr_1004]`` require for one.
EXTERNAL_DATA_TYPE_DESTS: frozenset[str] = frozenset({"IMPLEMENTATION-DATA-TYPE"})


def declared_type(data_types: DataDescription, name: str | None, *, application: bool) -> DataType | None:
    """The type *name* names on the side *application* says, or ``None``.

    A walk entered from a prototype's type stays on that side: an application type
    names only application types, an implementation type only implementation types
    and, through a value type's ``base``, a base type.
    """
    return data_types.application(name) if application else data_types.implementation(name)


def prototype_type_ref(prototype: DataElement | CSArgument | Variable) -> tuple[str | None, bool]:
    """The type a data element, an argument or a variable states, as ``(name, application)``.

    ``application_type`` wins when set; the schema admits exactly one of the two.
    """
    if prototype.application_type is not None:
        return prototype.application_type, True
    return prototype.implementation_type, False


def prototype_type_field(prototype: DataElement | CSArgument | Variable) -> str:
    """The name of the field that holds *prototype*'s type: ``application_type`` or ``implementation_type``."""
    return "application_type" if prototype.application_type is not None else "implementation_type"


#: The shape of an AUTOSAR reference PATH, transcribed from the ``Ref`` simple
#: type's own ``xsd:pattern`` in ``AUTOSAR_4-2-2.xsd`` and ``AUTOSAR_4-3-0.xsd``
#: (both carry it byte-identically): an optional leading ``/`` and then
#: ``Identifier``-shaped segments joined by ``/``. The schema's own grammar, because
#: an external stub's ``external_ref`` is written verbatim: a looser pattern lets
#: ``generate arxml`` exit 0 on a document a licensed XSD refuses, and with no licensed
#: schema ``arxml_validation``'s structural fallback ships it.
REF_PATH = re.compile(r"/?[a-zA-Z][a-zA-Z0-9_]{0,127}(?:/[a-zA-Z][a-zA-Z0-9_]{0,127})*")


#: How a refusal names the three root packages of
#: :data:`~swcraft_tools.arxml_version.ROOT_PACKAGE_NAMES`.
OWN_PACKAGE_PHRASE = "a package the generated ARXML declares this project's own elements in"


def own_package_of(external_ref: str) -> str | None:
    """The generated root package *external_ref* lies inside, as ``/Name``, or ``None``.

    A stub's reference into one of
    :data:`~swcraft_tools.arxml_version.ROOT_PACKAGE_NAMES` would resolve, in the
    generated document, to a declared element, while every model rule treats it as
    foreign. The judgement is the reference's **first segment** alone, with or without
    the leading ``/``, compared **case-insensitively**, because a sibling package
    differing only in case is not a distinct namespace to every AUTOSAR tool. A package
    of that name further down a path (``/Foreign/DataTypes/X``) is not refused. Answers
    only for a path ``REF_PATH`` accepts.
    """
    first = external_ref.lstrip("/").split("/", 1)[0].casefold()
    return next((f"/{name}" for name in ROOT_PACKAGE_NAMES if name.casefold() == first), None)


def own_package_problem(external_ref: str) -> str | None:
    """The sentence tail, no full stop, refusing *external_ref* for :func:`own_package_of`, or ``None``."""
    package = own_package_of(external_ref)
    if package is None:
        return None
    return (
        f"declares external_ref '{external_ref}', which lies inside {package}, {OWN_PACKAGE_PHRASE}: the "
        "reference would resolve to a declared element, or to one declared later, rather than to the source's element"
    )


def external_pair_problems(
    external_ref: str, external_dest: str, admitted: AbstractSet[str], what: str
) -> list[tuple[str, tuple[str, ...]]]:
    """What is wrong with a stub's reference pair: ``(tail without full stop, fields)``, in field order.

    A blank half is one finding naming every blank field; otherwise the path and the
    ``DEST`` are two independent findings. A well-formed path inside one of the
    generated root packages is the path's finding too.
    """
    blank = tuple(
        name for name, value in (("external_ref", external_ref), ("external_dest", external_dest)) if not value.strip()
    )
    if blank:
        return [
            (
                f"leaves {' and '.join(blank)} blank; a stub is emitted as its source's reference path and "
                "DEST verbatim, and both must be there",
                blank,
            )
        ]
    problems: list[tuple[str, tuple[str, ...]]] = []
    if not REF_PATH.fullmatch(external_ref):
        problems.append(
            (
                f"declares external_ref '{external_ref}', which is not an AUTOSAR reference path: a reference is "
                "Identifier-shaped segments joined by '/', such as '/Foreign/Pkg/Element'",
                ("external_ref",),
            )
        )
    elif (inside := own_package_problem(external_ref)) is not None:
        problems.append((inside, ("external_ref",)))
    if external_dest not in admitted:
        problems.append(
            (
                f"declares external_dest '{external_dest}', which is not {what}: one of {', '.join(sorted(admitted))}",
                ("external_dest",),
            )
        )
    return problems


#: The rule a declared name colliding with each generated suffix is reported under.
APPLICATION_FORM_RULES: dict[str, str] = {
    RESERVED_UNIT_SUFFIX: "data-type.unit-name-collision",
    RESERVED_COMPU_SUFFIX: "data-type.compu-name-collision",
    RESERVED_RANGE_SUFFIX: "data-type.range-name-collision",
}


#: The ``_CompuMethod`` ``SHORT-NAME`` suffix the ARXML writer derives from a declared
#: enumeration's name. Restated because ``validator`` must stay importable without
#: ``writer``. It is *reserved*: a declared type of that name collides with the
#: generated one.
GENERATED_COMPU_METHOD_SUFFIX = "_CompuMethod"


def _generated_form_owner(dt: DataType) -> str:
    """The words a collision message names the owner of a generated form by."""
    return "value type" if isinstance(dt, ValueDataType) else "application type"


def check_data_type_references(model_root: ModelInput) -> ConsistencyResult:
    """Check that all type references resolve to declared data types of the right kind.

    Mints ``data-type.duplicate-name``, ``data-type.compu-method-name-collision``, ``data-type.unit-name-collision``,
    ``data-type.compu-name-collision``, ``data-type.range-name-collision``,
    ``interface.data-element-undeclared-type``, ``interface.argument-undeclared-type``,
    ``data-type.value-type-undeclared-base``, ``data-type.reference-type-undeclared-base``,
    ``data-type.record-field-undeclared-type``, ``data-type.record-duplicate-field-name``,
    ``data-type.array-undeclared-element-type``, ``data-type.enum-duplicate-literal-name``,
    ``data-type.enum-duplicate-literal-value``, ``data-type.implementation-element-undeclared-type``,
    ``data-type.implementation-record-duplicate-element-name``,
    ``data-type.implementation-array-undeclared-element-type``, ``data-type.pointer-undeclared-target`` and
    ``data-type.external-incomplete-reference``; the model contract describes each.

    Beyond the contract: a reference type's ``base`` must also be a value type, another reference type, an
    implementation record, an implementation array or a pointer, a kind checked only once the name resolves; a name
    of another layer in a role is an undeclared name of that role's layer. A generated name is reserved only for a
    type declaring the field it is emitted for, and only within its package. An external data type stub resolves as a
    prototype's type and a reference type's ``base``, never as a value type's ``base`` or a composite's member, and
    generates no name. A reference cycle is :func:`check_data_type_cycles`' finding.

    Args:
        model_root: The model root, or a view of it.

    Returns:
        ConsistencyResult with consistent=True only when all references resolve.
    """
    view = as_view(model_root)
    interfaces = view.interfaces
    description = view.data_description
    data_types = description.data_types

    records = _duplicate_data_type_records(data_types)
    # The role checks resolve a duplicated name to its FIRST declaration.
    records.extend(_compu_method_collision_records(data_types))
    records.extend(_generated_form_collision_records(data_types))
    records.extend(_interface_type_records(interfaces, description))
    records.extend(_type_dependency_records(data_types, description))
    return consistency_result(records)


def _duplicate_data_type_records(data_types: Sequence[DataType]) -> list[ConsistencyIssue]:
    """``data-type.duplicate-name``: a name declared twice inside one data type layer.

    Types of two layers may share a name.
    """
    records: list[ConsistencyIssue] = []
    _seen: dict[tuple[str, str], int] = {}
    for dt in data_types:
        _key = (data_type_layer(dt), dt.name)
        _seen[_key] = _seen.get(_key, 0) + 1
    for (dt_layer, dt_name), count in _seen.items():
        if count > 1:
            records.append(
                ConsistencyIssue(
                    rule="data-type.duplicate-name",
                    message=f"Duplicate data type name '{dt_name}' declared in multiple files.",
                    subjects=(
                        Subject(
                            "base_types"
                            if dt_layer == "base_types"
                            else "application_types"
                            if dt_layer == "application_types"
                            else "implementation_types",
                            dt_name,
                        ),
                    ),
                )
            )
    return records


def _compu_method_collision_records(data_types: Sequence[DataType]) -> list[ConsistencyIssue]:
    """``data-type.compu-method-name-collision``: a declared type named after an enum's generated compu method.

    Only within the enum's package, and only for an enum stating ``literals``. An
    external data type stub emits nothing, so it collides with nothing.
    """
    records: list[ConsistencyIssue] = []
    _enum_packages = {dt.name: data_description_package_path(dt) for dt in data_types if generates_compu_method(dt)}
    for dt in data_types:
        if isinstance(dt, ExternalDataType):
            continue
        if dt.name.endswith(GENERATED_COMPU_METHOD_SUFFIX):
            base = dt.name[: -len(GENERATED_COMPU_METHOD_SUFFIX)]
            if _enum_packages.get(base) == data_description_package_path(dt):
                records.append(
                    ConsistencyIssue(
                        rule="data-type.compu-method-name-collision",
                        message=(
                            f"Data type name '{dt.name}' conflicts with auto-generated CompuMethod name for enum "
                            f"'{base}'."
                        ),
                        subjects=(Subject("application_types", dt.name),),
                    )
                )
    return records


def _generated_form_collision_records(data_types: Sequence[DataType]) -> list[ConsistencyIssue]:
    """The three collisions with a name an application type, or a value type declaring a bound, generates.

    ``data-type.unit-name-collision``, ``data-type.compu-name-collision`` and
    ``data-type.range-name-collision``, one element at a time, in that order.
    """
    # Three blocks rather than a loop: the binding gate reads the bound callable's own
    # source for each rule identifier.
    records: list[ConsistencyIssue] = []
    _generated_form_stems: dict[str, dict[str, dict[str, str]]] = {suffix: {} for suffix in APPLICATION_FORM_RULES}
    for dt in data_types:
        for suffix in generated_data_type_forms(dt):
            _generated_form_stems[suffix].setdefault(data_description_package_path(dt), {})[dt.name] = (
                _generated_form_owner(dt)
            )
    for dt in data_types:
        if isinstance(dt, ExternalDataType):
            continue
        _package = data_description_package_path(dt)
        _stems = {suffix: by_package.get(_package, {}) for suffix, by_package in _generated_form_stems.items()}
        if dt.name.endswith(RESERVED_UNIT_SUFFIX) and (
            (stem := dt.name[: -len(RESERVED_UNIT_SUFFIX)]) in _stems[RESERVED_UNIT_SUFFIX]
        ):
            records.append(
                ConsistencyIssue(
                    rule="data-type.unit-name-collision",
                    message=(
                        f"Data type name '{dt.name}' conflicts with the auto-generated UNIT name for application "
                        f"type '{stem}'."
                    ),
                    subjects=(Subject("application_types", dt.name),),
                )
            )
        if dt.name.endswith(RESERVED_COMPU_SUFFIX) and (
            (stem := dt.name[: -len(RESERVED_COMPU_SUFFIX)]) in _stems[RESERVED_COMPU_SUFFIX]
        ):
            records.append(
                ConsistencyIssue(
                    rule="data-type.compu-name-collision",
                    message=(
                        f"Data type name '{dt.name}' conflicts with the auto-generated COMPU-METHOD name for "
                        f"application type '{stem}'."
                    ),
                    subjects=(Subject("application_types", dt.name),),
                )
            )
        if dt.name.endswith(RESERVED_RANGE_SUFFIX) and (
            (stem := dt.name[: -len(RESERVED_RANGE_SUFFIX)]) in _stems[RESERVED_RANGE_SUFFIX]
        ):
            records.append(
                ConsistencyIssue(
                    rule="data-type.range-name-collision",
                    message=(
                        f"Data type name '{dt.name}' conflicts with the auto-generated DATA-CONSTR name for "
                        f"{_stems[RESERVED_RANGE_SUFFIX][stem]} '{stem}'."
                    ),
                    subjects=(
                        Subject(
                            "application_types"
                            if isinstance(dt, APPLICATION_DATA_TYPE_CLASSES)
                            else "implementation_types",
                            dt.name,
                        ),
                    ),
                )
            )
    return records


def _interface_type_records(interfaces: Sequence[Interface], description: DataDescription) -> list[ConsistencyIssue]:
    """Checks 1 and 2: every SR data element's and every CS argument's type resolves on the side its field names."""
    records: list[ConsistencyIssue] = []
    for iface in interfaces:
        if isinstance(iface, SRInterface):
            for de in iface.data_elements:
                de_type, de_application = prototype_type_ref(de)
                if declared_type(description, de_type, application=de_application) is None:
                    records.append(
                        ConsistencyIssue(
                            rule="interface.data-element-undeclared-type",
                            message=(
                                f"Interface '{iface.name}' element '{de.name}' references undeclared type '{de_type}'."
                            ),
                            subjects=(
                                Subject(
                                    "interfaces",
                                    iface.name,
                                    (
                                        field_step("data_elements"),
                                        member_step(de.name),
                                        field_step(prototype_type_field(de)),
                                    ),
                                ),
                            ),
                            target=de_type,
                        )
                    )
        elif isinstance(iface, CSInterface):
            for op in iface.operations:
                for arg in op.arguments:
                    arg_type, arg_application = prototype_type_ref(arg)
                    if declared_type(description, arg_type, application=arg_application) is None:
                        records.append(
                            ConsistencyIssue(
                                rule="interface.argument-undeclared-type",
                                message=(
                                    f"Interface '{iface.name}' operation '{op.name}' "
                                    f"argument '{arg.name}' references "
                                    f"undeclared type '{arg_type}'."
                                ),
                                subjects=(
                                    Subject(
                                        "interfaces",
                                        iface.name,
                                        (
                                            field_step("operations"),
                                            member_step(op.name),
                                            field_step("arguments"),
                                            member_step(arg.name),
                                            field_step(prototype_type_field(arg)),
                                        ),
                                    ),
                                ),
                                target=arg_type,
                            )
                        )
    return records


def _type_dependency_records(data_types: Sequence[DataType], description: DataDescription) -> list[ConsistencyIssue]:
    """Checks 3-8, 10 and 11: each type's own references and member names, one type at a time, by its kind."""
    records: list[ConsistencyIssue] = []
    for dt in data_types:
        if isinstance(dt, ValueDataType):
            records.extend(_value_type_records(dt, description))
        elif isinstance(dt, ReferenceDataType):
            records.extend(_reference_type_records(dt, description))
        elif isinstance(dt, RecordDataType):
            records.extend(_record_type_records(dt, description))
        elif isinstance(dt, ArrayDataType):
            records.extend(_array_type_records(dt, description))
        elif isinstance(dt, EnumDataType):
            records.extend(_enum_type_records(dt))
        elif isinstance(dt, ImplementationRecordDataType):
            records.extend(_implementation_record_records(dt, description))
        elif isinstance(dt, ImplementationArrayDataType):
            records.extend(_implementation_array_records(dt, description))
        elif isinstance(dt, PointerDataType):
            records.extend(_pointer_type_records(dt, description))
        elif isinstance(dt, ExternalDataType):
            records.extend(_external_type_records(dt))
    return records


def _value_type_records(dt: ValueDataType, description: DataDescription) -> list[ConsistencyIssue]:
    """``data-type.value-type-undeclared-base``: a value type's ``base`` names no declared base type."""
    element = Subject("implementation_types", dt.name)
    if description.base(dt.base) is not None:
        return []
    return [
        ConsistencyIssue(
            rule="data-type.value-type-undeclared-base",
            message=(
                f"Value type '{dt.name}' base '{dt.base}' is not declared in the base_types layer; "
                f"a value type's `base` must name a declared base type."
            ),
            subjects=(element.at(field_step("base")),),
            target=dt.base,
        )
    ]


def _reference_type_records(dt: ReferenceDataType, description: DataDescription) -> list[ConsistencyIssue]:
    """``data-type.reference-type-undeclared-base``: a reference type's ``base`` names no implementation type.

    Every implementation kind is an admitted base, so a resolved base needs no kind check.
    """
    element = Subject("implementation_types", dt.name)
    if description.implementation(dt.base) is not None:
        return []
    return [
        ConsistencyIssue(
            rule="data-type.reference-type-undeclared-base",
            message=(
                f"Reference type '{dt.name}' base '{dt.base}' is not declared in the "
                "implementation_types layer; "
                f"a reference type's `base` must name a declared value_type, reference_type, "
                f"implementation_record, implementation_array, pointer or external data type stub."
            ),
            subjects=(element.at(field_step("base")),),
            target=dt.base,
        )
    ]


def _record_type_records(dt: RecordDataType, description: DataDescription) -> list[ConsistencyIssue]:
    """A record's field types resolve among the application types, and its field names are unique."""
    owner = Subject("application_types", dt.name)
    records: list[ConsistencyIssue] = []
    for f in dt.fields:
        if description.application(f.type) is None:
            records.append(
                ConsistencyIssue(
                    rule="data-type.record-field-undeclared-type",
                    message=(f"Record '{dt.name}' field '{f.name}' references undeclared type '{f.type}'."),
                    subjects=(owner.at(field_step("fields"), member_step(f.name), field_step("type")),),
                    target=f.type,
                )
            )
    _field_names = [f.name for f in dt.fields]
    _field_name_dups = {n for n in _field_names if _field_names.count(n) > 1}
    if _field_name_dups:
        records.append(
            ConsistencyIssue(
                rule="data-type.record-duplicate-field-name",
                message=(
                    f"Record '{dt.name}' has duplicate field name(s) "
                    f"{sorted(_field_name_dups)}; field names must be unique within a record."
                ),
                subjects=tuple(owner.at(field_step("fields"), member_step(name)) for name in sorted(_field_name_dups)),
            )
        )
    return records


def _array_type_records(dt: ArrayDataType, description: DataDescription) -> list[ConsistencyIssue]:
    """``data-type.array-undeclared-element-type``: an array's ``element_type`` names no application type."""
    owner = Subject("application_types", dt.name)
    if description.application(dt.element_type) is not None:
        return []
    return [
        ConsistencyIssue(
            rule="data-type.array-undeclared-element-type",
            message=(
                f"Array '{dt.name}' element_type '{dt.element_type}' is not declared in the application_types layer."
            ),
            subjects=(owner.at(field_step("element_type")),),
            target=dt.element_type,
        )
    ]


def _enum_type_records(dt: EnumDataType) -> list[ConsistencyIssue]:
    """An enumeration's literal names and literal values are each unique; one naming a texttable states none."""
    owner = Subject("application_types", dt.name)
    records: list[ConsistencyIssue] = []
    _lit_names = [lit.name for lit in dt.literals or ()]
    _lit_name_dups = {n for n in _lit_names if _lit_names.count(n) > 1}
    if _lit_name_dups:
        records.append(
            ConsistencyIssue(
                rule="data-type.enum-duplicate-literal-name",
                message=(
                    f"Enum '{dt.name}' has duplicate literal name(s) "
                    f"{sorted(_lit_name_dups)}; literal names must be unique within an enum."
                ),
                subjects=tuple(owner.at(field_step("literals"), member_step(name)) for name in sorted(_lit_name_dups)),
            )
        )
    _lit_values = [lit.value for lit in dt.literals or ()]
    _lit_value_dups = {v for v in _lit_values if _lit_values.count(v) > 1}
    if _lit_value_dups:
        records.append(
            ConsistencyIssue(
                rule="data-type.enum-duplicate-literal-value",
                message=(
                    f"Enum '{dt.name}' has duplicate literal value(s) "
                    f"{sorted(_lit_value_dups)}; literal values must be unique within an enum."
                ),
                subjects=tuple(
                    owner.at(field_step("literals"), member_step(literal.name))
                    for literal in dt.literals or ()
                    if literal.value in _lit_value_dups
                ),
            )
        )
    return records


def _implementation_record_records(
    dt: ImplementationRecordDataType, description: DataDescription
) -> list[ConsistencyIssue]:
    """Each element names a declared implementation type, and the element names are unique."""
    element = Subject("implementation_types", dt.name)
    records: list[ConsistencyIssue] = []
    for implementation_element in dt.elements:
        address = element.at(field_step("elements"), member_step(implementation_element.name))
        if description.implementation(implementation_element.type) is None:
            records.append(
                ConsistencyIssue(
                    rule="data-type.implementation-element-undeclared-type",
                    message=(
                        f"Implementation record '{dt.name}' element '{implementation_element.name}' "
                        f"references undeclared type '{implementation_element.type}'."
                    ),
                    subjects=(address.at(field_step("type")),),
                    target=implementation_element.type,
                )
            )
    _element_names = [implementation_element.name for implementation_element in dt.elements]
    _element_name_dups = {n for n in _element_names if _element_names.count(n) > 1}
    if _element_name_dups:
        records.append(
            ConsistencyIssue(
                rule="data-type.implementation-record-duplicate-element-name",
                message=(
                    f"Implementation record '{dt.name}' has duplicate element name(s) "
                    f"{sorted(_element_name_dups)}; element names must be unique within an "
                    f"implementation record."
                ),
                subjects=tuple(
                    element.at(field_step("elements"), member_step(name)) for name in sorted(_element_name_dups)
                ),
            )
        )
    return records


def _implementation_array_records(
    dt: ImplementationArrayDataType, description: DataDescription
) -> list[ConsistencyIssue]:
    """``data-type.implementation-array-undeclared-element-type``: its ``element_type`` names no implementation type."""
    element = Subject("implementation_types", dt.name)
    if description.implementation(dt.element_type) is not None:
        return []
    return [
        ConsistencyIssue(
            rule="data-type.implementation-array-undeclared-element-type",
            message=(
                f"Implementation array '{dt.name}' element_type '{dt.element_type}' is not declared in "
                f"the implementation_types layer."
            ),
            subjects=(element.at(field_step("element_type")),),
            target=dt.element_type,
        )
    ]


def _pointer_type_records(dt: PointerDataType, description: DataDescription) -> list[ConsistencyIssue]:
    """``data-type.pointer-undeclared-target``: a pointer's ``target`` names no implementation type.

    A pointer to a pointer names another declared pointer: ``[constr_1254]``'s
    option 1. A pointer graph that comes back to itself is ``data-type.cycle``'s.
    """
    element = Subject("implementation_types", dt.name)
    if description.implementation(dt.target) is not None:
        return []
    return [
        ConsistencyIssue(
            rule="data-type.pointer-undeclared-target",
            message=(f"Pointer '{dt.name}' target '{dt.target}' is not declared in the implementation_types layer."),
            subjects=(element.at(field_step("target")),),
            target=dt.target,
        )
    ]


def _external_type_records(dt: ExternalDataType) -> list[ConsistencyIssue]:
    """``data-type.external-incomplete-reference``: the stub's own reference pair, against the one ``DEST`` admitted.

    Reported on the stub alone, never on a role naming it.
    """
    element = Subject("implementation_types", dt.name)
    records: list[ConsistencyIssue] = []
    for problem, fields in external_pair_problems(
        dt.external_ref, dt.external_dest, EXTERNAL_DATA_TYPE_DESTS, "an implementation data type DEST"
    ):
        records.append(
            ConsistencyIssue(
                rule="data-type.external-incomplete-reference",
                message=f"External data type stub '{dt.name}' {problem}.",
                subjects=tuple(element.at(field_step(name)) for name in fields),
            )
        )
    return records


# ---------------------------------------------------------------------------
# check_data_type_cycles
# ---------------------------------------------------------------------------


def data_type_references(dt: DataType) -> list[str]:
    """The data-type names *dt* references directly, in declaration order.

    A value or reference type's ``base``, a record's field types, an array's
    element type, an implementation record's element types, an implementation
    array's element type and a pointer's target. A base type, an enumeration, an
    application type and an external type stub reference nothing.

    What it does not reach: a walk that must *rewrite* or *label* the references,
    and ``export component``'s closure, which spell them themselves.
    """
    if isinstance(dt, (ValueDataType, ReferenceDataType)):
        return [dt.base]
    if isinstance(dt, RecordDataType):
        return [record_field.type for record_field in dt.fields]
    if isinstance(dt, ImplementationRecordDataType):
        return [implementation_element.type for implementation_element in dt.elements]
    if isinstance(dt, (ArrayDataType, ImplementationArrayDataType)):
        return [dt.element_type]
    if isinstance(dt, PointerDataType):
        return [dt.target]
    return []


def check_data_type_cycles(model_root: ModelInput) -> ConsistencyResult:
    """Detect dependency cycles in the data type graph.

    Edges:
    - value type     → [] (its ``base`` is a base type, an element of another layer)
    - reference type → [base] (a reference chain)
    - record         → [field.type for each field]
    - array          → [element_type]
    - implementation record → [element.type for each element]
    - implementation array  → [element_type]
    - pointer        → [target] (a recursive pointer graph is a cycle, refused)
    - enum, application type, external stub → [] (no edges)

    A base type is no node, so a value type and the base type it rests on may share
    a name. A node is ``(application, name)``: the graph has no edge across the
    application and implementation layers. References to undeclared types
    contribute no edges (``check_data_type_references`` reports them).

    Args:
        model_root: The model root, or a view of it.

    Returns:
        ConsistencyResult with consistent=True when the graph is acyclic.
    """
    view = as_view(model_root)
    data_types = view.data_description.data_types

    records: list[ConsistencyIssue] = []

    _seen: dict[tuple[str, str], int] = {}
    for dt in data_types:
        _key = (data_type_layer(dt), dt.name)
        _seen[_key] = _seen.get(_key, 0) + 1
    for (dt_layer, dt_name), count in _seen.items():
        if count > 1:
            records.append(
                ConsistencyIssue(
                    rule="data-type.duplicate-name",
                    message=f"Duplicate data type name '{dt_name}' declared in multiple files.",
                    subjects=(
                        Subject(
                            "base_types"
                            if dt_layer == "base_types"
                            else "application_types"
                            if dt_layer == "application_types"
                            else "implementation_types",
                            dt_name,
                        ),
                    ),
                )
            )

    # A pointer's target is an edge like any other, so a recursive pointer graph (a
    # linked list) is one cycle finding.
    graph: dict[tuple[bool, str], list[tuple[bool, str]]] = {}
    for dt in data_types:
        if isinstance(dt, BaseDataType):
            continue
        side = isinstance(dt, APPLICATION_DATA_TYPE_CLASSES)
        graph[(side, dt.name)] = (
            [] if isinstance(dt, ValueDataType) else [(side, name) for name in data_type_references(dt)]
        )

    # "white" = unvisited, "gray" = on the DFS stack, "black" = done
    state: dict[tuple[bool, str], str] = {}

    def _dfs(start: tuple[bool, str], path: list[tuple[bool, str]]) -> None:
        state[start] = "gray"
        for neighbor in graph.get(start, []):
            if state.get(neighbor) == "gray":
                cycle_start = path.index(neighbor)
                cycle_path = path[cycle_start:] + [neighbor]
                records.append(
                    ConsistencyIssue(
                        rule="data-type.cycle",
                        message=f"Cycle detected: {' -> '.join(name for _, name in cycle_path)}",
                        # Every type on the cycle: no single one is the offender.
                        subjects=tuple(
                            Subject("application_types" if side else "implementation_types", name)
                            for side, name in dict.fromkeys(cycle_path)
                        ),
                    )
                )
            elif state.get(neighbor, "white") == "white":
                path.append(neighbor)
                _dfs(neighbor, path)
                path.pop()
        state[start] = "black"

    for node in sorted(graph, key=lambda node: (node[1], node[0])):
        if state.get(node, "white") == "white":
            _dfs(node, [node])

    # Several fields pointing to the same gray node emit one cycle more than once.
    return consistency_result(deduplicated(records))


def check_base_type_native_declarations(model_root: ModelInput) -> ConsistencyResult:
    """Report every base type that declares no ``native_declaration``, or a blank one.

    ``data-type.base-type-native-declaration-missing``. The value is the C type
    name generated code declares for the ``SW-BASE-TYPE``, and SWCraft never
    derives one: the ARXML writer emits it as declared and refuses a base type
    without it. The standard itself does not require it —
    ``BaseTypeDirectDefinition.nativeDeclaration`` is ``[0..1]`` and the element
    is ``minOccurs="0"`` at 4.2.2 and 4.3.0 — so this is SWCraft's strictness,
    taken for the code generator and not for the schema. A finding and never a
    load failure, so the read-only commands still read the project.

    What it does **not** reach: whether the declared text is a C type name at
    all. Any non-blank string is accepted and emitted as written.
    """
    view = as_view(model_root)
    records = [
        ConsistencyIssue(
            rule="data-type.base-type-native-declaration-missing",
            message=(
                f"Base type '{dt.name}' declares no native_declaration; declare the C type name generated code "
                "uses for it (for example `native_declaration: uint8_t`) — SWCraft never derives one."
            ),
            subjects=(Subject("base_types", dt.name, (field_step("native_declaration"),)),),
        )
        for dt in view.data_description.data_types
        if isinstance(dt, BaseDataType) and not dt.native_declaration.strip()
    ]
    return consistency_result(records)


# ---------------------------------------------------------------------------
# check_mapping_set_references
# ---------------------------------------------------------------------------


#: The **must-be-mapped** application kinds: the scalar physical view, so no record or
#: array is ever mandatory to map. The ``MAP_MAY_*`` pair below is what a map **may**
#: name, widened with the composites (and the pointer, so that a map onto one is
#: ``mapping-set.pointer-mapped`` alone rather than two findings for one cause).
MAP_APPLICATION_KINDS: tuple[type, ...] = (ApplicationDataType, EnumDataType)


MAP_MAY_APPLICATION_KINDS: tuple[type, ...] = (ApplicationDataType, EnumDataType, RecordDataType, ArrayDataType)


MAP_MAY_IMPLEMENTATION_KINDS: tuple[type, ...] = (
    ValueDataType,
    ReferenceDataType,
    ImplementationRecordDataType,
    ImplementationArrayDataType,
    PointerDataType,
)


MAP_MAY_APPLICATION_REPAIR = "a map's `application` must name a declared application_type, enum, record or array."


MAP_MAY_IMPLEMENTATION_REPAIR = (
    "a map's `implementation` must name a declared value_type, reference_type, implementation_record or "
    "implementation_array."
)


COMPOSITE_MAP_REPAIR = (
    "a record pairs with an implementation record of as many elements and an array with an implementation array "
    "of the same length, member by member in declaration order, and a scalar member with a type ending at a "
    "value_type — member names are not compared."
)


def mapped_pointer(implementation: str, data_types: DataDescription) -> tuple[str, ...] | None:
    """The names from a map's *implementation* to the first pointer it reaches, or ``None``.

    ``[constr_1066]`` (R4.3.0, normative): an application data type is never
    mapped to an implementation data type of category ``DATA_REFERENCE``; R22-11
    extends the same sentence to one that *contains subElements of category* …
    ``DATA_REFERENCE``, and that later reading is applied here. So a pointer is reached directly, at the end of a
    chain of reference types, or anywhere inside the implementation record or
    array the map names. A member that does not resolve, and a composite graph
    that comes back to itself, are passed over (other rules' findings).
    """
    return _pointer_within(implementation, data_types, frozenset())


def _pointer_within(name: str, data_types: DataDescription, seen: frozenset[str]) -> tuple[str, ...] | None:
    if name in seen:
        return None
    end = implementation_end(name, data_types)
    if end is None:
        return None
    chain, declared = end
    if isinstance(declared, PointerDataType):
        return chain
    seen = seen | frozenset(chain)
    members = (
        data_type_references(declared)
        if isinstance(declared, (ImplementationRecordDataType, ImplementationArrayDataType))
        else []
    )
    for member in members:
        inner = _pointer_within(member, data_types, seen)
        if inner is not None:
            return (*chain, *inner)
    return None


def _counted(count: int, noun: str) -> str:
    """``1 element`` / ``2 elements``."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def composite_map_difference(application: str, implementation: str, data_types: DataDescription) -> str | None:
    """The first judged way a composite map's two sides differ, or ``None`` — compatible **or** not judged.

    ``[TPS_SWCT_01191]`` (normative, R4.3.0 SWCT ``:11208``) says a mapped
    application and implementation data type *shall be compatible*, and
    ``[constr_1059]``, ``[constr_1060]`` and ``[constr_1061]`` give the category
    conditions that compatibility needs. What this judges is **partial coverage**
    of that item through a **positional subset SWCraft chooses**, never a claim
    of conformance:

    * the **category** — a record pairs with an implementation record, an array
      with an implementation array, and a scalar member (an application type or
      an enumeration) with a type ending at a value type;
    * a record's member **count**, and each member **by position**, recursively
      by category and shape;
    * an array's **length** and its element, recursively (both sides are fixed
      size by construction).

    **Not judged, a stated false accept:** member names; whether a scalar's
    range, conversion or float encoding is representable in its implementation
    type (a top-level scalar map is not asked here at all); two members of the
    same judged shape swapped in position; and a member whose type does not
    resolve, is an external data type stub, or is a pointer. Such a member is
    passed over and the members after it are still judged.

    A reference type on the implementation side is followed to what it ends at,
    at the top and at every member. ``None`` unless the application side is a
    record or an array, or the implementation side ends at an implementation
    record or array.
    """
    end = implementation_end(implementation, data_types)
    if not isinstance(data_types.application(application), (RecordDataType, ArrayDataType)) and not (
        end is not None and isinstance(end[1], (ImplementationRecordDataType, ImplementationArrayDataType))
    ):
        return None
    return _map_member_difference(application, implementation, data_types, frozenset())


def _map_member_difference(
    application: str,
    implementation: str,
    data_types: DataDescription,
    seen: frozenset[tuple[str, str]],
) -> str | None:
    """:func:`composite_map_difference` one level down: a pair of members, or the top pair itself."""
    declared = data_types.application(application)
    end = implementation_end(implementation, data_types)
    if declared is None or end is None:
        return None
    chain, stored = end
    stored_name = chain[-1]
    if not isinstance(declared, MAP_MAY_APPLICATION_KINDS) or not isinstance(
        stored, (ValueDataType, ImplementationRecordDataType, ImplementationArrayDataType)
    ):
        # A stub, a pointer or a kind its role does not admit: another rule's, or outside the project.
        return None
    if (application, stored_name) in seen:
        return None
    seen = seen | {(application, stored_name)}
    if isinstance(declared, RecordDataType) and isinstance(stored, ImplementationRecordDataType):
        if len(declared.fields) != len(stored.elements):
            return (
                f"record '{application}' has {_counted(len(declared.fields), 'field')} and implementation record "
                f"'{stored_name}' {_counted(len(stored.elements), 'element')}"
            )
        for position, (record_field, implementation_element) in enumerate(
            zip(declared.fields, stored.elements), start=1
        ):
            difference = _map_member_difference(record_field.type, implementation_element.type, data_types, seen)
            if difference is not None:
                return f"member {position} of record '{application}': {difference}"
        return None
    if isinstance(declared, ArrayDataType) and isinstance(stored, ImplementationArrayDataType):
        if declared.length != stored.length:
            return (
                f"array '{application}' vs implementation array '{stored_name}': length {declared.length} vs "
                f"{stored.length}"
            )
        difference = _map_member_difference(declared.element_type, stored.element_type, data_types, seen)
        return None if difference is None else f"element of array '{application}': {difference}"
    if isinstance(declared, (ApplicationDataType, EnumDataType)) and isinstance(stored, ValueDataType):
        return None
    return f"category {declared.kind} '{application}' vs {stored.kind} '{stored_name}'"


def check_mapping_set_references(model_root: ModelInput) -> ConsistencyResult:
    """Resolve every reference of the ``mapping_sets/`` layer, and every reference into it.

    Six rules from one read of three layers: ``mapping-set.unknown-application``,
    ``mapping-set.unknown-implementation``, ``mapping-set.pointer-mapped`` (:func:`mapped_pointer`,
    ``[constr_1066]``), ``mapping-set.incompatible-composite-map`` (:func:`composite_map_difference`, a partial
    coverage of ``[TPS_SWCT_01191]``), ``swc.unknown-mapping-set`` and ``mapping-set.duplicate-name``; the model
    contract describes each.

    Beyond the contract: ``swc.unknown-mapping-set`` is reported at the component; ``target`` is carried only when
    a name resolves to nothing; a set name a data type also takes is legal. Whether a scalar pair is meaningful is
    not judged; one set mapping a type twice is refused at load, two sets doing so are
    :func:`check_application_type_double_mapping`'s.

    Args:
        model_root: The model root, or a view of it.

    Returns:
        ConsistencyResult with consistent=True only when every reference resolves.
    """
    view = as_view(model_root)
    type_map = view.data_description
    mapping_sets = view.mapping_sets
    swcs = view.swcs

    declared_sets: set[str] = {mapping_set.name for mapping_set in mapping_sets}
    records: list[ConsistencyIssue] = []

    for name in duplicates([mapping_set.name for mapping_set in mapping_sets]):
        records.append(
            ConsistencyIssue(
                rule="mapping-set.duplicate-name",
                message=f"Duplicate mapping set name '{name}' declared in multiple files.",
                subjects=(Subject("mapping_sets", name),),
            )
        )

    for mapping_set in mapping_sets:
        element = Subject("mapping_sets", mapping_set.name)
        for entry in mapping_set.maps:
            member = (field_step("maps"), member_step(entry.application))
            application = type_map.application(entry.application)
            if application is None:
                records.append(
                    ConsistencyIssue(
                        rule="mapping-set.unknown-application",
                        message=(
                            f"Mapping set '{mapping_set.name}' maps '{entry.application}', which is not declared "
                            f"in the application_types layer; {MAP_MAY_APPLICATION_REPAIR}"
                        ),
                        subjects=(element.at(*member, field_step("application")),),
                        target=entry.application,
                    )
                )
            # Every application_types kind is admitted, so a resolved name needs no kind check.
            implementation = type_map.implementation(entry.implementation)
            if implementation is None:
                records.append(
                    ConsistencyIssue(
                        rule="mapping-set.unknown-implementation",
                        message=(
                            f"Mapping set '{mapping_set.name}' maps '{entry.application}' onto "
                            f"'{entry.implementation}', which is not declared in the implementation_types layer; "
                            f"{MAP_MAY_IMPLEMENTATION_REPAIR}"
                        ),
                        subjects=(element.at(*member, field_step("implementation")),),
                        target=entry.implementation,
                    )
                )
            elif not isinstance(implementation, MAP_MAY_IMPLEMENTATION_KINDS):
                records.append(
                    ConsistencyIssue(
                        rule="mapping-set.unknown-implementation",
                        message=(
                            f"Mapping set '{mapping_set.name}' maps '{entry.application}' onto "
                            f"'{entry.implementation}', which is declared as a '{implementation.kind}'; "
                            f"{MAP_MAY_IMPLEMENTATION_REPAIR}"
                        ),
                        subjects=(element.at(*member, field_step("implementation")),),
                    )
                )
            else:
                # `[constr_1066]` first, whatever the application side: one cause, one finding.
                pointer = mapped_pointer(entry.implementation, type_map)
                if pointer is not None:
                    records.append(
                        ConsistencyIssue(
                            rule="mapping-set.pointer-mapped",
                            message=(
                                f"Mapping set '{mapping_set.name}' maps '{entry.application}' onto "
                                f"'{entry.implementation}', which reaches the pointer '{pointer[-1]}' "
                                f"({chain_text(pointer)}); an application type is never mapped to a pointer — "
                                f"directly, through a reference type or inside an implementation record or array "
                                f"([constr_1066])."
                            ),
                            subjects=(element.at(*member, field_step("implementation")),),
                        )
                    )
                elif isinstance(application, MAP_MAY_APPLICATION_KINDS):
                    difference = composite_map_difference(entry.application, entry.implementation, type_map)
                    if difference is not None:
                        records.append(
                            ConsistencyIssue(
                                rule="mapping-set.incompatible-composite-map",
                                message=(
                                    f"Mapping set '{mapping_set.name}' maps '{entry.application}' onto "
                                    f"'{entry.implementation}', which SWCraft's positional check of a composite "
                                    f"map refuses: {difference}; {COMPOSITE_MAP_REPAIR}"
                                ),
                                subjects=(element.at(*member, field_step("implementation")),),
                            )
                        )

    for swc in swcs:
        for name in swc.data_type_mappings:
            if name not in declared_sets:
                records.append(
                    ConsistencyIssue(
                        rule="swc.unknown-mapping-set",
                        message=(
                            f"SWC '{swc.name}' names mapping set '{name}', which is not declared in the "
                            f"mapping_sets layer."
                        ),
                        # A whole_value list: the member is its own identity.
                        subjects=(Subject("swc", swc.name, (field_step("data_type_mappings"), member_step(name))),),
                        target=name,
                    )
                )

    return consistency_result(records)


# ---------------------------------------------------------------------------
# check_application_type_mapping
# ---------------------------------------------------------------------------


def _port_member_application_types(interface: Interface) -> Iterator[str]:
    """Every application type a port speaking *interface* puts on the bus, in declaration order.

    A data element's or an argument's ``application_type``, where one is set.
    Repeats are the caller's to drop.
    """
    if isinstance(interface, SRInterface):
        for element in interface.data_elements:
            if element.application_type is not None:
                yield element.application_type
    elif isinstance(interface, CSInterface):
        for operation in interface.operations:
            for argument in operation.arguments:
                if argument.application_type is not None:
                    yield argument.application_type


def check_application_type_mapping(model_root: ModelInput) -> ConsistencyResult:
    """Every physical type a component's ports speak is paired with an implementation type.

    ``data-type.application-type-unmapped``, ``[constr_1004]``'s direction: the
    RTE needs the implementation type that stores a physical one. One finding per
    ``(component, type)``; a component's variables speak their ``application_type``
    too, and the message says where the type is spoken.

    **It abstains wherever another rule owns the defect**: a port naming no
    declared interface (``port.undeclared-interface``), a prototype naming no
    declared type (``interface.*-undeclared-type``), and a component naming a set
    that is undeclared (``swc.unknown-mapping-set``) or declared twice
    (``mapping-set.duplicate-name``) -- the whole component, so a second typo'd
    entry hides its genuinely unmapped types until it is repaired.

    What it does **not** reach: a **record** or an **array** on a port, and the
    **members** of one: a composite *may* be mapped, but none is *required*
    (:data:`MAP_APPLICATION_KINDS`). Nor a mapping declared and never used, which
    is legal.

    Args:
        model_root: The model root, or a view of it.

    Returns:
        ConsistencyResult with consistent=True only when every physical type a
        port speaks is mapped by a set its component names.
    """
    view = as_view(model_root)
    swcs = view.swcs
    interfaces = view.interfaces
    type_map = view.data_description
    mapping_sets = view.mapping_sets

    interface_map: dict[str, Interface] = {interface.name: interface for interface in interfaces}
    set_map: dict[str, MappingSet] = {mapping_set.name: mapping_set for mapping_set in mapping_sets}

    # `set_map` is last-wins, so a duplicated set's maps depend on reader order.
    duplicated_sets = frozenset(duplicates([mapping_set.name for mapping_set in mapping_sets]))

    records: list[ConsistencyIssue] = []
    for swc in swcs:
        # Shared with `unmapped_type_repairs`, so the refusal and the repair cannot disagree.
        unmapped = _unmapped_application_types(swc, interface_map, type_map, set_map, duplicated_sets)

        # The repair is decided per component, not per type (`_repair_carriers`).
        carriers = _repair_carriers(swc, unmapped, set_map, duplicated_sets)
        fixes = {type_name: _fix_of(swc, type_name, carriers, set_map, duplicated_sets) for type_name in unmapped}

        variable_types = frozenset(variable_application_types(swc))
        for type_name in unmapped:
            on_ports = any(
                type_name in _port_member_application_types(interface_map[port.interface])
                for port in swc.ports
                if port.interface in interface_map
            )
            where = (
                "on its ports and in its variables"
                if on_ports and type_name in variable_types
                else ("on its ports" if on_ports else "in its variables")
            )
            records.append(
                ConsistencyIssue(
                    rule="data-type.application-type-unmapped",
                    message=(
                        f"SWC '{swc.name}' speaks type '{type_name}' {where}, and none of the mapping sets it "
                        f"names pairs it with an implementation type; add the pair to a set the component names, "
                        f"or name a set that already carries it.{fixes[type_name]}"
                    ),
                    subjects=(Subject("swc", swc.name, (field_step("data_type_mappings"),)),),
                )
            )

    return consistency_result(records)


def application_type_pairings(names: Sequence[str], sets: Mapping[str, MappingSet]) -> dict[str, dict[str, list[str]]]:
    """Every implementation type each application type is paired with through *names*, by set.

    ``{application: {implementation: [set, ...]}}``, in the order *names* lists
    the sets and each set its maps; a name listed twice is read once, and a name
    *sets* does not declare pairs nothing. Both sides are read **as named** and
    are not resolved: ``[constr_1004]`` counts the maps a component's behaviour
    references.
    """
    pairings: dict[str, dict[str, list[str]]] = {}
    for set_name in dict.fromkeys(names):
        mapping_set = sets.get(set_name)
        if mapping_set is None:
            continue
        for entry in mapping_set.maps:
            through = pairings.setdefault(entry.application, {}).setdefault(entry.implementation, [])
            if set_name not in through:
                through.append(set_name)
    return pairings


def application_type_double_mappings(
    names: Sequence[str], sets: Mapping[str, MappingSet]
) -> dict[str, dict[str, list[str]]]:
    """The entries of :func:`application_type_pairings` that pair a type with two or more implementation types.

    ``[constr_1004]``'s condition for one component, in the pairings' own order.
    """
    return {application: onto for application, onto in application_type_pairings(names, sets).items() if len(onto) > 1}


def check_application_type_double_mapping(model_root: ModelInput) -> ConsistencyResult:
    """No component's mapping sets pair one application type with two implementation types.

    ``data-type.application-type-double-mapped``, the *exactly one* half of
    ``[constr_1004]`` that :func:`check_application_type_mapping` does not decide.
    The scope is the **union** of the sets a component's ``data_type_mappings``
    names: one finding per ``(component, application type)`` that union pairs with
    two or more implementation types, over every map a named set carries. Two sets
    pairing a type with the **same** implementation type agree and are not
    reported; a double mapping between two components is legal.

    It abstains on a component naming a set two files declare
    (``mapping-set.duplicate-name``). A set name declared nowhere pairs nothing, and
    the rest of the component is still read. A side naming no declared type still
    counts as a pairing, so a typo'd implementation beside a correct one is also
    reported under ``mapping-set.unknown-implementation``; composite members are
    not mapped one by one.

    Args:
        model_root: The model root, or a view of it.

    Returns:
        ConsistencyResult with consistent=True only when no component's sets
        pair one application type with more than one implementation type.
    """
    view = as_view(model_root)
    swcs = view.swcs
    mapping_sets = view.mapping_sets

    set_map: dict[str, MappingSet] = {mapping_set.name: mapping_set for mapping_set in mapping_sets}
    duplicated_sets = frozenset(duplicates([mapping_set.name for mapping_set in mapping_sets]))

    records: list[ConsistencyIssue] = []
    for swc in swcs:
        if any(name in duplicated_sets for name in swc.data_type_mappings):
            continue
        for application, onto in application_type_double_mappings(swc.data_type_mappings, set_map).items():
            routes = " and ".join(
                f"onto '{implementation}' through set{'s' if len(through) > 1 else ''} "
                + ", ".join(f"'{set_name}'" for set_name in through)
                for implementation, through in onto.items()
            )
            records.append(
                ConsistencyIssue(
                    rule="data-type.application-type-double-mapped",
                    message=(
                        f"SWC '{swc.name}' maps type '{application}' {routes}; a component maps each application "
                        f"type onto exactly one implementation type ([constr_1004]) — remove one of the maps, or "
                        f"stop naming one of the sets."
                    ),
                    subjects=(Subject("swc", swc.name, (field_step("data_type_mappings"),)),),
                )
            )

    return consistency_result(records)


def _unmapped_application_types(
    swc: SWComponent,
    interface_map: Mapping[str, Interface],
    type_map: DataDescription,
    set_map: Mapping[str, MappingSet],
    duplicated_sets: frozenset[str],
) -> list[str]:
    """The application types *swc* speaks that no set it names maps, in order; empty where the rule abstains.

    A component naming a set two files declare, or a set the model does not
    declare, answers nothing. A port naming no declared interface is skipped, and
    so is a type that is not declared or not of a kind in
    :data:`MAP_APPLICATION_KINDS`. Variables' types follow the ports'.
    """
    if any(name in duplicated_sets or name not in set_map for name in swc.data_type_mappings):
        return []
    mapped = {entry.application for name in swc.data_type_mappings for entry in set_map[name].maps}
    spoken: list[str] = []
    for port in swc.ports:
        interface = interface_map.get(port.interface) if port.interface is not None else None
        if interface is None:
            continue
        spoken.extend(_port_member_application_types(interface))
    spoken.extend(variable_application_types(swc))
    unmapped: dict[str, None] = {}
    for type_name in spoken:
        declared = type_map.application(type_name)
        if declared is None or not isinstance(declared, MAP_APPLICATION_KINDS):
            continue
        if type_name not in mapped:
            unmapped.setdefault(type_name, None)
    return list(unmapped)


def variable_application_types(swc: SWComponent) -> list[str]:
    """Every ``application_type`` *swc*'s variables state, in declaration order."""
    return [variable.application_type for variable in swc.variables if variable.application_type is not None]


def _conflicts(swc: SWComponent, name: str, set_map: Mapping[str, MappingSet]) -> bool:
    """Whether naming set *name* in *swc*'s ``data_type_mappings`` pairs a type with two implementation types.

    Such a set is no repair: naming it trades one refusal for another.
    """
    return bool(application_type_double_mappings((*swc.data_type_mappings, name), set_map))


def _repair_carriers(
    swc: SWComponent, unmapped: Sequence[str], set_map: Mapping[str, MappingSet], duplicated_sets: frozenset[str]
) -> list[str]:
    """The declared sets whose naming alone repairs every type *swc* leaves unmapped, in declaration order.

    A set qualifies when it is declared once, the component does not name it
    already, it pairs every type in *unmapped*, and naming it pairs none of the
    component's types with a second implementation type. Exactly one such set is
    the unique repair. Empty when *unmapped* is. What it does not reach: a repair
    spread over two sets, which is the author's choice.
    """
    if not unmapped:
        return []
    return [
        name
        for name, mapping_set in set_map.items()
        if name not in duplicated_sets
        and name not in swc.data_type_mappings
        and set(unmapped) <= {entry.application for entry in mapping_set.maps}
        and not _conflicts(swc, name, set_map)
    ]


def _fix_of(
    swc: SWComponent,
    type_name: str,
    carriers: Sequence[str],
    set_map: Mapping[str, MappingSet],
    duplicated_sets: frozenset[str],
) -> str:
    """The repair an ``application-type-unmapped`` message names, as a sentence (may be empty).

    *carriers* is the component's :func:`_repair_carriers`; exactly one is named
    as the unique repair. Otherwise the declared sets that pair *type_name* are
    named as candidates, in declaration order, each one whose naming would pair a
    type with a second implementation type said to. A set two files declare is
    never a candidate (``mapping-set.duplicate-name``'s). Prose only: the
    protocol's error shape is closed.
    """
    if len(carriers) == 1:
        return (
            f" Set '{carriers[0]}' is the one declared set that carries every type SWC '{swc.name}' leaves "
            f"unmapped without pairing one of its types with a second implementation type: name it in the "
            f"component's data_type_mappings."
        )
    candidates = [
        name
        for name, mapping_set in set_map.items()
        if name not in duplicated_sets and any(entry.application == type_name for entry in mapping_set.maps)
    ]
    if not candidates:
        return ""
    sentence = " Declared sets that carry it: " + ", ".join(f"'{name}'" for name in candidates) + "."
    conflicting = [name for name in candidates if _conflicts(swc, name, set_map)]
    if conflicting:
        sentence += (
            " Naming "
            + ", ".join(f"'{name}'" for name in conflicting)
            + " would pair a type the component maps with a second implementation type."
        )
    return sentence


#: The rule ``change_set``'s ``complete: true`` repairs. :func:`check_application_type_mapping`
#: spells the literal itself, because the binding gate reads the rule identifier in its source.
APPLICATION_TYPE_UNMAPPED = "data-type.application-type-unmapped"


class MemberRepair(NamedTuple):
    """A repair one ``create`` makes: *value* as the member *subject* addresses."""

    subject: Subject
    value: str


def unmapped_type_repairs(model_root: ModelInput, subjects: AbstractSet[Subject]) -> dict[Subject, MemberRepair]:
    """The unique repair of each ``data-type.application-type-unmapped`` finding subject named.

    The completion half of :func:`check_application_type_mapping` (``change_set``
    with ``complete: true``). *subjects* are the rule's own finding subjects (a
    component's ``data_type_mappings``); each one with a unique repair maps to the
    ``create`` of the one set :func:`_repair_carriers` finds, as a member of that
    collection. A subject with no such set, with several, or of another rule has
    no entry. It abstains exactly where the rule does. What it does not reach: a
    repair spread over two sets, and every rule but this one.
    """
    view = as_view(model_root)
    collection = (field_step("data_type_mappings"),)
    wanted = {subject.element: subject for subject in subjects if (subject.layer, subject.path) == ("swc", collection)}
    swcs = view.swcs
    interfaces = {interface.name: interface for interface in view.interfaces}
    type_map = view.data_description
    mapping_sets = view.mapping_sets
    set_map: dict[str, MappingSet] = {mapping_set.name: mapping_set for mapping_set in mapping_sets}
    duplicated_sets = frozenset(duplicates([mapping_set.name for mapping_set in mapping_sets]))

    repairs: dict[Subject, MemberRepair] = {}
    for swc in swcs:
        subject = wanted.get(swc.name)
        if subject is None:
            continue
        unmapped = _unmapped_application_types(swc, interfaces, type_map, set_map, duplicated_sets)
        carriers = _repair_carriers(swc, unmapped, set_map, duplicated_sets)
        if len(carriers) == 1:
            repairs[subject] = MemberRepair(subject=subject.at(member_step(carriers[0])), value=carriers[0])
    return repairs
