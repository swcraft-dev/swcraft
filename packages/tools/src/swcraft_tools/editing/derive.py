"""A planned model state -> the change set that reaches it.

The module takes planned **model elements** and knows nothing about ARXML. A
planned element the baseline lacks becomes one element-level ``create``; one it
has is diffed and projected through
:func:`~swcraft_tools.editing.delta.project_delta`, each projected leaf becoming
the operation of the same name at the same address.

The derivation is replayed in memory through
:func:`~swcraft_tools.editing.apply.apply_operations` and must reproduce every
planned element's canonical payload exactly; anything else is a
:class:`DerivationFault`, raised, never returned.

An empty derivation is ``None``: ``changeSetRequest`` declares ``minItems: 1`` on
``operations``. The baseline token is copied from the caller, never computed; the
``change_set`` call refuses a stale one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from swcraft_tools.differ import diff_snapshots
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.address import canonical_value
from swcraft_tools.editing.apply import apply_operations
from swcraft_tools.editing.delta import project_delta
from swcraft_tools.editing.protocol import ABSENT, DeltaEntry, parse_change_operation
from swcraft_tools.editing.read import publish_element
from swcraft_tools.model_state import ModelState
from swcraft_tools.snapshot import ModelSnapshot

__all__ = ["DerivationFault", "PlannedElement", "derive_change_set"]

#: ``(layer, element, placement)``; ``placement`` is ``{}`` for layers without one.
#: A plain tuple so a producer outside this package need not import ``editing/``.
PlannedElement = tuple[str, BaseModel, Mapping[str, str]]


class DerivationFault(RuntimeError):
    """The planned state cannot be expressed as a change set that reaches it.

    Always an internal defect of the planner or of this module, never something a
    client's request can provoke.
    """


def derive_change_set(
    baseline: ModelSnapshot,
    planned: Sequence[PlannedElement],
    *,
    baseline_token: str,
    intent: str | None = None,
    registry: reg.Registry | None = None,
) -> dict[str, Any] | None:
    """The ``change_set`` request that turns *baseline* into *baseline* + *planned*.

    Args:
        baseline: the model as it is; never mutated.
        planned: the elements the model should hold. One the baseline holds under
            the same identity is replaced; others are added. Elements absent from
            *planned* are left alone (an upsert, not a synchronization).
        baseline_token: the ``discover`` baseline token, copied unchanged.
        intent: one sentence saying why, for the reviewer.

    Returns:
        A complete protocol ``change_set`` request, or ``None`` when *planned*
        changes nothing.

    Raises:
        DerivationFault: an undeclared layer, or a replay that does not
            reproduce the planned payloads.
    """
    registry = reg.or_current(registry)
    # `diff_snapshots` resolves `current_registry()` itself; binding it keeps the
    # diff and the delta projection on the same layer set.
    with reg.kernel_context(registry):
        staged = _planned_snapshot(baseline, planned, registry)
        entries = project_delta(
            diff_snapshots(baseline, staged),
            baseline=baseline,
            staged=staged,
            registry=registry,
        )
        if not entries:
            return None

        operations = [_operation(entry) for entry in entries]
        _require_replay(baseline, operations, planned, registry)

        request: dict[str, Any] = {"operation": "change_set", "baseline": baseline_token}
        if intent is not None:
            request["intent"] = intent
        request["operations"] = operations
        return request


def _planned_snapshot(
    baseline: ModelSnapshot,
    planned: Sequence[PlannedElement],
    registry: reg.Registry,
) -> ModelSnapshot:
    """*baseline* with every planned element put in its layer, in place or appended.

    A replacement stays in place because no address can express a reordering of
    the layer. The snapshot is built from a composed state rather than by
    ``dataclasses.replace``, which rebuilds the adapter from its declared fields
    only and would drop every layer without an adapter field. The baseline is
    copied, never mutated; ``absent`` is carried over unchanged.
    """
    state = baseline.state
    layers: dict[str, list[Any]] = {name: list(values) for name, values in state.layers.items()}
    placements: dict[str, dict[str, dict[str, str]]] = {
        name: {key: dict(value) for key, value in table.items()} for name, table in state.placement.items()
    }
    for layer_name, element, placement in planned:
        layer = _layer(registry, layer_name)
        identity = str(getattr(element, layer.identity_field))
        elements = layers.setdefault(layer.name, [])
        for position, existing in enumerate(elements):
            if str(getattr(existing, layer.identity_field, None)) == identity:
                elements[position] = element
                break
        else:
            elements.append(element)
        if placement:
            placements.setdefault(layer.name, {})[identity] = dict(placement)
    return ModelSnapshot.from_state(ModelState(layers=layers, placement=placements, absent=state.absent))


def _layer(registry: reg.Registry, name: str) -> reg.LayerDescriptor:
    try:
        return registry.layer(name)
    except KeyError:
        raise DerivationFault(
            f"no layer '{name}' is declared; the model has {list(registry.layer_names)}. A plan states the "
            "protocol's layer names, not the snapshot's attribute names"
        ) from None


def _operation(entry: DeltaEntry) -> dict[str, Any]:
    """One delta entry as the change operation of the same name at the same address."""
    address = entry.address.to_json()
    if entry.operation == "remove":
        return {"operation": "remove", "address": address}
    if entry.after is ABSENT:
        # Reachable: the projection drops `after` for a change it cannot express
        # as an operation, such as a rewrite of an immutable field.
        raise DerivationFault(f"the projected {entry.operation} at {address} carries no `after` value")
    return {"operation": entry.operation, "address": address, "value": entry.after}


def _require_replay(
    baseline: ModelSnapshot,
    operations: Sequence[Mapping[str, Any]],
    planned: Sequence[PlannedElement],
    registry: reg.Registry,
) -> None:
    """The replay of *operations* against *baseline*, which must give the planned payloads back.

    Each operation is parsed as a submitted request is, so a malformed derived
    operation fails here rather than at the client's ``change_set`` call.
    """
    try:
        parsed = [parse_change_operation(operation, position) for position, operation in enumerate(operations)]
    except ValueError as exc:
        raise DerivationFault(f"a derived operation is not a protocol change operation: {exc}") from exc

    outcome = apply_operations(baseline, parsed, registry=registry)
    if outcome.violations:
        violation = outcome.violations[0]
        raise DerivationFault(f"replaying the derived operations was refused ({violation.error}): {violation.message}")
    if outcome.inapplicable:
        miss = outcome.inapplicable[0]
        raise DerivationFault(f"a derived operation could not be applied ({miss.reason}): {miss.message}")

    for layer_name, element, placement in planned:
        layer = _layer(registry, layer_name)
        identity = str(getattr(element, layer.identity_field))
        produced = outcome.staged.get(layer, identity)
        expected = publish_element(element, registry, placement=dict(placement) or None)
        if produced is None or _canonical(produced) != _canonical(expected):
            raise DerivationFault(
                f"replaying the derived operations did not reproduce the planned '{identity}' of layer "
                f"'{layer.name}': the change set would land something other than what the plan reported"
            )


def _canonical(value: Any) -> str:
    """One value's canonical serialization, for byte-level comparison."""
    return json.dumps(canonical_value(value), ensure_ascii=False)
