"""Data type resolution and the interface and port compatibility predicate.

Holds :func:`interface_compatibility`, the one predicate the connector and delegation rules and the
writer ask (``[constr_1069]``/``[constr_1079]``, ``[constr_1070]``/``[constr_1080]``, with the type
comparison of ``[constr_1050]``, ``[constr_1047]``, ``[constr_1051]``, ``[constr_1068]`` and
``[constr_1187]``), the type resolver it compares through, the :func:`check_port_compatibility`
pre-flight, and the *not judged* predicate for pairs across an external stub.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from swcraft_tools.data_description import DataDescription
from swcraft_tools.schema import (
    ApplicationDataType,
    BaseDataType,
    Conversion,
    ConversionLimits,
    CSArgument,
    CSInterface,
    CSOperation,
    DataElement,
    ExternalDataType,
    ExternalInterface,
    ImplementationArrayDataType,
    ImplementationRecordDataType,
    Interface,
    Linear,
    PointerDataType,
    ReferenceDataType,
    SRInterface,
    ValueDataType,
    ValueRange,
)
from swcraft_tools.validator.data_types import declared_type, prototype_type_ref
from swcraft_tools.validator.description_elements import (
    NOT_RESOLVED,
    NotResolved,
    stated_literals,
    stated_range,
    stated_unit_text,
)
from swcraft_tools.validator.model_view import ModelInput, as_view
from swcraft_tools.validator.results import CompatibilityResult
from swcraft_tools.validator.rules import EXTERNAL_INTERFACE_DESTS

#: The two members of :data:`EXTERNAL_INTERFACE_DESTS` that name an **abstract**
#: metaclass. ``[TPS_GST_00173]``: a reference whose ``DEST`` is an abstract
#: class may point at an instance of any concrete subclass, so such a stub does
#: not tell its interface kind.
ABSTRACT_INTERFACE_DESTS: frozenset[str] = frozenset({"DATA-INTERFACE", "PORT-INTERFACE"})


#: The ``DEST`` a declared interface of each kind is referenced by, restated from
#: the writer so that ``validator`` stays importable without ``writer``.
_DECLARED_INTERFACE_DESTS: dict[str, str] = {
    "sender_receiver": "SENDER-RECEIVER-INTERFACE",
    "client_server": "CLIENT-SERVER-INTERFACE",
}


def interface_kind_dest(interface: Interface) -> str | None:
    """The concrete ``DEST`` *interface* is referenced by, or ``None`` where it tells none.

    A stub's is its ``external_dest`` when that is a concrete member of
    :data:`EXTERNAL_INTERFACE_DESTS`; an abstract or unadmitted one gives ``None``.
    """
    if isinstance(interface, ExternalInterface):
        dest = interface.external_dest
        if dest in EXTERNAL_INTERFACE_DESTS and dest not in ABSTRACT_INTERFACE_DESTS:
            return dest
        return None
    return _DECLARED_INTERFACE_DESTS.get(interface.kind)


def _interface_kind_label(interface: Interface) -> str:
    """How a message names *interface*'s kind: ``sender_receiver``, or ``external_interface (DEST)``."""
    if isinstance(interface, ExternalInterface):
        return f"{interface.kind} ({interface.external_dest})"
    return interface.kind


#: The two computations :func:`interface_compatibility` performs.
_COMPATIBILITY_CONTEXTS = frozenset({"subset", "shared-member"})


@dataclass(frozen=True)
class Incompatibility:
    """Why two interfaces may not be joined: one rendered sentence, and nothing else.

    ``subject`` names the first incompatibility in declaration order, with no
    trailing period; the caller frames it.
    """

    subject: str


def _render_subject(head: str, *steps: str) -> str:
    """The one spelling of a subject: its steps, outermost first, joined by ``": "``.

    Every :class:`Incompatibility` is built from this function.
    """
    return ": ".join((head, *steps))


def interface_mismatch_reason(
    head: str,
    required: str,
    required_label: str,
    provided: str,
    provided_label: str,
    subject: str,
) -> str:
    """The one frame the two validator rules put :func:`_render_subject`'s subject in.

    ``{head}: required 'A' (label) is not satisfied by provided 'B' (label):
    {subject}.`` — *head* is the caller's own opening.
    """
    return (
        f"{head}: required '{required}' ({required_label}) is not satisfied by "
        f"provided '{provided}' ({provided_label}): {subject}."
    )


@dataclass(frozen=True)
class ResolvedType:
    """A data type after every reference is resolved — the shape ``[constr_1050]`` compares.

    Type names appear only beside a member, for the message; the comparison never reads one.

    Attributes:
        kind: ``value_type`` (also a reference chain resting on one), ``enum``,
            ``record``, ``array``, ``application_type``, ``implementation_record``,
            ``implementation_array`` or ``pointer``.
        base: the base type a ``value_type`` resolves to.
        application: the declared element an ``application_type`` resolves to (itself).
        physics: an ``application_type``'s unit, conversion and range as it states them.
        literals: an ``enum``'s ``(name, value)`` pairs, in declaration order.
        fields: a ``record``'s or ``implementation_record``'s ``(name, declared
            type name, resolved type)``, in order.
        element: an array's ``(declared element type name, resolved type)``.
        length: an array's length, set with ``element``.
        target: a ``pointer``'s ``(declared target name, resolved type)``.
        element_name: an ``implementation_array``'s sub-element short name;
            ``None`` on every other kind.
    """

    kind: str
    base: BaseDataType | None = None
    application: ApplicationDataType | None = None
    physics: StatedPhysics | None = None
    literals: tuple[tuple[str, int], ...] = ()
    fields: tuple[tuple[str, str, ResolvedType], ...] = ()
    element: tuple[str, ResolvedType] | None = None
    length: int | None = None
    target: tuple[str, ResolvedType] | None = None
    element_name: str | None = None


def resolve_type(
    name: str,
    data_types: DataDescription,
    memo: dict[tuple[bool, str], ResolvedType | None],
    *,
    application: bool,
) -> ResolvedType | None:
    """*name* resolved by following every reference, or ``None`` where it cannot be.

    ``None`` is an abstention wherever another rule owns the cause: an
    undeclared name, a ``base``, element or target of the wrong kind, a cycle,
    an ``external_type`` stub (its content is outside the project), or a
    composite with an unresolvable member. *memo* is keyed ``(application,
    name)``, since the two sides may share a name; an entry is set to ``None``
    before its members resolve, which stops a cycle. *application* is the side
    *name* is asked on.
    """
    key = (application, name)
    if key in memo:
        return memo[key]
    memo[key] = None
    declared = declared_type(data_types, name, application=application)
    resolved: ResolvedType | None = None
    if declared is None:
        pass
    elif declared.kind == "value_type":
        base = data_types.base(declared.base)
        if base is not None and base.kind == "base_type":
            resolved = ResolvedType("value_type", base=base)
    elif declared.kind == "reference_type":
        target = data_types.implementation(declared.base)
        if isinstance(target, _IMPLEMENTATION_ROLE_CLASSES):
            resolved = resolve_type(declared.base, data_types, memo, application=False)
    elif declared.kind == "implementation_record":
        elements: list[tuple[str, str, ResolvedType]] = []
        for implementation_element in declared.elements:
            element_type = _resolve_implementation_member(implementation_element.type, data_types, memo)
            if element_type is None:
                break
            elements.append((implementation_element.name, implementation_element.type, element_type))
        else:
            resolved = ResolvedType("implementation_record", fields=tuple(elements))
    elif declared.kind == "implementation_array":
        member_type = _resolve_implementation_member(declared.element_type, data_types, memo)
        if member_type is not None:
            resolved = ResolvedType(
                "implementation_array",
                element=(declared.element_type, member_type),
                length=declared.length,
                element_name=declared.element_name,
            )
    elif declared.kind == "pointer":
        target_type = _resolve_implementation_member(declared.target, data_types, memo)
        if target_type is not None:
            resolved = ResolvedType("pointer", target=(declared.target, target_type))
    elif declared.kind == "application_type":
        physics = _stated_physics(declared, data_types)
        if physics is not None:
            resolved = ResolvedType("application_type", application=declared, physics=physics)
    elif declared.kind == "enum":
        literals = stated_literals(declared, data_types)
        if not isinstance(literals, NotResolved):
            resolved = ResolvedType("enum", literals=literals)
    elif declared.kind == "record":
        fields: list[tuple[str, str, ResolvedType]] = []
        for record_field in declared.fields:
            field_type = resolve_type(record_field.type, data_types, memo, application=True)
            if field_type is None:
                break
            fields.append((record_field.name, record_field.type, field_type))
        else:
            resolved = ResolvedType("record", fields=tuple(fields))
    elif declared.kind == "array":
        element_type = resolve_type(declared.element_type, data_types, memo, application=True)
        if element_type is not None:
            resolved = ResolvedType("array", element=(declared.element_type, element_type), length=declared.length)
    # A `base_type` stays `None`: a data prototype is never typed by one.
    memo[key] = resolved
    return resolved


#: Every kind of the ``implementation_types`` layer: what a reference type's
#: ``base``, an implementation composite's element and a pointer's target admit.
_IMPLEMENTATION_ROLE_CLASSES: tuple[type, ...] = (
    ValueDataType,
    ReferenceDataType,
    ImplementationRecordDataType,
    ImplementationArrayDataType,
    PointerDataType,
    ExternalDataType,
)


def _resolve_implementation_member(
    name: str,
    data_types: DataDescription,
    memo: dict[tuple[bool, str], ResolvedType | None],
) -> ResolvedType | None:
    """An implementation composite's member or a pointer's target, resolved — or ``None``.

    ``None`` for an undeclared name, a kind the role does not admit, or an external stub.
    """
    declared = data_types.implementation(name)
    if not isinstance(declared, _IMPLEMENTATION_ROLE_CLASSES):
        return None
    return resolve_type(name, data_types, memo, application=False)


def _base_types_compatible(required: BaseDataType, provided: BaseDataType) -> str | None:
    """The first ``[constr_1220]`` attribute two base types differ in, or ``None``.

    ``size``, ``encoding`` and ``native_declaration`` — the three of the five the
    model carries (``byteOrder`` and ``memAlignment`` are not judged). A blank
    ``native_declaration`` is not compared.
    """
    if required.size != provided.size:
        return f"base type size {required.size} vs {provided.size}"
    if required.encoding != provided.encoding:
        return f"base type encoding {required.encoding} vs {provided.encoding}"
    declared_required = required.native_declaration.strip()
    declared_provided = provided.native_declaration.strip()
    if declared_required and declared_provided and declared_required != declared_provided:
        return f"base type native declaration '{declared_required}' vs '{declared_provided}'"
    return None


#: The one word every physics subject spells for *nothing is declared*.
_PHYSICS_ABSENT = "none"


#: Its counterpart for ``conversion`` and ``range``, whose presence is the
#: difference (``conversion: {}`` is the identity scale, and a declared one).
_PHYSICS_DECLARED = "declared"


def _physics_unit(unit: str) -> str:
    """A declared unit as a subject prints it, or :data:`_PHYSICS_ABSENT`.

    Stripped first: the writer emits no ``UNIT`` for a blank one.
    """
    declared = unit.strip()
    return f"'{declared}'" if declared else _PHYSICS_ABSENT


def _physics_bound(bound: int | float | None) -> str:
    """A declared range bound as a subject prints it, or :data:`_PHYSICS_ABSENT`."""
    return _PHYSICS_ABSENT if bound is None else f"{bound}"


def _physics_presence(declared: object | None) -> str:
    """:data:`_PHYSICS_DECLARED` or :data:`_PHYSICS_ABSENT` for one nested object."""
    return _PHYSICS_ABSENT if declared is None else _PHYSICS_DECLARED


def _conversions_compatible(required: Conversion | None, provided: Conversion | None) -> str | None:
    """How two application types' conversions differ, or ``None``.

    ``[constr_1051]`` 2: two ``SwDataDefProps`` are compatible only if they
    *refer to compatible conversion methods or neither of them associates such a
    method*. So presence decides first (``conversion: {}`` emits an identity
    LINEAR ``COMPU-METHOD``, no ``conversion`` emits none).

    Two declared conversions are then compared for identity, coefficient by
    coefficient and limit by limit, which for one LINEAR scale is the whole of
    ``[constr_1163]`` (SWCT R4.2.2 ``:21705``, R4.3.0 ``:24107``: every attribute
    but the names and the documentation identical, and the scales compatible).
    """
    if (required is None) != (provided is None):
        return f"application type conversion {_physics_presence(required)} vs {_physics_presence(provided)}"
    if required is None or provided is None:
        return None
    if required.factor != provided.factor:
        return f"application type conversion factor {required.factor} vs {provided.factor}"
    if required.offset != provided.offset:
        return f"application type conversion offset {required.offset} vs {provided.offset}"
    if _stated_limits(required) != _stated_limits(provided):
        return (
            f"application type conversion limits {_conversion_limits_text(required)} "
            f"vs {_conversion_limits_text(provided)}"
        )
    return None


def _conversion_limits_text(conversion: Conversion) -> str:
    """A conversion's scale limits as a subject prints them: ``[lower, upper]``, or :data:`_PHYSICS_ABSENT`.

    A closed side is a bracket, an open one a parenthesis, an absent one infinity.
    """
    limits = _stated_limits(conversion)
    if limits is None:
        return _PHYSICS_ABSENT
    lower, upper = limits.lower, limits.upper
    opening = "(-inf" if lower is None else f"{'[' if lower.interval == 'closed' else '('}{lower.value}"
    closing = "inf)" if upper is None else f"{upper.value}{']' if upper.interval == 'closed' else ')'}"
    return f"{opening}, {closing}"


def _ranges_compatible(required: ValueRange | None, provided: ValueRange | None) -> str | None:
    """How two application types' ranges differ, or ``None``.

    ``[constr_1051]`` 4 defers to the ``DataConstr`` rules, read here
    **fail-closed as identity**: stricter than ``[constr_1126]``, which lets the
    providing type's limits lie within the requiring type's, and than
    ``[constr_1054]``, so it can only falsely reject, never falsely accept. An
    interval comparison would need a direction ``shared-member`` lacks.

    Presence decides first: a ``range`` with neither bound still emits a ``DATA-CONSTR``.
    """
    if (required is None) != (provided is None):
        return f"application type range {_physics_presence(required)} vs {_physics_presence(provided)}"
    if required is None or provided is None:
        return None
    if required.min != provided.min:
        return f"application type range min {_physics_bound(required.min)} vs {_physics_bound(provided.min)}"
    if required.max != provided.max:
        return f"application type range max {_physics_bound(required.max)} vs {_physics_bound(provided.max)}"
    return None


def _application_types_compatible(required: StatedPhysics, provided: StatedPhysics) -> str | None:
    """The first of the three physics fields two application types differ in, or ``None``.

    ``[constr_1068]`` 1(a) asks a data prototype pair for *compatible
    AutosarDataTypes*, and ``[constr_1047]`` is what that means for two
    ``ApplicationPrimitiveDataType``s: **1(a)** the same ``category`` (the
    resolved ``kind``, compared as ``[constr_1047]`` 1(a) in :func:`_types_compatible`) and **1(b)**
    compatible ``SwDataDefProps``, which is ``[constr_1051]``, read fail-closed
    as identity. ``[constr_1047]`` 2 and 3 and ``[constr_1068]`` 2 need a
    ``DataPrototypeMapping``, which a connector does not have.

    The order is ``[constr_1051]``'s own: the ``Unit`` (1), the conversion
    method (2), the ``DataConstr`` (4); the model carries no ``invalidValue``
    (3) and no ``swRecordLayout`` (5).

    The unit is compared as **text**, fail-closed: ``[constr_1052]`` compares
    two ``Unit`` elements by ``factorSiToUnit``, ``offsetSiToUnit`` and
    ``physicalDimension`` (``shortName``s may differ), none of which the model declares.

    Each side is what the type states, inline or through the element a
    reference field names. A data constraint's ``level`` is not compared:
    ``[constr_1051]`` 4 is about the limits.
    """
    if required.unit != provided.unit:
        return f"application type unit {_physics_unit(required.unit)} vs {_physics_unit(provided.unit)}"
    difference = _conversions_compatible(required.conversion, provided.conversion)
    if difference is not None:
        return difference
    return _ranges_compatible(required.range, provided.range)


def type_pair_difference(name_a: str, a: ResolvedType, name_b: str, b: ResolvedType) -> str | None:
    """``type 'A' vs 'B' — <first difference>`` for two resolved types, or ``None``."""
    difference = _types_compatible(a, b)
    return None if difference is None else f"type '{name_a}' vs '{name_b}' — {difference}"


def _types_compatible(a: ResolvedType, b: ResolvedType) -> str | None:
    """The first attribute two resolved types differ in, as a subject's tail, or ``None``.

    Kind first (``[constr_1050]`` 1(a); for an ``application_type`` this is
    ``[constr_1047]`` 1(a)), then kind by kind the structure (1(b), and
    ``[constr_1187]`` for a composite): a count before a position, a position's
    name before its value or its type. Member names count; type names never do.
    An implementation record and array compare as the record and array do
    (``[constr_1187]`` names implementation ``STRUCTURE`` and ``ARRAY``); a
    pointer compares by its target, SWCraft's own reading.

    The tail raises, so a kind :func:`resolve_type` adds untaught fails loudly
    instead of passing as compatible.
    """
    if a.kind != b.kind:
        return f"kind {a.kind} vs {b.kind}"
    if a.kind == "value_type":
        if a.base is None or b.base is None:
            return None
        return _base_types_compatible(a.base, b.base)
    if a.kind == "application_type":
        if a.physics is None or b.physics is None:
            return None
        return _application_types_compatible(a.physics, b.physics)
    if a.kind == "enum":
        return _enum_difference(a, b)
    if a.kind == "record":
        return _record_difference(a, b)
    if a.kind == "array":
        return _array_difference(a, b)
    if a.kind == "implementation_record":
        return _implementation_record_difference(a, b)
    if a.kind == "implementation_array":
        return _implementation_array_difference(a, b)
    if a.kind == "pointer":
        return _pointer_difference(a, b)
    raise AssertionError(f"resolve_type produced a kind this comparison has not been taught: {a.kind!r}")


def _enum_difference(a: ResolvedType, b: ResolvedType) -> str | None:
    """The first difference of two ``enum`` types' literals, or ``None``: the count, then name and value."""
    if len(a.literals) != len(b.literals):
        return f"enum literals {len(a.literals)} vs {len(b.literals)}"
    for position, ((name_a, value_a), (name_b, value_b)) in enumerate(zip(a.literals, b.literals), start=1):
        if name_a != name_b:
            return f"enum literal {position} '{name_a}' vs '{name_b}'"
        if value_a != value_b:
            return f"enum literal {position} '{name_a}' value {value_a} vs {value_b}"
    return None


def _record_difference(a: ResolvedType, b: ResolvedType) -> str | None:
    """The first difference of two ``record`` types' fields, or ``None``: the count, then name and type."""
    if len(a.fields) != len(b.fields):
        return f"record fields {len(a.fields)} vs {len(b.fields)}"
    for position, (field_a, field_b) in enumerate(zip(a.fields, b.fields), start=1):
        if field_a[0] != field_b[0]:
            return f"record field {position} '{field_a[0]}' vs '{field_b[0]}'"
        difference = type_pair_difference(field_a[1], field_a[2], field_b[1], field_b[2])
        if difference is not None:
            return _render_subject(f"record field '{field_a[0]}'", difference)
    return None


def _array_difference(a: ResolvedType, b: ResolvedType) -> str | None:
    """The first difference of two ``array`` types, or ``None``: the length, then the element type."""
    if a.element is None or b.element is None:
        return None
    if a.length != b.length:
        return f"array length {a.length} vs {b.length}"
    difference = type_pair_difference(a.element[0], a.element[1], b.element[0], b.element[1])
    return None if difference is None else f"array element {difference}"


def _implementation_record_difference(a: ResolvedType, b: ResolvedType) -> str | None:
    """The first difference of two ``implementation_record`` types, compared as :func:`_record_difference` does.

    ``[constr_1187]`` governs a data prototype typed by an implementation
    STRUCTURE or ARRAY exactly as one typed by an application composite -- the
    same number of elements, compatible types in the same order, every attribute
    but the type's own short name matching -- so element names are compared.
    """
    if len(a.fields) != len(b.fields):
        return f"implementation record elements {len(a.fields)} vs {len(b.fields)}"
    for position, (element_a, element_b) in enumerate(zip(a.fields, b.fields), start=1):
        if element_a[0] != element_b[0]:
            return f"implementation record element {position} '{element_a[0]}' vs '{element_b[0]}'"
        difference = type_pair_difference(element_a[1], element_a[2], element_b[1], element_b[2])
        if difference is not None:
            return _render_subject(f"implementation record element '{element_a[0]}'", difference)
    return None


def _implementation_array_difference(a: ResolvedType, b: ResolvedType) -> str | None:
    """The first difference of two ``implementation_array`` types: the length, the element name, then its type."""
    if a.element is None or b.element is None:
        return None
    if a.length != b.length:
        return f"implementation array length {a.length} vs {b.length}"
    if a.element_name != b.element_name:
        return f"implementation array element name '{a.element_name}' vs '{b.element_name}'"
    difference = type_pair_difference(a.element[0], a.element[1], b.element[0], b.element[1])
    return None if difference is None else f"implementation array element {difference}"


def _pointer_difference(a: ResolvedType, b: ResolvedType) -> str | None:
    """How two ``pointer`` types' targets differ, or ``None``.

    No constraint states the compatibility of two ``DATA_REFERENCE``
    prototypes; this is SWCraft's own fail-closed reading.
    """
    if a.target is None or b.target is None:
        return None
    difference = type_pair_difference(a.target[0], a.target[1], b.target[0], b.target[1])
    return None if difference is None else f"pointer target {difference}"


def _prototype_type_difference(
    prototype_a: DataElement | CSArgument,
    prototype_b: DataElement | CSArgument,
    data_types: DataDescription,
    memo: dict[tuple[bool, str], ResolvedType | None],
) -> str | None:
    """How two data prototypes' declared types differ, or ``None`` — compatible or abstained.

    The same name on the same side is compatible unread.
    """
    (name_a, application_a), (name_b, application_b) = prototype_type_ref(prototype_a), prototype_type_ref(prototype_b)
    if (name_a, application_a) == (name_b, application_b):
        return None
    a = None if name_a is None else resolve_type(name_a, data_types, memo, application=application_a)
    b = None if name_b is None else resolve_type(name_b, data_types, memo, application=application_b)
    if a is None or b is None or name_a is None or name_b is None:
        return None
    return type_pair_difference(name_a, a, name_b, b)


def _operation_difference(
    required: CSOperation,
    provided: CSOperation,
    data_types: DataDescription,
    memo: dict[tuple[bool, str], ResolvedType | None],
) -> str | None:
    """How two same-named operations' signatures differ, or ``None``.

    ``[constr_1078]``: the same number of arguments, and the n-th pair compatible
    — which ``[constr_1076]`` judges by ``direction`` and type alone. An
    argument's name takes no part; the required side's is quoted for the reader.
    """
    if len(required.arguments) != len(provided.arguments):
        count = len(required.arguments)
        return f"{count} argument{'' if count == 1 else 's'} vs {len(provided.arguments)}"
    for position, (argument, counterpart) in enumerate(zip(required.arguments, provided.arguments), start=1):
        if argument.direction != counterpart.direction:
            return f"argument {position} '{argument.name}' direction {argument.direction} vs {counterpart.direction}"
        difference = _prototype_type_difference(argument, counterpart, data_types, memo)
        if difference is not None:
            return _render_subject(f"argument {position} '{argument.name}'", difference)
    return None


#: What a member of each interface kind is called in a subject. A kind that is
#: not a key is an abstention, so a third kind cannot fall into the client-server arm.
_MEMBER_NOUNS: dict[str, str] = {"sender_receiver": "data element", "client_server": "operation"}


@dataclass(frozen=True)
class _MemberVerdict:
    """One member of the required interface, judged against its counterpart.

    Attributes:
        name: the member's declared name, in *required*'s declaration order.
        offered: whether the provided interface declares a member of that name.
        difference: how the two differ, or ``None`` when compatible or abstained.
    """

    name: str
    offered: bool
    difference: str | None


def _queued_word(element: DataElement) -> str:
    """How a compatibility subject names *element*'s queued-ness: ``queued`` or ``not queued``."""
    return "queued" if is_queued(element) else "not queued"


def _element_difference(
    element: DataElement,
    counterpart: DataElement,
    data_types: DataDescription,
    memo: dict[tuple[bool, str], ResolvedType | None],
) -> str | None:
    """How the required *element* and its same-named *counterpart* differ, or ``None``.

    The resolved types first, then ``[constr_1068]`` 1(c): *"The attribute
    swImplPolicy is either set to queued for both or none"*; ``standard`` and an
    absent ``sw_impl_policy`` are both *not queued*. ``None`` also when the type
    comparison abstains; queued-ness is still judged then, because it reads no type.
    """
    difference = _prototype_type_difference(element, counterpart, data_types, memo)
    if difference is not None:
        return difference
    if _queued_word(element) != _queued_word(counterpart):
        return f"sw_impl_policy {_queued_word(element)} vs {_queued_word(counterpart)}"
    return None


def _member_verdicts(
    required_interface: SRInterface | CSInterface,
    provided_interface: SRInterface | CSInterface,
    data_types: DataDescription,
    memo: dict[tuple[bool, str], ResolvedType | None],
) -> Iterator[_MemberVerdict]:
    """Each member of *required_interface*, in declaration order, against its counterpart.

    Both interfaces must be of one kind :data:`_MEMBER_NOUNS` admits. Lazy, so
    no member after the answer is resolved. Of a member name declared twice in
    *provided_interface* the first is the counterpart.

    Raises:
        AssertionError: the two interfaces are not both SRInterface or both CSInterface.
    """
    if isinstance(required_interface, SRInterface) and isinstance(provided_interface, SRInterface):
        offered_elements: dict[str, DataElement] = {}
        for element in provided_interface.data_elements:
            offered_elements.setdefault(element.name, element)
        for element in required_interface.data_elements:
            counterpart = offered_elements.get(element.name)
            yield _MemberVerdict(
                element.name,
                counterpart is not None,
                None if counterpart is None else _element_difference(element, counterpart, data_types, memo),
            )
        return
    if isinstance(required_interface, CSInterface) and isinstance(provided_interface, CSInterface):
        offered_operations: dict[str, CSOperation] = {}
        for operation in provided_interface.operations:
            offered_operations.setdefault(operation.name, operation)
        for operation in required_interface.operations:
            counterpart_operation = offered_operations.get(operation.name)
            yield _MemberVerdict(
                operation.name,
                counterpart_operation is not None,
                None
                if counterpart_operation is None
                else _operation_difference(operation, counterpart_operation, data_types, memo),
            )
        return
    raise AssertionError(
        "expected two interfaces of one class this generator has been taught, got "
        f"{type(required_interface).__name__} / {type(provided_interface).__name__} "
        f"(kind {required_interface.kind!r})"
    )


def interface_compatibility(
    required: str | None,
    provided: str | None,
    *,
    context: str,
    interfaces: Mapping[str, Interface],
    data_types: DataDescription,
) -> Incompatibility | None:
    """Whether the interface named *required* is satisfied by the one named *provided*.

    ``None`` when it is, or when this rule abstains; otherwise the **first** incompatibility in the declaration
    order of *required*'s members. What compatible means is the model contract's ``connector.interface-mismatch``
    and ``composition.delegation-interface-mismatch``. *context* selects one of two computations:

    * ``subset`` — every member of *required* has a compatible one **of the same name** in *provided*
      (``[constr_1069]``/``[constr_1079]`` 1(a) for an assembly connector, required = the ``to`` port's interface;
      ``[constr_1070]``/``[constr_1080]`` 1(a) for a ``require`` delegation, required = the inner port's); a member
      that abstains is skipped;
    * ``shared-member`` — **at least one** member of *required* has one (``[constr_1070]``/``[constr_1080]`` 2(a)
      for a ``provide`` delegation, required = the inner port's); a member that abstains counts as compatible.

    Beyond the contract: an ``application_type``'s category, ``unit``, ``conversion`` and ``range`` are judged for
    identity (``[constr_1051]`` 1, 2 and 4), stricter than ``[constr_1052]`` and ``[constr_1126]``; a data element's
    ``init``, ``asil`` and description are not judged. The same name on
    both sides returns ``None`` unread; an interface not in *interfaces*, or ``None``, abstains. No layer is read.

    Raises:
        ValueError: *context* is neither ``subset`` nor ``shared-member``.
    """
    if context not in _COMPATIBILITY_CONTEXTS:
        raise ValueError(f"context must be one of {sorted(_COMPATIBILITY_CONTEXTS)}, not {context!r}")
    if required == provided or required is None or provided is None:
        return None
    required_interface = interfaces.get(required)
    provided_interface = interfaces.get(provided)
    if required_interface is None or provided_interface is None:
        return None
    if isinstance(required_interface, ExternalInterface) or isinstance(provided_interface, ExternalInterface):
        required_dest = interface_kind_dest(required_interface)
        provided_dest = interface_kind_dest(provided_interface)
        if required_dest is None or provided_dest is None or required_dest == provided_dest:
            return None
        return Incompatibility(
            _render_subject(
                "interface kinds differ",
                f"{_interface_kind_label(required_interface)} vs {_interface_kind_label(provided_interface)}",
            )
        )
    if required_interface.kind != provided_interface.kind:
        return Incompatibility(
            _render_subject("interface kinds differ", f"{required_interface.kind} vs {provided_interface.kind}")
        )
    noun = _MEMBER_NOUNS.get(required_interface.kind)
    if noun is None:
        return None
    # `[constr_1069]`/`[constr_1079]` item 2 and
    # `[constr_1070]`/`[constr_1080]` item 3 -- "the values of their isService
    # attributes are identical" -- in both contexts.
    if required_interface.is_service != provided_interface.is_service:
        return Incompatibility(
            _render_subject(
                "is_service differs",
                f"{str(required_interface.is_service).lower()} vs {str(provided_interface.is_service).lower()}",
            )
        )

    memo: dict[tuple[bool, str], ResolvedType | None] = {}
    verdicts = _member_verdicts(required_interface, provided_interface, data_types, memo)

    if context == "subset":
        for verdict in verdicts:
            if not verdict.offered:
                return Incompatibility(_render_subject(f"{noun} '{verdict.name}' has no counterpart in '{provided}'"))
            if verdict.difference is not None:
                return Incompatibility(_render_subject(f"{noun} '{verdict.name}'", verdict.difference))
        return None

    # `shared-member`: the first compatible (or abstaining) counterpart answers the pair.
    first_failure: _MemberVerdict | None = None
    for verdict in verdicts:
        if not verdict.offered:
            continue
        if verdict.difference is None:
            return None
        if first_failure is None:
            first_failure = verdict
    if first_failure is None:
        return Incompatibility(_render_subject(f"no {noun} of '{required}' has a counterpart in '{provided}'"))
    detail = _render_subject(f"{noun} '{first_failure.name}'", f"{first_failure.difference}")
    return Incompatibility(
        _render_subject(f"no {noun} of '{required}' has a compatible counterpart in '{provided}' ({detail})")
    )


# ---------------------------------------------------------------------------
# check_port_compatibility
# ---------------------------------------------------------------------------


def check_port_compatibility(
    model_root: ModelInput,
    provider_swc: str,
    provider_port: str,
    requirer_swc: str,
    requirer_port: str,
) -> CompatibilityResult:
    """Check whether two ports are compatible for connection.

    Compatible means one port is "provide" and the other "require", and the interface the **require** port needs is
    satisfied by the one the **provide** port offers: :func:`interface_compatibility` in ``subset`` context, the
    same predicate as the gate.

    Either interface undeclared answers ``compatible: true`` (``port.undeclared-interface`` reports it). Across an
    external interface stub where the predicate abstained, the result stays ``compatible`` and ``reason`` begins
    ``not judged: external interface '<name>'``; a differing kind is an ordinary incompatibility, and one stub on
    both ends is ``compatible`` with an empty ``reason``. For a port of an external component stub, after the
    direction, the result is ``compatible`` and ``reason`` begins ``not judged: external component '<name>' port
    '<port>'``; its ``observed_interface`` hint is never compared.

    Args:
        model_root: The model root, or a view of it.
        provider_swc: Name of the first SWC (called "provider" by convention).
        provider_port: Port name on the first SWC.
        requirer_swc: Name of the second SWC (called "requirer" by convention).
        requirer_port: Port name on the second SWC.

    Returns:
        CompatibilityResult with compatible=True/False and a reason string.

    Raises:
        ValueError: If any SWC or port name is not found in the model.
    """
    view = as_view(model_root)
    swcs = view.swcs
    swc_map = {swc.name: swc for swc in swcs}

    if provider_swc not in swc_map:
        raise ValueError(f"SWC not found: {provider_swc}")
    swc_a = swc_map[provider_swc]

    port_map_a = {p.name: p for p in swc_a.ports}
    if provider_port not in port_map_a:
        raise ValueError(f"Port not found: {provider_port} on SWC {provider_swc}")
    port_a = port_map_a[provider_port]

    if requirer_swc not in swc_map:
        raise ValueError(f"SWC not found: {requirer_swc}")
    swc_b = swc_map[requirer_swc]

    port_map_b = {p.name: p for p in swc_b.ports}
    if requirer_port not in port_map_b:
        raise ValueError(f"Port not found: {requirer_port} on SWC {requirer_swc}")
    port_b = port_map_b[requirer_port]

    directions = {port_a.direction, port_b.direction}
    if directions != {"provide", "require"}:
        return CompatibilityResult(
            compatible=False,
            reason=(
                f"Both ports have direction '{port_a.direction}' and '{port_b.direction}'; "
                "one must be 'provide' and the other 'require'."
            ),
        )

    # Still `compatible` across a stub, because `check-model` accepts the pair; the reason says it is not judged.
    ends = (PortEnd(provider_swc, swc_a.external, provider_port), PortEnd(requirer_swc, swc_b.external, requirer_port))
    if (component_reason := not_judged_component(ends)) is not None:
        return CompatibilityResult(compatible=True, reason=component_reason)

    # The parameter names are a convention only: the roles are read off the resolved ports.
    if port_a.direction == "require":
        required, required_label = port_a, f"{provider_swc}.{provider_port}"
        provided, provided_label = port_b, f"{requirer_swc}.{requirer_port}"
    else:
        required, required_label = port_b, f"{requirer_swc}.{requirer_port}"
        provided, provided_label = port_a, f"{provider_swc}.{provider_port}"

    if required.interface != provided.interface:
        interfaces = interface_index(view.interfaces)
        # A side without an interface skips the data-type read; the stub answer below still needs `interfaces`.
        if required.interface is not None and provided.interface is not None:
            incompatibility = interface_compatibility(
                required.interface,
                provided.interface,
                context="subset",
                interfaces=interfaces,
                data_types=view.data_description,
            )
            if incompatibility is not None:
                return CompatibilityResult(
                    compatible=False,
                    reason=interface_mismatch_reason(
                        "Interface mismatch",
                        required.interface,
                        required_label,
                        provided.interface,
                        provided_label,
                        incompatibility.subject,
                    ),
                )
        stub_reason = not_judged_stub(required.interface, provided.interface, context="subset", interfaces=interfaces)
        if stub_reason is not None:
            return CompatibilityResult(compatible=True, reason=stub_reason)

    return CompatibilityResult(compatible=True, reason="")


def _not_judged_component_reason(owner: str, port: str) -> str:
    """The pre-flight's answer for a pair one of whose ports, *port*, is on the external component stub *owner*."""
    return (
        f"not judged: external component '{owner}' port '{port}' is a port of a stub whose type is "
        "outside the project; it declares no interface, so only the direction can be compared."
    )


def _not_judged_reason(stub: ExternalInterface) -> str:
    """The pre-flight's answer for a pair the predicate abstained on because of *stub*."""
    if interface_kind_dest(stub) is None:
        why = f"its DEST '{stub.external_dest}' does not tell its interface kind"
    else:
        why = f"its kind ({stub.external_dest}) agrees"
    return (
        f"not judged: external interface '{stub.name}' is a stub whose content is outside the project; "
        f"{why}, and nothing beyond an external interface's kind can be compared."
    )


def interface_index(interfaces: list[Interface]) -> dict[str, Interface]:
    """Declared interface name -> interface, **first** declaration winning.

    A name declared twice is ``interface.duplicate-name``'s finding; resolution stays deterministic.
    """
    index: dict[str, Interface] = {}
    for interface in interfaces:
        index.setdefault(interface.name, interface)
    return index


def is_queued(element: DataElement) -> bool:
    """Whether *element* states ``sw_impl_policy: queued``; absent and ``standard`` are both *not queued*."""
    return element.sw_impl_policy == "queued"


class StatedPhysics(NamedTuple):
    """An application type's unit, conversion and range as it states them (:func:`_stated_physics`).

    ``unit`` is stripped, ``""`` for none; ``conversion`` and ``range`` are in
    the inline fields' shape, ``None`` for none.
    """

    unit: str
    conversion: Conversion | None
    range: ValueRange | None


def stated_conversion(
    application_type: ApplicationDataType, description: DataDescription
) -> Conversion | None | NotResolved:
    """The conversion *application_type* states: its inline ``conversion``, or the linear compu method it names.

    In the inline field's shape, so the two forms compare alike. :data:`NOT_RESOLVED`
    where ``compu_method_ref`` names no declared compu method, or a ``texttable``.
    """
    if application_type.compu_method_ref is None:
        return application_type.conversion
    compu_method = description.compu_method(application_type.compu_method_ref)
    if not isinstance(compu_method, Linear):
        return NOT_RESOLVED
    limits = None if compu_method.limits is None else ConversionLimits.model_validate(compu_method.limits.model_dump())
    return Conversion(factor=compu_method.factor, offset=compu_method.offset, limits=limits)


def _stated_physics(application_type: ApplicationDataType, description: DataDescription) -> StatedPhysics | None:
    """*application_type*'s three properties as it states them, or ``None`` where any one is :data:`NOT_RESOLVED`."""
    unit = stated_unit_text(application_type, description)
    conversion = stated_conversion(application_type, description)
    bounds = stated_range(application_type, description)
    if isinstance(unit, NotResolved) or isinstance(conversion, NotResolved) or isinstance(bounds, NotResolved):
        return None
    return StatedPhysics(unit, conversion, bounds)


def _stated_limits(conversion: Conversion) -> ConversionLimits | None:
    """*conversion*'s ``limits``, or ``None`` where they state neither bound.

    ``limits: {}`` and an absent ``limits`` emit the same ``COMPU-METHOD``, so they compare as one.
    """
    limits = conversion.limits
    return None if limits is None or (limits.lower is None and limits.upper is None) else limits


class PortEnd(NamedTuple):
    """One end of a pair of ports as the *not judged* predicate takes it."""

    owner: str
    owner_is_external_stub: bool
    port: str


def not_judged_component(ends: Sequence[PortEnd]) -> str | None:
    """The component half of the *not judged* predicate: an end on an external component stub's port.

    The reason for the first end, in the caller's order, whose owner is a stub,
    or ``None``. The caller judges the direction first.

    The predicate's four cases, of which this is the first (the other three are
    :func:`not_judged_stub`'s): (1) an end on an external component stub's port;
    (2) two **different** external interface stubs whose kinds agree or are not
    told; (3) a declared interface against a different stub whose kind agrees or
    is not told; (4) an unset interface against a stub.

    **What it does not reach:** an overview's bundle and the sequence view ask
    nothing, and ``check-model``'s own passes answer ``compatible`` with no
    marker across a stub.
    """
    for end in ends:
        if end.owner_is_external_stub:
            return _not_judged_component_reason(end.owner, end.port)
    return None


def not_judged_stub(
    required: str | None,
    provided: str | None,
    *,
    context: str,
    interfaces: Mapping[str, Interface],
) -> str | None:
    """The interface half of the *not judged* predicate: a pair compared across an external interface stub.

    Answers cases (2), (3) and (4) of :func:`not_judged_component`'s list with
    the reason for the first stub of ``(required, provided)``. ``None`` when the
    names are equal, when neither is a stub, or when the kinds differ (a
    finding, not *not judged*). The predicate is asked with an empty
    :class:`DataDescription`, since across a stub it reads only the kind.
    *required*, *provided* and *context* are :func:`interface_compatibility`'s roles.

    **What it does not reach:** an overview's bundle and the sequence view ask nothing.
    """
    if required == provided:
        return None
    stubs = [
        interface
        for interface in (
            interfaces.get(required) if required is not None else None,
            interfaces.get(provided) if provided is not None else None,
        )
        if isinstance(interface, ExternalInterface)
    ]
    if not stubs:
        return None
    if required is not None and provided is not None:
        incompatibility = interface_compatibility(
            required, provided, context=context, interfaces=interfaces, data_types=DataDescription(())
        )
        if incompatibility is not None:
            return None
    return _not_judged_reason(stubs[0])
