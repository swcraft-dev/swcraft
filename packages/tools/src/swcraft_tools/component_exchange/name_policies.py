"""The two policies by which a component import refuses a name instead of renaming it.

``arxml_import`` mints a replacement for a colliding or reserved name; a component import reaches
a populated project and fails closed instead, so a name the writer would refuse is a refusal with
nothing written, never a rename. The two policies:

* **identifier** -- :func:`illegal_identifiers` (a name or a generated form of it that is not a
  legal AUTOSAR ``Identifier``) and :func:`access_point_collisions` (two access entries of one
  runnable written with one ``SHORT-NAME``), both over the incoming closure alone;
* **reserved-name** -- :func:`reserved_name_violations`, the collisions of the merged model's
  names with the writer's generated ones.

:func:`shape_violations` is the export-shape check. ``import_`` raises
``import_.ReservedNameError`` from the lines these functions return; nothing here imports it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from swcraft_tools.arxml_import.reconcile import (
    data_type_key,
    folded_stem,
)
from swcraft_tools.arxml_import.report import (
    PLACEMENT_WHAT,
)
from swcraft_tools.arxml_reader import ImportModel
from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH, check_identifier, short_name_key
from swcraft_tools.schema import (
    CallReturnedEvent,
    CSInterface,
    DataDescriptionElement,
    DataEvent,
    DataType,
    EnumDataType,
    ExternalDataType,
    ExternalInterface,
    ImplementationArrayDataType,
    ImplementationRecordDataType,
    Interface,
    MappingSet,
    OperationEvent,
    RecordDataType,
    SRInterface,
    SWComponent,
    TextTable,
)
from swcraft_tools.validator import (
    data_description_package_path,
    data_type_layer,
    derived_event_names,
    derived_result_point_name,
    result_point_sites,
    shared_access_point_names,
    stated_access_point_names,
    variable_access_entries,
)
from swcraft_tools.writer.arxml_generation import (
    ACCESS_LIST_PREFIXES as _ACCESS_LIST_PREFIXES,
)
from swcraft_tools.writer.arxml_generation import (
    BUDGET_SUFFIX as _BUDGET_SUFFIX,
)
from swcraft_tools.writer.arxml_generation import (
    IMPLEMENTATION_SUFFIX as _IMPLEMENTATION_SUFFIX,
)
from swcraft_tools.writer.arxml_generation import (
    INTERNAL_BEHAVIOR_SUFFIX as _INTERNAL_BEHAVIOR_SUFFIX,
)
from swcraft_tools.writer.arxml_generation import (
    RESERVED_DATA_TYPE_FORM_RULES as _RESERVED_DATA_TYPE_FORM_RULES,
)
from swcraft_tools.writer.arxml_generation import (
    RESERVED_DATA_TYPE_RULES as _RESERVED_DATA_TYPE_RULES,
)
from swcraft_tools.writer.arxml_generation import (
    TIMING_SUFFIX as _TIMING_SUFFIX,
)
from swcraft_tools.writer.arxml_generation import (
    VARIABLE_ACCESS_LIST_PREFIXES as _VARIABLE_ACCESS_LIST_PREFIXES,
)
from swcraft_tools.writer.arxml_generation import (
    access_point_name as _access_point_name,
)
from swcraft_tools.writer.arxml_generation import (
    emits_swc_timing as _emits_swc_timing,
)
from swcraft_tools.writer.arxml_generation import (
    generated_data_type_forms as _generated_data_type_forms,
)
from swcraft_tools.writer.arxml_generation import (
    generates_kind_form as _generates_kind_form,
)
from swcraft_tools.writer.arxml_generation import (
    init_constant_name as _init_constant_name,
)
from swcraft_tools.writer.arxml_generation import (
    runnable_access_entries as _writer_access_entries,
)


@dataclass
class _NameAudit:
    """The offenders of one :func:`illegal_identifiers` run, one line each, in the order they were found."""

    violations: list[str] = field(default_factory=list)

    def declared(self, subject: str, name: str, authored: str | None = None) -> bool:
        """Whether a name as authored conforms.

        `authored` is the string the export used, when the mapping pipeline has already moved this
        element. Both it and the emitted `name` are checked (one is what the schema sees, the other what
        the sender can search for), and a mismatch is spelled out.
        """
        authored = name if authored is None else authored
        reason = check_identifier(authored)
        if reason is None and authored != name:
            reason = check_identifier(name)
        if reason is None:
            return True
        quoted = f"'{authored}'" if authored == name else f"'{authored}' (mapped to '{name}')"
        self.violations.append(f"{subject} {quoted} is not a legal AUTOSAR Identifier: {reason}")
        return False

    def generated(self, subject: str, name: str, emitted: str) -> None:
        """The check of a SHORT-NAME the writer derives from one already-conforming *name*."""
        reason = check_identifier(emitted)
        if reason is not None:
            self.violations.append(
                f"{subject} '{name}' generates the SHORT-NAME '{emitted}' "
                f"({len(emitted)} characters), which is not a legal AUTOSAR Identifier: {reason}"
            )

    def generated_pair(self, subject: str, first: str, second: str, emitted: str) -> None:
        """:meth:`generated`'s twin for a form derived from two declared names.

        The line states the pair's joint budget, taken off the emitted name so a counter suffix is
        charged to them, and which of the two to shorten.
        """
        reason = check_identifier(emitted)
        if reason is not None:
            joint = MAX_IDENTIFIER_LENGTH - (len(emitted) - len(first) - len(second))
            longer = first if len(first) > len(second) else second
            allowance = max(1, len(longer) - max(0, len(emitted) - MAX_IDENTIFIER_LENGTH))
            self.violations.append(
                f"{subject} '{first}'.'{second}' generates the SHORT-NAME '{emitted}' "
                f"({len(emitted)} characters; '{first}' and '{second}' get {joint} characters between them, "
                f"so shorten '{longer}' to at most {allowance} characters), which is not a legal AUTOSAR "
                f"Identifier: {reason}"
            )


def _audit_component(audit: _NameAudit, swc: SWComponent) -> None:
    """The component's name, its stated behaviour name, its ports and runnables, with their generated forms."""
    # A stated name is checked as declared, and the generated form it replaces is not. Derived
    # names come from the validator's functions, so this guard and `check-model` judge one set.
    stated_behaviour = swc.behavior_name
    if audit.declared("SWC name", swc.name):
        suffixes = (_IMPLEMENTATION_SUFFIX,) + ((_INTERNAL_BEHAVIOR_SUFFIX,) if stated_behaviour is None else ())
        for suffix in suffixes:
            audit.generated("SWC name", swc.name, f"{swc.name}{suffix}")
    if stated_behaviour is not None:
        audit.declared(f"SWC '{swc.name}' stated internal behaviour name", stated_behaviour)
    for port in swc.ports:
        audit.declared(f"SWC '{swc.name}' port name", port.name)
    for runnable in swc.runnables:
        # `{runnable}_Budget` exists only for a runnable stating `wcet`.
        if audit.declared(f"SWC '{swc.name}' runnable name", runnable.name) and runnable.wcet is not None:
            audit.generated(f"SWC '{swc.name}' runnable name", runnable.name, f"{runnable.name}{_BUDGET_SUFFIX}")


def _audit_event_names(audit: _NameAudit, swc: SWComponent) -> None:
    """The events' names, stated or derived, through the validator's restatement of the writer's derivation."""
    # The validator's restatement of the writer's derivation, retroactive counter included, so the
    # guard is never stricter or laxer than the writer. `TMT_`/`IT_` are the runnable's; the
    # port-and-member forms are the pair's, budgeted off the FINAL name. A stated event name keeps
    # its slot in the counter.
    pairs = [(runnable, event) for runnable in swc.runnables for event in runnable.events]
    for (runnable, event), emitted in zip(pairs, derived_event_names(swc), strict=True):
        if event.name is not None:
            audit.declared(f"SWC '{swc.name}' runnable '{runnable.name}' stated event name", event.name)
            continue
        if isinstance(event, OperationEvent | DataEvent | CallReturnedEvent):
            if isinstance(event, OperationEvent):
                member, words = event.operation, "operation event"
            elif isinstance(event, CallReturnedEvent):
                member, words = event.operation, "call-returned event"
            else:
                member, words = event.element, "data-received event"
            if check_identifier(event.port) is not None or check_identifier(member) is not None:
                continue  # one bad declared name is one root cause, reported where it is declared
            audit.generated_pair(f"SWC '{swc.name}' runnable '{runnable.name}' {words}", event.port, member, emitted)
        elif check_identifier(runnable.name) is None:
            audit.generated(f"SWC '{swc.name}' runnable name", runnable.name, emitted)


def _audit_access_points(audit: _NameAudit, swc: SWComponent) -> None:
    """Every runnable's access points and result points, stated or derived."""
    for runnable in swc.runnables:
        for (list_name, port_name, member_field, member), stated in zip(
            _writer_access_entries(runnable), stated_access_point_names(runnable), strict=True
        ):
            if stated is not None:
                audit.declared(
                    f"SWC '{swc.name}' runnable '{runnable.name}' {list_name} port '{port_name}' {member_field} "
                    f"'{member}' stated access point name",
                    stated,
                )
                continue
            if check_identifier(port_name) is not None or check_identifier(member) is not None:
                continue  # one bad declared name is one root cause, reported where it is declared
            audit.generated(
                f"SWC '{swc.name}' runnable '{runnable.name}' {list_name} port '{port_name}' {member_field} name",
                member,
                _access_point_name(_ACCESS_LIST_PREFIXES[list_name], port_name, member),
            )
        # The result points, siblings of the access points. An unstated `result` derives
        # `Result_{port}_{operation}`, charged only when both declared names conform.
        for subject, port_name, operation, stated, _steps, _field in result_point_sites(swc.name, runnable):
            if stated is not None:
                audit.declared(f"{subject} stated name", stated)
            elif check_identifier(port_name) is None and check_identifier(operation) is None:
                audit.generated_pair(subject, port_name, operation, derived_result_point_name(port_name, operation))


def _audit_variables(audit: _NameAudit, swc: SWComponent) -> None:
    """The inter-runnable variables, and the access names of each runnable to them."""
    # A variable's name is its VARIABLE-DATA-PROTOTYPE's SHORT-NAME. An access stating no name
    # generates `RV_{variable}` / `WV_{variable}`, charged only when the variable name conforms.
    for variable in swc.variables:
        audit.declared(f"SWC '{swc.name}' variable name", variable.name)
    for runnable in swc.runnables:
        for list_name, variable_name, stated in variable_access_entries(runnable):
            subject = f"SWC '{swc.name}' runnable '{runnable.name}' {list_name}"
            if stated is not None:
                audit.declared(f"{subject} variable '{variable_name}' stated access name", stated)
            elif check_identifier(variable_name) is None:
                prefix = _VARIABLE_ACCESS_LIST_PREFIXES[list_name]
                audit.generated(f"{subject} variable name", variable_name, f"{prefix}{variable_name}")


def _audit_exclusive_areas(audit: _NameAudit, swc: SWComponent) -> None:
    """The component's exclusive areas."""
    # A `can_enter` / `runs_inside` entry is a reference and writes no SHORT-NAME of its own.
    for area in swc.exclusive_areas:
        audit.declared(f"SWC '{swc.name}' exclusive area name", area.name)


def _audit_interfaces(audit: _NameAudit, interfaces: list[Interface]) -> None:
    """The interfaces, and the data elements, operations and arguments they hold."""
    for iface in sorted(interfaces, key=lambda i: i.name):
        interface_conforms = audit.declared("Interface name", iface.name)
        if isinstance(iface, SRInterface):
            for element in iface.data_elements:
                element_conforms = audit.declared(f"Interface '{iface.name}' data element name", element.name)
                if interface_conforms and element_conforms:
                    audit.generated(
                        f"Interface '{iface.name}' data element name",
                        element.name,
                        _init_constant_name(iface.name, element.name),
                    )
        elif isinstance(iface, CSInterface):
            for cs_operation in iface.operations:
                audit.declared(f"Interface '{iface.name}' operation name", cs_operation.name)
                for argument in cs_operation.arguments:
                    audit.declared(
                        f"Interface '{iface.name}' operation '{cs_operation.name}' argument name",
                        argument.name,
                    )


def _audit_data_types(
    audit: _NameAudit, data_types: list[DataType], source_names: Mapping[tuple[str, str], str]
) -> None:
    """The data types, the generated forms of each, and the literals, fields and elements it holds."""
    for dt in sorted(data_types, key=lambda d: d.name):
        authored_dt = source_names.get(data_type_key(dt), dt.name)
        if audit.declared("Data type name", dt.name, authored_dt):
            for suffix, _what, base_kinds in _RESERVED_DATA_TYPE_RULES:
                if _generates_kind_form(dt, base_kinds):
                    audit.generated("Data type name", dt.name, f"{dt.name}{suffix}")
            # Field-scoped forms: an application type with no `unit` generates no `{name}_Unit`,
            # and charging it for one would make this guard stricter than the writer.
            minted = _generated_data_type_forms(dt)
            for suffix, _what, kind, _fields in _RESERVED_DATA_TYPE_FORM_RULES:
                if isinstance(dt, kind) and suffix in minted:
                    audit.generated("Data type name", dt.name, f"{dt.name}{suffix}")
        if isinstance(dt, EnumDataType):
            for literal in dt.literals or ():  # none where a texttable is named
                audit.declared(f"Enum '{authored_dt}' literal name", literal.name)
        elif isinstance(dt, RecordDataType):
            for field in dt.fields:
                audit.declared(f"Record '{authored_dt}' field name", field.name)
        elif isinstance(dt, ImplementationRecordDataType):
            for implementation_element in dt.elements:
                audit.declared(f"Implementation record '{authored_dt}' element name", implementation_element.name)
        elif isinstance(dt, ImplementationArrayDataType):
            # The array's one sub-element carries `element_name` as its SHORT-NAME.
            audit.declared(f"Implementation array '{authored_dt}' element name", dt.element_name)


def _audit_mapping_sets(audit: _NameAudit, mapping_sets: Sequence[MappingSet]) -> None:
    """The mapping sets: one ``SHORT-NAME`` each, and no generated form lengthens it."""
    for mapping_set in sorted(mapping_sets, key=lambda s: s.name):
        audit.declared("Mapping set name", mapping_set.name)


def _audit_description_elements(audit: _NameAudit, elements: Sequence[DataDescriptionElement]) -> None:
    """The units, compu methods and data constraints, and a texttable's entries."""
    for description_element in sorted(elements, key=lambda e: (e.name, data_type_layer(e))):
        what = PLACEMENT_WHAT[data_type_layer(description_element)]
        audit.declared(f"{what[0].upper()}{what[1:]} name", description_element.name)
        if isinstance(description_element, TextTable):
            for entry in description_element.entries:
                audit.declared(f"Compu method '{description_element.name}' entry name", entry.name)


def illegal_identifiers(
    swc: SWComponent,
    interfaces: list[Interface],
    data_types: list[DataType],
    data_type_source_names: Mapping[tuple[str, str], str] | None = None,
    mapping_sets: Sequence[MappingSet] = (),
    elements: Sequence[DataDescriptionElement] = (),
) -> list[str]:
    """Every name this import would write that is not a legal AUTOSAR ``Identifier``.

    Scope is the incoming closure alone, never rewritten. The emitted form is checked, not only the
    declared one: every generated form of the writer, checked only when its declared names conform,
    so one bad name is one violation. A type the registry renamed is reported under its declared name.

    **Does not reach:** two elements of one implementation record or two record fields sharing a
    name; the collision of two access entries (:func:`access_point_collisions`).

    Args:
        data_type_source_names: ``(layer, emitted name)`` -> the declared ``SHORT-NAME``, for every
            type the registry moved.

    Returns:
        One line per violation, ordered deterministically; empty when every name is legal.
    """
    audit = _NameAudit()
    _audit_component(audit, swc)
    _audit_event_names(audit, swc)
    _audit_access_points(audit, swc)
    _audit_variables(audit, swc)
    _audit_exclusive_areas(audit, swc)
    _audit_interfaces(audit, interfaces)
    _audit_data_types(audit, data_types, data_type_source_names or {})
    _audit_mapping_sets(audit, mapping_sets)
    _audit_description_elements(audit, elements)
    return audit.violations


def access_point_collisions(swc: SWComponent) -> list[str]:
    """Every pair of access entries of one runnable written with one ``SHORT-NAME``, stated or derived.

    Port ``a_b`` writing element ``c`` and port ``a`` writing element ``b_c`` both derive ``W_a_b_c``.
    The clash scope is one runnable; no target state takes part.

    Returns:
        One line per colliding name, ordered by runnable and then by name.
    """
    return [
        f"SWC '{swc.name}' runnable '{runnable.name}': access point SHORT-NAME '{emitted}' is "
        f"{'written for' if stated else 'derived from'} {claimants}"
        for runnable in swc.runnables
        for emitted, claimants, stated in shared_access_point_names(runnable)
    ]


def _swc_name_claims(
    swc: SWComponent,
    target_swcs: Mapping[str, SWComponent],
    target_composition_names: set[str],
) -> list[str]:
    """The ``/ComponentTypes`` names the merged model would carry twice or generate twice.

    The namespace is the ``swc`` layer and the ``compositions`` layer: a component type, its
    ``SWC-IMPLEMENTATION`` and a composition are siblings in one ``AR-PACKAGE``. The base set is the
    components that actually emit an implementation: an ``external: true`` component emits nothing,
    so refusing ``Stub_Implementation`` would make the guard stricter than the writer. A composition
    claims a name and generates none. ``{base}_Timing`` is generated only where a runnable declares a
    ``wcet``; the incoming name is a base when *swc*, as written, emits one. A pair wholly inside the
    target is not reported.
    """
    violations: list[str] = []

    merged_claimants = set(target_swcs) | target_composition_names | {swc.name}
    # The incoming component is never a stub (the export refuses one), so it is always a base.
    implementation_bases = {name for name, target in target_swcs.items() if not target.external} | {swc.name}
    for name in sorted(merged_claimants):
        for base in _generated_bases(name, _IMPLEMENTATION_SUFFIX, implementation_bases):
            if swc.name not in (name, base):
                continue  # a pair wholly inside the target is not this import's
            what = "composition" if name in target_composition_names else "SWC"
            violations.append(
                f"{what} name '{name}' collides with the SWC-IMPLEMENTATION name auto-generated for '{base}'"
            )
    # The incoming name is a base when *swc* AS WRITTEN emits a timing, never on the target's copy
    # alone: an update that drops every budgeted runnable does not write its budgets.
    timing_bases = {name for name, target in target_swcs.items() if name != swc.name and _emits_swc_timing(target)} | (
        {swc.name} if _emits_swc_timing(swc) else set()
    )
    for name in sorted(merged_claimants):
        for base in _generated_bases(name, _TIMING_SUFFIX, timing_bases):
            if swc.name not in (name, base):
                continue  # a pair wholly inside the target is not this import's
            what = "composition" if name in target_composition_names else "SWC"
            violations.append(f"{what} name '{name}' collides with the SWC-TIMING name auto-generated for '{base}'")
    if swc.name in target_composition_names:
        violations.append(
            f"SWC name '{swc.name}' is also declared as a composition in the target project; a composition, "
            "a component and a component's generated SWC-IMPLEMENTATION are siblings in one AR-PACKAGE"
        )
    return violations


def _data_type_name_claims(
    data_types: list[DataType],
    target_types: Mapping[tuple[str, str], DataType],
) -> list[str]:
    """The data type names the merged model would claim twice, a generated form against a declared name.

    Both directions are checked: the import may bring the suffixed name or the base. Kind-scoped
    forms are read from ``RESERVED_DATA_TYPE_RULES`` (a record gets no ``_CompuMethod``),
    field-scoped ones from ``generated_data_type_forms``. A generated form collides inside its
    owner's package only (``validator.data_description_package_path``), and a mapping set lies in a
    package of its own. A pair wholly inside the target is not reported; an incoming element equal
    to the target's (a reuse) still counts as incoming.
    """
    violations: list[str] = []

    # Keyed `(layer, name)`: a form lies in its owner's layer's package, so a claim is looked for
    # among the names of the claimant's own layer.
    imported_types = {(data_type_layer(dt), dt.name): dt for dt in data_types}
    merged_types: dict[tuple[str, str], DataType] = {**dict(target_types), **imported_types}
    for key in sorted(merged_types, key=lambda key: (key[1], key[0])):
        layer, name = key
        if isinstance(merged_types[key], ExternalDataType):
            continue  # a stub emits no SHORT-NAME, so it claims none (`arxml_generation`'s `_emitting_types`)
        package = data_description_package_path(merged_types[key])
        layer_names = [held for held_layer, held in merged_types if held_layer == layer]
        for suffix, what, base_kinds in _RESERVED_DATA_TYPE_RULES:
            for base in _generated_bases(name, suffix, layer_names):
                if not _generates_kind_form(merged_types[(layer, base)], base_kinds):
                    continue  # an enumeration naming a texttable generates no form
                if data_description_package_path(merged_types[(layer, base)]) != package:
                    continue  # the form lies in its owner's package, not *name*'s
                if key not in imported_types and (layer, base) not in imported_types:
                    continue  # both sides pre-date this import
                violations.append(f"data type name '{name}' collides with the {what} auto-generated for '{base}'")
        # Field-scoped forms: a `Speed_Unit` beside an application type `Speed` declaring no `unit`
        # is an ordinary name, and refusing it would refuse a name generation accepts.
        for suffix, what, kind, _fields in _RESERVED_DATA_TYPE_FORM_RULES:
            for base in _generated_bases(name, suffix, layer_names):
                stem = merged_types[(layer, base)]
                if not isinstance(stem, kind) or suffix not in _generated_data_type_forms(stem):
                    continue
                if data_description_package_path(stem) != package:
                    continue  # the form lies in its owner's package, not *name*'s
                if key not in imported_types and (layer, base) not in imported_types:
                    continue  # both sides pre-date this import
                violations.append(f"data type name '{name}' collides with the {what} auto-generated for '{base}'")
    return violations


def _interface_name_claims(
    interfaces: list[Interface],
    target_interfaces: Mapping[str, Interface],
) -> list[str]:
    """The interface names the merged model would claim twice, against a generated constant or another constant.

    The writer emits one ``CONSTANT-SPECIFICATION`` per sender-receiver data element beside the
    interfaces, so ``{I}_{E}_Init`` collides two ways, both refused (the length half is
    :func:`illegal_identifiers`'):

    * against a declared interface name, compared ignoring case;
    * against another pair's constant, compared exactly: interface ``A_B`` with element ``C`` and
      interface ``A`` with element ``B_C`` derive one name.

    A collision wholly inside the target is not reported. A client-server interface derives no
    constant and is still a sibling of every constant.
    """
    violations: list[str] = []

    imported_interfaces = {iface.name: iface for iface in interfaces}
    merged_interfaces: dict[str, Interface] = {**dict(target_interfaces), **imported_interfaces}
    # Every pair, whether or not the element carries an `init` today, as the writer reserves it.
    claimants: dict[str, list[tuple[str, str]]] = {}
    for name, iface in sorted(merged_interfaces.items()):
        if isinstance(iface, SRInterface):
            for element in iface.data_elements:
                claimants.setdefault(_init_constant_name(name, element.name), []).append((name, element.name))
    # An interface name against a constant is compared ignoring case; constant against constant exactly.
    interfaces_by_key: dict[str, list[str]] = {}
    for name in sorted(merged_interfaces):
        interfaces_by_key.setdefault(short_name_key(name), []).append(name)
    for emitted, pairs in sorted(claimants.items()):
        colliding = [
            name
            for name in interfaces_by_key.get(short_name_key(emitted), ())
            if not isinstance(merged_interfaces[name], ExternalInterface)
        ]
        # At least one side must be new: blaming a pre-existing collision would make an unrelated
        # component unimportable.
        fresh = any(name in imported_interfaces for name in colliding) or any(
            owner in imported_interfaces for owner, _ in pairs
        )
        if not fresh:
            continue
        for name in colliding:
            owner, element_name = pairs[0]
            violations.append(
                f"interface name '{name}' collides with the CONSTANT-SPECIFICATION auto-generated for "
                f"interface '{owner}' data element '{element_name}'"
            )
        if len(pairs) > 1:
            derived = " and ".join(f"interface '{owner}' data element '{element}'" for owner, element in pairs)
            violations.append(
                f"CONSTANT-SPECIFICATION SHORT-NAME '{emitted}' is derived from {derived} -- an interface name "
                "and a data element name may both contain '_', so the derivation is not injective"
            )
    return violations


def reserved_name_violations(
    swc: SWComponent,
    data_types: list[DataType],
    target_swcs: Mapping[str, SWComponent],
    target_composition_names: set[str],
    target_types: Mapping[tuple[str, str], DataType],
    interfaces: list[Interface],
    target_interfaces: Mapping[str, Interface],
) -> list[str]:
    """Every reserved-name collision the merged model would carry.

    **A generated claim is compared ignoring case, a declared one exactly**: the writer emits the
    generated name beside them in one package, where ``[constr_2508]`` compares ignoring case. The
    access-point forms are not here (:func:`access_point_collisions`).

    Args:
        swc: the SWC **as it will be written**, the kept behaviour and carried budgets applied.
        target_types: the target's data types by ``(layer, name)``.

    Returns:
        One line per violation, empty when the merged model is emittable.
    """
    return [
        *_swc_name_claims(swc, target_swcs, target_composition_names),
        *_data_type_name_claims(data_types, target_types),
        *_interface_name_claims(interfaces, target_interfaces),
    ]


def shape_violations(model: ImportModel) -> list[str]:
    """The component-export shape contract checks."""
    violations: list[str] = []
    if len(model.swcs) != 1:
        violations.append(
            f"a component export contains exactly one SWC, found {len(model.swcs)} "
            f"({', '.join(sorted(s.name for s in model.swcs)) or 'none'}) -- "
            "a whole-model export cannot be imported as a component"
        )
    if model.compositions:
        violations.append(
            "composition/connector content is present "
            f"({', '.join(sorted(c.name for c in model.compositions))}) -- "
            "integration-owned content never appears in a component export"
        )
    return violations


def _generated_bases(name: str, suffix: str, bases: Iterable[str]) -> list[str]:
    """Every name of *bases* that generates *name* with *suffix*, compared ignoring case.

    ``mode_compumethod`` names the enum ``Mode``'s ``Mode_CompuMethod`` as ``[constr_2508]``
    compares it. Sorted, so a case-only pair of bases yields one line per base in one order.
    """
    stem = folded_stem(short_name_key(name), suffix)
    if stem is None:
        return []
    return sorted(base for base in bases if short_name_key(base) == stem)
