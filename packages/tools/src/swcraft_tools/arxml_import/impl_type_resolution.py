"""Resolution of ``IMPLEMENTATION-DATA-TYPE`` elements.

``resolve_impl_type`` turns each implementation data type of the export into a
SWCraft data type, in dependency order: value types, type references, and the
structure, array and data-reference composites, with the skip reasons for every
shape the model cannot carry. :class:`ImplResolution` is the state one import shares.

Depends on ``registry``, ``merge`` and ``unresolved`` only.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from swcraft_tools.arxml_import.merge import (
    CONFLICT,
    EXCLUDED,
    LAYER_IMPLEMENTATION_TYPES,
    MergeDecisions,
    record_chained_to,
)
from swcraft_tools.arxml_import.registry import (
    PathRegistry,
)
from swcraft_tools.arxml_import.unresolved import (
    UnresolvedReferences,
    UnresolvedReferrer,
)
from swcraft_tools.arxml_reader import (
    ImplDataTypeInfo,
    SkipCategory,
    SkipLog,
    SkipReason,
    StandardRule,
    SwBaseTypeInfo,
    blank_short_name,
    not_supported,
)
from swcraft_tools.schema import (
    DataType,
    ImplementationArrayDataType,
    ImplementationElement,
    ImplementationRecordDataType,
    PointerDataType,
    ReferenceDataType,
    ValueDataType,
)


@dataclass(frozen=True)
class ImplResolution:
    """What every :func:`resolve_impl_type` call of one import shares.

    The binding is frozen; the containers are the import's accumulators, which the
    recursion writes into. Only ``visiting`` changes per branch, so it stays a
    parameter.
    """

    impl_by_path: dict[str, ImplDataTypeInfo]
    sw_base_by_path: dict[str, SwBaseTypeInfo]
    registry: PathRegistry
    skip_log: SkipLog
    data_types: list[DataType]
    merges: MergeDecisions | None = None
    unresolved: UnresolvedReferences | None = None
    failed: set[str] = field(default_factory=set)
    #: :func:`merge.record_chained_to`'s arguments. Required: an empty
    #: *declared* would file every unrooted chain as outside the file.
    declared: frozenset[str] = field(kw_only=True)
    upstream: Sequence[SkipLog] = field(kw_only=True)
    #: The ``DATA-CONSTR`` paths this import declares, and per one a value type
    #: reads as its range, the descriptions of those value types.
    constr_paths: frozenset[str] = frozenset()
    consumed_constraints: dict[str, set[str]] = field(default_factory=dict)
    #: What a value or reference type's ``compu_method_ref`` is written as (the
    #: ``texttable`` element's name, a kept unresolved path, else ``None``), and the
    #: paths of the types written with one.
    compu_reference: Callable[[ImplDataTypeInfo], str | None] = field(kw_only=True)
    compu_carried: set[str] = field(default_factory=set)


def _implementation_compu_method(impl: ImplDataTypeInfo, ctx: ImplResolution) -> str | None:
    """What *impl*'s ``compu_method_ref`` is written as (:attr:`ImplResolution.compu_reference`), noting it carried."""
    reference = ctx.compu_reference(impl)
    if reference is not None:
        ctx.compu_carried.add(impl.path)
    return reference


def _value_type(impl: ImplDataTypeInfo, name: str, base: str, ctx: ImplResolution) -> ValueDataType:
    """The ``value_type``, with the range the reader carried from its ``DATA-CONSTR``.

    A constraint the file declares is consumed by the type that names it, so it is
    not reported a second time as an element nothing carries.
    """
    if impl.data_constr_ref and impl.data_constr_ref in ctx.constr_paths:
        ctx.consumed_constraints.setdefault(impl.data_constr_ref, set()).add(impl.description)
    return ValueDataType(
        name=name,
        kind="value_type",
        base=base,
        value_min=impl.value_min,
        value_max=impl.value_max,
        compu_method_ref=_implementation_compu_method(impl, ctx),
        description=impl.description,
    )


def resolve_impl_type(path: str, ctx: ImplResolution, visiting: set[str]) -> str | None:
    """Resolve one IMPLEMENTATION-DATA-TYPE path to its emitted SWCraft name.

    Builds and registers the SWCraft DataType the first time a path is seen.
    The merge verdict (`ctx.merges`) is consulted here because the function is
    recursive: a member of a merged group is redirected onto the primary, and a
    member of a conflicting group refused, wherever it is first met. With no
    verdict (``ctx.merges`` is ``None``, ``import component``) nothing is
    redirected or refused.

    VALUE -> `value_type`; TYPE_REFERENCE -> `reference_type`; STRUCTURE, ARRAY
    and DATA_REFERENCE -> `implementation_record`, `implementation_array` or
    `pointer` in the shapes `_unsupported_shape` admits. Any other category is
    skipped as unsupported. Every type is registered under its ``SHORT-NAME`` in
    the ``implementation_types`` layer.

    `visiting` turns a cyclic TYPE_REFERENCE chain or pointer graph into a
    skip. `ctx.failed` remembers a path whose category branch failed, so such a
    type has one skip entry, not one per referrer, and a cycle's path ends in it
    too. The missing-type and blank-name refusals record on every visit; a
    verdict refusal records nothing (it is reported elsewhere).

    Returns:
        The emitted name, or ``None`` when refused: with a skip entry, except for
        an EXCLUDED or CONFLICT verdict, which is reported elsewhere.
    """
    skip_log, registry = ctx.skip_log, ctx.registry
    if ctx.merges is not None:
        path = ctx.merges.primary_path(path)
        if _refused_by_verdict(path, ctx):
            return None

    existing = registry.resolve(path)
    if existing is not None:
        return existing

    if path in ctx.failed:
        return None

    if path in visiting:
        skip_log.record(
            path,
            "IMPLEMENTATION-DATA-TYPE",
            "cyclic type reference",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return None

    impl = ctx.impl_by_path.get(path)
    if impl is None:
        _report_missing_impl(path, ctx)
        return None

    if not impl.name or not impl.name.strip():
        skip_log.record_reason(impl.path, "IMPLEMENTATION-DATA-TYPE", blank_short_name(), effect="abort")
        return None

    visiting.add(path)
    try:
        if impl.category == "VALUE":
            return _resolve_value_impl(impl, ctx)

        if impl.category == "TYPE_REFERENCE":
            return _resolve_reference_impl(impl, ctx, visiting)

        if impl.category in _COMPOSITE_CATEGORIES:
            return _resolve_composite_impl(impl, ctx, visiting)

        skip_log.record(
            impl.path,
            "IMPLEMENTATION-DATA-TYPE",
            f"unsupported category '{impl.category}'",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return None
    finally:
        visiting.discard(path)
        if registry.resolve(path) is None:
            ctx.failed.add(path)


def _refused_by_verdict(path: str, ctx: ImplResolution) -> bool:
    """Whether the merge verdict refuses *path*, which is already redirected onto its primary.

    A member of a conflicting group is refused after its name is claimed; one a
    ``--packages`` selection excluded is refused with nothing claimed.
    """
    merges = ctx.merges
    assert merges is not None
    outcome = merges.outcome(path)
    if outcome == EXCLUDED:
        # The kept member holds the name; the reference that brought us here
        # is already reported as outside the selected packages.
        return True
    if outcome == CONFLICT:
        # No skip entry: the group has a conflict record of its own.
        impl = ctx.impl_by_path.get(path)
        if impl is not None and (impl.name or "").strip():
            group = merges.group_of(path)
            ctx.registry.claim(impl.name, LAYER_IMPLEMENTATION_TYPES, path, group=group.key if group else None)
        return True
    return False


def _report_missing_impl(path: str, ctx: ImplResolution) -> None:
    """Record why *path* names no implementation type this import holds.

    The path is a SW-BASE-TYPE whose own mapping failed, a declaration the file
    does not make, or a declaration that is not an implementation type SWCraft
    kept.
    """
    skip_log = ctx.skip_log
    if path in ctx.sw_base_by_path:
        # A SW-BASE-TYPE that failed its own mapping (e.g. missing size/encoding).
        skip_log.record(
            path,
            "SW-BASE-TYPE",
            "referenced base type was itself skipped",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
    elif path not in ctx.declared:
        # Only `import component` reaches this: `import model` keeps every
        # reference the file declares nowhere.
        skip_log.record(
            path,
            "IMPLEMENTATION-DATA-TYPE",
            f"unresolved reference {path} (the file declares no implementation data type at that path)",
            category=SkipCategory.OUTSIDE_FILE,
            effect="abort",
        )
    else:
        # Declared, but not as an implementation type SWCraft kept: the entry
        # inherits the declaration's own.
        record_chained_to(
            skip_log,
            ctx.declared,
            path,
            "IMPLEMENTATION-DATA-TYPE",
            f"unresolved reference {path}",
            cause_path=path,
            upstream=ctx.upstream,
        )


def _register_impl(impl: ImplDataTypeInfo, ctx: ImplResolution) -> str:
    """Register *impl* under its declared name in the implementation layer.

    Names are reserved per layer, so a kept same-name pair's implementation
    member keeps the name too.
    """
    return ctx.registry.register(impl.path, impl.name, LAYER_IMPLEMENTATION_TYPES)


def _resolve_value_impl(impl: ImplDataTypeInfo, ctx: ImplResolution) -> str | None:
    """A VALUE implementation type as a `value_type`, over the SW-BASE-TYPE its BASE-TYPE-REF names.

    `base` references the base type and never copies it
    (`rule:interface.data-element-base-type` refuses a data prototype typed by a
    base type). A kept unresolved target is the export's own path in `base`.
    """
    skip_log, registry, unresolved = ctx.skip_log, ctx.registry, ctx.unresolved
    if not impl.base_type_ref:
        skip_log.record(
            impl.path,
            "IMPLEMENTATION-DATA-TYPE",
            "VALUE category impl type has no BASE-TYPE-REF",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return None
    # An unresolved target (absent from the file, dropped as a conflicting
    # duplicate group, or excluded by `--packages`) is kept as the export's own
    # path in `base`; `check-model` reports it and `generate arxml` refuses it.
    # Asked before the declaredness test: two of the three causes belong to a
    # target the export does declare.
    if (
        unresolved is not None
        and (
            kept := unresolved.keep(
                impl.base_type_ref,
                UnresolvedReferrer(
                    path=impl.path,
                    tag="IMPLEMENTATION-DATA-TYPE",
                    attribute="BASE-TYPE-REF",
                    field="implementation_types.value_type.base",
                ),
            )
        )
        is not None
    ):
        name = _register_impl(impl, ctx)
        ctx.data_types.append(_value_type(impl, name, kept, ctx))
        unresolved.commit(impl.path)
        return name
    if impl.base_type_ref not in ctx.sw_base_by_path:
        if impl.base_type_ref not in ctx.declared:
            # `import component` alone: `import model` kept it above.
            skip_log.record(
                impl.path,
                "IMPLEMENTATION-DATA-TYPE",
                f"unresolved reference {impl.base_type_ref} (the file declares no base type at that path)",
                category=SkipCategory.OUTSIDE_FILE,
                effect="abort",
            )
        else:
            record_chained_to(
                skip_log,
                ctx.declared,
                impl.path,
                "IMPLEMENTATION-DATA-TYPE",
                f"unresolved reference {impl.base_type_ref} (base type)",
                cause_path=impl.base_type_ref,
                upstream=ctx.upstream,
            )
        return None
    base_name = registry.resolve(impl.base_type_ref)
    if base_name is None:
        # A chain behind the skipped base type inherits its entry.
        record_chained_to(
            skip_log,
            ctx.declared,
            impl.path,
            "IMPLEMENTATION-DATA-TYPE",
            f"unresolved reference {impl.base_type_ref} (base type was skipped)",
            cause_path=cause_path(impl.base_type_ref, ctx.merges),
            upstream=ctx.upstream,
        )
        return None
    name = _register_impl(impl, ctx)
    ctx.data_types.append(_value_type(impl, name, base_name, ctx))
    # Its compu method's unresolved-reference record may now be reported.
    if unresolved is not None:
        unresolved.commit(impl.path)
    return name


def _resolve_reference_impl(impl: ImplDataTypeInfo, ctx: ImplResolution, visiting: set[str]) -> str | None:
    """A TYPE_REFERENCE implementation type as a `reference_type`, one step over its target.

    `base` is the emitted name of the implementation type its
    IMPLEMENTATION-DATA-TYPE-REF names, resolved recursively first; a chain of
    any depth is written one link per element and nothing is flattened
    (`rule:data-type.reference-type-base-not-implementation-type` refuses only a
    base, enum, record or array target).
    """
    if not impl.impl_type_ref:
        ctx.skip_log.record(
            impl.path,
            "IMPLEMENTATION-DATA-TYPE",
            "TYPE_REFERENCE category impl type has no IMPLEMENTATION-DATA-TYPE-REF",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return None
    # A conflicting target still has its name claimed, by the loop over the
    # declared implementation types rather than by this call.
    target_name = _implementation_member(
        impl,
        impl.impl_type_ref,
        "implementation_types.reference_type.base",
        "",
        "base implementation type",
        f"TYPE_REFERENCE names the SW-BASE-TYPE {impl.impl_type_ref}; a reference type rests on an "
        "implementation data type, never on a base type",
        ctx,
        visiting,
    )
    if target_name is None:
        return None
    name = _register_impl(impl, ctx)
    ctx.data_types.append(
        ReferenceDataType(
            name=name,
            kind="reference_type",
            base=target_name,
            compu_method_ref=_implementation_compu_method(impl, ctx),
            description=impl.description,
        )
    )
    if ctx.unresolved is not None:
        ctx.unresolved.commit(impl.path)
    return name


def _resolve_composite_impl(impl: ImplDataTypeInfo, ctx: ImplResolution, visiting: set[str]) -> str | None:
    """A STRUCTURE, fixed-size ARRAY or DATA_REFERENCE implementation type, in the one shape the model holds.

    Every other shape is skipped with a sentence naming what is unsupported,
    never normalised into the supported one. Each member reference is resolved
    recursively first.
    """
    shape = _unsupported_shape(impl)
    if shape is not None:
        ctx.skip_log.record_reason(impl.path, "IMPLEMENTATION-DATA-TYPE", shape, effect="abort")
        return None
    members: list[str] = []
    for member_ref, member_field, member_name, role in _composite_members(impl):
        member_type = _implementation_member(
            impl,
            member_ref,
            member_field,
            member_name,
            role,
            f"{role} names the SW-BASE-TYPE {member_ref}; it names an implementation data type, never a base type",
            ctx,
            visiting,
        )
        if member_type is None:
            return None
        members.append(member_type)
    name = _register_impl(impl, ctx)
    ctx.data_types.append(_composite_data_type(impl, name, members))
    if ctx.unresolved is not None:
        ctx.unresolved.commit(impl.path)
    return name


def _implementation_member(
    impl: ImplDataTypeInfo,
    ref: str,
    field_id: str,
    member: str,
    role: str,
    base_type_refusal: str,
    ctx: ImplResolution,
    visiting: set[str],
) -> str | None:
    """The name one reference of *impl* is written with, or ``None`` with *impl*'s skip already recorded.

    The reference is a reference type's base, a composite's member or a pointer's target. A ``SW-BASE-TYPE`` it
    names refuses *impl* with *base_type_refusal*; an unresolved target is kept as the export's own path in the role
    *field_id*; otherwise the target is resolved recursively, and a failure refuses *impl* as
    ``unresolved reference REF (ROLE)``. The unresolved-reference policy is asked before the recursion, which would
    record a second skip entry for the path.
    """
    if ref in ctx.sw_base_by_path:
        # An IMPLEMENTATION-DATA-TYPE-REF is `SwDataDefProps.implementationDataType`,
        # whose destination type is an `ImplementationDataType` (SWCT R4.2.2
        # :14768, R4.3.0 :15882), which a SW-BASE-TYPE is not: the file breaks
        # `[TPS_GST_00020]`'s destination type, in every one of the roles.
        ctx.skip_log.record(
            impl.path,
            "IMPLEMENTATION-DATA-TYPE",
            base_type_refusal,
            category=SkipCategory.EXPORT_DEFECT,
            rule=StandardRule.REFERENCE_DESTINATION_TYPE,
            effect="abort",
        )
        return None
    if ctx.unresolved is not None:
        kept = ctx.unresolved.keep(
            ref,
            UnresolvedReferrer(
                path=impl.path,
                tag="IMPLEMENTATION-DATA-TYPE",
                attribute="IMPLEMENTATION-DATA-TYPE-REF",
                field=field_id,
                member=member,
            ),
        )
        if kept is not None:
            return kept
    resolved = resolve_impl_type(ref, ctx, visiting)
    if resolved is None:
        # A chain behind the member's own type inherits that type's entry.
        record_chained_to(
            ctx.skip_log,
            ctx.declared,
            impl.path,
            "IMPLEMENTATION-DATA-TYPE",
            f"unresolved reference {ref} ({role})",
            cause_path=cause_path(ref, ctx.merges),
            upstream=ctx.upstream,
        )
    return resolved


def cause_path(ref: str, merges: MergeDecisions | None) -> str:
    """The path a chained type entry's root is recorded at: *ref*, or its merged group's primary."""
    return merges.primary_path(ref) if merges is not None else ref


#: The composite categories, each imported in the one shape the model holds
#: (:func:`_unsupported_shape`).
_COMPOSITE_CATEGORIES = frozenset({"STRUCTURE", "ARRAY", "DATA_REFERENCE"})

#: Every category :func:`resolve_impl_type` maps. ``UNION`` and
#: ``FUNCTION_REFERENCE`` are not imported.
IMPORTED_IMPLEMENTATION_CATEGORIES = frozenset({"VALUE", "TYPE_REFERENCE"}) | _COMPOSITE_CATEGORIES


def _unsupported_shape(impl: ImplDataTypeInfo) -> SkipReason | None:
    """Why *impl*, a ``STRUCTURE``, ``ARRAY`` or ``DATA_REFERENCE``, is not a shape the model holds -- or ``None``.

    The supported shapes, and no other:

    * a ``STRUCTURE`` whose every sub-element is a depth-one ``TYPE_REFERENCE``
      to a named implementation type -- an ``implementation_record``;
    * an ``ARRAY`` of exactly one such sub-element carrying a positive
      ``ARRAY-SIZE``, ``ARRAY-SIZE-SEMANTICS`` ``FIXED-SIZE`` and no
      ``ARRAY-SIZE-HANDLING`` -- an ``implementation_array``;
    * a ``DATA_REFERENCE`` whose ``SW-POINTER-TARGET-PROPS`` carry
      ``TARGET-CATEGORY`` ``TYPE_REFERENCE`` and an ``IMPLEMENTATION-DATA-TYPE-REF``
      and nothing else -- a ``pointer``.

    Nothing is normalised into a supported shape. Not reached: a qualifier the
    reader does not carry on a sub-element's own conditional (a ``COMPU-METHOD-REF``
    or ``DATA-CONSTR-REF`` there) is not seen, and so not reported.

    Every reason is :attr:`SkipCategory.NOT_SUPPORTED` except a sub-element with a
    missing or blank ``SHORT-NAME``, which the file breaks ``[TPS_GST_00057]`` with
    (:attr:`StandardRule.SHORT_NAME_REQUIRED`).
    """
    unsupported = "this release does not import it"
    if impl.category == "DATA_REFERENCE":
        if impl.pointer_target_nested:
            return not_supported(
                "DATA_REFERENCE is an inline pointer to a pointer (its SW-POINTER-TARGET-PROPS nest another); "
                f"a pointer to a pointer is imported through a named pointer type only, and {unsupported}"
            )
        if impl.pointer_target_base_type_ref is not None:
            return not_supported(
                "DATA_REFERENCE targets a SW-BASE-TYPE directly (a void or inline VALUE target); "
                f"a pointer's target is a named implementation data type, and {unsupported}"
            )
        if impl.pointer_target_category != "TYPE_REFERENCE":
            return not_supported(
                f"DATA_REFERENCE has TARGET-CATEGORY '{impl.pointer_target_category}'; a pointer's target is a "
                f"TYPE_REFERENCE to a named implementation data type, and {unsupported}"
            )
        if not impl.pointer_target_ref:
            return not_supported(f"DATA_REFERENCE declares no target IMPLEMENTATION-DATA-TYPE-REF, and {unsupported}")
        if impl.pointer_target_qualifiers:
            return not_supported(
                "DATA_REFERENCE's SW-POINTER-TARGET-PROPS carry "
                + ", ".join(impl.pointer_target_qualifiers)
                + f", which a pointer does not hold, and {unsupported}"
            )
        return None
    if not impl.sub_elements:
        return not_supported(f"{impl.category} declares no SUB-ELEMENTS, and {unsupported}")
    for element in impl.sub_elements:
        subject = f"{impl.category} sub-element '{element.name}'"
        if not (element.name or "").strip():
            return blank_short_name(
                f"{impl.category} has a sub-element with missing or blank SHORT-NAME, and {unsupported}"
            )
        if element.nested_sub_elements or element.category in {"STRUCTURE", "ARRAY", "UNION"}:
            return not_supported(
                f"{subject} is a nested inline {element.category} member; a sub-element is imported as a "
                f"TYPE_REFERENCE to a named implementation data type only, and {unsupported}"
            )
        if element.category == "VALUE" or element.base_type_ref is not None:
            return not_supported(
                f"{subject} is an inline VALUE member (a BASE-TYPE-REF of its own); a sub-element is imported as "
                f"a TYPE_REFERENCE to a named implementation data type only, and {unsupported}"
            )
        if element.category != "TYPE_REFERENCE":
            return not_supported(
                f"{subject} has CATEGORY '{element.category}'; a sub-element is imported as a TYPE_REFERENCE to a "
                f"named implementation data type only, and {unsupported}"
            )
        if not element.impl_type_ref:
            return not_supported(f"{subject} declares no IMPLEMENTATION-DATA-TYPE-REF, and {unsupported}")
    if impl.category == "STRUCTURE":
        names = [element.name for element in impl.sub_elements]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            return not_supported(
                f"STRUCTURE declares the sub-element SHORT-NAME(s) {duplicates} more than once, and {unsupported}"
            )
        return None
    if len(impl.sub_elements) != 1:
        return not_supported(
            f"ARRAY declares {len(impl.sub_elements)} sub-elements where an array has exactly one, and {unsupported}"
        )
    (element,) = impl.sub_elements
    if element.array_size_handling is not None:
        return not_supported(
            f"ARRAY carries ARRAY-SIZE-HANDLING '{element.array_size_handling}'; size handling belongs to a "
            f"variable-size array, and {unsupported}"
        )
    if element.array_size_semantics is None:
        return not_supported(
            f"ARRAY declares no ARRAY-SIZE-SEMANTICS, so it is not stated FIXED-SIZE, and {unsupported}"
        )
    if element.array_size_semantics != "FIXED-SIZE":
        return not_supported(
            f"ARRAY is {element.array_size_semantics}; an implementation array is FIXED-SIZE only, and {unsupported}"
        )
    if element.array_size is None or element.array_size <= 0:
        return not_supported(f"ARRAY declares no positive ARRAY-SIZE, and {unsupported}")
    return None


def _composite_members(impl: ImplDataTypeInfo) -> list[tuple[str, str, str, str]]:
    """``(reference, SWCraft field, member name, role)`` per type a supported composite names, in order.

    Asked only after :func:`_unsupported_shape` answered ``None``. The member name
    is empty for a pointer's target.
    """
    if impl.category == "DATA_REFERENCE":
        return [
            (impl.pointer_target_ref or "", "implementation_types.pointer.target", "", "pointer target was skipped")
        ]
    if impl.category == "ARRAY":
        (element,) = impl.sub_elements
        return [
            (
                element.impl_type_ref or "",
                "implementation_types.implementation_array.element_type",
                element.name,
                f"array element '{element.name}' type was skipped",
            )
        ]
    return [
        (
            element.impl_type_ref or "",
            "implementation_types.implementation_element.type",
            element.name,
            f"element '{element.name}' type was skipped",
        )
        for element in impl.sub_elements
    ]


def _composite_data_type(impl: ImplDataTypeInfo, name: str, members: list[str]) -> DataType:
    """The ``implementation_record``, ``implementation_array`` or ``pointer`` a supported *impl* becomes.

    *members* are the emitted names of :func:`_composite_members`' references,
    in the same order; element names, their order and ``element_name`` are the
    source's own, never invented.
    """
    if impl.category == "DATA_REFERENCE":
        return PointerDataType(name=name, kind="pointer", target=members[0], description=impl.description)
    if impl.category == "ARRAY":
        (element,) = impl.sub_elements
        assert element.array_size is not None  # `_unsupported_shape` refused every other case
        return ImplementationArrayDataType(
            name=name,
            kind="implementation_array",
            element_type=members[0],
            length=element.array_size,
            element_name=element.name,
            description=impl.description,
        )
    return ImplementationRecordDataType(
        name=name,
        kind="implementation_record",
        elements=[
            ImplementationElement(name=element.name, type=member, description=element.description)
            for element, member in zip(impl.sub_elements, members, strict=True)
        ],
        description=impl.description,
    )
