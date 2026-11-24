"""Canonical, YAML-independent metrics for the models the specimen generator generates.

This module knows only the typed in-memory architecture, placement and project objects;
serialization and file-system concerns live in :mod:`sidecar` and :mod:`validate`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING

from .architecture import Architecture, ArchitectureComponent
from .model import BaseType, Project, hardware_names
from .preset import ASIL, Preset

if TYPE_CHECKING:
    from .placement import PlacementResult


@dataclass(frozen=True)
class LayerCounts:
    """Element counts per layer, keyed by the pinned SWCraft release's ``swcraft list`` names.

    The field names, in this order, are the one source of the fifteen layer names: the sidecar's
    ``layer_counts`` and the ``list`` scenario's ``layers.<name>`` paths both follow them.
    ``hardware`` counts the processor, the cores, the memories and the partitions.
    """

    requirements: int
    compositions: int
    swc: int
    interfaces: int
    application_types: int
    implementation_types: int
    base_types: int
    units: int
    compu_methods: int
    data_constraints: int
    mapping_sets: int
    connectors: int
    tasks: int
    sequences: int
    hardware: int


@dataclass(frozen=True)
class DomainCount:
    domain: str
    components: int
    runnables: int


@dataclass(frozen=True)
class ComponentCount:
    component: str
    domain: str
    architectural_role: str
    asil: ASIL
    runnables: int


@dataclass(frozen=True)
class PeriodSummary:
    period_us: int
    runnables: int


@dataclass(frozen=True)
class AsilCount:
    asil: ASIL
    count: int


@dataclass(frozen=True)
class CommunicationSummary:
    total: int
    sender_receiver: int
    client_server: int
    intra_domain: int
    inter_domain: int
    planted_intra_core: int
    planted_cross_core: int
    emitted_intra_core: int
    emitted_cross_core: int
    connector_count: int
    fan_out_connectors: int


@dataclass(frozen=True)
class GraphDegree:
    component: str
    in_degree: int
    out_degree: int
    total_degree: int


@dataclass(frozen=True)
class MemoryMetric:
    memory: str
    size_bytes: int
    threshold_capacity: int
    consumption: int
    remaining_capacity: int
    accessible_cores: tuple[str, ...]


@dataclass(frozen=True)
class CoreMetric:
    core: str
    task_count: int
    non_empty_task_count: int
    load: Fraction
    threshold: Fraction
    remaining_capacity: Fraction


@dataclass(frozen=True)
class PlacementMetric:
    component_to_core: tuple[tuple[str, str], ...]
    core_metrics: tuple[CoreMetric, ...]
    sender_receiver_cost: Fraction
    client_server_cost: Fraction
    total_cost: Fraction
    displaced: tuple[str, ...]
    pinned: tuple[str, ...]
    configured_displacement_count: int
    actual_displacement_count: int


@dataclass(frozen=True)
class StableLandmarks:
    gateway_component: str | None
    shared_service_component: str | None
    diagnostics_component: str | None
    safety_monitor_component: str | None
    highest_fan_in_component: str | None
    highest_fan_out_component: str | None
    sender_receiver_interface: str | None
    client_server_interface: str | None
    representative_domain_requirement: str | None
    representative_feature_requirement: str | None


@dataclass(frozen=True)
class GenerationMetrics:
    layer_counts: LayerCounts
    domain_counts: tuple[DomainCount, ...]
    components: tuple[ComponentCount, ...]
    period_distribution: tuple[PeriodSummary, ...]
    domain_asil: tuple[AsilCount, ...]
    component_asil: tuple[AsilCount, ...]
    runnable_wcet_us: tuple[tuple[str, str, int], ...]
    communication: CommunicationSummary
    graph_degree: tuple[GraphDegree, ...]
    memory: tuple[MemoryMetric, ...]
    planted: PlacementMetric
    emitted: PlacementMetric
    landmarks: StableLandmarks


def component_cpu_demand(component: ArchitectureComponent) -> Fraction:
    return sum((Fraction(r.wcet_us, r.period_us) for r in component.runnables), Fraction())


def per_core_loads(
    architecture: Architecture, mapping: Mapping[str, str], preset: Preset
) -> tuple[tuple[str, Fraction], ...]:
    keys = sorted(
        {(r.period_us, c.asil) for c in architecture.components for r in c.runnables},
        key=lambda x: (x[0], x[1].value),
    )
    values = {
        core.name: sum((Fraction(preset.task_overhead_us, period) for period, _ in keys), Fraction())
        for core in preset.cores
    }
    for component in architecture.components:
        values[mapping[component.name]] += component_cpu_demand(component)
    return tuple((core.name, values[core.name]) for core in preset.cores)


def task_derived_load(wcet_us: int, period_us: int) -> Fraction:
    return Fraction(wcet_us, period_us)


def sender_receiver_rate(payload_bytes: int, provider_period_us: int) -> Fraction:
    return Fraction(payload_bytes * 1_000_000, provider_period_us)


def client_server_rate(request_bytes: int, response_bytes: int, caller_period_us: int) -> Fraction:
    return Fraction((request_bytes + response_bytes) * 1_000_000, caller_period_us)


def exchange_cost(architecture: Architecture, mapping: Mapping[str, str]) -> tuple[Fraction, Fraction]:
    sr = Fraction()
    cs = Fraction()
    for connector in architecture.connectors:
        if mapping[connector.provider] == mapping[connector.consumer]:
            continue
        if connector.kind == "sender_receiver":
            sr += sender_receiver_rate(connector.payload_bytes, connector.period_us)
        else:
            cs += client_server_rate(connector.request_bytes, connector.response_bytes, connector.period_us)
    return sr, cs


def memory_consumption(project: Project) -> tuple[MemoryMetric, ...]:
    totals = {memory.name: 0 for memory in project.memories}
    for component in project.components:
        for name, amount in component.memory:
            totals[name] += amount
    return tuple(
        MemoryMetric(
            m.name,
            m.size_bytes,
            int(m.size_bytes * m.threshold),
            totals[m.name],
            int(m.size_bytes * m.threshold) - totals[m.name],
            m.accessible,
        )
        for m in project.memories
    )


def layer_counts(project: Project) -> LayerCounts:
    """The project's element count per layer, in the ``swcraft list`` order."""
    base_types = sum(isinstance(item, BaseType) for item in project.data_types)
    return LayerCounts(
        requirements=len(project.requirements),
        compositions=len(project.compositions),
        swc=len(project.components),
        interfaces=len(project.interfaces),
        application_types=len(project.application_types),
        implementation_types=len(project.data_types) - base_types,
        base_types=base_types,
        units=len(project.units),
        compu_methods=len(project.compu_methods),
        data_constraints=len(project.data_constraints),
        mapping_sets=len(project.mapping_sets),
        connectors=len(project.connectors),
        tasks=len(project.tasks),
        sequences=len(project.sequences),
        hardware=len(hardware_names(project)),
    )


def _placement_metric(result: PlacementResult, label: str, preset: Preset) -> PlacementMetric:
    placement = result.planted if label == "planted" else result.emitted
    tasks = result.planted_tasks if label == "planted" else result.emitted_tasks
    loads = result.planted_core_loads if label == "planted" else result.emitted_core_loads
    cost = result.planted_cost if label == "planted" else result.emitted_cost
    by_core = {core.name: [0, 0] for core in result.cores}
    for task in tasks.tasks:
        by_core[task.core][0] += 1
        if task.runnables:
            by_core[task.core][1] += 1
    load_by_core = dict(loads)
    core_metrics = tuple(
        CoreMetric(
            core.name,
            by_core[core.name][0],
            by_core[core.name][1],
            load_by_core[core.name],
            Fraction(preset.core_threshold),
            Fraction(preset.core_threshold) - load_by_core[core.name],
        )
        for core in result.cores
    )
    displaced = result.displaced if label == "emitted" else ()
    return PlacementMetric(
        placement.mapping,
        core_metrics,
        cost.sender_receiver,
        cost.client_server,
        cost.total,
        displaced,
        result.pinned,
        preset.displaced_components,
        len(result.displaced),
    )


def build_metrics(
    architecture: Architecture, result: PlacementResult, project: Project, preset: Preset
) -> GenerationMetrics:
    components = tuple(
        ComponentCount(c.name, c.domain, c.architectural_role, c.asil, len(c.runnables))
        for c in architecture.components
    )
    domain_counts = tuple(
        DomainCount(
            d.name,
            len(d.components),
            sum(len(next(c for c in architecture.components if c.name == name).runnables) for name in d.components),
        )
        for d in architecture.domains
    )
    periods: dict[int, int] = {}
    wcets: list[tuple[str, str, int]] = []
    for component in architecture.components:
        for runnable in component.runnables:
            periods[runnable.period_us] = periods.get(runnable.period_us, 0) + 1
            wcets.append((component.name, runnable.name, runnable.wcet_us))
    domain_asil = tuple(AsilCount(asil, sum(d.asil is asil for d in architecture.domains)) for asil in ASIL)
    component_asil = tuple(AsilCount(asil, sum(c.asil is asil for c in architecture.components)) for asil in ASIL)
    indegree = {c.name: 0 for c in architecture.components}
    outdegree = dict(indegree)
    for link in architecture.connectors:
        outdegree[link.provider] += 1
        indegree[link.consumer] += 1
    graph = tuple(
        GraphDegree(c.name, indegree[c.name], outdegree[c.name], indegree[c.name] + outdegree[c.name])
        for c in architecture.components
    )
    planted_map = dict(result.planted.mapping)
    emitted_map = dict(result.emitted.mapping)
    sr: Fraction | int = 0
    cs: Fraction | int = 0
    intra_domain: Fraction | int = 0
    inter_domain: Fraction | int = 0
    fan_out = 0
    provider_counts: dict[str, int] = {}
    for link in architecture.connectors:
        provider_counts[link.provider] = provider_counts.get(link.provider, 0) + 1
        rate = (
            sender_receiver_rate(link.payload_bytes, link.period_us)
            if link.kind == "sender_receiver"
            else client_server_rate(link.request_bytes, link.response_bytes, link.period_us)
        )
        if link.kind == "sender_receiver":
            sr += rate
        else:
            cs += rate
        if (
            next(c for c in architecture.components if c.name == link.provider).domain
            == next(c for c in architecture.components if c.name == link.consumer).domain
        ):
            intra_domain += rate
        else:
            inter_domain += rate
    fan_out = sum(max(0, count - 1) for count in provider_counts.values())
    rates = {
        connector.name: (
            sender_receiver_rate(connector.payload_bytes, connector.period_us)
            if connector.kind == "sender_receiver"
            else client_server_rate(connector.request_bytes, connector.response_bytes, connector.period_us)
        )
        for connector in architecture.connectors
    }
    planted_intra = sum(
        (
            rates[connector.name]
            for connector in architecture.connectors
            if planted_map[connector.provider] == planted_map[connector.consumer]
        ),
        Fraction(),
    )
    emitted_intra = sum(
        (
            rates[connector.name]
            for connector in architecture.connectors
            if emitted_map[connector.provider] == emitted_map[connector.consumer]
        ),
        Fraction(),
    )
    comm = CommunicationSummary(
        int(sr + cs),
        int(sr),
        int(cs),
        int(intra_domain),
        int(inter_domain),
        int(planted_intra),
        int(sr + cs - planted_intra),
        int(emitted_intra),
        int(sr + cs - emitted_intra),
        len(architecture.connectors),
        fan_out,
    )

    def role(name: str) -> str | None:
        values = sorted(c.name for c in architecture.components if c.architectural_role == name)
        return values[0] if values else None

    high_in = sorted(graph, key=lambda g: (-g.in_degree, g.component))[0].component if graph else None
    high_out = sorted(graph, key=lambda g: (-g.out_degree, g.component))[0].component if graph else None
    landmarks = StableLandmarks(
        role("domain_gateway"),
        role("shared_service"),
        role("diagnostics"),
        role("safety_monitor"),
        high_in,
        high_out,
        next((i.name for i in architecture.interfaces if i.kind == "sender_receiver"), None),
        next((i.name for i in architecture.interfaces if i.kind == "client_server"), None),
        next(
            (
                r.id
                for r in architecture.requirements
                if "DOMAIN" in r.id or "POWERTRAIN" in r.id or "VEHICLE_STATE" in r.id
            ),
            None,
        ),
        next((r.id for r in architecture.requirements if "ARCHITECTURE_INTERACTION" in r.id), None),
    )
    return GenerationMetrics(
        layer_counts(project),
        domain_counts,
        components,
        tuple(PeriodSummary(p, periods[p]) for p in sorted(periods)),
        domain_asil,
        component_asil,
        tuple(wcets),
        comm,
        graph,
        memory_consumption(project),
        _placement_metric(result, "planted", preset),
        _placement_metric(result, "emitted", preset),
        landmarks,
    )
