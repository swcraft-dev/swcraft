"""The import's second naming pass: resolve collisions with the names the ARXML writer reserves.

``PathRegistry._reserve()`` resolves bare-name collisions while the mapping passes walk ARXML paths; a collision
with a writer-reserved name is resolved here, over the final, selected element set, by ``pipeline.run_import()``
before anything is written, with every reference following the renamed element. A reservation applies only where
the writer emits the form (``{x}_CompuMethod`` for an enumeration stating literals, ``{x}_Unit`` / ``{x}_Compu`` /
``{x}_Range`` where the application type states the field, ``{x}_Range`` where a value type states a bound).
``import component`` renames nothing; it refuses on the same reserved-name rules instead.

The element whose name is the *derived* form moves:

===================================== ==========================================
Collision                             Renamed
===================================== ==========================================
``x`` (enum) + ``x_CompuMethod``      the declared ``x_CompuMethod``
``x`` (application, with the field) + the declared ``x_Unit`` / ``x_Compu`` /
``x_Unit`` / ``x_Compu`` / ``x_Range`` ``x_Range``
``x`` (value type, with a bound) +    the declared ``x_Range``
``x_Range``
component type ``A`` + component type the declared ``A_Implementation`` (either
``A_Implementation``                  may be a composition)
SR pair ``(I, E)`` + interface        the declared ``I_E_Init``
``I_E_Init``
===================================== ==========================================

Not repaired: two declared SR pairs deriving one constant name (interface ``A_B`` with element ``C`` and
interface ``A`` with element ``B_C`` both derive ``A_B_C_Init``); such an export imports with nothing renamed and
``check-model`` and the writer refuse the project.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar

from pydantic import BaseModel

from swcraft_tools.arxml_import.merge import (
    ELEMENT_LAYERS,
    ELEMENT_NOUN_BY_LAYER,
    LAYER_APPLICATION_TYPES,
    LAYER_BASE_TYPES,
    LAYER_COMPOSITIONS,
    LAYER_IMPLEMENTATION_TYPES,
    LAYER_INTERFACES,
    LAYER_MAPPING_SETS,
    LAYER_SWC,
)
from swcraft_tools.arxml_import.registry import (
    RENAME_SUFFIX,
    NameCollisionError,
    PathRegistry,
    fit_identifier,
)
from swcraft_tools.arxml_import.swc_mapping import ImportedSwc
from swcraft_tools.arxml_version import DATA_DESCRIPTION_PACKAGE_NAMES, ROOT_PACKAGE_NAMES
from swcraft_tools.guarantee import required
from swcraft_tools.identifier import check_identifier, short_name_key
from swcraft_tools.schema import (
    ArrayDataType,
    Composition,
    CompositionMember,
    Connector,
    ConnectorEndpoint,
    CSArgument,
    CSInterface,
    DataDescriptionElement,
    DataElement,
    DataType,
    EnumDataType,
    ExternalDataType,
    ExternalInterface,
    ImplementationArrayDataType,
    ImplementationRecordDataType,
    Interface,
    MappingSet,
    PointerDataType,
    RecordDataType,
    ReferenceDataType,
    SRInterface,
    TextTable,
    ValueDataType,
    Variable,
)
from swcraft_tools.validator import data_description_package_path, data_type_layer
from swcraft_tools.writer.arxml_generation import (
    IMPLEMENTATION_SUFFIX,
    RESERVED_DATA_TYPE_FORM_RULES,
    RESERVED_DATA_TYPE_RULES,
    TIMING_SUFFIX,
    emits_swc_timing,
    generated_data_type_forms,
    generates_kind_form,
    init_constant_name,
)

#: How many rename rounds the pass may run before it gives up.
#:
#: A replacement is a declared name from then on, so renaming ``x_CompuMethod`` to ``x_CompuMethod_app``
#: can make a declared ``x_CompuMethod_app_CompuMethod`` collide; re-scanning keeps the guarantee for
#: adversarial input, and the bound makes an oscillating rule fail loudly instead of hanging.
_MAX_PASSES = 8

_M = TypeVar("_M", bound=BaseModel)
_Prototype = TypeVar("_Prototype", DataElement, CSArgument, Variable)
#: The key :func:`reserved_data_type_names` hands back: a name, or ``(layer, name)``.
_K = TypeVar("_K")
#: A data type's identity in the import: ``(layer, name)``.
_TypeKey = tuple[str, str]


@dataclass
class ReconciledModel:
    """The element lists ``run_import()`` carries, after reconciliation.

    Reference-equal to the inputs when nothing collided.

    The three ``*_source_names`` maps (emitted name -> declared name) are for diagnostics: renaming runs
    before :func:`identifier_violations`, so a refusal can quote the name the export declared rather than
    the replacement the importer minted. They are kept per namespace because a data type, an interface
    and a component may legally share one name.
    """

    data_types: list[DataType]
    interfaces: list[Interface]
    swcs: list[ImportedSwc]
    connectors: list[Connector]
    #: A map names a data type on both sides, so a data-type rename follows into it. A set's own name
    #: never moves: it lies in its own package and shares no SHORT-NAME scope with the types.
    mapping_sets: list[MappingSet] = field(default_factory=list)
    #: A boundary port names an interface and a member names a component, so both renames follow into them.
    compositions: list[Composition] = field(default_factory=list)
    #: ``(layer, emitted data-type name)`` -> the declared name, for every type moved in either phase
    #: (the bare-name suffixing included, so not empty for a typical tool export).
    data_type_source_names: dict[tuple[str, str], str] = field(default_factory=dict)
    #: Component-type name -> the declared name, for every component and composition this pass moved
    #: (the two share one ``/ComponentTypes`` ``SHORT-NAME`` scope).
    swc_source_names: dict[str, str] = field(default_factory=dict)
    #: Interface name -> the declared name, for every interface :func:`_reconcile_interfaces` moved.
    interface_source_names: dict[str, str] = field(default_factory=dict)


def free_name(original: str, taken: set[str]) -> str:
    """A conforming, unclaimed replacement for the reserved name `original`.

    ``{original}_app``, then ``{original}_2_app``, ``{original}_3_app``, … The counter goes before the token
    so ``_app`` stays last, and the stem is re-truncated to the 128-character limit each time. The result is
    never itself writer-reserved (no reserved suffix ends in ``_app``), but it can make another declared name
    reserved, which is why the callers re-scan (:data:`_MAX_PASSES`).

    Args:
        original: the declared name being replaced.
        taken: every name already claimed in that namespace, including this pass's earlier replacements.
            Compared ignoring case: a case-only pair is what ``[constr_2508]`` forbids in one package.
    """
    taken_keys = {short_name_key(name) for name in taken}
    candidate = fit_identifier(original, RENAME_SUFFIX)
    counter = 2
    while short_name_key(candidate) in taken_keys:
        candidate = fit_identifier(original, f"_{counter}{RENAME_SUFFIX}")
        counter += 1
    return candidate


def reserved_data_type_names(by_name: Mapping[_K, DataType]) -> dict[_K, str]:
    """Every declared data-type name the ARXML writer reserves, with its reason.

    Read from the writer's own rules (``RESERVED_DATA_TYPE_RULES``, ``RESERVED_DATA_TYPE_FORM_RULES``,
    ``generated_data_type_forms``), scoped exactly as the writer scopes them: a guard stricter than the
    writer would rename names generation accepts. So ``Mode_CompuMethod`` beside a record ``Mode``, or beside
    an enumeration naming a table, is no collision, nor is ``Speed_Unit`` beside an application type that
    declares no ``unit``. Only the derived side is returned, and only inside the owner's package (a generated
    form lies in the package of its type).

    Compared ignoring case, because every claim here is a generated one: a declared ``gear_t_compumethod``
    beside an enumeration ``Gear_T`` would be a case-only pair with the generated ``Gear_T_CompuMethod``,
    which ``[constr_2508]`` forbids. A case-only pair of two declared types is package placement's
    (``place_case_variants``).

    Args:
        by_name: every data type this import would write, keyed by its emitted name or by
            ``(layer, name)``; the key is only handed back.

    Returns:
        The key of each reserved type -> the reason, worded for the import report.
    """
    by_key: dict[str, list[DataType]] = {}
    for dt in by_name.values():
        by_key.setdefault(short_name_key(dt.name), []).append(dt)
    reserved: dict[_K, str] = {}
    for key_of_dt, dt in by_name.items():
        name = dt.name
        if isinstance(dt, ExternalDataType):
            # An external type stub emits no SHORT-NAME, so it is out of every generated-name collision.
            continue
        key = short_name_key(name)
        for suffix, what, kind, _fields in RESERVED_DATA_TYPE_FORM_RULES:
            stem = folded_stem(key, suffix)
            if stem is None:
                continue
            base = next(
                (
                    candidate
                    for candidate in by_key.get(stem, ())
                    if isinstance(candidate, kind)
                    and suffix in generated_data_type_forms(candidate)
                    and data_description_package_path(candidate) == data_description_package_path(dt)
                ),
                None,
            )
            if base is not None:
                reserved[key_of_dt] = f"collides with the {what} auto-generated for '{base.name}'"
                break
        if key_of_dt in reserved:
            continue
        for suffix, what, base_kinds in RESERVED_DATA_TYPE_RULES:
            stem = folded_stem(key, suffix)
            if stem is None:
                continue
            base = next(
                (
                    candidate
                    for candidate in by_key.get(stem, ())
                    if generates_kind_form(candidate, base_kinds)
                    and data_description_package_path(candidate) == data_description_package_path(dt)
                ),
                None,
            )
            if base is not None:
                reserved[key_of_dt] = f"collides with the {what} auto-generated for '{base.name}'"
                break
    return reserved


def folded_stem(key: str, suffix: str) -> str | None:
    """The stem of *key* before *suffix*, both compared ignoring case, or ``None`` when it does not end in it.

    *key* is already a :func:`~swcraft_tools.identifier.short_name_key`; the suffix is folded here.
    """
    folded = short_name_key(suffix)
    if not key.endswith(folded) or len(key) == len(folded):
        return None
    return key[: -len(folded)]


def reserved_swc_names(names: set[str], implementation_bases: set[str], timing_bases: set[str]) -> dict[str, str]:
    """Every declared component-type name the ARXML writer reserves, with its reason.

    ``{A}_Implementation`` (and ``{A}_Timing``) where ``A`` is a base below. ``{A}_InternalBehavior`` is not
    reserved: it is nested inside its own component. A composition and a component are siblings in
    ``/ComponentTypes``, so both are claimants; but a composition and an external stub emit no
    ``SWC-IMPLEMENTATION``, so neither is a base, and a component ``A_Implementation`` beside a composition
    ``A`` keeps its name. Compared ignoring case, as :func:`reserved_data_type_names` explains. It does
    **not** reach which side of a collision moves: :func:`_reconcile_swcs` moves the derived side.

    Args:
        names: the claimants, every component-type name this import would write (components and
            compositions).
        implementation_bases: the names the writer generates a ``SWC-IMPLEMENTATION`` for, every
            non-``external`` component's.
        timing_bases: the names the writer generates a ``SWC-TIMING`` for, every non-``external`` component
            one of whose runnables declares a ``wcet`` (``arxml_generation.emits_swc_timing``).
    """
    bases_by_key: dict[str, list[str]] = {}
    for base in implementation_bases:
        bases_by_key.setdefault(short_name_key(base), []).append(base)
    timing_by_key: dict[str, list[str]] = {}
    for base in timing_bases:
        timing_by_key.setdefault(short_name_key(base), []).append(base)
    reserved: dict[str, str] = {}
    for name in names:
        stem = folded_stem(short_name_key(name), IMPLEMENTATION_SUFFIX)
        if stem is not None and stem in bases_by_key:
            base = sorted(bases_by_key[stem])[0]
            reserved[name] = f"collides with the SWC-IMPLEMENTATION auto-generated for '{base}'"
            continue
        stem = folded_stem(short_name_key(name), TIMING_SUFFIX)
        if stem is not None and stem in timing_by_key:
            base = sorted(timing_by_key[stem])[0]
            reserved[name] = f"collides with the SWC-TIMING auto-generated for '{base}'"
    return reserved


def reserved_interface_names(interfaces: list[Interface]) -> dict[str, str]:
    """Every declared interface name the ARXML writer reserves, with its reason.

    The writer emits one ``CONSTANT-SPECIFICATION`` ``{I}_{E}_Init`` per sender-receiver data element into
    ``PortInterfaces`` beside the interfaces, so an interface of that name collides with the constant of
    the pair ``(I, E)``; a client-server interface derives none and is still reserved against, as a sibling in
    the same package. The names come from the writer's ``init_constant_name``, never an ``_Init`` suffix rule (a
    guard stricter than the writer renames names generation accepts), for every declared pair whether or not it
    carries an ``init``. Two pairs deriving one constant are not here: there is no derived
    side to move.

    Args:
        interfaces: every interface this import would write, of both kinds.

    Returns:
        Reserved name -> the reason, worded for the import report.
    """
    # An interface against a generated constant is compared ignoring case.
    owners: dict[str, tuple[str, str]] = {}
    for iface in interfaces:
        if isinstance(iface, SRInterface):
            for element in iface.data_elements:
                owners.setdefault(
                    short_name_key(init_constant_name(iface.name, element.name)), (iface.name, element.name)
                )

    reserved: dict[str, str] = {}
    for iface in interfaces:
        if isinstance(iface, ExternalInterface):
            # An external interface stub emits no SHORT-NAME and derives no constant.
            continue
        owner = owners.get(short_name_key(iface.name))
        if owner is not None:
            reserved[iface.name] = (
                "collides with the CONSTANT-SPECIFICATION auto-generated for interface "
                f"'{owner[0]}' data element '{owner[1]}'"
            )
    return reserved


def reconcile_reserved_names(
    data_types: list[DataType],
    interfaces: list[Interface],
    swcs: list[ImportedSwc],
    connectors: list[Connector],
    compositions: list[Composition],
    registry: PathRegistry,
    mapping_sets: list[MappingSet],
) -> ReconciledModel:
    """Rename every element whose name the ARXML writer reserves, and rewrite every reference to it.

    Renames are appended to ``registry.renames``, so they reach the import report's one rename section.
    Candidates are processed sorted by emitted name, which is unique at this point, so two runs of one
    import produce identical names.

    Args:
        data_types: the mapped data types, after ``--packages`` selection.
        interfaces: the mapped interfaces, after selection; they follow a type rename and may themselves be
            renamed (an interface named like a derived init constant).
        swcs: the mapped components, after selection, external stubs included; their ports follow an
            interface rename.
        connectors: the mapped connectors, after selection; their endpoints follow a component rename. A
            connector's own name is the export's and never changes.
        compositions: the mapped compositions, after selection; boundary ports and members follow both
            renames.
        registry: the shared ``PathRegistry``; mutated (path map, name set and rename lines).
        mapping_sets: the mapped mapping sets, after selection; a map side follows a type rename. Required,
            with no default, so an omitted list cannot skip it silently; pass ``[]`` for none.

    Returns:
        ReconciledModel with the reconciled lists. The inputs are not mutated; ``registry`` is.

    Raises:
        NameCollisionError: the scan did not settle within :data:`_MAX_PASSES`.
    """
    mapping_sets = list(mapping_sets)
    data_types, interfaces, mapping_sets, swcs, data_type_source_names = _reconcile_data_types(
        data_types, interfaces, mapping_sets, swcs, registry
    )
    # Before the component half: an interface rename rewrites `Port.interface`, so it runs on the
    # component list as handed in, not on components the SWC half has already replaced.
    interfaces, swcs, compositions, interface_source_names = _reconcile_interfaces(
        interfaces, swcs, compositions, registry
    )
    swcs, connectors, compositions, swc_source_names = _reconcile_swcs(swcs, connectors, compositions, registry)
    return ReconciledModel(
        data_types=data_types,
        interfaces=interfaces,
        swcs=swcs,
        connectors=connectors,
        mapping_sets=mapping_sets,
        compositions=compositions,
        data_type_source_names=data_type_source_names,
        swc_source_names=swc_source_names,
        interface_source_names=interface_source_names,
    )


def _tracked(origins: dict[str, str], renames: Mapping[str, str]) -> None:
    """Fold one round's ``old -> new`` renames into ``new -> declared``, chaining across rounds.

    The component and interface namespaces' counterpart of :meth:`PathRegistry.rename`, whose path map
    holds neither name.
    """
    for old, new in renames.items():
        origins[new] = origins.pop(old, old)


def _reconcile_data_types(
    data_types: list[DataType],
    interfaces: list[Interface],
    mapping_sets: list[MappingSet],
    swcs: list[ImportedSwc],
    registry: PathRegistry,
) -> tuple[list[DataType], list[Interface], list[MappingSet], list[ImportedSwc], dict[_TypeKey, str]]:
    """The data-type half: rename, rewrite every type reference, re-scan.

    A type is ``(layer, name)``: a replacement avoids the names of its own layer, and a rename rewrites only
    the references whose field targets that layer (in data types, interfaces, mapping sets and the
    components' inter-runnable variables). The loop runs ``_MAX_PASSES + 1`` scans so that a model settling on
    the last permitted rename round is re-checked before it is called unsettled.

    The last return value is the registry's ``source_names``, because both rename phases write there.
    """
    for attempt in range(_MAX_PASSES + 1):
        by_key: dict[_TypeKey, DataType] = {data_type_key(dt): dt for dt in data_types}
        reserved = reserved_data_type_names(by_key)
        if not reserved:
            return data_types, interfaces, mapping_sets, swcs, registry.source_names
        if attempt == _MAX_PASSES:
            break

        taken: dict[str, set[str]] = {}
        renames: dict[_TypeKey, str] = {}
        for key in sorted(reserved, key=lambda key: (key[1], key[0])):
            layer, name = key
            if layer not in taken:
                taken[layer] = {held for held_layer, held in by_key if held_layer == layer}
                taken[layer] |= registry.claimed_names(layer)
            replacement = free_name(name, taken[layer])
            taken[layer].add(replacement)
            renames[key] = replacement
            registry.rename(name, replacement, reserved[key], layer=layer)

        data_types = [_renamed_data_type(dt, renames) for dt in data_types]
        interfaces = [renamed_interface(iface, renames) for iface in interfaces]
        mapping_sets = [_renamed_mapping_set(mapping_set, renames) for mapping_set in mapping_sets]
        swcs = [swc_with_renamed_variable_types(imported, renames) for imported in swcs]

    raise NameCollisionError(
        f"Reserved-name reconciliation did not settle within {_MAX_PASSES} passes for data types "
        f"{sorted(name for _layer, name in reserved)} -- every rename resolved one collision and created another"
    )


def _reconcile_interfaces(
    interfaces: list[Interface],
    swcs: list[ImportedSwc],
    compositions: list[Composition],
    registry: PathRegistry,
) -> tuple[list[Interface], list[ImportedSwc], list[Composition], dict[str, str]]:
    """The interface half: rename, follow into every component and composition port, re-scan.

    Re-scanning matters here too: an interface renamed to ``X_app`` derives ``X_app_e_Init``, which another
    declared interface may be named. ``record_rename``, not ``rename``: an interface name is not in the
    registry's path map. The last return value is this pass's own ``emitted name -> declared name`` map; no
    earlier phase renames an interface.
    """
    origins: dict[str, str] = {}
    for attempt in range(_MAX_PASSES + 1):
        reserved = reserved_interface_names(interfaces)
        if not reserved:
            return interfaces, swcs, compositions, origins
        if attempt == _MAX_PASSES:
            break

        taken = {iface.name for iface in interfaces}
        renames: dict[str, str] = {}
        for name in sorted(reserved):
            replacement = free_name(name, taken)
            taken.add(replacement)
            renames[name] = replacement
            registry.record_rename(name, replacement, reserved[name], layer=LAYER_INTERFACES)

        _tracked(origins, renames)
        interfaces = [
            renamed(iface, {"name": renames[iface.name]}) if iface.name in renames else iface for iface in interfaces
        ]
        swcs = [swc_with_renamed_interfaces(imported, renames) for imported in swcs]
        compositions = [composition_with_renamed_interfaces(comp, renames) for comp in compositions]

    raise NameCollisionError(
        f"Reserved-name reconciliation did not settle within {_MAX_PASSES} passes for interfaces "
        f"{sorted(reserved)} -- every rename resolved one collision and created another"
    )


def swc_with_renamed_interfaces(imported: ImportedSwc, renames: dict[str, str]) -> ImportedSwc:
    """`imported` with every port that names a renamed interface re-pointed; as-is when nothing moved.

    *renames* may also be keyed by the ARXML path a port was kept with, onto the external interface stub
    minted for it (``pipeline._apply_stubs``).
    """
    if not any(port.interface in renames for port in imported.swc.ports):
        return imported
    # `replace`, so every fact `map_swcs` carried beside the component travels with it.
    return replace(
        imported,
        swc=_revalidated(
            imported.swc,
            {
                "ports": [
                    port.model_copy(update={"interface": renames[port.interface]})
                    if port.interface in renames
                    else port
                    for port in imported.swc.ports
                ]
            },
        ),
    )


def swc_with_renamed_variable_types(imported: ImportedSwc, renames: Mapping[_TypeKey, str]) -> ImportedSwc:
    """`imported` with every inter-runnable variable whose type moved re-pointed; as-is when nothing moved.

    *renames* may also be keyed ``(implementation_types, path)``, onto the external type stub minted for a
    kept path (``pipeline._apply_stubs``).
    """
    if not any(_prototype_type_key(variable) in renames for variable in imported.swc.variables):
        return imported
    return replace(
        imported,
        swc=_revalidated(
            imported.swc,
            {"variables": [_renamed_prototype(variable, renames) for variable in imported.swc.variables]},
        ),
    )


def composition_with_renamed_interfaces(composition: Composition, renames: dict[str, str]) -> Composition:
    """`composition` with every boundary port that names a renamed interface re-pointed; as-is when nothing moved.

    ``model_copy`` per port, because a field-by-field rebuild would erase any field it does not name.
    """
    if not any(port.interface in renames for port in composition.ports):
        return composition
    return _revalidated(
        composition,
        {
            "ports": [
                port.model_copy(update={"interface": renames[port.interface]}) if port.interface in renames else port
                for port in composition.ports
            ]
        },
    )


def _renamed_member(member: CompositionMember, renames: dict[str, str]) -> CompositionMember:
    """One member with whichever of its two type fields it declares re-pointed; as-is when it did not move."""
    field_name = "composition" if member.composition is not None else "type"
    declared = member.composition if member.composition is not None else member.type
    if declared not in renames:
        return member
    return _revalidated(member, {field_name: renames[declared]})


def _renamed_composition(composition: Composition, renames: dict[str, str]) -> Composition:
    """`composition` with its own name and every member's type reference re-pointed; as-is when nothing moved.

    A composition shares the component-type namespace, so it may be renamed itself. Not reached here: the
    boundary ports (:func:`composition_with_renamed_interfaces`) and the connectors naming it
    (:func:`_renamed_connector`).
    """
    update: dict[str, object] = {}
    if composition.name in renames:
        update["name"] = renames[composition.name]
    members = [_renamed_member(member, renames) for member in composition.members]
    if any(new is not old for new, old in zip(members, composition.members)):
        update["members"] = members
    return renamed(composition, update)


def _reconcile_swcs(
    swcs: list[ImportedSwc],
    connectors: list[Connector],
    compositions: list[Composition],
    registry: PathRegistry,
) -> tuple[list[ImportedSwc], list[Connector], list[Composition], dict[str, str]]:
    """The component-type half: rename, follow into every reference, re-scan.

    The namespace is the components and the compositions together: they share one ``/ComponentTypes``
    ``SHORT-NAME`` scope. A rename follows into a connector endpoint's ``swc`` and ``composition``, the
    connector's own ``composition`` and a member's ``type`` and ``composition``. Not followed: a delegation
    names a member and ports, not a type. The last return value is this pass's own
    ``emitted name -> declared name`` map, for components and compositions alike.
    """
    origins: dict[str, str] = {}
    for attempt in range(_MAX_PASSES + 1):
        names = {imported.swc.name for imported in swcs} | {composition.name for composition in compositions}
        # A composition and an external stub emit no SWC-IMPLEMENTATION: claimants, never bases.
        implementation_bases = {imported.swc.name for imported in swcs if not imported.swc.external}
        timing_bases = {imported.swc.name for imported in swcs if emits_swc_timing(imported.swc)}
        reserved = reserved_swc_names(names, implementation_bases, timing_bases)
        if not reserved:
            return swcs, connectors, compositions, origins
        if attempt == _MAX_PASSES:
            break

        taken = set(names)
        renames: dict[str, str] = {}
        for name in sorted(reserved):
            replacement = free_name(name, taken)
            taken.add(replacement)
            renames[name] = replacement
            # record_rename, not rename: rewriting the path map would re-point a data type of the same name.
            registry.record_rename(name, replacement, reserved[name], layer=LAYER_SWC)

        _tracked(origins, renames)
        swcs = [
            replace(imported, swc=_revalidated(imported.swc, {"name": renames[imported.swc.name]}))
            if imported.swc.name in renames
            else imported
            for imported in swcs
        ]
        connectors = [_renamed_connector(connector, renames) for connector in connectors]
        compositions = [_renamed_composition(comp, renames) for comp in compositions]

    raise NameCollisionError(
        f"Reserved-name reconciliation did not settle within {_MAX_PASSES} passes for component types "
        f"{sorted(reserved)} -- every rename resolved one collision and created another"
    )


def _revalidated(model: _M, update: dict[str, object]) -> _M:
    """``model_copy(update=...)`` put back through Pydantic validation, which ``model_copy`` skips."""
    return type(model).model_validate(model.model_copy(update=update).model_dump())


def data_type_key(dt: DataType | DataDescriptionElement) -> _TypeKey:
    """*dt*'s identity in the import: ``(layer, name)``, the layer read from its class."""
    return (data_type_layer(dt), dt.name)


def _renamed_data_type(dt: DataType, renames: Mapping[_TypeKey, str]) -> DataType:
    """`dt` with its own name and every type it references remapped.

    *renames* is keyed ``(layer, name)``: each reference is looked up in the layer its field targets, so a
    rename of one member of a same-name pair moves only the references that mean that member.
    """
    app, impl, base = LAYER_APPLICATION_TYPES, LAYER_IMPLEMENTATION_TYPES, LAYER_BASE_TYPES
    update: dict[str, object] = {}
    if data_type_key(dt) in renames:
        update["name"] = renames[data_type_key(dt)]
    if isinstance(dt, ValueDataType) and (base, dt.base) in renames:
        update["base"] = renames[(base, dt.base)]
    elif isinstance(dt, ReferenceDataType) and (impl, dt.base) in renames:
        update["base"] = renames[(impl, dt.base)]
    elif isinstance(dt, ArrayDataType) and (app, dt.element_type) in renames:
        update["element_type"] = renames[(app, dt.element_type)]
    elif isinstance(dt, RecordDataType) and any((app, f.type) in renames for f in dt.fields):
        update["fields"] = [
            f.model_copy(update={"type": renames[(app, f.type)]}) if (app, f.type) in renames else f for f in dt.fields
        ]
    elif isinstance(dt, ImplementationRecordDataType) and any((impl, e.type) in renames for e in dt.elements):
        update["elements"] = [
            e.model_copy(update={"type": renames[(impl, e.type)]}) if (impl, e.type) in renames else e
            for e in dt.elements
        ]
    elif isinstance(dt, ImplementationArrayDataType) and (impl, dt.element_type) in renames:
        update["element_type"] = renames[(impl, dt.element_type)]
    elif isinstance(dt, PointerDataType) and (impl, dt.target) in renames:
        update["target"] = renames[(impl, dt.target)]
    return renamed(dt, update)


def _renamed_mapping_set(mapping_set: MappingSet, renames: Mapping[_TypeKey, str]) -> MappingSet:
    """`mapping_set` with every data-type reference on either side remapped, each side in its own layer."""
    app, impl = LAYER_APPLICATION_TYPES, LAYER_IMPLEMENTATION_TYPES
    if not any(
        (app, entry.application) in renames or (impl, entry.implementation) in renames for entry in mapping_set.maps
    ):
        return mapping_set
    return renamed(
        mapping_set,
        {
            "maps": [
                entry.model_copy(
                    update={
                        "application": renames.get((app, entry.application), entry.application),
                        "implementation": renames.get((impl, entry.implementation), entry.implementation),
                    }
                )
                for entry in mapping_set.maps
            ]
        },
    )


def _prototype_type_key(prototype: DataElement | CSArgument | Variable) -> tuple[str, str]:
    """The ``(layer, name)`` the one type field *prototype* sets names."""
    if prototype.application_type is not None:
        return (LAYER_APPLICATION_TYPES, prototype.application_type)
    return (
        LAYER_IMPLEMENTATION_TYPES,
        required(prototype.implementation_type, "exactly_one_type_field admits one type"),
    )


def _renamed_prototype(prototype: _Prototype, renames: Mapping[_TypeKey, str]) -> _Prototype:
    """*prototype* with the type field it sets remapped, the other left unset.

    Only a rename in that field's layer applies: ``application_type`` for an application type,
    ``implementation_type`` for an implementation type or a kept unresolved path.
    """
    key = _prototype_type_key(prototype)
    if key not in renames:
        return prototype
    field = "application_type" if prototype.application_type is not None else "implementation_type"
    return prototype.model_copy(update={field: renames[key]})


def renamed_interface(iface: Interface, renames: Mapping[_TypeKey, str]) -> Interface:
    """`iface` with every data-element and argument type remapped (:func:`_renamed_prototype`).

    *renames* may also be keyed ``(implementation_types, path)``, onto the external type stub minted for a
    kept path (``pipeline._apply_stubs``).
    """
    if isinstance(iface, SRInterface):
        if not any(_prototype_type_key(element) in renames for element in iface.data_elements):
            return iface
        return renamed(
            iface,
            {"data_elements": [_renamed_prototype(element, renames) for element in iface.data_elements]},
        )
    if isinstance(iface, CSInterface):
        if not any(_prototype_type_key(arg) in renames for op in iface.operations for arg in op.arguments):
            return iface
        return renamed(
            iface,
            {
                "operations": [
                    op.model_copy(update={"arguments": [_renamed_prototype(arg, renames) for arg in op.arguments]})
                    for op in iface.operations
                ]
            },
        )
    return iface


def _renamed_connector(connector: Connector, renames: dict[str, str]) -> Connector:
    """`connector` with both endpoints and its own ``composition`` re-pointed; its own name is the export's."""
    from_ = _renamed_endpoint(connector.from_, renames)
    to = _renamed_endpoint(connector.to, renames)
    composition = renames.get(connector.composition) if connector.composition is not None else None
    if from_ is connector.from_ and to is connector.to and composition is None:
        return connector
    return Connector(
        name=connector.name,
        **{"from": from_},
        to=to,
        composition=composition if composition is not None else connector.composition,
        description=connector.description,
    )


def _renamed_endpoint(endpoint: ConnectorEndpoint, renames: dict[str, str]) -> ConnectorEndpoint:
    """One endpoint with whichever of its two type fields it declares re-pointed; as-is when it did not move.

    Rebuilt on the declared field alone: an endpoint declares exactly one of ``swc`` and ``composition``.
    """
    field_name = "composition" if endpoint.swc is None else "swc"
    declared = endpoint.composition if endpoint.swc is None else endpoint.swc
    if declared not in renames:
        return endpoint
    return ConnectorEndpoint(**{field_name: renames[declared], "port": endpoint.port})


def renamed(model: _M, update: dict[str, object]) -> _M:
    """`model` re-validated with `update` applied, or `model` if nothing changed."""
    return _revalidated(model, update) if update else model


@dataclass
class _IdentifierAudit:
    """The offenders of one :func:`identifier_violations` run, one line each, in the order they were found."""

    violations: list[str] = field(default_factory=list)

    def check(self, subject: str, name: str, origins: Mapping[str, str] | None = None) -> None:
        """Check the declared name ``origins`` gives ``name``, then ``name`` itself; one line if either offends."""
        # Both names: the declared one is what a refusal quotes, the emitted one is what the schema sees.
        # Checking only the emitted one would pass an over-long declaration under its capped replacement.
        declared = (origins or {}).get(name, name)
        reason = check_identifier(declared)
        if reason is None and declared != name:
            reason = check_identifier(name)
        if reason is None:
            return
        if declared == name:
            self.violations.append(f"{subject} '{name}': {reason}")
        else:
            self.violations.append(f"{subject} '{declared}' (renamed to '{name}' by this import): {reason}")


def _audit_data_types(audit: _IdentifierAudit, reconciled: ReconciledModel) -> None:
    """The data types: each name, and the literals, fields and elements it holds."""
    types = reconciled.data_type_source_names

    for dt in reconciled.data_types:
        declared_dt = types.get(data_type_key(dt), dt.name)
        audit.check("data type", dt.name, {dt.name: declared_dt})
        if isinstance(dt, EnumDataType):
            # None where the enumeration names a table.
            for literal in dt.literals or ():
                audit.check(f"enum '{declared_dt}' literal", literal.name)
        elif isinstance(dt, RecordDataType):
            for record_field in dt.fields:
                audit.check(f"record '{declared_dt}' field", record_field.name)
        # Each is an emitted IMPLEMENTATION-DATA-TYPE-ELEMENT SHORT-NAME, the export's own.
        elif isinstance(dt, ImplementationRecordDataType):
            for implementation_element in dt.elements:
                audit.check(f"implementation record '{declared_dt}' element", implementation_element.name)
        elif isinstance(dt, ImplementationArrayDataType):
            audit.check(f"implementation array '{declared_dt}' element", dt.element_name)


def _audit_data_description_elements(audit: _IdentifierAudit, elements: Sequence[DataDescriptionElement]) -> None:
    """The units, compu methods and data constraints, and a table's entries."""
    # No pass renames these, so they carry no origin map.
    for element in elements:
        audit.check(ELEMENT_NOUN_BY_LAYER[data_type_key(element)[0]], element.name)
        if isinstance(element, TextTable):
            for entry in element.entries:
                audit.check(f"compu method '{element.name}' entry", entry.name)


def _audit_interfaces(audit: _IdentifierAudit, reconciled: ReconciledModel) -> None:
    """The interfaces, and the data elements, operations and arguments they hold."""
    ifaces = reconciled.interface_source_names

    for iface in reconciled.interfaces:
        audit.check("interface", iface.name, ifaces)
        declared_iface = ifaces.get(iface.name, iface.name)
        if isinstance(iface, SRInterface):
            for data_element in iface.data_elements:
                audit.check(f"interface '{declared_iface}' data element", data_element.name)
        elif isinstance(iface, CSInterface):
            for operation in iface.operations:
                audit.check(f"interface '{declared_iface}' operation", operation.name)
                for argument in operation.arguments:
                    audit.check(f"interface '{declared_iface}' operation '{operation.name}' argument", argument.name)


def _audit_swcs(audit: _IdentifierAudit, reconciled: ReconciledModel) -> None:
    """The components, and the ports, runnables, variables and exclusive areas they hold."""
    components = reconciled.swc_source_names

    for imported in reconciled.swcs:
        audit.check("SWC", imported.swc.name, components)
        declared_swc = components.get(imported.swc.name, imported.swc.name)
        # A stub's port name is the last segment of the ends written on it, so it is checked too.
        for port in imported.swc.ports:
            audit.check(f"SWC '{declared_swc}' port", port.name)
        for runnable in imported.swc.runnables:
            audit.check(f"SWC '{declared_swc}' runnable", runnable.name)
        # A stated access name is not checked here: `settle_stated_names` declines an illegal one.
        for variable in imported.swc.variables:
            audit.check(f"SWC '{declared_swc}' variable", variable.name)
        for area in imported.swc.exclusive_areas:
            audit.check(f"SWC '{declared_swc}' exclusive area", area.name)


def _audit_compositions(audit: _IdentifierAudit, reconciled: ReconciledModel) -> None:
    """The compositions, and the ports, members and delegations they hold."""
    components = reconciled.swc_source_names

    # A composition's own name takes the component origin map: `_reconcile_swcs` may move it. Nothing
    # renames the other three sites.
    for composition in reconciled.compositions:
        audit.check("composition", composition.name, components)
        declared_composition = components.get(composition.name, composition.name)
        for composition_port in composition.ports:
            audit.check(f"composition '{declared_composition}' port", composition_port.name)
        for member in composition.members:
            audit.check(f"composition '{declared_composition}' member", member.name)
        for delegation in composition.delegations:
            audit.check(f"composition '{declared_composition}' delegation", delegation.name)


def identifier_violations(reconciled: ReconciledModel, elements: Sequence[DataDescriptionElement]) -> list[str]:
    """Every *declared* name in the reconciled set that is no legal Identifier.

    ``arxml_reader`` does not enforce the ``IDENTIFIER--SIMPLE`` facet, so without this check an export
    declaring ``Wheel Speed Swc`` would import into a project ``check-model`` and ``generate arxml`` refuse.
    Pattern and length are both refused: renaming cannot repair a declared name (``_app`` leaves it illegal,
    and SWCraft has no ``swcraft rename``), and an over-long ``SHORT-NAME`` is not schema-valid ARXML.

    Not reached: a legal declared name whose *generated* form overflows (a 112-character component's
    ``_InternalBehavior``). The import succeeds and the first ``check-model`` names it; the writer refuses
    to generate it.

    Each offender is checked under both its declared name (recovered through ``ReconciledModel``'s
    ``*_source_names`` maps) and its emitted one, and the message quotes the declared name, the string the
    reader can find in the export.

    Args:
        reconciled: the element set ``run_import()`` is about to write.
        elements: the units, compu methods and data constraints it writes
            beside them, each its source's own name -- and a table's entries,
            each an enumeration literal's name.

    Returns:
        One line per offender, in a stable order; empty for a conforming export.
    """
    audit = _IdentifierAudit()
    _audit_data_types(audit, reconciled)
    _audit_data_description_elements(audit, elements)
    _audit_interfaces(audit, reconciled)
    _audit_swcs(audit, reconciled)
    for connector in reconciled.connectors:
        audit.check("connector", connector.name)

    _audit_compositions(audit, reconciled)

    # A set's name is emitted and never renamed; a map's two sides are references, not SHORT-NAMEs.
    for mapping_set in reconciled.mapping_sets:
        audit.check("mapping set", mapping_set.name)

    return audit.violations


# ---------------------------------------------------------------------------
# Package placement
# ---------------------------------------------------------------------------

#: The sub-package a member is placed in when the last segment of its source
#: package is no legal AUTOSAR ``Identifier`` (``Platform Types``,
#: ``platform-types``): the reader indexes a package's ``SHORT-NAME`` as
#: written, and ``package`` is judged by ``rule:identifier.declared``.
CASE_VARIANT_PACKAGE = "CaseVariant"


_DATA_TYPES_ROOT, _PORT_INTERFACES_ROOT, _COMPONENT_TYPES_ROOT = ROOT_PACKAGE_NAMES

#: The home package the writer emits each layer's elements into, as its path below the document root, for
#: every layer whose elements share one home. A data type's home depends on its kind, so it has no row:
#: :func:`home_package` asks the validator for it.
ROOT_PACKAGE_OF_LAYER: dict[str, str] = {
    LAYER_MAPPING_SETS: f"{_DATA_TYPES_ROOT}/{DATA_DESCRIPTION_PACKAGE_NAMES['mapping_sets']}",
    LAYER_INTERFACES: _PORT_INTERFACES_ROOT,
    LAYER_SWC: _COMPONENT_TYPES_ROOT,
    LAYER_COMPOSITIONS: _COMPONENT_TYPES_ROOT,
}


def home_package(layer: str, element: Any) -> str:
    """The home package the writer emits *element* of *layer* into, as its path below the document root."""
    if layer in (LAYER_IMPLEMENTATION_TYPES, LAYER_BASE_TYPES, LAYER_APPLICATION_TYPES, *ELEMENT_LAYERS):
        return data_description_package_path(element)
    return ROOT_PACKAGE_OF_LAYER[layer]


def kept_package(home: str, name: str, source_path: str) -> str | None:
    """The sub-package an incoming element's own path names in SWCraft's layout, ``/{home}/{package}/{name}``.

    Shared by both importers: it reads back the ``package`` a SWCraft-written export recorded. *home* is the
    element's home package below the document root (:func:`home_package`). Any other path shape, or a
    segment that is no legal ``Identifier``, names none.

    What it does not reach: it reads the path's shape only, so a foreign package tree named like one of
    SWCraft's homes is read as SWCraft's layout too; and a file an earlier release exported, its data types under
    ``/DataTypes/{name}`` or ``/DataTypes/{package}/{name}``, is read like any foreign file, with no ``package``
    recovered and no line saying so.
    """
    prefix = f"/{home}/"
    if not source_path.startswith(prefix):
        return None
    rest = source_path[len(prefix) :].split("/")
    if len(rest) == 2 and rest[1] == name:
        return rest[0] if check_identifier(rest[0]) is None else None
    return None


@dataclass(frozen=True)
class PlacementMember:
    """One declared element emitted into a root package, as package placement sees it.

    Args:
        layer: the element's layer.
        name: its ``SHORT-NAME``.
        source_path: its full AUTOSAR path in the file being imported; ``""``
            for a *fixed* member, which no file declares.
        generated: every name the writer generates beside it and places in its
            package (``{enum}_CompuMethod``, ``{app}_Unit`` / ``_Compu`` /
            ``_Range``, ``{I}_{E}_Init``, ``{swc}_Implementation``).
        package: the sub-package it already sits in, ``None`` for the root.
        fixed: a member that never moves -- ``import component``'s target
            elements (a shared definition is never overwritten).
    """

    layer: str
    name: str
    source_path: str
    generated: tuple[str, ...] = ()
    package: str | None = None
    fixed: bool = False

    @property
    def claimed(self) -> tuple[str, ...]:
        """Every ``SHORT-NAME`` it puts into its package: its own and its generated forms'."""
        return (self.name, *self.generated)


@dataclass(frozen=True)
class PlacedMember:
    """One member package placement moved into a sub-package.

    ``unused_segment`` is the source segment that could not be used because it
    is no legal ``Identifier`` (the member then went to
    :data:`CASE_VARIANT_PACKAGE` or a numbered form of it), ``None`` otherwise.
    """

    declaration: PlacementMember
    package: str
    unused_segment: str | None = None


@dataclass(frozen=True)
class SamePackagePair:
    """Two declared members of one source package whose names differ only in case.

    The source's own ``[constr_2508]`` defect: ``kept`` is the first by full
    source path, compared ordinally, ``dropped`` the other.
    """

    kept: PlacementMember
    dropped: PlacementMember


@dataclass(frozen=True)
class RootLevelTwin:
    """A member that has to move and has no source package to move to.

    Its file declares it directly in the root package itself, ``/{Root}/{name}``,
    beside a *fixed* member differing only in case -- ``import component``'s
    refusal, which names ``twin`` so an author can place it by hand first.
    """

    declaration: PlacementMember
    twin: PlacementMember


@dataclass(frozen=True)
class CaseVariantPlacement:
    """What :func:`place_case_variants` decided for one root package."""

    placed: tuple[PlacedMember, ...] = ()
    same_package: tuple[SamePackagePair, ...] = ()
    unplaceable: tuple[RootLevelTwin, ...] = ()


def _source_package(path: str) -> str:
    """The package path *path* is declared in: everything before its last ``/``."""
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _stays_first(root: str, member: PlacementMember) -> tuple[bool, str]:
    """The order that picks the member which stays in the root package.

    First a member whose source path is exactly ``/{root}/{name}`` (SWCraft's own layout, so its export
    imports back unchanged), then the first by full source path, compared ordinally.
    """
    return (member.source_path != f"/{root}/{member.name}", member.source_path)


def place_case_variants(root: str, members: Iterable[PlacementMember]) -> CaseVariantPlacement:
    """Place each declared member that would share *root* with a name differing only in case.

    Shared by ``import model`` and ``import component``. It only **records** a ``package``; it renames nothing and
    moves no reference. Groups by :func:`~swcraft_tools.identifier.short_name_key` are processed in key order, so the
    result does not depend on mapping order. A group spelling its name two or more ways is a collision:

    - two members from **one** source package (``[constr_2508]``): the first by full source path is kept, the other
      is returned in ``same_package`` and placed nowhere;
    - the member that stays is a fixed one when the group holds any, else the first by :func:`_stays_first`;
    - every other spelling moves to the last segment of its source package path, or :data:`CASE_VARIANT_PACKAGE`
      when that segment is no legal ``Identifier``. A candidate equal by ``short_name_key`` to any name in the root
      (an element, a generated form, a sub-package already there or chosen) is **taken** -- unless it is a
      sub-package chosen before that the member's own segment names exactly, with no collision inside -- and moves to
      ``{segment}_2``, ``{segment}_3``, ..., each capped by :func:`~swcraft_tools.arxml_import.registry.fit_identifier`;
    - a member whose source package is the root itself has no segment and is returned in ``unplaceable``.

    A spelling repeated exactly (an exact duplicate, or a declared name equal to a generated one) is left alone here;
    ``name_space_collisions`` over the placed model is the fail-closed check.

    Args:
        root: the home package's path below the document root (``DataTypes/Base``, ``PortInterfaces``, …);
            :func:`~swcraft_tools.validator.root_package_members` gives each.
        members: every declared element emitted into that home.
    """
    members = list(members)
    root_members = [member for member in members if member.package is None]
    root_keys = {short_name_key(name) for member in root_members for name in member.claimed}
    # Every sub-package of the root: its key -> its one spelling, and the names inside it.
    sub_packages: dict[str, str] = {}
    contents: dict[str, set[str]] = {}
    for member in members:
        if member.package is not None:
            sub_packages.setdefault(short_name_key(member.package), member.package)
            contents.setdefault(member.package, set()).update(short_name_key(name) for name in member.claimed)

    groups: dict[str, list[PlacementMember]] = {}
    for member in root_members:
        groups.setdefault(short_name_key(member.name), []).append(member)

    placed: list[PlacedMember] = []
    same_package: list[SamePackagePair] = []
    unplaceable: list[RootLevelTwin] = []
    for key in sorted(groups):
        group = groups[key]
        if len({member.name for member in group}) < 2:
            continue
        fixed = sorted((member for member in group if member.fixed), key=lambda member: (member.name, member.layer))
        survivors: list[PlacementMember] = []
        first_in_package: dict[str, PlacementMember] = {}
        for member in sorted((member for member in group if not member.fixed), key=lambda member: member.source_path):
            kept = first_in_package.setdefault(_source_package(member.source_path), member)
            if kept is not member and kept.name != member.name:
                same_package.append(SamePackagePair(kept, member))
            else:
                survivors.append(member)
        if fixed:
            stayers = fixed
            movers = survivors
        else:
            ordered = sorted(survivors, key=lambda member: _stays_first(root, member))
            stayers = ordered[:1]
            movers = sorted(ordered[1:], key=lambda member: member.source_path)
        staying = {member.name for member in stayers}
        taken_by_group: set[str] = set()
        for member in movers:
            if member.name in staying:
                continue
            source_package = _source_package(member.source_path)
            if source_package in ("", f"/{root}"):
                unplaceable.append(RootLevelTwin(member, stayers[0]))
                continue
            segment = source_package.rsplit("/", 1)[-1]
            legal = check_identifier(segment) is None
            candidate = _free_package(
                segment if legal else CASE_VARIANT_PACKAGE,
                reusable=legal,
                inside={short_name_key(name) for name in member.claimed},
                root_keys=root_keys,
                sub_packages=sub_packages,
                contents=contents,
                taken_by_group=taken_by_group,
            )
            sub_packages.setdefault(short_name_key(candidate), candidate)
            contents.setdefault(candidate, set()).update(short_name_key(name) for name in member.claimed)
            taken_by_group.add(candidate)
            placed.append(PlacedMember(member, candidate, None if legal else segment))
    return CaseVariantPlacement(tuple(placed), tuple(same_package), tuple(unplaceable))


def _free_package(
    base: str,
    *,
    reusable: bool,
    inside: set[str],
    root_keys: set[str],
    sub_packages: Mapping[str, str],
    contents: Mapping[str, set[str]],
    taken_by_group: set[str],
) -> str:
    """The first of *base*, ``{base}_2``, ``{base}_3``, ... that is not taken (:func:`place_case_variants`).

    *reusable* is whether *base* is the member's own legal source segment, the
    one case in which an existing sub-package of exactly that spelling may be
    shared -- provided none of the names in *inside* is already in it.
    """
    counter = 1
    while True:
        first = counter == 1
        candidate = base if first else fit_identifier(base, f"_{counter}")
        counter += 1
        key = short_name_key(candidate)
        if check_identifier(candidate) is not None or key in root_keys or candidate in taken_by_group:
            continue
        existing = sub_packages.get(key)
        if existing is None:
            return candidate
        # Only the member's own segment, never a numbered form of it, may share a sub-package.
        if reusable and first and existing == candidate and not contents[candidate] & inside:
            return candidate
