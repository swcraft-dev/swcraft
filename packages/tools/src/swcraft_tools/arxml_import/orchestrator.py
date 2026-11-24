"""The passes ``pipeline.run_import()`` calls between and after the mappers.

The stub passes (the ``init`` values reached through an external stub, the stub
member chains, a data type's stubbed references), the ``--packages`` pruning
helpers (the members and connectors a narrowed composition keeps), the reserved
device-name and case-variant skips, package placement and its collision refusal,
the removal of an empty root composition, the report lines of the access
collapses and the incompatible mappings, and the skips of the asynchronous-call
findings. ``pipeline.py`` sets their order.

Imports only modules before it in the package's dependency graph
(``type_mappers`` and ``swc_mapper`` not among them).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, TypeVar

from swcraft_tools.arxml_import.connectors import (
    MappedCompositions,
)
from swcraft_tools.arxml_import.merge import (
    ELEMENT_TAG_BY_LAYER,
    LAYER_COMPOSITIONS,
    LAYER_CONNECTORS,
    LAYER_IMPLEMENTATION_TYPES,
    LAYER_INTERFACES,
    LAYER_MAPPING_SETS,
    LAYER_SWC,
)
from swcraft_tools.arxml_import.reconcile import (
    PlacementMember,
    ReconciledModel,
    data_type_key,
    kept_package,
    place_case_variants,
    renamed,
)
from swcraft_tools.arxml_import.registry import PathRegistry
from swcraft_tools.arxml_import.report import (
    case_only_duplicate_skip,
    package_placement_line,
    reserved_device_name_skips,
)
from swcraft_tools.arxml_import.swc_mapping import (
    COM_SPEC_BLOCK,
    ImportedSwc,
    PortInitEquivalent,
    PortInitLabels,
    port_init_not_carried,
    report_port_init_labels,
)
from swcraft_tools.arxml_import.type_mapping import (
    MappedSets,
)
from swcraft_tools.arxml_import.unresolved import (
    MintedStubs,
    UnresolvedReferences,
)
from swcraft_tools.arxml_reader import (
    ABORT,
    ImportModel,
    SkipCategory,
    SkipEntry,
    SkipLog,
    SkipReason,
    StandardRule,
)
from swcraft_tools.arxml_version import ArxmlVersion
from swcraft_tools.data_description import DataDescription
from swcraft_tools.reader import (
    find_element_file,
    read_compositions,
)
from swcraft_tools.schema import (
    Composition,
    CompositionMember,
    CompositionPort,
    Connector,
    ConnectorEndpoint,
    DataDescriptionElement,
    DataElement,
    DataType,
    ImplementationArrayDataType,
    ImplementationRecordDataType,
    Interface,
    MappingSet,
    PointerDataType,
    Port,
    PortComSpec,
    ReferenceDataType,
    SRInterface,
    SWComponent,
    Variable,
)
from swcraft_tools.validator import (
    ASYNC_WITHOUT_RESULT,
    NAME_SPACE_LEVEL_PACKAGE,
    async_call_records,
    async_call_subject,
    composite_map_difference,
    mapped_pointer,
    name_space_collisions,
    root_package_members,
)
from swcraft_tools.validator.description_elements import chain_text, implementation_end
from swcraft_tools.validator.init_values import external_type_chain

if TYPE_CHECKING:
    from swcraft_tools.validator import ConsistencyIssue


def access_collapse_lines(imported_swcs: list[ImportedSwc]) -> list[str]:
    """The "Collapsed duplicate access points" lines of the components this import writes.

    Read off the final component set, after the ``--packages`` selection and the
    reserved-name pass, so no line names a component the project never receives;
    de-duplicated in order. A line names the component by the export's own
    SHORT-NAME, as every skip line cites the export's path.
    """
    return list(dict.fromkeys(line for imported in imported_swcs for line in imported.access_collapses))


def member_type_key(member: CompositionMember) -> tuple[bool, str | None]:
    """``(is a composition, the name)`` for the type one member places.

    ``is not None``, never truthiness: an empty string is a declared name the
    schema refuses, not an absent field.
    """
    if member.composition is not None:
        return True, member.composition
    return False, member.type


def _endpoint_type_key(endpoint: ConnectorEndpoint) -> tuple[bool, str | None]:
    """The :func:`member_type_key` of a connector endpoint, whose two type fields follow the same rule."""
    if endpoint.composition is not None:
        return True, endpoint.composition
    return False, endpoint.swc


def is_wired(connector: Connector, placed_by_composition: dict[str, set[tuple[bool, str | None]]]) -> bool:
    """True when `connector`'s composition survived a selection and still places both endpoint types.

    A composition kept with a member pruned away would leave a connector
    ``rule:composition.connector-endpoint-not-a-member`` refuses. The key is the
    pair ``(is a composition, name)``, so an endpoint naming a composition never
    matches a member naming a component of that name.
    """
    placed = placed_by_composition.get(connector.composition or "")
    if placed is None:
        return False
    return all(_endpoint_type_key(endpoint) in placed for endpoint in (connector.from_, connector.to))


def pruned_compositions(
    compositions: list[Composition],
    kept_composition_names: set[str],
    kept_type_names: set[str],
) -> tuple[list[Composition], list[str]]:
    """`compositions` narrowed to a ``--packages`` selection, and the member names dropped on the way.

    A composition outside the selection goes whole; one inside it keeps only the
    members whose type survived and the delegations naming a kept member. One
    composition still carries ``root: true`` when the derived root is kept; none
    when it is not, and the project keeps the root ``swcraft init`` scaffolded.

    Returns:
        ``(compositions, dropped member names)``, each dropped name qualified by
        its composition.
    """
    kept: list[Composition] = []
    dropped: list[str] = []
    for composition in compositions:
        if composition.name not in kept_composition_names:
            dropped.extend(f"{composition.name}.{member.name}" for member in composition.members)
            continue
        members = [
            member for member in composition.members if _member_kept(member, kept_composition_names, kept_type_names)
        ]
        dropped.extend(f"{composition.name}.{member.name}" for member in composition.members if member not in members)
        member_names = {member.name for member in members}
        kept.append(
            composition.model_copy(
                update={
                    "members": members,
                    "delegations": [d for d in composition.delegations if d.member in member_names],
                }
            )
        )
    return kept, dropped


def _member_kept(
    member: CompositionMember,
    kept_composition_names: set[str],
    kept_type_names: set[str],
) -> bool:
    """True when a ``--packages`` selection kept the type this member places."""
    if member.composition is not None:
        return member.composition in kept_composition_names
    return member.type in kept_type_names


def remove_empty_root_compositions(model_root: Path, written_names: set[str]) -> list[str]:
    """Remove every standing composition that declares ``root: true`` and nothing else, and say what happened.

    Called only when the mapped hierarchy carries its own root, which would
    otherwise stand beside it as a second root (``rule:composition.multiple-roots``).
    The shape is what ``swcraft init`` scaffolds: ``root: true`` and no boundary
    port, member, delegation or ``satisfies``; an empty non-root composition is a
    placeholder a person wrote and is kept. The file is located by declared
    identity (``find_element_file``), never by composing ``{name}.yaml``, so a root
    whose file was renamed by hand is found.

    What this does **not** reach: a root carrying a port, a member or a delegation
    is never removed, so a ``--force`` import into a project holding a
    hand-authored hierarchy writes its own root beside it and ``check-model``
    reports the two. Without ``--force`` the emptiness guard refuses that project.

    Args:
        model_root: the project's model directory.
        written_names: the names of the compositions this import is about to
            write; those are overwritten, not removed, and get no line.

    Returns:
        One line per element acted on: the removal, or the failure to locate a
        file for an element the layer just parsed (the layer and the filesystem
        disagree, and the project is about to be written into).
    """
    lines: list[str] = []
    for composition in read_compositions(str(model_root)):
        if not composition.root:
            continue
        # A `satisfies` list is content, the emptiness guard's predicate -- a
        # root the architect linked is never the scaffold.
        if composition.ports or composition.members or composition.delegations or composition.satisfies:
            continue
        if composition.name in written_names:
            continue
        target = find_element_file(str(model_root), "compositions", composition.name)
        if target is None:
            lines.append(
                f"the empty root composition '{composition.name}' could not be located in "
                "compositions/ and was left in place -- the project now declares two roots"
            )
            continue
        removed = Path(target)
        removed.unlink()
        lines.append(
            f"{_storage_name(model_root, removed)}: the empty root composition '{composition.name}' was "
            "removed and replaced by the root this import mapped"
        )
    return lines


def _storage_name(model_root: PurePath, path: PurePath) -> str:
    """*path* as the report names it: relative to the model root, POSIX-spelled.

    No report names where the project is stored; ``as_posix()`` so a Windows run
    prints the name a POSIX one does.
    """
    return path.relative_to(model_root).as_posix()


@dataclass(frozen=True)
class _ReservedCandidate:
    """One element asked about ``rule:storage.reserved-device-name``: its layer, name, ARXML path and fallback tag."""

    layer: str
    name: str
    path: str
    fallback_tag: str


@dataclass(frozen=True)
class ReservedSkip:
    """One element skipped for ``rule:storage.reserved-device-name``, and its skip entry."""

    layer: str
    name: str
    skip: SkipEntry


def skip_reserved_device_names(
    model: ImportModel,
    reconciled: ReconciledModel,
    registry: PathRegistry,
    mapped_sets: MappedSets,
    mapped: MappedCompositions,
    elements: Sequence[DataDescriptionElement],
) -> list[ReservedSkip]:
    """Every element of *reconciled* whose name Windows reserves for a device.

    Asked of the six layers this import writes, over the **final** names (after the
    ``--packages`` selection and the reserved-name pass), in layer and element
    order. The skip entry is
    :func:`~swcraft_tools.arxml_import.report.reserved_device_name_skip`'s, so its
    reason reads as ``import component``'s; its path is the element's ARXML
    declaration and its tag that declaration's (an external stub, which the export
    does not declare, reads ``SW-COMPONENT-TYPE``).

    **The element is skipped and nothing else is.** A port typed by a skipped
    interface, a member placing a skipped component, a connector endpoint on one, a
    type naming a skipped base: each keeps the name it spelled, which ``check-model``
    then reports and ``generate arxml`` refuses until the name is changed at the
    source; a dropped referrer would be a silent loss. No interface or type stub is
    skipped for its own name (``mint_stubs`` never names one for a device, the name
    being SWCraft's to choose), and a stub whose every referrer this skip dropped is
    not written -- the caller drops it, since a stub exists only while a kept element
    names it.
    """
    type_paths = registry.source_paths
    # A mapping set's path is indexed rather than defaulted, as the write loop
    # indexes it: `map_mapping_sets` records a path for every set it maps, so a
    # missing one is a bug to surface, not an element to skip under a guessed tag.
    candidates: list[_ReservedCandidate] = [
        *(
            _ReservedCandidate(data_type_key(dt)[0], dt.name, type_paths.get(data_type_key(dt), ""), "DATA-TYPE")
            for dt in reconciled.data_types
        ),
        # The units, compu methods and data constraints, each under its layer's
        # tag; every element is registered, so its path is indexed.
        *(
            _ReservedCandidate(
                data_type_key(element)[0],
                element.name,
                type_paths[data_type_key(element)],
                ELEMENT_TAG_BY_LAYER[data_type_key(element)[0]],
            )
            for element in elements
        ),
        *(
            _ReservedCandidate(
                LAYER_MAPPING_SETS,
                mapping_set.name,
                mapped_sets.source_paths[mapping_set.name],
                "DATA-TYPE-MAPPING-SET",
            )
            for mapping_set in reconciled.mapping_sets
        ),
        *(
            _ReservedCandidate(
                LAYER_INTERFACES,
                iface.name,
                registry.interface_source_paths.get(reconciled.interface_source_names.get(iface.name, iface.name), ""),
                "PORT-INTERFACE",
            )
            for iface in reconciled.interfaces
        ),
        *(
            _ReservedCandidate(LAYER_SWC, imported.swc.name, imported.path, "SW-COMPONENT-TYPE")
            for imported in reconciled.swcs
        ),
        *(
            _ReservedCandidate(
                LAYER_CONNECTORS, connector.name, mapped.connector_source_paths.get(connector.name, ""), "CONNECTOR"
            )
            for connector in reconciled.connectors
        ),
        *(
            _ReservedCandidate(
                LAYER_COMPOSITIONS,
                composition.name,
                mapped.composition_source_paths.get(
                    reconciled.swc_source_names.get(composition.name, composition.name), ""
                ),
                "COMPOSITION-SW-COMPONENT-TYPE",
            )
            for composition in reconciled.compositions
        ),
    ]
    return [
        ReservedSkip(candidates[index].layer, candidates[index].name, skip)
        for index, skip in reserved_device_name_skips(
            model, ((candidate.name, candidate.path, candidate.fallback_tag) for candidate in candidates)
        )
    ]


def without_inits_through_a_stub(
    interfaces: list[Interface],
    data_types: list[DataType],
    registry: PathRegistry,
    unresolved: UnresolvedReferences,
    skip_log: SkipLog,
    minted: MintedStubs,
) -> tuple[list[Interface], frozenset[str]]:
    """*interfaces* without each ``init`` whose type reaches a minted type stub, and the dropped elements' paths.

    A chain through a reference type, or through a member of an implementation
    composite, reaches a stub only once
    :meth:`~swcraft_tools.arxml_import.unresolved.UnresolvedReferences.mint_stubs`
    has run, so ``map_interfaces`` could not see it (an element typed by a stub
    directly lost its ``init`` there). Such an element meets
    ``rule:interface.init-on-external-type`` and the writer refuses it, so the
    ``init`` is dropped here with a skip line naming the chain, recorded through
    :meth:`~swcraft_tools.arxml_import.unresolved.UnresolvedReferences.chain_skip`
    so it names the stub only when the stub is written. The chain is judged by
    ``validator.external_type_chain``, the rule's own walk.

    The paths returned are what the caller narrows the radix lines by: the value
    was shaped, and a hexadecimal spelling recorded, while the chain's base was
    still a path.
    """
    if not minted.type_names:
        return interfaces, frozenset()

    # The view over this function's own index; the last declaration wins.
    type_map = DataDescription.last_declared(data_types)
    stub_paths = {name: path for path, name in minted.type_names.items()}
    kept: list[Interface] = []
    dropped: set[str] = set()
    for iface in interfaces:
        if not isinstance(iface, SRInterface) or all(element.init is None for element in iface.data_elements):
            kept.append(iface)
            continue
        elements: list[DataElement] = []
        for element in iface.data_elements:
            element_path = f"{registry.interface_source_paths.get(iface.name, iface.name)}/{element.name}"
            reach = _stub_reach(element.implementation_type, type_map, stub_paths) if element.init is not None else None
            if reach is None:
                elements.append(element)
                continue
            target, text, reword = reach
            dropped.add(element_path)
            unresolved.chain_skip(
                skip_log,
                element_path,
                "INIT-VALUE",
                text,
                target=target,
                layer=LAYER_IMPLEMENTATION_TYPES,
                once_stubbed=reword,
                effect="abort",
            )
            elements.append(element.model_copy(update={"init": None}))
        kept.append(renamed(iface, {"data_elements": elements}))
    return kept, frozenset(dropped)


def _stub_reach(
    type_name: str | None, type_map: DataDescription, stub_paths: Mapping[str, str]
) -> tuple[str, str, Callable[[str], str]] | None:
    """Whether an ``init`` shaped against *type_name* can no longer be written, now the stubs are minted.

    ``(target, line, re-wording)`` when it cannot -- the type reaches a minted type
    stub through at least one reference type, or a member of an implementation
    composite it ends at does -- and ``None`` otherwise. *target* is the stub's
    source path, *line* the sentence while it is a path, and the re-wording the one
    ``UnresolvedReferences.chain_skip`` applies once the stub is written. One walk
    for a data element's own ``init`` and a port's own value for it.
    """
    # *type_name* is a data element's `implementation_type`, an implementation
    # type, or `None` for an element whose `application_type` is set, which never
    # reaches a stub.
    if type_name is None:
        return None
    chain = external_type_chain(type_name, type_map, application=False)
    if chain is not None and len(chain) >= 2 and chain[-1] in stub_paths:
        target = stub_paths[chain[-1]]
        through = chain_text(chain[:-1])
        return (
            target,
            f"init: declared type '{type_name}' resolves through the reference chain {through} to "
            f"'{target}', which this export does not declare, so no init value can be shaped against it",
            lambda stub, type_name=type_name, chain=chain, target=target: (
                f"init: declared type '{type_name}' resolves through the reference chain "
                + chain_text(chain)
                + f" to the external type stub '{stub}' ({target}), whose content the export does not "
                "declare, so no init value can be shaped against it"
            ),
        )
    # The rule's walk reaches into an implementation composite's members, so a
    # member typed by a minted stub refuses the whole value. A member typed by the
    # stub directly never gets here: its type was still a path while
    # `map_interfaces` shaped the value, and `_map_init_value` dropped the init.
    inside = _stub_member_chain(type_name, type_map, frozenset(stub_paths), frozenset())
    if inside is None:
        return None
    member, member_chain = inside
    target = stub_paths[member_chain[-1]]
    return (
        target,
        f"init: declared type '{type_name}' has the member '{member}' whose type resolves through "
        f"{chain_text(member_chain[:-1])} to '{target}', which this export does not declare, so no init "
        "value can be shaped against it",
        lambda stub, type_name=type_name, member=member, chain=member_chain, target=target: (
            f"init: declared type '{type_name}' has the member '{member}' whose type resolves through "
            + chain_text(chain[:-1])
            + f" to the external type stub '{stub}' ({target}), whose content the export does not declare, "
            "so no init value can be shaped against it"
        ),
    )


@dataclass(frozen=True)
class StubContext:
    """What the three port-init passes after the stubs are minted ask of the model.

    Built once from the interfaces :func:`without_inits_through_a_stub` left, the
    data types and the minted stubs: the data types by name, each minted type
    stub's source path by its name (empty when none was minted), and the
    sender/receiver interfaces by name.
    """

    type_map: DataDescription
    stub_paths: Mapping[str, str]
    sr_interfaces: Mapping[str, SRInterface]

    @classmethod
    def of(cls, interfaces: list[Interface], data_types: list[DataType], minted: MintedStubs) -> StubContext:
        """The context of one import, once its stubs are minted."""
        return cls(
            # The last declaration wins.
            type_map=DataDescription.last_declared(data_types),
            stub_paths={name: path for path, name in minted.type_names.items()},
            sr_interfaces={iface.name: iface for iface in interfaces if isinstance(iface, SRInterface)},
        )

    def interface_by_port(self, ports: Sequence[Port | CompositionPort]) -> dict[str, SRInterface | None]:
        """Each port's sender/receiver interface by port name, ``None`` for a port typed by none."""
        return {port.name: self.sr_interfaces.get(port.interface or "") for port in ports}


@dataclass(frozen=True)
class PortOwner:
    """A component's or a composition's boundary ports, with what the mapper held for them.

    The ports, the own init values they carry by equivalence
    (``swc_mapping.port_init_equivalents``), the held ``SHORT-LABEL`` lines
    (``swc_mapping.port_init_labels``) -- each named by its port name -- and
    the log the owner's lines go to.
    """

    owned_ports: Sequence[Port | CompositionPort]
    equivalents: Sequence[PortInitEquivalent]
    labels: Sequence[PortInitLabels]
    skip_log: SkipLog


def _element_of(
    interface_by_port: Mapping[str, SRInterface | None], port_name: str, element_name: str
) -> DataElement | None:
    """The data element *element_name* of the interface of port *port_name*, or ``None``."""
    iface = interface_by_port.get(port_name)
    return next((e for e in iface.data_elements if e.name == element_name), None) if iface is not None else None


def without_port_inits_through_a_stub(
    imported_swcs: list[ImportedSwc],
    context: StubContext,
    unresolved: UnresolvedReferences,
    skip_log: SkipLog,
) -> list[ImportedSwc]:
    """*imported_swcs* without each port's own ``init`` whose element's type reaches a minted type stub.

    :func:`without_inits_through_a_stub`'s twin for the port's own value, which
    would meet ``rule:interface.init-on-external-type`` at the port. One line at
    ``({port}/{block}, INIT-VALUE)`` through ``UnresolvedReferences.chain_skip``,
    and its radix line goes with the value.
    """
    if not context.stub_paths:
        return imported_swcs
    result: list[ImportedSwc] = []
    for imported in imported_swcs:
        dropped: set[tuple[str, str]] = set()
        ports: list[Port] = []
        for port in imported.swc.ports:
            iface = context.sr_interfaces.get(port.interface or "")
            if iface is None or all(entry.init is None for entry in port.com_specs):
                ports.append(port)
                continue
            elements = {element.name: element for element in iface.data_elements}
            entries: list[PortComSpec] = []
            for entry in port.com_specs:
                element = elements.get(entry.data_element)
                reach = (
                    _stub_reach(element.implementation_type, context.type_map, context.stub_paths)
                    if entry.init is not None and element
                    else None
                )
                if reach is None:
                    entries.append(entry)
                    continue
                target, text, reword = reach
                block = COM_SPEC_BLOCK[port.direction]
                prefix = port_init_not_carried(entry.data_element)
                unresolved.chain_skip(
                    skip_log,
                    f"{imported.path}/{port.name}/{block}",
                    "INIT-VALUE",
                    prefix + text,
                    target=target,
                    layer=LAYER_IMPLEMENTATION_TYPES,
                    # mypy types no lambda with defaulted parameters in a context (a checker limitation)
                    once_stubbed=lambda stub, prefix=prefix, reword=reword: prefix + reword(stub),  # type: ignore[misc]
                    effect="report",  # a com-spec value on a descriptive port
                )
                dropped.add((port.name, entry.data_element))
                bare = entry.model_copy(update={"init": None})
                if bare != PortComSpec(data_element=entry.data_element):
                    entries.append(bare)
            ports.append(port.model_copy(update={"com_specs": entries}))
        if not dropped:
            result.append(imported)
            continue
        result.append(
            replace(
                imported,
                swc=imported.swc.model_copy(update={"ports": ports}),
                port_init_radix=[
                    r for r in imported.port_init_radix if (r.port_name, r.data_element_name) not in dropped
                ],
            )
        )
    return result


def without_variable_inits_through_a_stub(
    imported_swcs: list[ImportedSwc],
    context: StubContext,
    unresolved: UnresolvedReferences,
    skip_log: SkipLog,
) -> list[ImportedSwc]:
    """*imported_swcs* without each inter-runnable variable's ``init`` whose type reaches a minted type stub.

    :func:`without_inits_through_a_stub`'s twin for a variable: :func:`_stub_reach`'s
    line at ``({variable}, INIT-VALUE)`` through ``UnresolvedReferences.chain_skip``,
    behavioural as the direct case's line in ``swc_mapping.map_variables`` is, and
    its radix line goes with the value.
    """
    if not context.stub_paths:
        return imported_swcs
    result: list[ImportedSwc] = []
    for imported in imported_swcs:
        dropped: set[str] = set()
        variables: list[Variable] = []
        for variable in imported.swc.variables:
            reach = (
                _stub_reach(variable.implementation_type, context.type_map, context.stub_paths)
                if variable.init is not None
                else None
            )
            if reach is None:
                variables.append(variable)
                continue
            target, text, reword = reach
            unresolved.chain_skip(
                skip_log,
                imported.variable_paths.get(variable.name, f"{imported.path}/{variable.name}"),
                "INIT-VALUE",
                text,
                target=target,
                layer=LAYER_IMPLEMENTATION_TYPES,
                once_stubbed=reword,
                effect="abort",
            )
            dropped.add(variable.name)
            variables.append(variable.model_copy(update={"init": None}))
        if not dropped:
            result.append(imported)
            continue
        result.append(
            replace(
                imported,
                swc=imported.swc.model_copy(update={"variables": variables}),
                variable_init_radix=[
                    (name, line) for name, line in imported.variable_init_radix if name not in dropped
                ],
            )
        )
    return result


def name_equivalent_port_inits_through_a_stub(
    owners: Sequence[PortOwner],
    context: StubContext,
    unresolved: UnresolvedReferences,
) -> None:
    """One line per port's own value carried by equivalence whose element's ``init`` a stub took.

    A com-spec value equal to its element's own is carried by equivalence (no
    ``com_specs`` entry; the writer's reference to the element's constant states
    it). Once :func:`without_inits_through_a_stub` drops the element's ``init``,
    the writer writes no ``INIT-VALUE`` at the com-spec, so each such value is
    named here at ``({port}/{block}, INIT-VALUE)``, chained to the stub through
    ``UnresolvedReferences.chain_skip``. *owners* are components' and
    compositions' boundary ports alike (:class:`PortOwner`).

    **What this does not reach**: a com-spec whose ``CONSTANT-REF`` names the
    element's own constant (``type_mapping.port_init_overrides``) -- no outcome is
    decided for it, and its loss stays the element's own line's.
    """
    if not context.stub_paths:
        return
    for owner in owners:
        if not owner.equivalents:
            continue
        interface_by_port = context.interface_by_port(owner.owned_ports)
        for port_name, element_name, key in owner.equivalents:
            element = _element_of(interface_by_port, port_name, element_name)
            reach = (
                _stub_reach(element.implementation_type, context.type_map, context.stub_paths)
                if element is not None
                else None
            )
            if reach is None:
                continue
            target, text, reword = reach
            prefix = port_init_not_carried(element_name)
            unresolved.chain_skip(
                owner.skip_log,
                key,
                "INIT-VALUE",
                prefix + text,
                target=target,
                layer=LAYER_IMPLEMENTATION_TYPES,
                # mypy types no lambda with defaulted parameters in a context (a checker limitation)
                once_stubbed=lambda stub, prefix=prefix, reword=reword: prefix + reword(stub),  # type: ignore[misc]
                effect="report",  # a com-spec value on a descriptive port
            )


def held_port_init_labels(owners: Sequence[PortOwner], context: StubContext) -> None:
    """Record each held ``SHORT-LABEL`` line of a port's own init value whose com-spec still holds one.

    The line is spent by a label difference only while the output holds an
    ``INIT-VALUE`` at that com-spec, which the two stub passes take away where the
    element's type reaches a minted type stub (:func:`_stub_reach`); such a line is
    not recorded, so it stays free to cover another com-spec's label difference at
    the same key. *owners* as :class:`PortOwner`.
    """
    if not context.stub_paths:
        for owner in owners:
            report_port_init_labels(owner.labels, owner.skip_log)
        return
    for owner in owners:
        if not owner.labels:
            continue
        interface_by_port = context.interface_by_port(owner.owned_ports)

        def reaches_a_stub(port_name: str, element_name: str, interface_by_port=interface_by_port) -> bool:
            element = _element_of(interface_by_port, port_name, element_name)
            return (
                element is not None
                and _stub_reach(element.implementation_type, context.type_map, context.stub_paths) is not None
            )

        report_port_init_labels(owner.labels, owner.skip_log, holds_no_init_value=reaches_a_stub)


def incompatible_map_lines(mapping_sets: list[MappingSet], data_types: list[DataType]) -> list[str]:
    """One line per written map SWCraft's supported subset refuses, in set and map order.

    Asked through the validator's own two predicates in its order -- a map reaching
    a pointer (``mapped_pointer``, ``[constr_1066]``), then the positional composite
    check (``composite_map_difference``) -- so a line is exactly a map
    ``check-model`` reports and ``generate arxml`` refuses, one line per cause.
    What it does **not** reach: a side kept as an unresolved path or of a kind the
    side may not name (the unresolved-reference section and
    ``rule:mapping-set.unknown-*`` answer those), and every facet the positional
    check does not judge.
    """
    # The view over this function's own index; the last declaration wins.
    by_name = DataDescription.last_declared(data_types)
    lines: list[str] = []
    for mapping_set in mapping_sets:
        for entry in mapping_set.maps:
            reached = mapped_pointer(entry.implementation, by_name)
            if reached is not None:
                lines.append(
                    f"{mapping_set.name}: {entry.application} -> {entry.implementation}: reaches the pointer "
                    f"{chain_text(reached)}, and an application data type is never mapped to one ([constr_1066])"
                )
                continue
            difference = composite_map_difference(entry.application, entry.implementation, by_name)
            if difference is not None:
                lines.append(f"{mapping_set.name}: {entry.application} -> {entry.implementation}: {difference}")
    return lines


def _stub_member_chain(
    type_name: str,
    type_map: DataDescription,
    stubs: frozenset[str],
    seen: frozenset[str],
) -> tuple[str, tuple[str, ...]] | None:
    """The first member of the implementation composite *type_name* ends at whose type reaches a stub in *stubs*.

    ``(member, chain)``: the member's dotted name inside the composite (an array's
    one element is ``[]``) and ``validator.external_type_chain``'s names from the
    member's type to the stub; ``None`` for every other type. *type_name* is
    followed down its reference chain first; nested composites are walked, and a
    composite met twice is passed over (a cycle is ``data-type.cycle``'s).
    """
    end = implementation_end(type_name, type_map)
    if end is None or end[1].name in seen:
        return None
    declared = end[1]
    seen = seen | {declared.name}
    members: list[tuple[str, str]]
    if isinstance(declared, ImplementationRecordDataType):
        members = [(element.name, element.type) for element in declared.elements]
    elif isinstance(declared, ImplementationArrayDataType):
        members = [("[]", declared.element_type)]
    else:
        return None
    for label, member_type in members:
        chain = external_type_chain(member_type, type_map, application=False)
        if chain is not None and chain[-1] in stubs:
            return label, chain
        inner = _stub_member_chain(member_type, type_map, stubs, seen)
        if inner is not None:
            return f"{label}.{inner[0]}", inner[1]
    return None


def with_stubbed_references(dt: DataType, type_names: Mapping[str, str]) -> DataType:
    """*dt* with every reference in a stub-admitting data-type role re-pointed at its external type stub.

    The roles are ``unresolved.STUB_ROLE_LAYERS``': a reference type's ``base``, an
    implementation record's element ``type``, an implementation array's
    ``element_type`` and a pointer's ``target``. A value type's base, a record
    field's and an application array element's type keep the path even when the
    same path was stubbed for another role.
    """
    if isinstance(dt, ReferenceDataType) and dt.base in type_names:
        return renamed(dt, {"base": type_names[dt.base]})
    if isinstance(dt, ImplementationRecordDataType) and any(e.type in type_names for e in dt.elements):
        return renamed(
            dt,
            {
                "elements": [
                    e.model_copy(update={"type": type_names[e.type]}) if e.type in type_names else e
                    for e in dt.elements
                ]
            },
        )
    if isinstance(dt, ImplementationArrayDataType) and dt.element_type in type_names:
        return renamed(dt, {"element_type": type_names[dt.element_type]})
    if isinstance(dt, PointerDataType) and dt.target in type_names:
        return renamed(dt, {"target": type_names[dt.target]})
    return dt


#: The fallback tag of a same-package skip whose path the reader did not index,
#: as :func:`skip_reserved_device_names` falls back. The three element layers' rows are
#: ``merge.ELEMENT_TAG_BY_LAYER``'s, joined by ``|`` (a ``**`` unpacking of a module name is a
#: widening trigger the coverage field oracle cannot bind).
_PLACEMENT_FALLBACK_TAG = (
    {
        "implementation_types": "DATA-TYPE",
        "base_types": "DATA-TYPE",
        "application_types": "DATA-TYPE",
    }
    | ELEMENT_TAG_BY_LAYER
    | {
        LAYER_MAPPING_SETS: "DATA-TYPE-MAPPING-SET",
        LAYER_INTERFACES: "PORT-INTERFACE",
        LAYER_SWC: "SW-COMPONENT-TYPE",
        LAYER_COMPOSITIONS: "COMPOSITION-SW-COMPONENT-TYPE",
    }
)

_Placeable = TypeVar(
    "_Placeable", bound=DataType | MappingSet | Interface | SWComponent | Composition | DataDescriptionElement
)


@dataclass(frozen=True)
class ImportPlacement:
    """``import model``'s package placement: what moves, what is skipped, and the report's lines."""

    packages: dict[tuple[str, str], str]
    skips: list[ReservedSkip]
    lines: list[str]


def place_final_packages(
    model: ImportModel,
    reconciled: ReconciledModel,
    registry: PathRegistry,
    mapped_sets: MappedSets,
    mapped: MappedCompositions,
    reserved_skips: Sequence[ReservedSkip],
    elements: Sequence[DataDescriptionElement],
) -> ImportPlacement:
    """Package placement over the final element set.

    Runs after mapping, naming and the reserved-name pass, over the elements the
    device-name skip keeps, reading each root package's declared elements with
    their full source paths through the write loop's origin maps.

    First, an element whose source path is exactly ``/{Home}/{package}/{name}``
    -- SWCraft's own layout, the home being a root or, for a data type and a
    mapping set, its layer's package ``/DataTypes/{LayerPackage}`` -- keeps that
    ``package`` (:func:`~swcraft_tools.arxml_import.reconcile.kept_package`), with
    no report line: it is the round trip of the ``package`` the exporting project
    recorded. Then :func:`~swcraft_tools.arxml_import.reconcile.place_case_variants`
    places the root's remaining members: a member it places gets that ``package``;
    the second of two declarations of one source package is skipped with a
    ``[constr_2508]`` line (:func:`~swcraft_tools.arxml_import.report.case_only_duplicate_skip`).

    Nothing else gets a ``package``, and nothing is renamed. A foreign export's
    elements keep nothing in the first step, unless its top-level package is named
    like one of SWCraft's roots, which the rule cannot tell from SWCraft's layout.

    Raises:
        ValueError: a member with no source package to move to -- unreachable here,
            and refused rather than guessed.
    """
    skipped = {(entry.layer, entry.name) for entry in reserved_skips}
    type_paths = registry.source_paths
    # Indexed, never ``.get(..., "")``: a member without a source path would read as
    # declared in a package called "" -- two such members one shared package, one of
    # them skipped with a [constr_2508] line the file never earned -- so a miss is a
    # KeyError, a defect of the origin maps, and never a verdict.
    paths: dict[tuple[str, str], str] = {
        **{data_type_key(dt): type_paths[data_type_key(dt)] for dt in reconciled.data_types},
        # Each element in its layer's package.
        **{data_type_key(element): type_paths[data_type_key(element)] for element in elements},
        **{(LAYER_MAPPING_SETS, s.name): mapped_sets.source_paths[s.name] for s in reconciled.mapping_sets},
        **{
            (LAYER_INTERFACES, i.name): registry.interface_source_paths[
                reconciled.interface_source_names.get(i.name, i.name)
            ]
            for i in reconciled.interfaces
        },
        **{(LAYER_SWC, imported.swc.name): imported.path for imported in reconciled.swcs},
        **{
            (LAYER_COMPOSITIONS, c.name): mapped.composition_source_paths[
                reconciled.swc_source_names.get(c.name, c.name)
            ]
            for c in reconciled.compositions
        },
    }
    by_root = root_package_members(
        [imported.swc for imported in reconciled.swcs if (LAYER_SWC, imported.swc.name) not in skipped],
        [i for i in reconciled.interfaces if (LAYER_INTERFACES, i.name) not in skipped],
        [dt for dt in reconciled.data_types if data_type_key(dt) not in skipped],
        [c for c in reconciled.compositions if (LAYER_COMPOSITIONS, c.name) not in skipped],
        [s for s in reconciled.mapping_sets if (LAYER_MAPPING_SETS, s.name) not in skipped],
        [element for element in elements if data_type_key(element) not in skipped],
    )
    packages: dict[tuple[str, str], str] = {}
    skips: list[ReservedSkip] = []
    lines: list[str] = []
    for home, members in by_root.items():
        kept: dict[tuple[str, str], str] = {}
        for member in members:
            package = kept_package(home, member.name, paths[(member.layer, member.name)])
            if package is not None:
                kept[(member.layer, member.name)] = package
        packages.update(kept)
        decided = place_case_variants(
            home,
            (
                PlacementMember(
                    member.layer,
                    member.name,
                    paths[(member.layer, member.name)],
                    member.generated,
                    kept.get((member.layer, member.name)),
                )
                for member in members
            ),
        )
        if decided.unplaceable:
            raise ValueError(
                "Import aborted with nothing written: no source package to place "
                + "; ".join(
                    f"'{twin.declaration.name}' ({twin.declaration.source_path})" for twin in decided.unplaceable
                )
            )
        for pair in decided.same_package:
            raw = model.path_index.get(pair.dropped.source_path)
            tag = raw.tag if raw is not None else _PLACEMENT_FALLBACK_TAG[pair.dropped.layer]
            skip = case_only_duplicate_skip(
                pair.dropped.source_path,
                tag,
                pair.kept.name,
                pair.kept.source_path,
                variant_rich=model.variant_rich,
            )
            skips.append(ReservedSkip(pair.dropped.layer, pair.dropped.name, skip))
        for placed in decided.placed:
            packages[(placed.declaration.layer, placed.declaration.name)] = placed.package
            lines.append(package_placement_line(placed, home))
    return ImportPlacement(packages, skips, lines)


def with_package(element: _Placeable, package: str | None) -> _Placeable:
    """*element* with its recorded ``package``, or unchanged when package placement gave it none."""
    if package is None:
        return element
    # mypy widens a union-bound type variable through model_copy's Self to the union (a checker limitation)
    return element.model_copy(update={"package": package})  # type: ignore[return-value]


def refuse_package_level_collisions(
    source: str,
    data_types: Sequence[DataType],
    mapping_sets: Sequence[MappingSet],
    interfaces: Sequence[Interface],
    imported_swcs: Sequence[ImportedSwc],
    compositions: Sequence[Composition],
    elements: Sequence[DataDescriptionElement],
) -> None:
    """Refuse, with nothing written, a placed model that still holds a package-level name-space group.

    A fail-closed check (``validator.name_space_collisions`` over the package name
    spaces), so a case the placement rules did not foresee is refused rather than
    written as a project the writer refuses. A group **below** the package level
    is not this import's to refuse: the source's owner is the model's, and such a
    pair imports and is reported by ``check-model``.
    """
    collisions = [
        collision
        for collision in name_space_collisions(
            [imported.swc for imported in imported_swcs],
            interfaces,
            data_types,
            compositions,
            [],
            mapping_sets,
            elements,
        )
        if collision.level == NAME_SPACE_LEVEL_PACKAGE
    ]
    if collisions:
        raise ValueError(
            f"Import aborted with nothing written: '{source}' would leave names equal ignoring case in one "
            "package of the generated ARXML, which package placement could not keep apart -- "
            + "; ".join(collision.sentence for collision in collisions)
        )


# ---------------------------------------------------------------------------
# The asynchronous call findings the file states
# ---------------------------------------------------------------------------

#: The rule each asynchronous call finding the file states breaks, by the validator's rule id.
_ASYNC_CALL_FINDING_RULES: dict[str, StandardRule] = {
    "swc.call.mixed-synchrony": StandardRule.CALL_SYNCHRONY_EXCLUSIVE,
    "swc.call.timeout-mismatch": StandardRule.CALL_TIMEOUTS_IDENTICAL,
    "swc.call.several-results": StandardRule.ONE_RESULT_POINT_PER_CALL,
    "swc.call.async-without-result": StandardRule.ONE_RESULT_POINT_PER_CALL,
    "swc.call.init-runnable-result": StandardRule.NO_RESULT_POINT_IN_AN_INIT_RUNNABLE,
}
#: The tag every finding's line is keyed at: ``map_swcs`` maps application components alone.
_COMPONENT_TAG = "APPLICATION-SW-COMPONENT-TYPE"


def async_call_finding_skips(
    model: ImportModel, imported_swcs: Sequence[ImportedSwc], arxml_version: ArxmlVersion
) -> SkipLog:
    """One section-1 line per asynchronous call finding of the written components that the file states.

    ``validator.async_call_records`` over each final component at the project's
    *arxml_version*, the rules ``check-model`` applies. Each finding is imported as
    the file states it, nothing refused or dropped, and is one export-defect line
    citing its rule with ``check-model``'s message: ``[constr_2022]`` (mixed
    synchrony), ``[constr_2023]`` (timeouts), ``[constr_2006]`` (two result points
    of one call point) and ``[constr_1259]`` (a result point of an init-started
    runnable), keyed at the component's own path and tag.

    ``swc.call.async-without-result`` is the file's defect only when the file
    declares R4.2.2 (where ``[constr_2006]`` asks exactly one result point) **and**
    no result point of the file references the call
    (:attr:`~swcraft_tools.arxml_import.swc_mapping.ImportedSwc.stated_result_calls`);
    a call whose result point the import did not carry already has that point's
    line, and a file of R4.3.0 in a 4.2.2 project is the report's version line. A
    ``swc.call.several-results`` the import's own collapse would cause never
    reaches here (``runnable_accesses.bind_polls`` and ``map_returns_event`` skip
    the second result point). A finding of a rule the file cannot state
    (``unknown-call``, ``ambiguous-call``) would be a section-2 line, never
    dropped. A file declaring no release SWCraft writes, imported into a 4.2.2
    project, gets one ``NOT_SUPPORTED`` line per call with no result point
    (:func:`_no_release_no_result_reason`); a 4.3.0 project gets none.

    What this does **not** reach: a call whose result fetch is a wait point (the
    importer's skip); two synchronous call points of one operation with different
    timeouts, which the validator does not judge; and ``import component``, which
    names its findings in its own report.
    """
    skip_log = SkipLog()
    raw_by_path = {raw.path: raw for raw in model.swcs}
    for imported in imported_swcs:
        raw = raw_by_path.get(imported.path)
        if imported.swc.external or raw is None:
            continue
        for record in async_call_records([imported.swc], arxml_version):
            if record.rule == ASYNC_WITHOUT_RESULT:
                if not _states_no_result(imported, record):
                    continue
                if model.declared_release is None and arxml_version == ArxmlVersion.V4_2_2:
                    skip_log.record_reason(
                        raw.path,
                        _COMPONENT_TAG,
                        SkipReason(_no_release_no_result_reason(record), SkipCategory.NOT_SUPPORTED),
                        effect=ABORT,
                    )
                    continue
                if model.declared_release != ArxmlVersion.V4_2_2:
                    continue
            text = (
                f"{record.message} -- imported as the file states it; the model is one check-model refuses "
                f"(rule:{record.rule})"
            )
            rule = _ASYNC_CALL_FINDING_RULES.get(record.rule)
            reason = (
                SkipReason(text, SkipCategory.NOT_SUPPORTED)
                if rule is None
                else SkipReason(text, SkipCategory.EXPORT_DEFECT, rule)
            )
            skip_log.record_reason(raw.path, _COMPONENT_TAG, reason, effect=ABORT)
    return skip_log


def _states_no_result(imported: ImportedSwc, record: ConsistencyIssue) -> bool:
    """Whether the file itself leaves the call of a ``swc.call.async-without-result`` *record* without a result.

    No result point of the file may reference the
    call (:attr:`~swcraft_tools.arxml_import.swc_mapping.ImportedSwc.stated_result_calls`;
    a call the stub decision held is keyed by its source operation). The call is
    :func:`~swcraft_tools.validator.async_call_subject`'s, the one decoder this module
    shares with ``component_exchange.import_``.
    """
    return async_call_subject(record) not in imported.stated_result_calls


def _no_release_no_result_reason(record: ConsistencyIssue) -> str:
    """The line of a call with no result point in a file that declares no release SWCraft writes.

    The file's release is unknown, so no standard text is cited: the line names the
    call (``check-model``'s message), the rule id, that the project's 4.2.2 rules
    apply, and the three repairs. Path-free.
    """
    return (
        f"{record.message} -- imported as the file states it; the file declares no AUTOSAR release SWCraft "
        "writes, so this project's arxml_version 4.2.2 rules apply, and check-model refuses the model "
        f"(rule:{record.rule}); set poll: true on the call, give it a call_returned event, or set "
        "arxml_version: 4.3.0 in swcraft.yaml"
    )
