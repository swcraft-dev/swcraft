"""Data-type and interface mapping.

The per-element policies that ``map_data_types()`` and ``map_interfaces()`` in
``type_mappers.py`` apply, plus ``map_mapping_sets()`` and ``map_variable_types()``.

Never-crash invariant: every mapper guards each field ``schema.py`` would reject
(a blank SHORT-NAME, a non-positive size, a NaN or infinite number, which a bare
``<= 0`` never catches) *before* constructing the SWCraft object, and skips the
one offending element with a reason instead of raising, so one bad element never
aborts the import.

Within the package it depends only on ``registry``, ``merge``, ``merge_verdict``,
``unresolved`` and ``impl_type_resolution``.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Literal, NamedTuple

from swcraft_tools.arxml_import.impl_type_resolution import IMPORTED_IMPLEMENTATION_CATEGORIES
from swcraft_tools.arxml_import.merge import (
    ALIAS,
    LAYER_APPLICATION_TYPES,
    LAYER_BASE_TYPES,
    LAYER_COMPU_METHODS,
    LAYER_DATA_CONSTRAINTS,
    LAYER_IMPLEMENTATION_TYPES,
    LAYER_INTERFACES,
    LAYER_UNITS,
    MAP,
    CarriedLimit,
    MergeDecisions,
    ScaleLimitLine,
    declared_paths,
    linear_coefficients,
    linear_scale_limits,
    texttable_defect,
    texttable_limit_losses,
)
from swcraft_tools.arxml_import.merge_verdict import carried_unit_text
from swcraft_tools.arxml_import.registry import (
    CarriedValueLine,
    PathRegistry,
    PortInitOutcome,
    VariableTypeOutcome,
)
from swcraft_tools.arxml_import.unresolved import (
    CAUSE_ABSENT,
    CAUSE_CONFLICT,
    CAUSE_SELECTION,
    UnresolvedReferences,
    UnresolvedReferrer,
)
from swcraft_tools.arxml_reader import (
    ABORT,
    CARRIED_APPLICATION_CATEGORIES,
    REPORT,
    AppPrimitiveDataTypeInfo,
    CompositionInfo,
    CompuMethodInfo,
    CompuScaleInfo,
    ConstantArray,
    ConstantRecord,
    ConstantScalar,
    ConstantSpecInfo,
    ConstantValue,
    CSInterfaceInfo,
    DataConstrInfo,
    DataTypeMapInfo,
    DataTypeMappingSetInfo,
    ImplDataTypeInfo,
    ImplSubElementInfo,
    ImportModel,
    PortComSpecInitInfo,
    SkipCategory,
    SkipEntry,
    SkipLog,
    SkipReason,
    SRInterfaceInfo,
    StandardRule,
    SwcInfo,
    SwcVariableInfo,
    UnitInfo,
    ValueForm,
    blank_short_name,
    com_spec_block,
    not_supported,
    parse_int_literal,
    phys_limits,
)
from swcraft_tools.data_description import DataDescription
from swcraft_tools.guarantee import required
from swcraft_tools.schema import (
    ApplicationDataType,
    ArrayDataType,
    Conversion,
    ConversionLimitBound,
    ConversionLimits,
    DataConstr,
    DataDescriptionElement,
    DataElement,
    DataType,
    DataTypeMap,
    EnumDataType,
    ImplementationArrayDataType,
    ImplementationElement,
    ImplementationRecordDataType,
    InitValue,
    Linear,
    LinearLimitBound,
    LinearLimits,
    MappingSet,
    PointerDataType,
    RecordDataType,
    RecordField,
    ReferenceDataType,
    TableEntry,
    TextTable,
    Unit,
    ValueDataType,
    ValueRange,
)
from swcraft_tools.validator import NotResolved, implementation_table, stated_literals
from swcraft_tools.validator.data_types import MAP_MAY_APPLICATION_KINDS as may_application
from swcraft_tools.validator.data_types import MAP_MAY_IMPLEMENTATION_KINDS as may_implementation
from swcraft_tools.validator.data_types import declared_type
from swcraft_tools.validator.init_values import external_type_chain, reference_chain_end
from swcraft_tools.writer.arxml_generation import argument_array_typed


def _read_conversion(compu: CompuMethodInfo) -> tuple[Conversion | None, str | None]:
    """One compu method as a ``conversion``, or ``None`` with the sentence why."""
    coefficients, reason = linear_coefficients(compu)
    if coefficients is None:
        return None, reason
    factor, offset = coefficients
    # Limits are carried whole or not at all; their lines are recorded where the method is consumed.
    limits, _lines = linear_scale_limits(compu)
    return Conversion(factor=factor, offset=offset, limits=_conversion_limits(limits)), None


def _conversion_limits(carried: dict[str, CarriedLimit] | None) -> ConversionLimits | None:
    """The carried scale limits as a ``conversion``'s ``limits``, or ``None`` where none is carried."""
    if carried is None:
        return None
    return ConversionLimits(
        lower=_conversion_bound(carried.get("lower")), upper=_conversion_bound(carried.get("upper"))
    )


def _conversion_bound(bound: CarriedLimit | None) -> ConversionLimitBound | None:
    if bound is None:
        return None
    return ConversionLimitBound(value=bound["value"], interval=bound["interval"])


def _linear_limits(carried: dict[str, CarriedLimit] | None) -> LinearLimits | None:
    """The carried scale limits as a ``linear``'s ``limits``, or ``None`` where none is carried."""
    if carried is None:
        return None
    return LinearLimits(lower=_linear_bound(carried.get("lower")), upper=_linear_bound(carried.get("upper")))


def _linear_bound(bound: CarriedLimit | None) -> LinearLimitBound | None:
    if bound is None:
        return None
    return LinearLimitBound(value=bound["value"], interval=bound["interval"])


def _read_range(constr: DataConstrInfo, *, own_form: bool = False) -> tuple[ValueRange | None, list[str]]:
    """One ``DATA-CONSTR``'s physical limits as a ``range``, with its notes.

    SWCraft's own private form (*own_form*) is read back as the ``range`` it was written
    from, ``range: {}`` included, with no note.

    Only ``PHYS-CONSTRS`` of the first ``DATA-CONSTR-RULE`` is read: ``INTERNAL-CONSTRS``
    is a different quantity and a second rule is an opinion the model cannot hold. A rule
    with neither limit, or a lower limit above the upper one, imports **without** the
    range rather than reaching ``ValueRange``'s refusal, which would abort the import.

    A limit infinite on its own side is no bound and takes no note here
    (:func:`infinite_limit_lines` reports it). A declared limit that cannot be read as a
    number is a note of its own, so a closed range never silently re-exports half-open. A
    limit is carried exactly or not at all: a float spelling no float holds exactly is a
    note, the bound not imported.

    Not reached: an ``INTERVAL-TYPE`` other than ``INFINITE`` is not read, so an ``OPEN``
    bound round-trips as ``CLOSED`` with no note; a rule whose only limits are infinities
    writes no ``DATA-CONSTR`` back.
    """
    if own_form:
        return ValueRange(min=constr.phys_lower, max=constr.phys_upper), []
    notes: list[str] = []
    if constr.rule_count > 1:
        notes.append(
            f"DATA-CONSTR {constr.path} declares {constr.rule_count} DATA-CONSTR-RULEs and only the "
            "first one's PHYS-CONSTRS is read; what the later rules say is not imported"
        )
    if constr.unreadable_limits:
        notes.append(
            f"DATA-CONSTR {constr.path} declares a PHYS-CONSTRS "
            + ", ".join(sorted(constr.unreadable_limits))
            + " this release cannot read as a number; that bound is not imported"
        )
    if constr.inexact_limits:
        notes.append(
            f"DATA-CONSTR {constr.path} declares a PHYS-CONSTRS "
            + ", ".join(sorted(constr.inexact_limits))
            + " no model value holds exactly; that bound is not imported"
        )
    if constr.phys_lower is None and constr.phys_upper is None:
        own_side_infinite = bool(_infinite_sides(constr))
        if not constr.unreadable_limits and not constr.inexact_limits and not own_side_infinite:
            # An unreadable or own-side infinite limit is declared, so "declares no limit" would be false.
            notes.append(
                f"DATA-CONSTR {constr.path} declares no PHYS-CONSTRS limit; the type is imported without a range"
            )
        return None, notes
    if constr.phys_lower is not None and constr.phys_upper is not None and constr.phys_lower > constr.phys_upper:
        notes.append(
            f"DATA-CONSTR {constr.path} declares a PHYS-CONSTRS lower limit above its upper one; "
            "the type is imported without a range"
        )
        return None, notes
    return ValueRange(min=constr.phys_lower, max=constr.phys_upper), notes


#: The ``CATEGORY`` the writer emits on every ``APPLICATION-PRIMITIVE-DATA-TYPE``, enum included.
_WRITTEN_APPLICATION_CATEGORY = "VALUE"


def uncarried_category_note(app: AppPrimitiveDataTypeInfo, kind: Literal["enum", "application_type"]) -> str | None:
    """The physics note for an application primitive re-exported under another ``CATEGORY``.

    *kind* is the SWCraft kind the type is imported as; it and
    :data:`~swcraft_tools.arxml_reader.CARRIED_APPLICATION_CATEGORIES` choose only the
    wording. It is a note on the element, never a skip entry: the element is imported.

    Returns ``None`` when the category is ``VALUE`` or the source declares none.

    What this does **not** reach:

    * A bare primitive collapsed onto its same-name implementation twin: no application
      element is written, so no note is recorded and its category is lost.
    * Whether ``VALUE`` is the category the standard requires: ``[constr_1634]`` also
      admits ``BOOLEAN`` for a ``TEXTTABLE`` method at R22-11, so a ``BOOLEAN``
      enumeration imports as an enum and is noted as re-exported ``VALUE``.
    * The kind, in the second wording: that ``BOOLEAN`` enumeration's note says it has no
      SWCraft kind of its own, although it was imported as an enum.
    """
    if not app.category or app.category == _WRITTEN_APPLICATION_CATEGORY:
        return None
    if app.category in CARRIED_APPLICATION_CATEGORIES:
        # "an" holds only because both members of *kind*'s Literal start with a vowel.
        return (
            f"CATEGORY '{app.category}' is imported as an {kind} -- the type would be re-exported as "
            f"CATEGORY '{_WRITTEN_APPLICATION_CATEGORY}'"
        )
    return (
        f"CATEGORY '{app.category}' has no SWCraft kind of its own -- the type is imported and "
        f"would be re-exported as CATEGORY '{_WRITTEN_APPLICATION_CATEGORY}'"
    )


def read_physics(
    app: AppPrimitiveDataTypeInfo,
    unit_by_path: dict[str, UnitInfo],
    constr_by_path: dict[str, DataConstrInfo],
    compu_by_path: dict[str, CompuMethodInfo],
    compu_method_ref: str | None = None,
    *,
    own_forms: frozenset[str],
) -> tuple[str, Conversion | None, ValueRange | None, set[str], list[str]]:
    """An application primitive's unit, conversion, range and consumed paths, and notes on what is not carried.

    The unit is the ``UNIT``'s ``DISPLAY-NAME``, else its ``SHORT-NAME``: the writer emits
    ``unit`` as ``DISPLAY-NAME`` under a ``SHORT-NAME`` derived from the type, so reading the
    short name first would turn ``km/h`` into ``Speed_Unit`` on every re-import.

    Returns:
        ``(unit, conversion, range, consumed_paths, notes)``. *consumed_paths*
        holds the ``UNIT``, ``COMPU-METHOD`` and ``DATA-CONSTR`` paths this type
        carried inline, so the caller does not report them as unrepresented.
    """
    consumed: set[str] = set()
    notes: list[str] = []

    unit_text = ""
    if app.unit_ref:
        unit_info = unit_by_path.get(app.unit_ref)
        if unit_info is None:
            notes.append(f"UNIT {app.unit_ref} is not declared in this export; the type is imported without a unit")
        else:
            consumed.add(unit_info.path)
            unit_text = carried_unit_text(unit_info)

    conversion: Conversion | None = None
    if compu_method_ref:
        compu = compu_by_path.get(compu_method_ref)
        if compu is None:
            notes.append(
                f"compu method {compu_method_ref} is not declared in this export; the type is "
                "imported without a conversion"
            )
        else:
            conversion, reason = _read_conversion(compu)
            if conversion is not None:
                consumed.add(compu.path)
            if reason is not None:
                notes.append(reason)

    value_range: ValueRange | None = None
    if app.data_constr_ref:
        constr = constr_by_path.get(app.data_constr_ref)
        if constr is None:
            notes.append(
                f"DATA-CONSTR {app.data_constr_ref} is not declared in this export; the type is "
                "imported without a range"
            )
        else:
            consumed.add(constr.path)
            value_range, range_notes = _read_range(constr, own_form=constr.path in own_forms)
            notes.extend(range_notes)

    return unit_text, conversion, value_range, consumed, notes


# ---------------------------------------------------------------------------
# The report lines of the data types and the interfaces
# ---------------------------------------------------------------------------


def report_scale_limit(compu_path: str, tag: str, skip_log: SkipLog) -> None:
    """A ``TEXTTABLE`` scale's limit the writer does not write back. Behavioural."""
    skip_log.record(
        f"{compu_path}/COMPU-INTERNAL-TO-PHYS",
        tag,
        f"{tag} of a TEXTTABLE COMPU-SCALE not carried: it is not proven equal to what SWCraft writes "
        "(the literal's single value, CLOSED)",
        category=SkipCategory.NOT_SUPPORTED,
        effect=ABORT,
    )


def report_linear_limits(compu_path: str, lines: list[ScaleLimitLine], skip_log: SkipLog) -> None:
    """Every line a ``LINEAR`` scale's limits owe, keyed at ``{method}/COMPU-INTERNAL-TO-PHYS``.

    An own-side infinite limit is carried as the absent bound it means (descriptive); an
    unreadable or incoherent pair is not carried (behavioural).
    """
    for line in lines:
        skip_log.record(
            f"{compu_path}/COMPU-INTERNAL-TO-PHYS",
            line.tag,
            line.reason,
            category=SkipCategory.NOT_SUPPORTED,
            effect=ABORT if line.lost else REPORT,
        )


def argument_policy_meaning(type_name: str, data_types_by_name: DataDescription, *, application: bool) -> bool | None:
    """What the writer's output means for an argument typed *type_name*: array (True), non-array (False), or None.

    ``None`` when the type is undeclared or its reference chain ends at no declaration:
    it becomes an external type stub, on which the writer states no
    ``SERVER-ARGUMENT-IMPL-POLICY``.
    """
    declared = declared_type(data_types_by_name, type_name, application=application)
    if declared is None or external_type_chain(type_name, data_types_by_name, application=application) is not None:
        return None
    if isinstance(declared, ReferenceDataType) and reference_chain_end(declared, data_types_by_name) is None:
        return None
    # The writer's own rule, read and not restated.
    return argument_array_typed(type_name, data_types_by_name, application=application)


def unreadable_is_service_reason(text: str) -> str:
    """The line of an ``IS-SERVICE`` text that is no ``xsd:boolean``, whose interface is not imported.

    ``IS-SERVICE`` is a ``BOOLEAN``, whose ``BOOLEAN--SIMPLE`` restricts ``xsd:string`` with
    the pattern ``0|1|true|false`` (XSD R4.3.0:79700-79704; R4.2.2:69226-69230), so any
    other text after XML whitespace is no value of the field. The model's ``is_service``
    cannot be unset and ``false`` would be a value the file never stated, so the interface
    is not carried. The text is quoted by ``repr``, so a no-break space shows as itself.
    """
    return (
        f"IS-SERVICE {text!r} not carried: the text could not be read as a boolean (an IS-SERVICE is an "
        "xsd:boolean -- true, false, 1 or 0), and SWCraft never reads it as false -- the interface is not "
        "imported"
    )


def _interface_members(interface: SRInterfaceInfo | CSInterfaceInfo) -> list[tuple[str, str]]:
    """Every member *interface* declares, as ``(path, tag)``: a data element, or an operation and its arguments.

    Each path is spelled from the ``SHORT-NAME`` as the file writes it, blank included,
    because that is the key a member's difference is reported at.
    """
    if isinstance(interface, SRInterfaceInfo):
        return [(f"{interface.path}/{de.name}", "VARIABLE-DATA-PROTOTYPE") for de in interface.data_elements]
    members: list[tuple[str, str]] = []
    for op in interface.operations:
        members.append((f"{interface.path}/{op.name}", "CLIENT-SERVER-OPERATION"))
        members.extend((f"{interface.path}/{op.name}/{arg.name}", "ARGUMENT-DATA-PROTOTYPE") for arg in op.arguments)
    return members


def drop_interface(
    skip_log: SkipLog, interface: SRInterfaceInfo | CSInterfaceInfo, tag: str, reason: SkipReason
) -> None:
    """Record *interface* as not imported for *reason*, and chain each of its members to that line.

    The one site of every interface drop. A member that already has a line of its tag at
    its own key (an operation refused before the interface was left with none) is not lined
    twice; without a chained line a member would be a silent drop.

    What this does not reach: a member the reader never held (one with no
    ``SHORT-NAME`` element is spelled as if it had an empty one) and the three verdicts
    that leave a member unmapped without a line here -- a conflicting group's member, a
    merged group's alias, a member ``--packages`` excluded.
    """
    root = skip_log.record_reason(interface.path, tag, reason, effect="abort")
    for member_path, member_tag in _interface_members(interface):
        if skip_log.first_tagged(member_path, member_tag) is None:
            skip_log.record_chained(
                member_path,
                member_tag,
                f"chain-skipped: interface '{interface.name}' was skipped ({root.reason})",
                root=root,
            )


def report_server_argument_policy(path: str, literal: str, array: bool | None, skip_log: SkipLog) -> None:
    """A ``SERVER-ARGUMENT-IMPL-POLICY`` meaning other than the writer's output. Behavioural.

    Table 4.8: absent means ``useArrayBaseType`` on an array-typed argument and
    ``useArgumentType`` otherwise; the writer's output means exactly that, so a
    source stating the same is carried by equivalence and any other value --
    ``USE-VOID`` included -- is this line.
    """
    if array is None:
        reason = (
            f"SERVER-ARGUMENT-IMPL-POLICY {literal} on an argument whose type is declared outside the file not "
            "carried: SWCraft writes none there, which means the default of a type neither file declares"
        )
    else:
        reason = (
            f"SERVER-ARGUMENT-IMPL-POLICY {literal} on {'an array-typed' if array else 'a non-array'} argument not "
            f"carried: SWCraft's output means {'USE-ARRAY-BASE-TYPE' if array else 'USE-ARGUMENT-TYPE'} there"
        )
    skip_log.record(
        path,
        "SERVER-ARGUMENT-IMPL-POLICY",
        reason,
        category=SkipCategory.NOT_SUPPORTED,
        effect=ABORT,
    )


def server_argument_policy_carried(literal: str | None, array: bool | None) -> bool:
    """Whether the policy is absent or the one the writer's output means for the argument.

    Where ``array`` is ``None`` the writer states none, so only an absent policy is carried.
    """
    if array is None:
        return literal is None
    return literal is None or literal == ("USE-ARRAY-BASE-TYPE" if array else "USE-ARGUMENT-TYPE")


def value_label_losses(
    value: ConstantValue, type_name: str, data_types_by_name: DataDescription, *, application: bool
) -> int:
    """How many of *value*'s ``SHORT-LABEL``s the writer does not write back.

    The writer labels a record member with its field name and nothing else, so only that
    label is carried. Ask only of a value that mapped, so the shapes agree.
    """

    def walk(node: ConstantValue, declared: object, field_name: str | None) -> int:
        lost = int(node.label is not None and node.label.strip() != (field_name or ""))
        if isinstance(declared, ReferenceDataType):
            declared = reference_chain_end(declared, data_types_by_name)
        if isinstance(node, ConstantRecord) and isinstance(declared, RecordDataType | ImplementationRecordDataType):
            member_of = (
                data_types_by_name.application
                if isinstance(declared, RecordDataType)
                else data_types_by_name.implementation
            )
            members: Sequence[RecordField | ImplementationElement] = (
                declared.fields if isinstance(declared, RecordDataType) else declared.elements
            )
            for member, field_value in zip(members, node.fields, strict=False):
                lost += walk(field_value.value, member_of(member.type), member.name)
        elif isinstance(node, ConstantArray) and isinstance(declared, ArrayDataType | ImplementationArrayDataType):
            element_of = (
                data_types_by_name.application
                if isinstance(declared, ArrayDataType)
                else data_types_by_name.implementation
            )
            for item in node.members:
                lost += walk(item, element_of(declared.element_type), None)
        return lost

    return walk(value, declared_type(data_types_by_name, type_name, application=application), None)


def report_value_label(constant_path: str, skip_log: SkipLog) -> None:
    """One value specification ``SHORT-LABEL`` the writer does not write back. Descriptive."""
    skip_log.record(
        f"{constant_path}/VALUE-SPEC",
        "SHORT-LABEL",
        "SHORT-LABEL of a value specification not carried: SWCraft labels an init value's record members with "
        "their field names and nothing else",
        category=SkipCategory.NOT_SUPPORTED,
        effect=REPORT,
    )


def report_value_line(path: str, tag: str, line: CarriedValueLine, skip_log: SkipLog) -> None:
    """One line about how a carried init value is written, with the line's own category and rule."""
    skip_log.record(path, tag, line.text, category=line.category, rule=line.rule, effect=REPORT)


# ---------------------------------------------------------------------------
# Units, compu methods and data constraints as elements
# ---------------------------------------------------------------------------


class FormReference(NamedTuple):
    """How a type states one form it names: as an element, as the export's path, inline, or not at all.

    ``kind`` is ``element`` (*value* is the element's emitted name), ``kept`` (*value* is the
    export's own path: a conflicting declaration or one a selection excluded), ``inline``
    (*value* is the path of the form read into the type's own field) or ``none``.
    """

    kind: Literal["element", "kept", "inline", "none"]
    value: str | None = None

    @property
    def named(self) -> str | None:
        """What the type's reference field holds: the element's name or the kept path, ``None`` for neither."""
        return self.value if self.kind in ("element", "kept") else None

    @property
    def inline_value(self) -> str | None:
        """The path of the form read into the type's own field, ``None`` unless it is read inline."""
        return self.value if self.kind == "inline" else None


def form_reference(
    ref: str | None,
    layer: str,
    registry: PathRegistry,
    unresolved: UnresolvedReferences | None,
    inline: frozenset[str],
    referrer: UnresolvedReferrer,
) -> FormReference:
    """How a type naming the form at *ref* in *layer* states it (:class:`FormReference`).

    SWCraft's own private form (*inline*) is read inline. Otherwise the element *ref* was
    created as (through a merged group's alias) is named; a conflicting or excluded form
    keeps the export's path, staged against *referrer* and committed by the caller with the
    type; anything else, an undeclared form included, is read inline.
    """
    if ref is None:
        return FormReference("none")
    if ref in inline:
        return FormReference("inline", ref)
    name = registry.resolve(ref)
    if name is not None and registry.layer_of(ref) == layer:
        return FormReference("element", name)
    if unresolved is not None and unresolved.cause(ref) in (CAUSE_CONFLICT, CAUSE_SELECTION):
        kept = unresolved.keep(ref, referrer)
        if kept is not None:
            return FormReference("kept", kept)
    return FormReference("inline", ref)


def report_form_not_carried(path: str, tag: str, reason: str, skip_log: SkipLog) -> None:
    """A form no element holds, or an implementation type's compu method no element answers.

    Behavioural: what the line names (an SI relation, a physical-to-internal direction, an
    implementation table's enumeration constants) is content the model does not hold.
    """
    skip_log.record(path, tag, reason, category=SkipCategory.NOT_SUPPORTED, effect=ABORT)


def report_enum_physics(app: AppPrimitiveDataTypeInfo, skip_log: SkipLog) -> None:
    """An enumeration's ``UNIT-REF`` and ``DATA-CONSTR-REF``, one line each: an ``enum`` holds neither.

    An enumeration states its literals or names a ``texttable``, and no unit and no range.
    What is not carried is the type's reference, keyed at its ``SW-DATA-DEF-PROPS``.
    """
    for tag, ref, what in (("UNIT-REF", app.unit_ref, "unit"), ("DATA-CONSTR-REF", app.data_constr_ref, "range")):
        if ref is not None:
            report_form_not_carried(
                f"{app.path}/SW-DATA-DEF-PROPS",
                tag,
                f"{tag} of an APPLICATION-PRIMITIVE-DATA-TYPE imported as an enum not carried: an enum holds no "
                f"{what}, so the type is written without it",
                skip_log,
            )


def _constr_holds_whole(constr: DataConstrInfo) -> bool:
    """Whether a ``data_constr`` element holds *constr*: one rule of physical limits alone.

    Every stated limit is read exactly, and the lower one is not above the upper one.
    """
    return (
        constr.rule_count == 1
        and not constr.internal_present
        and not constr.unreadable_limits
        and not constr.inexact_limits
        and not (
            constr.phys_lower is not None and constr.phys_upper is not None and constr.phys_lower > constr.phys_upper
        )
    )


@dataclass
class Forms:
    """What :func:`map_forms` created: the elements, their source paths, the created methods by name."""

    elements: list[DataDescriptionElement] = field(default_factory=list)
    created: set[str] = field(default_factory=set)
    compu_by_name: dict[str, CompuMethodInfo] = field(default_factory=dict)


def map_forms(
    model: ImportModel,
    registry: PathRegistry,
    verdict: Callable[[str, str, str], str],
    unresolved: UnresolvedReferences | None,
    inline: frozenset[str],
    skip_log: SkipLog,
) -> Forms:
    """Every ``UNIT``, ``COMPU-METHOD`` and ``DATA-CONSTR`` an element can hold, each an element of its layer.

    A ``UNIT`` is a ``unit`` with its ``DISPLAY-NAME``; a ``TEXTTABLE`` that can be an
    enumeration is a ``texttable``, one entry per scale; a ``LINEAR`` read exactly as
    ``[offset, factor] / [1]`` whose unit is an element too is a ``linear``; a
    ``DATA-CONSTR`` of one rule of physical limits is a ``data_constr`` with its
    ``CONSTR-LEVEL``. Each keeps its source name and its ``DESC``. A merged group is
    created as its primary, a conflicting group creates nothing, and SWCraft's own private
    form (*inline*) is no element.

    Not created, with a line: a unit stating an SI relation, a method stating
    ``COMPU-PHYS-TO-INTERNAL``. Not created, and read inline by a type naming it: a table
    that cannot be an enumeration, a method of another category or shape, a ``LINEAR``
    whose unit is no element, a constraint of several rules or of internal limits.
    """
    forms = Forms()

    def blank(name: str | None) -> bool:
        return not name or not name.strip()

    for unit_info in model.units:
        if unit_info.path in inline or blank(unit_info.name):
            continue
        if verdict(unit_info.path, unit_info.name, LAYER_UNITS) != MAP:
            continue
        if unit_info.si_relation:
            report_form_not_carried(
                unit_info.path,
                "UNIT",
                f"UNIT not imported as an element: it states {', '.join(tag for tag, _ in unit_info.si_relation)}, "
                "which a unit element does not hold -- "
                "a data type naming it carries its text inline, "
                "and whether that unit is compatible with the unit of a compu method is not judged",
                skip_log,
            )
            continue
        name = registry.register(unit_info.path, unit_info.name, LAYER_UNITS)
        forms.elements.append(
            Unit(
                name=name,
                display_name=unit_info.display_name if unit_info.display_name is not None else "",
                description=unit_info.description,
            )
        )
        forms.created.add(unit_info.path)

    for compu in model.compu_methods:
        if compu.path in inline or blank(compu.name):
            continue
        if verdict(compu.path, compu.name, LAYER_COMPU_METHODS) != MAP:
            continue
        if compu.phys_to_internal:
            report_form_not_carried(
                compu.path,
                "COMPU-METHOD",
                "COMPU-METHOD not imported as an element: it states COMPU-PHYS-TO-INTERNAL, and a compu method "
                "element holds the internal-to-physical direction alone -- a data type naming it carries what it "
                "can of it inline",
                skip_log,
            )
            continue
        element: TextTable | Linear
        if compu.category == "TEXTTABLE":
            if texttable_defect(compu) is not None:
                continue
            element = TextTable(
                name=registry.register(compu.path, compu.name, LAYER_COMPU_METHODS),
                kind="texttable",
                entries=[
                    TableEntry(name=scale.literal, value=scale.value, description=scale.description)
                    for scale in compu.scales
                ],
                description=compu.description,
            )
            for tag in texttable_limit_losses(compu):
                report_scale_limit(compu.path, tag, skip_log)
            if compu.unit_ref is not None:
                report_form_not_carried(
                    compu.path,
                    "UNIT-REF",
                    "UNIT-REF of a TEXTTABLE compu-method not carried: a texttable element holds no unit, so the "
                    "table is written without it",
                    skip_log,
                )
        elif compu.category == "LINEAR":
            coefficients, _reason = linear_coefficients(compu)
            if coefficients is None:
                continue
            factor, offset = coefficients
            unit_name = form_reference(
                compu.unit_ref,
                LAYER_UNITS,
                registry,
                unresolved,
                inline,
                UnresolvedReferrer(
                    path=compu.path, tag="COMPU-METHOD", attribute="UNIT-REF", field="compu_methods.linear.unit"
                ),
            )
            if unit_name.named is None:
                continue
            limits, lines = linear_scale_limits(compu)
            element = Linear(
                name=registry.register(compu.path, compu.name, LAYER_COMPU_METHODS),
                kind="linear",
                unit=unit_name.named,
                factor=factor,
                offset=offset,
                limits=_linear_limits(limits),
                description=compu.description,
            )
            report_linear_limits(compu.path, lines, skip_log)
        else:
            continue
        forms.elements.append(element)
        forms.created.add(compu.path)
        forms.compu_by_name[element.name] = compu
        if unresolved is not None:
            unresolved.commit(compu.path)

    #: A merged group's non-primary member with a level: the merge compared the level, so
    #: the primary's element carries it too.
    leveled_aliases: list[DataConstrInfo] = []
    for constr in model.data_constrs:
        if constr.path in inline or blank(constr.name):
            continue
        outcome = verdict(constr.path, constr.name, LAYER_DATA_CONSTRAINTS)
        if outcome == ALIAS and constr.level is not None:
            leveled_aliases.append(constr)
        if outcome != MAP or not _constr_holds_whole(constr):
            continue
        forms.elements.append(
            DataConstr(
                name=registry.register(constr.path, constr.name, LAYER_DATA_CONSTRAINTS),
                min=constr.phys_lower,
                max=constr.phys_upper,
                level=constr.level,
                description=constr.description,
            )
        )
        forms.created.add(constr.path)
        registry.record_represented(constr.path, infinite_limit_lines(constr, inline=False))
        if constr.level is not None:
            # The reader's line about the level is withdrawn once the level is carried.
            registry.carried_reader_lines.add((f"{constr.path}/DATA-CONSTR-RULES", "CONSTR-LEVEL"))
    for constr in leveled_aliases:
        if registry.merge_aliases.get(constr.path) in forms.created:
            registry.carried_reader_lines.add((f"{constr.path}/DATA-CONSTR-RULES", "CONSTR-LEVEL"))
    return forms


# ---------------------------------------------------------------------------
# DATA-TYPE-MAPPING-SET -> the `mapping_sets/` layer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MappedSets:
    """What :func:`map_mapping_sets` returns.

    ``sets`` are the elements, in reader order; ``source_paths`` is each written set's
    declaration path; ``name_by_path`` answers a component's ``DATA-TYPE-MAPPING-REF``, a
    merged group's alias included.
    """

    sets: list[MappingSet]
    source_paths: dict[str, str]
    name_by_path: dict[str, str]


def _kind_in(view: DataDescription, layer: str | None, name: str) -> DataType | None:
    """The element *name* names in *layer*, one of the three data type layers, or ``None``.

    One name may be held in two layers; *layer* is that of the path that resolved to *name*.
    """
    if layer == LAYER_APPLICATION_TYPES:
        return view.application(name)
    if layer == LAYER_IMPLEMENTATION_TYPES:
        return view.implementation(name)
    if layer == LAYER_BASE_TYPES:
        return view.base(name)
    return None


def _map_side_reason(ref: str, side: str, registry: PathRegistry, impl_by_path: dict[str, ImplDataTypeInfo]) -> str:
    """Why one side of a ``DATA-TYPE-MAP`` cannot be written, in one sentence.

    Either the target is an ``IMPLEMENTATION-DATA-TYPE`` of a category outside
    :data:`IMPORTED_IMPLEMENTATION_CATEGORIES`, or it was not imported for a reason of its
    own (an unsupported shape included), which the skipped list names.
    """
    impl = impl_by_path.get(ref)
    if impl is not None and registry.resolve(ref) is None and impl.category not in IMPORTED_IMPLEMENTATION_CATEGORIES:
        return (
            f"{side} side {ref} is an IMPLEMENTATION-DATA-TYPE of CATEGORY "
            f"'{impl.category}', which this release does not import as a data type"
        )
    return f"{side} side {ref} was not imported as a data type (see the skipped list for its own reason)"


@dataclass(frozen=True)
class _SetMapping:
    """What every mapping set of one :func:`map_mapping_sets` call shares."""

    registry: PathRegistry
    skip_log: SkipLog
    merges: MergeDecisions | None
    unresolved: UnresolvedReferences | None
    impl_by_path: dict[str, ImplDataTypeInfo]
    #: The view over the call's own data types, last declaration winning.
    kind_by_name: DataDescription
    #: The rows that caused a twin collapse, both sides read through a merge alias, and their
    #: application sides; decided by the registry's own record, never by a reason's text.
    aliases: dict[str, str]
    collapsing: set[tuple[str, str]]
    collapsed_apps: set[str]

    def canonical(self, path: str) -> str:
        return self.aliases.get(path, path)


def map_mapping_sets(
    model: ImportModel,
    registry: PathRegistry,
    data_types: list[DataType],
    merges: MergeDecisions | None = None,
    unresolved: UnresolvedReferences | None = None,
) -> tuple[MappedSets, SkipLog]:
    """Map every ``DATA-TYPE-MAPPING-SET`` to a ``mapping_sets/`` element.

    The set is written as the export declares it: one element per set, one entry
    per ``DATA-TYPE-MAP``, in declaration order.

    **A map is written when both sides land on a declared data type of a kind that side
    may name**, and is otherwise dropped with a reason; a set is written however many of
    its maps are dropped, unless its ``SHORT-NAME`` is blank, its name was already imported
    or its merge outcome is not MAP (an alias takes its primary's name). Dropped maps:

    * a row whose application side was collapsed onto its same-name implementation twin:
      the row that caused the collapse is a report line, any other self-map an abort;
    * a side naming a declaration this import did not write for a reason of its own;
    * a side resolving to a data type of a kind that side may not name.

    A pair the positional composite check refuses, and a map onto a pointer
    (``[constr_1066]``), are **kept** for ``check-model`` to report; the source's pair is
    never reordered, renamed or substituted to make it pass.

    A side whose target the export declares nowhere, a ``--packages`` selection excluded
    or a conflicting group holds keeps the path the export spelled, under the roles
    ``mapping_sets.map.application`` and ``mapping_sets.map.implementation``. Two
    declarations of one set name are one element when their maps resolve alike and none
    when they do not; ``merges=None`` (``import component``) takes no verdict.

    Returns:
        ``(MappedSets, SkipLog)``.
    """
    skip_log = SkipLog()
    sets: list[MappingSet] = []
    source_paths: dict[str, str] = {}
    name_by_path: dict[str, str] = {}
    used_names: set[str] = set()
    aliases = registry.merge_aliases
    collapsing = {
        (aliases.get(record.app_path, record.app_path), aliases.get(record.impl_path, record.impl_path))
        for record in registry.reduction_records
    }
    ctx = _SetMapping(
        registry=registry,
        skip_log=skip_log,
        merges=merges,
        unresolved=unresolved,
        impl_by_path={impl.path: impl for impl in model.impl_data_types},
        kind_by_name=DataDescription.last_declared(data_types),
        aliases=aliases,
        collapsing=collapsing,
        collapsed_apps={application_path for application_path, _ in collapsing},
    )

    for declaration in model.data_type_mapping_sets:
        if not declaration.name or not declaration.name.strip():
            skip_log.record_reason(declaration.path, "DATA-TYPE-MAPPING-SET", blank_short_name(), effect="abort")
            continue
        outcome = merges.outcome(declaration.path) if merges is not None else MAP
        if outcome != MAP:
            # An alias is resolved by the pass after the loop.
            continue
        if declaration.name in used_names:
            # Without a verdict every declaration maps; one file per element, and no rename policy.
            skip_log.record(
                declaration.path,
                "DATA-TYPE-MAPPING-SET",
                f"a mapping set named '{declaration.name}' was already imported",
                category=SkipCategory.NOT_SUPPORTED,
                effect="abort",
            )
            continue
        entries = _map_set_rows(declaration, ctx)
        sets.append(MappingSet(name=declaration.name, maps=entries, description=declaration.description))
        used_names.add(declaration.name)
        source_paths[declaration.name] = declaration.path
        name_by_path[declaration.path] = declaration.name
        if unresolved is not None:
            unresolved.commit(declaration.path)

    if merges is not None:
        # After the loop: a merged group's non-primary may be declared before its primary.
        for declaration in model.data_type_mapping_sets:
            if declaration.path in name_by_path:
                continue
            primary = merges.primary_path(declaration.path)
            if primary in name_by_path and merges.outcome(declaration.path) == ALIAS:
                name_by_path[declaration.path] = name_by_path[primary]

    return MappedSets(sets=sets, source_paths=source_paths, name_by_path=name_by_path), skip_log


def _map_set_rows(declaration: DataTypeMappingSetInfo, ctx: _SetMapping) -> list[DataTypeMap]:
    """The ``DATA-TYPE-MAP`` rows of one set that are written, in declaration order."""
    registry = ctx.registry
    entries: list[DataTypeMap] = []
    # Sides as `(layer, name)`, never bare names: a kept same-name pair is two elements under one name.
    written_sides: list[tuple[tuple[str | None, str], tuple[str | None, str]]] = []
    # One row per application type, and a row both of whose sides resolve is preferred over
    # one that needs the export's path: a `--packages` selection may leave a map onto each
    # member of a group. Keyed on the element the application side resolves to, so two
    # members of one merged group are one application type. A map is no referrable, so
    # every line is keyed at `{set}/DATA-TYPE-MAPS`.
    resolvable: set[tuple[str | None, str]] = set()
    for entry in declaration.maps:
        resolved_application = registry.resolve(entry.application_ref)
        if resolved_application is not None and registry.resolve(entry.implementation_ref) is not None:
            resolvable.add((registry.layer_of(entry.application_ref), resolved_application))
    for entry in declaration.maps:
        _map_set_row(entry, declaration, entries, written_sides, resolvable, ctx)
    return entries


def _map_set_row(
    entry: DataTypeMapInfo,
    declaration: DataTypeMappingSetInfo,
    entries: list[DataTypeMap],
    written_sides: list[tuple[tuple[str | None, str], tuple[str | None, str]]],
    resolvable: set[tuple[str | None, str]],
    ctx: _SetMapping,
) -> None:
    """Write one ``DATA-TYPE-MAP`` row onto *entries*, or drop it with a line in ``ctx.skip_log``.

    *entries* and *written_sides* grow by the row written; *resolvable* holds the
    application elements some row of the set resolves on both sides.
    """
    registry, skip_log, unresolved = ctx.registry, ctx.skip_log, ctx.unresolved
    if (
        registry.layer_of(entry.application_ref),
        registry.resolve(entry.application_ref),
    ) in resolvable and registry.resolve(entry.implementation_ref) is None:
        # Another row for this application type resolves both sides, whatever the order.
        skip_log.record(
            f"{declaration.path}/DATA-TYPE-MAPS",
            "DATA-TYPE-MAP",
            f"a second row of this set maps {entry.application_ref} onto a type this import carries",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return
    # Resolved here, staged only after every refusal: `commit` is keyed on the set's path, so
    # a reference staged by a dropped row would be promoted by the set's commit.
    staged: list[tuple[str, str, str]] = []
    application = registry.resolve(entry.application_ref)
    if application is None and unresolved is not None:
        application = unresolved.answer(entry.application_ref)
        if application is not None:
            staged.append((entry.application_ref, "APPLICATION-DATA-TYPE-REF", "mapping_sets.map.application"))
    if application is None:
        skip_log.record(
            f"{declaration.path}/DATA-TYPE-MAPS",
            "DATA-TYPE-MAP",
            _map_side_reason(entry.application_ref, "application", registry, ctx.impl_by_path),
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return
    application_side = (registry.layer_of(entry.application_ref), application)
    same_pair = next(
        (
            written
            for written, sides in zip(entries, written_sides, strict=True)
            if sides
            == (
                application_side,
                (registry.layer_of(entry.implementation_ref), registry.resolve(entry.implementation_ref)),
            )
        ),
        None,
    )
    if same_pair is not None:
        # The surviving row maps the same pair, so nothing is lost: descriptive.
        skip_log.record(
            f"{declaration.path}/DATA-TYPE-MAPS",
            "DATA-TYPE-MAP",
            f"a second row maps the pair '{same_pair.application}' -> '{same_pair.implementation}' this "
            "set already maps: the one row is written",
            category=SkipCategory.NOT_SUPPORTED,
            effect=REPORT,
        )
        return
    if any(sides[0] == application_side for sides in written_sides):
        # The model refuses a second row for one application type at load. Asked before the
        # implementation side, so a row that cannot be written stages no reference.
        skip_log.record(
            f"{declaration.path}/DATA-TYPE-MAPS",
            "DATA-TYPE-MAP",
            f"the application type '{application}' is already mapped in this set",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return
    implementation = registry.resolve(entry.implementation_ref)
    if implementation is None and unresolved is not None:
        implementation = unresolved.answer(entry.implementation_ref)
        if implementation is not None:
            staged.append((entry.implementation_ref, "IMPLEMENTATION-DATA-TYPE-REF", "mapping_sets.map.implementation"))
    if implementation is None:
        skip_log.record(
            f"{declaration.path}/DATA-TYPE-MAPS",
            "DATA-TYPE-MAP",
            _map_side_reason(entry.implementation_ref, "implementation", registry, ctx.impl_by_path),
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return
    application_layer = registry.layer_of(entry.application_ref)
    implementation_layer = registry.layer_of(entry.implementation_ref)
    if application_layer is not None and (application_layer, application) == (
        implementation_layer,
        implementation,
    ):
        reason, folded = _self_map_line(entry, application, implementation, ctx)
        skip_log.record(
            f"{declaration.path}/DATA-TYPE-MAPS",
            "DATA-TYPE-MAP",
            reason,
            category=SkipCategory.NOT_SUPPORTED,
            effect=REPORT if folded else ABORT,
        )
        return
    # The kinds each side may name are the validator's own sets, never restated here. A side
    # kept as the export's path resolves into no layer and is judged by nothing here.
    application_kind = _kind_in(ctx.kind_by_name, application_layer, application)
    if application_kind is not None and not isinstance(application_kind, may_application):
        skip_log.record(
            f"{declaration.path}/DATA-TYPE-MAPS",
            "DATA-TYPE-MAP",
            f"application side resolves to the data type '{application}', which is a "
            f"'{application_kind.kind}'; only an application type, an enum, a record or an array may be "
            "mapped",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return
    implementation_kind = _kind_in(ctx.kind_by_name, implementation_layer, implementation)
    if implementation_kind is not None and not isinstance(implementation_kind, may_implementation):
        skip_log.record(
            f"{declaration.path}/DATA-TYPE-MAPS",
            "DATA-TYPE-MAP",
            f"implementation side resolves to the data type '{implementation}', which is a "
            f"'{implementation_kind.kind}'; only a value type, a reference type, an implementation "
            "record, an implementation array or a pointer is written on that side",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return
    entries.append(DataTypeMap(application=application, implementation=implementation))
    written_sides.append((application_side, (implementation_layer, implementation)))
    for target, attribute, field_role in staged:
        assert unresolved is not None
        unresolved.keep(
            target,
            UnresolvedReferrer(
                path=declaration.path,
                tag="DATA-TYPE-MAPPING-SET",
                attribute=attribute,
                field=field_role,
            ),
        )


def _self_map_line(entry: DataTypeMapInfo, application: str, implementation: str, ctx: _SetMapping) -> tuple[str, bool]:
    """The skip line for a row whose two sides are one element, and whether the row is a collapse's own.

    Asked of the element, `(layer, name)`, never of the name alone: a kept same-name pair is
    two elements of two layers, and its map is written.
    """
    application_key = ctx.canonical(entry.application_ref)
    folded = (application_key, ctx.canonical(entry.implementation_ref)) in ctx.collapsing
    if folded:
        # The row that caused the importer's own collapse: a reduction consequence, never a
        # refusal. Named as the file declares it, since `application` is the survivor's name.
        declared = entry.application_ref.rsplit("/", 1)[-1]
        reason = (
            f"the map row of the same-name pair '{declared}' is folded into its collapse: both sides "
            f"are the one implementation type '{implementation}'"
        )
    else:
        # A self-map the file itself carries; the parenthesis claims a collapse only when one happened.
        collapsed = application_key in ctx.collapsed_apps
        reason = f"both sides resolve to the data type '{application}'" + (
            " (the same-name pair was collapsed)" if collapsed else ""
        )
    return reason, folded


# ---------------------------------------------------------------------------
# Interface mapping (SR + CS)
# ---------------------------------------------------------------------------

CS_DIRECTION_MAP: dict[str, Literal["in", "out", "inout"]] = {"IN": "in", "OUT": "out", "INOUT": "inout"}

#: The last path segment of a skip line about a member with no ``SHORT-NAME``. Not the
#: owner's path, which would say the owner did not survive; angle brackets are illegal in
#: an AUTOSAR ``Identifier``, so the token collides with no declared name.
UNNAMED_MEMBER = "<unnamed>"


# ---------------------------------------------------------------------------
# The element-level init value
#
# `INIT-VALUE -> CONSTANT-REFERENCE -> CONSTANT-SPECIFICATION -> value`, shaped against
# the element's declared type. A refused value imports the element without its init:
# the field is optional and a wrong init is not.
# ---------------------------------------------------------------------------


def _com_spec_init_key(com_spec: PortComSpecInitInfo) -> str:
    """The path a port-level ``INIT-VALUE`` line is keyed at: ``{port}/{block}``.

    The com-spec's init value lies in non-referrable structure below the imported port.
    """
    return f"{com_spec.port_path}/{com_spec_block(com_spec.block_tag, com_spec.com_spec_tag)}"


def _com_spec_init_source(com_spec: PortComSpecInitInfo) -> str:
    """What one port COM-SPEC's INIT-VALUE names, in the reader's own words.

    `unreadable_reason` is quoted, never re-authored: only the reader knows which inner
    tags it found.
    """
    if com_spec.constant_ref is not None:
        return f"references constant '{com_spec.constant_ref}'"
    return f"this reader could not follow ({com_spec.unreadable_reason})"


def port_com_spec_inits(model: ImportModel, skip_log: SkipLog) -> dict[str, list[PortComSpecInitInfo]]:
    """Every port COM-SPEC init the reader found, keyed by the path of the data element it names.

    An SWC's ports and a composition's boundary ports are both walked. A COM-SPEC whose
    DATA-ELEMENT-REF is absent or blank binds to no data element and is **reported here**
    rather than dropped, naming the port, the tag and the constant; only COM-SPEC kinds
    AUTOSAR defines with a DATA-ELEMENT-REF reach it.

    What this does not reach: whether a key is ever read. A caller must run
    :func:`report_unread_port_com_spec_inits` once its elements are mapped, or every
    unread entry is dropped silently; nothing enforces that.
    """
    by_element: dict[str, list[PortComSpecInitInfo]] = {}
    owners: list[SwcInfo | CompositionInfo] = [*model.swcs, *model.compositions]
    for owner in owners:
        for port in owner.ports:
            for com_spec in port.com_spec_inits:
                if not (ref := com_spec.data_element_ref or "").strip():
                    skip_log.record(
                        _com_spec_init_key(com_spec),
                        "INIT-VALUE",
                        f"carries an INIT-VALUE that {_com_spec_init_source(com_spec)} but has "
                        "a missing or blank DATA-ELEMENT-REF, so it names no data element whose "
                        "type the value could be carried against",
                        category=SkipCategory.NOT_SUPPORTED,
                        # A com-spec value on a descriptive port: a line, never a refusal.
                        effect="report",
                    )
                    continue
                by_element.setdefault(ref, []).append(com_spec)
    return by_element


#: Why an unread port COM-SPEC init was not read, one sentence per cause.
_UNREAD_INIT_ABSENT = "the file declares no interface at '{interface}'"
_UNREAD_INIT_CONFLICT = (
    "its interface '{interface}' is one of several conflicting declarations of one name, none of which was imported"
)
_UNREAD_INIT_SELECTION = "its interface '{interface}' is a declaration the package selection left out"
_UNREAD_INIT_MERGED = (
    "its interface '{interface}' was merged with another declaration of the same name and the element was "
    "mapped under that one"
)
_UNREAD_INIT_NO_SUCH_ELEMENT = "its interface '{interface}' was imported and declares no data element of that name"
_UNREAD_INIT_SKIPPED = "its interface '{interface}' was skipped"


def _unread_init_cause(
    interface_path: str,
    *,
    skip_log: SkipLog,
    declared: Collection[str],
    mapped_interface_paths: Collection[str],
    merges: MergeDecisions | None,
    unresolved: UnresolvedReferences | None,
) -> tuple[str, SkipCategory]:
    """Which one cause an unread port COM-SPEC init has, and its category.

    Asked in order: the unresolved-reference policy's cause (a target the file declares
    nowhere is :attr:`SkipCategory.OUTSIDE_FILE`, a conflicting group or a ``--packages``
    selection SWCraft's), a merged group's other member, an imported interface with no
    element of that name, an interface this mapper skipped; without a policy, *declared*
    tells whether the file declares the interface at all. Everything but outside the file
    is :attr:`SkipCategory.NOT_SUPPORTED`.

    What this does not reach: a hand-built model without a path index declares nothing,
    so its interfaces are answered *outside the file*.
    """
    if unresolved is not None:
        cause = unresolved.cause(interface_path)
        if cause == CAUSE_ABSENT:
            return _UNREAD_INIT_ABSENT, SkipCategory.OUTSIDE_FILE
        if cause == CAUSE_CONFLICT:
            return _UNREAD_INIT_CONFLICT, SkipCategory.NOT_SUPPORTED
        if cause == CAUSE_SELECTION:
            return _UNREAD_INIT_SELECTION, SkipCategory.NOT_SUPPORTED
    if merges is not None and merges.outcome(interface_path) == ALIAS:
        return _UNREAD_INIT_MERGED, SkipCategory.NOT_SUPPORTED
    if interface_path in mapped_interface_paths:
        return _UNREAD_INIT_NO_SUCH_ELEMENT, SkipCategory.NOT_SUPPORTED
    if skip_log.first_at(interface_path) is not None:
        return _UNREAD_INIT_SKIPPED, SkipCategory.NOT_SUPPORTED
    if unresolved is None and interface_path not in declared:
        return _UNREAD_INIT_ABSENT, SkipCategory.OUTSIDE_FILE
    return _UNREAD_INIT_SKIPPED, SkipCategory.NOT_SUPPORTED


@dataclass(frozen=True)
class HeldUnreadInit:
    """One unread port COM-SPEC init line whose interface the file declares nowhere, held.

    ``import component`` answers such a path only after the target project is read, so the
    line is recorded by the late pass (:meth:`record`).
    """

    key: str
    interface_path: str
    element_path: str
    com_spec: PortComSpecInitInfo
    reason: str
    category: SkipCategory

    def record(self, skip_log: SkipLog, *, stub: str | None = None) -> SkipEntry:
        """Record the held line, descriptive, naming the interface stub *stub* if one took the path.

        Call only for a path the target project did not answer; an answered one is decided
        by :func:`late_port_init_outcome`.
        """
        if stub is None:
            return skip_log.record(self.key, "INIT-VALUE", self.reason, category=self.category, effect="report")
        return skip_log.record(
            self.key,
            "INIT-VALUE",
            _stub_unread_init_reason(
                _com_spec_init_source(self.com_spec), self.element_path, self.interface_path, stub
            ),
            category=SkipCategory.NOT_SUPPORTED,
            effect="report",
        )


def _stub_unread_init_reason(source: str, element_path: str, interface_path: str, stub: str) -> str:
    """The line of a port com-spec init whose element is a member of the external interface stub *stub*."""
    return (
        f"carries an INIT-VALUE that {source} but its DATA-ELEMENT-REF names "
        f"'{element_path}', a member of the external interface stub '{stub}' "
        f"({interface_path}), which holds the names of its members and never their types, so "
        "there is no data element type the value could be carried against"
    )


def late_port_init_outcome(
    com_spec: PortComSpecInitInfo,
    element: DataElement,
    constants_by_path: dict[str, ConstantSpecInfo],
    data_description: DataDescription,
    source_types: SourceTypes,
) -> PortInitOutcome:
    """A port's own init value for one element of an interface the target project answered.

    :func:`port_init_outcome` asked against the target element's type and ``init``, the
    file's constants and *data_description* (the target's merged with the file's mapped
    types). Every com-spec is decided, a ``CONSTANT-REF`` to the element's own constant
    included: the target element names no constant path to compare it with.

    What this does not reach: the file declares no type for the element, so the
    value-form judgement (``[constr_1221]``, ``[constr_1385]``) is not applied; ranges and
    base-type sizes stay the conformance gate's.
    """
    application = element.application_type is not None
    type_name = element.application_type if application else element.implementation_type
    return port_init_outcome(
        com_spec,
        element.init,
        type_name or "",
        constants_by_path,
        data_description,
        application=application,
        source_type_ref="",
        source_types=source_types,
    )


def report_unread_port_com_spec_inits(
    port_inits_by_element: dict[str, list[PortComSpecInitInfo]],
    read_element_paths: set[str],
    skip_log: SkipLog,
    unresolved: UnresolvedReferences | None = None,
    *,
    declared: Collection[str],
    mapped_interface_paths: Collection[str],
    merges: MergeDecisions | None = None,
    held: list[HeldUnreadInit] | None = None,
) -> None:
    """Every keyed port COM-SPEC init that no data element read, reported.

    Call at the end of :func:`map_interfaces`, once the read keys are known; the lines
    then follow the interface skip lines they chain from. One line per COM-SPEC, keyed at
    its port, naming the one cause :func:`_unread_init_cause` decides, with its category.

    What this does not reach: an element whose key was read and whose interface a later
    element abandoned (the interface's chained line covers both); a DATA-ELEMENT-REF
    padded with whitespace (no ref in the importer is stripped); a COM-SPEC naming a
    merged group's non-primary member, which is reported rather than resolved through the
    merge, because constants merge by value and overrides are decided by constant path.

    *held* (``import component``, with *unresolved* ``None``) collects each line whose
    interface the file declares nowhere as a :class:`HeldUnreadInit`, for the late pass;
    otherwise every line is recorded here.
    """
    for element_path, com_specs in port_inits_by_element.items():
        if element_path in read_element_paths:
            continue
        interface_path = element_path.rpartition("/")[0]
        cause, category = _unread_init_cause(
            interface_path,
            skip_log=skip_log,
            declared=declared,
            mapped_interface_paths=mapped_interface_paths,
            merges=merges,
            unresolved=unresolved,
        )
        for com_spec in com_specs:
            source = _com_spec_init_source(com_spec)
            reason = (
                f"carries an INIT-VALUE that {source} but its "
                f"DATA-ELEMENT-REF names '{element_path}', for which this import mapped no "
                f"data element under that path ({cause.format(interface=interface_path)}), so there is "
                "no data element whose type the value could be carried against"
            )
            if held is not None and unresolved is None and cause == _UNREAD_INIT_ABSENT:
                # `import component` answers the path after the target project is read.
                held.append(
                    HeldUnreadInit(
                        key=_com_spec_init_key(com_spec),
                        interface_path=interface_path,
                        element_path=element_path,
                        com_spec=com_spec,
                        reason=reason,
                        category=category,
                    )
                )
                continue
            if unresolved is None or category is not SkipCategory.OUTSIDE_FILE:
                # Only a target the file declares nowhere can become a stub and be re-worded.
                skip_log.record(_com_spec_init_key(com_spec), "INIT-VALUE", reason, category=category, effect="report")
                continue
            # The interface may become an external interface stub; the value is never carried,
            # because a stub holds its members' names and never their types.
            unresolved.chain_skip(
                skip_log,
                _com_spec_init_key(com_spec),
                "INIT-VALUE",
                reason,
                target=interface_path,
                layer=LAYER_INTERFACES,
                once_stubbed=partial(_stub_unread_init_reason, source, element_path, interface_path),
                effect="report",
            )


def undeclared_type_reason(type_name: str) -> str:
    """Why no init can be shaped against *type_name*: it is not among the mapped data types.

    One spelling, so the stub re-wording cannot miss the sentence it replaces.
    """
    return f"declared type '{type_name}' is not among the mapped data types"


def stub_typed_init_reason(init_reason: str, missing: str, stub: str, path: str) -> str:
    """The init line of an element whose declared type became the external type stub *stub*.

    Also the line of a port's own value for such an element. A reason holding *missing*
    is re-worded in place; any other keeps its sentence and gains a clause naming the stub.
    """
    if missing in init_reason:
        return init_reason.replace(
            missing,
            f"declared type is the external type stub '{stub}' ({path}), whose content "
            "the export does not declare, so no init value can be shaped against it",
        )
    return f"{init_reason}; the element's declared type is the external type stub '{stub}' ({path})"


# ---------------------------------------------------------------------------
# The judgement of a source init value against the SOURCE's types
# ---------------------------------------------------------------------------

#: The categories of a table on the implementation side: the two ``[constr_1225]``
#: names in both releases (SWCT R4.2.2 ``:15421``, R4.3.0 ``:16544``).
_TABLE_CATEGORIES = frozenset({"TEXTTABLE", "BITFIELD_TEXTTABLE"})
#: The two categories ``[constr_1284]`` admits a text value under in R4.2.2
#: (``:20008``) and not in R4.3.0 (``:21951``): no table here, and a text value
#: under one is SWCraft's own line, never the rule's.
_MIXED_TABLE_CATEGORIES = frozenset({"SCALE_LINEAR_AND_TEXTTABLE", "SCALE_RATIONAL_AND_TEXTTABLE"})

#: The opening words of every init line whose value is not carried.
INIT_NOT_CARRIED = "the init value is not carried: "
#: The opening of a prototype's own value given as a number and carried as the literal its
#: type's table gives that number -- a defect line (``[constr_1221]`` or ``[constr_1225]``)
#: whose value is carried.
INIT_CARRIED = "init value carried: "
#: One number read through its type's ``TEXTTABLE``: a ``str.format`` pattern over
#: ``number`` (as the file spells it) and ``literal`` (the scale's ``VT``).
NUMBER_READ_AS_LITERAL_SENTENCE = (
    "given as the number {number}, carried as the literal {literal} its type's table gives that number"
)


class InitCause(NamedTuple):
    """One cause an init leaf is not carried for: its section, its rule, and its words."""

    #: 1 -- the file breaks a rule (:attr:`SkipCategory.EXPORT_DEFECT`); 2 --
    #: SWCraft's own (:attr:`SkipCategory.NOT_SUPPORTED`).
    section: Literal[1, 2]
    rule: StandardRule | None
    #: What the counted leaves are; the line follows it with ``" (1 leaf)"`` /
    #: ``" ({n} leaves)"``.
    words: str


#: A prototype's own value of an application primitive type given as a text value:
#: ``[constr_1221]``'s first sentence asks an application value.
TEXT_VALUE_UNDER_AN_APPLICATION_TYPE = InitCause(
    1,
    StandardRule.APPLICATION_VALUE_FOR_AN_APPLICATION_PRIMITIVE,
    "a text value where its application primitive type takes an application value",
)
#: A prototype's own value of an application primitive type that is not an
#: enumeration, given in the numerical form: ``[constr_1221]``'s first sentence asks
#: an application value.
NUMBER_WHERE_AN_APPLICATION_VALUE_IS_TAKEN = InitCause(
    1,
    StandardRule.APPLICATION_VALUE_FOR_AN_APPLICATION_PRIMITIVE,
    "a number in the numerical form where its application primitive type takes an application value",
)
#: A prototype's own value of an enumeration -- an application primitive whose compu
#: method is a ``TEXTTABLE`` -- that is not the application form with one text naming
#: one of its scales: a text naming none, or a number its table does not read as one
#: literal. ``[constr_1221]``'s second sentence asks a value that matches one of the
#: scales' text values.
NOT_AN_ENUMERATION_TEXT = InitCause(
    1,
    StandardRule.APPLICATION_VALUE_FOR_AN_APPLICATION_PRIMITIVE,
    "not an application value whose text names one scale of its enumeration, nor a number its table reads as one",
)
#: A prototype's own value under an implementation type that references a table,
#: other than a text value naming one of its scales or a number the table reads as one
#: literal (``[constr_1225]``).
NOT_A_TEXT_VALUE_UNDER_A_TABLE = InitCause(
    1,
    StandardRule.TABLE_INIT_IS_A_TEXT_VALUE,
    "not a text value naming one scale of the table its implementation type references, nor a number that table "
    "reads as one",
)
#: A prototype's own value under an implementation type with no table, given in the
#: application form (``[constr_1385]``).
APPLICATION_VALUE_UNDER_AN_IMPLEMENTATION_TYPE = InitCause(
    1,
    StandardRule.NO_APPLICATION_VALUE_FOR_AN_IMPLEMENTATION_TYPE,
    "an application value where its implementation type takes none",
)
#: A text value under a type with no table in an implementation type's role
#: (``[constr_1284]``, in both releases).
TEXT_VALUE_WITHOUT_A_TABLE = InitCause(
    1,
    StandardRule.TEXT_VALUE_UNDER_A_TABLE_ONLY,
    "a text value under a type that references no TEXTTABLE or BITFIELD_TEXTTABLE implementation compu method",
)
#: The application form with a text (a ``VT``) under a type that takes a number --
#: an application primitive that is not an enumeration, or an implementation type
#: with no table -- as a prototype's own value or as a member. The model holds a text
#: only as an entry's name, and no rule of both releases names this case: SWCraft's
#: own line.
APPLICATION_TEXT_WHERE_A_NUMBER_IS_TAKEN = InitCause(
    2,
    None,
    "a text in the application form under a type that takes a number",
)
#: A text value under an implementation type whose compu method is one of the two
#: mixed categories: R4.2.2 admits it, R4.3.0 does not, so no rule is cited.
TEXT_VALUE_UNDER_A_MIXED_TABLE = InitCause(
    2,
    None,
    "a text value under a SCALE_LINEAR_AND_TEXTTABLE or SCALE_RATIONAL_AND_TEXTTABLE compu method, which R4.2.2 "
    "admits and R4.3.0 does not",
)
#: A member, given as a number, under an implementation type that references a table,
#: which the table does not read as one literal (:func:`_scale_reading`): a number no
#: single scale names, a ``BITFIELD_TEXTTABLE``, a table with a ``SYMBOL`` or one the
#: reader dropped a scale of.
MEMBER_NUMBER_UNDER_A_TABLE = InitCause(
    2,
    None,
    "a member given as a number under a type that references a table, which no one single-point scale of a whole "
    "TEXTTABLE without a SYMBOL names",
)
#: A member under an implementation type that references a table, given as a text
#: that names none of its scales or in the application form.
MEMBER_NAMING_NO_ENTRY = InitCause(
    2,
    None,
    "a member under a type that references a table that is no text value naming one of its scales",
)
#: A member of an enumeration, given as a number in either form, which its table does
#: not read as one literal (:func:`_scale_reading`). No rule of R4.2.2 prescribes a
#: member's form, and R4.3's ``[TPS_SWCT_01695]`` calls a value unusable only where it
#: matches no scale's symbolic name: SWCraft's own line, in files of both releases.
ENUMERATION_MEMBER_GIVEN_AS_A_NUMBER = InitCause(
    2,
    None,
    "an enumeration member given as a number, which no one single-point scale of a whole table without a SYMBOL names",
)
#: A member of an enumeration in the application form whose text names none of its
#: scales: SWCraft's own line, as the member's number is.
ENUMERATION_MEMBER_NAMING_NO_SCALE = InitCause(
    2,
    None,
    "an enumeration member whose text in the application form names none of its scales",
)
#: A text equal to a scale's ``SHORT-LABEL`` and to no ``VT``: SWCraft holds one
#: name per entry, its ``VT``.
TEXT_NAMING_A_SHORT_LABEL = InitCause(
    2,
    None,
    "a text naming a scale by its SHORT-LABEL alone, where SWCraft holds one name per entry, the scale's VT",
)
#: A text under a table one of whose scales states a ``SYMBOL``: R4.3's
#: ``[TPS_SWCT_01696]`` ranks a symbol first, and SWCraft holds none.
TEXT_UNDER_A_SYMBOL_TABLE = InitCause(
    2,
    None,
    "a text under a table whose scales state a SYMBOL, which SWCraft does not hold",
)

#: Every cause, in the tables' order: the section-1 causes by rule order
#: (``[constr_1221]``, ``[constr_1225]``, ``[constr_1385]``, ``[constr_1284]``),
#: then the section-2 ones. The line is section 1 with the first rule broken in
#: this order when any cause is a rule's, section 2 otherwise.
INIT_CAUSES: tuple[InitCause, ...] = (
    TEXT_VALUE_UNDER_AN_APPLICATION_TYPE,
    NUMBER_WHERE_AN_APPLICATION_VALUE_IS_TAKEN,
    NOT_AN_ENUMERATION_TEXT,
    NOT_A_TEXT_VALUE_UNDER_A_TABLE,
    APPLICATION_VALUE_UNDER_AN_IMPLEMENTATION_TYPE,
    TEXT_VALUE_WITHOUT_A_TABLE,
    APPLICATION_TEXT_WHERE_A_NUMBER_IS_TAKEN,
    TEXT_VALUE_UNDER_A_MIXED_TABLE,
    MEMBER_NUMBER_UNDER_A_TABLE,
    MEMBER_NAMING_NO_ENTRY,
    ENUMERATION_MEMBER_GIVEN_AS_A_NUMBER,
    ENUMERATION_MEMBER_NAMING_NO_SCALE,
    TEXT_NAMING_A_SHORT_LABEL,
    TEXT_UNDER_A_SYMBOL_TABLE,
)

#: The line of a collapsed twin whose implementation type references a table: the model
#: can hold only an entry's name there, and a number is read as a literal only through
#: the file's own type, which states no table.
TWIN_TABLE_SENTENCE = (
    INIT_NOT_CARRIED + "the type is imported as its implementation twin, which references a table, "
    "and SWCraft reads a number as a literal only through the file's own type"
)

#: **The closed set of init line patterns**, one per cause, the twin line and the two
#: reading lines, as ``str.format`` patterns -- a cause's over ``{count}`` and
#: ``{leaves}``, a reading's over ``{number}`` and ``{literal}``. Nothing else in this
#: module writes a line about an init value.
INIT_LINE_PATTERNS: tuple[str, ...] = (
    *(f"{cause.words} ({{count}} {{leaves}})" for cause in INIT_CAUSES),
    TWIN_TABLE_SENTENCE,
    INIT_CARRIED + NUMBER_READ_AS_LITERAL_SENTENCE,
    NUMBER_READ_AS_LITERAL_SENTENCE,
)


def init_line_reason(causes: Sequence[tuple[InitCause, int]]) -> SkipReason:
    """One line for one value that is not carried: every cause with its leaf count, in the tables' order."""
    ordered = sorted(causes, key=lambda item: INIT_CAUSES.index(item[0]))
    text = INIT_NOT_CARRIED + "; ".join(
        f"{cause.words} ({count} {'leaf' if count == 1 else 'leaves'})" for cause, count in ordered
    )
    ruled = [cause for cause, _count in ordered if cause.section == 1]
    if ruled:
        return SkipReason(text, SkipCategory.EXPORT_DEFECT, ruled[0].rule)
    return not_supported(text)


class SourceTypes:
    """The file's own type records by path: what a source init value is judged against.

    The source's types, never the model's: a reduction or a merge changes what is written,
    never what the source is held to.
    """

    def __init__(self, model: ImportModel) -> None:
        self.application = {info.path: info for info in model.app_primitive_data_types}
        self.records = {info.path: info for info in model.app_record_data_types}
        self.arrays = {info.path: info for info in model.app_array_data_types}
        self.implementation = {info.path: info for info in model.impl_data_types}
        self.compu_methods = {info.path: info for info in model.compu_methods}


class InitReading(NamedTuple):
    """One leaf given as a number and read through its type's table.

    ``number`` is the number as the file spells it, ``literal`` the scale's ``VT``, ``own``
    whether the leaf is the prototype's own value (a defect line) or a member (a
    descriptive one), and ``rule`` the rule an own value breaks: ``[constr_1221]``
    under an enumeration, ``[constr_1225]`` under an implementation type with a table.
    """

    number: str
    literal: str
    own: bool
    rule: StandardRule


class InitJudgement(NamedTuple):
    """What :func:`read_init_value` answers: the causes, the readings, and the value the model shapes.

    ``value`` is the source value with every read leaf replaced by its literal's text.
    """

    causes: tuple[tuple[InitCause, int], ...]
    readings: tuple[InitReading, ...]
    value: ConstantValue


class _Read(NamedTuple):
    """A leaf's judgement when its table reads the number as one literal."""

    literal: str


class _Tally(dict[InitCause, int]):
    """The leaves counted per cause by one walk, whether an application value states another unit, and the readings."""

    #: An application-form leaf under an application primitive whose
    #: ``UNIT-REF`` is not the unit its type states (another, one the type does not
    #: state, or none where the type states one).
    unit_differs: bool = False

    def __init__(self) -> None:
        super().__init__()
        #: Each leaf read through its type's table, in walk order.
        self.readings: list[InitReading] = []
        #: The literal each read leaf is shaped as, by the leaf object's identity within this walk.
        self.literals: dict[int, str] = {}


def judge_init_value(value: ConstantValue, type_ref: str, source: SourceTypes) -> tuple[tuple[InitCause, int], ...]:
    """Why *value*, declared under the file's type *type_ref*, is not carried -- or nothing.

    :func:`read_init_value`'s causes.
    """
    return read_init_value(value, type_ref, source).causes


def read_init_value(value: ConstantValue, type_ref: str, source: SourceTypes) -> InitJudgement:
    """Why *value*, declared under the file's type *type_ref*, is not carried; and what was read.

    Walks the value against the file's **source** type records, a record's members by
    position and an array's by index. Returns each cause with its leaf count, in the
    tables' order; empty where every leaf conforms **and where the walk cannot pair the
    value with a source type**, so :func:`_map_init_value`'s own line stands.

    It judges: an implementation type that references a table, a text value anywhere, an
    application primitive that is not an enumeration (its own value in the application
    form, ``[constr_1221]``), an implementation type with no table (its own value never in
    the application form, ``[constr_1385]``) and an enumeration (the application form with
    one text naming one of its scales). A number under an enumeration or a ``TEXTTABLE``
    that the table reads as one literal names no cause and is an :class:`InitReading`;
    one it does not read keeps its cause (``[constr_1221]`` or ``[constr_1225]`` for a
    prototype's own value, SWCraft's own line for a member).

    What it does **not** reach: an inline sub-element's own ``COMPU-METHOD-REF``, which
    the reader does not keep; a text that may name a ``TEXTTABLE`` scale the reader
    dropped as malformed; a ``BITFIELD_TEXTTABLE``'s scales, not read as texts. Each names
    no cause, and the mapper's line stands.
    """
    counts = _Tally()
    if not _judge_value(value, type_ref, source, counts, own=True, chain=()):
        return InitJudgement((), (), value)
    return InitJudgement(
        tuple((cause, counts[cause]) for cause in INIT_CAUSES if counts.get(cause)),
        tuple(counts.readings),
        _with_literals(value, counts.literals),
    )


def _with_literals(value: ConstantValue, literals: dict[int, str]) -> ConstantValue:
    """*value* with each leaf :func:`read_init_value` read replaced by its literal's text, in a copy.

    Every line about the value is taken from the source value, never from this copy.
    """
    if not literals:
        return value
    if isinstance(value, ConstantScalar):
        literal = literals.get(id(value))
        if literal is None:
            return value
        return replace(value, value=literal, form=ValueForm.APPLICATION, radix_source=None)
    if isinstance(value, ConstantArray):
        return replace(value, members=[_with_literals(member, literals) for member in value.members])
    return replace(
        value, fields=[replace(field, value=_with_literals(field.value, literals)) for field in value.fields]
    )


def init_value_unit_differs(value: ConstantValue, type_ref: str, source: SourceTypes) -> bool:
    """Whether a carried value in the application form states a unit other than its source type's.

    ``True`` where any application-form leaf under a non-enumeration application primitive
    states a ``UNIT`` other than the type's, a missing unit on either side included (paths
    compared as the file writes them): the
    writer writes the type's own unit, so the leaf's is not carried. Under an enumeration the writer writes no
    ``UNIT-REF`` at any level, so any unit the leaf states is not carried.

    What it does **not** reach: an application-form leaf under an implementation type
    (``[constr_1385]``'s at the top), a leaf in the numerical form, and a value the walk
    cannot pair with its source type.
    """
    counts = _Tally()
    if not _judge_value(value, type_ref, source, counts, own=True, chain=()):
        return False
    return counts.unit_differs


#: A carried value whose source type is an application primitive the import collapsed onto
#: its implementation twin, written in the numerical form its model type asks for. Descriptive.
TWIN_NUMERICAL_SENTENCE = "written in the numerical form: the type is the implementation type its twin collapsed to"
#: A carried value in the application form whose ``UNIT-REF`` is not its
#: type's -- the writer writes the type's own unit. Descriptive: ``[constr_1391]`` asks
#: the two to be compatible, a matter of the SI relation the model does not hold.
VALUE_UNIT_SENTENCE = "the unit of a value is not carried: a value's unit is its type's"


def carried_value_lines(
    value: ConstantValue, source_type_ref: str, source: SourceTypes, *, application: bool
) -> tuple[CarriedValueLine, ...]:
    """The lines a carried value takes, each with its category.

    :data:`TWIN_NUMERICAL_SENTENCE` where the twin collapsed (*application* false) and
    :data:`VALUE_UNIT_SENTENCE` where a leaf states another unit, both descriptive; then
    one line per leaf read through its type's table: a prototype's own value a defect
    citing the rule it breaks, a member's descriptive.
    """
    lines: list[CarriedValueLine] = []
    if not application and source_type_ref in source.application:
        lines.append(CarriedValueLine(TWIN_NUMERICAL_SENTENCE, SkipCategory.NOT_SUPPORTED))
    if init_value_unit_differs(value, source_type_ref, source):
        lines.append(CarriedValueLine(VALUE_UNIT_SENTENCE, SkipCategory.NOT_SUPPORTED))
    lines.extend(_reading_line(reading) for reading in read_init_value(value, source_type_ref, source).readings)
    return tuple(lines)


def _reading_line(reading: InitReading) -> CarriedValueLine:
    """The line of one number read through its type's table."""
    sentence = NUMBER_READ_AS_LITERAL_SENTENCE.format(number=reading.number, literal=reading.literal)
    if reading.own:
        return CarriedValueLine(INIT_CARRIED + sentence, SkipCategory.EXPORT_DEFECT, reading.rule)
    return CarriedValueLine(sentence, SkipCategory.NOT_SUPPORTED)


def _judge_value(
    value: ConstantValue,
    type_ref: str | None,
    source: SourceTypes,
    counts: _Tally,
    *,
    own: bool,
    chain: tuple[str, ...],
) -> bool:
    """One value against one source type, its causes counted into *counts*; ``False`` where it cannot pair."""
    if type_ref is None or type_ref in chain:
        return False
    chain = (*chain, type_ref)
    application = source.application.get(type_ref)
    if application is not None:
        compu = source.compu_methods.get(application.compu_method_ref) if application.compu_method_ref else None
        if application.compu_method_ref and compu is None:
            return False
        return _judge_leaf(value, compu, counts, own=own, implementation=False, unit_ref=application.unit_ref)
    record = source.records.get(type_ref)
    if record is not None:
        if not isinstance(value, ConstantRecord) or len(value.fields) != len(record.elements):
            return False
        return all(
            _judge_value(member.value, element.type_ref, source, counts, own=False, chain=chain)
            for element, member in zip(record.elements, value.fields, strict=True)
        )
    array = source.arrays.get(type_ref)
    if array is not None:
        if not isinstance(value, ConstantArray) or (
            array.max_number_of_elements is not None and len(value.members) != array.max_number_of_elements
        ):
            return False
        return all(
            _judge_value(member, array.element_type_ref, source, counts, own=False, chain=chain)
            for member in value.members
        )
    return _judge_implementation(value, type_ref, source, counts, own=own, chain=chain)


def _judge_implementation(
    value: ConstantValue,
    type_ref: str,
    source: SourceTypes,
    counts: _Tally,
    *,
    own: bool,
    chain: tuple[str, ...],
) -> bool:
    """An implementation type, down its ``TYPE_REFERENCE`` chain: the first ``COMPU-METHOD-REF`` on the way decides."""
    implementation = source.implementation.get(type_ref)
    compu_ref: str | None = None
    seen: list[str] = []
    while implementation is not None and implementation.category == "TYPE_REFERENCE":
        if implementation.path in seen:
            return False
        seen.append(implementation.path)
        compu_ref = compu_ref or implementation.compu_method_ref
        implementation = source.implementation.get(implementation.impl_type_ref or "")
    if implementation is None:
        return False
    compu_ref = compu_ref or implementation.compu_method_ref
    if implementation.category == "VALUE":
        compu = source.compu_methods.get(compu_ref) if compu_ref else None
        if compu_ref and compu is None:
            return False
        return _judge_leaf(value, compu, counts, own=own, implementation=True)
    if implementation.category == "STRUCTURE":
        if not isinstance(value, ConstantRecord) or len(value.fields) != len(implementation.sub_elements):
            return False
        return all(
            _judge_sub_element(member.value, sub, source, counts, chain=chain)
            for sub, member in zip(implementation.sub_elements, value.fields, strict=True)
        )
    if implementation.category == "ARRAY" and len(implementation.sub_elements) == 1:
        (sub,) = implementation.sub_elements
        if not isinstance(value, ConstantArray) or (
            sub.array_size is not None and len(value.members) != sub.array_size
        ):
            return False
        return all(_judge_sub_element(member, sub, source, counts, chain=chain) for member in value.members)
    return False


def _judge_sub_element(
    value: ConstantValue,
    sub: ImplSubElementInfo,
    source: SourceTypes,
    counts: _Tally,
    *,
    chain: tuple[str, ...],
) -> bool:
    """A member typed by an implementation sub-element: its referenced type, or an inline ``VALUE``.

    The reader keeps no inline sub-element's own ``COMPU-METHOD-REF``, so a text there
    pairs nothing; a number is judged as under a type with no table.
    """
    if sub.nested_sub_elements:
        return False
    if sub.impl_type_ref is not None:
        return _judge_value(value, sub.impl_type_ref, source, counts, own=False, chain=chain)
    if sub.category == "VALUE":
        if isinstance(value, ConstantScalar) and isinstance(value.value, str):
            return False
        return _judge_leaf(value, None, counts, own=False, implementation=True)
    return False


#: A text naming none of the scales the reader kept, under a ``TEXTTABLE`` that lost a
#: malformed scale: it may name the dropped one, so it names no cause rather than blame the
#: file for an entry SWCraft dropped.
_UNPAIRED: Literal["unpaired"] = "unpaired"
#: What a leaf's judgement answers: the cause it names, :data:`_UNPAIRED`, or none.
_LeafCause = InitCause | Literal["unpaired"] | None


def _judge_leaf(
    value: ConstantValue,
    compu: CompuMethodInfo | None,
    counts: _Tally,
    *,
    own: bool,
    implementation: bool,
    unit_ref: str | None = None,
) -> bool:
    """One leaf, its cause counted into *counts*; ``False`` where the value is a composite.

    A prototype's own value is held to the form ``[constr_1221]`` / ``[constr_1385]``
    asks; a member's number is carried in either form.
    """
    if not isinstance(value, ConstantScalar):
        return False
    category = compu.category if compu is not None else None
    text_value = value.form == ValueForm.TEXT
    application_form = value.form == ValueForm.APPLICATION
    enumeration = not implementation and category == "TEXTTABLE"
    cause: _LeafCause | _Read = None
    if implementation and compu is not None and category in _TABLE_CATEGORIES:
        cause = _table_leaf_cause(value, compu, own=own)
    elif text_value:
        if implementation and category in _MIXED_TABLE_CATEGORIES:
            cause = TEXT_VALUE_UNDER_A_MIXED_TABLE
        elif own and not implementation:
            cause = TEXT_VALUE_UNDER_AN_APPLICATION_TYPE
        else:
            cause = TEXT_VALUE_WITHOUT_A_TABLE
    elif own and implementation and application_form:
        cause = APPLICATION_VALUE_UNDER_AN_IMPLEMENTATION_TYPE
    elif enumeration and compu is not None:
        cause = _enumeration_leaf_cause(value, compu, own=own)
    elif application_form and isinstance(value.value, str):
        cause = APPLICATION_TEXT_WHERE_A_NUMBER_IS_TAKEN
    elif own and not implementation and not application_form:
        cause = NUMBER_WHERE_AN_APPLICATION_VALUE_IS_TAKEN
    if cause == _UNPAIRED:
        return False
    if isinstance(cause, _Read):
        rule = (
            StandardRule.TABLE_INIT_IS_A_TEXT_VALUE
            if implementation
            else StandardRule.APPLICATION_VALUE_FOR_AN_APPLICATION_PRIMITIVE
        )
        counts.readings.append(InitReading(_number_text(value), cause.literal, own, rule))
        counts.literals[id(value)] = cause.literal
        cause = None
    # An enumeration leaf is written with no UNIT-REF at any level.
    written_unit = None if enumeration else unit_ref
    if cause is not None:
        counts[cause] = counts.get(cause, 0) + 1
    elif application_form and not implementation and value.unit_ref != written_unit:
        counts.unit_differs = True
    return True


def _lost_a_scale(compu: CompuMethodInfo) -> bool:
    """Whether the reader dropped a malformed scale of *compu*, so the scales it kept are not the whole table."""
    return len(compu.scales) < compu.scale_count


def _number_text(value: ConstantScalar) -> str:
    """The number of a read leaf as the file spells it: its radix spelling where it had one."""
    return value.radix_source if value.radix_source is not None else str(value.value)


def _scale_reading(value: ConstantScalar, compu: CompuMethodInfo) -> _Read | None:
    """The literal a ``TEXTTABLE`` gives the number *value*, or ``None``.

    The table's internal-to-physical direction: an integral number inside exactly one
    scale's interval, that scale a single point (``UPPER-LIMIT`` absent or equal to
    ``LOWER-LIMIT``, both ``INTERVAL-TYPE``s ``CLOSED`` or absent -- the scale the writer
    writes for a literal), gives that scale's ``VT``. ``None`` -- the number is not read
    and its cause stands -- for a ``BITFIELD_TEXTTABLE`` (a number may combine several
    bits); a table any scale of which states a ``SYMBOL`` (R4.3.0's ``[TPS_SWCT_01696]``,
    SWCT ``:18503``, ranks it above ``vt``, and SWCraft holds none); one the reader
    dropped a scale of; a table the import cannot hold as one (:func:`merge.texttable_defect`:
    no scale, a blank ``VT``, two scales of one name or one value -- the table is then the
    defect, already reported, and the model holds no literal to carry, whichever scale the
    number names); one with a scale whose ``UPPER-LIMIT`` does not parse; a number that is
    not integral; and a number no scale, two scales, or one range scale contains.
    """
    number = value.value
    if isinstance(number, bool) or not isinstance(number, int | float):
        return None
    if isinstance(number, float) and not number.is_integer():
        return None
    if (
        compu.category != "TEXTTABLE"
        or _lost_a_scale(compu)
        or any(scale.states_symbol for scale in compu.scales)
        or texttable_defect(compu) is not None
    ):
        return None
    containing: list[tuple[CompuScaleInfo, int]] = []
    for scale in compu.scales:
        upper = (
            parse_int_literal(scale.upper_limit, grammar="formula") if scale.upper_limit is not None else scale.value
        )
        if upper is None:
            return None
        above = scale.lower_interval == "INFINITE" or (
            number > scale.value if scale.lower_interval == "OPEN" else number >= scale.value
        )
        below = scale.upper_interval == "INFINITE" or (
            number < upper if scale.upper_interval == "OPEN" else number <= upper
        )
        if above and below:
            containing.append((scale, upper))
    if len(containing) != 1:
        return None
    ((scale, upper),) = containing
    if (
        upper != scale.value
        or scale.lower_interval not in (None, "CLOSED")
        or scale.upper_interval not in (None, "CLOSED")
    ):
        return None
    return _Read(scale.literal)


def _table_leaf_cause(value: ConstantScalar, compu: CompuMethodInfo, *, own: bool) -> _LeafCause | _Read:
    """A leaf under an implementation type that references a table.

    A number the table reads as one literal is a reading; every other leaf that is no text
    value keeps its cause.
    """
    if value.form != ValueForm.TEXT:
        if isinstance(value.value, str):
            return NOT_A_TEXT_VALUE_UNDER_A_TABLE if own else MEMBER_NAMING_NO_ENTRY
        reading = _scale_reading(value, compu)
        if reading is not None:
            return reading
        return NOT_A_TEXT_VALUE_UNDER_A_TABLE if own else MEMBER_NUMBER_UNDER_A_TABLE
    if compu.category != "TEXTTABLE":
        # A BITFIELD_TEXTTABLE's scales are not read as texts: nothing to match.
        return None
    if any(scale.states_symbol for scale in compu.scales):
        return TEXT_UNDER_A_SYMBOL_TABLE
    if any(scale.literal == value.value for scale in compu.scales):
        # One scale: it conforms. Two or more: the table is the defect, which the
        # import of the table names; the mapper's own line stands.
        return None
    if any(scale.short_label == value.value for scale in compu.scales):
        return TEXT_NAMING_A_SHORT_LABEL
    if _lost_a_scale(compu):
        return _UNPAIRED
    return NOT_A_TEXT_VALUE_UNDER_A_TABLE if own else MEMBER_NAMING_NO_ENTRY


def _enumeration_leaf_cause(value: ConstantScalar, compu: CompuMethodInfo, *, own: bool) -> _LeafCause | _Read:
    """A leaf under an enumeration that is no text value.

    It conforms in the application form with one text naming exactly one scale by its
    ``VT``. A number is read through the table where it reads as one literal, and is
    otherwise not carried: as the prototype's own value it breaks ``[constr_1221]``'s
    second sentence (SWCT R4.2.2 ``:21113``, R4.3.0 ``:23511``) -- a read one is still
    that defect -- and as a member it is SWCraft's own line. A text naming a scale by its
    ``SHORT-LABEL`` alone, or under a table whose scales state a ``SYMBOL``, is SWCraft's
    own line; a text naming two scales names no cause (the table is the defect).
    """
    if isinstance(value.value, str) and value.form == ValueForm.APPLICATION:
        if any(scale.states_symbol for scale in compu.scales):
            return TEXT_UNDER_A_SYMBOL_TABLE
        if any(scale.literal == value.value for scale in compu.scales):
            return None
        if any(scale.short_label == value.value for scale in compu.scales):
            return TEXT_NAMING_A_SHORT_LABEL
        if _lost_a_scale(compu):
            return _UNPAIRED
        return NOT_AN_ENUMERATION_TEXT if own else ENUMERATION_MEMBER_NAMING_NO_SCALE
    if not isinstance(value.value, str):
        reading = _scale_reading(value, compu)
        if reading is not None:
            return reading
    return NOT_AN_ENUMERATION_TEXT if own else ENUMERATION_MEMBER_GIVEN_AS_A_NUMBER


#: The guarantee :func:`_map_init_value`'s own recursion reads: a value exactly where it gives no reason.
_ONE_OF_TWO = "_map_init_value gives a value or a reason, exactly one"


def _map_init_value(
    value: ConstantValue,
    type_name: str,
    data_types_by_name: DataDescription,
    where: str,
    *,
    application: bool,
) -> tuple[InitValue | None, SkipReason | None]:
    """One `ConstantValue` shaped against one declared SWCraft type.

    Returns (mapped value, reason) with exactly one of the two set. The walk follows the
    declared type, never the value: a record's members are matched **positionally** (a
    member's SHORT-LABEL is optional and need not repeat the field name), an array's
    against its declared length, and an enum's literal by its **name**. A number is a
    literal's name only where :func:`read_init_value` read it through the file's table.

    Every reason is SWCraft's own (``NOT_SUPPORTED``, no rule): this walk judges against
    the **model's** types, and the file's own rule breaks are named by
    :func:`judge_init_value`.

    What this does not reach: a value type's ``value_min``/``value_max`` and a base type's
    ``size``, ranges that ``check_init_conformance`` reports on the model.
    """
    # Every member type is asked on the side the prototype's field states.
    declared = declared_type(data_types_by_name, type_name, application=application)
    if declared is None:
        return None, not_supported(f"{where}: {undeclared_type_reason(type_name)}")

    if isinstance(declared, EnumDataType):
        return _map_enum_init(value, type_name, declared, data_types_by_name, where)

    if isinstance(declared, RecordDataType):
        return _map_record_init(value, type_name, declared, data_types_by_name, where)

    if isinstance(declared, ArrayDataType):
        return _map_array_init(value, type_name, declared, data_types_by_name, where, application=application)

    if isinstance(declared, ImplementationRecordDataType):
        return _map_implementation_record_init(value, type_name, declared, data_types_by_name, where)

    if isinstance(declared, ImplementationArrayDataType):
        return _map_implementation_array_init(
            value, type_name, declared, data_types_by_name, where, application=application
        )

    if isinstance(declared, PointerDataType):
        return None, not_supported(
            f"{where}: pointer type '{type_name}' takes no init value (a pointer's value is an address)"
        )

    if isinstance(declared, ReferenceDataType):
        # The value takes the shape of what the chain ends at; a value type or a kept path takes one scalar.
        end = reference_chain_end(declared, data_types_by_name)
        if isinstance(end, ImplementationRecordDataType | ImplementationArrayDataType | PointerDataType):
            return _map_init_value(value, end.name, data_types_by_name, where, application=False)

    if isinstance(declared, ApplicationDataType):
        # Which form the file had is judged against the file's own type, not here.
        if not isinstance(value, ConstantScalar):
            return None, not_supported(
                f"{where}: application type '{type_name}' takes a single value, not a composite one"
            )
        if isinstance(value.value, str):
            return None, not_supported(
                f"{where}: application type '{type_name}' takes a number, not the text '{value.value}'"
            )
        return value.value, None

    if isinstance(declared, ValueDataType | ReferenceDataType):
        # A type stating a table, on itself or along its chain, holds an entry's name (`[constr_1225]`).
        table = implementation_table(declared, data_types_by_name)
        if table is not None:
            if not isinstance(value, ConstantScalar):
                return None, not_supported(f"{where}: type '{type_name}' takes a single value, not a composite one")
            if isinstance(value.value, str):
                if any(entry.name == value.value for entry in table.table.entries):
                    return value.value, None
                return None, not_supported(
                    f"{where}: text '{value.value}' names no entry of the table '{table.compu_method_ref}' "
                    f"type '{type_name}' references"
                )
            # A number under a source table was already read or refused (`[constr_1225]`), so one
            # reaches here only for a collapsed twin, whose source type states no table.
            return None, not_supported(TWIN_TABLE_SENTENCE)

    if not isinstance(value, ConstantScalar):
        return None, not_supported(f"{where}: type '{type_name}' takes a single value, not a composite one")
    if isinstance(value.value, str):
        return None, not_supported(f"{where}: type '{type_name}' takes a number, not the text '{value.value}'")
    return value.value, None


def _map_enum_init(
    value: ConstantValue,
    type_name: str,
    declared: EnumDataType,
    data_types_by_name: DataDescription,
    where: str,
) -> tuple[InitValue | None, SkipReason | None]:
    """An enumeration's value: the name of one of its literals."""
    if not isinstance(value, ConstantScalar):
        return None, not_supported(f"{where}: enum type '{type_name}' takes a single value, not a composite one")
    stated = stated_literals(declared, data_types_by_name)
    if isinstance(stated, NotResolved):
        return None, not_supported(
            f"{where}: enum type '{type_name}' names a compu method this import does not hold, so the "
            "value cannot be matched with a literal"
        )
    if not isinstance(value.value, str):
        # `read_init_value` already replaced every number the file's table reads and named every
        # other one (`[constr_1221]` for a prototype's own value), so a number reaches here only
        # where the walk paired nothing (no source type, or the model's type is not the source's).
        # Judged by the model's type it cites no rule, so this line is SWCraft's.
        return None, not_supported(
            f"{where}: enum type '{type_name}' takes the name of one of its literals, and SWCraft reads the "
            f"number {value.value} as a literal only through the file's own table"
        )
    # A text that spells a number is a text, never that number's literal.
    if any(name == value.value for name, _number in stated):
        return value.value, None
    spelled = ", ".join(f"{name}={number}" for name, number in stated)
    # SWCraft's own line, never `[constr_1221]`: judged by the model's literals, it cites no rule.
    return None, not_supported(
        f"{where}: text '{value.value}' names no literal of enum type '{type_name}' "
        f"(declared literals: {spelled or 'none'})"
    )


def _map_record_init(
    value: ConstantValue,
    type_name: str,
    declared: RecordDataType,
    data_types_by_name: DataDescription,
    where: str,
) -> tuple[InitValue | None, SkipReason | None]:
    """A record's value, its members matched positionally against the declared fields."""
    if not isinstance(value, ConstantRecord):
        return None, not_supported(f"{where}: record type '{type_name}' needs a RECORD-VALUE-SPECIFICATION")
    if len(value.fields) != len(declared.fields):
        return None, not_supported(
            f"{where}: record type '{type_name}' declares {len(declared.fields)} fields "
            f"but the constant carries {len(value.fields)} members"
        )
    mapped_record: dict[str, InitValue] = {}
    for field_decl, member in zip(declared.fields, value.fields):
        mapped, reason = _map_init_value(
            member.value, field_decl.type, data_types_by_name, f"{where}.{field_decl.name}", application=True
        )
        if reason is not None:
            return None, reason
        mapped_record[field_decl.name] = required(mapped, _ONE_OF_TWO)
    return mapped_record, None


def _map_array_init(
    value: ConstantValue,
    type_name: str,
    declared: ArrayDataType,
    data_types_by_name: DataDescription,
    where: str,
    *,
    application: bool,
) -> tuple[InitValue | None, SkipReason | None]:
    """An array's value, one mapped member per element of the declared length."""
    if not isinstance(value, ConstantArray):
        return None, not_supported(f"{where}: array type '{type_name}' needs an ARRAY-VALUE-SPECIFICATION")
    if len(value.members) != declared.length:
        return None, not_supported(
            f"{where}: array type '{type_name}' declares length {declared.length} "
            f"but the constant carries {len(value.members)} members"
        )
    mapped_array: list[InitValue] = []
    for index, array_member in enumerate(value.members):
        mapped, reason = _map_init_value(
            array_member, declared.element_type, data_types_by_name, f"{where}[{index}]", application=application
        )
        if reason is not None:
            return None, reason
        mapped_array.append(required(mapped, _ONE_OF_TWO))
    return mapped_array, None


def _map_implementation_record_init(
    value: ConstantValue,
    type_name: str,
    declared: ImplementationRecordDataType,
    data_types_by_name: DataDescription,
    where: str,
) -> tuple[InitValue | None, SkipReason | None]:
    """An implementation record's value, matched positionally against its declared elements."""
    if not isinstance(value, ConstantRecord):
        return None, not_supported(
            f"{where}: implementation record type '{type_name}' needs a RECORD-VALUE-SPECIFICATION"
        )
    if len(value.fields) != len(declared.elements):
        return None, not_supported(
            f"{where}: implementation record type '{type_name}' declares {len(declared.elements)} elements "
            f"but the constant carries {len(value.fields)} members"
        )
    mapped_elements: dict[str, InitValue] = {}
    for implementation_element, member in zip(declared.elements, value.fields, strict=True):
        mapped, reason = _map_init_value(
            member.value,
            implementation_element.type,
            data_types_by_name,
            f"{where}.{implementation_element.name}",
            application=False,
        )
        if reason is not None:
            return None, reason
        mapped_elements[implementation_element.name] = required(mapped, _ONE_OF_TWO)
    return mapped_elements, None


def _map_implementation_array_init(
    value: ConstantValue,
    type_name: str,
    declared: ImplementationArrayDataType,
    data_types_by_name: DataDescription,
    where: str,
    *,
    application: bool,
) -> tuple[InitValue | None, SkipReason | None]:
    """An implementation array's value, one mapped member per element of the declared length."""
    if not isinstance(value, ConstantArray):
        return None, not_supported(
            f"{where}: implementation array type '{type_name}' needs an ARRAY-VALUE-SPECIFICATION"
        )
    if len(value.members) != declared.length:
        return None, not_supported(
            f"{where}: implementation array type '{type_name}' declares length {declared.length} "
            f"but the constant carries {len(value.members)} members"
        )
    mapped_members: list[InitValue] = []
    for index, array_member in enumerate(value.members):
        mapped, reason = _map_init_value(
            array_member, declared.element_type, data_types_by_name, f"{where}[{index}]", application=application
        )
        if reason is not None:
            return None, reason
        mapped_members.append(required(mapped, _ONE_OF_TWO))
    return mapped_members, None


def port_init_overrides(init_ref: str | None, port_inits: list[PortComSpecInitInfo]) -> list[PortComSpecInitInfo]:
    """The port COM-SPEC inits of one element that are not the element's own reference.

    A com-spec whose ``CONSTANT-REF`` names the element's own constant repeats the
    element's value and is no override.
    """
    return [com_spec for com_spec in port_inits if com_spec.constant_ref is None or com_spec.constant_ref != init_ref]


def _init_constant_value(
    constant_ref: str, constants_by_path: dict[str, ConstantSpecInfo], lead: str
) -> tuple[ConstantValue | None, SkipReason | None]:
    """The value of the constant an init references, or the reason it has none; exactly one is ``None``.

    *lead* opens the reason (``it references`` for a port, ``INIT-VALUE references`` for a variable).
    """
    constant = constants_by_path.get(constant_ref)
    if constant is None:
        return None, not_supported(f"{lead} '{constant_ref}', which is not a CONSTANT-SPECIFICATION in this file")
    if constant.value is None:
        return None, not_supported(
            f"{lead} '{constant_ref}': {constant.unreadable_reason or 'the constant carries no value'}"
        )
    return constant.value, None


def _judged_init_value(
    value: ConstantValue,
    type_name: str,
    data_types_by_name: DataDescription,
    *,
    application: bool,
    source_type_ref: str,
    source_types: SourceTypes,
) -> tuple[InitValue | None, SkipReason | None]:
    """*value* judged and shaped; exactly one of the two is ``None``.

    Judged against the file's type of the prototype (*source_type_ref*) before the model's
    type shapes it.
    """
    judgement = read_init_value(value, source_type_ref, source_types)
    if judgement.causes:
        return None, init_line_reason(judgement.causes)
    return _map_init_value(judgement.value, type_name, data_types_by_name, "init", application=application)


def port_init_outcome(
    com_spec: PortComSpecInitInfo,
    element_init: object | None,
    type_name: str,
    constants_by_path: dict[str, ConstantSpecInfo],
    data_types_by_name: DataDescription,
    *,
    application: bool,
    source_type_ref: str,
    source_types: SourceTypes,
) -> PortInitOutcome:
    """One port's own init value for one data element, decided.

    The value is written in place or the referenced constant's, shaped against the
    element's type. One equal to *element_init* is carried by equivalence
    (:class:`PortInitOutcome` with neither answer); one that differs is carried. A reason
    is the reader's or the mapper's sentence, quoted, never re-authored.
    """
    in_place = com_spec.value is not None
    value: ConstantValue | None
    if com_spec.value is not None:
        value = com_spec.value
    elif com_spec.constant_ref is None:
        return PortInitOutcome(reason=not_supported(f"this reader could not follow it ({com_spec.unreadable_reason})"))
    else:
        value, reason = _init_constant_value(com_spec.constant_ref, constants_by_path, "it references")
        if value is None:
            return PortInitOutcome(reason=reason)
    mapped, reason = _judged_init_value(
        value,
        type_name,
        data_types_by_name,
        application=application,
        source_type_ref=source_type_ref,
        source_types=source_types,
    )
    if reason is not None:
        return PortInitOutcome(reason=reason, in_place=in_place)
    lost = value_label_losses(value, type_name, data_types_by_name, application=application)
    # `-0.0 == 0.0` is carried by equivalence: the writer prints `-0.0` as `0.0` anyway.
    value_lines = carried_value_lines(value, source_type_ref, source_types, application=application)
    if mapped == element_init:
        return PortInitOutcome(lost_labels=lost, in_place=in_place, value_lines=value_lines)
    return PortInitOutcome(
        value=mapped,
        lost_labels=lost,
        in_place=in_place,
        radix_spellings=tuple(radix_spellings(value, mapped)),
        value_lines=value_lines,
    )


def init_value_for_element(
    init_ref: str | None,
    type_name: str,
    constants_by_path: dict[str, ConstantSpecInfo],
    data_types_by_name: DataDescription,
    *,
    application: bool,
    source_type_ref: str,
    source_types: SourceTypes,
) -> tuple[object | None, SkipReason | None]:
    """One data element's init value, or the reason it has none.

    Decided by the element alone: a com-spec's ``initValue`` supersedes the data
    prototype's on that port only (``[TPS_SWCT_01182]``), so a port's own value is carried
    on its ``com_specs`` entry. An INIT-VALUE the reader could not follow arrives as
    ``init_ref is None``; the reader's own line names it.
    """
    if init_ref is None:
        return None, None

    constant = constants_by_path.get(init_ref)
    if constant is None:
        return None, not_supported(
            f"INIT-VALUE references '{init_ref}', which is not a CONSTANT-SPECIFICATION in this file"
        )
    if constant.unreadable_reason is not None:
        return None, not_supported(f"INIT-VALUE references '{init_ref}': {constant.unreadable_reason}")
    if constant.value is None:
        # Unreachable today; a branch, not an `assert`, which `python -O` strips.
        return None, not_supported(f"INIT-VALUE references '{init_ref}', which carries neither a value nor a reason")
    return _judged_init_value(
        constant.value,
        type_name,
        data_types_by_name,
        application=application,
        source_type_ref=source_type_ref,
        source_types=source_types,
    )


def radix_spellings(value: ConstantValue, mapped: object) -> list[str]:
    """Every hexadecimal, octal or binary spelling this import re-spelled in decimal, in document order.

    `mapped` is the same leaf's written form, walked in parallel: only a written number
    makes "was written decimal" true, so a leaf written as a literal's name is left out.

    What this does not reach: a tree deeper than CPython's recursion limit.
    """
    if isinstance(value, ConstantScalar):
        if value.radix_source is None:
            return []
        # `bool` is an `int` in Python; admitting it would widen a number into a truth value.
        if not isinstance(mapped, (int, float)) or isinstance(mapped, bool):
            return []
        return [value.radix_source]
    if isinstance(value, ConstantArray):
        if not isinstance(mapped, list) or len(mapped) != len(value.members):
            return []
        return [
            spelling
            for member, member_mapped in zip(value.members, mapped)
            for spelling in radix_spellings(member, member_mapped)
        ]
    if isinstance(value, ConstantRecord):
        # The mapped dict is built positionally, so `values()` pairs up with `fields`.
        if not isinstance(mapped, dict) or len(mapped) != len(value.fields):
            return []
        return [
            spelling
            for member, member_mapped in zip(value.fields, mapped.values())
            for spelling in radix_spellings(member.value, member_mapped)
        ]
    # Unreachable; a new `ConstantValue` member is then a missing line, not an `AttributeError`.
    return []


#: A data element's ``SW-IMPL-POLICY`` literal -> the model's
#: ``sw_impl_policy``. ``MEASUREMENT-POINT`` is not carried, and
#: ``FIXED`` and ``CONST`` have no place on a variable data prototype
#: (``[constr_2035]``); none of the three is in the table.
_SW_IMPL_POLICY_VALUES: dict[str, Literal["standard", "queued"]] = {"STANDARD": "standard", "QUEUED": "queued"}
#: A data element's ``SW-CALIBRATION-ACCESS`` literal -> the
#: model's ``sw_calibration_access`` (``SwCalibrationAccessEnum``).
_SW_CALIBRATION_ACCESS_VALUES: dict[str, Literal["not_accessible", "read_only", "read_write"]] = {
    "NOT-ACCESSIBLE": "not_accessible",
    "READ-ONLY": "read_only",
    "READ-WRITE": "read_write",
}
#: The calibration accesses ``[constr_1017]`` supports on a queued element: only
#: ``notAccessible`` (Tables 5.49 / 5.50), the element-local rule
#: ``rule:interfaces.data_element.calibration-access-supported``.
_QUEUED_CALIBRATION_ACCESSES = frozenset({"not_accessible"})


def data_element_properties(
    policy_text: str | None, access_text: str | None
) -> tuple[Literal["standard", "queued"] | None, Literal["not_accessible", "read_only", "read_write"] | None]:
    """A data element's ``(sw_impl_policy, sw_calibration_access)`` as the source states them.

    An explicit ``STANDARD`` or ``NOT-ACCESSIBLE`` is recorded; an absent literal, or one
    the model does not carry, is ``None``. A calibration access ``[constr_1017]`` does not
    support beside a ``queued`` policy is not carried, since a model holding both would not
    load.
    """
    policy = _SW_IMPL_POLICY_VALUES.get(policy_text or "")
    access = _SW_CALIBRATION_ACCESS_VALUES.get(access_text or "")
    if policy == "queued" and access is not None and access not in _QUEUED_CALIBRATION_ACCESSES:
        access = None
    return policy, access


def map_variable_types(
    model: ImportModel,
    registry: PathRegistry,
    data_types: list[DataType],
    *,
    elements: Sequence[DataDescriptionElement],
) -> dict[str, VariableTypeOutcome]:
    """Decide every inter-runnable variable's type and initial value, keyed by the variable's path.

    Call once, after :func:`map_interfaces` (whose registry it reads) and before
    ``map_swcs``, which spends the outcomes.

    The **type** resolves as a data element's does, and the layer it resolves into decides
    the field the variable states. A path the file declares and the import did not keep is
    a skip; a path the file declares nowhere is a skip that may be kept as an external type
    stub, unless its ``DEST`` is an application kind, for which no stub exists. A
    ``SW-BASE-TYPE`` named directly is the file's defect.

    The **initial value** is judged and shaped as a data element's is; a refused value is
    an ``init_reason`` and the variable is kept without it.

    What this does not reach: a variable of a component the component mapper later skips
    still has an outcome here; it is spent by no one.
    """
    data_types_by_name = DataDescription.last_declared([*data_types, *elements])
    constants_by_path: dict[str, ConstantSpecInfo] = {c.path: c for c in model.constant_specs}
    source_types = SourceTypes(model)
    declared = declared_paths(model)
    outcomes: dict[str, VariableTypeOutcome] = {}
    for swc in model.swcs:
        for variable in swc.variables:
            outcomes[variable.path] = _variable_type_outcome(
                variable,
                registry,
                data_types_by_name,
                declared,
                constants_by_path,
                source_types,
            )
    return outcomes


def resolve_prototype_type(
    type_ref: str, registry: PathRegistry, data_types_by_name: DataDescription, declared: Collection[str]
) -> tuple[str | None, SkipReason | None]:
    """One data prototype's ``TYPE-TREF`` path, resolved to its emitted SWCraft name, or the reason it is not.

    The one rule for a data element, an argument and an inter-runnable variable. Exactly
    one of the two is ``None``.
    """
    if not type_ref:
        # No path to ask whether another file declares it.
        return None, not_supported("has no TYPE-TREF")
    resolved = registry.resolve(type_ref)
    if resolved is None:
        if type_ref not in declared:
            # Reached by `import component` alone: `import model` keeps such a reference.
            return None, SkipReason(
                f"unresolved reference {type_ref} (the file declares no data type at that path)",
                SkipCategory.OUTSIDE_FILE,
            )
        return None, not_supported(f"unresolved reference {type_ref}")
    # The name alone may be held by two layers; the path's layer decides the field the prototype states.
    layer = registry.layer_of(type_ref)
    if layer == LAYER_BASE_TYPES:
        # `AutosarDataPrototype.type` is an `AutosarDataType` (SWCT R4.2.2 :13888, R4.3.0
        # :14836), which a SwBaseType is not, so the file breaks `[TPS_GST_00020]`'s
        # destination type.
        return None, SkipReason(
            f"references the SW-BASE-TYPE {type_ref} directly -- a data prototype's type is an AutosarDataType "
            "(an application or implementation data type), which a SW-BASE-TYPE is not",
            SkipCategory.EXPORT_DEFECT,
            StandardRule.REFERENCE_DESTINATION_TYPE,
        )
    if _kind_in(data_types_by_name, layer, resolved) is None:
        # Should not happen (registry and data_types are built together); never trusted blindly.
        return None, not_supported(f"unresolved reference {type_ref} (mapped type '{resolved}' missing)")
    return resolved, None


def _variable_type_outcome(
    variable: SwcVariableInfo,
    registry: PathRegistry,
    data_types_by_name: DataDescription,
    declared: Collection[str],
    constants_by_path: dict[str, ConstantSpecInfo],
    source_types: SourceTypes,
) -> VariableTypeOutcome:
    """One variable's :class:`VariableTypeOutcome` (:func:`map_variable_types` states the rules)."""
    # Carried as stated: a variable holds no policy ([constr_2038] / [constr_2039] admit
    # STANDARD alone), so [constr_1017]'s queued-policy combinations never bind it.
    access = _SW_CALIBRATION_ACCESS_VALUES.get(variable.sw_calibration_access or "")
    emitted, reason = resolve_prototype_type(variable.type_ref, registry, data_types_by_name, declared)
    if emitted is None:
        reason = required(reason, "resolve_prototype_type gives a name or a reason")
        application_dest = (variable.type_dest or "").startswith("APPLICATION-")
        if reason.category == SkipCategory.OUTSIDE_FILE and application_dest:
            reason = SkipReason(
                f"{reason.text}; it is an application type, and no external stub exists for one",
                reason.category,
                reason.rule,
            )
        outside = variable.type_ref not in declared and bool(variable.type_ref) and not application_dest
        return VariableTypeOutcome(
            skip=reason,
            keep_target=variable.type_ref if outside else None,
            sw_calibration_access=access,
        )
    application = registry.layer_of(variable.type_ref) == LAYER_APPLICATION_TYPES
    outcome = VariableTypeOutcome(type_name=emitted, application=application, sw_calibration_access=access)
    if variable.init_unreadable is not None:
        return replace(
            outcome, init_reason=not_supported(f"this reader could not follow it ({variable.init_unreadable})")
        )
    value: ConstantValue | None = variable.init_value
    constant_ref = variable.init_constant_ref
    if value is None and constant_ref is not None:
        value, constant_reason = _init_constant_value(constant_ref, constants_by_path, "INIT-VALUE references")
        if value is None:
            return replace(outcome, init_reason=constant_reason)
    if value is None:
        return outcome
    mapped, map_reason = _judged_init_value(
        value,
        emitted,
        data_types_by_name,
        application=application,
        source_type_ref=variable.type_ref,
        source_types=source_types,
    )
    if map_reason is not None:
        return replace(outcome, init_reason=map_reason)
    # The value is written in place, so its lines are keyed at the variable's own INIT-VALUE,
    # never at the constant: one label line per value.
    lost = value_label_losses(value, emitted, data_types_by_name, application=application)
    lines: list[tuple[str, str, CarriedValueLine]] = []
    if lost:
        lines.append(
            (
                f"{variable.path}/INIT-VALUE",
                "SHORT-LABEL",
                CarriedValueLine(
                    f"{lost} SHORT-LABEL(s) of the inter-runnable variable's init value not carried: SWCraft labels "
                    "an init value's record members with their field names and nothing else",
                    SkipCategory.NOT_SUPPORTED,
                ),
            )
        )
    lines.extend(
        (variable.path, "INIT-VALUE", line)
        for line in carried_value_lines(value, variable.type_ref, source_types, application=application)
    )
    return replace(
        outcome,
        init=mapped,
        lines=tuple(lines),
        radix_spellings=tuple(radix_spellings(value, mapped)),
    )


#: What an own-side infinite ``PHYS-CONSTRS`` limit becomes.
INFINITE_LIMIT_REPRESENTED = "infinite physical limit represented as an absent bound"


def _infinite_sides(constr: DataConstrInfo) -> list[str]:
    """The ``PHYS-CONSTRS`` sides *constr*'s first rule declares infinite on their own side.

    The reader's judgement: the side's own infinite spelling, or ``INTERVAL-TYPE="INFINITE"``
    whatever number it holds; the other side's infinity and ``NaN`` are unreadable limits.
    """
    return [limit.side for limit in phys_limits(constr) if limit.bounds_nothing]


def infinite_limit_lines(constr: DataConstrInfo, *, inline: bool = True) -> list[str]:
    """One report line per own-side infinite ``PHYS-CONSTRS`` limit of *constr*.

    The value is carried as an absent bound, so the line is neither a skip entry nor a
    physics note. With *inline* the line names the constraint (an application type's
    range); otherwise it belongs to the ``data_constr`` element.

    What this does not reach: an own-side infinity in ``INTERNAL-CONSTRS`` is read as absent
    with no line, and an ``OPEN`` physical limit is carried as its number.
    """
    subject = f"DATA-CONSTR {constr.path} " if inline else ""
    return [f"{subject}PHYS-CONSTRS {side}: {INFINITE_LIMIT_REPRESENTED}" for side in _infinite_sides(constr)]
