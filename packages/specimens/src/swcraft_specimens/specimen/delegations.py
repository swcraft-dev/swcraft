"""Delegations: the ports no connector reaches, chain steps and repeats.

Notation: the n components hold P ports, U of them unconnected, R = P - U reached; A =
`connectors.assembly`, D = `connectors.delegation`, C = `layers.compositions`; R_c of the reached
ports are reached by a connector end.

The components are **entered** whole, in ascending reached ports, until the entered ones reach R_c;
the last entered may be partial, its other reached ports delegated from the root (D_root). Every
component not entered is a member of the deepest composition, and its reached ports (D_deep) are
delegated from there, so D_root + D_deep = R - R_c.

The sources, used in this order until D is met exactly (`layout.Layout` fixes how many of each;
`build_delegations` lays exactly those):

1. **reach**: one delegation per delegation-only port; R - R_c of them;
2. **chain steps**: a D_deep port's boundary port is delegated from a new boundary port of the
   parent composition, one step at a time up to the root; capacity D_deep x (C - 1);
3. **repeats**: a star centre is delegated from a new boundary port of the root; capacity R_c - A.

So R - R_c <= D <= R - A + (C - 1) x D_deep(R_c); the admitted R_c for one placement are
`admitted_reach`'s interval, or {0} when A = 0. The `delegations` refusal names the bounds every
layout has (`layout.layout_bounds`); `no-layout` is the refusal when no layout exists.

Every delegation has a boundary port of its own, `BP<n>` within its composition, with the direction
and interface of its inner port; the delegations are `Dlg<n>` within their composition. A port is
reached by at most one delegation and at most one connector end, except a repeat's provide port.
The built delegations are counted again after the fact; a mismatch is a `GenerationError`, never a
refusal.
"""

from __future__ import annotations

import bisect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from ..errors import GenerationError
from ..model import Composition, CompositionPort, Delegation, Port
from ..passport import Refusal
from .common import ceil_div, stated_count
from .vocabulary import DELEGATION, DELEGATIONS

if TYPE_CHECKING:
    from .layout import Layout

#: The stem of a boundary port's name, numbered within its composition.
BOUNDARY_PORT_STEM = "BP"
#: The stem of a delegation's name, numbered within its composition.
DELEGATION_STEM = "Dlg"


def delegation_count(stated: Mapping[str, Any]) -> int:
    """D, the delegations a stated passport asks for: none when the leaf is unstated."""
    return stated_count(stated, DELEGATION)


@dataclass(frozen=True)
class EntryOrder:
    """One unconnected placement's reached ports in the order the components are entered.

    With their running sums: ascending r_j, ties by index (`of`).
    """

    reached: tuple[int, ...]
    order: tuple[int, ...]
    #: prefix[k]: the reached ports of the first k components entered.
    prefix: tuple[int, ...]

    @classmethod
    def of(cls, reached: Sequence[int]) -> EntryOrder:
        order = sorted(range(len(reached)), key=lambda j: (reached[j], j))
        prefix = [0]
        for j in order:
            prefix.append(prefix[-1] + reached[j])
        return cls(tuple(reached), tuple(order), tuple(prefix))

    @property
    def total(self) -> int:
        """R, the reached ports."""
        return self.prefix[-1]

    def entered(self, connected: int) -> int:
        """How many components are entered for R_c = `connected`.

        The fewest whose reached ports reach it.
        """
        return bisect.bisect_left(self.prefix, connected)

    def deep(self, connected: int) -> int:
        """D_deep(R_c): the connector-free components' reached ports."""
        return self.total - self.prefix[self.entered(connected)]

    def split(self, connected: int) -> Split:
        """The split this order makes at R_c = `connected`."""
        if not 0 <= connected <= self.total:
            raise GenerationError("the connector-reached ports exceed the reached ports")
        count = self.entered(connected)
        entered = self.order[:count]
        delegated = list(self.reached)
        for j in entered:
            delegated[j] = 0
        if count:
            delegated[entered[-1]] = self.prefix[count] - connected
        return Split(tuple(sorted(entered)), tuple(delegated))


@dataclass(frozen=True)
class Split:
    """The split of one placement at one R_c.

    Which components are entered (in component order) and each component's delegation-only ports
    (D_root on the last entered, every reached port on a connector-free one).
    """

    entered: tuple[int, ...]
    delegated: tuple[int, ...]

    @property
    def connector_free(self) -> tuple[int, ...]:
        entered = set(self.entered)
        return tuple(j for j in range(len(self.delegated)) if j not in entered)


def capacity(entry: EntryOrder, connectors: int, connected: int, compositions: int) -> int:
    """The most delegations the three sources give at R_c = `connected`.

    R - A + (C - 1) x D_deep(R_c), the repeats' R_c - A included.
    """
    return entry.total - connectors + (compositions - 1) * entry.deep(connected)


def most_connectors(entry: EntryOrder, delegations: int, connected: int, compositions: int) -> int:
    """The most connectors A for which `capacity` still holds D = `delegations` at R_c = `connected`.

    R - D + (C - 1) x D_deep(R_c), `capacity` with A and D swapped (with A unstated, the
    construction chooses none above it).
    """
    return capacity(entry, delegations, connected, compositions)


def reach_range(total: int, connectors: int | None, connectable: int) -> tuple[int, int] | None:
    """The R_c the connectors admit before D; `None` when empty.

    [A + 1, min(2A, R, the SR + CS ports)] for a stated A > 0, [0, 0] for A = 0, and
    [2, min(R, the SR + CS ports)] (or [0, 0] alone) for an unstated one.
    """
    if connectors == 0:
        return 0, 0
    if connectors is None:
        high = min(total, connectable)
        return (2, high) if high >= 2 else None
    low, high = connectors + 1, min(2 * connectors, total, connectable)
    return (low, high) if low <= high else None


def admitted_reach(
    entry: EntryOrder,
    connectors: int | None,
    delegations: int,
    compositions: int,
    connectable: int,
) -> list[int]:
    """The R_c one placement admits, from the top down.

    With A unstated, the capacity is checked at the fewest connectors R_c admits, ceil(R_c / 2),
    and again at the A the construction chooses; R_c = 0 (no connector) comes last.
    """
    total = entry.total
    candidates: list[int] = []
    span = reach_range(total, connectors, connectable)
    if span is not None and span != (0, 0):
        low, high = max(span[0], total - delegations), span[1]

        def holds(connected: int) -> bool:
            least = connectors if connectors is not None else ceil_div(connected, 2)
            return delegations <= capacity(entry, least, connected, compositions)

        if low <= high and holds(low):
            # The capacity falls as R_c rises: the largest R_c that holds, by bisection.
            first, last = low, high
            while first < last:
                middle = (first + last + 1) // 2
                if holds(middle):
                    first = middle
                else:
                    last = middle - 1
            candidates.extend(range(first, low - 1, -1))
    if (connectors in (0, None)) and total <= delegations <= compositions * total:
        candidates.append(0)
    return candidates


def delegation_refusal(
    delegations: int,
    total: int,
    connectors: int | None,
    compositions: int,
    bounds: tuple[int, int],
) -> Refusal:
    """`delegations` at `connectors.delegation`: D outside the bounds the layout admits.

    The bounds are those the reached ports, the connectors and the compositions admit.
    """
    least, most = bounds
    with_connectors = f"{connectors} connectors" if connectors is not None else "the connectors chosen"
    return Refusal(
        DELEGATION,
        DELEGATIONS,
        f"states {delegations}; with {total} reached ports, {with_connectors} and "
        f"{compositions} compositions the delegations number at least {least} and at most "
        f"{most}",
    )


def _delegated_ports(ports: Sequence[Port], open_ports: Sequence[str], count: int, stubs: frozenset[str]) -> list[str]:
    """`count` of a component's ports no connector reaches, returned in port order.

    Those on a stub are taken first, then in port order.
    """
    position = {port.name: index for index, port in enumerate(ports)}
    interface = {port.name: port.interface for port in ports}
    chosen = sorted(open_ports, key=lambda name: (interface[name] not in stubs, position[name]))
    return sorted(chosen[:count], key=lambda name: position[name])


class _Composition:
    """One composition's boundary ports and delegations as they are added."""

    def __init__(self, composition: Composition) -> None:
        self.composition = composition
        self.ports: list[CompositionPort] = []
        self.delegations: list[Delegation] = []

    def delegate(self, member: str, member_port: str, direction: str, interface: str) -> str:
        """A new boundary port delegated to `member`'s `member_port`; returns its name."""
        port = f"{BOUNDARY_PORT_STEM}{len(self.ports) + 1}"
        self.ports.append(CompositionPort(port, direction, interface))
        self.delegations.append(
            Delegation(
                f"{DELEGATION_STEM}{len(self.delegations) + 1}",
                port,
                member,
                member_port,
            )
        )
        return port

    def built(self) -> Composition:
        return replace(
            self.composition,
            ports=(*self.composition.ports, *self.ports),
            delegations=(*self.composition.delegations, *self.delegations),
        )


def build_delegations(
    compositions: Sequence[Composition],
    component_names: Sequence[str],
    ports: Sequence[Sequence[Port]],
    open_ports: Sequence[Sequence[str]],
    layout: Layout,
    centres: Sequence[tuple[int, str]],
    stubs: frozenset[str],
) -> tuple[Composition, ...]:
    """The chain `compositions` (root first, deepest last) with the delegations `layout` decided.

    Its reach, chain steps and repeats, exactly, in that order.

    `ports[j]` and `open_ports[j]` are component j's ports and the names of those no connector
    reaches; `layout.delegated[j]` of them are reached by a delegation alone, from the composition
    component j is a member of; `centres` the star centres as (component, provide port), in
    emission order; `stubs` the external interface stubs' names. A source the chain cannot give
    as decided is a `GenerationError`."""
    chain = [_Composition(composition) for composition in compositions]
    root, deepest = chain[0], chain[-1]
    member_of: dict[str, tuple[_Composition, str]] = {}
    for holder in chain:
        for member in holder.composition.members:
            if member.type is not None:
                member_of[member.type] = (holder, member.name)
    # 1. reach: every delegation-only port, from the composition its component is a member of.
    reach = 0
    deep_ports: list[str] = []
    for j, name in enumerate(component_names):
        if not layout.delegated[j]:
            continue
        holder, member_name = member_of[name]
        by_name = {port.name: port for port in ports[j]}
        chosen = _delegated_ports(ports[j], open_ports[j], layout.delegated[j], stubs)
        if len(chosen) != layout.delegated[j]:
            raise GenerationError("a component has fewer open ports than its delegation-only ones")
        for port_name in chosen:
            port = by_name[port_name]
            boundary = holder.delegate(member_name, port_name, port.direction, port.interface)
            reach += 1
            if holder is deepest:
                deep_ports.append(boundary)
    # 2. chain steps: each D_deep port's boundary port up the chain, port by port.
    steps = 0
    if len(chain) > 1:
        boundary_of = {port.name: port for port in deepest.ports}
        for boundary in deep_ports:
            step_port = boundary_of[boundary]
            current = boundary
            for depth in range(len(chain) - 1, 0, -1):
                if steps == layout.steps:
                    break
                parent, child = chain[depth - 1], chain[depth]
                current = parent.delegate(child.composition.name, current, step_port.direction, step_port.interface)
                steps += 1
            if steps == layout.steps:
                break
    # 3. repeats: a star centre delegated from the root.
    repeats = 0
    for j, port_name in centres:
        if repeats == layout.repeats:
            break
        holder, member_name = member_of[component_names[j]]
        if holder is not root:
            raise GenerationError("a star centre lies outside the root")
        port = next(p for p in ports[j] if p.name == port_name)
        root.delegate(member_name, port_name, port.direction, port.interface)
        repeats += 1
    if (reach, steps, repeats) != (layout.reach, layout.steps, layout.repeats):
        raise GenerationError("the delegations do not reproduce the layout's sources")
    return tuple(holder.built() for holder in chain)
