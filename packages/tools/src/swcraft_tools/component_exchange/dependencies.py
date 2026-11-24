"""``import component`` resolves against the project it would leave.

This module owns three things:

* :class:`MissingReference`, the **dependency verdict** -- *this reference has no answer* -- never a
  ``SkipEntry``. Its ``kind`` is one of :data:`MISSING_KINDS`: an operation, a data element, a mode
  group or a mapping set;
* the **two resolver tiers** -- the file's own declaration at the exact path first, then the target
  project's **declared** interface of the reference's last segment and ``DEST`` kind, and nothing else;
* the **late pass**, :func:`resolve_against_target`, run after the target read and before the
  reserved-name checks -- and, for a path neither tier answers, the **stub decision**: a target stub
  of the same ``external_ref`` reused, else an interface stub minted as ``import model`` mints one.

What this module does **not** reach: a type reference (refused earlier); a reference to a declaration
the file makes and SWCraft skipped (*skipped* stays distinct from *absent*); and, for a
target-resolved port's own init value, the value-form judgement against the file's own type and every
range and base-type size. A stubbed component does not round-trip through ``generate arxml --component``.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace

from swcraft_tools.arxml_import.registry import PathRegistry, PortInitKey, PortInitOutcome
from swcraft_tools.arxml_import.swc_mapping import (
    ComSpecMemberMiss,
    HeldStubComSpecs,
    ImportedSwc,
    LatePort,
    UnansweredRecord,
    admit_held_records,
    bind_late_port,
    bind_minted_stub_port,
    bind_reused_stub_port,
    hold_late_stub_com_specs,
    record_unanswered_port,
)
from swcraft_tools.arxml_import.type_mapping import HeldUnreadInit, SourceTypes, late_port_init_outcome
from swcraft_tools.arxml_import.unresolved import UnresolvedReferences
from swcraft_tools.arxml_reader import ImportModel, SkipLog, SwcInfo
from swcraft_tools.data_description import DataDescription
from swcraft_tools.schema import (
    DataEvent,
    ExternalInterface,
    Interface,
    MappingSet,
    Port,
    SRInterface,
    SWComponent,
    TimingEvent,
)
from swcraft_tools.validator.compatibility import interface_kind_dest

#: What each kind of a :class:`MissingReference` is called in a rendered line.
_KIND_NOUN: dict[str, str] = {
    "operation": "operation",
    "data_element": "data element",
    "mode_group": "mode group",
    "mapping_set": "mapping set",
}

#: The closed ``kind`` vocabulary of a :class:`MissingReference`.
MISSING_KINDS: tuple[str, ...] = tuple(_KIND_NOUN)

#: The phrase a held record takes in its referrer, keyed by its collection and, for ``events``
#: (which an operation event and a data-received event both join), by the source element's tag.
_COLLECTION_PHRASE: dict[tuple[str, str | None], str] = {
    ("events", "OPERATION-INVOKED-EVENT"): "operation event",
    ("events", "DATA-RECEIVED-EVENT"): "data-received event",
    ("reads", None): "read",
    ("writes", None): "write",
    ("calls", None): "call",
}


@dataclass(frozen=True)
class MissingReference:
    """One reference of a component's behaviour that neither the file nor the target project answers.

    ``referrer_layer``/``referrer_name`` are the referring element (``swc`` and the component),
    ``referrer`` the part of it that makes the reference. ``missing_member`` is the missing member's
    name (empty for a mapping set, itself the missing element); ``missing_layer``/``missing_name`` the
    element it was looked up in, by the name it is written under. No ARXML or storage path: paths stay
    internal to resolution. ``missing_is_stub`` marks an external interface stub, whose members are all
    inferred, so the line does not say *does not declare*.
    """

    referrer_layer: str
    referrer_name: str
    referrer: str
    missing_member: str
    reference_kind: str
    missing_layer: str
    missing_name: str
    missing_is_stub: bool = False

    def __post_init__(self) -> None:
        if self.reference_kind not in MISSING_KINDS:
            raise ValueError(f"unknown missing-reference kind {self.reference_kind!r}")

    def line(self) -> str:
        """The record as one line, by layer and element, naming no path."""
        where = f"{self.referrer_layer} '{self.referrer_name}' {self.referrer}"
        if self.reference_kind == "mapping_set":
            return (
                f"{where} names the {self.missing_layer} element '{self.missing_name}', "
                "which neither the file nor the project declares"
            )
        if self.missing_is_stub:
            return (
                f"{where} names the {_KIND_NOUN[self.reference_kind]} '{self.missing_member}' of the external "
                f"interface stub '{self.missing_name}', which neither the file nor the project declares"
            )
        return (
            f"{where} names the {_KIND_NOUN[self.reference_kind]} '{self.missing_member}', which the "
            f"{self.missing_layer} element '{self.missing_name}' does not declare"
        )


@dataclass
class LatePass:
    """What :func:`resolve_against_target` decided for one component.

    ``imported`` has every answered port bound and every answered held record admitted, its names not
    yet settled (the caller settles them once); ``skip_log`` holds the lines the pass recorded;
    ``minted`` the interface stubs the import creates, ``reused`` the target stubs it re-points at.
    """

    imported: ImportedSwc
    missing: list[MissingReference] = field(default_factory=list)
    skip_log: SkipLog = field(default_factory=SkipLog)
    minted: tuple[ExternalInterface, ...] = ()
    reused: tuple[ExternalInterface, ...] = ()


def target_declared_interface(
    path: str, target_interfaces: Mapping[str, Interface], dests: Collection[str]
) -> Interface | None:
    """The target's **declared** interface the reference *path* names by its last segment, or ``None``.

    Only an interface **of the kind the reference names** answers: every one of *dests* (the ``DEST``
    each occurrence wrote) must be that interface's ``DEST``; an abstract or absent ``DEST`` binds nothing. A target
    ``ExternalInterface`` is never an answer.
    """
    leaf = path.rpartition("/")[2]
    interface = target_interfaces.get(leaf) if leaf else None
    if interface is None or isinstance(interface, ExternalInterface):
        return None
    if not dests or set(dests) != {interface_kind_dest(interface)}:
        return None
    return interface


def needs_late_pass(imported: ImportedSwc, model: ImportModel, mapping_set_name_by_path: Mapping[str, str]) -> bool:
    """Whether the component holds anything only the target project can answer."""
    return bool(
        imported.late_ports
        or imported.held_stub_records
        or imported.com_spec_member_misses
        or imported.behaviour_member_misses
        or _has_dangling_set_ref(raw_swc(model, imported), mapping_set_name_by_path)
    )


def target_stubs_by_external_ref(
    target_interfaces: Mapping[str, Interface],
) -> dict[str, tuple[ExternalInterface, ...]]:
    """The target's interface stubs by their ``external_ref``, the one key a stub is matched by.

    Never by leaf: a stub stands for its own path. Stubs sharing an ``external_ref`` are listed by name,
    and the first whose ``external_dest`` the occurrences carry is reused
    (:meth:`UnresolvedReferences.reuse`), so the choice does not depend on read order.
    """
    stubs: dict[str, list[ExternalInterface]] = {}
    for name in sorted(target_interfaces):
        interface = target_interfaces[name]
        if isinstance(interface, ExternalInterface):
            stubs.setdefault(interface.external_ref, []).append(interface)
    return {path: tuple(candidates) for path, candidates in stubs.items()}


def resolve_against_target(
    imported: ImportedSwc,
    *,
    model: ImportModel,
    unresolved: UnresolvedReferences,
    target_interfaces: Mapping[str, Interface],
    target_mapping_sets: Mapping[str, MappingSet],
    mapping_set_name_by_path: Mapping[str, str],
    registry: PathRegistry,
    claimed_interface_names: Collection[str] = (),
    held_unread_inits: Sequence[HeldUnreadInit] = (),
    data_description: DataDescription,
) -> LatePass:
    """The late pass: answer what the component holds against the project the import would leave.

    - A kept port whose interface the file declares nowhere is bound to the target's declared interface
      (:func:`target_declared_interface`).
    - Otherwise the target's stub with that ``external_ref`` is reused, else a stub is minted
      (:meth:`UnresolvedReferences.mint_stubs`), never under one of *claimed_interface_names*. A path
      whose occurrences disagree on ``DEST``, whose ``DEST`` no stub admits, or which is malformed keeps
      its refusal.
    - Every held event, read, write and call whose member the answering interface lacks is a
      :class:`MissingReference`; so is a disabled mode through such a port (``mode_group``), a mapping
      set neither tier declares, and a com-spec naming an undeclared member.
    - A held unread-init line under a path the target answered is decided against the target element
      and carried on the bound port or told with its reason; any other held line is descriptive.
    """
    skip_log = SkipLog()
    late_by_name = {late.port_name: late for late in imported.late_ports}
    # Read before any path is withdrawn: two ports on one path see one answer.
    dests_by_path = {
        path: unresolved.occurrence_dests(path) for path in {late.interface_path for late in imported.late_ports}
    }
    bound: dict[str, Interface] = {}
    unanswered_ports: list[LatePort] = []
    for late in imported.late_ports:
        path = late.interface_path
        interface = target_declared_interface(path, target_interfaces, dests_by_path[path])
        if interface is None:
            unanswered_ports.append(late)
            continue
        unresolved.withdraw(path)
        bound[late.port_name] = interface

    # The target's stub of the same `external_ref` first, then one minted.
    stubs = _stub_decision(unanswered_ports, target_interfaces, unresolved, registry, claimed_interface_names, skip_log)
    reused_by_port, stubbed, minted_interfaces = stubs.reused_by_port, stubs.stubbed, stubs.minted

    # The file's com-spec inits under a path the target answered, decided against the target's
    # element; every other held line is told below.
    bound_paths = {late_by_name[name].interface_path: interface for name, interface in bound.items()}
    port_inits = _late_port_inits(held_unread_inits, bound_paths, model, data_description)
    imported, member_misses = _rebound_ports(imported, late_by_name, bound, stubs, port_inits, unresolved, skip_log)
    # A chained line names a stub only once the import writes it (or the target holds it).
    unresolved.name_written_stubs(
        interface_names={*claimed_interface_names, *(stub.name for stub in minted_interfaces)}, type_names=()
    )

    imported, unanswered = admit_held_records(imported, bound, skip_log)
    # A member the file's own interface lacks at the exact path is a verdict too.
    unanswered = [*imported.behaviour_member_misses, *unanswered]
    imported = replace(imported, behaviour_member_misses=[])
    missing = [_record_verdict(imported.swc.name, record, stubbed) for record in unanswered]
    missing.extend(_disabled_mode_verdicts(imported.swc, unanswered_ports, stubbed, bound))
    missing.extend(_com_spec_verdict(imported.swc.name, miss) for miss in member_misses)

    # Every other held unread-init line, descriptive; one whose path a stub took names the stub.
    stub_by_path = dict(stubs.minted_names)
    for port_name, reused in reused_by_port.items():
        stub_by_path[late_by_name[port_name].interface_path] = reused.name
    for held in held_unread_inits:
        if held.interface_path in bound_paths:
            continue
        held.record(skip_log, stub=stub_by_path.get(held.interface_path))

    imported, set_verdicts = _resolve_mapping_sets(imported, model, target_mapping_sets, mapping_set_name_by_path)
    missing.extend(set_verdicts)
    # Put back through Pydantic validation: `model_copy` runs no validator.
    imported = replace(imported, swc=SWComponent.model_validate(imported.swc.model_dump()))
    return LatePass(
        imported=imported,
        missing=missing,
        skip_log=skip_log,
        minted=minted_interfaces,
        reused=tuple({stub.name: stub for stub in reused_by_port.values()}.values()),
    )


def raw_swc(model: ImportModel, imported: ImportedSwc) -> SwcInfo | None:
    """The reader's record of *imported*'s component, by its path, or ``None``."""
    return next((raw for raw in model.swcs if raw.path == imported.path), None)


def _record_verdict(swc_name: str, record: UnansweredRecord, stubbed: Mapping[str, str]) -> MissingReference:
    """The verdict of one held event, read, write or call no declared interface answered.

    Through a stub port the missing element is named by the stub's name (*stubbed*, by port name).
    """
    phrase = _COLLECTION_PHRASE[(record.collection, record.tag if record.collection == "events" else None)]
    stub = stubbed.get(record.port)
    return MissingReference(
        referrer_layer="swc",
        referrer_name=swc_name,
        referrer=f"runnable '{record.runnable_name}' {phrase} through port '{record.port}'",
        missing_member=record.member_name,
        reference_kind="operation" if record.is_operation else "data_element",
        missing_layer="interfaces",
        missing_name=stub if stub is not None else record.interface_name,
        missing_is_stub=stub is not None,
    )


def _com_spec_verdict(swc_name: str, miss: ComSpecMemberMiss) -> MissingReference:
    """The verdict of a com-spec naming a member its port's interface does not declare."""
    return MissingReference(
        referrer_layer="swc",
        referrer_name=swc_name,
        referrer=f"port '{miss.port_name}' {miss.tag}",
        missing_member=miss.member_name,
        reference_kind="operation" if miss.is_operation else "data_element",
        missing_layer="interfaces",
        missing_name=miss.interface_name,
    )


def _late_port_inits(
    held_unread_inits: Sequence[HeldUnreadInit],
    bound_paths: Mapping[str, Interface],
    model: ImportModel,
    data_description: DataDescription,
) -> dict[PortInitKey, PortInitOutcome]:
    """Every held com-spec init under a path the target answered, decided against the target's element.

    Keyed by ``PortInitKey.of_init`` as ``map_interfaces`` keys its own, so ``bind_late_port`` finds the
    gated com-spec's own value. An init naming an element the interface does not declare gets no
    outcome: its com-spec is already a verdict or a skipped line.
    """
    if not bound_paths:
        return {}
    constants_by_path = {constant.path: constant for constant in model.constant_specs}
    source_types = SourceTypes(model)
    outcomes: dict[PortInitKey, PortInitOutcome] = {}
    for held in held_unread_inits:
        interface = bound_paths.get(held.interface_path)
        if not isinstance(interface, SRInterface):
            continue
        name = held.element_path.rpartition("/")[2]
        element = next((element for element in interface.data_elements if element.name == name), None)
        if element is None:
            continue
        outcomes.setdefault(
            PortInitKey.of_init(held.com_spec),
            late_port_init_outcome(held.com_spec, element, constants_by_path, data_description, source_types),
        )
    return outcomes


def _disabled_mode_verdicts(
    swc: SWComponent,
    unanswered_ports: Sequence[LatePort],
    stubbed: Mapping[str, str],
    bound: Mapping[str, Interface],
) -> list[MissingReference]:
    """One verdict per event's disabled mode through a port the late pass decided (``mode_group``).

    Timing and data-received events are asked. Nothing the project declares answers such a group:
    through a stub, or through a bound declared interface, since a mode group belongs to a mode-switch
    interface, which no declared SWCraft interface is. The missing element is named by the stub, the
    bound interface, or else the path's last segment.
    """
    names = {
        late.port_name: stubbed.get(late.port_name, late.interface_path.rpartition("/")[2]) for late in unanswered_ports
    }
    names.update({port_name: interface.name for port_name, interface in bound.items()})
    verdicts: list[MissingReference] = []
    for runnable in swc.runnables:
        for event in runnable.events:
            if isinstance(event, TimingEvent):
                kind = "timing event"
            elif isinstance(event, DataEvent):
                kind = "data-received event"
            else:
                continue
            for mode in event.disabled_modes:
                interface_name = names.get(mode.port)
                if interface_name is None:
                    continue
                verdicts.append(
                    MissingReference(
                        referrer_layer="swc",
                        referrer_name=swc.name,
                        referrer=f"runnable '{runnable.name}' {kind} disabled mode through port '{mode.port}'",
                        missing_member=mode.mode_group_ref.rpartition("/")[2],
                        reference_kind="mode_group",
                        missing_layer="interfaces",
                        missing_name=interface_name,
                        missing_is_stub=mode.port in stubbed,
                    )
                )
    return verdicts


def _resolve_mapping_sets(
    imported: ImportedSwc,
    model: ImportModel,
    target_mapping_sets: Mapping[str, MappingSet],
    mapping_set_name_by_path: Mapping[str, str],
) -> tuple[ImportedSwc, list[MissingReference]]:
    """The component's ``data_type_mappings`` by the two tiers, with a verdict per set neither declares.

    The file's set at the exact path first, then the target's set of the reference's last segment. In
    the export's order, de-duplicated; a component with no dangling reference is returned unchanged.
    """
    raw = raw_swc(model, imported)
    if raw is None or not _has_dangling_set_ref(raw, mapping_set_name_by_path):
        return imported, []
    names: list[str] = []
    verdicts: list[MissingReference] = []
    for ref in raw.data_type_mapping_refs:
        name = mapping_set_name_by_path.get(ref)
        if name is None:
            leaf = ref.rpartition("/")[2]
            if leaf in target_mapping_sets:
                name = leaf
            else:
                verdicts.append(
                    MissingReference(
                        referrer_layer="swc",
                        referrer_name=imported.swc.name,
                        referrer="internal behaviour",
                        missing_member="",
                        reference_kind="mapping_set",
                        missing_layer="mapping_sets",
                        missing_name=leaf,
                    )
                )
                continue
        if name not in names:
            names.append(name)
    swc = imported.swc.model_copy(update={"data_type_mappings": names})
    return replace(imported, swc=swc), verdicts


def _has_dangling_set_ref(raw: SwcInfo | None, mapping_set_name_by_path: Mapping[str, str]) -> bool:
    """Whether the component names a ``DATA-TYPE-MAPPING-REF`` onto a set the file does not map."""
    return raw is not None and any(ref not in mapping_set_name_by_path for ref in raw.data_type_mapping_refs)


@dataclass(frozen=True)
class _StubDecision:
    """What the stub decision gave the ports neither tier answered, by port name.

    ``held_by_port`` holds the com-specs of a port awaiting a minted stub; ``stubbed`` the stub name
    every stubbed port takes; ``minted_names`` the minted stubs' names by interface path.
    """

    reused_by_port: dict[str, ExternalInterface]
    held_by_port: dict[str, HeldStubComSpecs]
    stubbed: dict[str, str]
    minted: tuple[ExternalInterface, ...]
    minted_names: dict[str, str]


def _stub_decision(
    unanswered_ports: Sequence[LatePort],
    target_interfaces: Mapping[str, Interface],
    unresolved: UnresolvedReferences,
    registry: PathRegistry,
    claimed_interface_names: Collection[str],
    skip_log: SkipLog,
) -> _StubDecision:
    """The stub decision over the ports neither tier answered: the target's stub reused, else one minted.

    A port the safeguards decline keeps its refusal and takes no stub.
    """
    target_stubs = target_stubs_by_external_ref(target_interfaces)
    reused_by_port: dict[str, ExternalInterface] = {}
    held_by_port: dict[str, HeldStubComSpecs] = {}
    for late in unanswered_ports:
        path = late.interface_path
        stub = next(
            (candidate for candidate in target_stubs.get(path, ()) if unresolved.reuse(path, candidate) is not None),
            None,
        )
        if stub is not None:
            reused_by_port[late.port_name] = stub
        else:
            held_by_port[late.port_name] = hold_late_stub_com_specs(late, unresolved, skip_log)
    minted_interfaces: tuple[ExternalInterface, ...] = ()
    minted_names: dict[str, str] = {}
    if held_by_port:
        minted = unresolved.mint_stubs(
            registry,
            interface_names=claimed_interface_names,
            # The component policy keeps no type reference, so no type stub is decided.
            type_names=(),
        )
        minted_interfaces, minted_names = minted.interfaces, minted.interface_names
    stubbed: dict[str, str] = {}
    for late in unanswered_ports:
        reused = reused_by_port.get(late.port_name)
        name = reused.name if reused is not None else minted_names.get(late.interface_path)
        if name is None:
            # The safeguards declined the path: the port keeps its refusal.
            record_unanswered_port(late, skip_log)
            continue
        stubbed[late.port_name] = name
    return _StubDecision(reused_by_port, held_by_port, stubbed, minted_interfaces, minted_names)


def _rebound_ports(
    imported: ImportedSwc,
    late_by_name: Mapping[str, LatePort],
    bound: Mapping[str, Interface],
    stubs: _StubDecision,
    port_inits: Mapping[PortInitKey, PortInitOutcome],
    unresolved: UnresolvedReferences,
    skip_log: SkipLog,
) -> tuple[ImportedSwc, list[ComSpecMemberMiss]]:
    """*imported* with each late port bound, re-pointed at a reused stub or at a minted one.

    Returns the component with its late ports spent and the com-spec member misses, each a verdict.
    """
    member_misses: list[ComSpecMemberMiss] = list(imported.com_spec_member_misses)
    radix = list(imported.port_init_radix)
    equivalents = list(imported.port_init_equivalents)
    swc = imported.swc
    if bound or stubs.stubbed:
        ports: list[Port] = []
        for port in swc.ports:
            if port.name in bound:
                binding = bind_late_port(
                    port, late_by_name[port.name], bound[port.name], skip_log, port_inits=port_inits
                )
                port = binding.port
                member_misses.extend(binding.member_misses)
                radix.extend(binding.init_radix)
                equivalents.extend(binding.init_equivalents)
            elif port.name in stubs.reused_by_port:
                port = bind_reused_stub_port(port, late_by_name[port.name], stubs.reused_by_port[port.name], skip_log)
            elif port.name in stubs.stubbed:
                port = bind_minted_stub_port(
                    port, stubs.held_by_port[port.name], stubs.stubbed[port.name], unresolved, skip_log
                )
            ports.append(port)
        swc = swc.model_copy(update={"ports": ports})
    rebound = replace(
        imported,
        swc=swc,
        late_ports=[],
        com_spec_member_misses=[],
        port_init_radix=radix,
        port_init_equivalents=equivalents,
    )
    return rebound, member_misses
