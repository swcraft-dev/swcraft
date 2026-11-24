"""Composition mapping and connector import.

:func:`map_compositions` maps every COMPOSITION-SW-COMPONENT-TYPE onto one ``compositions/`` element with its
boundary ports, members and delegations, and every assembly connector onto a connector naming the composition it
is drawn inside. Nothing is flattened: a delegation stays a delegation.

A prototype whose ``TYPE-TREF`` names no component or composition this import maps becomes an **external stub**: a
component carrying the source's ``TYPE-TREF`` and ``DEST`` verbatim, named ``<leaf>_ext<X>`` by
:func:`~swcraft_tools.arxml_import.registry.stub_names` (X ranks the namesakes by sorted ``TYPE-TREF``, passing
over a claimed name) and recorded on the shared ``PathRegistry`` as a rename. That is the one name minted here. A
blank ``TYPE-TREF``, or one whose reference pair cannot be carried, mints no stub: the prototype is skipped and
reported.

A stub's ports are minted from the connector ends that reach it, by name and direction and never with an
interface. An end mints a port only when its target port path's parent is the stub's ``TYPE-TREF``
(``[TPS_GST_00162]``, the test a mapped member's end meets too), its last segment is a legal ``Identifier``, and
its ``DEST`` is a P- or R-port prototype agreeing with the element holding it; two ends stating contradicting
directions for one port mint none. Any other end on a stub is skipped and reported. :func:`finish_stub_ports`
runs last: it drops a minted port no kept connector reaches and adds the ``observed_interface`` hint.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import ValidationError

from swcraft_tools.arxml_import.merge import (
    CONFLICT,
    CONFLICTING,
    EXCLUDED,
    LAYER_SWC,
    MAP,
    SELECTED,
    GroupKey,
    MergeDecisions,
    chained_reason,
    declared_paths,
)
from swcraft_tools.arxml_import.registry import PathRegistry, stub_name, stub_names
from swcraft_tools.arxml_import.swc_mapping import (
    HeldStubComSpecs,
    ImportedSwc,
    PortInitEquivalent,
    PortInitLabels,
    build_interface_path_index,
    held_stub_com_specs,
    map_ports,
    port_init_equivalents,
    port_init_labels,
)
from swcraft_tools.arxml_import.unresolved import UnresolvedReferences
from swcraft_tools.arxml_reader import (
    AssemblyConnectorInfo,
    CompositionInfo,
    ConnectorPortRef,
    DelegationConnectorInfo,
    ImportModel,
    SkipCategory,
    SkipLog,
    SkipReason,
    StandardRule,
    SwcInfo,
    blank_short_name,
    duplicate_name_reason,
    not_supported,
)
from swcraft_tools.identifier import check_identifier
from swcraft_tools.schema import (
    INTERFACE_KIND_PROSE,
    Composition,
    CompositionMember,
    CompositionPort,
    Connector,
    ConnectorEndpoint,
    Delegation,
    Interface,
    Port,
    SWComponent,
)
from swcraft_tools.validator.compositions import DEST_TOKEN
from swcraft_tools.validator.data_types import OWN_PACKAGE_PHRASE, REF_PATH, own_package_of

#: The ``swc/{group}/`` directory every external stub is written into. One directory rather than the type's own
#: parent package: a stub points outside the imported model, and a package this project does not carry would read
#: as if it did.
EXTERNAL_STUB_GROUP = "external"

_COMPOSITION_TAG = "COMPOSITION-SW-COMPONENT-TYPE"
_PROTOTYPE_TAG = "SW-COMPONENT-PROTOTYPE"
_ASSEMBLY_TAG = "ASSEMBLY-SW-CONNECTOR"
_DELEGATION_TAG = "DELEGATION-SW-CONNECTOR"

#: The two directions a port takes.
_PortDirection = Literal["provide", "require"]


def raw_swc_index(model: ImportModel) -> dict[str, SwcInfo]:
    """ARXML path -> `SwcInfo` for every APPLICATION-SW-COMPONENT-TYPE in `model`."""
    return {s.path: s for s in model.swcs}


def composition_index(model: ImportModel) -> dict[str, CompositionInfo]:
    """ARXML path -> `CompositionInfo` for every COMPOSITION-SW-COMPONENT-TYPE in `model`."""
    return {c.path: c for c in model.compositions}


@dataclass
class _MappedMember:
    """One member of one composition, beside what its type resolved to.

    ``field_name`` is ``"type"`` or ``"composition"`` -- the model's spelling of the ``TYPE-TREF``'s ``DEST`` --
    and ``type_path`` is the referenced type's ARXML path, so a port reference is checked against the type it
    claims to name rather than matched on a bare last segment.
    """

    member: CompositionMember
    field_name: str
    type_name: str
    type_path: str
    #: True when the type resolved to an external stub: its ends mint the stub's ports (:func:`_stub_end`), and
    #: nothing else about the type is known.
    external: bool


@dataclass
class MappedCompositions:
    """The mapped result of one import run.

    :func:`map_compositions` returns its ``SkipLog`` beside this object, and the names it mints reach the shared
    ``PathRegistry``.
    """

    compositions: list[Composition]
    connectors: list[Connector]
    #: The external stubs, as ``ImportedSwc`` so they travel the component pipeline (``--packages``, reserved-name
    #: reconciliation, the write loop) like a mapped component.
    stubs: list[ImportedSwc] = field(default_factory=list)
    #: The source ``TYPE-TREF`` of each stub -> its report line. Keyed so a caller that drops a stub
    #: (``--packages``) drops its line: a report must not name a component the project never receives.
    stub_lines: dict[str, str] = field(default_factory=dict)
    #: Composition name -> the ARXML path of every assembly connector and delegation inside it skipped because an
    #: end on an external stub names a target port path the context prototype's type does not own
    #: ([TPS_GST_00162]) -- an export defect, never repaired by minting a port from the last segment. Counted only
    #: where that end is why the connector fell. Keyed by composition because ``--packages`` can drop a whole one,
    #: and a count inside a composition the project never receives would be a claim with nothing behind it.
    unowned_port_paths: dict[str, list[str]] = field(default_factory=dict)
    #: The source ``TYPE-TREF`` of each stub -> one line per port the export states with contradicting directions.
    #: No port is minted for such a name and every connector reaching it is skipped; keyed as :attr:`stub_lines`.
    stub_port_contradictions: dict[str, list[str]] = field(default_factory=dict)
    #: Connector name -> the ARXML path of the declaration mapped under it; a skipped connector is not in it. With
    #: :attr:`composition_source_paths` it is the write order for the two layers, so the export decides which of
    #: two elements differing only in case gets the plain file name.
    connector_source_paths: dict[str, str] = field(default_factory=dict)
    #: Composition name, before any reserved-name rename -> its ARXML path.
    composition_source_paths: dict[str, str] = field(default_factory=dict)
    #: Composition name -> one ``(port, data element, report key)`` per own init value a boundary port carries by
    #: equivalence with its element's (``swc_mapping.port_init_equivalents``).
    port_init_equivalents: dict[str, list[PortInitEquivalent]] = field(default_factory=dict)
    #: Composition name -> the held ``SHORT-LABEL`` lines of its boundary ports' own init values
    #: (``swc_mapping.port_init_labels``), reported once the stub passes decide which com-specs still hold one.
    port_init_labels: dict[str, list[PortInitLabels]] = field(default_factory=dict)
    #: Composition name -> its boundary ports' com-specs held for the stub decision
    #: (``swc_mapping.held_stub_com_specs``).
    held_stub_com_specs: dict[str, list[HeldStubComSpecs]] = field(default_factory=dict)

    #: No ``root_name`` field: ``--packages`` and the reserved-name pass change the compositions after this, so the
    #: root is read off the final set's ``root`` flags.


def derive_root(model: ImportModel, merges: MergeDecisions | None = None) -> CompositionInfo | None:
    """The composition no prototype instantiates -- the root of the hierarchy.

    AUTOSAR does not mark a root: nesting is by instantiation, so the root is the composition nothing
    instantiates. Under the merge verdict every path is collapsed onto its group's primary first, so two merged
    declarations of one name are one element, and a declaration the verdict does not import (``CONFLICT`` or
    ``EXCLUDED``) is no candidate. With ``merges=None`` every declaration counts on its own.

    Zero or two roots is an import error naming them, never a guess: ``rule:composition.no-root`` and
    ``rule:composition.multiple-roots`` would refuse the project. An export with no composition at all answers
    ``None``: nothing is written into ``compositions/``.

    What this does **not** reach: an export whose only root candidates belong to a conflicting group refuses the
    whole import rather than reporting the group under *Conflicting declarations*.

    Raises:
        ValueError: two or more compositions are instantiated by no prototype,
            or every root candidate was refused by the verdict.
    """
    if not model.compositions:
        return None
    by_path = {comp.path: comp for comp in model.compositions}

    def primary(path: str) -> str:
        return merges.primary_path(path) if merges is not None else path

    def refused_by_the_verdict(path: str) -> bool:
        return merges is not None and merges.outcome(path) in (CONFLICT, EXCLUDED)

    referenced = {primary(proto.type_ref) for comp in model.compositions for proto in comp.prototypes}
    unreferenced: list[CompositionInfo] = []
    refused: list[str] = []
    seen: set[str] = set()
    for comp in model.compositions:
        key = primary(comp.path)
        if key in referenced or key in seen:
            continue
        seen.add(key)
        if refused_by_the_verdict(comp.path):
            refused.append(comp.path)
            continue
        # `by_path[key]` and not `comp`: for a merged group the primary is the
        # member that is mapped, and it need not be the one this loop met first.
        unreferenced.append(by_path.get(key, comp))
    if len(unreferenced) == 1:
        return unreferenced[0]
    if not unreferenced and refused:
        raise ValueError(
            "no root composition: the only composition(s) instantiated by no prototype ("
            + ", ".join(sorted(refused))
            + ") were not imported -- every declaration of that name differs from the others, so "
            "nothing of the group was imported"
        )
    if not unreferenced:
        raise ValueError(
            "every COMPOSITION-SW-COMPONENT-TYPE is instantiated by some prototype, so no "
            "composition is the root of the hierarchy (a cycle, or a partial export)"
        )
    raise ValueError(
        "no single root composition: "
        + ", ".join(sorted(comp.path for comp in unreferenced))
        + " are each instantiated by no prototype, and exactly one must be"
    )


def _unusable_reference(type_ref: str, dest: str | None) -> str | None:
    """Why an unresolvable ``(TYPE-TREF, DEST)`` pair cannot become an external stub, or ``None``.

    A stub carries the pair verbatim onto the prototype the writer emits, so a pair SWCraft's own gate rejects
    must not be imported. The checks are ``validator``'s own, so this guard is never narrower or stricter than
    ``rule:swc.external-incomplete-reference`` (the two shapes) and
    ``rule:swc.external-reference-inside-generated-package`` (a path inside a root package the writer declares the
    project's own elements in, which would bind every prototype of the stub to one of them).

    Returns:
        A phrase for the skip line, or ``None`` when the pair is usable.
    """
    if not REF_PATH.fullmatch(type_ref):
        return "is not an AUTOSAR reference path"
    package = own_package_of(type_ref)
    if package is not None:
        return f"lies inside {package}, {OWN_PACKAGE_PHRASE}"
    if dest is None or not dest.strip():
        return "declared with no DEST attribute"
    if not DEST_TOKEN.fullmatch(dest):
        return f"declared with DEST {dest!r}, which is not an AUTOSAR reference DEST"
    return None


@dataclass
class _ExternalStubs:
    """The external stubs :func:`_mint_external_stubs` mints, and the prototype types it cannot stub."""

    stubs: list[ImportedSwc] = field(default_factory=list)
    #: The source ``TYPE-TREF`` of each stub -> its report line.
    stub_lines: dict[str, str] = field(default_factory=dict)
    #: The source ``TYPE-TREF`` of each stub -> the stub's name.
    stub_name_by_type_ref: dict[str, str] = field(default_factory=dict)
    #: Every unresolvable ``TYPE-TREF`` whose reference pair cannot be carried -> the reason.
    unusable_reference_type_refs: dict[str, str] = field(default_factory=dict)


@dataclass
class _PlacedCompositions:
    """What :func:`_map_ports_and_members` builds for every mapped composition."""

    #: Composition path -> its kept boundary ports.
    ports_by_composition: dict[str, list[CompositionPort]] = field(default_factory=dict)
    #: Composition name -> its boundary ports' init values carried by equivalence.
    port_init_equivalents_by_name: dict[str, list[PortInitEquivalent]] = field(default_factory=dict)
    #: Composition name -> its boundary ports' held ``SHORT-LABEL`` lines.
    port_init_labels_by_name: dict[str, list[PortInitLabels]] = field(default_factory=dict)
    #: Composition name -> its boundary ports' com-specs held for the stub decision.
    held_com_specs_by_name: dict[str, list[HeldStubComSpecs]] = field(default_factory=dict)
    #: Composition path -> boundary port path -> the kept port's name.
    outer_port_name_by_path: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Composition path -> its placed members, in declaration order.
    members_by_composition: dict[str, list[_MappedMember]] = field(default_factory=dict)
    #: Prototype path -> the member placed for it.
    member_by_proto_path: dict[str, _MappedMember] = field(default_factory=dict)


@dataclass(frozen=True)
class _PlacementContext:
    """What the stub minting and pass 1 read and never change: the call's arguments and the name indices."""

    model: ImportModel
    merges: MergeDecisions | None
    registry: PathRegistry
    unresolved: UnresolvedReferences | None
    #: The skip log of the whole :func:`map_compositions` call.
    skip_log: SkipLog
    #: ``map_interfaces``' own skip log, where a boundary port chained behind a skipped interface finds its entry.
    iface_skip_log: SkipLog
    #: ``map_interfaces``' output, for the boundary ports.
    mapped_interfaces: list[Interface]
    #: ARXML path -> emitted interface name, for the interfaces that survived.
    interface_path_to_name: dict[str, str]
    #: Every path the file declares.
    declared: frozenset[str]
    #: ARXML path -> the mapped component, a merged declaration's path included.
    imported_by_path: dict[str, ImportedSwc]
    #: Composition path -> the name it is mapped under.
    composition_name_by_path: dict[str, str]
    #: Every reference a prototype may name a mapped composition by -> its name.
    composition_name_by_reference: dict[str, str]


@dataclass(frozen=True)
class _StubPorts:
    """What :func:`_mint_stub_ports` answers: the stubs with their ports, and the ports it could not mint."""

    #: ``(stub TYPE-TREF, port name)`` of every stub port the export states with two directions.
    contradictory: frozenset[tuple[str, str]]
    #: Stub ``TYPE-TREF`` -> one report line per such port.
    contradiction_lines: dict[str, list[str]]
    #: The external stubs, each with its minted ports.
    stubs: list[ImportedSwc]


@dataclass(frozen=True)
class _CompositionContext:
    """What the connector mapping reads and never changes: the indices built before the first connector."""

    model: ImportModel
    merges: MergeDecisions | None
    #: The skip log of the whole :func:`map_compositions` call.
    skip_log: SkipLog
    #: Where a chained end's root is searched, nearest first (`merge.chain_root`): this
    #: pass's own log (prototypes and boundary ports), `map_swcs`' (a member's ports), and
    #: the reader's last.
    chain_logs: tuple[SkipLog, SkipLog, SkipLog]
    #: Every path the file declares.
    declared: frozenset[str]
    #: The path of every composition the file declares.
    composition_paths: frozenset[str]
    #: The path of every port of every component and composition type the reader handed over.
    port_paths: frozenset[str]
    #: ARXML path -> the mapped component, a merged declaration's path included.
    imported_by_path: dict[str, ImportedSwc]
    #: Every composition's kept boundary ports and placed members.
    placed: _PlacedCompositions
    #: ``(stub TYPE-TREF, port name)`` of every stub port the export states with two directions.
    contradictory: frozenset[tuple[str, str]]
    #: The kind of every mapped interface, by the name a port names it by.
    interface_kind_by_name: dict[str, str]


@dataclass(frozen=True)
class _CompositionScope:
    """One composition while its connectors are mapped; built anew for each composition."""

    comp: CompositionInfo
    name: str
    #: The path of every prototype the composition declares.
    prototype_paths: frozenset[str]
    #: Boundary port path -> the kept port's name.
    outer_ports: dict[str, str]
    #: Kept boundary port name -> its direction.
    outer_directions: dict[str, str]


@dataclass
class _CompositionState:
    """What one composition's connector mapping fills; created anew for each composition."""

    #: Connector and delegation name -> the declaration that claimed it: one name space
    #: ([constr_2508]).
    local_names: dict[str, str] = field(default_factory=dict)
    #: The mirror of `rule:composition.duplicate-delegation-link`, keyed on the triple the
    #: rule keys on; one triple in two compositions is two different `CONNECTORS` blocks
    #: and the legal shape.
    seen_delegation_links: dict[tuple[str, str, str], str] = field(default_factory=dict)
    #: The delegations kept, in document order.
    delegations: list[Delegation] = field(default_factory=list)


@dataclass
class _ConnectorAccumulator:
    """What the connector mapping fills across the compositions, for the whole call."""

    connectors: list[Connector] = field(default_factory=list)
    delegations_by_composition: dict[str, list[Delegation]] = field(default_factory=dict)
    #: Composition name -> the path of every connector skipped for an end the context's type does not own.
    unowned_port_paths: dict[str, list[str]] = field(default_factory=dict)
    #: Connector name -> the path of the declaration mapped (or held) under it.
    seen_connector_names: dict[str, str] = field(default_factory=dict)
    #: Every MERGED assembly-connector group one member of which this mapping has
    #: already taken.
    merged_connector_groups: set[GroupKey] = field(default_factory=set)
    #: The importer's mirror of `rule:connector.duplicate-link`'s key -> the path that declared it.
    seen_links: dict[tuple[str, str, str, str, str, str, str], str] = field(default_factory=dict)


def map_compositions(
    model: ImportModel,
    imported_swcs: list[ImportedSwc],
    interfaces: list[Interface],
    iface_skip_log: SkipLog,
    registry: PathRegistry,
    merges: MergeDecisions | None = None,
    unresolved: UnresolvedReferences | None = None,
    *,
    swc_skip_log: SkipLog,
) -> tuple[MappedCompositions, SkipLog]:
    """Map every COMPOSITION-SW-COMPONENT-TYPE onto the ``compositions/`` layer.

    One :class:`~swcraft_tools.schema.Composition` per composition the export declares, in document order, with
    its boundary ports, members (``SW-COMPONENT-PROTOTYPE``) and delegations (``DELEGATION-SW-CONNECTOR``), and
    one :class:`~swcraft_tools.schema.Connector` per ``ASSEMBLY-SW-CONNECTOR`` naming the composition it is drawn
    inside. Every rule skips and reports the element it refuses rather than raising. Reachability from the root and
    ``Identifier`` conformance are not checked here.

    Args:
        model: the whole read export.
        imported_swcs: ``map_swcs``' output -- what "resolvable in the imported model" means for a prototype's
            type.
        interfaces: ``map_interfaces``' output, for the boundary ports.
        iface_skip_log: ``map_interfaces``' own skip log, which builds the interface path index and where a
            boundary port chained behind a skipped interface finds its entry.
        registry: the shared ``PathRegistry``; a stub whose derived name is already claimed is recorded on it as
            a rename.
        merges: the merge verdict; with ``None`` every declaration is mapped on its own.
        unresolved: the unresolved-reference policy, handed on for the boundary ports.
        swc_skip_log: ``map_swcs``' own skip log, from which a connector or delegation end chained behind a
            member's port that ``map_swcs`` skipped inherits that port's entry.

    Returns:
        ``(MappedCompositions, skip_log)``.

    Raises:
        ValueError: the export has no single root composition (:func:`derive_root`), or the root did not survive
            the name rules.
    """
    skip_log = SkipLog()
    chain_logs = (skip_log, swc_skip_log, model.skip_log)
    root = derive_root(model, merges)
    interface_path_to_name = build_interface_path_index(model, interfaces, iface_skip_log, merges)
    imported_by_path = _imported_by_path(imported_swcs, merges)
    claimed, composition_name_by_path = _claim_composition_names(model, imported_swcs, merges, skip_log)
    composition_name_by_reference = _composition_name_by_reference(composition_name_by_path, merges)
    _refuse_a_lost_root(root, composition_name_by_path, skip_log)
    placement = _PlacementContext(
        model=model,
        merges=merges,
        registry=registry,
        unresolved=unresolved,
        skip_log=skip_log,
        iface_skip_log=iface_skip_log,
        mapped_interfaces=interfaces,
        interface_path_to_name=interface_path_to_name,
        # `map_ports` splits an unresolved boundary-port interface by it.
        declared=declared_paths(model),
        imported_by_path=imported_by_path,
        composition_name_by_path=composition_name_by_path,
        composition_name_by_reference=composition_name_by_reference,
    )
    external = _mint_external_stubs(placement, claimed)

    # --- pass 1: ports and members -----------------------------------------
    composition_paths = frozenset(comp.path for comp in model.compositions)
    # Every port the reader handed over, kept or not: the one set a connector end or a delegation's outer port may
    # chain on; any other declared path was never a skipped port and inherits nothing. What it does not reach: a
    # port kind the reader refuses itself is not in it, and gets the not-a-port sentence.
    owners: tuple[SwcInfo | CompositionInfo, ...] = (*model.swcs, *model.compositions)
    port_paths = frozenset(port.path for owner in owners for port in owner.ports)
    placed = _map_ports_and_members(placement, external)

    # A port whose interface the file does not declare carries the kept full path or, on an external stub, no
    # interface at all: neither is here.
    interface_kind_by_name: dict[str, str] = {interface.name: interface.kind for interface in interfaces}

    stub_ports = _mint_stub_ports(placement, placed, external.stubs)

    ctx = _CompositionContext(
        model=model,
        merges=merges,
        skip_log=skip_log,
        chain_logs=chain_logs,
        declared=placement.declared,
        composition_paths=composition_paths,
        port_paths=port_paths,
        imported_by_path=placement.imported_by_path,
        placed=placed,
        contradictory=stub_ports.contradictory,
        interface_kind_by_name=interface_kind_by_name,
    )
    # --- pass 2: delegations and assembly connectors -----------------------
    # A second pass because a member's type may be a composition declared later
    # in the document, and its ports are what an inner reference resolves against.
    acc = _ConnectorAccumulator()
    for comp in model.compositions:
        composition_name = composition_name_by_path.get(comp.path)
        if composition_name is None:
            continue
        _map_composition_connectors(ctx, acc, comp, composition_name)

    compositions = _build_compositions(model, root, composition_name_by_path, placed, acc.delegations_by_composition)

    return (
        MappedCompositions(
            compositions=compositions,
            connectors=acc.connectors,
            stubs=stub_ports.stubs,
            stub_lines=external.stub_lines,
            unowned_port_paths=acc.unowned_port_paths,
            stub_port_contradictions=stub_ports.contradiction_lines,
            connector_source_paths=dict(acc.seen_connector_names),
            composition_source_paths={name: path for path, name in composition_name_by_path.items()},
            port_init_equivalents=placed.port_init_equivalents_by_name,
            port_init_labels=placed.port_init_labels_by_name,
            held_stub_com_specs=placed.held_com_specs_by_name,
        ),
        skip_log,
    )


def _imported_by_path(imported_swcs: list[ImportedSwc], merges: MergeDecisions | None) -> dict[str, ImportedSwc]:
    """ARXML path -> the mapped component, with every merged declaration's path mapped onto its primary's."""
    imported_by_path = {imported.path: imported for imported in imported_swcs}
    # A prototype naming a component declaration merged into another finds the element mapped in its place.
    if merges is not None:
        for alias, primary in merges.alias_paths().items():
            if primary in imported_by_path:
                imported_by_path.setdefault(alias, imported_by_path[primary])
    return imported_by_path


def _claim_composition_names(
    model: ImportModel,
    imported_swcs: list[ImportedSwc],
    merges: MergeDecisions | None,
    skip_log: SkipLog,
) -> tuple[set[str], dict[str, str]]:
    """Claim each composition's name, before anything resolves against them.

    Skip+reports a composition whose ``SHORT-NAME`` is blank or already declared by a **component** -- the two
    are siblings in ``/ComponentTypes`` and share one ``SHORT-NAME`` scope. Under the merge verdict a conflicting
    group claims its name and imports nothing and a merged group's other members are not mapped; with no verdict
    a second composition of one name takes the same skip.

    Returns:
        ``(claimed, composition_name_by_path)``: every name the components and
        compositions claim, and the path -> name of every composition mapped.
    """
    claimed: set[str] = {imported.swc.name for imported in imported_swcs}
    # A component group the verdict decided CONFLICTING imports nothing, so its name is in no `ImportedSwc`; it is
    # claimed all the same, or a composition of the contested name would get through and vanish from the next
    # import once the export is repaired. A conflicting composition group claims its name in the loop below.
    if merges is not None:
        claimed |= {group.key.name for group in merges.conflicting if group.key.layer == LAYER_SWC}
    composition_name_by_path: dict[str, str] = {}
    for comp in model.compositions:
        if not comp.name or not comp.name.strip():
            skip_log.record_reason(
                comp.path,
                _COMPOSITION_TAG,
                blank_short_name("composition has missing or blank SHORT-NAME"),
                effect="abort",
            )
            continue
        if merges is not None:
            outcome = merges.outcome(comp.path)
            if outcome == CONFLICT:
                # The name stays claimed, so a component of that name is refused as if the group had imported.
                # No skip entry: the group carries a record of its own.
                claimed.add(comp.name)
                continue
            if outcome != MAP:
                # A merged group's other member: reachable by reference through `composition_name_by_reference`.
                continue
        # The cross-kind collision; `claimed` also holds every composition name accepted or conflicted above.
        # Under a verdict a second composition of one name never arrives here, because every declaration of one
        # (layer, kind, SHORT-NAME) is one group with at most one `MAP` member.
        if comp.name in claimed:
            skip_log.record(
                comp.path,
                _COMPOSITION_TAG,
                f"duplicate component-type name '{comp.name}' -- a composition and a component are "
                "siblings in /ComponentTypes and cannot share one SHORT-NAME",
                category=SkipCategory.NOT_SUPPORTED,
                effect="abort",
            )
            continue
        claimed.add(comp.name)
        composition_name_by_path[comp.path] = comp.name
    return claimed, composition_name_by_path


def _composition_name_by_reference(
    composition_name_by_path: dict[str, str], merges: MergeDecisions | None
) -> dict[str, str]:
    """Composition path -> mapped name, every merged declaration's path mapped onto its primary's."""
    # Separate from `composition_name_by_path`, which the mapping loop iterates and `composition_source_paths`
    # inverts: one map for both would write a merged group twice.
    composition_name_by_reference = dict(composition_name_by_path)
    if merges is not None:
        for alias, primary in merges.alias_paths().items():
            if primary in composition_name_by_path:
                composition_name_by_reference.setdefault(alias, composition_name_by_path[primary])
    return composition_name_by_reference


def _refuse_a_lost_root(
    root: CompositionInfo | None, composition_name_by_path: dict[str, str], skip_log: SkipLog
) -> None:
    """Refuse an import whose root composition the name rules dropped.

    Raises:
        ValueError: :func:`derive_root` answered a composition that
            :func:`_claim_composition_names` did not map, so the import would
            write a hierarchy in which no composition declares ``root: true``.
    """
    # Refused rather than skip+reported: this is not a construct SWCraft cannot carry but an import whose project
    # the gate would reject, which is malformed input.
    if root is not None and root.path not in composition_name_by_path:
        # The first entry at the root's path is the one that dropped it.
        root_entry = skip_log.first_at(root.path)
        root_reason = root_entry.reason if root_entry is not None else "no reason was recorded"
        raise ValueError(
            f"the root composition {root.path} did not survive mapping "
            f"({root_reason}), so this import would write a hierarchy in "
            "which no composition declares root: true -- a project "
            "`rule:composition.no-root` refuses"
        )


def _mint_external_stubs(placement: _PlacementContext, claimed: set[str]) -> _ExternalStubs:
    """Mint one external stub per prototype type unresolvable in the imported model.

    A stub carries the source's ``TYPE-TREF`` and ``DEST`` verbatim and is named
    ``<leaf>_ext<X>``, X ranked over the namesakes by sorted ``TYPE-TREF``; each
    name is added to *claimed* and recorded on the registry as a rename. A
    ``TYPE-TREF`` whose reference pair cannot be carried
    (:func:`_unusable_reference`) is recorded with its reason and reported by
    :func:`_place_members`, once per prototype.
    """
    model = placement.model
    registry = placement.registry
    skip_log = placement.skip_log
    imported_by_path = placement.imported_by_path
    composition_name_by_path = placement.composition_name_by_path
    composition_name_by_reference = placement.composition_name_by_reference
    external = _ExternalStubs()
    stubs = external.stubs
    stub_lines = external.stub_lines
    stub_name_by_type_ref = external.stub_name_by_type_ref
    # Recorded here and reported by `_place_members`, so one prototype produces one skip line.
    unusable_reference_type_refs = external.unusable_reference_type_refs
    # X ranks the namesakes by sorted `TYPE-TREF`, so the stubs are named once all are known.
    pending: dict[str, tuple[str, str]] = {}
    for comp in model.compositions:
        if comp.path not in composition_name_by_path:
            continue
        for proto in comp.prototypes:
            if not proto.type_ref.strip():
                continue  # reported by `_place_members`
            if proto.type_ref in imported_by_path or proto.type_ref in composition_name_by_reference:
                continue
            if proto.type_ref in pending:
                continue
            if proto.type_ref in unusable_reference_type_refs:
                continue
            unusable = _unusable_reference(proto.type_ref, proto.dest)
            if unusable is not None:
                unusable_reference_type_refs[proto.type_ref] = unusable
                continue  # reported by `_place_members`
            leaf = proto.type_ref.rsplit("/", 1)[-1] or proto.name
            # Checked BEFORE ranking, on the rank-0 name, so a refused stub takes no rank. Defensive: no
            # input that passed `_unusable_reference` (identifier segments only) is known to reach it.
            name, _ = stub_name(leaf, 0, ())
            try:
                SWComponent(name=name, external=True, external_ref=proto.type_ref, external_dest=proto.dest)
            except ValidationError as exc:
                skip_log.record(
                    proto.path,
                    _PROTOTYPE_TAG,
                    f"external stub name {name!r} derived from {proto.type_ref} is invalid: {exc}",
                    category=SkipCategory.NOT_SUPPORTED,
                    effect="abort",
                )
                continue
            pending[proto.type_ref] = (leaf, proto.dest)
    ranked = stub_names({type_ref: leaf for type_ref, (leaf, _) in pending.items()}, claimed)
    for type_ref, (leaf, dest) in pending.items():
        minted = ranked[type_ref]
        name = minted.name
        stub = SWComponent(name=name, external=True, external_ref=type_ref, external_dest=dest)
        claimed.add(name)
        stub_name_by_type_ref[type_ref] = name
        stubs.append(ImportedSwc(swc=stub, group=EXTERNAL_STUB_GROUP, path=type_ref))
        # `moved=False`: the stub never carried the leaf, so a component holding it is not this line's element.
        registry.record_rename(leaf, name, f"{minted.reason} -- the stub of {type_ref}", layer=LAYER_SWC, moved=False)
        stub_lines[type_ref] = (
            f"{type_ref} (DEST {dest}) is not resolvable in the imported "
            f"model -- imported as external stub '{name}', declaring only the ports its "
            "connectors reach, by name and direction"
        )
    return external


def _map_ports_and_members(placement: _PlacementContext, external: _ExternalStubs) -> _PlacedCompositions:
    """Map every mapped composition's boundary ports and place its members.

    A boundary port is skipped by the same rules, through the same ``map_ports``, as a component's ports.
    """
    model = placement.model
    registry = placement.registry
    placed = _PlacedCompositions()
    for comp in model.compositions:
        name = placement.composition_name_by_path.get(comp.path)
        if name is None:
            continue
        raw_ports, port_by_path = map_ports(
            name,
            comp.ports,
            placement.interface_path_to_name,
            placement.skip_log,
            placement.unresolved,
            field_id="compositions.composition_port.interface",
            variant_rich=model.variant_rich,
            declared=placement.declared,
            chain_logs=(placement.iface_skip_log, model.skip_log),
            report_interfaces={interface.name: interface for interface in placement.mapped_interfaces},
            port_inits=registry.port_inits,
        )
        placed.ports_by_composition[comp.path] = [_composition_port(port) for port in raw_ports]
        placed.outer_port_name_by_path[comp.path] = {
            path: outcome.port.name for path, outcome in port_by_path.items() if outcome.port is not None
        }
        if equivalents := port_init_equivalents(port_by_path):
            placed.port_init_equivalents_by_name[name] = equivalents
        if labels := port_init_labels(port_by_path):
            placed.port_init_labels_by_name[name] = labels
        if held := held_stub_com_specs(port_by_path):
            placed.held_com_specs_by_name[name] = held

        placed.members_by_composition[comp.path] = _place_members(
            placement, comp, name, external, placed.member_by_proto_path
        )
    return placed


def _composition_port(port: Port) -> CompositionPort:
    """The boundary port of the same four fields as one port ``map_ports`` kept."""
    # `map_ports` keeps no port whose interface it could not name.
    assert port.interface is not None
    return CompositionPort(
        name=port.name,
        direction=port.direction,
        interface=port.interface,
        description=port.description,
    )


def _place_members(
    placement: _PlacementContext,
    comp: CompositionInfo,
    name: str,
    external: _ExternalStubs,
    member_by_proto_path: dict[str, _MappedMember],
) -> list[_MappedMember]:
    """Place one composition's members, in declaration order, each into *member_by_proto_path* too.

    Skip+reports a prototype with no ``TYPE-TREF``, an unresolvable type whose reference pair a stub cannot carry
    (:func:`_unusable_reference`), a blank or duplicate ``SHORT-NAME``, or a type its composition already places
    (``rule:composition.member-type-repeated`` -- a connector endpoint names the type, so a second placement would
    make the writer's member resolution ambiguous).
    """
    model = placement.model
    skip_log = placement.skip_log
    imported_by_path = placement.imported_by_path
    composition_name_by_reference = placement.composition_name_by_reference
    stub_name_by_type_ref = external.stub_name_by_type_ref
    unusable_reference_type_refs = external.unusable_reference_type_refs
    mapped_members: list[_MappedMember] = []
    seen_member_names: set[str] = set()
    placed_types: set[tuple[str, str]] = set()
    for proto in comp.prototypes:
        if not proto.name or not proto.name.strip():
            skip_log.record_reason(
                proto.path,
                _PROTOTYPE_TAG,
                blank_short_name("component prototype has missing or blank SHORT-NAME"),
                effect="abort",
            )
            continue
        if proto.name in seen_member_names:
            # One composition's COMPONENTS share its name space: [constr_2508], unless the file is variant-rich.
            duplicate = duplicate_name_reason(
                f"duplicate member name '{proto.name}' in composition '{name}'",
                variant_rich=model.variant_rich,
            )
            skip_log.record_reason(proto.path, _PROTOTYPE_TAG, duplicate, effect="abort")
            continue
        if not proto.type_ref.strip():
            # A whitespace-only TYPE-TREF is "no TYPE-TREF" here and in `_mint_external_stubs`, so one prototype
            # produces one skip line.
            skip_log.record(
                proto.path,
                _PROTOTYPE_TAG,
                "component prototype has no TYPE-TREF",
                category=SkipCategory.NOT_SUPPORTED,
                effect="abort",
            )
            continue
        imported = imported_by_path.get(proto.type_ref)
        external_member = False
        if imported is not None:
            field_name, type_name, type_path = "type", imported.swc.name, proto.type_ref
        elif proto.type_ref in composition_name_by_reference:
            field_name, type_name, type_path = (
                "composition",
                composition_name_by_reference[proto.type_ref],
                proto.type_ref,
            )
        elif proto.type_ref in stub_name_by_type_ref:
            field_name, type_name, type_path = "type", stub_name_by_type_ref[proto.type_ref], proto.type_ref
            external_member = True
        elif proto.type_ref in unusable_reference_type_refs:
            skip_log.record(
                proto.path,
                _PROTOTYPE_TAG,
                f"unresolved reference {proto.type_ref} "
                f"{unusable_reference_type_refs[proto.type_ref]} -- an external stub carries the "
                "source's reference and its DEST verbatim, and both must be usable",
                category=SkipCategory.NOT_SUPPORTED,
                effect="abort",
            )
            continue
        else:
            skip_log.record(
                proto.path,
                _PROTOTYPE_TAG,
                f"unresolved reference {proto.type_ref} (component prototype type)",
                category=SkipCategory.NOT_SUPPORTED,
                effect="abort",
            )
            continue
        if (field_name, type_name) in placed_types:
            skip_log.record(
                proto.path,
                _PROTOTYPE_TAG,
                f"composition '{name}' already places type '{type_name}' -- a type may be placed at "
                "most once per composition, because a connector endpoint names the type and is "
                "resolved to its member",
                category=SkipCategory.NOT_SUPPORTED,
                effect="abort",
            )
            continue
        try:
            member = CompositionMember(name=proto.name, description=proto.description, **{field_name: type_name})
        except ValidationError as exc:
            skip_log.record(
                proto.path,
                _PROTOTYPE_TAG,
                f"member {proto.name!r} is invalid: {exc}",
                category=SkipCategory.NOT_SUPPORTED,
                effect="abort",
            )
            continue
        seen_member_names.add(proto.name)
        placed_types.add((field_name, type_name))
        mapped = _MappedMember(
            member=member,
            field_name=field_name,
            type_name=type_name,
            type_path=type_path,
            external=external_member,
        )
        mapped_members.append(mapped)
        member_by_proto_path[proto.path] = mapped
    return mapped_members


def _type_directions(
    mapped: _MappedMember,
    ports_by_composition: dict[str, list[CompositionPort]],
    imported_by_path: dict[str, ImportedSwc],
) -> dict[str, str]:
    """Port name -> direction, over the ports of the member's type that survived mapping.

    Named attribute reads, never a computed ``getattr``: one on a contract-bound receiver makes the coverage field
    oracle abstain on every field of every bound class this module names.
    """
    if mapped.field_name == "composition":
        return {port.name: port.direction for port in ports_by_composition.get(mapped.type_path, [])}
    # Indexed, not `.get(...) or {}`: a non-stub `type` member was built from `imported_by_path`, both callers skip
    # stubs, and an empty answer would silently drop every connector of the member.
    return {port.name: port.direction for port in imported_by_path[mapped.type_path].swc.ports}


def _end_interface_kind(ctx: _CompositionContext, mapped: _MappedMember, port_name: str) -> str | None:
    """``"sender_receiver"`` or ``"client_server"`` for one resolved end, or ``None`` for an undeclared interface.

    ``None`` when the file does not declare the end's interface: an external stub's port, or a port whose interface
    is kept as an undeclared path. A reference's ``DEST`` naming a kind is the referrer's claim about content the file
    does not carry.
    """
    ports_by_composition = ctx.placed.ports_by_composition
    imported_by_path = ctx.imported_by_path
    interface_kind_by_name = ctx.interface_kind_by_name
    interfaces_by_port: dict[str, str | None]
    if mapped.external:
        return None
    if mapped.field_name == "composition":
        interfaces_by_port = {port.name: port.interface for port in ports_by_composition.get(mapped.type_path, [])}
    else:
        interfaces_by_port = {port.name: port.interface for port in imported_by_path[mapped.type_path].swc.ports}
    kind = interface_kind_by_name.get(interfaces_by_port.get(port_name) or "")
    return kind if kind in _CONNECTABLE_INTERFACE_KINDS else None


def _mint_stub_ports(placement: _PlacementContext, placed: _PlacedCompositions, stubs: list[ImportedSwc]) -> _StubPorts:
    """Mint the external stubs' ports from the census of their connector ends.

    One port per ``(stub, port name)``, from every consistent end of a connector whose references all resolve
    (:func:`_census_stub_ports`); a name those ends state with both directions mints nothing and is reported. Runs
    after the members are placed (the census resolves a connector's other ends) and before any connector is
    mapped (a stub's end resolves against these ports).
    """
    imported_by_path = placement.imported_by_path
    member_by_proto_path = placed.member_by_proto_path
    census = _census_stub_ports(
        placement.model,
        placement.composition_name_by_path,
        member_by_proto_path,
        placement.merges,
        {
            mapped.type_path: set(_type_directions(mapped, placed.ports_by_composition, imported_by_path))
            for mapped in member_by_proto_path.values()
            if not mapped.external
        },
        placed.outer_port_name_by_path,
    )
    contradictory = frozenset(key for key, directions in census.items() if len(directions) > 1)
    stub_port_contradictions: dict[str, list[str]] = {}
    minted_ports: dict[str, list[Port]] = {}
    for (type_path, port_name), directions in census.items():
        if (type_path, port_name) in contradictory:
            stub_port_contradictions.setdefault(type_path, []).append(
                f"{type_path}/{port_name} -- "
                + ", ".join(f"{direction} at {count} end(s)" for direction, count in sorted(directions.items()))
            )
            continue
        (direction,) = directions
        minted_ports.setdefault(type_path, []).append(Port(name=port_name, direction=direction))
    stubs = [
        ImportedSwc(
            swc=stub.swc.model_copy(update={"ports": minted_ports.get(stub.path, [])}),
            group=stub.group,
            path=stub.path,
        )
        for stub in stubs
    ]
    return _StubPorts(contradictory=contradictory, contradiction_lines=stub_port_contradictions, stubs=stubs)


def _map_composition_connectors(
    ctx: _CompositionContext, acc: _ConnectorAccumulator, comp: CompositionInfo, composition_name: str
) -> None:
    """Map one composition's assembly connectors, then its delegations, in document order.

    The connector and delegation names of one composition share its name space;
    the duplicate-delegation-link mirror is scoped to the composition.
    """
    state = _CompositionState()
    scope = _CompositionScope(
        comp=comp,
        name=composition_name,
        prototype_paths=frozenset(proto.path for proto in comp.prototypes),
        outer_ports=ctx.placed.outer_port_name_by_path[comp.path],
        outer_directions={port.name: port.direction for port in ctx.placed.ports_by_composition[comp.path]},
    )
    for ac in comp.assembly_connectors:
        _map_assembly_connector(ctx, acc, scope, state, ac)
    for deleg in comp.delegation_connectors:
        _map_delegation_connector(ctx, acc, scope, state, deleg)
    acc.delegations_by_composition[comp.path] = state.delegations


def _member_at(
    ctx: _CompositionContext, scope: _CompositionScope, component_ref: str | None
) -> tuple[_MappedMember | None, SkipReason | None]:
    """The member one ``CONTEXT-COMPONENT-REF`` names, or a reason.

    An instance reference is type-relative, so it must name a prototype of the composition the connector is drawn
    inside; one naming another composition's prototype is skipped rather than silently attributed.
    """
    comp = scope.comp
    name = scope.name
    prototype_paths = scope.prototype_paths
    chain_logs = ctx.chain_logs
    declared = ctx.declared
    member_by_proto_path = ctx.placed.member_by_proto_path
    if not component_ref:
        return None, not_supported("connector endpoint has no component reference")
    if component_ref.rsplit("/", 1)[0] != comp.path:
        # The context component is the instance reference's first context element, and the base is this
        # composition: [constr_2530].
        return None, SkipReason(
            f"unresolved reference {component_ref} (not a member of '{name}')",
            SkipCategory.EXPORT_DEFECT,
            StandardRule.INSTANCE_REF_CONTEXT,
        )
    mapped = member_by_proto_path.get(component_ref)
    if mapped is None:
        if component_ref in prototype_paths:
            # Pass 1 skipped the prototype and recorded why: the end inherits that entry.
            return None, chained_reason(
                f"unresolved reference {component_ref} (component prototype was skipped)",
                cause_path=component_ref,
                chain_logs=chain_logs,
            )
        if component_ref in declared:
            # Something else of this composition named where a prototype belongs; no rule is quoted for it.
            return None, not_supported(
                f"unresolved reference {component_ref} (names no component prototype of composition '{name}')"
            )
        # A composition's `component` aggregation is atpSplitable (SWCT
        # R4.2.2 :3296, R4.3.0 :3645): another file may declare it.
        return None, SkipReason(
            f"unresolved reference {component_ref} (the file declares no component prototype of "
            f"composition '{name}' at that path)",
            SkipCategory.OUTSIDE_FILE,
        )
    return mapped, None


def _port_at(
    ctx: _CompositionContext, mapped: _MappedMember, ref: ConnectorPortRef
) -> tuple[tuple[str, str] | None, SkipReason | None, bool]:
    """``(port name, direction)`` on the member's own type, or a reason.

    The reference is checked against the **type it claims to name**, not matched on its last segment: a
    ``TARGET-*-PORT-REF`` under another component's path is XSD-valid wrong wiring, which resolving by name would
    import. An end on an external stub meets the same test through :func:`_stub_end`, and a port the census found
    contradicting is refused.

    Returns:
        ``(end, reason, unowned)``; ``unowned`` is ``True`` only when a stub's end names a target port path the
        context's type does not own, so the caller counts the connector into ``unowned_port_paths``.
    """
    chain_logs = ctx.chain_logs
    contradictory = ctx.contradictory
    declared = ctx.declared
    port_paths = ctx.port_paths
    port_ref = ref.port_ref
    if mapped.external:
        end, stub_reason, unowned = _stub_end(mapped, ref)
        if end is None:
            return None, stub_reason, unowned
        if (mapped.type_path, end[0]) in contradictory:
            # Which DEST is wrong only the port's declaration could say, and no file SWCraft has declares it.
            return (
                None,
                SkipReason(
                    f"the export states contradicting directions for {port_ref}, so no port '{end[0]}' "
                    f"is minted on the external stub '{mapped.type_name}' (see the stub ports not imported)",
                    SkipCategory.OUTSIDE_FILE,
                ),
                False,
            )
        return end, None, False
    if not port_ref:
        return None, not_supported("connector endpoint has no port reference"), False
    port_name = _member_port_name(mapped.type_path, port_ref)
    if port_name is None:
        # The target is not a feature of the context element's type, so it leads to no inner instance:
        # [TPS_GST_00162], the rule a stub's end meets in `_stub_end`.
        return (
            None,
            SkipReason(
                f"unresolved reference {port_ref} (not a port of member "
                f"'{mapped.member.name}'s type '{mapped.type_name}')",
                SkipCategory.EXPORT_DEFECT,
                StandardRule.INSTANCE_REF_PATH,
            ),
            False,
        )
    direction = _type_directions(mapped, ctx.placed.ports_by_composition, ctx.imported_by_path).get(port_name)
    if direction is None:
        if port_ref in port_paths:
            # The mapper skipped the port and recorded why: the end inherits that entry.
            return (
                None,
                chained_reason(
                    f"unresolved reference {port_ref} (port was skipped)",
                    cause_path=port_ref,
                    chain_logs=chain_logs,
                ),
                False,
            )
        if port_ref in declared:
            # Something else of the type named where a port belongs; no rule is quoted for it.
            return (
                None,
                not_supported(f"unresolved reference {port_ref} (names no port of type '{mapped.type_name}')"),
                False,
            )
        # `SwComponentType.port` is atpSplitable (SWCT R4.2.2 :2732,
        # R4.3.0 :3083): another file may declare it.
        return (
            None,
            SkipReason(
                f"unresolved reference {port_ref} (the file declares no port of type "
                f"'{mapped.type_name}' at that path)",
                SkipCategory.OUTSIDE_FILE,
            ),
            False,
        )
    return (port_name, direction), None, False


def _held_by_the_verdict(
    ctx: _CompositionContext,
    acc: _ConnectorAccumulator,
    scope: _CompositionScope,
    state: _CompositionState,
    ac: AssemblyConnectorInfo,
) -> bool:
    """Whether the merge verdict keeps one assembly connector from being mapped.

    A connector of a conflicting group is not imported and its name stays
    claimed; of a group a ``--packages`` selection re-admitted, only the
    selected member is mapped and every other is skip+reported; of a merged
    group, the first member reached is mapped and the others are not.
    """
    merges = ctx.merges
    skip_log = ctx.skip_log
    seen_connector_names = acc.seen_connector_names
    merged_connector_groups = acc.merged_connector_groups
    local_names = state.local_names
    name = scope.name
    if merges is not None:
        connector_group = merges.group_of(ac.path)
        if connector_group is not None:
            if connector_group.verdict == CONFLICTING:
                # The name stays claimed in the flat `connectors/` namespace and in its own composition, as if the
                # group had imported.
                seen_connector_names.setdefault(ac.name, ac.path)
                local_names.setdefault(ac.name, ac.path)
                return True
            if connector_group.verdict == SELECTED:
                # A `--packages` selection re-admitted exactly one member of a conflicting group: its members
                # differ, so only the selected one is mapped, never the first reached. Every other member is
                # recorded, because its composition can still enter through the dependency closure and nothing
                # is silently dropped; the CONFLICTING branch needs no line, its group is printed whole in the
                # report's *Conflicting declarations* section.
                if ac.path != connector_group.primary:
                    skip_log.record(
                        ac.path,
                        _ASSEMBLY_TAG,
                        f"the --packages selection kept another declaration of the connector name "
                        f"'{ac.name}' at {connector_group.primary}; connectors/ is one flat "
                        f"namespace, so this declaration inside composition '{name}' is not imported",
                        category=SkipCategory.NOT_SUPPORTED,
                        effect="abort",
                    )
                    return True
            else:
                if connector_group.key in merged_connector_groups:
                    return True
                # First reached wins, not the path-order primary: a connector sits inside a composition, and the
                # primary's composition may never be reached. A merged group's members are equal, so this is no
                # choice about content.
                merged_connector_groups.add(connector_group.key)
    return False


def _map_assembly_connector(
    ctx: _CompositionContext,
    acc: _ConnectorAccumulator,
    scope: _CompositionScope,
    state: _CompositionState,
    ac: AssemblyConnectorInfo,
) -> None:
    """Map one ``ASSEMBLY-SW-CONNECTOR`` onto a :class:`~swcraft_tools.schema.Connector`, or skip+report it.

    Skipped: a blank name; a connector the merge verdict holds (:func:`_held_by_the_verdict`); a second connector
    of one name inside the composition; an end whose prototype or port reference does not resolve **against the
    type it names** -- on an external stub also an end whose target port path the stub's reference does not own
    ([TPS_GST_00162]), whose ``DEST`` is absent, unsupported or contradicts its element, or whose port the export
    states with two directions; two ends that are not one provide and one require; and a second connector of one
    name across the flat ``connectors/`` layer or of an endpoint pair already mapped
    (``rule:connector.duplicate-name`` and ``rule:connector.duplicate-link`` would refuse the written project,
    and the second file would overwrite the first).
    """
    model = ctx.model
    skip_log = ctx.skip_log
    connectors = acc.connectors
    unowned_port_paths = acc.unowned_port_paths
    seen_connector_names = acc.seen_connector_names
    seen_links = acc.seen_links
    name = scope.name
    local_names = state.local_names
    if not ac.name or not ac.name.strip():
        skip_log.record_reason(
            ac.path,
            _ASSEMBLY_TAG,
            blank_short_name("connector has missing or blank SHORT-NAME"),
            effect="abort",
        )
        return
    if _held_by_the_verdict(ctx, acc, scope, state, ac):
        return
    if ac.name in local_names:
        # Two connectors of one composition share its name space: [constr_2508], unless the file is variant-rich.
        # Asked before either end is resolved, so the defect is not reported under an end's reason.
        skip_log.record_reason(
            ac.path,
            _ASSEMBLY_TAG,
            duplicate_name_reason(
                f"duplicate connector SHORT-NAME '{ac.name}' inside composition '{name}' (already "
                f"declared by {local_names[ac.name]})",
                variant_rich=model.variant_rich,
            ),
            effect="abort",
        )
        return
    member_a, reason_a = _member_at(ctx, scope, ac.provider.component_ref)
    member_b, reason_b = _member_at(ctx, scope, ac.requester.component_ref)
    if member_a is None:
        assert reason_a is not None  # `_member_at` answers a reason whenever it answers no member
        skip_log.record_reason(ac.path, _ASSEMBLY_TAG, reason_a, prefix="provider end: ", effect="abort")
        return
    if member_b is None:
        assert reason_b is not None  # `_member_at` answers a reason whenever it answers no member
        skip_log.record_reason(ac.path, _ASSEMBLY_TAG, reason_b, prefix="requester end: ", effect="abort")
        return
    end_a, reason_a, unowned_a = _port_at(ctx, member_a, ac.provider)
    end_b, reason_b, unowned_b = _port_at(ctx, member_b, ac.requester)
    # An end whose target port path the context's type does not own is reported before any other end reason,
    # whichever end carries it, so it is counted as the export's instance-reference defect.
    if unowned_a or unowned_b:
        side, reason = ("provider", reason_a) if unowned_a else ("requester", reason_b)
        assert reason is not None  # `_port_at` answers a reason with every refusal it marks unowned
        skip_log.record_reason(ac.path, _ASSEMBLY_TAG, reason, prefix=f"{side} end: ", effect="abort")
        unowned_port_paths.setdefault(name, []).append(ac.path)
        return
    if end_a is None:
        assert reason_a is not None  # `_port_at` answers a reason whenever it answers no end
        skip_log.record_reason(ac.path, _ASSEMBLY_TAG, reason_a, prefix="provider end: ", effect="abort")
        return
    if end_b is None:
        assert reason_b is not None  # `_port_at` answers a reason whenever it answers no end
        skip_log.record_reason(ac.path, _ASSEMBLY_TAG, reason_b, prefix="requester end: ", effect="abort")
        return

    port_a, direction_a = end_a
    port_b, direction_b = end_b
    if direction_a == "provide" and direction_b == "require":
        provider, provider_port, requester, requester_port = member_a, port_a, member_b, port_b
    elif direction_b == "provide" and direction_a == "require":
        provider, provider_port, requester, requester_port = member_b, port_b, member_a, port_a
    else:
        same = _same_direction_reason(
            direction_a, _end_interface_kind(ctx, member_a, port_a), _end_interface_kind(ctx, member_b, port_b)
        )
        skip_log.record_reason(ac.path, _ASSEMBLY_TAG, same, effect="abort")
        return

    if ac.name in seen_connector_names:
        # Only SWCraft's flat `connectors/` makes two compositions' connectors one name space.
        skip_log.record(
            ac.path,
            _ASSEMBLY_TAG,
            f"duplicate connector name '{ac.name}' (already produced by "
            f"{seen_connector_names[ac.name]} -- would overwrite that connector's YAML file)",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return
    # `rule:connector.duplicate-link`'s own key: the composition, then each endpoint's field (`swc: X` and
    # `composition: X` differ), name and port. One wiring in two compositions is legal.
    assembly_link = (
        name,
        _endpoint_field(provider),
        provider.type_name,
        provider_port,
        _endpoint_field(requester),
        requester.type_name,
        requester_port,
    )
    if assembly_link in seen_links:
        # A type is placed at most once per composition, so one key is one pair of port prototypes: [constr_1086].
        skip_log.record(
            ac.path,
            _ASSEMBLY_TAG,
            f"duplicate connector link {provider.type_name}.{provider_port} -> "
            f"{requester.type_name}.{requester_port} inside composition '{name}' (already "
            f"declared by {seen_links[assembly_link]}) -- `rule:connector.duplicate-link` would "
            "refuse the imported project",
            category=SkipCategory.EXPORT_DEFECT,
            rule=StandardRule.ONE_CONNECTOR_PER_PAIR,
            effect="abort",
        )
        return
    seen_connector_names[ac.name] = ac.path
    seen_links[assembly_link] = ac.path
    local_names[ac.name] = ac.path
    connectors.append(
        Connector(
            name=ac.name,
            **{"from": _connector_endpoint(provider, provider_port)},
            to=_connector_endpoint(requester, requester_port),
            composition=name,
            description=ac.description,
        )
    )


def _map_delegation_connector(
    ctx: _CompositionContext,
    acc: _ConnectorAccumulator,
    scope: _CompositionScope,
    state: _CompositionState,
    deleg: DelegationConnectorInfo,
) -> None:
    """Map one ``DELEGATION-SW-CONNECTOR`` onto a :class:`~swcraft_tools.schema.Delegation`, or skip+report it.

    Skipped: a blank name; a second connector of one name inside the composition; an inner end or an outer port
    that does not resolve, the inner end checked as an assembly end is (:func:`_port_at`); two ports of different
    directions; and a second delegation wiring the boundary port, member and member port one already mapped wires
    (``rule:composition.duplicate-delegation-link`` would refuse the written project; a file can carry such pairs
    differing only in ``SHORT-NAME``, which is why this skip exists beside the rule). Several delegations on one
    outer port are not skipped: wiring different member ports is the legal fan-out shape.
    """
    model = ctx.model
    skip_log = ctx.skip_log
    unowned_port_paths = acc.unowned_port_paths
    comp = scope.comp
    name = scope.name
    outer_ports = scope.outer_ports
    outer_directions = scope.outer_directions
    local_names = state.local_names
    seen_delegation_links = state.seen_delegation_links
    if not deleg.name or not deleg.name.strip():
        skip_log.record_reason(
            deleg.path,
            _DELEGATION_TAG,
            blank_short_name("delegation has missing or blank SHORT-NAME"),
            effect="abort",
        )
        return
    if deleg.name in local_names:
        # One composition's CONNECTORS share its name space: [constr_2508], unless the file is variant-rich.
        duplicate = duplicate_name_reason(
            f"duplicate connector SHORT-NAME '{deleg.name}' inside composition '{name}'",
            variant_rich=model.variant_rich,
        )
        skip_log.record_reason(deleg.path, _DELEGATION_TAG, duplicate, effect="abort")
        return
    mapped, reason = _member_at(ctx, scope, deleg.inner.component_ref)
    if mapped is None:
        assert reason is not None  # `_member_at` answers a reason whenever it answers no member
        skip_log.record_reason(deleg.path, _DELEGATION_TAG, reason, prefix="inner end: ", effect="abort")
        return
    outer_name = outer_ports.get(deleg.outer_port_ref or "")
    if outer_name is None:
        outer = _unresolved_outer_port_reason(
            deleg.outer_port_ref,
            comp.path,
            name,
            ctx.declared,
            ctx.composition_paths,
            port_paths=ctx.port_paths,
            chain_logs=ctx.chain_logs,
        )
        skip_log.record_reason(deleg.path, _DELEGATION_TAG, outer, effect="abort")
        return
    inner, reason, unowned = _port_at(ctx, mapped, deleg.inner)
    if unowned:
        unowned_port_paths.setdefault(name, []).append(deleg.path)
    if inner is None:
        assert reason is not None  # `_port_at` answers a reason whenever it answers no end
        skip_log.record_reason(deleg.path, _DELEGATION_TAG, reason, prefix="inner end: ", effect="abort")
        return
    inner_port, inner_direction = inner
    outer_direction = outer_directions[outer_name]
    if outer_direction != inner_direction:
        skip_log.record(
            deleg.path,
            _DELEGATION_TAG,
            f"boundary port '{outer_name}' is {outer_direction} while inner port "
            f"'{mapped.member.name}.{inner_port}' is {inner_direction} -- a delegation's two "
            "ports must declare the same direction",
            category=SkipCategory.EXPORT_DEFECT,
            rule=StandardRule.DELEGATION_SAME_KIND,
            effect="abort",
        )
        return
    delegation_link = (outer_name, mapped.member.name, inner_port)
    first = seen_delegation_links.get(delegation_link)
    if first is not None:
        skip_log.record(
            deleg.path,
            _DELEGATION_TAG,
            f"duplicate delegation link {outer_name} -> {mapped.member.name}.{inner_port} "
            f"inside composition '{name}' (already declared by {first}) -- "
            "`rule:composition.duplicate-delegation-link` would refuse the imported project",
            # One pair of port prototypes, connected twice ([constr_1086]).
            category=SkipCategory.EXPORT_DEFECT,
            rule=StandardRule.ONE_CONNECTOR_PER_PAIR,
            effect="abort",
        )
        return
    seen_delegation_links[delegation_link] = deleg.path
    local_names[deleg.name] = deleg.path
    state.delegations.append(
        Delegation(
            name=deleg.name,
            port=outer_name,
            member=mapped.member.name,
            member_port=inner_port,
            description=deleg.description,
        )
    )


def _build_compositions(
    model: ImportModel,
    root: CompositionInfo | None,
    composition_name_by_path: dict[str, str],
    placed: _PlacedCompositions,
    delegations_by_composition: dict[str, list[Delegation]],
) -> list[Composition]:
    """One :class:`~swcraft_tools.schema.Composition` per mapped composition, in document order.

    The composition's ``LONG-NAME`` becomes its ``title`` exactly as held
    whenever it has one, blank included; a composition with no ``LONG-NAME``
    takes its own ``SHORT-NAME``. The root :func:`derive_root` answered carries
    ``root: true``.
    """
    ports_by_composition = placed.ports_by_composition
    members_by_composition = placed.members_by_composition
    compositions: list[Composition] = []
    for comp in model.compositions:
        name = composition_name_by_path.get(comp.path)
        if name is None:
            continue
        is_root = root is not None and comp.path == root.path
        compositions.append(
            Composition(
                name=name,
                # Blank included: the writer writes a `title` differing from the `name` back as the `LONG-NAME`. A
                # title is required, and inventing prose would be a claim the export never made.
                title=comp.long_name if comp.long_name is not None else name,
                description=comp.description,
                root=is_root,
                ports=ports_by_composition[comp.path],
                members=[mapped.member for mapped in members_by_composition[comp.path]],
                delegations=delegations_by_composition.get(comp.path, []),
            )
        )
    return compositions


def _endpoint_field(mapped: _MappedMember) -> str:
    """Which of :class:`~swcraft_tools.schema.ConnectorEndpoint`'s two type fields carries this member's type.

    ``"composition"`` or ``"swc"``; one spelling for the endpoint and the duplicate-link key.
    """
    return "composition" if mapped.field_name == "composition" else "swc"


def _connector_endpoint(mapped: _MappedMember, port: str) -> ConnectorEndpoint:
    """One connector endpoint naming the member's **type**, in the field the ``TYPE-TREF``'s ``DEST`` selects."""
    if _endpoint_field(mapped) == "composition":
        return ConnectorEndpoint(composition=mapped.type_name, port=port)
    return ConnectorEndpoint(swc=mapped.type_name, port=port)


#: The two port ``DEST`` values an end on a stub may carry, and the direction each
#: states. A ``PR-PORT-PROTOTYPE`` is not here: SWCraft has no such direction, so
#: an end naming one is reported rather than minted.
_DEST_DIRECTION: dict[str, _PortDirection] = {"P-PORT-PROTOTYPE": "provide", "R-PORT-PROTOTYPE": "require"}

#: The phrase of every refusal of an end whose target port path the context's type does not own, spelled once so
#: the report's category and the skip reasons cannot drift apart.
UNOWNED_TARGET_PORT = "target port path is not owned by the context component's type"


#: The two interface kinds a port of an imported component can be typed by; any other kind reads as undeclared.
_CONNECTABLE_INTERFACE_KINDS = frozenset({"sender_receiver", "client_server"})


def _same_direction_reason(direction: str, kind_a: str | None, kind_b: str | None) -> SkipReason:
    """Why an assembly connector whose two ends are both *direction* is skipped.

    Decided by the ends' **declared** interface kinds: two that differ -- sender-receiver with client-server --
    are ``[constr_1036]`` (SWCT R4.2.2 :3949, R4.3.0 :4300); every other pair -- one kind on both ends, or an
    undeclared interface (``None``) -- is ``[TPS_SWCT_01113]`` (SWCT R4.2.2 :2877, R4.3.0 :3228): an assembly
    hooks a provide port up to a require port, so two ends of one direction break it whatever the interfaces.

    **What this does not reach:** a mixed-kind assembly whose directions do pair up never gets here; the importer
    compares no interfaces, and ``swcraft validate`` refuses the imported connector.
    """
    both = f"resolved endpoints are both '{direction}'"
    if kind_a is None or kind_b is None or kind_a == kind_b:
        return SkipReason(
            f"{both} -- a connector needs exactly one provide and one require end",
            SkipCategory.EXPORT_DEFECT,
            StandardRule.CONNECT_PROVIDE_TO_REQUIRE,
        )
    return SkipReason(
        f"{both}, one typed by a {INTERFACE_KIND_PROSE[kind_a]} and one by a {INTERFACE_KIND_PROSE[kind_b]} "
        "interface -- "
        "ports typed by interfaces of different kinds cannot be connected",
        SkipCategory.EXPORT_DEFECT,
        StandardRule.CONNECT_SAME_INTERFACE_KIND,
    )


def _unresolved_outer_port_reason(
    outer_port_ref: str | None,
    composition_path: str,
    composition_name: str,
    declared: Collection[str],
    composition_paths: frozenset[str],
    *,
    port_paths: frozenset[str],
    chain_logs: Sequence[SkipLog],
) -> SkipReason:
    """Why a delegation's ``OUTER-PORT-REF`` names no mapped boundary port.

    The outer port must be the delegation's own composition's (``[constr_1088]``, SWCT R4.2.2 :3530, R4.3.0
    :3880); the citation is made only when the port's owner is **another composition** of the file
    (*composition_paths*). An owner the file declares nowhere is outside the file; any other owner is no quoted
    defect. Under the delegation's own composition, a boundary port the reader handed over (*port_paths*) was
    skipped by ``map_ports`` and the end inherits its entry from *chain_logs*; anything else declared there names
    no port and inherits nothing; and an undeclared path may be in another file, because
    ``SwComponentType.port`` is atpSplitable (SWCT R4.2.2 :2732, R4.3.0 :3083).
    """
    if not outer_port_ref:
        return not_supported("delegation has no outer port reference")
    owner = outer_port_ref.rpartition("/")[0]
    if owner != composition_path:
        if owner in composition_paths:
            return SkipReason(
                f"outer port {outer_port_ref} is not a port of composition '{composition_name}' "
                f"({composition_path}), which owns the delegation",
                SkipCategory.EXPORT_DEFECT,
                StandardRule.DELEGATION_INSIDE_COMPOSITION,
            )
        if owner and owner not in declared:
            return SkipReason(
                f"outer port {outer_port_ref} is not a port of composition '{composition_name}' "
                f"({composition_path}), and the file declares nothing at {owner}",
                SkipCategory.OUTSIDE_FILE,
            )
        return not_supported(
            f"outer port {outer_port_ref} is not a port of composition '{composition_name}' "
            f"({composition_path}), which owns the delegation, and names no port of a composition"
        )
    if outer_port_ref in port_paths:
        return chained_reason(
            f"unresolved reference {outer_port_ref} (boundary port was skipped)",
            cause_path=outer_port_ref,
            chain_logs=chain_logs,
        )
    if outer_port_ref in declared:
        return not_supported(
            f"unresolved reference {outer_port_ref} (names no port of composition '{composition_name}')"
        )
    return SkipReason(
        f"unresolved reference {outer_port_ref} (the file declares no port of composition '{composition_name}' "
        "at that path)",
        SkipCategory.OUTSIDE_FILE,
    )


def _member_port_name(type_path: str, port_ref: str) -> str | None:
    """The port a mapped member's end names on its type, or ``None`` when the end's parent path is not that type's."""
    parent, _, port_name = port_ref.rpartition("/")
    return port_name if parent == type_path else None


def _stub_end(
    mapped: _MappedMember, ref: ConnectorPortRef
) -> tuple[tuple[str, _PortDirection] | None, SkipReason | None, bool]:
    """One connector end on an external stub: ``(port name, direction)``, or why not.

    The third value is ``True`` exactly when the target port path is not owned by the stub's type.

    **The instance reference must be consistent** (Generic Structure Template [TPS_GST_00162], R4.2.2 :5295,
    R4.3.0 :6147: the context features plus the target *are* the path from the root to the inner instance, so a
    target that is not a feature of the last context element's type leads to none). The parent of the target port
    path must be the stub's ``external_ref``: the same short name under another package is another type
    ([TPS_GST_00169]), and the writer can only emit ``external_ref`` plus the port name. The last segment and the
    ``DEST`` are never trusted to repair it.

    **The direction is the end's own ``DEST``**, checked against the element that holds the reference --
    ``PROVIDER-IREF`` / ``REQUESTER-IREF``, or a delegation's ``P-`` / ``R-PORT-IN-COMPOSITION-INSTANCE-REF``
    wrapper. An absent or unsupported ``DEST``, or one the element contradicts, mints nothing: the writer's
    ``DEST`` is fixed by that element and would silently replace what the export stated.
    """
    port_ref = ref.port_ref
    if not port_ref:
        return None, not_supported("connector endpoint has no port reference"), False
    parent, _, port_name = port_ref.rpartition("/")
    if parent != mapped.type_path:
        return (
            None,
            SkipReason(
                f"unresolved reference {port_ref} ({UNOWNED_TARGET_PORT}: member '{mapped.member.name}' is the "
                f"external stub '{mapped.type_name}' typed {mapped.type_path}; no port is minted from it)",
                SkipCategory.EXPORT_DEFECT,
                StandardRule.INSTANCE_REF_PATH,
            ),
            True,
        )
    if not port_name.strip():
        return (
            None,
            not_supported(f"unresolved reference {port_ref} (the target port path names no port)"),
            False,
        )
    if (reason := check_identifier(port_name)) is not None:
        # The writer emits this segment verbatim, and a reference path segment is an Identifier ([TPS_GST_00169]):
        # such a source is not schema-valid, and the segment is never repaired.
        return (
            None,
            not_supported(
                f"unresolved reference {port_ref} (the target port path's last segment {port_name!r} {reason}; "
                "no port is minted from it)"
            ),
            False,
        )
    dest = ref.dest
    never_inferred = f"the external stub '{mapped.type_name}' mints a port from the end's own DEST and never infers one"
    if dest is None or not dest.strip():
        return (
            None,
            not_supported(f"{port_ref} is referenced with no DEST attribute -- {never_inferred}"),
            False,
        )
    direction = _DEST_DIRECTION.get(dest)
    if direction is None:
        return (
            None,
            not_supported(
                f"{port_ref} is referenced with DEST {dest!r}, which is not a P- or R-PORT-PROTOTYPE -- "
                f"{never_inferred}"
            ),
            False,
        )
    if ref.stated_direction != direction:
        return (
            None,
            not_supported(
                f"{port_ref} is referenced with DEST {dest} by an end that states a "
                f"{ref.stated_direction or 'no'} port -- {never_inferred}"
            ),
            False,
        )
    return (port_name, direction), None, False


def _census_stub_ports(
    model: ImportModel,
    composition_name_by_path: dict[str, str],
    member_by_proto_path: dict[str, _MappedMember],
    merges: MergeDecisions | None,
    port_names_by_type_path: dict[str, set[str]],
    outer_port_name_by_path: dict[str, dict[str, str]],
) -> dict[tuple[str, str], dict[_PortDirection, int]]:
    """``(stub TYPE-TREF, port name)`` -> direction -> how many ends state it.

    An end is an **occurrence** of a stub's port when :func:`_stub_end` accepts it and **every other reference of
    its connector resolves** -- the other assembly end to a port of a member's type (or another accepted stub
    end), a delegation's outer reference to a kept boundary port -- so a connector the export got wrong elsewhere
    cannot strike a port a sound connector needs. Two directions among the occurrences are a contradiction and
    mint nothing. The ends of a connector the merge verdict imports nothing of (``CONFLICT`` / ``EXCLUDED``) are
    not occurrences. Not asked here: the name and pairing checks the mapping loop makes next; a connector refused
    for one of those still states its port, and :func:`finish_stub_ports` drops a port no kept connector reaches.

    Insertion-ordered in document order, so a stub's ports are written in the order the export first states them.
    """
    census: dict[tuple[str, str], dict[_PortDirection, int]] = {}

    def resolve(comp_path: str, ref: ConnectorPortRef) -> tuple[bool, tuple[str, str, _PortDirection] | None]:
        """``(resolves, (stub TYPE-TREF, port, direction) or None)`` for one end."""
        component_ref = ref.component_ref
        if not component_ref or component_ref.rsplit("/", 1)[0] != comp_path:
            return False, None
        mapped = member_by_proto_path.get(component_ref)
        if mapped is None:
            return False, None
        if not mapped.external:
            port_name = _member_port_name(mapped.type_path, ref.port_ref or "")
            return port_name is not None and port_name in port_names_by_type_path[mapped.type_path], None
        end, _reason, _unowned = _stub_end(mapped, ref)
        if end is None:
            return False, None
        return True, (mapped.type_path, *end)

    def record(occurrences: list[tuple[str, str, _PortDirection] | None]) -> None:
        for occurrence in occurrences:
            if occurrence is None:
                continue
            type_path, port_name, direction = occurrence
            directions = census.setdefault((type_path, port_name), {})
            directions[direction] = directions.get(direction, 0) + 1

    for comp in model.compositions:
        if comp.path not in composition_name_by_path:
            continue
        for ac in comp.assembly_connectors:
            if merges is not None and merges.outcome(ac.path) in (CONFLICT, EXCLUDED):
                continue
            ends = [resolve(comp.path, ac.provider), resolve(comp.path, ac.requester)]
            if all(resolves for resolves, _occurrence in ends):
                record([occurrence for _resolves, occurrence in ends])
        for deleg in comp.delegation_connectors:
            resolves, occurrence = resolve(comp.path, deleg.inner)
            if resolves and (deleg.outer_port_ref or "") in outer_port_name_by_path[comp.path]:
                record([occurrence])
    return census


@dataclass
class FinishedStubPorts:
    """What :func:`finish_stub_ports` answers: the components, and three report inputs."""

    #: Every component, the stubs' ports pruned and hinted, in the order given.
    swcs: list[ImportedSwc]
    #: One line per stub that kept a port: its name, reference and port counts.
    port_lines: list[str]
    #: One line per stub port whose known neighbours name different interfaces.
    ambiguous_lines: list[str]


def finish_stub_ports(
    swcs: list[ImportedSwc],
    connectors: list[Connector],
    compositions: list[Composition],
    interface_names: set[str],
) -> FinishedStubPorts:
    """Prune the stubs' minted ports to the ones a kept connector reaches, and hint each.

    Run **last** -- after ``--packages``, every interface rename and the reserved device-name skip -- so it reads
    the final element set: a port whose every connector fell is dropped, and a hint names an interface by the name
    this import writes.

    **The hint** (``Port.observed_interface``) is the declared ``interface`` of the port's direct neighbours --
    the other end of a kept assembly connector, or the boundary port of a kept delegation -- when every neighbour
    names the same one. Nothing propagates: a neighbour on a stub's port contributes nothing, and neither does an
    interface this import does not write (a kept full path); an external interface stub is written, so it counts.
    Neighbours naming different interfaces leave the hint empty and are reported with their candidates. No rule
    reads it.
    """
    stubs = {imported.swc.name for imported in swcs if imported.swc.external}
    declared_ports = {
        imported.swc.name: {port.name: port.interface for port in imported.swc.ports}
        for imported in swcs
        if not imported.swc.external
    }
    composition_ports = {comp.name: {port.name: port.interface for port in comp.ports} for comp in compositions}
    reached: dict[tuple[str, str], list[str]] = {}

    def neighbour_interface(endpoint: ConnectorEndpoint) -> str | None:
        if endpoint.swc is not None:
            interface = declared_ports.get(endpoint.swc, {}).get(endpoint.port)
        else:
            interface = composition_ports.get(endpoint.composition or "", {}).get(endpoint.port)
        return interface if interface in interface_names else None

    def reach(stub: str, port: str, interface: str | None) -> None:
        candidates = reached.setdefault((stub, port), [])
        if interface is not None:
            candidates.append(interface)

    for connector in connectors:
        for end, other in ((connector.from_, connector.to), (connector.to, connector.from_)):
            if end.swc in stubs:
                reach(end.swc, end.port, neighbour_interface(other))
    for comp in compositions:
        member_types = {member.name: member.type for member in comp.members}
        for delegation in comp.delegations:
            member_type = member_types.get(delegation.member)
            if member_type in stubs:
                boundary = ConnectorEndpoint(composition=comp.name, port=delegation.port)
                reach(member_type, delegation.member_port, neighbour_interface(boundary))

    finished: list[ImportedSwc] = []
    port_lines: list[str] = []
    ambiguous_lines: list[str] = []
    for imported in swcs:
        swc = imported.swc
        if not swc.external:
            finished.append(imported)
            continue
        ports: list[Port] = []
        for port in swc.ports:
            candidates = reached.get((swc.name, port.name))
            if candidates is None:
                continue
            distinct = sorted(set(candidates))
            if len(distinct) > 1:
                ambiguous_lines.append(f"{swc.name}.{port.name} -- candidates: {', '.join(distinct)}")
            hint = distinct[0] if len(distinct) == 1 else None
            ports.append(port.model_copy(update={"observed_interface": hint}))
        if ports:
            provide = sum(1 for port in ports if port.direction == "provide")
            hinted = sum(1 for port in ports if port.observed_interface is not None)
            port_lines.append(
                f"{swc.name} ({swc.external_ref}): {len(ports)} port(s) -- {provide} provide, "
                f"{len(ports) - provide} require; {hinted} with an observed interface"
            )
        finished.append(
            ImportedSwc(swc=swc.model_copy(update={"ports": ports}), group=imported.group, path=imported.path)
        )
    return FinishedStubPorts(swcs=finished, port_lines=port_lines, ambiguous_lines=ambiguous_lines)
