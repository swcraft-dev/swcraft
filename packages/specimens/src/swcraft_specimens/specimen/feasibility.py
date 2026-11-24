"""The refusals before generation: the stated leaves checked against the stated inequalities.

Each failing leaf is refused with a closed class, every refusal collected, in leaf-table order:

- `version`: `passport_version` is not `1.x.y`, or `contract_version`'s major is not
  `passport.CONTRACT_MAJOR`; the refusal names both versions once they parse;
- `ceiling`: a count above `COUNT_CEILING`, stated or multiplied or added up from the stated counts;
  only the `version` and `ceiling` refusals are then reported, since the work grows with the counts;
- the component classes (`order`, `hub-rule`, `port-total`, `sum`), decided by `components.py`;
- the interface classes the stated leaves decide alone (`order`, `layer-total`), by `interfaces.py`;
- the fan-out and connector classes (`fan-out`, `sum`, `unconnected`, `connectors`, `links`), by the
  shape (`shape.py`), only when the port leaves and the fan-out order are not refused; a refusal the
  derivation caused has the class `derived`;
- `delegations` and `no-layout` at `connectors.delegation`, by the shape; with D > 0 and
  `layers.compositions` refused the shape is not decided;
- `layer-total` at `layers.compositions` for a count of 0 (SWCraft needs one root);
- the hardware classes (`hardware-topology`, `later-shape`), by `hardware.py`;
- the task classes (`sum`, `tasks`), by `tasks.py`;
- the requirement classes (`sum`, `requirements`, `derived`), by `requirements.py`;
- the identifier classes (`identifier-length`, `identifier-mean`), by `identifiers.py`; a layer whose
  count leaf is refused, and the connectors when their count is the construction's and the shape is
  refused, are not checked.

The check and the builder use the same modules, so they never disagree. The version leaves are
matched in full here (the schema's `pattern` accepts one trailing newline). No refusal echoes a
string value.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..passport import CONTRACT_MAJOR, MODEL_CONTRACT_VERSION, Refusal
from .common import CEILING_SENTENCE, COUNT_CEILING
from .components import component_refusals
from .compositions import composition_refusals
from .connectors import connector_spec, construction_spec
from .delegations import delegation_count
from .hardware import HARDWARE_COUNTS, hardware_ceiling_refusals, hardware_refusals
from .identifiers import identifier_counts, identifier_refusals
from .interfaces import interface_refusals
from .leaves import LEAVES, MET_TYPE_LAYERS, stated_leaves
from .requirements import requirement_ceiling_refusals, requirement_refusals
from .shape import Shape, derived_refusal, specimen_shape
from .tasks import task_ceiling_refusals, task_refusals
from .vocabulary import (
    ASSEMBLY,
    CEILING,
    CONTRACT_VERSION_LEAF,
    COUNT_DECIDED,
    FAN_OUT_MAX,
    FAN_OUT_TOP,
    HUBS,
    LAYER_BASE,
    LAYER_COMPOSITIONS,
    LAYER_CONNECTORS,
    LAYER_HARDWARE,
    LAYER_IMPLEMENTATION,
    LAYER_INTERFACES,
    LAYER_REQUIREMENTS,
    LAYER_SEQUENCES,
    LAYER_SWC,
    LAYER_TASKS,
    MET,
    PASSPORT_VERSION_LEAF,
    PORTS_TOP,
    PORTS_TOTAL,
    TASKS_IDLE,
    VERSION,
    layer_leaf,
)

_PASSPORT_VERSION = re.compile(r"1\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_CONTRACT_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def _version_refusals(stated: Mapping[str, Any]) -> list[Refusal]:
    refusals: list[Refusal] = []
    passport_version = stated.get(PASSPORT_VERSION_LEAF)
    if not (isinstance(passport_version, str) and _PASSPORT_VERSION.fullmatch(passport_version)):
        refusals.append(
            Refusal(
                PASSPORT_VERSION_LEAF,
                VERSION,
                "is not a passport version 1.x.y, the major the specimen generator reads",
            )
        )
    contract_version = stated.get(CONTRACT_VERSION_LEAF)
    match = _CONTRACT_VERSION.fullmatch(contract_version) if isinstance(contract_version, str) else None
    if match is None:
        refusals.append(Refusal(CONTRACT_VERSION_LEAF, VERSION, "is not a model contract version x.y.z"))
    elif match.group(1) != str(CONTRACT_MAJOR):
        # Compared as digits: a schema-valid major past Python's integer-conversion limit
        # would raise in int().
        refusals.append(
            Refusal(
                CONTRACT_VERSION_LEAF,
                VERSION,
                f"states model contract {contract_version} (major {match.group(1)}); "
                f"the specimen generator writes model contract {MODEL_CONTRACT_VERSION} "
                f"(major {CONTRACT_MAJOR})",
            )
        )
    return refusals


def _ceiling_sentence(value: Any) -> str | None:
    """Why a stated count leaf exceeds the ceiling, or `None`.

    A list leaf (the hubs, the port and the fan-out heads) counts its entries, each entry and the
    ports it places in all.
    """
    ceiling = CEILING_SENTENCE
    if not isinstance(value, list):
        return f"states {int(value)}; {ceiling}" if int(value) > COUNT_CEILING else None
    largest = max((int(item) for item in value), default=0)
    if largest > COUNT_CEILING:
        return f"states an entry of {largest}; {ceiling}"
    if len(value) > COUNT_CEILING:
        return f"states {len(value)} entries; {ceiling}"
    if sum(int(item) for item in value) > COUNT_CEILING:
        return f"places {sum(int(item) for item in value)} ports in all; {ceiling}"
    return None


def _ceiling_refusals(stated: Mapping[str, Any]) -> list[Refusal]:
    refusals: list[Refusal] = []
    for path, value in stated.items():
        if LEAVES[path].leaf_class != MET or path.startswith("identifiers."):
            continue
        sentence = _ceiling_sentence(value)
        if sentence is not None:
            refusals.append(Refusal(path, CEILING, sentence))
    if refusals:
        return refusals
    refusals.extend(hardware_ceiling_refusals(stated))
    refusals.extend(task_ceiling_refusals(stated))
    refusals.extend(requirement_ceiling_refusals(stated))
    connectors = connector_spec(stated)
    count = connectors.connectors
    if PORTS_TOTAL not in stated and count:
        least = connectors.unconnected + count + 1
        if least > COUNT_CEILING:
            refusals.append(
                Refusal(
                    connectors.connector_leaf,
                    CEILING,
                    f"states {count}; with {connectors.unconnected} unconnected ports the "
                    f"components hold at least {least} ports, and {CEILING_SENTENCE}",
                )
            )
    return refusals


#: The leaves a layer's element count comes from, by identifier layer.
_COUNT_LEAVES: dict[str, frozenset[str]] = {
    "swc": frozenset({LAYER_SWC}),
    "interfaces": frozenset({LAYER_INTERFACES}),
    "connectors": frozenset({LAYER_CONNECTORS, ASSEMBLY}),
    "implementation_types": frozenset({LAYER_IMPLEMENTATION}),
    "base_types": frozenset({LAYER_BASE}),
    "hardware": frozenset({LAYER_HARDWARE, *HARDWARE_COUNTS}),
    "compositions": frozenset({LAYER_COMPOSITIONS}),
    # A sequence count is refused only through B.
    "tasks": frozenset({LAYER_TASKS, TASKS_IDLE}),
    "sequences": frozenset({LAYER_SEQUENCES, LAYER_TASKS, TASKS_IDLE}),
    "requirements": frozenset({LAYER_REQUIREMENTS}),
    **{layer: frozenset({layer_leaf(layer)}) for layer in MET_TYPE_LAYERS},
}


def check_feasibility(passport: Mapping[str, Any]) -> list[Refusal]:
    """Every refusal before generation of a schema-valid passport, in leaf-table order."""
    stated = stated_leaves(passport)
    by_leaf: dict[str, Refusal] = {}
    for refusal in _version_refusals(stated):
        by_leaf[refusal.leaf] = refusal
    ceiling = _ceiling_refusals(stated)
    if ceiling:
        for refusal in ceiling:
            by_leaf.setdefault(refusal.leaf, refusal)
        return [by_leaf[path] for path in LEAVES if path in by_leaf]
    components = component_refusals(stated)
    for refusal in components:
        by_leaf.setdefault(refusal.leaf, refusal)
    compositions = composition_refusals(stated)
    for refusal in compositions:
        by_leaf.setdefault(refusal.leaf, refusal)
    interfaces = interface_refusals(stated)
    for refusal in interfaces:
        by_leaf.setdefault(refusal.leaf, refusal)
    connectors = connector_spec(stated).connectors
    refused = {refusal.leaf for refusal in [*components, *compositions, *interfaces]}
    shape: Shape | list[Refusal] | None = None
    # The shape reads C only for the delegations: with D > 0 and C refused it is not decided
    # against a count the passport cannot have.
    depends = {PORTS_TOTAL, PORTS_TOP, HUBS, FAN_OUT_MAX, FAN_OUT_TOP}
    if delegation_count(stated):
        depends.add(LAYER_COMPOSITIONS)
    if not depends & refused:
        shape = specimen_shape(stated)
        if isinstance(shape, Shape):
            connectors = shape.connectors
        else:
            for refusal in shape:
                by_leaf.setdefault(refusal.leaf, refusal)
    for refusal in hardware_refusals(stated):
        by_leaf.setdefault(refusal.leaf, refusal)
    for refusal in task_refusals(stated):
        by_leaf.setdefault(refusal.leaf, refusal)
    ports: int | None = int(stated[PORTS_TOTAL]) if PORTS_TOTAL in stated else None
    if ports is None and isinstance(shape, Shape):
        ports = sum(shape.counts)
    for refusal in requirement_refusals(stated, ports):
        by_leaf.setdefault(refusal.leaf, refusal)
    unchecked = frozenset(layer for layer, leaves in _COUNT_LEAVES.items() if leaves & set(by_leaf))
    if connectors is None:
        unchecked |= {"connectors"}
    counts = identifier_counts(stated, 0 if connectors is None else connectors)
    names = construction_spec(stated).names if connectors is not None else None
    unmet = shape if isinstance(shape, Shape) and shape.names_unmet else None
    for refusal in identifier_refusals(stated, counts, unchecked, connectors_derived=names is not None):
        if unmet is not None:
            if unmet.derived and refusal.leaf in COUNT_DECIDED:
                # No count the search fits at the totals tried meets the leaf, and the total
                # was derived: other totals were not tried.
                refusal = derived_refusal(refusal, unmet.derived)
        elif names is not None and names.avoidable(refusal):
            # The backstop: a refusal some other count the stated leaves admit avoids is the
            # construction's choice of A, not a stated one.
            refusal = derived_refusal(refusal, [f"{ASSEMBLY} derived as {counts.connectors}"])
        by_leaf.setdefault(refusal.leaf, refusal)
    return [by_leaf[path] for path in LEAVES if path in by_leaf]
