"""The merge verdict: every declaration kind's comparison signature, and the decision over each group.

:mod:`~swcraft_tools.arxml_import.merge` builds the groups -- the declarations sharing one
``(layer, kind, SHORT-NAME)`` -- and the reference chains the comparison reads. This module decides each
group: it signs every declaration of a group with the row of its kind (:func:`_signature`), compares the
rows, and returns :class:`~swcraft_tools.arxml_import.merge.MergeDecisions` from :func:`decide_merges`.
What the comparison reads and what it leaves out is stated once, in
:mod:`~swcraft_tools.arxml_import.merge`'s module docstring. ``merge`` uses nothing of this module.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from swcraft_tools.arxml_import.merge import (
    COM_SPEC_KIND_FIELDS,
    CONFLICTING,
    FILTER_COM_SPEC_KIND,
    LAYER_BY_TAG,
    MERGED,
    GroupKey,
    MergeDecisions,
    MergeGroup,
    carried_interval,
    com_spec_values,
    compared_mapping_targets,
    declared_paths,
    inline_forms,
    read_com_spec_children,
    silent_values_without_place,
    stripped_text,
    unread_com_spec_children,
)
from swcraft_tools.arxml_reader import (
    OTHER_LIMIT,
    AppArrayDataTypeInfo,
    AppPrimitiveDataTypeInfo,
    AppRecordDataTypeInfo,
    AssemblyConnectorInfo,
    CompositionInfo,
    CompuMethodInfo,
    ComSpecInfo,
    ConnectorPortRef,
    ConstantArray,
    ConstantRecord,
    ConstantScalar,
    ConstantSpecInfo,
    ConstantValue,
    CSInterfaceInfo,
    DataConstrInfo,
    DataElementComSpecInfo,
    DataTypeMappingSetInfo,
    ImplDataTypeInfo,
    ImportModel,
    PhysLimit,
    SkipCategory,
    SkipLog,
    SRInterfaceInfo,
    SwBaseTypeInfo,
    SwcEventInfo,
    SwcInfo,
    SwcPortInfo,
    SwcRunnableInfo,
    UnitInfo,
    com_spec_block,
    exact_decimal,
    is_infinite_limit,
    parse_exact_number,
    parse_int_literal,
    parse_xsd_boolean,
    phys_limits,
)


def _prefixes(path: str) -> Iterator[str]:
    """``/a/b/c`` -> ``/a/b/c``, ``/a/b``, ``/a`` -- longest first."""
    current = path
    while current:
        yield current
        head, slash, _ = current.rpartition("/")
        if not slash:
            return
        current = head


class _Token:
    """A reference path as the comparison reads it, and what it reached.

    A reference is compared **resolved**: a path whose longest declaration-path
    prefix is a member of a multi-member group is rewritten to that group's key
    plus whatever sits below it, so a reference into a duplicated declaration
    resolves to the group and not to one copy. Every other path is its own
    token, unresolved ones included: an absent target is compared as the export
    spelled it, which is what the import writes into the model for it.

    Every call records the group it reached, so the reference graph is read off
    the same enumeration the comparison uses.
    """

    def __init__(self, group_by_path: dict[str, MergeGroup], declaration_paths: frozenset[str]) -> None:
        self._groups = group_by_path
        self._declarations = declaration_paths
        self._cache: dict[str, tuple[str, GroupKey | None, bool]] = {}
        self.reached: set[GroupKey] = set()
        self.reached_whole: set[GroupKey] = set()
        """The groups reached by a reference naming a member's **own** path.

        Kept apart from :attr:`reached`: a reference *inside* a declaration (a
        connector naming its own composition's prototype) is content, while one
        to the declaration *itself* (a composition placing its own type) is a
        cycle of length one."""

    def forget_what_was_reached(self) -> None:
        """Start a fresh group's edge collection.

        The two ``reached`` sets are per group; the cache is not, because a
        path's resolution does not depend on the group being signed.
        """
        self.reached = set()
        self.reached_whole = set()

    def __call__(self, path: str | None) -> str | None:
        if path is None:
            return None
        resolved = self._cache.get(path)
        if resolved is None:
            resolved = self._resolve(path)
            self._cache[path] = resolved
        token, key, whole = resolved
        if key is not None:
            self.reached.add(key)
            if whole:
                self.reached_whole.add(key)
        return token

    def _resolve(self, path: str) -> tuple[str, GroupKey | None, bool]:
        for prefix in _prefixes(path):
            group = self._groups.get(prefix)
            if group is not None:
                return f"{group.key}{path[len(prefix) :]}", group.key, prefix == path
            if prefix in self._declarations:
                return path, None, False
        return path, None, False

    def reaches_a_declaration(self, path: str) -> bool:
        """Whether *path* names, or sits inside, something this import can map.

        True for a member of a multi-member group and for a declaration of a
        kind in :data:`LAYER_BY_TAG`; false both for a target the export does
        not declare and for one it declares under a kind the importer never
        reads (``MODE-SWITCH-INTERFACE``, ``TRIGGER-INTERFACE``,
        ``PARAMETER-INTERFACE``, ``NV-DATA-INTERFACE``). A caller that must tell
        those two apart asks :func:`declared_paths` as well: the first is
        written into the model as a path, the second is dropped with its port.

        Records nothing.
        """
        return any(prefix in self._groups or prefix in self._declarations for prefix in _prefixes(path))


@dataclass
class _Declaration:
    """One declaration that takes a verdict, with what identifies it."""

    path: str
    tag: str
    name: str
    description: str
    info: Any
    #: The owning composition's path, for an assembly connector; ``None`` for
    #: every package-level declaration.
    owner: str | None = None


def _declarations(model: ImportModel, inline: Collection[str]) -> list[_Declaration]:
    """Every declaration of a kind in :data:`LAYER_BY_TAG`, in reader order.

    A blank or missing ``SHORT-NAME`` takes no verdict: the mapping layer skips
    it with its own reason. Nor does a form in *inline* (:func:`inline_forms`):
    SWCraft's own private form is read into its type's field and is no element.
    """
    found: list[_Declaration] = []

    def add(tag: str, infos: Iterable[Any], *, owner: str | None = None) -> None:
        for info in infos:
            if not stripped_text(getattr(info, "name", "")) or info.path in inline:
                continue
            found.append(
                _Declaration(
                    path=info.path,
                    tag=tag,
                    name=info.name,
                    description=getattr(info, "description", "") or "",
                    info=info,
                    owner=owner,
                )
            )

    add("SW-BASE-TYPE", model.sw_base_types)
    add("IMPLEMENTATION-DATA-TYPE", model.impl_data_types)
    add("APPLICATION-PRIMITIVE-DATA-TYPE", model.app_primitive_data_types)
    add("APPLICATION-RECORD-DATA-TYPE", model.app_record_data_types)
    add("APPLICATION-ARRAY-DATA-TYPE", model.app_array_data_types)
    add("SENDER-RECEIVER-INTERFACE", model.sr_interfaces)
    add("CLIENT-SERVER-INTERFACE", model.cs_interfaces)
    add("APPLICATION-SW-COMPONENT-TYPE", model.swcs)
    add("COMPOSITION-SW-COMPONENT-TYPE", model.compositions)
    for composition in model.compositions:
        add("ASSEMBLY-SW-CONNECTOR", composition.assembly_connectors, owner=composition.path)
    add("DATA-TYPE-MAPPING-SET", model.data_type_mapping_sets)
    add("UNIT", model.units)
    add("COMPU-METHOD", model.compu_methods)
    add("DATA-CONSTR", model.data_constrs)
    return found


@dataclass(frozen=True)
class _Context:
    """What one comparison needs besides the declaration itself.

    :attr:`constants_by_path`: a constant specification is not grouped, so its
    value is **inlined**. :attr:`compu_by_path`, :attr:`unit_by_path` and
    :attr:`constr_by_path` are read for the forms in :attr:`inline` and for a
    value type's bounds; every other form is compared by its resolved reference.
    :attr:`effective_target_by_app_path`: the ``DATA-TYPE-MAP`` entry a bare
    application primitive collapses onto (:func:`compared_mapping_targets`).
    :attr:`declared`: what the export declares (:func:`declared_paths`).
    """

    token: _Token
    declared: frozenset[str]
    compu_by_path: dict[str, CompuMethodInfo]
    constants_by_path: dict[str, ConstantSpecInfo]
    effective_target_by_app_path: dict[str, tuple[str, ...]] = field(default_factory=dict)
    unit_by_path: dict[str, UnitInfo] = field(default_factory=dict)
    constr_by_path: dict[str, DataConstrInfo] = field(default_factory=dict)
    #: :func:`inline_forms` of the model: the forms a type's row compares by content.
    inline: frozenset[str] = frozenset()


def _value_key(value: ConstantValue) -> Any:
    """One read constant value as the comparison reads it: **what is written**.

    The key leaves out
    :attr:`~swcraft_tools.arxml_reader.ConstantScalar.radix_source`, the raw
    spelling the report uses to say a radix was normalized: ``0x0A`` and ``10``
    are one value, and a documentation difference may not drop two elements.
    It names the written members only -- a scalar's ``value``, an array's
    members in order, a record's fields by ``label`` (a declared
    ``SHORT-LABEL``) and value -- recursively.

    A text leaf (a ``VT`` or a text value) is signed as a text, so ``"10"`` is
    not ``10``. The leaf's form (numerical or application, ``VT`` or text value)
    is not signed, nor is a value's ``UNIT-REF``: the unit is its type's, which
    is compared on its own row.

    **What this does not reach:** it normalizes the radix, not the value, and
    inherits Python's numeric equality: ``1`` and ``1.0`` are one key although
    the writer would write two.
    """
    if isinstance(value, ConstantScalar):
        if isinstance(value.value, str):
            return ("text", value.value)
        return ("scalar", value.value)
    if isinstance(value, ConstantArray):
        return ("array", tuple(_value_key(member) for member in value.members))
    if isinstance(value, ConstantRecord):
        return ("record", tuple((field_.label, _value_key(field_.value)) for field_ in value.fields))
    # A branch rather than an assert, so `python -O` cannot silence a new value kind.
    return ("unknown", repr(value))  # pragma: no cover


def _constant_signature(ref: str | None, ctx: _Context) -> Any:
    """A ``CONSTANT-SPECIFICATION`` reference as its **value**, inlined.

    The value is what is written, not the path: two copies of one cluster carry
    two constants of one name in two packages. A constant the file does not
    declare, or one the reader could not read, falls back to the reference's
    token beside the reason -- the fail-closed answer.
    """
    if ref is None:
        return None
    constant = ctx.constants_by_path.get(ref)
    if constant is None:
        return ("unresolved", ctx.token(ref))
    if constant.unreadable_reason is not None:
        return ("unreadable", ctx.token(ref), constant.unreadable_reason)
    if constant.value is None:
        # Unreachable (`ConstantSpecInfo` sets exactly one of the two); fail closed.
        return ("valueless", ctx.token(ref))
    return ("value", _value_key(constant.value))


def _port_interface_signature(ref: str | None, ctx: _Context) -> Any:
    """A port's ``*-INTERFACE-TREF`` as the comparison reads it.

    Normally the resolved token. The exception is a path this export
    **declares** under a kind the import never maps (``MODE-SWITCH-INTERFACE``,
    ``TRIGGER-INTERFACE``, ``PARAMETER-INTERFACE``, ``NV-DATA-INTERFACE``): two
    copies of one cluster name two paths that differ in their package prefix by
    construction, and the port is skipped either way, so the last segment stands
    for the path, beside a marker that keeps it apart from any resolvable token.
    Two such references still conflict when their short names differ.

    A target the export declares **nowhere** takes the full path: the import
    keeps the port with that path in ``Port.interface``. Scoped to a port's
    interface reference: elsewhere an unresolved path can be an element's own
    content (a stub's ``SWComponent.external_ref``).
    """
    if ref is None:
        return None
    if ctx.token.reaches_a_declaration(ref):
        return ctx.token(ref)
    if ref in ctx.declared:
        return ("unmappable-interface", ref.rsplit("/", 1)[-1])
    return ref


def _names_a_member_of(ref: str | None, port: SwcPortInfo) -> bool:
    """Whether *ref*'s parent is *port*'s own interface path, compared raw, as the component mapper's gate compares it.

    The mapper refuses a com-spec whose reference does not sit under the port's
    own ``INTERFACE-TREF`` spelling, which the resolved token alone cannot tell.
    """
    parent, slash, _member = (ref or "").rpartition("/")
    return bool(slash) and parent == port.interface_ref


def _com_spec_signature(port: SwcPortInfo, spec: DataElementComSpecInfo, ctx: _Context) -> tuple[Any, ...]:
    """One sender/receiver com-spec as the component mapper judges and carries it.

    Its values are signed as :func:`com_spec_values` reads them: a value equal
    to the writer's silent one is no value, while a carried value, a text the
    model cannot hold and, on the non-queued receiver, a filter other than a
    bare ``ALWAYS`` each count. Beside them, every fact the mapper's gate
    decides from: the block, the kind, the resolved reference, whether it sits
    under the port's own interface path (:func:`_names_a_member_of`) and its
    ordinal among the com-specs naming its element (:func:`_com_spec_ordinal`),
    never its raw position. A com-spec stating nothing is signed too: its
    presence decides which com-spec is an element's first.
    """
    block = com_spec_block(spec.block_tag, spec.com_spec_tag)
    carried, uncarried = com_spec_values(spec)
    return (
        block,
        spec.com_spec_tag,
        ctx.token(spec.data_element_ref),
        _names_a_member_of(spec.data_element_ref, port),
        _com_spec_ordinal(port, block, spec.data_element_ref, spec.position),
        tuple(sorted(carried.items())),
        tuple(sorted(uncarried.items())),
        # A bare ALWAYS filter is no filter on the non-queued receiver alone; on
        # another kind every FILTER is one unread child, signed by the shape.
        spec.filter_is_silent or spec.com_spec_tag != FILTER_COM_SPEC_KIND,
        # A silent value on a kind with no place for it is lost and reported.
        tuple(silent_values_without_place(spec)),
    )


def _com_spec_shape_signature(
    port: SwcPortInfo,
    shape: ComSpecInfo,
    ctx: _Context,
    operations_seen: dict[str | None, int],
    clients_seen: dict[str | None, int],
) -> tuple[Any, ...]:
    """One com-spec of any kind as the mapper's loss report judges it (``swc_mapping._com_spec_losses``).

    The block, the kind, which com-spec it is, every child the importer does not
    read by value and every attribute. Children and attributes are signed
    **sorted**: the report names each by its tag or name alone, and an
    attribute's order is no fact of XML.

    Which com-spec it is: a sender/receiver com-spec's resolved reference,
    whether it sits under the port's own interface path and its ordinal. A
    ``SERVER-COM-SPEC``'s operation resolved, the same path check, how many
    server com-specs naming that operation precede it (*operations_seen*, one
    per port, updated here) and its queue length. A ``CLIENT-COM-SPEC``'s the
    same way without a queue length (*clients_seen*): a ``ClientComSpec`` is
    defined by the operation it references (``ClientComSpec.operation``,
    multiplicity 1 -- SWCT R4.3.0:8891-8894, Table
    4.75; R4.2.2:8353-8356, Table 4.73), so two declarations whose client
    com-specs differ only in the operation are two components.
    """
    read = (
        read_com_spec_children(shape.tag)
        if shape.data is not None or shape.server is not None or shape.tag == "CLIENT-COM-SPEC"
        else frozenset()
    )
    data: tuple[Any, ...] | None = None
    if shape.data is not None:
        ref = shape.data.data_element_ref
        block = com_spec_block(shape.data.block_tag, shape.data.com_spec_tag)
        data = (
            ctx.token(ref),
            _names_a_member_of(ref, port),
            _com_spec_ordinal(port, block, ref, shape.data.position),
        )
    server: tuple[Any, ...] | None = None
    if shape.server is not None:
        ref = shape.server.operation_ref
        server = (
            ctx.token(ref),
            _names_a_member_of(ref, port),
            operations_seen.get(ref, 0),
            _server_queue_length_signature(shape.server.queue_length),
        )
        operations_seen[ref] = operations_seen.get(ref, 0) + 1
    client: tuple[Any, ...] | None = None
    if shape.client is not None:
        ref = shape.client.operation_ref
        client = (ctx.token(ref), _names_a_member_of(ref, port), clients_seen.get(ref, 0))
        clients_seen[ref] = clients_seen.get(ref, 0) + 1
    return (
        com_spec_block(shape.block_tag, shape.tag),
        shape.tag,
        data,
        tuple(sorted(unread_com_spec_children(shape, read))),
        tuple(sorted(shape.attributes)),
        server,
        client,
    )


def _server_queue_length_signature(length: str | None) -> int | str | None:
    """A ``SERVER-COM-SPEC``'s ``QUEUE-LENGTH`` as the report judges it.

    ``None`` when absent or 1 (both are written as 1); otherwise the integer it
    reads as, so ``2`` and ``0x2`` are one value; the text as spelled when it
    reads as no integer, fail-closed.
    """
    if length is None:
        return None
    value = parse_int_literal(length, grammar="positive-integer")
    if value is None:
        return length
    return None if value == 1 else value


def _com_spec_ordinal(port: SwcPortInfo, block: str, data_element_ref: str | None, position: int) -> int:
    """How many sender/receiver com-specs in *block* naming *data_element_ref* precede *position*.

    ``0`` for the first, the one the mapper's gate passes and whose value alone
    it can carry. The reference is compared raw, as the gate compares it.
    """
    return sum(
        1
        for spec in port.data_element_com_specs
        if com_spec_block(spec.block_tag, spec.com_spec_tag) == block
        and spec.data_element_ref == data_element_ref
        and spec.position < position
    )


def _port_signature(port: SwcPortInfo, ctx: _Context, *, carries_com_specs: bool) -> tuple[Any, ...]:
    """One port: name, direction, **resolved** interface, and its com-specs.

    A fact of a port's com-specs is signed exactly as far as the mapper's model
    output or its report lines depend on it -- no further, because a conflict refuses the
    import of both declarations, and no less, because the mapper judges the
    primary alone and a fact it would carry or name differently on another
    declaration is then fitted or lost with no line. So:

    * **in order** -- the sender/receiver com-specs that may write a
      ``com_specs`` entry (on a component's port, *carries_com_specs*): entries
      keep source order, so two orders are two models;
    * **as a multiset**, each member naming which com-spec it is (block, kind,
      resolved reference, and :func:`_com_spec_ordinal`) -- every other
      sender/receiver com-spec, every init and every com-spec's shape
      (:func:`_com_spec_shape_signature`);
    * **by value** -- what is carried as a value (:func:`com_spec_values`, an
      init's constant inlined, a queue length as its integer); as spelled only
      what no reading makes one value;
    * **by tag or name, sorted** -- every unread child
      (:func:`unread_com_spec_children`) and every attribute.

    What this does not reach, signed fail-closed: an unread child is signed
    under the com-spec that holds it, so one child on two different com-specs
    conflicts even where the report lines agree; and a com-spec whose only fact
    is an ``INIT-VALUE`` naming its element's own constant is still signed in
    order, because the comparison does not hold the element's constant."""
    operations_seen: dict[str | None, int] = {}
    clients_seen: dict[str | None, int] = {}
    inits_at = {(com_spec_block(init.block_tag, init.com_spec_tag), init.position) for init in port.com_spec_inits}
    ordered: list[tuple[Any, ...]] = []
    unordered: list[tuple[Any, ...]] = []
    for spec in port.data_element_com_specs:
        block = com_spec_block(spec.block_tag, spec.com_spec_tag)
        carried, _uncarried = com_spec_values(spec)
        fields = COM_SPEC_KIND_FIELDS.get(spec.com_spec_tag, frozenset())
        writes_an_entry = carries_com_specs and (
            any(name in fields for name in carried) or (block, spec.position) in inits_at
        )
        (ordered if writes_an_entry else unordered).append(_com_spec_signature(port, spec, ctx))
    return (
        port.name,
        port.kind,
        _port_interface_signature(port.interface_ref, ctx),
        tuple(
            sorted(
                (
                    (
                        com_spec_block(com_spec.block_tag, com_spec.com_spec_tag),
                        com_spec.com_spec_tag,
                        ctx.token(com_spec.data_element_ref),
                        _com_spec_ordinal(
                            port,
                            com_spec_block(com_spec.block_tag, com_spec.com_spec_tag),
                            com_spec.data_element_ref,
                            com_spec.position,
                        ),
                        _constant_signature(com_spec.constant_ref, ctx),
                        None if com_spec.value is None else ("in place", _value_key(com_spec.value)),
                        com_spec.unreadable_reason,
                    )
                    for com_spec in port.com_spec_inits
                ),
                key=repr,
            )
        ),
        tuple(ordered),
        tuple(sorted(unordered, key=repr)),
        tuple(
            sorted(
                (
                    _com_spec_shape_signature(port, shape, ctx, operations_seen, clients_seen)
                    for shape in port.com_spec_shapes
                ),
                key=repr,
            )
        ),
    )


def _is_service_signature(text: str | None) -> tuple[bool, str | None]:
    """An interface's ``IS-SERVICE`` as it is written.

    ``false``/``0`` and an absent one write the same file, so they are one
    value; an unreadable text, which drops its interface with a line, is kept
    apart, fail-closed.
    """
    value = parse_xsd_boolean(text)
    return (value is True, text if text is not None and value is None else None)


def _endpoint_signature(ref: ConnectorPortRef, ctx: _Context) -> tuple[Any, ...]:
    """One connector end: its two references, the target's ``DEST`` and the direction its element states.

    An end on an external component stub mints the stub's port from the last
    two, so two declarations differing only there declare two different ports.
    """
    return (ctx.token(ref.component_ref), ctx.token(ref.port_ref), ref.dest, ref.stated_direction)


def _compu_signature(info: AppPrimitiveDataTypeInfo, ctx: _Context) -> Any:
    """An application primitive type's backing ``COMPU-METHOD``, **inlined**.

    Asked for SWCraft's own private form alone; every other form is an element
    and the row compares the reference to it (:func:`_signature`). The category,
    the text-table scales in order, the rational scales and the ``COMPU-SCALE``
    count, which decides whether a conversion is imported at all (a method of
    several scales is imported without one). A compu method the file does not
    declare falls back to the reference's token, fail-closed.
    """
    if info.compu_method_ref is None:
        return None
    compu = ctx.compu_by_path.get(info.compu_method_ref)
    if compu is None:
        return ("unresolved", ctx.token(info.compu_method_ref))
    return (
        compu.category,
        tuple((scale.value, scale.literal) for scale in compu.scales),
        tuple(
            (
                scale.numerator,
                scale.denominator,
                # A private form's limits are carried inline. Compared by value, the
                # interval as carried (absent is CLOSED), so `16`, `0x10` and `16.0` are one.
                _limit_signature(scale.lower_limit, carried_interval(scale.lower_interval), "LOWER-LIMIT"),
                _limit_signature(scale.upper_limit, carried_interval(scale.upper_interval), "UPPER-LIMIT"),
            )
            for scale in compu.rational_scales
        ),
        compu.scale_count,
    )


def _unit_signature(info: AppPrimitiveDataTypeInfo, ctx: _Context) -> Any:
    """The ``UNIT`` an application primitive names, **inlined** as the text the model comes to hold.

    Exactly, surrounding whitespace included (:func:`carried_unit_text`). Asked
    for SWCraft's own private form alone; every other form is an element and
    the row compares the reference to it (:func:`_signature`). A reference the
    export does not declare falls back to the reference's token, fail-closed.
    """
    if info.unit_ref is None:
        return None
    unit = ctx.unit_by_path.get(info.unit_ref)
    if unit is None:
        return ("unresolved", ctx.token(info.unit_ref))
    return carried_unit_text(unit)


def carried_unit_text(unit: UnitInfo) -> str:
    """The ``unit`` an application type naming *unit* comes to hold.

    The ``DISPLAY-NAME`` exactly as written, surrounding whitespace included;
    the ``SHORT-NAME`` where the element declares none; and ``""`` -- no unit --
    for a ``DISPLAY-NAME`` of blanks alone, as the writer emits no ``UNIT`` for
    a ``unit`` of spaces.
    """
    if unit.display_name is None:
        return unit.name
    return unit.display_name if unit.display_name.strip() else ""


def _data_constr_signature(info: AppPrimitiveDataTypeInfo, ctx: _Context) -> Any:
    """The ``DATA-CONSTR`` an application primitive names, **inlined** as the range the model comes to hold.

    Asked for SWCraft's own private form alone, as :func:`_unit_signature` is.
    The two physical limits, and the rule count: a constraint of more than one
    ``DATA-CONSTR-RULE`` is reported and its range not carried.
    """
    if info.data_constr_ref is None:
        return None
    constr = ctx.constr_by_path.get(info.data_constr_ref)
    if constr is None:
        return ("unresolved", ctx.token(info.data_constr_ref))
    return (
        _phys_limit_signature(phys_limits(constr).lower_limit),
        _phys_limit_signature(phys_limits(constr).upper_limit),
        constr.rule_count,
    )


def _limit_signature(text: str | None, interval: str | None, side: str, *, infinite_bounds: bool = True) -> Any:
    """One limit as the source states it, compared by **value**: its exact number and its interval.

    The text's exact decimal, so ``0x10``, ``16`` and ``16.0`` are one limit and two limits differing
    anywhere (above 2**53 included) are two. Taken from the source text, never from the carried bound,
    because an uncarried bound is ``None`` on both sides. A text that is no finite number, or one no
    model value holds exactly, is compared as spelled. An ``INFINITE`` limit and *side*'s own infinity
    are the absent limit's ``None`` -- unless *infinite_bounds* is false, when ``INFINITE`` over a text
    is compared as spelled (:func:`_phys_limit_signature`); the other side's infinity is always
    compared as spelled.
    """
    if text is None:
        return None
    misplaced = is_infinite_limit(text, OTHER_LIMIT[side])
    if not misplaced and ((interval == "INFINITE" and infinite_bounds) or is_infinite_limit(text, side)):
        return None
    number = exact_decimal(text, grammar="formula")
    held = number is not None and not parse_exact_number(text, grammar="formula")[1]
    return (number if held else ("text", text), interval)


def _value_constr_signature(info: ImplDataTypeInfo, ctx: _Context) -> Any:
    """The ``DATA-CONSTR`` a ``VALUE`` implementation type names, **inlined** as the bounds the model comes to hold.

    The importer writes ``value_min`` / ``value_max`` from the constraint's
    ``INTERNAL-CONSTRS``, so the two limits are compared as the source states
    them (:func:`_limit_signature`), beside the rule count. A reference the file
    does not declare is compared by its token; every other category is ``None``.
    """
    if info.category != "VALUE" or info.data_constr_ref is None:
        return None
    constr = ctx.constr_by_path.get(info.data_constr_ref)
    if constr is None:
        return ("unresolved", ctx.token(info.data_constr_ref))
    return (
        _limit_signature(constr.internal_lower_text, constr.internal_lower_interval or "CLOSED", "LOWER-LIMIT"),
        _limit_signature(constr.internal_upper_text, constr.internal_upper_interval or "CLOSED", "UPPER-LIMIT"),
        constr.rule_count,
    )


def _mapping_signature(info: AppPrimitiveDataTypeInfo, ctx: _Context) -> Any:
    """The ``DATA-TYPE-MAP`` entry that decides what this application primitive **becomes**, resolved.

    A mapping entry decides one thing about a primitive: whether it collapses
    onto its same-name implementation twin and *becomes* that type. So the row
    is the twin entry where the declarations say the collapse can be taken
    (:func:`merge._mapping_chain`) and the empty tuple elsewhere: primitives the
    collapse does not reach are compared by the rest of the row. The token
    makes the entry an **edge** of the reference graph, so a group whose
    collapse target conflicts inherits that conflict.
    """
    return tuple(ctx.token(target) for target in ctx.effective_target_by_app_path.get(info.path, ()))


def _mapping_set_signature(info: DataTypeMappingSetInfo, ctx: _Context) -> Any:
    """A ``DATA-TYPE-MAPPING-SET``'s own equality row: its maps, in declaration order, each side resolved.

    Order is compared, the module's rule for every list, at the cost of an
    over-conflict on one set of maps in two orders.

    What it does **not** carry: whether either side is a kind the map may name,
    whether the target is an implementation composite of a shape the importer
    does not map, whether SWCraft's composite check accepts the pair, and
    whether the map survives a ``--packages`` narrowing -- all decided after the
    verdict, over the mapped model.
    """
    return tuple((ctx.token(entry.application_ref), ctx.token(entry.implementation_ref)) for entry in info.maps)


def _base_type_signature(info: SwBaseTypeInfo) -> tuple[tuple[str, Any], ...]:
    """The row of an ``SW-BASE-TYPE``: its five properties plus the category."""
    # `[constr_1220]`'s five properties plus the category. `BYTE-ORDER` and
    # `MEM-ALIGNMENT` are compared as spelled (stripped by the reader): parsing
    # would make an unreadable value indistinguishable from an absent one.
    return (
        ("BASE-TYPE-SIZE", info.size),
        # Stripped here, not by the reader: the model fields keep the text as
        # written, and a spelling other than the primary's is reported.
        ("BASE-TYPE-ENCODING", stripped_text(info.encoding)),
        ("NATIVE-DECLARATION", stripped_text(info.native_declaration)),
        ("BYTE-ORDER", info.byte_order),
        ("MEM-ALIGNMENT", info.mem_alignment),
        ("CATEGORY", info.category),
    )


def _implementation_type_signature(info: ImplDataTypeInfo, ctx: _Context) -> tuple[tuple[str, Any], ...]:
    """The row of an ``IMPLEMENTATION-DATA-TYPE``.

    Its category, both references, its sub-elements, its pointer target, its constraint and its compu method.
    """
    # Everything the reader carries for sub-elements and pointer targets is
    # compared, unsupported shapes included: two declarations differing only in
    # what neither can import still differ in what the import report says.
    return (
        ("CATEGORY", info.category),
        ("BASE-TYPE-REF", ctx.token(info.base_type_ref)),
        ("IMPLEMENTATION-DATA-TYPE-REF", ctx.token(info.impl_type_ref)),
        (
            "SUB-ELEMENTS",
            tuple(
                (
                    element.name,
                    element.category,
                    ctx.token(element.impl_type_ref),
                    ctx.token(element.base_type_ref),
                    element.array_size,
                    element.array_size_semantics,
                    element.array_size_handling,
                    element.nested_sub_elements,
                )
                for element in info.sub_elements
            ),
        ),
        (
            "SW-POINTER-TARGET-PROPS",
            (
                info.pointer_target_category,
                ctx.token(info.pointer_target_ref),
                ctx.token(info.pointer_target_base_type_ref),
                info.pointer_target_nested,
                info.pointer_target_qualifiers,
            ),
        ),
        ("DATA-CONSTR", _value_constr_signature(info, ctx)),
        ("COMPU-METHOD-REF", ctx.token(info.compu_method_ref)),
    )


def _application_primitive_signature(info: AppPrimitiveDataTypeInfo, ctx: _Context) -> tuple[tuple[str, Any], ...]:
    """The row of an application primitive.

    Its category, then each of its compu method, unit and constraint (resolved, or by content for SWCraft's own
    private form), then its mapping.
    """
    # Coarsest first, so a conflict line names the coarsest difference:
    # `CATEGORY`, then `COMPU-METHOD`, which decides the kind (enum or
    # application type). Each form is compared as the resolved reference, except
    # SWCraft's own private form read inline, whose content the type holds.
    return (
        ("CATEGORY", info.category),
        (
            "COMPU-METHOD",
            _compu_signature(info, ctx) if info.compu_method_ref in ctx.inline else ctx.token(info.compu_method_ref),
        ),
        ("UNIT", _unit_signature(info, ctx) if info.unit_ref in ctx.inline else ctx.token(info.unit_ref)),
        (
            "DATA-CONSTR",
            _data_constr_signature(info, ctx)
            if info.data_constr_ref in ctx.inline
            else ctx.token(info.data_constr_ref),
        ),
        ("DATA-TYPE-MAP", _mapping_signature(info, ctx)),
    )


def _unit_element_signature(info: UnitInfo, ctx: _Context) -> tuple[tuple[str, Any], ...]:
    """The row of a ``UNIT``: its display name and its ``SI-RELATION``."""
    return (
        ("DISPLAY-NAME", info.display_name),
        (
            "SI-RELATION",
            tuple(
                (tag, ctx.token(text) if tag == "PHYSICAL-DIMENSION-REF" else text) for tag, text in info.si_relation
            ),
        ),
    )


def _compu_method_element_signature(info: CompuMethodInfo, ctx: _Context) -> tuple[tuple[str, Any], ...]:
    """The row of a ``COMPU-METHOD``.

    Its category, unit, scales, rational coefficients, scale count and physical-to-internal text.
    """
    return (
        ("CATEGORY", info.category),
        ("UNIT-REF", ctx.token(info.unit_ref)),
        (
            "COMPU-SCALES",
            tuple(
                (scale.value, scale.literal, scale.upper_limit, scale.lower_interval, scale.upper_interval)
                for scale in info.scales
            ),
        ),
        (
            "COMPU-RATIONAL-COEFFS",
            tuple(
                (
                    scale.numerator,
                    scale.denominator,
                    _limit_signature(scale.lower_limit, carried_interval(scale.lower_interval), "LOWER-LIMIT"),
                    _limit_signature(scale.upper_limit, carried_interval(scale.upper_interval), "UPPER-LIMIT"),
                )
                for scale in info.rational_scales
            ),
        ),
        ("SCALE-COUNT", info.scale_count),
        ("COMPU-PHYS-TO-INTERNAL", info.phys_to_internal),
    )


def _data_constr_element_signature(info: DataConstrInfo) -> tuple[tuple[str, Any], ...]:
    """The row of a ``DATA-CONSTR``: its rule count, level, physical constraint and internal constraint."""
    return (
        ("RULES", info.rule_count),
        ("CONSTR-LEVEL", info.level_text),
        (
            "PHYS-CONSTRS",
            (
                info.phys_present,
                _phys_limit_signature(phys_limits(info).lower_limit),
                _phys_limit_signature(phys_limits(info).upper_limit),
            ),
        ),
        (
            "INTERNAL-CONSTRS",
            (
                info.internal_present,
                _limit_signature(info.internal_lower_text, info.internal_lower_interval, "LOWER-LIMIT"),
                _limit_signature(info.internal_upper_text, info.internal_upper_interval, "UPPER-LIMIT"),
                info.internal_other_tags,
            ),
        ),
    )


def _sender_receiver_signature(info: SRInterfaceInfo, ctx: _Context) -> tuple[tuple[str, Any], ...]:
    """The row of a sender-receiver interface: ``IS-SERVICE`` and its data elements."""
    # The two element literals are compared as spelled: an explicit `STANDARD`
    # is carried where an absent one is not, so they write two files.
    return (
        ("IS-SERVICE", _is_service_signature(info.is_service)),
        (
            "DATA-ELEMENTS",
            tuple(
                (
                    element.name,
                    ctx.token(element.type_ref),
                    _constant_signature(element.init_ref, ctx),
                    element.init_unreadable,
                    element.sw_impl_policy,
                    element.sw_calibration_access,
                )
                for element in info.data_elements
            ),
        ),
    )


def _client_server_signature(info: CSInterfaceInfo, ctx: _Context) -> tuple[tuple[str, Any], ...]:
    """The row of a client-server interface: ``IS-SERVICE``, its operations and their arguments."""
    return (
        ("IS-SERVICE", _is_service_signature(info.is_service)),
        (
            "OPERATIONS",
            tuple(
                (
                    operation.name,
                    tuple(
                        (
                            argument.name,
                            ctx.token(argument.type_ref),
                            argument.direction,
                            # Not carried, but reported where it differs from
                            # what the writer writes, so it changes the report.
                            argument.server_argument_impl_policy,
                        )
                        for argument in operation.arguments
                    ),
                )
                for operation in info.operations
            ),
        ),
    )


def _runnable_signature(runnable: SwcRunnableInfo, ctx: _Context) -> tuple[Any, ...]:
    """The row of one runnable of a component type: its triggers, accesses, local accesses, areas and result points."""
    return (
        runnable.name,
        # An unreadable text is kept apart from an absent element: the mapper
        # skips the runnable for the one and writes 0 for the other.
        (runnable.min_start_interval, runnable.unreadable_min_start_interval),
        # As written into `concurrent`: absent and `false` are one value; an
        # unreadable text is kept apart, fail-closed.
        (runnable.can_be_invoked_concurrently is True, runnable.unreadable_concurrency),
        tuple(
            (
                access.name,
                access.kind,
                access.tag,
                ctx.token(access.port_ref),
                ctx.token(access.target_ref),
                access.timeout_text,
            )
            for access in runnable.accesses
        ),
        # Written as `symbol`, or reported; compared as spelled either way.
        runnable.symbol,
        tuple(
            (
                access.name,
                access.container,
                access.tag,
                access.reference_forms,
                ctx.token(access.variable_ref),
                access.scope,
            )
            for access in runnable.local_accesses
        ),
        tuple((ref.role, ref.tag, ctx.token(ref.ref), ref.dest) for ref in runnable.area_refs),
        tuple(
            (point.name, point.tag, ctx.token(point.call_point_ref), point.call_point_dest)
            for point in runnable.result_points
        ),
    )


def _event_signature(event: SwcEventInfo, ctx: _Context) -> tuple[Any, ...]:
    """The row of one event of a component type: its trigger, its targets and its disabled modes."""
    return (
        event.name,
        event.kind,
        ctx.token(event.runnable_ref),
        # As the interval above: an unreadable PERIOD apart from an absent one.
        (event.period, event.unreadable_period),
        ctx.token(event.context_port_ref),
        ctx.token(event.target_operation_ref),
        # The context port's `DEST` verbatim: an R-port and a PR-port of one
        # path are two triggers, and only the first is carried.
        ctx.token(event.context_r_port_ref),
        event.context_r_port_dest,
        ctx.token(event.target_data_element_ref),
        ctx.token(event.event_source_ref),
        event.event_source_dest,
        # The two mode references verbatim, as they are carried.
        tuple((ctx.token(mode.context_port_ref), mode.mode_group_ref, mode.mode_ref) for mode in event.disabled_modes),
    )


def _swc_signature(info: SwcInfo, ctx: _Context) -> tuple[tuple[str, Any], ...]:
    """The row of a component type.

    Its ports, runnables, exclusive areas, variables, port API options, events, mapping sets and
    ``HANDLE-TERMINATION-AND-RESTART``.
    """
    return (
        ("PORTS", tuple(_port_signature(port, ctx, carries_com_specs=True) for port in info.ports)),
        (
            "RUNNABLES",
            tuple(_runnable_signature(runnable, ctx) for runnable in info.runnables),
        ),
        # An area's `DESC` follows the nested-description policy (`_nested_descriptions`).
        ("EXCLUSIVE-AREAS", tuple(area.name for area in info.exclusive_areas)),
        (
            "INTER-RUNNABLE-VARIABLES",
            tuple(
                (
                    variable.name,
                    variable.role,
                    ctx.token(variable.type_ref),
                    _constant_signature(variable.init_constant_ref, ctx),
                    None if variable.init_value is None else ("in place", _value_key(variable.init_value)),
                    variable.init_unreadable,
                    variable.sw_calibration_access,
                    variable.sw_impl_policy,
                )
                for variable in info.variables
            ),
        ),
        # Not carried, but reported per option for the primary alone, so an
        # option of another declaration would be lost with no line.
        (
            "PORT-API-OPTIONS",
            tuple(
                (
                    ctx.token(option.port_ref),
                    option.port_ref_dest,
                    option.child_tags,
                    option.stated_attributes,
                    option.enable_take_address,
                    option.indirect_api,
                )
                for option in info.port_api_options
            ),
        ),
        (
            "EVENTS",
            tuple(_event_signature(event, ctx) for event in info.events),
        ),
        (
            "DATA-TYPE-MAPPING-REFS",
            tuple(ctx.token(ref) for ref in info.data_type_mapping_refs),
        ),
        # An absent element is a value of its own, never read as `NO-SUPPORT`.
        ("HANDLE-TERMINATION-AND-RESTART", info.handle_termination_and_restart),
    )


def _composition_signature(info: CompositionInfo, ctx: _Context) -> tuple[tuple[str, Any], ...]:
    """The row of a composition: its ports, components and its assembly and delegation connectors."""
    return (
        ("PORTS", tuple(_port_signature(port, ctx, carries_com_specs=False) for port in info.ports)),
        (
            "COMPONENTS",
            tuple((prototype.name, ctx.token(prototype.type_ref), prototype.dest) for prototype in info.prototypes),
        ),
        (
            "ASSEMBLY-SW-CONNECTORS",
            tuple(
                (
                    connector.name,
                    _endpoint_signature(connector.provider, ctx),
                    _endpoint_signature(connector.requester, ctx),
                )
                for connector in info.assembly_connectors
            ),
        ),
        (
            "DELEGATION-SW-CONNECTORS",
            tuple(
                (
                    delegation.name,
                    _endpoint_signature(delegation.inner, ctx),
                    ctx.token(delegation.outer_port_ref),
                )
                for delegation in info.delegation_connectors
            ),
        ),
    )


def _signature(declaration: _Declaration, ctx: _Context) -> tuple[tuple[str, Any], ...]:
    """The equality row for one declaration: ordered ``(property, value)`` pairs.

    Ordered so that a conflict can name the **first** property on which two
    members differ. Every reference is compared resolved, through *token*.

    Where the mapping layer chooses by precedence, the row compares the choice,
    not both candidates, which would refuse pairs on a property neither member
    is mapped through. The mapping layer's precedences:

    * the ``DATA-TYPE-MAPPING-SET`` entry: compared as the entry the layer acts
      on (:func:`compared_mapping_targets`);
    * the backing ``COMPU-METHOD`` of an application primitive: compared
      unconditionally, because the enum branch stays reachable for every
      primitive without a registering twin (:func:`_mapping_signature`);
    * an implementation type's ``CATEGORY``, which picks ``BASE-TYPE-REF`` or
      ``IMPLEMENTATION-DATA-TYPE-REF``: both refs are compared, which can refuse
      a pair but never merge one, because ``CATEGORY`` is compared in this row;
    * first-wins over duplicates inside one declaration (a repeated port name):
      the losing duplicate is compared although never mapped, because it is
      skipped and reported, so two copies differing in it differ in what the
      import tells the user.
    """
    info = declaration.info
    if isinstance(info, SwBaseTypeInfo):
        return _base_type_signature(info)
    if isinstance(info, ImplDataTypeInfo):
        return _implementation_type_signature(info, ctx)
    if isinstance(info, AppPrimitiveDataTypeInfo):
        return _application_primitive_signature(info, ctx)
    if isinstance(info, DataTypeMappingSetInfo):
        return (("DATA-TYPE-MAPS", _mapping_set_signature(info, ctx)),)
    if isinstance(info, UnitInfo):
        return _unit_element_signature(info, ctx)
    if isinstance(info, CompuMethodInfo):
        return _compu_method_element_signature(info, ctx)
    if isinstance(info, DataConstrInfo):
        return _data_constr_element_signature(info)
    if isinstance(info, AppRecordDataTypeInfo):
        return (("ELEMENTS", tuple((element.name, ctx.token(element.type_ref)) for element in info.elements)),)
    if isinstance(info, AppArrayDataTypeInfo):
        return (
            ("ELEMENT TYPE-TREF", ctx.token(info.element_type_ref)),
            ("MAX-NUMBER-OF-ELEMENTS", info.max_number_of_elements),
        )
    if isinstance(info, SRInterfaceInfo):
        return _sender_receiver_signature(info, ctx)
    if isinstance(info, CSInterfaceInfo):
        return _client_server_signature(info, ctx)
    if isinstance(info, SwcInfo):
        return _swc_signature(info, ctx)
    if isinstance(info, CompositionInfo):
        return _composition_signature(info, ctx)
    if isinstance(info, AssemblyConnectorInfo):
        return (
            ("composition", ctx.token(declaration.owner)),
            ("PROVIDER-IREF", _endpoint_signature(info.provider, ctx)),
            ("REQUESTER-IREF", _endpoint_signature(info.requester, ctx)),
        )
    raise AssertionError(f"no equality row for {type(info).__name__}")  # pragma: no cover


def _first_difference(rows: Sequence[tuple[tuple[str, Any], ...]]) -> str | None:
    """The first property on which any member differs from the first, or ``None``."""
    first = rows[0]
    for other in rows[1:]:
        for (name, mine), (_, theirs) in zip(first, other, strict=True):
            if mine != theirs:
                return name
    return None


def _cyclic_keys(edges: dict[GroupKey, set[GroupKey]]) -> set[GroupKey]:
    """Every group on a cycle of the group reference graph, a self-edge included.

    Tarjan's strongly connected components, iterative so that a malformed
    export cannot exhaust the interpreter's stack."""
    index: dict[GroupKey, int] = {}
    low: dict[GroupKey, int] = {}
    on_stack: set[GroupKey] = set()
    stack: list[GroupKey] = []
    counter = 0
    cyclic: set[GroupKey] = set()

    for root in sorted(edges):
        if root in index:
            continue
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        work: list[tuple[GroupKey, Iterator[GroupKey]]] = [(root, iter(sorted(edges[root])))]
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in edges:
                    continue
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(edges[child]))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component: set[GroupKey] = set()
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.add(member)
                    if member == node:
                        break
                if len(component) > 1 or node in edges[node]:
                    cyclic |= component
    return cyclic


def _decision_order(edges: dict[GroupKey, set[GroupKey]]) -> list[GroupKey]:
    """The groups bottom-up: every group after each group it references.

    A cycle's members come out in a stable order of their own; each is decided
    ``cyclic`` anyway.
    """
    ordered: list[GroupKey] = []
    state: dict[GroupKey, int] = {}
    for root in sorted(edges):
        if state.get(root):
            continue
        state[root] = 1
        work: list[tuple[GroupKey, Iterator[GroupKey]]] = [(root, iter(sorted(edges[root])))]
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in edges or state.get(child):
                    continue
                state[child] = 1
                work.append((child, iter(sorted(edges[child]))))
                advanced = True
                break
            if advanced:
                continue
            work.pop()
            state[node] = 2
            ordered.append(node)
    return ordered


class _NestedDescription(NamedTuple):
    """One description below a declaration: the member's source path and its text."""

    source_path: str
    text: str


#: What a member path the primary declaration does not carry reads as: no text.
_NO_NESTED_DESCRIPTION = _NestedDescription("", "")


def _nested_descriptions(
    info: object, compu_by_path: dict[str, CompuMethodInfo], inline: frozenset[str]
) -> dict[str, _NestedDescription]:
    """Every description the reader carries below one declaration, by member path.

    ``{member path below the element: (member source path, text)}``; ``{}`` for
    a kind with no described members. The member path matches the members of
    two merged declarations; the source path is what the report line names. The
    first member of a repeated path wins, as in the mapping layer. A component's
    and a composition's member paths are prefixed by their list (``ports/X``,
    ``runnables/X``, ...), because a port and a runnable may share a name. An
    array's element is not listed: the reader reports its ``DESC`` already.
    """
    found: list[tuple[str, str, str]] = []
    if isinstance(info, SwcInfo | CompositionInfo):
        found.extend((f"ports/{port.name}", port.path, port.description) for port in info.ports)
    if isinstance(info, SwcInfo):
        found.extend((f"runnables/{runnable.name}", runnable.path, runnable.description) for runnable in info.runnables)
        found.extend((f"variables/{variable.name}", variable.path, variable.description) for variable in info.variables)
        found.extend((f"exclusive_areas/{area.name}", area.path, area.description) for area in info.exclusive_areas)
    elif isinstance(info, CompositionInfo):
        found.extend(
            (f"prototypes/{prototype.name}", prototype.path, prototype.description) for prototype in info.prototypes
        )
        found.extend(
            (f"delegations/{delegation.name}", delegation.path, delegation.description)
            for delegation in info.delegation_connectors
        )
    elif isinstance(info, SRInterfaceInfo):
        found.extend(
            (element.name, f"{info.path}/{element.name}", element.description) for element in info.data_elements
        )
    elif isinstance(info, CSInterfaceInfo):
        for operation in info.operations:
            found.append((operation.name, f"{info.path}/{operation.name}", operation.description))
            found.extend(
                (
                    f"{operation.name}/{argument.name}",
                    f"{info.path}/{operation.name}/{argument.name}",
                    argument.description,
                )
                for argument in operation.arguments
            )
    elif isinstance(info, AppPrimitiveDataTypeInfo):
        # A table is an element whose own group reports its entries' texts (the
        # next arm); an enumeration holds its literals only from a private form.
        compu = compu_by_path.get(info.compu_method_ref) if info.compu_method_ref in inline else None
        if compu is not None and compu.category == "TEXTTABLE":
            found.extend((scale.literal, f"{compu.path}/{scale.literal}", scale.description) for scale in compu.scales)
    elif isinstance(info, CompuMethodInfo) and info.category == "TEXTTABLE":
        found.extend((scale.literal, f"{info.path}/{scale.literal}", scale.description) for scale in info.scales)
    elif isinstance(info, AppRecordDataTypeInfo):
        found.extend((element.name, f"{info.path}/{element.name}", element.description) for element in info.elements)
    elif isinstance(info, ImplDataTypeInfo) and info.category != "ARRAY":
        found.extend(
            (element.name, f"{info.path}/{element.name}", element.description) for element in info.sub_elements
        )
    by_member: dict[str, _NestedDescription] = {}
    for member_path, source_path, text in found:
        by_member.setdefault(member_path, _NestedDescription(source_path, text))
    return by_member


def record_dropped_nested_descriptions(decisions: MergeDecisions, skip_log: SkipLog) -> None:
    """One section-2 skip line per :attr:`MergeGroup.dropped_nested_descriptions` entry.

    Tagged ``DESC`` and recorded at the member's own source path, ``NOT_SUPPORTED``:
    the merge is SWCraft's policy. ``import model``'s alone -- ``import
    component`` takes no verdict.
    """
    for group in decisions.merged:
        for source_path, member_path in group.dropped_nested_descriptions:
            skip_log.record(
                source_path,
                "DESC",
                f"DESC of {group.key.name}/{member_path} not carried: the merged {group.key.kind} keeps "
                f"{group.primary}'s description",
                category=SkipCategory.NOT_SUPPORTED,
                effect="abort",
            )


def decide_merges(model: ImportModel) -> MergeDecisions:
    """The merge verdict for every duplicated ``(layer, kind, SHORT-NAME)``.

    Bottom-up over the reference graph: whether two references are the same
    depends on whether their targets merged, so a group is decided only once
    every group it references is.

    Three ways to conflict, in the order the record names them: the group's own
    properties differ; its verdict would depend on itself through a reference
    cycle (fail-closed -- a cycle has no least solution and the standard defines
    no equality to fall back on); or a group it references conflicts, so its own
    reference resolves to nothing at all.

    Returns:
        :class:`MergeDecisions` -- empty for an export declaring every name
        once in every one of the five layers.
    """
    inline = inline_forms(model)
    declarations = _declarations(model, inline)
    by_key: dict[GroupKey, list[_Declaration]] = {}
    for declaration in declarations:
        key = GroupKey(LAYER_BY_TAG[declaration.tag], declaration.tag, declaration.name)
        by_key.setdefault(key, []).append(declaration)
    duplicated = {
        key: sorted(members, key=lambda declaration: declaration.path)
        for key, members in by_key.items()
        if len(members) > 1
    }
    if not duplicated:
        return MergeDecisions()

    declaration_paths = frozenset(declaration.path for declaration in declarations)
    compu_by_path = {compu.path: compu for compu in model.compu_methods}
    constants_by_path = {constant.path: constant for constant in model.constant_specs}
    effective_target_by_app_path = compared_mapping_targets(model)

    # A token depends on the grouping alone, so placeholder groups suffice
    # before any verdict exists.
    placeholder: dict[str, MergeGroup] = {}
    for key, members in duplicated.items():
        stub = MergeGroup(key=key, paths=tuple(member.path for member in members), verdict=MERGED)
        for member in members:
            placeholder[member.path] = stub
    token = _Token(placeholder, declaration_paths)
    ctx = _Context(
        token=token,
        declared=declared_paths(model),
        compu_by_path=compu_by_path,
        constants_by_path=constants_by_path,
        effective_target_by_app_path=effective_target_by_app_path,
        unit_by_path={unit.path: unit for unit in model.units},
        constr_by_path={constr.path: constr for constr in model.data_constrs},
        inline=inline,
    )

    own_difference: dict[GroupKey, str | None] = {}
    edges: dict[GroupKey, set[GroupKey]] = {}
    for key, members in duplicated.items():
        rows: list[tuple[tuple[str, Any], ...]] = []
        token.forget_what_was_reached()
        for member in members:
            rows.append(_signature(member, ctx))
        # A self-edge only for a reference to a member's own path (a cycle of
        # length one); a reference inside a member is content (`_Token.reached_whole`).
        edges[key] = {reached for reached in token.reached if reached != key}
        if key in token.reached_whole:
            edges[key].add(key)
        own_difference[key] = _first_difference(rows)

    cyclic = _cyclic_keys(edges)

    verdict: dict[GroupKey, MergeGroup] = {}
    for key in _decision_order(edges):
        members = duplicated[key]
        paths = tuple(member.path for member in members)
        difference = own_difference[key]
        if difference is not None:
            verdict[key] = MergeGroup(key=key, paths=paths, verdict=CONFLICTING, difference=difference)
            continue
        if key in cyclic:
            verdict[key] = MergeGroup(key=key, paths=paths, verdict=CONFLICTING, cyclic=True)
            continue
        # Both halves of the condition are unreachable (a self-edge or an
        # undecided reference means a cycle, answered above); written rather than
        # asserted, so equality is never assumed.
        inherited = next(
            (
                other
                for other in sorted(edges[key])
                if other != key and (other not in verdict or verdict[other].verdict == CONFLICTING)
            ),
            None,
        )
        if inherited is not None:
            verdict[key] = MergeGroup(key=key, paths=paths, verdict=CONFLICTING, inherited_from=inherited)
            continue
        # Compared exactly: a description differing in whitespace alone is still
        # dropped. Only an empty one is nothing to drop.
        kept = members[0].description
        dropped = tuple(
            (member.path, member.description)
            for member in members[1:]
            if member.description and member.description != kept
        )
        kept_nested = _nested_descriptions(members[0].info, compu_by_path, inline)
        dropped_nested = tuple(
            (source_path, member_path)
            for member in members[1:]
            for member_path, (source_path, text) in _nested_descriptions(member.info, compu_by_path, inline).items()
            if text and text != kept_nested.get(member_path, _NO_NESTED_DESCRIPTION).text
        )
        kept_long_name = getattr(members[0].info, "long_name", None)
        dropped_long_names = tuple(
            (member.path, member.info.long_name)
            for member in members[1:]
            if isinstance(member.info, CompositionInfo)
            and member.info.long_name is not None
            and member.info.long_name != kept_long_name
        )
        kept_encoding = getattr(members[0].info, "encoding", None)
        dropped_encodings = tuple(
            (member.path, member.info.encoding)
            for member in members[1:]
            if isinstance(member.info, SwBaseTypeInfo) and member.info.encoding != kept_encoding
        )
        kept_native_declaration = getattr(members[0].info, "native_declaration", None)
        dropped_native_declarations = tuple(
            (member.path, member.info.native_declaration)
            for member in members[1:]
            if isinstance(member.info, SwBaseTypeInfo) and member.info.native_declaration != kept_native_declaration
        )
        verdict[key] = MergeGroup(
            key=key,
            paths=paths,
            verdict=MERGED,
            dropped_descriptions=dropped,
            dropped_nested_descriptions=dropped_nested,
            dropped_long_names=dropped_long_names,
            dropped_encodings=dropped_encodings,
            dropped_native_declarations=dropped_native_declarations,
        )

    groups = tuple(verdict[key] for key in sorted(verdict))
    return MergeDecisions(
        groups=groups,
        group_by_path={path: group for group in groups for path in group.paths},
    )


def _phys_limit_signature(limit: PhysLimit) -> Any:
    """:func:`_limit_signature` for a ``PHYS-CONSTRS`` limit, which differs in one text: ``NaN`` under ``INFINITE``.

    On a physical limit ``NaN`` is unreadable under every interval. So an ``INFINITE`` physical limit
    that bounds nothing to the reader (:attr:`~swcraft_tools.arxml_reader.PhysLimit.bounds_nothing`)
    is the absent limit's ``None``, and one that does not (``NaN``, or the other side's infinity) is
    compared as spelled with its interval. An unstated interval signs as ``CLOSED``.
    """
    infinite_bounds = limit.text is None or limit.bounds_nothing
    return _limit_signature(limit.text, limit.interval_type or "CLOSED", limit.side, infinite_bounds=infinite_bounds)
