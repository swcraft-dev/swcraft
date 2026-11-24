"""Exhaustive small-placement oracle.

It imports nothing from swcraft_specimens: its parameters are typed ``Any`` and read by duck typing.
"""

from __future__ import annotations

from fractions import Fraction
from itertools import combinations, product
from typing import Any

ASIL_ORDER = {"A": 1, "B": 2, "C": 3, "D": 4}


def _capable(capability: Any, required: Any) -> bool:
    cap = getattr(capability, "value", capability)
    req = getattr(required, "value", required)
    if req in ("absent", "QM"):
        return bool(cap != "absent")
    return bool(cap in ASIL_ORDER and ASIL_ORDER[cap] >= ASIL_ORDER[req])


def _mapping(value: Any) -> dict[str, str]:
    return dict(value.mapping)


def _demands(architecture: Any) -> dict[str, Fraction]:
    return {
        component.name: sum((Fraction(r.wcet_us, r.period_us) for r in component.runnables), Fraction())
        for component in architecture.components
    }


def _baseline(architecture: Any, preset: Any) -> dict[str, Fraction]:
    keys = {(r.period_us, c.asil) for c in architecture.components for r in c.runnables}
    return {
        core.name: sum((Fraction(preset.task_overhead_us, period) for period, _ in keys), Fraction())
        for core in preset.cores
    }


def _cost(architecture: Any, mapping: dict[str, str]) -> tuple[Fraction, Fraction]:
    sender_receiver = Fraction()
    client_server = Fraction()
    for link in architecture.connectors:
        if mapping[link.provider] == mapping[link.consumer]:
            continue
        if link.kind == "sender_receiver":
            sender_receiver += Fraction(link.payload_bytes * 1_000_000, link.period_us)
        else:
            client_server += Fraction((link.request_bytes + link.response_bytes) * 1_000_000, link.period_us)
    return sender_receiver, client_server


def _feasible(architecture: Any, placement: Any, preset: Any, budgets: Any, mapping: dict[str, str]) -> bool:
    components = {c.name: c for c in architecture.components}
    capabilities = {c.name: c.capability for c in preset.cores}
    for name, core in mapping.items():
        if not _capable(capabilities[core], components[name].asil):
            return False
    # A positive private budget is accessible only from its declared core, which pins the component.
    for budget in budgets:
        if budget.bytes and budget.memory != "RAM_SHARED":
            declared_core = budget.memory.removeprefix("RAM_")
            if mapping[budget.component] != declared_core:
                return False
    capacities = {"RAM_SHARED": int(preset.memories.shared.size_bytes * preset.memories.shared.threshold)}
    capacities.update(
        {
            f"RAM_{core.name}": int(
                preset.memories.private_per_core.size_bytes * preset.memories.private_per_core.threshold
            )
            for core in preset.cores
        }
    )
    for memory, capacity in capacities.items():
        if sum(b.bytes for b in budgets if b.memory == memory) > capacity:
            return False
    baseline = _baseline(architecture, preset)
    demands = _demands(architecture)
    for core in preset.cores:
        load = baseline[core.name] + sum(
            (demands[name] for name, assigned in mapping.items() if assigned == core.name),
            Fraction(),
        )
        if load > Fraction(preset.core_threshold):
            return False
    return True


def exhaustive(architecture: Any, placement: Any, preset: Any, moves: int) -> dict[str, Any]:
    """Return exact optimum and lexicographically smallest feasible witness."""
    emitted = _mapping(placement.emitted)
    names = tuple(c.name for c in architecture.components)
    cores = tuple(c.name for c in preset.cores)
    best: dict[str, Any] | None = None
    candidates = 0
    for count in range(moves + 1):
        for selected in combinations(names, count):
            for alternatives in product(*(tuple(c for c in cores if c != emitted[name]) for name in selected)):
                candidates += 1
                candidate = dict(emitted)
                candidate.update(zip(selected, alternatives))
                if not _feasible(architecture, placement, preset, placement.memory_budgets, candidate):
                    continue
                sr, cs = _cost(architecture, candidate)
                key = (sr + cs, sr, cs, tuple(candidate[name] for name in names))
                if best is None or key < best["key"]:
                    best = {
                        "key": key,
                        "mapping": candidate,
                        "sender_receiver": sr,
                        "client_server": cs,
                    }
    if best is None:
        raise AssertionError(f"no feasible oracle witness for N={moves}")
    moved = tuple(name for name in names if best["mapping"][name] != emitted[name])
    return {
        "N": moves,
        "optimum_total_cost": {
            "numerator": best["key"][0].numerator,
            "denominator": best["key"][0].denominator,
        },
        "sender_receiver_cost": {
            "numerator": best["sender_receiver"].numerator,
            "denominator": best["sender_receiver"].denominator,
        },
        "client_server_cost": {
            "numerator": best["client_server"].numerator,
            "denominator": best["client_server"].denominator,
        },
        "moves": len(moved),
        "moved_components": list(moved),
        "target_cores": [best["mapping"][name] for name in moved],
        "component_to_core": [{"component": name, "core": best["mapping"][name]} for name in names],
        "feasible": True,
        "enumerated_candidates": candidates,
    }
