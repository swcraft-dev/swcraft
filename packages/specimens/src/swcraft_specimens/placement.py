"""Placement, resource accounting, and Architecture -> Project conversion."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256

from .architecture import (
    Architecture,
    ArchitectureComponent,
)
from .deterministic import SplitMix64, shuffle
from .errors import GenerationError
from .model import (
    Argument,
    Component,
    Composition,
    CompositionMember,
    CompositionPort,
    Connector,
    ConnectorEndpoint,
    Core,
    DataElement,
    Delegation,
    Domain,
    Interface,
    Memory,
    Operation,
    Port,
    Processor,
    Project,
    Requirement,
    Runnable,
    Task,
    component_links,
)
from .preset import ASIL, Preset

ASIL_ORDER = {ASIL.QM: 0, ASIL.A: 1, ASIL.B: 2, ASIL.C: 3, ASIL.D: 4, ASIL.ABSENT: -1}
SEARCH_LIMIT = 250_000


@dataclass(frozen=True)
class TaskKey:
    core: str
    period_us: int
    effective_asil: ASIL


@dataclass(frozen=True)
class Placement:
    mapping: tuple[tuple[str, str], ...]

    def core_for(self, component: str) -> str:
        try:
            return dict(self.mapping)[component]
        except KeyError:
            raise GenerationError(f"component {component} is absent from placement") from None


@dataclass(frozen=True)
class ComponentDemand:
    component: str
    demand: Fraction


@dataclass(frozen=True)
class MemoryBudget:
    component: str
    memory: str
    bytes: int


@dataclass(frozen=True)
class ExchangeCost:
    sender_receiver: Fraction
    client_server: Fraction

    @property
    def total(self) -> Fraction:
        return self.sender_receiver + self.client_server


@dataclass(frozen=True)
class PlacementTasks:
    tasks: tuple[Task, ...]


@dataclass(frozen=True)
class PlacementResult:
    architecture: Architecture
    processor: Processor
    cores: tuple[Core, ...]
    memories: tuple[Memory, ...]
    planted: Placement
    emitted: Placement
    displaced: tuple[str, ...]
    pinned: tuple[str, ...]
    memory_budgets: tuple[MemoryBudget, ...]
    planted_tasks: PlacementTasks
    emitted_tasks: PlacementTasks
    planted_core_loads: tuple[tuple[str, Fraction], ...]
    emitted_core_loads: tuple[tuple[str, Fraction], ...]
    planted_cost: ExchangeCost
    emitted_cost: ExchangeCost


def component_demand(component: ArchitectureComponent) -> Fraction:
    return sum((Fraction(r.wcet_us, r.period_us) for r in component.runnables), Fraction(0))


def _keys(architecture: Architecture) -> tuple[tuple[int, ASIL], ...]:
    return tuple(
        sorted(
            {(r.period_us, c.asil) for c in architecture.components for r in c.runnables},
            key=lambda x: (x[0], ASIL_ORDER[x[1]]),
        )
    )


def _baseline(preset: Preset, keys: Iterable[tuple[int, ASIL]]) -> Fraction:
    return sum((Fraction(preset.task_overhead_us, period) for period, _ in keys), Fraction(0))


def _loads(architecture: Architecture, placement: Placement, preset: Preset) -> tuple[tuple[str, Fraction], ...]:
    keys = _keys(architecture)
    values = {core.name: _baseline(preset, keys) for core in preset.cores}
    for component in architecture.components:
        values[placement.core_for(component.name)] += component_demand(component)
    return tuple((core.name, values[core.name]) for core in preset.cores)


def _capable(capability: ASIL, required: ASIL) -> bool:
    if required is ASIL.ABSENT:
        return True
    if required is ASIL.QM:
        # QM is not an ordinal below A.  It has no rated requirement, but it
        # still needs a declared core capability; absent capability is not a
        # substitute for a declared QM/rated core.
        return capability is not ASIL.ABSENT
    return capability not in (ASIL.QM, ASIL.ABSENT) and ASIL_ORDER[capability] >= ASIL_ORDER[required]


def _validate_placement(architecture: Architecture, placement: Placement, preset: Preset, label: str) -> None:
    names = tuple(c.name for c in architecture.components)
    if tuple(name for name, _ in placement.mapping) != names or len(set(name for name, _ in placement.mapping)) != len(
        names
    ):
        raise GenerationError(f"{label} placement: component mapping must contain every component exactly once")
    core_names = {c.name for c in preset.cores}
    by_name = {c.name: c for c in architecture.components}
    capabilities = {c.name: c.capability for c in preset.cores}
    for name, core in placement.mapping:
        if core not in core_names:
            raise GenerationError(f"{label} placement: component {name} references unknown core {core}")
        if not _capable(capabilities[core], by_name[name].asil):
            raise GenerationError(
                f"{label} placement: component {name} ASIL {by_name[name].asil.value} exceeds capability of {core}"
            )
    for core, load in _loads(architecture, placement, preset):
        if load > Fraction(preset.core_threshold):
            raise GenerationError(f"{label} CPU threshold exceeded on core {core}: {load} > {preset.core_threshold}")


def _memory_formula(component: ArchitectureComponent) -> int:
    # Fixed base plus observable structure; integer and independent of placement/search order.
    accesses = sum(len(r.reads) + len(r.writes) + len(r.calls) for r in component.runnables)
    return 64 + 32 * len(component.runnables) + 16 * len(component.ports) + 8 * accesses


def _select_memory(
    architecture: Architecture, preset: Preset, planted: Placement, rng: SplitMix64
) -> tuple[tuple[MemoryBudget, ...], tuple[str, ...]]:
    shared_name = "RAM_SHARED"
    private_names = {core.name: f"RAM_{core.name}" for core in preset.cores}
    components = {c.name: c for c in architecture.components}
    budgets: list[MemoryBudget] = []
    for component in architecture.components:
        amount = _memory_formula(component)
        budgets.append(MemoryBudget(component.name, shared_name, amount))
        for core in preset.cores:
            budgets.append(MemoryBudget(component.name, private_names[core.name], 0))
    required = int(Fraction(preset.private_memory_share) * preset.components + Fraction(1, 2))
    order = shuffle(rng, [c.name for c in architecture.components])
    capacity = {
        core.name: int(preset.memories.private_per_core.size_bytes * preset.memories.private_per_core.threshold)
        for core in preset.cores
    }
    selected: list[str] = []
    for name in order:
        if len(selected) == required:
            break
        core_name = planted.core_for(name)
        amount = _memory_formula(components[name])
        if amount <= capacity[core_name]:
            capacity[core_name] -= amount
            selected.append(name)
            for i, budget in enumerate(budgets):
                if budget.component == name and budget.memory == private_names[core_name]:
                    budgets[i] = MemoryBudget(name, budget.memory, amount)
    if len(selected) != required:
        raise GenerationError(f"private memory selection impossible: requested {required}, selected {len(selected)}")
    shared_total = sum(b.bytes for b in budgets if b.memory == shared_name)
    shared_capacity = int(preset.memories.shared.size_bytes * preset.memories.shared.threshold)
    if shared_total > shared_capacity:
        raise GenerationError(f"shared memory too small: {shared_total} > {shared_capacity}")
    return tuple(budgets), tuple(sorted(selected, key=order.index))


def _build_tasks(architecture: Architecture, placement: Placement, preset: Preset) -> PlacementTasks:
    keys = _keys(architecture)
    grouped: dict[tuple[str, int, ASIL], list[tuple[str, str]]] = {
        (core.name, period, asil): [] for core in preset.cores for period, asil in keys
    }
    for component in architecture.components:
        core_name = placement.core_for(component.name)
        for runnable in component.runnables:
            grouped[(core_name, runnable.period_us, component.asil)].append((component.name, runnable.name))
    sorted_keys = sorted(keys, key=lambda x: (x[0], ASIL_ORDER[x[1]]))
    tasks: list[Task] = []
    for core in preset.cores:
        for priority, (period, asil) in enumerate(sorted_keys[::-1], 1):
            refs = tuple(grouped[(core.name, period, asil)])
            wcet = preset.task_overhead_us + sum(
                next(r.wcet_us for c in architecture.components if c.name == swc for r in c.runnables if r.name == run)
                for swc, run in refs
            )
            tasks.append(
                Task(
                    f"Task_{core.name}_{period}us_{asil.value}",
                    period,
                    priority,
                    core.name,
                    wcet,
                    refs,
                    asil,
                )
            )
    return PlacementTasks(tuple(tasks))


def _cost(architecture: Architecture, placement: Placement) -> ExchangeCost:
    by_core = dict(placement.mapping)
    sr = Fraction(0)
    cs = Fraction(0)
    for link in architecture.connectors:
        if by_core[link.provider] == by_core[link.consumer]:
            continue
        if link.kind == "sender_receiver":
            sr += Fraction(link.payload_bytes * 1_000_000, link.period_us)
        else:
            # ArchitectureConnector.period_us is the caller period for client-server.
            cs += Fraction((link.request_bytes + link.response_bytes) * 1_000_000, link.period_us)
    return ExchangeCost(sr, cs)


def _displace(
    architecture: Architecture,
    planted: Placement,
    pinned: tuple[str, ...],
    preset: Preset,
    rng: SplitMix64,
) -> tuple[Placement, tuple[str, ...]]:
    if preset.displaced_components == 0:
        return planted, ()
    names = [c.name for c in architecture.components if c.name not in pinned]
    candidate_order = shuffle(rng, names)
    core_order = [c.name for c in preset.cores]
    by_name = {c.name: c for c in architecture.components}
    capabilities = {c.name: c.capability for c in preset.cores}
    best: tuple[Fraction, Placement, tuple[str, ...]] | None = None
    nodes = 0

    def visit(index: int, mapping: dict[str, str], moved: list[str]) -> None:
        nonlocal nodes, best
        if best is not None:
            return
        nodes += 1
        if nodes > SEARCH_LIMIT:
            return
        if len(moved) == preset.displaced_components:
            candidate = Placement(tuple((name, mapping[name]) for name in (c.name for c in architecture.components)))
            cost = _cost(architecture, candidate).total
            if cost > _cost(architecture, planted).total and (best is None or cost > best[0]):
                best = (cost, candidate, tuple(moved))
            return
        if index >= len(candidate_order):
            return
        remaining = len(candidate_order) - index
        if len(moved) + remaining < preset.displaced_components:
            return
        # Include/exclude branching permits combinations with zero or negative intermediate deltas.
        name = candidate_order[index]
        original = mapping[name]
        for core in core_order:
            if core == original or not _capable(capabilities[core], by_name[name].asil):
                continue
            mapping[name] = core
            try:
                _validate_placement(
                    architecture,
                    Placement(tuple((n, mapping[n]) for n in (c.name for c in architecture.components))),
                    preset,
                    "emitted",
                )
            except GenerationError:
                mapping[name] = original
                continue
            visit(index + 1, mapping, moved + [name])
            mapping[name] = original
            if best is not None and nodes > SEARCH_LIMIT:
                return
        visit(index + 1, mapping, moved)

    visit(0, dict(planted.mapping), [])
    if best is None:
        raise GenerationError(
            "displacement failed: exchange cost cannot increase for requested "
            f"M={preset.displaced_components}; "
            f"eligible unpinned={len(names)}, search limit={SEARCH_LIMIT}"
        )
    ordered = tuple(name for name, core in best[1].mapping if core != planted.core_for(name))
    return best[1], ordered


def build_placement(architecture: Architecture, preset: Preset, rng: SplitMix64 | None = None) -> PlacementResult:
    if rng is None:
        rng = SplitMix64(0)
        rng.state = int(dict(architecture.metadata).get("prng_state", 0))
    demands = {
        d.name: sum(
            (component_demand(next(c for c in architecture.components if c.name == name)) for name in d.components),
            Fraction(0),
        )
        for d in architecture.domains
    }
    keys = _keys(architecture)
    base = _baseline(preset, keys)
    domain_order = sorted(architecture.domains, key=lambda d: d.name)
    core_names = [c.name for c in preset.cores]
    best: tuple[tuple[Fraction, Fraction | int, tuple[str, ...]], Placement] | None = None
    mapping: dict[str, str] = {}
    nodes = 0

    if base > Fraction(preset.core_threshold):
        raise GenerationError(f"baseline CPU overhead exceeds core threshold: {base} > {preset.core_threshold}")
    for domain in domain_order:
        if not any(_capable(core.capability, domain.asil) for core in preset.cores):
            raise GenerationError(f"domain {domain.name} ASIL {domain.asil.value} has no capable core")

    def pack(index: int) -> None:
        nonlocal best, nodes
        nodes += 1
        if nodes > SEARCH_LIMIT:
            return
        if index == len(domain_order):
            loads = {core: base for core in core_names}
            for domain_name, core_name in mapping.items():
                loads[core_name] += demands[domain_name]
            if any(load > Fraction(preset.core_threshold) for load in loads.values()):
                return
            deviations = [abs(load - Fraction(preset.target_core_load)) for load in loads.values()]
            score = (
                max(deviations),
                sum(deviations),
                tuple(mapping[d.name] for d in architecture.domains),
            )
            candidate = Placement(tuple((c.name, mapping[c.domain]) for c in architecture.components))
            if best is None or score < best[0]:
                best = (score, candidate)
            return
        domain = domain_order[index]
        for core in core_names:
            if not _capable(next(c.capability for c in preset.cores if c.name == core), domain.asil):
                continue
            if demands[domain.name] + base > Fraction(preset.core_threshold):
                raise GenerationError(
                    f"domain {domain.name} demand {demands[domain.name]} plus baseline {base} "
                    f"exceeds core threshold {preset.core_threshold}"
                )
            mapping[domain.name] = core
            pack(index + 1)
            mapping.pop(domain.name)

    pack(0)
    if best is None:
        first = domain_order[0].name if domain_order else "?"
        raise GenerationError(
            f"planted packing failed: domain {first} has no feasible capability/load assignment "
            f"within search limit {SEARCH_LIMIT} (visited {nodes} nodes)"
        )
    planted = best[1]
    _validate_placement(architecture, planted, preset, "planted")
    budgets, pinned = _select_memory(architecture, preset, planted, rng)
    emitted, displaced = _displace(architecture, planted, pinned, preset, rng)
    planted_tasks = _build_tasks(architecture, planted, preset)
    emitted_tasks = _build_tasks(architecture, emitted, preset)
    result = PlacementResult(
        architecture,
        Processor("CPU"),
        tuple(Core(c.name, preset.core_threshold, c.capability) for c in preset.cores),
        tuple(
            [
                Memory(
                    "RAM_SHARED",
                    preset.memories.shared.size_bytes,
                    preset.memories.shared.threshold,
                    tuple(c.name for c in preset.cores),
                )
            ]
            + [
                Memory(
                    f"RAM_{c.name}",
                    preset.memories.private_per_core.size_bytes,
                    preset.memories.private_per_core.threshold,
                    (c.name,),
                )
                for c in preset.cores
            ]
        ),
        planted,
        emitted,
        displaced,
        pinned,
        budgets,
        planted_tasks,
        emitted_tasks,
        _loads(architecture, planted, preset),
        _loads(architecture, emitted, preset),
        _cost(architecture, planted),
        _cost(architecture, emitted),
    )
    validate_placement(result, preset)
    return result


def validate_placement(result: PlacementResult, preset: Preset) -> None:
    architecture = result.architecture
    _validate_placement(architecture, result.planted, preset, "planted")
    _validate_placement(architecture, result.emitted, preset, "emitted")
    diff = tuple(name for name, core in result.emitted.mapping if core != result.planted.core_for(name))
    if any(name in result.pinned for name in diff):
        moved = next(name for name in diff if name in result.pinned)
        raise GenerationError(f"pinned component {moved} was moved")
    if diff != result.displaced or len(diff) != preset.displaced_components:
        raise GenerationError(f"displaced mapping mismatch: expected {preset.displaced_components}, found {len(diff)}")
    if preset.displaced_components and result.emitted_cost.total <= result.planted_cost.total:
        raise GenerationError(
            f"emitted exchange cost constraint violated: planted={result.planted_cost.total}, "
            f"emitted={result.emitted_cost.total}"
        )
    if result.planted_cost != _cost(architecture, result.planted):
        raise GenerationError("planted exchange cost mismatch")
    if result.emitted_cost != _cost(architecture, result.emitted):
        raise GenerationError("emitted exchange cost mismatch")
    by_budget = {(b.component, b.memory): b.bytes for b in result.memory_budgets}
    expected_memories = {m.name for m in result.memories}
    for c in architecture.components:
        if {memory for component, memory in by_budget if component == c.name} != expected_memories:
            raise GenerationError(f"component {c.name} lacks a memory budget for every declared memory")
        if by_budget.get((c.name, "RAM_SHARED"), 0) <= 0:
            raise GenerationError(f"component {c.name} shared memory budget must be positive")
    shared_total = sum(b for (c, m), b in by_budget.items() if m == "RAM_SHARED")
    shared_limit = int(preset.memories.shared.size_bytes * preset.memories.shared.threshold)
    if shared_total > shared_limit:
        raise GenerationError(f"shared memory threshold exceeded: {shared_total} > {shared_limit}")
    for core in preset.cores:
        total = sum(b for (c, m), b in by_budget.items() if m == f"RAM_{core.name}")
        limit = int(preset.memories.private_per_core.size_bytes * preset.memories.private_per_core.threshold)
        if total > limit:
            raise GenerationError(f"private memory threshold exceeded on {core.name}: {total} > {limit}")
    memory_access = {memory.name: set(memory.accessible) for memory in result.memories}
    for (component, memory), amount in by_budget.items():
        if amount and result.planted.core_for(component) not in memory_access.get(memory, set()):
            raise GenerationError(
                f"private budget {component}.{memory} is inaccessible from planted core "
                f"{result.planted.core_for(component)}"
            )
    positive_private = tuple(sorted(c for c, m in by_budget if m != "RAM_SHARED" and by_budget[(c, m)] > 0))
    if positive_private != tuple(sorted(result.pinned)):
        raise GenerationError("pinned set does not match positive private memory budgets")
    for tasks, placement, label in (
        (result.planted_tasks, result.planted, "planted"),
        (result.emitted_tasks, result.emitted, "emitted"),
    ):
        refs = [ref for task in tasks.tasks for ref in task.runnables]
        expected = [(c.name, r.name) for c in architecture.components for r in c.runnables]
        if sorted(refs) != sorted(expected) or len(refs) != len(set(refs)):
            raise GenerationError(f"{label} task topology does not assign every runnable exactly once")
        expected_keys = {(core.name, period, asil) for core in preset.cores for period, asil in _keys(architecture)}
        actual_keys = {(task.core, task.period_us, task.asil) for task in tasks.tasks}
        if actual_keys != expected_keys or len(tasks.tasks) != len(expected_keys):
            raise GenerationError(f"{label} task topology is missing or duplicating a core/period/ASIL key")
        for task in tasks.tasks:
            expected_name = f"Task_{task.core}_{task.period_us}us_{task.asil.value}"
            if task.name != expected_name:
                raise GenerationError(
                    f"{label} task {task.name} has wrong core/period/ASIL identity; expected {expected_name}"
                )
            if any(placement.core_for(c) != task.core for c, _ in task.runnables):
                raise GenerationError(f"{label} task {task.name} assigns a runnable to the wrong core")
            expected_wcet = preset.task_overhead_us + sum(
                next(r.wcet_us for c in architecture.components if c.name == swc for r in c.runnables if r.name == run)
                for swc, run in task.runnables
            )
            if task.wcet_us != expected_wcet:
                raise GenerationError(f"{label} task {task.name} WCET mismatch: {task.wcet_us} != {expected_wcet}")
    expected_planted_loads = _loads(architecture, result.planted, preset)
    expected_emitted_loads = _loads(architecture, result.emitted, preset)
    if result.planted_core_loads != expected_planted_loads:
        raise GenerationError("planted CPU load mismatch")
    if result.emitted_core_loads != expected_emitted_loads:
        raise GenerationError("emitted CPU load mismatch")


def placement_fingerprint(result: PlacementResult) -> str:
    def p(x: Placement) -> str:
        return ",".join(f"{a}>{b}" for a, b in x.mapping)

    parts = [
        p(result.planted),
        p(result.emitted),
        ",".join(result.displaced),
        ",".join(result.pinned),
    ]
    parts += [f"{core}:{load}" for core, load in result.planted_core_loads + result.emitted_core_loads]
    parts += [f"{b.component}:{b.memory}:{b.bytes}" for b in result.memory_budgets]
    parts += [
        f"cost:{result.planted_cost.sender_receiver}:{result.planted_cost.client_server}:{result.emitted_cost.sender_receiver}:{result.emitted_cost.client_server}"
    ]
    for group in (result.planted_tasks.tasks, result.emitted_tasks.tasks):
        parts += [f"{t.name}:{t.wcet_us}:{t.runnables}" for t in group]
    return sha256("\n".join(parts).encode()).hexdigest()


def architecture_to_project(result: PlacementResult, preset: Preset) -> Project:
    a = result.architecture
    budgets = {(b.component, b.memory): b.bytes for b in result.memory_budgets}
    components = []
    for c in a.components:
        runs = tuple(Runnable(r.name, r.period_us, r.writes, r.reads, r.calls, r.wcet_us) for r in c.runnables)
        domain_token = next(d.token for d in a.domains if d.name == c.domain)
        components.append(
            Component(
                c.name,
                domain_token,
                c.asil,
                tuple(Port(p.name, p.direction, p.interface) for p in c.ports),
                runs,
                tuple((m.name, budgets[(c.name, m.name)]) for m in result.memories),
                c.description,
            )
        )
    interfaces = []
    for i in a.interfaces:
        if i.kind == "sender_receiver":
            interfaces.append(Interface(i.name, i.kind, tuple(DataElement(e.name, e.type) for e in i.data_elements)))
        else:
            interfaces.append(
                Interface(
                    i.name,
                    i.kind,
                    operations=(
                        Operation(
                            i.operation,
                            tuple(Argument(x.name, x.direction, x.type) for x in i.arguments),
                        ),
                    ),
                )
            )
    inter = [
        connector
        for connector in a.connectors
        if next(c for c in a.components if c.name == connector.provider).domain
        != next(c for c in a.components if c.name == connector.consumer).domain
    ]
    boundary: dict[str, list[tuple[CompositionPort, Delegation]]] = {d.name: [] for d in a.domains}
    boundary_lookup: dict[tuple[str, str, str], str] = {}
    for link in inter:
        provider_domain = next(c for c in a.components if c.name == link.provider).domain
        consumer_domain = next(c for c in a.components if c.name == link.consumer).domain
        for domain_name, component_name, port_name, direction in (
            (provider_domain, link.provider, link.provider_port, "provide"),
            (consumer_domain, link.consumer, link.consumer_port, "require"),
        ):
            if (domain_name, component_name, port_name) in boundary_lookup:
                continue
            boundary_port = f"{port_name}Boundary"
            if any(existing.name == boundary_port for existing, _ in boundary[domain_name]):
                boundary_port = f"{boundary_port}_{len(boundary[domain_name]) + 1:02d}"
            member_name = f"{component_name}Member"
            interface = next(
                p.interface for c in a.components if c.name == component_name for p in c.ports if p.name == port_name
            )
            cp = CompositionPort(
                boundary_port,
                direction,
                interface,
                f"Synthetic {direction} boundary for {link.name}.",
            )
            dg = Delegation(
                f"Delegate_{link.name}_{direction.title()}",
                boundary_port,
                member_name,
                port_name,
                f"Synthetic delegation for {link.name}.",
            )
            if not any(existing.name == cp.name for existing, _ in boundary[domain_name]):
                boundary[domain_name].append((cp, dg))
            boundary_lookup[(domain_name, component_name, port_name)] = boundary_port
    connector_rows = []
    for connector in a.connectors:
        provider_domain = next(c for c in a.components if c.name == connector.provider).domain
        consumer_domain = next(c for c in a.components if c.name == connector.consumer).domain
        if provider_domain == consumer_domain:
            connector_rows.append(
                Connector(
                    connector.name,
                    connector.kind,
                    ConnectorEndpoint(connector.provider, connector.provider_port),
                    ConnectorEndpoint(connector.consumer, connector.consumer_port),
                    provider_domain,
                    connector.description if hasattr(connector, "description") else "",
                )
            )
        else:
            provider_boundary = boundary_lookup[(provider_domain, connector.provider, connector.provider_port)]
            consumer_boundary = boundary_lookup[(consumer_domain, connector.consumer, connector.consumer_port)]
            connector_rows.append(
                Connector(
                    connector.name,
                    connector.kind,
                    ConnectorEndpoint(None, provider_boundary, provider_domain),
                    ConnectorEndpoint(None, consumer_boundary, consumer_domain),
                    None,
                    connector.description if hasattr(connector, "description") else "",
                )
            )
    connectors = tuple(connector_rows)
    reqs = tuple(
        Requirement(r.id, r.title, r.description, r.asil, component_links(r.components)) for r in a.requirements
    )
    domains = tuple(Domain(d.name, d.components, d.token, d.title, d.token, d.description) for d in a.domains)
    data_types = a.data_types
    compositions = []
    for d in a.domains:
        pairs = boundary[d.name]
        compositions.append(
            Composition(
                d.name,
                d.title,
                False,
                tuple(
                    CompositionMember(f"{n}Member", n, None, f"Synthetic member of {d.title}.") for n in d.components
                ),
                d.description,
                tuple(cp for cp, _ in pairs),
                tuple(dg for _, dg in pairs),
            )
        )
    root_members = tuple(
        CompositionMember(f"{d.name}Member", None, d.name, f"Synthetic {d.title} domain instance.") for d in a.domains
    )
    compositions.append(
        Composition(
            a.root.name,
            "Synthetic vehicle architecture",
            True,
            root_members,
            "Synthetic root composition for scalable specimen generator demonstrations.",
        )
    )
    comps = tuple(compositions)
    return Project(
        a.name,
        result.processor,
        result.cores,
        result.memories,
        domains,
        tuple(components),
        data_types,
        tuple(interfaces),
        connectors,
        reqs,
        result.emitted_tasks.tasks,
        comps,
        preset.emit_runnable_wcet,
    )
