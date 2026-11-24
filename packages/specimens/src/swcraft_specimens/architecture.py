"""In-memory architecture generation.

This module stops at software architecture.  It does not create tasks,
placements, memory budgets, SWCraft files, or ``generation.json``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from hashlib import sha256
from typing import Any

from .deterministic import SplitMix64, sample_without_replacement, uneven_partition, weighted_choice
from .errors import GenerationError
from .model import ArrayType, BaseType, DataType, RecordType, ValueType
from .names import (
    ARGUMENT_NAMES,
    SERVICE_NAMES,
    SIGNAL_NAMES,
    domain_catalog,
    semantic_component_name,
    semantic_runnable_name,
    validate_unique_identifiers,
)
from .preset import ASIL, ExecutionTimeParameters, Preset

DOMAIN_ROLES = (
    "input_processing",
    "state_estimation",
    "control",
    "actuation",
    "communication",
    "diagnostics",
    "safety_monitoring",
    "platform_services",
)
ASIL_LEVELS = (ASIL.QM, ASIL.A, ASIL.B, ASIL.C, ASIL.D)


@dataclass(frozen=True)
class ArchitectureRunnable:
    name: str
    period_us: int
    raw_execution_us: Decimal
    wcet_us: int
    calibration_factor: Fraction
    writes: tuple[tuple[str, str], ...] = ()
    reads: tuple[tuple[str, str], ...] = ()
    calls: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ArchitecturePort:
    name: str
    direction: str
    interface: str


@dataclass(frozen=True)
class ArchitectureComponent:
    name: str
    domain: str
    domain_role: str
    asil: ASIL
    architectural_role: str
    runnables: tuple[ArchitectureRunnable, ...]
    ports: tuple[ArchitecturePort, ...]
    description: str = ""


@dataclass(frozen=True)
class ArchitectureDomain:
    name: str
    token: str
    role: str
    asil: ASIL
    components: tuple[str, ...]
    title: str = ""
    description: str = ""


@dataclass(frozen=True)
class ArchitectureElement:
    name: str
    type: str
    size_bytes: int


@dataclass(frozen=True)
class ArchitectureArgument:
    name: str
    direction: str
    type: str
    size_bytes: int


@dataclass(frozen=True)
class ArchitectureInterface:
    name: str
    kind: str
    data_elements: tuple[ArchitectureElement, ...] = ()
    arguments: tuple[ArchitectureArgument, ...] = ()
    operation: str = "ExecuteCommand"
    description: str = ""


@dataclass(frozen=True)
class ArchitectureConnector:
    name: str
    kind: str
    provider: str
    consumer: str
    provider_port: str
    consumer_port: str
    interface: str
    provider_runnable: str
    consumer_runnable: str
    period_us: int
    payload_bytes: int
    request_bytes: int = 0
    response_bytes: int = 0
    motif: str = "background"
    owner: str | None = None
    provider_domain_port: str | None = None
    consumer_domain_port: str | None = None


@dataclass(frozen=True)
class ArchitectureRequirement:
    id: str
    title: str
    description: str
    asil: ASIL
    components: tuple[str, ...]


@dataclass(frozen=True)
class RootComposition:
    name: str
    root: bool
    members: tuple[str, ...]


@dataclass(frozen=True)
class Architecture:
    name: str
    domains: tuple[ArchitectureDomain, ...]
    components: tuple[ArchitectureComponent, ...]
    data_types: tuple[DataType, ...]
    interfaces: tuple[ArchitectureInterface, ...]
    connectors: tuple[ArchitectureConnector, ...]
    requirements: tuple[ArchitectureRequirement, ...]
    root: RootComposition
    metadata: tuple[tuple[str, Any], ...]


ProjectDraft = Architecture


def _metadata(**values: Any) -> tuple[tuple[str, Any], ...]:
    return tuple((key, values[key]) for key in values)


def _meta(architecture: Architecture, key: str) -> Any:
    return dict(architecture.metadata)[key]


def resolve_type_size(data_types: Iterable[DataType], type_name: str) -> int:
    """Resolve an exact byte size, rejecting missing references and cycles."""
    table = {item.name: item for item in data_types}
    active: set[str] = set()

    def visit(name: str) -> int:
        if name not in table:
            raise GenerationError(f"unresolved data type reference: {name}")
        if name in active:
            raise GenerationError(f"cyclic data type reference: {name}")
        active.add(name)
        item = table[name]
        if isinstance(item, BaseType):
            if item.size_bits % 8:
                raise GenerationError(f"base type {name} has non-byte size")
            size = item.size_bits // 8
        elif isinstance(item, ValueType):
            size = visit(item.base)
        elif isinstance(item, ArrayType):
            if item.length <= 0:
                raise GenerationError(f"array type {name} has non-positive length")
            size = visit(item.element_type) * item.length
        elif isinstance(item, RecordType):
            size = sum(visit(field.type) for field in item.fields)
        else:
            raise GenerationError(f"unsupported data type: {name}")
        active.remove(name)
        return size

    return visit(type_name)


def _representative_sizes(preset: Preset) -> tuple[int, ...]:
    """Use the lower representative of grouped WATERS size buckets."""
    values: list[int] = []
    for weighted in preset.signal_size_bytes:
        value = weighted.value
        if value <= 4:
            representative = value
        elif value <= 8:
            representative = 5
        elif value <= 16:
            representative = 9
        elif value <= 32:
            representative = 17
        elif value <= 64:
            representative = 33
        else:
            representative = 65
        if representative not in values:
            values.append(representative)
    return tuple(values)


def _asil_compatible(provider: ASIL, consumer: ASIL) -> bool:
    # Explicit QM is only compatible with QM.  Rated domains may communicate
    # across levels; this is a conservative, non-ordinal policy for v1.
    return (provider is ASIL.QM and consumer is ASIL.QM) or (
        provider not in (ASIL.QM, ASIL.ABSENT) and consumer not in (ASIL.QM, ASIL.ABSENT)
    )


def asil_compatible(provider: ASIL, consumer: ASIL) -> bool:
    """Public ASIL communication policy."""
    return _asil_compatible(provider, consumer)


def _round_fraction(value: Fraction) -> int:
    return (value.numerator * 2 + value.denominator) // (2 * value.denominator)


def _sample_execution(rng: SplitMix64, params: ExecutionTimeParameters) -> Decimal:
    # Integer microsecond interpolation keeps the raw draw exact and bounded.
    lo = params.minimum_us
    span = params.maximum_us - lo
    draw = rng.next_u64()
    return lo + span * Decimal(draw) / Decimal(1 << 64)


def _wcet(raw: Decimal, factor: Fraction, params: ExecutionTimeParameters) -> int:
    value = raw * Decimal(factor.numerator) / Decimal(factor.denominator)
    low = max(1, int(params.minimum_us.to_integral_value(rounding="ROUND_CEILING")))
    high = max(low, int(params.maximum_us.to_integral_value(rounding="ROUND_FLOOR")))
    return min(high, max(low, int(value.to_integral_value(rounding="ROUND_HALF_UP"))))


def _calibrate(
    raws: list[tuple[Decimal, int, ExecutionTimeParameters]], target: Fraction
) -> tuple[Fraction, list[int]]:
    if not raws:
        raise GenerationError("calibration has no runnables")
    raw_load = sum((Fraction(int(raw), period) for raw, period, _ in raws), Fraction(0))
    min_load = sum(
        (
            Fraction(max(1, int(params.minimum_us.to_integral_value(rounding="ROUND_CEILING"))), period)
            for _, period, params in raws
        ),
        Fraction(0),
    )
    max_load = sum(
        (
            Fraction(max(1, int(params.maximum_us.to_integral_value(rounding="ROUND_FLOOR"))), period)
            for _, period, params in raws
        ),
        Fraction(0),
    )
    if not min_load <= target <= max_load:
        raise GenerationError(f"target calibration infeasible: target={target}, reachable=[{min_load}, {max_load}]")
    factor = target / raw_load if raw_load else Fraction(1)
    values = []
    for raw, _, params in raws:
        values.append(_wcet(raw, factor, params))

    # Deterministic residual allocation, preserving raw order and bounds.
    def load() -> Fraction:
        return sum((Fraction(value, period) for value, (_, period, _) in zip(values, raws)), Fraction(0))

    while load() < target:
        changed = False
        for index, (_, period, params) in enumerate(raws):
            high = max(1, int(params.maximum_us.to_integral_value(rounding="ROUND_FLOOR")))
            if values[index] < high:
                candidate = load() + Fraction(1, period)
                if candidate <= target:
                    values[index] += 1
                    changed = True
        if not changed:
            break
    return factor, values


def _type_catalogue(sizes: tuple[int, ...]) -> tuple[DataType, ...]:
    types: list[DataType] = [
        BaseType("u8_base", 8, "NONE", "uint8_t"),
        ValueType("u8", "u8_base"),
        BaseType("u16_base", 16, "NONE", "uint16_t"),
        ValueType("u16", "u16_base"),
        BaseType("u32_base", 32, "NONE", "uint32_t"),
        ValueType("u32", "u32_base"),
    ]
    for size in sizes:
        if size <= 4:
            continue
        types.append(ArrayType(f"payload_{size}B", "u8", size))
    return tuple(types)


def _type_for_size(size: int) -> str:
    return {1: "u8", 2: "u16", 4: "u32"}.get(size, f"payload_{size}B")


def _pick_pair(rng: SplitMix64, pairs: list[tuple[str, str]], count: int) -> list[tuple[str, str]]:
    return sample_without_replacement(rng, pairs, min(count, len(pairs)))


@dataclass(frozen=True)
class _DrawnDomains:
    """The domains and the uncalibrated components, with each component's drawn runnables (``pending``, aligned)."""

    domains: list[ArchitectureDomain]
    components: list[ArchitectureComponent]
    component_domain: dict[str, str]
    pending: list[list[ArchitectureRunnable]]


@dataclass(frozen=True)
class _Calibration:
    """The calibrated components and the figures the metadata records."""

    components: list[ArchitectureComponent]
    raw_rows: list[tuple[Decimal, int, ExecutionTimeParameters]]
    calibrated: list[int]
    target: Fraction
    factor: Fraction


@dataclass
class _Links:
    """The selected edges, in order, and the candidate pairs they were drawn from; the motifs append to it."""

    selected: list[tuple[str, str, str]]
    selected_pairs: set[tuple[str, str]]
    pairs_by_domain: list[tuple[str, str]]
    inter_pairs: list[tuple[str, str]]


@dataclass
class _Edges:
    """What the edge loop builds: the type catalogue, the lists every edge appends to, the updated runnables."""

    data_types: tuple[DataType, ...]
    connectors: list[ArchitectureConnector]
    interfaces: list[ArchitectureInterface]
    port_lists: dict[str, list[ArchitecturePort]]
    mutable_runs: dict[tuple[str, str], ArchitectureRunnable]


@dataclass(frozen=True)
class _EdgeInterface:
    """One edge's interface and the connector figures it implies."""

    interface: ArchitectureInterface
    payload: int
    request: int
    response: int


def build_architecture(preset: Preset, rng: SplitMix64 | None = None) -> Architecture:
    """Generate one complete architecture from ``preset``, drawing from ``rng`` in a fixed order.

    ``rng`` defaults to ``SplitMix64(preset.seed)``. The draws come in this order, and the order is the
    contract every preset fingerprint relies on: the domain sizes and the domain ASILs, then per
    component the runnable count and per runnable its period and raw execution time
    (:func:`_draw_domains`); the intra-domain then the inter-domain link picks (:func:`_select_links`);
    per edge the arguments or the data elements, then the provider and the consumer runnable
    (:func:`_build_edges`). Calibration, the motifs, the final components and the requirements draw
    nothing. :func:`_assemble` records the generator's state after the last draw as ``prng_state``.
    The result passes :func:`validate_architecture`; raises :class:`GenerationError` when the preset
    cannot be met or the result is invalid, except an illegal or duplicate runnable name, which
    :func:`validate_architecture` raises as :class:`ValueError`.
    """
    rng = rng or SplitMix64(preset.seed)
    drawn = _draw_domains(preset, rng)
    calibration = _calibrate_components(preset, drawn)
    links = _select_links(preset, drawn.domains, drawn.component_domain, calibration.components, rng)
    _append_motif_edges(drawn.domains, calibration.components, links)
    edges = _build_edges(preset, links.selected, calibration.components, rng)
    components = _final_components(calibration.components, edges)
    requirements = _requirements(drawn.domains, components)
    arch = _assemble(drawn.domains, components, requirements, calibration, links, edges, rng)
    validate_architecture(arch, preset)
    return arch


def _domain_sizes(preset: Preset, rng: SplitMix64) -> list[int]:
    """Draw the component count of each domain, then rebalance it so that no domain holds over 25.

    Raises :class:`GenerationError` when the preset has more domains than roles, or when the
    rebalance cannot keep every domain at or below 25.
    """
    if preset.domains > len(DOMAIN_ROLES):
        raise GenerationError(f"domains={preset.domains} exceeds domain role catalogue size {len(DOMAIN_ROLES)}")
    sizes = uneven_partition(preset.components, preset.domains, rng)
    # Showcase invariant: a production domain remains readable in SWCraft.
    # Rebalance only the deterministic partition result; this consumes no draws.
    while max(sizes) > 25:
        donor = max(range(len(sizes)), key=lambda i: (sizes[i], -i))
        receiver = min(range(len(sizes)), key=lambda i: (sizes[i], i))
        if sizes[donor] <= 25 or sizes[receiver] >= 25:
            raise GenerationError("cannot keep production domain composition at or below 25 SWCs")
        sizes[donor] -= 1
        sizes[receiver] += 1
    return sizes


def _architectural_role(name: str, position: int, domain_size: int) -> str:
    """Return the architectural role of the component ``name`` at ``position`` in its domain."""
    if name in {"CentralCommunicationGateway", "VehicleStateService"}:
        architectural_role = "domain_gateway"
    elif name == "DiagnosticEventRouter":
        architectural_role = "diagnostics"
    elif name == "SafetyStateMonitor":
        architectural_role = "safety_monitor"
    elif name in {
        "TorqueRequestCoordinator",
        "VehicleSpeedProvider",
        "ThermalCoordinationManager",
    }:
        architectural_role = "shared_service"
    elif position == 0 and domain_size > 1:
        architectural_role = "domain_gateway"
    else:
        architectural_role = "regular"
    return architectural_role


def _draw_domains(preset: Preset, rng: SplitMix64) -> _DrawnDomains:
    """Draw the domain sizes and ASILs, then each domain's components and their runnables.

    Each runnable carries its drawn period and raw execution time and a placeholder WCET; the
    components carry no runnables or ports yet. Raises as :func:`_domain_sizes` does.
    """
    sizes = _domain_sizes(preset, rng)
    asil_items = [item for item, _ in preset.asil_weights]
    asil_weights = [weight for _, weight in preset.asil_weights]
    domain_asils = [weighted_choice(rng, asil_items, asil_weights) for _ in range(preset.domains)]

    domains: list[ArchitectureDomain] = []
    components: list[ArchitectureComponent] = []
    component_domain: dict[str, str] = {}
    # Identities and runnable count/period/raw draws are created in stable order.
    pending: list[list[ArchitectureRunnable]] = []
    for domain_index, (domain_size, role, asil) in enumerate(zip(sizes, DOMAIN_ROLES, domain_asils), 1):
        token, title, description, _ = domain_catalog(domain_index)
        domain_name = f"{title}Domain"
        names: list[str] = []
        for position in range(domain_size):
            name = semantic_component_name(domain_index, position)
            names.append(name)
            component_domain[name] = domain_name
            architectural_role = _architectural_role(name, position, domain_size)
            count = (
                rng.bounded(preset.runnables_per_component.maximum - preset.runnables_per_component.minimum + 1)
                + preset.runnables_per_component.minimum
            )
            pending.append([])
            for runnable_index in range(1, count + 1):
                period = weighted_choice(rng, [p for p, _ in preset.periods_us], [w for _, w in preset.periods_us])
                params = dict(preset.wcet_us_by_period)[period]
                raw = _sample_execution(rng, params)
                pending[-1].append(
                    ArchitectureRunnable(semantic_runnable_name(runnable_index), period, raw, 1, Fraction(1))
                )
            components.append(
                ArchitectureComponent(
                    name,
                    domain_name,
                    role,
                    asil,
                    architectural_role,
                    (),
                    (),
                    f"Synthetic {name} component providing {role.replace('_', ' ')} behavior.",
                )
            )
        domains.append(ArchitectureDomain(domain_name, token, role, asil, tuple(names), title, description))
    return _DrawnDomains(domains, components, component_domain, pending)


def _calibrate_components(preset: Preset, drawn: _DrawnDomains) -> _Calibration:
    """Calibrate every runnable's WCET to the preset's target load and give the components their runnables.

    Draws nothing. Raises :class:`GenerationError` when the target cannot be reached.
    """
    raw_rows: list[tuple[Decimal, int, ExecutionTimeParameters]] = []
    for runnables in drawn.pending:
        raw_rows.extend(
            (r.raw_execution_us, r.period_us, dict(preset.wcet_us_by_period)[r.period_us]) for r in runnables
        )
    requested_target = Fraction(preset.target_core_load) * max(1, len(preset.cores))
    reachable_max = sum(
        (
            Fraction(max(1, int(params.maximum_us.to_integral_value(rounding="ROUND_FLOOR"))), period)
            for _, period, params in raw_rows
        ),
        Fraction(0),
    )
    target = min(requested_target, reachable_max)
    factor, calibrated = _calibrate(raw_rows, target)
    offset = 0
    rebuilt: list[ArchitectureComponent] = []
    for component, runnables in zip(drawn.components, drawn.pending):
        new_runs = tuple(
            r.__class__(r.name, r.period_us, r.raw_execution_us, calibrated[offset + i], factor)
            for i, r in enumerate(runnables)
        )
        offset += len(runnables)
        rebuilt.append(
            component.__class__(
                component.name,
                component.domain,
                component.domain_role,
                component.asil,
                component.architectural_role,
                new_runs,
                (),
                component.description,
            )
        )
    return _Calibration(rebuilt, raw_rows, calibrated, target, factor)


def _select_links(
    preset: Preset,
    domains: list[ArchitectureDomain],
    component_domain: dict[str, str],
    components: list[ArchitectureComponent],
    rng: SplitMix64,
) -> _Links:
    """Select the edges: each domain's connectivity chain, then the intra- and the inter-domain picks.

    Draws the intra-domain picks first, then the inter-domain ones; an inter-domain pair joins two
    ASIL-compatible components.
    """
    component_by_name = {component.name: component for component in components}
    # Canonical candidate pairs; connectivity is selected before exact density extras.
    pairs_by_domain: list[tuple[str, str]] = []
    for domain in domains:
        pairs_by_domain.extend((a, b) for a in domain.components for b in domain.components if a != b)
    selected: list[tuple[str, str, str]] = []
    selected_pairs: set[tuple[str, str]] = set()
    for domain in domains:
        for left, right in zip(domain.components, domain.components[1:]):
            pair = (left, right)
            if pair not in selected_pairs:
                selected.append((left, right, "connectivity"))
                selected_pairs.add(pair)
    intra_count = _round_fraction(Fraction(preset.intra_domain_link_density) * len(pairs_by_domain))
    intra_extra = _pick_pair(
        rng,
        [p for p in pairs_by_domain if p not in selected_pairs],
        max(0, intra_count - len(selected)),
    )
    for pair in intra_extra:
        selected.append((pair[0], pair[1], "background"))
        selected_pairs.add(pair)
    inter_pairs = [
        (a, b)
        for a in (c.name for c in components)
        for b in (c.name for c in components)
        if a != b
        and component_domain[a] != component_domain[b]
        and _asil_compatible(component_by_name[a].asil, component_by_name[b].asil)
    ]
    inter_count = _round_fraction(Fraction(preset.inter_domain_link_density) * len(inter_pairs))
    for pair in _pick_pair(rng, [p for p in inter_pairs if p not in selected_pairs], inter_count):
        selected.append((pair[0], pair[1], "background"))
        selected_pairs.add(pair)
    return _Links(selected, selected_pairs, pairs_by_domain, inter_pairs)


def _append_motif_edges(
    domains: list[ArchitectureDomain],
    components: list[ArchitectureComponent],
    links: _Links,
) -> None:
    """Append the motif edges that are not selected yet and join ASIL-compatible components to ``links``.

    Draws nothing.
    """
    selected = links.selected
    selected_pairs = links.selected_pairs
    component_by_name = {component.name: component for component in components}
    # Bounded motifs use existing endpoints and are applied after background sampling.
    motif_pairs: list[tuple[str, str, str]] = []
    gateways = [d.components[0] for d in domains if len(d.components) > 1]
    if len(gateways) >= 2:
        motif_pairs.append((gateways[0], gateways[1], "domain_gateway"))
    if len(components) >= 3:
        motif_pairs.append((components[0].name, components[1].name, "shared_service"))
        motif_pairs.append((components[-1].name, components[0].name, "diagnostics_aggregation"))
    safety = next((c.name for c in components if c.architectural_role == "safety_monitor"), None)
    if safety and safety != components[0].name:
        motif_pairs.append((safety, components[0].name, "safety_monitoring"))
    for motif_pair in motif_pairs:
        if motif_pair[:2] not in selected_pairs and _asil_compatible(
            component_by_name[motif_pair[0]].asil, component_by_name[motif_pair[1]].asil
        ):
            selected.append(motif_pair)
            selected_pairs.add(motif_pair[:2])


def _build_edges(
    preset: Preset,
    selected: list[tuple[str, str, str]],
    components: list[ArchitectureComponent],
    rng: SplitMix64,
) -> _Edges:
    """Build the type catalogue, then one interface, two ports and one connector per selected edge, in order.

    The first ``client_server_share`` of the edges are client-server, the rest sender-receiver. Each
    edge draws through :func:`_build_edge`. Raises :class:`GenerationError` when the preset has no
    payload size.
    """
    payload_sizes = _representative_sizes(preset)
    if not payload_sizes:
        raise GenerationError("signal_size_bytes has no representative payload size")
    data_types = _type_catalogue(payload_sizes)
    component_by_name = {component.name: component for component in components}
    edges = _Edges(
        data_types,
        [],
        [],
        {c.name: [] for c in components},
        {(c.name, r.name): r for c in components for r in c.runnables},
    )
    cs_target = _round_fraction(Fraction(preset.client_server_share) * len(selected))
    for edge_index, edge in enumerate(selected, 1):
        _build_edge(edges, edge_index, edge, edge_index <= cs_target, payload_sizes, component_by_name, rng)
    return edges


def _edge_signal(
    edge_index: int, motif: str, provider_component: ArchitectureComponent, consumer_component: ArchitectureComponent
) -> str:
    """Return the signal name of one edge: from the catalogue by position, or the name its motif or domains imply."""
    provider, consumer = provider_component.name, consumer_component.name
    signal = SIGNAL_NAMES[(edge_index - 1) % len(SIGNAL_NAMES)]
    if motif == "diagnostics_aggregation" or "Diagnostic" in provider or "Diagnostic" in consumer:
        signal = "DiagnosticEvent"
    elif motif == "safety_monitoring":
        signal = "SafetyState"
    elif provider_component.domain != consumer_component.domain and edge_index == 1:
        signal = "VehicleMode"
    return signal


def _build_edge(
    edges: _Edges,
    edge_index: int,
    edge: tuple[str, str, str],
    is_cs: bool,
    payload_sizes: tuple[int, ...],
    component_by_name: dict[str, ArchitectureComponent],
    rng: SplitMix64,
) -> None:
    """Append one edge's interface, ports and connector to ``edges`` and record its runnables' accesses.

    Draws the interface's arguments or data elements, then the provider and the consumer runnable.
    """
    provider, consumer, motif = edge
    interfaces, connectors = edges.interfaces, edges.connectors
    provider_component = component_by_name[provider]
    consumer_component = component_by_name[consumer]
    signal = _edge_signal(edge_index, motif, provider_component, consumer_component)
    suffix = 1
    interface_name = f"If_{signal}"
    while interface_name in {item.name for item in interfaces}:
        suffix += 1
        interface_name = f"If_{signal}_{suffix:02d}"
    drawn = _edge_interface(is_cs, signal, interface_name, edge_index, payload_sizes, rng)
    interfaces.append(drawn.interface)
    kind = drawn.interface.kind
    endpoints = (provider, consumer)
    ports = _edge_ports(edges.port_lists, endpoints, (f"Provide{signal}", f"Require{signal}"), interface_name)
    provider_port, consumer_port = ports
    p_run, c_run = _edge_runnables(edges.mutable_runs, component_by_name, endpoints, is_cs, ports, drawn.interface, rng)
    connector_base = f"Conn_{signal}_To_{consumer_component.name}"
    connector_name = connector_base
    connector_suffix = 1
    while connector_name in {item.name for item in connectors}:
        connector_suffix += 1
        connector_name = f"{connector_base}_{connector_suffix:02d}"
    connectors.append(
        ArchitectureConnector(
            connector_name,
            kind,
            provider,
            consumer,
            provider_port,
            consumer_port,
            interface_name,
            p_run.name,
            c_run.name,
            p_run.period_us if kind == "sender_receiver" else c_run.period_us,
            drawn.payload,
            drawn.request,
            drawn.response,
            motif,
            None if provider_component.domain != consumer_component.domain else provider_component.domain,
        )
    )


def _edge_interface(
    is_cs: bool,
    signal: str,
    interface_name: str,
    edge_index: int,
    payload_sizes: tuple[int, ...],
    rng: SplitMix64,
) -> _EdgeInterface:
    """Draw one edge's interface: three arguments for a client-server edge, else its data elements.

    A client-server edge draws one payload size per argument; a sender-receiver edge draws its
    element count, then per element a type size and a size, and keeps the size.
    """
    if is_cs:
        argument_sizes = [payload_sizes[rng.bounded(len(payload_sizes))] for _ in range(3)]
        directions = ("in", "out", "inout")
        arguments = tuple(
            ArchitectureArgument(ARGUMENT_NAMES[n - 1], direction, _type_for_size(size), size)
            for n, (direction, size) in enumerate(zip(directions, argument_sizes), 1)
        )
        operation = SERVICE_NAMES[(edge_index - 1) % len(SERVICE_NAMES)]
        interface = ArchitectureInterface(
            interface_name,
            "client_server",
            arguments=arguments,
            operation=operation,
            description=f"Synthetic {operation} service for {signal}.",
        )
        request = sum(a.size_bytes for a in arguments if a.direction in ("in", "inout"))
        response = sum(a.size_bytes for a in arguments if a.direction in ("out", "inout"))
        payload = 0
    else:
        element_count = rng.bounded(8) + 1
        elements = tuple(
            ArchitectureElement(
                signal if n == 1 else f"{signal}_{n:02d}",
                _type_for_size(payload_sizes[rng.bounded(len(payload_sizes))]),
                payload_sizes[rng.bounded(len(payload_sizes))],
            )
            for n in range(1, element_count + 1)
        )
        # Make declared type and size agree even when two draws differ.
        elements = tuple(ArchitectureElement(e.name, _type_for_size(e.size_bytes), e.size_bytes) for e in elements)
        interface = ArchitectureInterface(
            interface_name,
            "sender_receiver",
            data_elements=elements,
            description=f"Synthetic {signal} sender-receiver stream.",
        )
        request = response = 0
        payload = sum(e.size_bytes for e in elements)
    return _EdgeInterface(interface, payload, request, response)


def _edge_ports(
    port_lists: dict[str, list[ArchitecturePort]],
    endpoints: tuple[str, str],
    ports: tuple[str, str],
    interface_name: str,
) -> tuple[str, str]:
    """Append one edge's provide and require ports, suffixed until both names are free; return the names.

    ``endpoints`` is the (provider, consumer) pair and ``ports`` the (provide, require) port names to start
    from, the convention :func:`_edge_runnables` shares.
    """
    provider, consumer = endpoints
    provider_port, consumer_port = ports
    existing_provider_ports = {port.name for port in port_lists[provider]}
    existing_consumer_ports = {port.name for port in port_lists[consumer]}
    port_suffix = 1
    original_provider_port, original_consumer_port = provider_port, consumer_port
    while provider_port in existing_provider_ports or consumer_port in existing_consumer_ports:
        port_suffix += 1
        provider_port = f"{original_provider_port}_{port_suffix:02d}"
        consumer_port = f"{original_consumer_port}_{port_suffix:02d}"
    port_lists[provider].append(ArchitecturePort(provider_port, "provide", interface_name))
    port_lists[consumer].append(ArchitecturePort(consumer_port, "require", interface_name))
    return provider_port, consumer_port


def _edge_runnables(
    mutable_runs: dict[tuple[str, str], ArchitectureRunnable],
    component_by_name: dict[str, ArchitectureComponent],
    endpoints: tuple[str, str],
    is_cs: bool,
    ports: tuple[str, str],
    interface: ArchitectureInterface,
    rng: SplitMix64,
) -> tuple[ArchitectureRunnable, ArchitectureRunnable]:
    """Draw the provider's runnable, then the consumer's, and record the edge's call or data accesses.

    A client-server edge adds a call to the consumer's runnable; a sender-receiver edge adds the
    provider's writes and the consumer's reads of every data element. Returns the two drawn runnables.
    """
    provider, consumer = endpoints
    provider_port, consumer_port = ports
    p_run = component_by_name[provider].runnables[rng.bounded(len(component_by_name[provider].runnables))]
    c_run = component_by_name[consumer].runnables[rng.bounded(len(component_by_name[consumer].runnables))]
    if is_cs:
        updated = mutable_runs[(consumer, c_run.name)]
        mutable_runs[(consumer, c_run.name)] = updated.__class__(
            updated.name,
            updated.period_us,
            updated.raw_execution_us,
            updated.wcet_us,
            updated.calibration_factor,
            updated.writes,
            updated.reads,
            updated.calls + ((consumer_port, interface.operation),),
        )
    else:
        pi = mutable_runs[(provider, p_run.name)]
        ci = mutable_runs[(consumer, c_run.name)]
        mutable_runs[(provider, p_run.name)] = pi.__class__(
            pi.name,
            pi.period_us,
            pi.raw_execution_us,
            pi.wcet_us,
            pi.calibration_factor,
            pi.writes + tuple((provider_port, element.name) for element in interface.data_elements),
            pi.reads,
            pi.calls,
        )
        mutable_runs[(consumer, c_run.name)] = ci.__class__(
            ci.name,
            ci.period_us,
            ci.raw_execution_us,
            ci.wcet_us,
            ci.calibration_factor,
            ci.writes,
            ci.reads + tuple((consumer_port, element.name) for element in interface.data_elements),
            ci.calls,
        )
    return p_run, c_run


def _final_components(components: list[ArchitectureComponent], edges: _Edges) -> list[ArchitectureComponent]:
    """Return the components with the runnables and ports the edge loop gave them. Draws nothing."""
    final_components: list[ArchitectureComponent] = []
    for component in components:
        final_components.append(
            component.__class__(
                component.name,
                component.domain,
                component.domain_role,
                component.asil,
                component.architectural_role,
                tuple(edges.mutable_runs[(component.name, r.name)] for r in component.runnables),
                tuple(edges.port_lists[component.name]),
                component.description,
            )
        )
    return final_components


def _requirements(
    domains: list[ArchitectureDomain], components: list[ArchitectureComponent]
) -> tuple[ArchitectureRequirement, ...]:
    """Return one requirement per domain, then one per ASIL over the components with a special role."""
    requirement_titles = {
        "PowertrainDomain": (
            "REQ_POWERTRAIN_CONTROL",
            "Synthetic powertrain coordination behavior",
        ),
        "ChassisDomain": (
            "REQ_VEHICLE_STATE_ESTIMATION",
            "Synthetic vehicle-state estimation behavior",
        ),
        "VehicleServicesDomain": (
            "REQ_DIAGNOSTIC_REPORTING",
            "Synthetic diagnostic event reporting behavior",
        ),
        "SafetySupervisionDomain": (
            "REQ_SAFETY_SUPERVISION",
            "Synthetic safety supervision behavior",
        ),
    }
    requirement_rows = []
    for d in domains:
        rid, title = requirement_titles.get(
            d.name,
            (f"REQ_{d.name.removesuffix('Domain').upper()}", f"Synthetic {d.title} behavior"),
        )
        requirement_rows.append(
            ArchitectureRequirement(
                rid,
                title,
                f"This synthetic model shall provide deterministic {d.title.lower()} behavior; "
                "it is not a real product requirement.",
                d.asil,
                d.components,
            )
        )
    requirements = tuple(requirement_rows)
    special = tuple(c.name for c in components if c.architectural_role != "regular")
    if special:
        for asil in ASIL_LEVELS:
            group = tuple(c.name for c in components if c.name in special and c.asil is asil)
            if group:
                requirements += (
                    ArchitectureRequirement(
                        f"REQ_{asil.value}_ARCHITECTURE_INTERACTION",
                        "Synthetic architectural interaction",
                        "This synthetic model shall preserve traceable interactions among demonstration components.",
                        asil,
                        group,
                    ),
                )
    return requirements


def _assemble(
    domains: list[ArchitectureDomain],
    components: list[ArchitectureComponent],
    requirements: tuple[ArchitectureRequirement, ...],
    calibration: _Calibration,
    links: _Links,
    edges: _Edges,
    rng: SplitMix64,
) -> Architecture:
    """Assemble the architecture and its metadata, recording ``rng``'s state as ``prng_state``. Draws nothing."""
    raw_rows = calibration.raw_rows
    target = calibration.target
    selected = links.selected
    achieved_load = sum(
        (Fraction(value, period) for value, (_, period, _) in zip(calibration.calibrated, raw_rows)),
        Fraction(0),
    )
    return Architecture(
        "SyntheticVehicleArchitecture",
        tuple(domains),
        tuple(components),
        edges.data_types,
        tuple(edges.interfaces),
        tuple(edges.connectors),
        requirements,
        RootComposition("SyntheticVehicleArchitecture", True, tuple(d.name for d in domains)),
        _metadata(
            generation_order=(
                "domain_sizes",
                "domain_asil",
                "component_identities",
                "runnable_counts",
                "periods",
                "raw_execution",
                "load_calibration",
                "graph_edges",
                "communication_kinds",
                "payloads_arguments",
            ),
            calibration_factor=calibration.factor,
            target_load=target,
            raw_load=sum((Fraction(int(raw), period) for raw, period, _ in raw_rows), Fraction(0)),
            achieved_load=achieved_load,
            residual=target - achieved_load,
            calibration_tolerance=Fraction(1, 100),
            selected_connectivity_edges=sum(1 for x in selected if x[2] == "connectivity"),
            selected_density_edges=sum(1 for x in selected if x[2] == "background"),
            selected_motif_edges=sum(1 for x in selected if x[2] not in ("background", "connectivity")),
            intra_candidates=len(links.pairs_by_domain),
            inter_candidates=len(links.inter_pairs),
            density_rounding="nearest integer, half-up",
            connectivity_priority=any(len(d.components) > 1 for d in domains),
            asil_policy="QM only with QM; rated ASILs may communicate",
            phase_boundary="the architecture places no hardware, tasks, or memory budgets",
            prng_state=rng.state,
        ),
    )


def validate_architecture(architecture: Architecture, preset: Preset) -> None:
    """Check ``architecture`` against ``preset`` and the generator's invariants; return ``None`` when it holds.

    Raises :class:`GenerationError` at the first violation, naming the domain, component, connector,
    interface or element it concerns, except an illegal or duplicate runnable name, which
    :func:`_check_runnables` raises as the :class:`ValueError` of ``validate_unique_identifiers``.
    The checks run in this order: cardinality, calibration and names; the domains; the connector
    identities; the runnables; each connector; the requirements' domains; the data type sizes; each
    domain's connectivity.
    """
    _check_cardinality(architecture, preset)
    components = {c.name: c for c in architecture.components}
    _check_domains(architecture, preset, components)
    ports = {(c.name, p.name): p for c in architecture.components for p in c.ports}
    interfaces = {i.name: i for i in architecture.interfaces}
    if len(architecture.connectors) != len({c.name for c in architecture.connectors}):
        raise GenerationError("duplicate connector identity")
    _check_runnables(architecture, preset)
    for connector in architecture.connectors:
        _check_connector(architecture, connector, components, ports, interfaces)
    requirement_domains = {
        component.domain
        for r in architecture.requirements
        for component in architecture.components
        if component.name in r.components
    }
    if len(requirement_domains) < preset.domains:
        raise GenerationError("each domain must have at least one semantic requirement")
    _check_type_sizes(architecture)
    _check_domain_connectivity(architecture)


def _check_cardinality(architecture: Architecture, preset: Preset) -> None:
    """Check the domain and component counts, the calibration residual, the component names and the root."""
    if len(architecture.domains) != preset.domains or len(architecture.components) != preset.components:
        raise GenerationError(
            f"cardinality mismatch: domains={len(architecture.domains)}/{preset.domains}, "
            f"components={len(architecture.components)}/{preset.components}"
        )
    metadata = dict(architecture.metadata)
    if abs(metadata["residual"]) > metadata["calibration_tolerance"]:
        raise GenerationError(
            f"calibration residual exceeds tolerance: {metadata['residual']} > {metadata['calibration_tolerance']}"
        )
    try:
        validate_unique_identifiers((c.name for c in architecture.components), "component names")
    except ValueError as exc:
        raise GenerationError(str(exc)) from None
    if len(architecture.root.members) != preset.domains or architecture.root.members != tuple(
        d.name for d in architecture.domains
    ):
        raise GenerationError("Root must contain every domain composition exactly once in canonical order")


def _check_domains(architecture: Architecture, preset: Preset, components: dict[str, ArchitectureComponent]) -> None:
    """Check the domain sizes, then each domain's role, membership and ASIL, in domain order."""
    if sum(len(d.components) for d in architecture.domains) != preset.components or any(
        not d.components for d in architecture.domains
    ):
        raise GenerationError("domain sizes must be positive and sum to component count")
    for d in architecture.domains:
        if d.role != DOMAIN_ROLES[architecture.domains.index(d)] or any(
            components[n].domain != d.name for n in d.components
        ):
            raise GenerationError(f"domain catalogue or membership invalid: {d.name}")
        if any(components[n].asil is not d.asil for n in d.components):
            raise GenerationError(f"domain ASIL mismatch: {d.name}")


def _check_runnables(architecture: Architecture, preset: Preset) -> None:
    """Check each component's runnable count and names, then each runnable's period and execution bounds.

    A bad runnable name raises ``validate_unique_identifiers``' :class:`ValueError`, unwrapped; every
    other violation raises :class:`GenerationError`.
    """
    for c in architecture.components:
        if not preset.runnables_per_component.minimum <= len(c.runnables) <= preset.runnables_per_component.maximum:
            raise GenerationError(f"runnable count out of bounds: {c.name}")
        validate_unique_identifiers((r.name for r in c.runnables), f"runnable names in {c.name}")
        for r in c.runnables:
            params = dict(preset.wcet_us_by_period)[r.period_us]
            low = max(1, int(params.minimum_us.to_integral_value(rounding="ROUND_CEILING")))
            high = max(low, int(params.maximum_us.to_integral_value(rounding="ROUND_FLOOR")))
            if (
                r.period_us not in dict(preset.periods_us)
                or not low <= r.wcet_us <= high
                or r.raw_execution_us < params.minimum_us
                or r.raw_execution_us > params.maximum_us
            ):
                raise GenerationError(f"timing/execution bounds invalid: {c.name}.{r.name}")


def _check_connector(
    architecture: Architecture,
    connector: ArchitectureConnector,
    components: dict[str, ArchitectureComponent],
    ports: dict[tuple[str, str], ArchitecturePort],
    interfaces: dict[str, ArchitectureInterface],
) -> None:
    """Check one connector: its endpoints, ASILs, ports and interface, then its runnables and accounting."""
    if (
        connector.provider == connector.consumer
        or connector.provider not in components
        or connector.consumer not in components
    ):
        raise GenerationError(f"invalid connector endpoints: {connector.name}")
    if not _asil_compatible(components[connector.provider].asil, components[connector.consumer].asil):
        raise GenerationError(f"ASIL-incompatible connector: {connector.name}")
    if (connector.provider, connector.provider_port) not in ports or (
        connector.consumer,
        connector.consumer_port,
    ) not in ports:
        raise GenerationError(f"orphaned connector port: {connector.name}")
    interface = interfaces.get(connector.interface)
    if interface is None or interface.kind != connector.kind:
        raise GenerationError(f"unresolved interface: {connector.name}")
    if connector.kind == "sender_receiver" and not 1 <= len(interface.data_elements) <= 8:
        raise GenerationError(f"sender-receiver element count invalid: {connector.name}")
    _check_connector_runnables(architecture, connector, interface)


def _check_connector_runnables(
    architecture: Architecture, connector: ArchitectureConnector, interface: ArchitectureInterface
) -> None:
    """Check one connector's period against its runnables, their accesses or call, and its byte accounting."""
    run_map = {(c.name, r.name): r for c in architecture.components for r in c.runnables}
    provider_run = run_map[(connector.provider, connector.provider_runnable)]
    consumer_run = run_map[(connector.consumer, connector.consumer_runnable)]
    expected_period = provider_run.period_us if connector.kind == "sender_receiver" else consumer_run.period_us
    if expected_period != connector.period_us:
        raise GenerationError(f"link frequency mismatch: {connector.name}")
    if connector.kind == "sender_receiver":
        elements = {element.name for element in interface.data_elements}
        writes = {element for port, element in provider_run.writes if port == connector.provider_port}
        reads = {element for port, element in consumer_run.reads if port == connector.consumer_port}
        if writes != elements or reads != elements:
            raise GenerationError(f"sender-receiver access mismatch: {connector.name}")
    if connector.kind == "client_server" and not any(p == connector.consumer_port for p, _ in consumer_run.calls):
        raise GenerationError(f"client call missing: {connector.name}")
    if connector.kind == "sender_receiver" and connector.payload_bytes != sum(
        e.size_bytes for e in interface.data_elements
    ):
        raise GenerationError(f"payload accounting mismatch: {connector.name}")
    if connector.kind == "client_server":
        request = sum(a.size_bytes for a in interface.arguments if a.direction in ("in", "inout"))
        response = sum(a.size_bytes for a in interface.arguments if a.direction in ("out", "inout"))
        if (request, response) != (connector.request_bytes, connector.response_bytes):
            raise GenerationError(f"request/response accounting mismatch: {connector.name}")


def _check_type_sizes(architecture: Architecture) -> None:
    """Resolve every data type's size, then check each element's and argument's size against its type."""
    for type_item in architecture.data_types:
        resolve_type_size(architecture.data_types, type_item.name)
    for interface in architecture.interfaces:
        if interface.kind == "sender_receiver":
            for element in interface.data_elements:
                if resolve_type_size(architecture.data_types, element.type) != element.size_bytes:
                    raise GenerationError(f"exact payload type size mismatch: {interface.name}.{element.name}")
        else:
            for argument in interface.arguments:
                if resolve_type_size(architecture.data_types, argument.type) != argument.size_bytes:
                    raise GenerationError(f"exact argument type size mismatch: {interface.name}.{argument.name}")


def _check_domain_connectivity(architecture: Architecture) -> None:
    """Check that the connectors inside each domain of two or more components join all of them."""
    for domain in architecture.domains:
        if len(domain.components) > 1:
            connected = {domain.components[0]}
            changed = True
            while changed:
                changed = False
                for link in architecture.connectors:
                    if (
                        link.provider in domain.components
                        and link.consumer in domain.components
                        and ((link.provider in connected) != (link.consumer in connected))
                    ):
                        connected.update((link.provider, link.consumer))
                        changed = True
            if connected != set(domain.components):
                raise GenerationError(f"domain is not weakly connected: {domain.name}")


def architecture_fingerprint(architecture: Architecture) -> str:
    """Hash a canonical, hash-seed-independent semantic representation."""
    parts: list[str] = [architecture.name]
    for d in architecture.domains:
        parts.append(f"D|{d.name}|{d.token}|{d.role}|{d.asil.value}|{','.join(d.components)}")
    for c in architecture.components:
        parts.append(f"C|{c.name}|{c.domain}|{c.architectural_role}|{c.asil.value}")
        for r in c.runnables:
            parts.append(f"R|{c.name}|{r.name}|{r.period_us}|{r.raw_execution_us}|{r.wcet_us}")
    for connector in architecture.connectors:
        parts.append(
            f"L|{connector.name}|{connector.kind}|{connector.provider}|{connector.consumer}|{connector.payload_bytes}|{connector.request_bytes}|{connector.response_bytes}|{connector.motif}"
        )
    return sha256("\n".join(parts).encode("utf-8")).hexdigest()


def build_project_draft(preset: Preset) -> Architecture:
    return build_architecture(preset)
