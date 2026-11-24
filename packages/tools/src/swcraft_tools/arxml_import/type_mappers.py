"""Mapping the data types and the interfaces of an export onto the model.

``map_data_types()`` maps every data-type-ish element the reader extracted (SW-BASE-TYPE,
IMPLEMENTATION-DATA-TYPE, APPLICATION-PRIMITIVE/RECORD/ARRAY-DATA-TYPE, COMPU-METHOD, UNIT,
DATA-CONSTR) onto the data description layers, resolved through the path-aware ``PathRegistry``.
``map_interfaces()`` maps every SENDER-RECEIVER- and CLIENT-SERVER-INTERFACE onto the
``interfaces/`` layer, reusing that same registry so a data element's or an argument's TYPE-TREF
takes exactly the name ``map_data_types()`` assigned the type. The per-element policies both read
live in ``type_mapping``, the implementation-type resolution in ``impl_type_resolution``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from typing import cast

from swcraft_tools.arxml_import.impl_type_resolution import (
    ImplResolution,
    cause_path,
    resolve_impl_type,
)
from swcraft_tools.arxml_import.merge import (
    CONFLICT,
    LAYER_APPLICATION_TYPES,
    LAYER_BASE_TYPES,
    LAYER_COMPU_METHODS,
    LAYER_DATA_CONSTRAINTS,
    LAYER_IMPLEMENTATION_TYPES,
    LAYER_UNITS,
    MAP,
    MergeDecisions,
    collapses_onto_twin,
    composite_member_targets,
    declared_paths,
    inline_forms,
    linear_scale_limits,
    record_chained_to,
    texttable_defect,
    texttable_limit_losses,
)
from swcraft_tools.arxml_import.registry import (
    PathRegistry,
    PortInitKey,
    PortInitOutcome,
)
from swcraft_tools.arxml_import.type_mapping import (
    CS_DIRECTION_MAP,
    UNNAMED_MEMBER,
    FormReference,
    Forms,
    HeldUnreadInit,
    SourceTypes,
    argument_policy_meaning,
    carried_value_lines,
    data_element_properties,
    drop_interface,
    form_reference,
    infinite_limit_lines,
    init_value_for_element,
    map_forms,
    port_com_spec_inits,
    port_init_outcome,
    port_init_overrides,
    radix_spellings,
    read_physics,
    report_enum_physics,
    report_form_not_carried,
    report_linear_limits,
    report_scale_limit,
    report_server_argument_policy,
    report_unread_port_com_spec_inits,
    report_value_label,
    report_value_line,
    resolve_prototype_type,
    server_argument_policy_carried,
    stub_typed_init_reason,
    uncarried_category_note,
    undeclared_type_reason,
    unreadable_is_service_reason,
    value_label_losses,
)
from swcraft_tools.arxml_import.unresolved import (
    UnresolvedReferences,
    UnresolvedReferrer,
)
from swcraft_tools.arxml_reader import (
    REPORT,
    AppArrayDataTypeInfo,
    AppPrimitiveDataTypeInfo,
    AppRecordDataTypeInfo,
    CompuMethodInfo,
    ConstantSpecInfo,
    CSInterfaceInfo,
    CSOperationInfo,
    DataConstrInfo,
    ImplDataTypeInfo,
    ImportModel,
    PortComSpecInitInfo,
    SkipCategory,
    SkipLog,
    SkipReason,
    SRDataElementInfo,
    SRInterfaceInfo,
    StandardRule,
    SwBaseTypeInfo,
    UnitInfo,
    blank_short_name,
    not_supported,
    parse_xsd_boolean,
)
from swcraft_tools.data_description import (
    DataDescription,
)
from swcraft_tools.schema import (
    ApplicationDataType,
    ArrayDataType,
    BaseDataType,
    CSArgument,
    CSInterface,
    CSOperation,
    DataDescriptionElement,
    DataElement,
    DataType,
    EnumDataType,
    EnumLiteral,
    InitValue,
    Interface,
    RecordDataType,
    RecordField,
    SRInterface,
)


@dataclass(frozen=True)
class _TypeContext:
    """What :func:`map_data_types` reads and never changes: its arguments and the indices over the export."""

    model: ImportModel
    merges: MergeDecisions | None
    unresolved: UnresolvedReferences | None
    kept_pairs: frozenset[str]
    declared: frozenset[str]
    sw_base_by_path: dict[str, SwBaseTypeInfo]
    impl_by_path: dict[str, ImplDataTypeInfo]
    compu_by_path: dict[str, CompuMethodInfo]
    unit_by_path: dict[str, UnitInfo]
    constr_by_path: dict[str, DataConstrInfo]
    app_by_path: dict[str, AppPrimitiveDataTypeInfo]
    #: Rule 1's twin-collapse candidates: application path -> implementation path.
    twin_impl_for_app_path: dict[str, str]
    members_of_composites: frozenset[str]
    composite_app_paths: frozenset[str]
    inline: frozenset[str]
    record_by_path: dict[str, AppRecordDataTypeInfo]
    array_by_path: dict[str, AppArrayDataTypeInfo]


@dataclass
class _TypeAccumulator:
    """What :func:`map_data_types` fills across the export's elements, for the whole call."""

    registry: PathRegistry = field(default_factory=PathRegistry)
    skip_log: SkipLog = field(default_factory=SkipLog)
    data_types: list[DataType] = field(default_factory=list)
    consumed_compu_paths: set[str] = field(default_factory=set)
    consumed_property_paths: set[str] = field(default_factory=set)
    #: Every consumed `COMPU-METHOD`, `UNIT` and `DATA-CONSTR` path, with the
    #: `description` of each type that consumed it.
    consumer_descriptions: dict[str, set[str]] = field(default_factory=dict)
    #: Composite paths already mapped, refused or being mapped now. A path is
    #: added before its members are resolved, which also guards against a
    #: composite that contains itself.
    decided_composites: set[str] = field(default_factory=set)


def map_data_types(
    model: ImportModel,
    merges: MergeDecisions | None = None,
    unresolved: UnresolvedReferences | None = None,
    *,
    elements: list[DataDescriptionElement],
    kept_pairs: frozenset[str] = frozenset(),
) -> tuple[list[DataType], PathRegistry, SkipLog]:
    """Map every data-type-ish element of *model* onto the data description layers.

    Each source type becomes the matching kind (a TEXTTABLE primitive an ``enum``, a fixed-size array only); any
    other shape is skipped with a sentence. Every ``UNIT``, ``COMPU-METHOD`` and ``DATA-CONSTR`` an element holds
    is created first, appended to *elements* and named by the type's reference field; one nothing carries is a
    skip line. SWCraft's own private form and every form no element holds are read inline.

    Names: an application type twinned by a DATA-TYPE-MAPPING-SET with a same-name implementation type collapses
    into it when its application side is bare and the pair is not kept (:func:`_keeps_both`); any other name
    colliding within one layer gets a deterministic ``_app`` (then ``_app_{package}``, then ``_{k}_app``) suffix.
    Names are reserved per layer.

    Args:
        model: The export, as the reader extracted it.
        merges: The verdict on same-name declarations across packages: a merged group's non-primary member is not
            mapped and its path aliases the primary's; neither member of a conflicting group is mapped. ``None``
            (``import component``) leaves the suffix as the only answer.
        unresolved: The unresolved-reference policy, or ``None`` to keep no reference.
        elements: The data description elements, filled here.
        kept_pairs: The names the target project declares both as an ``application_type`` and as an implementation
            type, whose pair is kept (``import component`` only).

    Returns:
        ``(data_types, registry, skip_log)`` -- ``registry`` carries the reduction/rename report lines,
        ``skip_log`` the skip reasons.

    Raises:
        NameCollisionError: one path declared twice; see ``PathRegistry.register``/``register_twin``.
        pydantic.ValidationError: a mapped element fails ``schema.py`` validation.
    """
    acc = _TypeAccumulator()
    ctx = _type_context(model, merges, unresolved, kept_pairs, acc)
    forms = map_forms(model, acc.registry, partial(_verdict, ctx, acc), unresolved, ctx.inline, acc.skip_log)
    elements.extend(forms.elements)
    _map_base_types(ctx, acc)
    resolution = _resolve_implementation_types(ctx, acc)
    for app in model.app_primitive_data_types:
        _map_application_primitive(ctx, acc, app)
    _map_composites(ctx, acc)
    _report_consumed_descriptions(ctx, acc)
    _report_implementation_compu(ctx, acc, resolution)
    _report_unconsumed_forms(ctx, acc, forms)
    return acc.data_types, acc.registry, acc.skip_log


def _type_context(
    model: ImportModel,
    merges: MergeDecisions | None,
    unresolved: UnresolvedReferences | None,
    kept_pairs: frozenset[str],
    acc: _TypeAccumulator,
) -> _TypeContext:
    """Index the export once, seed the registry's merge aliases and collect rule 1's twin-collapse candidates."""
    registry = acc.registry
    declared = declared_paths(model)
    # Seeded before the first `register()`, so a reference met while mapping
    # resolves through the alias whether or not the primary has been reached.
    if merges is not None:
        registry.merge_aliases.update(merges.alias_paths())

    sw_base_by_path = {b.path: b for b in model.sw_base_types}
    impl_by_path = {i.path: i for i in model.impl_data_types}
    compu_by_path = {c.path: c for c in model.compu_methods}
    unit_by_path = {u.path: u for u in model.units}
    constr_by_path = {c.path: c for c in model.data_constrs}
    app_by_path = {a.path: a for a in model.app_primitive_data_types}

    twin_impl_for_app_path: dict[str, str] = {}
    for mset in model.data_type_mapping_sets:
        for m in mset.maps:
            if m.application_ref.rsplit("/", 1)[-1] == m.implementation_ref.rsplit("/", 1)[-1]:
                # The last same-name entry wins, as in `merge._mapping_candidates`;
                # the two must agree or a `--packages` narrowing finds nothing registered.
                twin_impl_for_app_path[m.application_ref] = m.implementation_ref
    members_of_composites = composite_member_targets(model)
    composite_app_paths = frozenset(
        {record.path for record in model.app_record_data_types} | {array.path for array in model.app_array_data_types}
    )

    inline = inline_forms(model)
    record_by_path = {rec.path: rec for rec in model.app_record_data_types}
    array_by_path = {arr.path: arr for arr in model.app_array_data_types}
    return _TypeContext(
        model=model,
        merges=merges,
        unresolved=unresolved,
        kept_pairs=kept_pairs,
        declared=declared,
        sw_base_by_path=sw_base_by_path,
        impl_by_path=impl_by_path,
        compu_by_path=compu_by_path,
        unit_by_path=unit_by_path,
        constr_by_path=constr_by_path,
        app_by_path=app_by_path,
        twin_impl_for_app_path=twin_impl_for_app_path,
        members_of_composites=members_of_composites,
        composite_app_paths=composite_app_paths,
        inline=inline,
        record_by_path=record_by_path,
        array_by_path=array_by_path,
    )


def _verdict(ctx: _TypeContext, acc: _TypeAccumulator, path: str, name: str, layer: str) -> str:
    """The merge-verdict outcome for one declaration, claiming a contested name.

    `MAP` for a declaration in no group and for a merged group's primary,
    `ALIAS` for every other merged member, `CONFLICT` for every member of a
    conflicting group, whose name is claimed in *layer* at the first member met.
    """
    merges = ctx.merges
    if merges is None:
        return MAP
    outcome = merges.outcome(path)
    if outcome == CONFLICT:
        group = merges.group_of(path)
        acc.registry.claim(name, layer, path, group=group.key if group else None)
    return outcome


def _keeps_both(ctx: _TypeContext, app_path: str) -> bool:
    """Whether a same-name pair keeps BOTH members.

    The negation of `merge.collapses_onto_twin`: the application member says
    more than its twin, a composite member names it, or the target project keeps
    the pair. An application path the export does not declare keeps nothing. An
    application record or array always keeps both: its twin is of another kind,
    so there is nothing to collapse.

    On ``import component`` a bare pair the target project declares both members of
    is kept, because the composite that makes the project keep it may lie outside
    the component's closure. ``merge._mapping_chain`` runs on the bulk path only,
    where ``kept_pairs`` is empty, so the two readers of the relation agree there
    and only there.
    """
    if app_path in ctx.composite_app_paths:
        return True
    app = ctx.app_by_path.get(app_path)
    return app is not None and not collapses_onto_twin(
        app, ctx.compu_by_path, ctx.members_of_composites, ctx.kept_pairs
    )


def _implementation_compu(ctx: _TypeContext, acc: _TypeAccumulator, impl: ImplDataTypeInfo) -> str | None:
    """A value or reference type's ``compu_method_ref``: a ``texttable`` element's name, or a kept unresolved path."""
    if impl.compu_method_ref is None or impl.category not in ("VALUE", "TYPE_REFERENCE"):
        return None
    method = ctx.compu_by_path.get(impl.compu_method_ref)
    if method is not None and method.category != "TEXTTABLE":
        # `[constr_1158]` (SWCT R4.2.2 :12454, R4.3.0 :13199): an implementation
        # type's compu method is restricted to a TEXTTABLE or BITFIELD_TEXTTABLE
        # one; a method of another category is not carried, its type's line says so.
        return None
    reference = form_reference(
        impl.compu_method_ref,
        LAYER_COMPU_METHODS,
        acc.registry,
        ctx.unresolved,
        ctx.inline,
        UnresolvedReferrer(
            path=impl.path,
            tag="IMPLEMENTATION-DATA-TYPE",
            attribute="COMPU-METHOD-REF",
            field=(
                "implementation_types.value_type.compu_method_ref"
                if impl.category == "VALUE"
                else "implementation_types.reference_type.compu_method_ref"
            ),
        ),
    )
    return reference.named


def _map_base_types(ctx: _TypeContext, acc: _TypeAccumulator) -> None:
    """Map every SW-BASE-TYPE to a ``base_type``, or one skip line naming the size or encoding it lacks."""
    skip_log = acc.skip_log
    for base in ctx.model.sw_base_types:
        if not base.name or not base.name.strip():
            skip_log.record_reason(base.path, "SW-BASE-TYPE", blank_short_name(), effect="abort")
            continue
        if _verdict(ctx, acc, base.path, base.name, LAYER_BASE_TYPES) != MAP:
            continue
        if base.size is None or not (base.encoding or "").strip():
            # `.strip()`: `BaseDataType.encoding` rejects a blank encoding, and a
            # malformed element is skipped and reported, never a crash.
            skip_log.record(
                base.path,
                "SW-BASE-TYPE",
                "missing BASE-TYPE-SIZE, or missing/blank BASE-TYPE-ENCODING",
                category=SkipCategory.NOT_SUPPORTED,
                effect="abort",
            )
            continue
        if base.size <= 0:
            # `BaseDataType.size` must be > 0: skip and report, never a crash.
            skip_log.record(
                base.path,
                "SW-BASE-TYPE",
                f"BASE-TYPE-SIZE must be > 0, got {base.size}",
                category=SkipCategory.NOT_SUPPORTED,
                effect="abort",
            )
            continue
        name = acc.registry.register(base.path, base.name, LAYER_BASE_TYPES)
        acc.data_types.append(
            BaseDataType(
                name=name,
                kind="base_type",
                size=base.size,
                encoding=base.encoding,
                native_declaration=base.native_declaration,
                description=base.description,
            )
        )


def _resolve_implementation_types(ctx: _TypeContext, acc: _TypeAccumulator) -> ImplResolution:
    """Resolve every IMPLEMENTATION-DATA-TYPE in dependency order (:func:`resolve_impl_type`).

    A ``DATA-CONSTR`` a value type reads as its range is consumed exactly as one
    an application type reads.
    """
    model = ctx.model
    resolution = ImplResolution(
        ctx.impl_by_path,
        ctx.sw_base_by_path,
        acc.registry,
        acc.skip_log,
        acc.data_types,
        ctx.merges,
        ctx.unresolved,
        declared=ctx.declared,
        upstream=(model.skip_log,),
        constr_paths=frozenset(ctx.constr_by_path),
        compu_reference=partial(_implementation_compu, ctx, acc),
    )
    for impl in model.impl_data_types:
        resolve_impl_type(impl.path, resolution, set())

    for path, descriptions in resolution.consumed_constraints.items():
        acc.consumed_property_paths.add(path)
        acc.consumer_descriptions.setdefault(path, set()).update(descriptions)
    return resolution


def _physics(
    ctx: _TypeContext,
    acc: _TypeAccumulator,
    app: AppPrimitiveDataTypeInfo,
    ref: str | None,
    layer: str,
    attribute: str,
    field_name: str,
) -> FormReference:
    """How the application type *app* states the form *ref* in *layer* names."""
    return form_reference(
        ref,
        layer,
        acc.registry,
        ctx.unresolved,
        ctx.inline,
        UnresolvedReferrer(
            path=app.path,
            tag="APPLICATION-PRIMITIVE-DATA-TYPE",
            attribute=attribute,
            field=f"application_types.application_type.{field_name}",
        ),
    )


def _map_application_primitive(ctx: _TypeContext, acc: _TypeAccumulator, app: AppPrimitiveDataTypeInfo) -> None:
    """Map one APPLICATION-PRIMITIVE-DATA-TYPE: an ``enum``, a twin collapse or an ``application_type``."""
    skip_log = acc.skip_log
    registry = acc.registry
    if not app.name or not app.name.strip():
        skip_log.record_reason(app.path, "APPLICATION-PRIMITIVE-DATA-TYPE", blank_short_name(), effect="abort")
        return
    if _verdict(ctx, acc, app.path, app.name, LAYER_APPLICATION_TYPES) != MAP:
        return

    # The enum is asked before the twin collapse, which would lose its literals.
    compu = ctx.compu_by_path.get(app.compu_method_ref) if app.compu_method_ref else None
    table_defect: str | None = None
    if compu is not None and compu.category == "TEXTTABLE":
        table_defect = texttable_defect(compu)
        if _map_enum_naming_table(ctx, acc, app):
            return
    if compu is not None and compu.category == "TEXTTABLE" and table_defect is None:
        _map_enum_with_literals(acc, app, compu)
        return

    # Rule 1: a bare application side says what its twin says, so one element
    # is written and every reference to either path resolves to it.
    twin_impl_path = ctx.twin_impl_for_app_path.get(app.path)
    if (
        twin_impl_path is not None
        and not _keeps_both(ctx, app.path)
        and twin_impl_path in ctx.impl_by_path
        and registry.resolve(twin_impl_path) is not None
    ):
        survivor = registry.register_twin(app.path, twin_impl_path, ctx.impl_by_path[twin_impl_path].name)
        skip_log.record(
            app.path,
            "APPLICATION-PRIMITIVE-DATA-TYPE",
            f"collapsed into its implementation twin, written as '{survivor}' (the same-name pair): "
            "SWCraft writes the one implementation type",
            category=SkipCategory.NOT_SUPPORTED,
            effect=REPORT,
        )
        return

    _map_application_type(ctx, acc, app, compu, table_defect)


def _map_enum_naming_table(ctx: _TypeContext, acc: _TypeAccumulator, app: AppPrimitiveDataTypeInfo) -> bool:
    """An ``enum`` naming its table by ``compu_method_ref``, with no literals of its own.

    Returns whether the enumeration was written; it is not where the table is
    neither an element nor a kept unresolved path, and the caller then reads the
    table inline.
    """
    registry = acc.registry
    table = form_reference(
        app.compu_method_ref,
        LAYER_COMPU_METHODS,
        registry,
        ctx.unresolved,
        ctx.inline,
        UnresolvedReferrer(
            path=app.path,
            tag="APPLICATION-PRIMITIVE-DATA-TYPE",
            attribute="COMPU-METHOD-REF",
            field="application_types.enum.compu_method_ref",
        ),
    )
    if table.named is not None:
        name = registry.register(app.path, app.name, LAYER_APPLICATION_TYPES)
        acc.data_types.append(
            EnumDataType(name=name, kind="enum", compu_method_ref=table.named, description=app.description)
        )
        if ctx.unresolved is not None:
            ctx.unresolved.commit(app.path)
        if (category_note := uncarried_category_note(app, "enum")) is not None:
            registry.record_unread_physics(app.path, category_note)
        report_enum_physics(app, acc.skip_log)
        return True
    return False


def _map_enum_with_literals(acc: _TypeAccumulator, app: AppPrimitiveDataTypeInfo, compu: CompuMethodInfo) -> None:
    """An ``enum`` stating one literal per scale of *compu*, the table read inline.

    A ``UNIT-REF`` or a ``DATA-CONSTR-REF`` the type states is one line each, as an
    enum holds neither (:func:`report_enum_physics`).
    """
    registry = acc.registry
    name = registry.register(app.path, app.name, LAYER_APPLICATION_TYPES)
    acc.data_types.append(
        EnumDataType(
            name=name,
            kind="enum",
            literals=[EnumLiteral(name=s.literal, value=s.value, description=s.description) for s in compu.scales],
            description=app.description,
        )
    )
    acc.consumed_compu_paths.add(compu.path)
    acc.consumer_descriptions.setdefault(compu.path, set()).add(app.description)
    for tag in texttable_limit_losses(compu):
        report_scale_limit(compu.path, tag, acc.skip_log)
    if (category_note := uncarried_category_note(app, "enum")) is not None:
        registry.record_unread_physics(app.path, category_note)
    report_enum_physics(app, acc.skip_log)


def _map_application_type(
    ctx: _TypeContext,
    acc: _TypeAccumulator,
    app: AppPrimitiveDataTypeInfo,
    compu: CompuMethodInfo | None,
    table_defect: str | None,
) -> None:
    """An ``application_type`` carrying its ``unit``, ``conversion`` and ``range``.

    Each form that is an element is named by its reference field and only the
    others are read inline. What the model cannot hold is reported, not skipped,
    because the element is imported. A TEXTTABLE method whose table could not be
    read is no ``conversion``: the type is imported without its literals.
    """
    skip_log = acc.skip_log
    registry = acc.registry
    compu_by_path = ctx.compu_by_path
    # A TEXTTABLE method is never a `conversion`; its loss is already stated,
    # so it is not read again for a second, uninformative line.
    unit_form = _physics(ctx, acc, app, app.unit_ref, LAYER_UNITS, "UNIT-REF", "unit_ref")
    constr_form = _physics(
        ctx, acc, app, app.data_constr_ref, LAYER_DATA_CONSTRAINTS, "DATA-CONSTR-REF", "data_constr_ref"
    )
    compu_form = (
        FormReference("none")
        if compu is not None and compu.category == "TEXTTABLE"
        else _physics(ctx, acc, app, app.compu_method_ref, LAYER_COMPU_METHODS, "COMPU-METHOD-REF", "compu_method_ref")
    )
    unit_text, conversion, value_range, consumed, notes = read_physics(
        replace(
            app,
            unit_ref=unit_form.inline_value,
            data_constr_ref=constr_form.inline_value,
        ),
        ctx.unit_by_path,
        ctx.constr_by_path,
        compu_by_path,
        compu_method_ref=compu_form.inline_value,
        own_forms=ctx.inline,
    )
    if table_defect is not None:
        notes.insert(0, f"{table_defect}; the type is imported without its literals")
    if compu is not None and compu.category == "TEXTTABLE":
        skip_log.record(
            f"{app.path}/SW-DATA-DEF-PROPS",
            "COMPU-METHOD-REF",
            "COMPU-METHOD-REF to a TEXTTABLE compu-method not carried: the type is imported as an "
            "application type without literals, and the method's own line says why",
            category=SkipCategory.NOT_SUPPORTED,
            effect=REPORT,
        )
    if (category_note := uncarried_category_note(app, "application_type")) is not None:
        notes.insert(0, category_note)
    name = registry.register(app.path, app.name, LAYER_APPLICATION_TYPES)
    acc.data_types.append(
        ApplicationDataType(
            name=name,
            kind="application_type",
            unit=unit_text,
            conversion=conversion,
            range=value_range,
            unit_ref=unit_form.named,
            compu_method_ref=compu_form.named,
            data_constr_ref=constr_form.named,
            description=app.description,
        )
    )
    if ctx.unresolved is not None:
        ctx.unresolved.commit(app.path)
    acc.consumed_property_paths.update(consumed)
    for path in consumed:
        acc.consumer_descriptions.setdefault(path, set()).add(app.description)
    if app.compu_method_ref and app.compu_method_ref in consumed:
        acc.consumed_compu_paths.add(app.compu_method_ref)
        report_linear_limits(
            app.compu_method_ref, linear_scale_limits(compu_by_path[app.compu_method_ref])[1], skip_log
        )
    for note in notes:
        registry.record_unread_physics(app.path, note)
    if app.data_constr_ref in consumed:
        registry.record_represented(app.path, infinite_limit_lines(ctx.constr_by_path[app.data_constr_ref]))


def _map_composites(ctx: _TypeContext, acc: _TypeAccumulator) -> None:
    """Map every APPLICATION-RECORD- and APPLICATION-ARRAY-DATA-TYPE, in dependency order.

    The loops visit the declarations in reader order; a member naming a composite
    not yet mapped maps that composite first (:func:`_member_type`).
    """
    decided_composites = acc.decided_composites
    record_by_path = ctx.record_by_path
    array_by_path = ctx.array_by_path
    for rec in ctx.model.app_record_data_types:
        if rec.path not in decided_composites or record_by_path[rec.path] is not rec:
            _map_record(ctx, acc, rec)
    for arr in ctx.model.app_array_data_types:
        if arr.path not in decided_composites or array_by_path[arr.path] is not arr:
            _map_array(ctx, acc, arr)


def _member_type(ctx: _TypeContext, acc: _TypeAccumulator, ref: str) -> str | None:
    """The emitted name *ref* resolves to, mapping an application composite first if it names one."""
    registry = acc.registry
    resolved = registry.resolve(ref)
    if resolved is not None:
        return resolved
    target = ctx.merges.primary_path(ref) if ctx.merges is not None else ref
    if target in acc.decided_composites:
        return None
    if target in ctx.record_by_path:
        _map_record(ctx, acc, ctx.record_by_path[target])
    elif target in ctx.array_by_path:
        _map_array(ctx, acc, ctx.array_by_path[target])
    return registry.resolve(ref)


def _map_record(ctx: _TypeContext, acc: _TypeAccumulator, rec: AppRecordDataTypeInfo) -> None:
    """Map one APPLICATION-RECORD-DATA-TYPE to a ``record``, or skip the whole record with its reason.

    A field typed by a SW-BASE-TYPE or an IMPLEMENTATION-DATA-TYPE breaks
    ``[TPS_GST_00020]``'s destination type: a record element's type is an
    ApplicationDataType. A field whose type was skipped chains the record to that
    type's entry.
    """
    skip_log = acc.skip_log
    acc.decided_composites.add(rec.path)
    if not rec.name or not rec.name.strip():
        skip_log.record_reason(rec.path, "APPLICATION-RECORD-DATA-TYPE", blank_short_name(), effect="abort")
        return
    if _verdict(ctx, acc, rec.path, rec.name, LAYER_APPLICATION_TYPES) != MAP:
        return
    fields: list[RecordField] = []
    unresolved = ctx.unresolved
    sw_base_by_path = ctx.sw_base_by_path
    # Its category is decided where the cause is found (a blank member name is
    # the file's, `[TPS_GST_00057]`).
    chain_reason: SkipReason | None = None
    # The field type's path when ``chain_reason`` is a chain behind it.
    chain_cause: str | None = None
    _elem_names = [e.name for e in rec.elements]
    _elem_name_dups = sorted({n for n in _elem_names if _elem_names.count(n) > 1})
    if _elem_name_dups:
        # The writer refuses duplicate field names, so the record is skipped
        # rather than written as a model `generate arxml` refuses.
        chain_reason = not_supported(
            f"duplicate record element SHORT-NAME(s) {_elem_name_dups} (field names must be unique within a record)"
        )
    if chain_reason is None:
        for element in rec.elements:
            if not element.name or not element.name.strip():
                chain_reason = blank_short_name("a record element has missing or blank SHORT-NAME")
                break
            if not element.type_ref:
                # SWCraft's refusal, never a chain to "" or an outside-the-file entry.
                chain_reason = not_supported(f"field '{element.name}' has no TYPE-TREF")
                break
            target_name = _member_type(ctx, acc, element.type_ref)
            if target_name is None:
                target_name = (
                    unresolved.keep(
                        element.type_ref,
                        UnresolvedReferrer(
                            path=rec.path,
                            tag="APPLICATION-RECORD-DATA-TYPE",
                            attribute="TYPE-TREF",
                            field="application_types.record_field.type",
                            member=element.name,
                        ),
                    )
                    if unresolved is not None
                    else None
                )
            if target_name is None:
                chain_reason = not_supported(
                    f"unresolved reference {element.type_ref} (field '{element.name}' type was skipped)"
                )
                chain_cause = element.type_ref
                break
            if element.type_ref in sw_base_by_path:
                # A record element is an ApplicationCompositeElementDataPrototype,
                # whose type is an ApplicationDataType (SWCT R4.2.2 :13908,
                # R4.3.0 :14857): `[TPS_GST_00020]`'s destination type.
                chain_reason = SkipReason(
                    f"field '{element.name}' is typed by the SW-BASE-TYPE {element.type_ref} directly -- a "
                    "record element's type is an ApplicationDataType, which a SW-BASE-TYPE is not",
                    SkipCategory.EXPORT_DEFECT,
                    StandardRule.REFERENCE_DESTINATION_TYPE,
                )
                break
            if acc.registry.layer_of(element.type_ref) == LAYER_IMPLEMENTATION_TYPES:
                # Same destination type as above; and since an implementation type
                # may share its name with an application type, writing it here
                # would bind the field to that application type.
                chain_reason = SkipReason(
                    f"field '{element.name}' is typed by the IMPLEMENTATION-DATA-TYPE {element.type_ref} -- a "
                    "record element's type is an ApplicationDataType, which an ImplementationDataType is not",
                    SkipCategory.EXPORT_DEFECT,
                    StandardRule.REFERENCE_DESTINATION_TYPE,
                )
                break
            fields.append(RecordField(name=element.name, type=target_name, description=element.description))
    if chain_reason is not None and chain_cause is not None:
        # A chain behind the skipped field type inherits that type's entry.
        record_chained_to(
            skip_log,
            ctx.declared,
            rec.path,
            "APPLICATION-RECORD-DATA-TYPE",
            chain_reason.text,
            cause_path=cause_path(chain_cause, ctx.merges),
            upstream=(ctx.model.skip_log,),
        )
        return
    if chain_reason is not None:
        skip_log.record_reason(rec.path, "APPLICATION-RECORD-DATA-TYPE", chain_reason, effect="abort")
        return
    if not fields:
        skip_log.record(
            rec.path,
            "APPLICATION-RECORD-DATA-TYPE",
            "no record elements",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return
    name = acc.registry.register(rec.path, rec.name, LAYER_APPLICATION_TYPES)
    acc.data_types.append(RecordDataType(name=name, kind="record", fields=fields, description=rec.description))
    # Committed only now: a later field could still have abandoned the record.
    if ctx.unresolved is not None:
        ctx.unresolved.commit(rec.path)


def _map_array(ctx: _TypeContext, acc: _TypeAccumulator, arr: AppArrayDataTypeInfo) -> None:
    """Map one APPLICATION-ARRAY-DATA-TYPE to an ``array``, or skip it with its reason.

    An element typed by a SW-BASE-TYPE or an IMPLEMENTATION-DATA-TYPE breaks
    ``[TPS_GST_00020]``'s destination type; a missing or non-positive
    MAX-NUMBER-OF-ELEMENTS is SWCraft's refusal; an element type that was skipped
    chains the array to that type's entry.
    """
    skip_log = acc.skip_log
    unresolved = ctx.unresolved
    acc.decided_composites.add(arr.path)
    if not arr.name or not arr.name.strip():
        skip_log.record_reason(arr.path, "APPLICATION-ARRAY-DATA-TYPE", blank_short_name(), effect="abort")
        return
    if _verdict(ctx, acc, arr.path, arr.name, LAYER_APPLICATION_TYPES) != MAP:
        return
    if not arr.element_type_ref:
        # No ELEMENT or no TYPE-TREF: SWCraft's refusal, never a chain to "" or
        # an outside-the-file entry.
        skip_log.record(
            arr.path,
            "APPLICATION-ARRAY-DATA-TYPE",
            "array element has no TYPE-TREF",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return
    target_name = _member_type(ctx, acc, arr.element_type_ref)
    if target_name is None:
        target_name = (
            unresolved.keep(
                arr.element_type_ref,
                UnresolvedReferrer(
                    path=arr.path,
                    tag="APPLICATION-ARRAY-DATA-TYPE",
                    attribute="TYPE-TREF",
                    field="application_types.array.element_type",
                ),
            )
            if unresolved is not None
            else None
        )
    if target_name is None:
        # A chain behind the skipped element type inherits that type's entry.
        record_chained_to(
            skip_log,
            ctx.declared,
            arr.path,
            "APPLICATION-ARRAY-DATA-TYPE",
            f"unresolved reference {arr.element_type_ref} (element type was skipped)",
            cause_path=cause_path(arr.element_type_ref, ctx.merges),
            upstream=(ctx.model.skip_log,),
        )
        return
    if arr.element_type_ref in ctx.sw_base_by_path:
        # An array element is an ApplicationCompositeElementDataPrototype,
        # whose type is an ApplicationDataType (SWCT R4.2.2 :13908, R4.3.0
        # :14857): `[TPS_GST_00020]`'s destination type.
        skip_log.record(
            arr.path,
            "APPLICATION-ARRAY-DATA-TYPE",
            f"element typed by the SW-BASE-TYPE {arr.element_type_ref} directly -- an array element's type is "
            "an ApplicationDataType, which a SW-BASE-TYPE is not",
            category=SkipCategory.EXPORT_DEFECT,
            rule=StandardRule.REFERENCE_DESTINATION_TYPE,
            effect="abort",
        )
        return
    if acc.registry.layer_of(arr.element_type_ref) == LAYER_IMPLEMENTATION_TYPES:
        # As for a record field: it would bind to an application type of that name.
        skip_log.record(
            arr.path,
            "APPLICATION-ARRAY-DATA-TYPE",
            f"element typed by the IMPLEMENTATION-DATA-TYPE {arr.element_type_ref} -- an array element's type "
            "is an ApplicationDataType, which an ImplementationDataType is not",
            category=SkipCategory.EXPORT_DEFECT,
            rule=StandardRule.REFERENCE_DESTINATION_TYPE,
            effect="abort",
        )
        return
    if arr.max_number_of_elements is None:
        skip_log.record(
            arr.path,
            "APPLICATION-ARRAY-DATA-TYPE",
            "missing MAX-NUMBER-OF-ELEMENTS",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return
    if arr.max_number_of_elements <= 0:
        # `ArrayDataType.length` must be > 0: skip and report, never a crash.
        skip_log.record(
            arr.path,
            "APPLICATION-ARRAY-DATA-TYPE",
            f"MAX-NUMBER-OF-ELEMENTS must be > 0, got {arr.max_number_of_elements}",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return
    name = acc.registry.register(arr.path, arr.name, LAYER_APPLICATION_TYPES)
    acc.data_types.append(
        ArrayDataType(
            name=name,
            kind="array",
            element_type=target_name,
            length=arr.max_number_of_elements,
            description=arr.description,
        )
    )
    # Committed only now: the checks above could still have abandoned the array.
    if unresolved is not None:
        unresolved.commit(arr.path)


def _report_consumed_descriptions(ctx: _TypeContext, acc: _TypeAccumulator) -> None:
    """One ``DESC`` line per consumed physics element whose own text no consuming type carries.

    A COMPU-METHOD, UNIT or DATA-CONSTR read inline on the type that names it has no
    field for its own ``DESC``. It is lost only when its text is not the one a
    consuming type carries: a repetition loses nothing.
    """
    model = ctx.model
    # Tagged `DESC`: the element itself is carried, only its text is not.
    consumed_physics = [
        *((compu.path, compu.description) for compu in model.compu_methods),
        *((unit_info.path, unit_info.description) for unit_info in model.units),
        *((constr.path, constr.description) for constr in model.data_constrs),
    ]
    for path, description in consumed_physics:
        carried = acc.consumer_descriptions.get(path)
        if carried is None or not description or description in carried:
            continue
        acc.skip_log.record(
            path,
            "DESC",
            (
                "DESC not carried: it differs from the type's description, which is carried"
                if any(carried)
                else "DESC not carried: the type that consumes it carries no description"
            ),
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )


def _report_implementation_compu(ctx: _TypeContext, acc: _TypeAccumulator, resolution: ImplResolution) -> None:
    """An implementation type's ``COMPU-METHOD-REF``: the reader's line left out, one line where it is not carried.

    A member of a duplicate group that is not mapped gets none: the group's
    record, or its primary, speaks.
    """
    merges = ctx.merges
    for impl in ctx.model.impl_data_types:
        if impl.compu_method_ref is None:
            continue
        acc.registry.carried_reader_lines.add((f"{impl.path}/SW-DATA-DEF-PROPS", "COMPU-METHOD-REF"))
        if impl.path in resolution.compu_carried or (merges is not None and merges.outcome(impl.path) != MAP):
            continue
        report_form_not_carried(
            f"{impl.path}/SW-DATA-DEF-PROPS",
            "COMPU-METHOD-REF",
            "the compu method this implementation data type names is not imported as a texttable element, "
            "so the type is written without it",
            acc.skip_log,
        )


def _unmapped_member(merges: MergeDecisions | None, path: str) -> bool:
    """Whether *path* is a member of a duplicate group that is not mapped, which speaks through its group."""
    return merges is not None and merges.outcome(path) != MAP


def _report_unconsumed_forms(ctx: _TypeContext, acc: _TypeAccumulator, forms: Forms) -> None:
    """One skip line per COMPU-METHOD, UNIT and DATA-CONSTR nothing carries.

    A compu method read as an enum's literals or as an application type's
    ``conversion`` is represented inline on the type that names it; an element is
    represented too, referenced or not; a member of a duplicate group that is not
    mapped speaks through its group. Everything else is content this import does
    not carry, and none of it disappears in silence.
    """
    model = ctx.model
    merges = ctx.merges
    skip_log = acc.skip_log
    for compu in model.compu_methods:
        if (
            compu.path in acc.consumed_compu_paths
            or compu.path in forms.created
            or _unmapped_member(merges, compu.path)
        ):
            continue
        if compu.category == "TEXTTABLE":
            skip_log.record(
                compu.path,
                "COMPU-METHOD",
                "TEXTTABLE compu-method not used by any imported enum type (its application primitive "
                "type was reduced, skipped, or imported as an application type because the table "
                "itself could not be read)",
                category=SkipCategory.NOT_SUPPORTED,
                effect="abort",
            )
            continue
        skip_log.record(
            compu.path,
            "COMPU-METHOD",
            f"non-TEXTTABLE compu-method (category '{compu.category}') was not read as an application "
            "type's conversion and has no SWCraft representation",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )

    for unit_info in model.units:
        if (
            unit_info.path in acc.consumed_property_paths
            or unit_info.path in forms.created
            or _unmapped_member(merges, unit_info.path)
        ):
            continue
        skip_log.record(
            unit_info.path,
            "UNIT",
            "UNIT not carried: it is no element SWCraft holds, and no imported data type reads it inline",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )

    for constr in model.data_constrs:
        if (
            constr.path in acc.consumed_property_paths
            or constr.path in forms.created
            or _unmapped_member(merges, constr.path)
        ):
            continue
        skip_log.record(
            constr.path,
            "DATA-CONSTR",
            "DATA-CONSTR not carried: a data constraint element holds one rule of physical limits, and no "
            "imported data type reads this one inline",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )


@dataclass(frozen=True)
class _InterfaceContext:
    """What :func:`map_interfaces` reads across the interfaces: its arguments and the lookups built once per call."""

    model: ImportModel
    registry: PathRegistry
    merges: MergeDecisions | None
    unresolved: UnresolvedReferences | None
    declared: frozenset[str]
    #: The mapped types and elements, last declaration winning; the elements make
    #: an enumeration naming a table state its entries to an `init`.
    data_types_by_name: DataDescription
    constants_by_path: dict[str, ConstantSpecInfo]
    #: The file's own types, which judge a source value.
    source_types: SourceTypes
    port_inits_by_element: dict[str, list[PortComSpecInitInfo]]


@dataclass
class _InterfaceAccumulator:
    """What :func:`map_interfaces` fills across the interfaces, for the whole call."""

    skip_log: SkipLog
    interfaces: list[Interface] = field(default_factory=list)
    #: Every name claimed, with the path that claimed it.
    seen_names: dict[str, str] = field(default_factory=dict)
    # The authority on what was mapped: a conflicting group claims a name in
    # `seen_names` at a member it never imports.
    committed_interface_paths: set[str] = field(default_factory=set)
    #: The constants whose labels were reported, once per constant.
    labelled_constants: set[str] = field(default_factory=set)
    # The port com-spec init keys a data element read; a key nothing reads is an
    # INIT-VALUE reported at the end, which only the element loop can tell.
    read_element_paths: set[str] = field(default_factory=set)


def map_interfaces(
    model: ImportModel,
    registry: PathRegistry,
    data_types: list[DataType],
    *,
    merges: MergeDecisions | None = None,
    unresolved: UnresolvedReferences | None = None,
    elements: Sequence[DataDescriptionElement],
    held_unread_inits: list[HeldUnreadInit] | None = None,
) -> tuple[list[Interface], SkipLog]:
    """Map every SENDER-RECEIVER- and CLIENT-SERVER-INTERFACE of *model* onto the ``interfaces/`` layer.

    A data element's or an argument's TYPE-TREF resolves through *registry*, so it takes exactly the name
    :func:`map_data_types` assigned the type. A type that resolves to a base type skips the interface
    (``[TPS_GST_00020]``'s destination type, :attr:`StandardRule.REFERENCE_DESTINATION_TYPE`).

    Interfaces share one name space across both kinds and are never renamed: the second and further declarations of
    a name are skipped and reported. A port com-spec init no data element read is reported once every interface is
    mapped.

    Args:
        model: The export, as the reader extracted it.
        registry: The ``PathRegistry`` :func:`map_data_types` returned for this model; mutated in place (radix
            lines, port inits, interface source paths).
        data_types: :func:`map_data_types`' list.
        merges: The verdict on same-name declarations across packages, or ``None``.
        unresolved: The unresolved-reference policy, or ``None`` to keep no reference.
        elements: The data description elements :func:`map_data_types` created.
        held_unread_inits: ``import component``'s list: an unread port com-spec init whose interface the file
            declares nowhere is held there for the late pass rather than recorded. ``None`` records every line.

    Returns:
        ``(interfaces, skip_log)`` -- ``registry`` carries, in ``interface_source_paths``, the ARXML path of every
        interface committed and of each conflicting group's first member, whose name is claimed.

    Raises:
        pydantic.ValidationError: a mapped element fails ``schema.py`` validation.
    """
    acc = _InterfaceAccumulator(skip_log=SkipLog())
    ctx = _interface_context(model, registry, data_types, merges, unresolved, elements, acc)
    for sr in model.sr_interfaces:
        _map_sender_receiver_interface(ctx, acc, sr)
    for cs in model.cs_interfaces:
        _map_client_server_interface(ctx, acc, cs)

    report_unread_port_com_spec_inits(
        ctx.port_inits_by_element,
        acc.read_element_paths,
        acc.skip_log,
        unresolved,
        declared=ctx.declared,
        mapped_interface_paths=frozenset(acc.committed_interface_paths),
        merges=merges,
        held=held_unread_inits,
    )
    # Every name claimed, conflicting groups included, for the write order and
    # the stub-name reservation.
    registry.interface_source_paths.update(acc.seen_names)

    return acc.interfaces, acc.skip_log


def _interface_context(
    model: ImportModel,
    registry: PathRegistry,
    data_types: list[DataType],
    merges: MergeDecisions | None,
    unresolved: UnresolvedReferences | None,
    elements: Sequence[DataDescriptionElement],
    acc: _InterfaceAccumulator,
) -> _InterfaceContext:
    """Build the lookups the interfaces are mapped against, once; reading the port inits records into the log."""
    declared = declared_paths(model)
    data_types_by_name = DataDescription.last_declared([*data_types, *elements])

    # Built once per call: an export may carry a great many constants and port inits.
    constants_by_path: dict[str, ConstantSpecInfo] = {c.path: c for c in model.constant_specs}
    source_types = SourceTypes(model)
    port_inits_by_element = port_com_spec_inits(model, acc.skip_log)
    return _InterfaceContext(
        model=model,
        registry=registry,
        merges=merges,
        unresolved=unresolved,
        declared=declared,
        data_types_by_name=data_types_by_name,
        constants_by_path=constants_by_path,
        source_types=source_types,
        port_inits_by_element=port_inits_by_element,
    )


def _interface_verdict(ctx: _InterfaceContext, acc: _InterfaceAccumulator, path: str, name: str) -> str:
    """The merge-verdict outcome for one interface declaration.

    A conflicting group claims its name in `seen_names` at the first member met,
    so a sender-receiver and a client-server interface of one name, which the
    verdict never judges, are still answered by the first-wins duplicate check.
    """
    merges = ctx.merges
    if merges is None:
        return MAP
    outcome = merges.outcome(path)
    if outcome == CONFLICT:
        acc.seen_names.setdefault(name, path)
    return outcome


def _resolve_interface_type(ctx: _InterfaceContext, type_ref: str) -> tuple[str | None, SkipReason | None]:
    """Resolve one interface-facing TYPE-TREF to ``(emitted_name, skip_reason)`` by :func:`resolve_prototype_type`."""
    return resolve_prototype_type(type_ref, ctx.registry, ctx.data_types_by_name, ctx.declared)


def _emitted_application(registry: PathRegistry, type_ref: str) -> bool:
    """Whether the member's type, the path *type_ref*, is an application type, stated as ``application_type``.

    The layer the path resolves into decides, never the emitted name, which an
    application and an implementation type may share. A kept unresolved path is
    stated as ``implementation_type``, where an external type stub resolves it.
    """
    return registry.layer_of(type_ref) == LAYER_APPLICATION_TYPES


def _map_sender_receiver_interface(ctx: _InterfaceContext, acc: _InterfaceAccumulator, sr: SRInterfaceInfo) -> None:
    """One SENDER-RECEIVER-INTERFACE: an ``SRInterface`` committed with its data elements, or dropped with a reason.

    A data element that cannot be mapped drops the whole interface. The radix
    lines, the port inits and the unresolved-reference records an element produces
    are held until the interface is committed, because a later element can still
    abandon it.
    """
    skip_log = acc.skip_log
    seen_names = acc.seen_names
    if not sr.name or not sr.name.strip():
        drop_interface(skip_log, sr, "SENDER-RECEIVER-INTERFACE", blank_short_name())
        return
    if _interface_verdict(ctx, acc, sr.path, sr.name) != MAP:
        return
    if sr.name in seen_names:
        drop_interface(
            skip_log,
            sr,
            "SENDER-RECEIVER-INTERFACE",
            not_supported(f"duplicate interface name '{sr.name}' (already declared at {seen_names[sr.name]})"),
        )
        return
    if sr.is_service is not None and parse_xsd_boolean(sr.is_service) is None:
        # An `IS-SERVICE` no xsd:boolean reads is never a silent `false`. Decided
        # before any member is mapped, so a dropped interface stages nothing.
        drop_interface(
            skip_log, sr, "SENDER-RECEIVER-INTERFACE", not_supported(unreadable_is_service_reason(sr.is_service))
        )
        return

    data_elements: list[DataElement] = []
    # Held until the interface is committed: a later element can still abandon it.
    radix_normalizations: list[tuple[str, list[str]]] = []
    port_init_outcomes: list[tuple[PortInitKey, PortInitOutcome]] = []
    chain: SkipReason | None = None
    for de in sr.data_elements:
        mapped = _map_data_element(ctx, acc, sr, de, radix_normalizations, port_init_outcomes)
        if isinstance(mapped, SkipReason):
            chain = mapped
            break
        data_elements.append(mapped)
    if chain is not None:
        drop_interface(skip_log, sr, "SENDER-RECEIVER-INTERFACE", chain)
        return
    if not data_elements:
        drop_interface(skip_log, sr, "SENDER-RECEIVER-INTERFACE", not_supported("no data elements"))
        return

    seen_names[sr.name] = sr.path
    acc.committed_interface_paths.add(sr.path)
    acc.interfaces.append(
        SRInterface(
            name=sr.name,
            kind="sender_receiver",
            data_elements=data_elements,
            description=sr.description,
            # An absent `IS-SERVICE` is `false`, the value the writer writes.
            is_service=parse_xsd_boolean(sr.is_service) is True,
        )
    )
    # Each line carries the interface's name: `--packages` may still drop whole
    # interfaces, and the lines are filtered by that name.
    for element_path, spellings in radix_normalizations:
        ctx.registry.record_init_radix_normalization(sr.name, element_path, spellings)
    # One com-spec of one port per key (both positions are part of it), so the component mapper
    # finds the outcome of the com-spec it passed; a second com-spec for one element is reported there.
    for key, outcome in port_init_outcomes:
        ctx.registry.port_inits.setdefault(key, outcome)
    if ctx.unresolved is not None:
        ctx.unresolved.commit(sr.path)


def _map_data_element(
    ctx: _InterfaceContext,
    acc: _InterfaceAccumulator,
    sr: SRInterfaceInfo,
    de: SRDataElementInfo,
    radix_normalizations: list[tuple[str, list[str]]],
    port_init_outcomes: list[tuple[PortInitKey, PortInitOutcome]],
) -> DataElement | SkipReason:
    """One data element of *sr*, or why the whole interface is abandoned.

    A blank name is decided before the type is asked about, so no unresolved
    reference is staged for an element that keeps nothing. The radix lines and the
    port inits are appended to the interface's held lists.
    """
    unresolved = ctx.unresolved
    if not de.name or not de.name.strip():
        # Skips the whole interface; let through, `reconcile.identifier_violations`
        # would abort the whole import over one nameless element.
        return blank_short_name("a data element has missing or blank SHORT-NAME")
    emitted, reason = _resolve_interface_type(ctx, de.type_ref)
    if emitted is None and unresolved is not None:
        # A base type named directly stays skipped, the file's own defect
        # (`[TPS_GST_00020]`): its target is declared, so nothing is kept for it.
        emitted = unresolved.keep(
            de.type_ref,
            UnresolvedReferrer(
                path=sr.path,
                tag="SENDER-RECEIVER-INTERFACE",
                attribute="TYPE-TREF",
                field="interfaces.data_element.implementation_type",
                member=de.name,
            ),
        )
    if emitted is None:
        assert reason is not None  # `resolve_prototype_type` answers exactly one of the two
        return SkipReason(f"data element '{de.name}': {reason.text}", reason.category, reason.rule)
    element_path = f"{sr.path}/{de.name}"
    acc.read_element_paths.add(element_path)
    application = _emitted_application(ctx.registry, de.type_ref)
    init, init_reason = init_value_for_element(
        de.init_ref,
        emitted,
        ctx.constants_by_path,
        ctx.data_types_by_name,
        application=application,
        source_type_ref=de.type_ref,
        source_types=ctx.source_types,
    )
    _record_element_init(ctx, acc, de, element_path, emitted, application, init, init_reason, radix_normalizations)
    _decide_port_inits(ctx, de, element_path, emitted, application, init, port_init_outcomes)
    sw_impl_policy, sw_calibration_access = data_element_properties(de.sw_impl_policy, de.sw_calibration_access)
    return DataElement(
        name=de.name,
        application_type=emitted if application else None,
        implementation_type=None if application else emitted,
        # `_map_init_value` shapes an InitValue; its recursive walk is typed `object`.
        init=cast("InitValue | None", init),
        description=de.description,
        sw_impl_policy=sw_impl_policy,
        sw_calibration_access=sw_calibration_access,
    )


def _record_element_init(
    ctx: _InterfaceContext,
    acc: _InterfaceAccumulator,
    de: SRDataElementInfo,
    element_path: str,
    emitted: str,
    application: bool,
    init: object | None,
    init_reason: SkipReason | None,
    radix_normalizations: list[tuple[str, list[str]]],
) -> None:
    """The lines a data element's own init value leaves.

    A value refused is one ``INIT-VALUE`` line, the element landing without it;
    where the type is a kept unresolved path, the line names the external type stub
    that path may become. A value carried reports, once per constant, the labels
    and the value-form differences the writer does not write back, and holds the
    radix spellings it re-spelled in decimal in *radix_normalizations*.
    """
    unresolved = ctx.unresolved
    skip_log = acc.skip_log
    constants_by_path = ctx.constants_by_path
    if init_reason is not None and unresolved is not None and emitted == de.type_ref:
        # No init can be shaped against an unresolved type path; it may become an
        # external type stub (`rule:interface.init-on-external-type`).
        missing = undeclared_type_reason(emitted)
        init_text = init_reason.text

        # A named function rather than a lambda, so the deferred line's type is stated.
        def once_stubbed(stub: str, init_text: str = init_text, missing: str = missing, path: str = de.type_ref) -> str:
            return stub_typed_init_reason(init_text, missing, stub, path)

        unresolved.chain_skip(
            skip_log,
            element_path,
            "INIT-VALUE",
            init_reason.text,
            target=de.type_ref,
            layer=LAYER_IMPLEMENTATION_TYPES,
            once_stubbed=once_stubbed,
            effect="abort",
        )
    elif init_reason is not None:
        # The element lands without its init: the field is optional, a wrong
        # init is not.
        skip_log.record_reason(element_path, "INIT-VALUE", init_reason, effect="abort")
    elif de.init_ref is not None:
        source = constants_by_path.get(de.init_ref)
        if source is not None and source.value is not None and de.init_ref not in acc.labelled_constants:
            acc.labelled_constants.add(de.init_ref)
            for _label in range(
                value_label_losses(source.value, emitted, ctx.data_types_by_name, application=application)
            ):
                report_value_label(de.init_ref, skip_log)
            for line in carried_value_lines(source.value, de.type_ref, ctx.source_types, application=application):
                report_value_line(de.init_ref, "VALUE-SPEC", line, skip_log)
        spellings = radix_spellings(source.value, init) if source is not None and source.value is not None else []
        if spellings:
            radix_normalizations.append((element_path, spellings))


def _decide_port_inits(
    ctx: _InterfaceContext,
    de: SRDataElementInfo,
    element_path: str,
    emitted: str,
    application: bool,
    init: object | None,
    port_init_outcomes: list[tuple[PortInitKey, PortInitOutcome]],
) -> None:
    """Decide every port com-spec that initializes *de* with a value of its own, appending to *port_init_outcomes*.

    Each is decided against the element's type and its own value; whether it is
    carried or a line is the component mapper's, which knows whether the port is kept.
    """
    kept_unresolved = ctx.unresolved is not None and emitted == de.type_ref
    for com_spec in port_init_overrides(de.init_ref, ctx.port_inits_by_element.get(element_path, [])):
        outcome = port_init_outcome(
            com_spec,
            init,
            emitted,
            ctx.constants_by_path,
            ctx.data_types_by_name,
            application=application,
            source_type_ref=de.type_ref,
            source_types=ctx.source_types,
        )
        if kept_unresolved and outcome.reason is not None:
            missing = undeclared_type_reason(emitted)
            text = outcome.reason.text

            # A named function rather than a lambda, so the deferred line's type is stated.
            def once_stubbed(stub: str, text: str = text, missing: str = missing, path: str = de.type_ref) -> str:
                return stub_typed_init_reason(text, missing, stub, path)

            outcome = replace(outcome, type_target=de.type_ref, once_stubbed=once_stubbed)
        port_init_outcomes.append((PortInitKey.of_init(com_spec), outcome))


def _map_client_server_interface(ctx: _InterfaceContext, acc: _InterfaceAccumulator, cs: CSInterfaceInfo) -> None:
    """One CLIENT-SERVER-INTERFACE: a ``CSInterface`` committed with its operations, or dropped with a reason.

    An operation with a blank name or a blank argument name is skipped alone; an
    argument whose type or direction cannot be mapped drops the whole interface,
    and so does an interface left with no operation.
    """
    skip_log = acc.skip_log
    seen_names = acc.seen_names
    if not cs.name or not cs.name.strip():
        drop_interface(skip_log, cs, "CLIENT-SERVER-INTERFACE", blank_short_name())
        return
    if _interface_verdict(ctx, acc, cs.path, cs.name) != MAP:
        return
    if cs.name in seen_names:
        drop_interface(
            skip_log,
            cs,
            "CLIENT-SERVER-INTERFACE",
            not_supported(f"duplicate interface name '{cs.name}' (already declared at {seen_names[cs.name]})"),
        )
        return
    if cs.is_service is not None and parse_xsd_boolean(cs.is_service) is None:
        # An `IS-SERVICE` no xsd:boolean reads is never a silent `false`. Decided
        # before any member is mapped, so a dropped interface stages nothing.
        drop_interface(
            skip_log, cs, "CLIENT-SERVER-INTERFACE", not_supported(unreadable_is_service_reason(cs.is_service))
        )
        return

    operations: list[CSOperation] = []
    chain: SkipReason | None = None
    for op in cs.operations:
        mapped = _map_operation(ctx, acc, cs, op)
        if mapped is None:
            continue
        if isinstance(mapped, SkipReason):
            chain = mapped
            break
        operations.append(mapped)
    if chain is not None:
        drop_interface(skip_log, cs, "CLIENT-SERVER-INTERFACE", chain)
        return
    if not operations:
        drop_interface(skip_log, cs, "CLIENT-SERVER-INTERFACE", not_supported("no operations"))
        return

    seen_names[cs.name] = cs.path
    acc.committed_interface_paths.add(cs.path)
    acc.interfaces.append(
        CSInterface(
            name=cs.name,
            kind="client_server",
            operations=operations,
            description=cs.description,
            is_service=parse_xsd_boolean(cs.is_service) is True,
        )
    )
    # Committed once for all operations, so a dropped operation must stage nothing.
    if ctx.unresolved is not None:
        ctx.unresolved.commit(cs.path)


def _map_operation(
    ctx: _InterfaceContext, acc: _InterfaceAccumulator, cs: CSInterfaceInfo, op: CSOperationInfo
) -> CSOperation | SkipReason | None:
    """One operation of *cs*: the ``CSOperation``, ``None`` where it is skipped alone, or why the interface is dropped.

    A blank operation name, or a blank name of any of its arguments, skips this one
    operation with its line under the operation's own path, decided before any
    argument type is resolved. An argument whose type or direction cannot be
    mapped abandons the whole interface. An argument's
    ``SERVER-ARGUMENT-IMPL-POLICY`` the model does not carry is one line.
    """
    skip_log = acc.skip_log
    unresolved = ctx.unresolved
    # Recorded under the operation's path, not `cs.path`: a line at the
    # interface's path would make every port naming it be dropped.
    operation_path = f"{cs.path}/{op.name.strip()}" if (op.name or "").strip() else f"{cs.path}/{UNNAMED_MEMBER}"
    if not op.name or not op.name.strip():
        skip_log.record_reason(
            operation_path,
            "CLIENT-SERVER-OPERATION",
            blank_short_name("an operation has missing or blank SHORT-NAME"),
            effect="abort",
        )
        return None
    # Decided over the whole argument list before any type is resolved, so a
    # dropped operation stages no unresolved reference the interface's commit
    # would then report.
    blank_argument = any(not arg.name or not arg.name.strip() for arg in op.arguments)
    if blank_argument:
        skip_log.record_reason(
            operation_path,
            "CLIENT-SERVER-OPERATION",
            blank_short_name(f"operation '{op.name}': an argument has missing or blank SHORT-NAME"),
            effect="abort",
        )
        return None
    arguments: list[CSArgument] = []
    for arg in op.arguments:
        emitted, reason = _resolve_interface_type(ctx, arg.type_ref)
        if emitted is None and unresolved is not None:
            # The occurrence includes the operation: two operations may each have
            # an argument of one name, and the DEST is read per occurrence.
            emitted = unresolved.keep(
                arg.type_ref,
                UnresolvedReferrer(
                    path=cs.path,
                    tag="CLIENT-SERVER-INTERFACE",
                    attribute="TYPE-TREF",
                    field="interfaces.argument.implementation_type",
                    member=arg.name,
                ),
                occurrence=f"{cs.path}/{op.name}/{arg.name}",
            )
        if emitted is None:
            assert reason is not None  # `resolve_prototype_type` answers exactly one of the two
            return SkipReason(
                f"operation '{op.name}' argument '{arg.name}': {reason.text}", reason.category, reason.rule
            )
        direction = CS_DIRECTION_MAP.get(arg.direction)
        if direction is None:
            return not_supported(
                f"operation '{op.name}' argument '{arg.name}': unsupported direction '{arg.direction}'"
            )
        application = _emitted_application(ctx.registry, arg.type_ref)
        arguments.append(
            CSArgument(
                name=arg.name,
                direction=direction,
                application_type=emitted if application else None,
                implementation_type=None if application else emitted,
                description=arg.description,
            )
        )
        array = argument_policy_meaning(emitted, ctx.data_types_by_name, application=application)
        if not server_argument_policy_carried(arg.server_argument_impl_policy, array):
            report_server_argument_policy(
                f"{cs.path}/{op.name}/{arg.name}", arg.server_argument_impl_policy or "", array, skip_log
            )
    return CSOperation(name=op.name, arguments=arguments, description=op.description)
