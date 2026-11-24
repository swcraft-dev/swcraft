"""The shape of a specimen: the port counts, the fan-outs and the connectors, decided together.

:func:`specimen_shape` derives what the passport leaves unstated: `components.ports.total`,
`components.ports.top`, `interfaces.fan_out.top` and the connector count A (`connectors.assembly`,
`layers.connectors`; the fewest the search fits whose connector names pass the identifier check).
The placement (`components.py`), the fan-outs (`interfaces.py`) and the connector construction
(`connectors.py`) then decide their leaves for those inputs.

The conflicts the stated leaves decide alone are refused first, each at its leaf with its own class:
a fan-out range missing the port range or too many forced fan-out-1 ports (`fan-out`); unconnected
ports with no room (`unconnected` at `components.unconnected.max_on_one`, or at
`components.unconnected.total` for exactly one reached port with A unstated); a connector range
missing the totals, kept reached ports above A, A above the named interfaces' capacity or the
star-size bound above every admitted total (`connectors`); the `links` inequality and a hub's own
`links` conditions (`links`). Each of these is a closed-form bound, not every consequence of the
stated leaves.

The totals inside those bounds, none above `common.COUNT_CEILING`, are then tried from the preferred
one (two ports per free component on average) up, then down; per total up to `PLACEMENTS_TRIED`
placements and the fan-out tails from the fewest interfaces up. The first configuration all three
accept is the shape; it takes the full connector search, the others a quick one. With
`connectors.delegation` > 0 the shape also decides the layout (`layout.Layout`): a preferred
candidate, else the entry planner; no layout for the first configuration is `no-layout` at
`connectors.delegation`.

When no configuration is accepted, the first one's refusals are reported: with every derivation leaf
stated they keep their classes, otherwise each is `derived`, naming the derived leaves. The
configurations are capped at `SHAPE_BUDGET`, so a configuration outside the tried ones is refused
`derived`, never approximated; with delegations the time is not bounded by that budget.
"""

from __future__ import annotations

import bisect
import functools
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ..errors import GenerationError
from ..passport import Refusal

# `layout.plan` is reached through the module, so the tests can replace it.
from . import layout
from .common import COUNT_CEILING, Budget, ceil_div, hub_count, link_bounds
from .components import (
    Placement,
    PortSpec,
    choose_ports,
    place_ports,
    port_most_stated,
    port_placements,
    port_range,
    port_spec,
)
from .compositions import composition_count
from .connectors import (
    FULL_SEARCH,
    QUICK_SEARCH,
    ConnectorSpec,
    Search,
    assembly_sum_refusal,
    capacity_refusal,
    configuration_solution,
    connector_refusals,
    connector_spec,
    construction_spec,
    hub_links_refusal,
    kept_refusal,
    links_refusal,
    one_reached_refusal,
    solve_open,
    unconnected_placements,
    unconnected_refusal,
)
from .delegations import (
    EntryOrder,
    admitted_reach,
    delegation_count,
    delegation_refusal,
    most_connectors,
    reach_range,
)
from .interfaces import (
    connectable_fan_outs,
    fan_out_bounds,
    fan_out_capacity,
    fan_out_forced,
    forced_leaf,
    forced_sentence,
    split_fan_outs,
    tail_parts,
)
from .layout import (
    NO_DELEGATION,
    PREFERRED,
    UNPLANNED_STEP,
    Configuration,
    Layout,
    derived_total_clause,
    layout_bounds,
    no_layout_refusal,
)
from .vocabulary import (
    ASSEMBLY,
    CONNECTORS_CLASS,
    DELEGATION,
    DELEGATIONS,
    DERIVED,
    FAN_OUT,
    FAN_OUT_MAX,
    FAN_OUT_TOP,
    LINKS,
    LINKS_CLASS,
    NO_LAYOUT,
    PORTS_TOP,
    PORTS_TOTAL,
)

#: The configurations (a total and a number of interfaces beyond the head) one shape may try.
SHAPE_BUDGET = 64
#: The placements of one total the derivation tries when `components.ports.top` is unstated.
PLACEMENTS_TRIED = 6

#: A configuration: a total, one placement of it (`None` for no component) and the number of
#: interfaces beyond the fan-out head (`None` for none).
_Configuration = tuple[int, Placement | None, int | None]


@dataclass(frozen=True)
class Shape:
    """The port counts and fan-outs (descending), the connector search and the layout to execute."""

    counts: tuple[int, ...]
    fan_outs: tuple[int, ...]
    #: The connector search the shape ran; the builder runs it only when no layout solution is
    #: given, so a planner-built layout records `FULL_SEARCH`.
    search: Search
    #: The preferred candidate's layout, the planner's, or the search's with no delegation.
    layout: Layout
    #: With A unstated, whether no count the search fits meets the stated identifier leaves, so
    #: A is the fewest it fits and the identifier check refuses it (`_shape_cached`).
    names_unmet: bool = False
    #: The derived leaves (`leaf derived ...`) a refusal at that count names: none when the
    #: total is stated, so the refusal keeps its class.
    derived: tuple[str, ...] = ()

    @property
    def connectors(self) -> int:
        """A, the layout's connectors."""
        return self.layout.connectors


@dataclass(frozen=True)
class NoLayout:
    """A configuration with delegations that has no layout; `_search` refuses it as `no-layout`."""

    configuration: Configuration


def _preferred_total(spec: PortSpec) -> int | None:
    """The placement's own default total, or `None` when none.

    The smallest any median admits at which every free component holds two ports on average, else
    the smallest it admits.
    """
    chosen = choose_ports(spec)
    if isinstance(chosen, Refusal):
        return None
    _, free_sum = chosen
    return spec.placed_sum + free_sum


def _ceiling_binds(stated: Mapping[str, Any], spec: PortSpec, choosable: Sequence[int] | None) -> bool:
    """Whether the count ceiling, not a stated leaf, bounds the derived totals from above."""
    if port_most_stated(spec):
        return False
    _, (_, other_high) = _bounds(stated, spec, choosable)
    return other_high > COUNT_CEILING


def _star_connectors(connectors: ConnectorSpec, spec: PortSpec) -> int | None:
    """A when stated, or when `links` forces it (no hub and L > 0: A = m // 2 + L); else `None`."""
    if connectors.connectors is not None:
        return connectors.connectors
    least, most = link_bounds(spec.n, spec.k, connectors.links)
    return least if connectors.links and most == least else None


def star_size_total(stated: Mapping[str, Any]) -> int | None:
    """The least total U + ceil(f x A / (f - 1)): A connectors in stars of at most f ports.

    Applies with A stated or forced by `links` and a stated `fan_out.max` f >= 2, else `None`.

    What this does not reach: a necessary condition on the total, not on the placement; no bound is
    taken with A unstated and unforced.
    """
    found = _star_bound(stated, connector_spec(stated), port_spec(stated))
    return None if found is None else found[0]


def _star_bound(stated: Mapping[str, Any], connectors: ConnectorSpec, spec: PortSpec) -> tuple[int, int, int] | None:
    """`star_size_total`'s bound, the A and the f it reads, or `None` when it does not apply."""
    count = _star_connectors(connectors, spec)
    widest = int(stated[FAN_OUT_MAX]) if FAN_OUT_MAX in stated else None
    if not count or widest is None or widest < 2:
        return None
    return connectors.unconnected + ceil_div(widest * count, widest - 1), count, widest


def _star_refusal(
    stated: Mapping[str, Any], connectors: ConnectorSpec, spec: PortSpec, low: int, high: int
) -> Refusal | None:
    """`connectors` at the connector leaf when the star-size bound exceeds `high`.

    `low` to `high` are the totals the port and fan-out leaves admit, decided by a stated leaf.
    """
    found = _star_bound(stated, connectors, spec)
    if found is None or found[0] <= high:
        return None
    bound, count, widest = found
    if connectors.connectors is not None:
        subject = f"states {count}: the connectors"
    else:
        subject = f"states {connectors.links}, with {spec.n} components and no hub: the {count} connectors"
    return Refusal(
        connectors.connector_leaf,
        CONNECTORS_CLASS,
        f"{subject}, in stars of at most {widest} ports ({FAN_OUT_MAX}), reach at "
        f"least {bound - connectors.unconnected} ports, so with {connectors.unconnected} "
        f"unconnected at least {bound} component ports in all; the port and fan-out leaves "
        f"admit {_span(low, high)}",
    )


def _connector_range(connectors: ConnectorSpec, delegations: int) -> tuple[int, int] | None:
    """The totals a stated A admits: U + A + 1 to U + 2A + D, U to U + D for A = 0; `None` unstated."""
    count, unconnected = connectors.connectors, connectors.unconnected
    if count is None:
        return None
    if count == 0:
        return unconnected, unconnected + delegations
    return unconnected + count + 1, unconnected + 2 * count + delegations


def _count_range(unconnected: int, counts: Sequence[int], delegations: int) -> tuple[int, int]:
    """The totals some count of `counts` (ascending, not empty) admits with U unconnected and D delegations.

    0 needs U to U + D, a count a > 0 between U + a + 1 and U + 2a + D.
    """
    least, most = counts[0], counts[-1]
    return unconnected + (least + 1 if least else 0), unconnected + 2 * most + delegations


def _bounds(
    stated: Mapping[str, Any], spec: PortSpec, choosable: Sequence[int] | None
) -> tuple[tuple[int, int], tuple[int, int]]:
    """The port leaves' bounds of the total, and the fan-out and connector leaves' bounds.

    With A unstated the connector bounds are those of `choosable` (ascending; `None` when A is stated).
    """
    low, high = fan_out_bounds(stated)
    connectors = connector_spec(stated)
    delegations = delegation_count(stated)
    low = max(low, connectors.unconnected)
    span = _connector_range(connectors, delegations)
    if span is None and choosable:
        span = _count_range(connectors.unconnected, choosable, delegations)
    if span is not None:
        low, high = max(low, span[0]), min(high, span[1])
    star = _star_bound(stated, connectors, spec)
    low = max(low, 0 if star is None else star[0])
    return port_range(spec), (low, high)


def _admits_choosable(total: int, unconnected: int, choosable: Sequence[int], delegations: int = 0) -> bool:
    """Whether `total` ports admit some count of `choosable` (ascending).

    0 when the R reached ports are at most D, else one of ceil((R - D) / 2) to R - 1, at least 1.
    """
    reached = total - unconnected
    if reached < 0 or not choosable:
        return False
    if reached <= delegations and choosable[0] == 0:
        return True
    if reached < 2:
        return False
    index = bisect.bisect_left(choosable, max(1, ceil_div(reached - delegations, 2)))
    return index < len(choosable) and choosable[index] <= reached - 1


def _clamp(value: int, low: int, high: int) -> int:
    """`value` inside [low, high]."""
    return min(max(value, low), high)


def _totals(stated: Mapping[str, Any], spec: PortSpec, choosable: Sequence[int] | None) -> list[int]:
    """The totals to try, in order (see the module docstring)."""
    if spec.total is not None:
        return [spec.total]
    if spec.n == 0:
        return [0]
    preferred = _preferred_total(spec)
    if preferred is None:
        return []
    (port_low, port_high), (other_low, other_high) = _bounds(stated, spec, choosable)
    low = max(port_low, other_low)
    high = other_high if port_high < 0 else min(port_high, other_high)
    if low > high:
        return [_clamp(_clamp(preferred, other_low, other_high), port_low, port_high)]
    unconnected = connector_spec(stated).unconnected
    delegations = delegation_count(stated)
    above = range(max(preferred, low), high + 1)
    below = range(min(preferred - 1, high), low - 1, -1)
    totals: list[int] = []
    for values in (above, below):
        for total in values:
            if choosable and not _admits_choosable(total, unconnected, choosable, delegations):
                continue
            if len(totals) >= SHAPE_BUDGET:
                return totals
            totals.append(total)
    return totals


def _derives(stated: Mapping[str, Any], spec: PortSpec) -> list[str]:
    """The unstated leaves the derivation, or the construction, chooses values of.

    The total whenever a component's count is free (one component included), `top` with two
    components or more, the fan-outs beyond `max`, and A.
    """
    derived: list[str] = []
    if spec.total is None and spec.free > 0:
        derived.append(PORTS_TOTAL)
    if spec.n > 1 and spec.top is None:
        derived.append(PORTS_TOP)
    if FAN_OUT_TOP not in stated:
        derived.append(FAN_OUT_TOP)
    if connector_spec(stated).connectors is None:
        derived.append(ASSEMBLY)
    return derived


def _span(low: int, high: int) -> str:
    return f"exactly {low}" if low == high else f"between {low} and {high}"


def _unconnected_room(spec: PortSpec, on_one: int) -> int:
    """The most unconnected ports the components hold at most u1 = `on_one` each.

    The stated counts (the hubs and the placed part of `top`) min(p, u1) each, a free one at most
    u1 and at most the bound the stated counts put on it.
    """
    bound = spec.free_bound
    free = on_one if bound is None else min(on_one, bound)
    stated = sum(min(count, on_one) for count in (*spec.hubs, *spec.placed))
    return stated + spec.free * free


def _stated_refusals(stated: Mapping[str, Any], spec: PortSpec) -> list[Refusal]:
    """The conflicts the stated leaves decide alone, each with its own class."""
    port_low, port_high = port_range(spec)
    fan_low, fan_high = fan_out_bounds(stated)
    fan_leaf = FAN_OUT_TOP if FAN_OUT_TOP in stated else FAN_OUT_MAX
    if fan_high < port_low or fan_low > port_high:
        admits = (
            f"admits {_span(fan_low, fan_high)} component ports in all; the port leaves admit "
            f"{_span(port_low, port_high)}"
        )
        return [Refusal(fan_leaf, FAN_OUT, admits)]
    low, high = max(port_low, fan_low), min(port_high, fan_high)
    connectors = connector_spec(stated)
    total, count, on_one = connectors.unconnected, connectors.connectors, connectors.max_on_one
    # A bound growing with the most total is a stated conflict only when a stated leaf decides
    # that most, never the count ceiling alone.
    bounded = fan_high <= port_high or port_most_stated(spec)
    largest = spec.largest
    delegations = delegation_count(stated)
    refusals: list[Refusal] = []
    stubs, ones = fan_out_forced(stated, low, high)
    # The ports no connector reaches are U and at most D reached by delegation.
    delegated = min(delegations, max(0, high - total))
    if stubs + ones > total + delegated:
        refusals.append(
            Refusal(
                forced_leaf(stated, stubs),
                FAN_OUT,
                forced_sentence(stubs, ones, total, delegated),
            )
        )
    unconnected = unconnected_refusal(
        connectors,
        largest,
        min(high, _unconnected_room(spec, on_one)),
        f"the {spec.n} components, with the port and fan-out leaves,",
    )
    if unconnected is not None:
        refusals.append(unconnected)
    capacity = fan_out_capacity(stated, high) if bounded else None
    span = _connector_range(connectors, delegations)
    if span is not None and (span[1] < low or span[0] > high):
        refusals.append(
            Refusal(
                connectors.connector_leaf,
                CONNECTORS_CLASS,
                f"states {count}; with {total} unconnected ports they need {_span(*span)} "
                f"component ports in all, and the port and fan-out leaves admit "
                f"{_span(low, high)}",
            )
        )
    else:
        # With delegations a component's kept reached ports may be delegation-only.
        kept = kept_refusal(connectors, largest) if not delegations else None
        if kept is None and capacity is not None:
            kept = capacity_refusal(connectors, capacity)
        if kept is not None:
            refusals.append(kept)
    links = links_refusal(connectors, spec.n, spec.k)
    if links is None and spec.k and not refusals:
        # As the construction checks them: only once the closed-form inequalities pass.
        hubs = sum(spec.hubs)
        hub_room = sum(min(hub, on_one) for hub in spec.hubs)
        ordinary_room = _unconnected_room(spec, on_one) - hub_room
        # With delegations the kept reached ports need no connector end.
        links = hub_links_refusal(
            connectors,
            spec.n,
            spec.k,
            high - hubs if bounded else None,
            0 if delegations else max(0, low - hubs - min(total, ordinary_room)),
            0 if delegations else hubs - min(total, hub_room),
        )
    if bounded and delegations:
        reached = max(0, high - total)
        compositions = composition_count(stated)
        # D <= R - A + (C - 1) x D_deep with D_deep <= R - R_c <= R - A - 1 for A > 0 (R_c > A),
        # so C x (R - A) - (C - 1); for A = 0 (or unstated, where A = 0 stays open) C x R.
        if not count:
            most = compositions * reached
        elif reached > count:
            most = compositions * (reached - count) - (compositions - 1)
        else:
            # No R_c above A: the connector range refuses A; this bound only restates it.
            most = compositions * reached - count
        if delegations > most:
            with_connectors = (
                f", {compositions} compositions and {count} connectors"
                if count
                else f" and {compositions} compositions"
            )
            refusals.append(
                Refusal(
                    DELEGATION,
                    DELEGATIONS,
                    f"states {delegations}; the at most {reached} reached ports{with_connectors} "
                    f"admit at most {max(0, most)}",
                )
            )
    if links is None and count is None and not delegations:
        refusals.extend(
            _unstated_count_refusals(connectors, spec, fan_leaf, low - total, high - total, largest, capacity)
        )
    elif links is not None:
        refusals.append(links)
    if not refusals and bounded:
        # Last, so every conflict refused before it keeps its leaf and class.
        star = _star_refusal(stated, connectors, spec, low, high)
        if star is not None:
            refusals.append(star)
    return refusals


def _unstated_count_refusals(
    connectors: ConnectorSpec,
    spec: PortSpec,
    fan_leaf: str,
    reached_low: int,
    reached_high: int,
    largest: int | None,
    capacity: int | None,
) -> list[Refusal]:
    """With A unstated, the admitted counts against each other and against the `links` inequality.

    `capacity` is `None` when the count ceiling, not a stated leaf, bounds the totals. One reached
    port is `unconnected`; a needed count above the capacity is `fan-out`; no count at all is
    `connectors` when a stated leaf bounds the totals; the rest is `links`, whose upper end is a
    conflict only when a stated leaf bounds the totals.
    """
    if reached_low == reached_high == 1:
        return [one_reached_refusal()]
    least, most = link_bounds(spec.n, spec.k, connectors.links)
    none_reached = reached_low <= 0 <= reached_high
    if none_reached and least == 0:
        return []
    kept = 0 if largest is None else largest - min(largest, connectors.max_on_one)
    low = max(1, kept, ceil_div(max(reached_low, 2), 2))
    high = reached_high - 1
    if low <= high and capacity is not None and capacity < low:
        if not none_reached:
            return [
                Refusal(
                    fan_leaf,
                    FAN_OUT,
                    f"admits named interfaces for at most {capacity} connectors, each with a "
                    f"provide port of its own; the port and unconnected leaves need at least "
                    f"{low}",
                )
            ]
        high = capacity
    elif capacity is not None:
        high = min(high, capacity)
    if low > high:
        if not none_reached:
            if kept <= reached_high - 1 or capacity is None:
                return []
            # The largest count keeps more reached ports than the others can take: no count (a conflict
            # only when a stated leaf, not the count ceiling, bounds the totals).
            return [
                Refusal(
                    connectors.connector_leaf,
                    CONNECTORS_CLASS,
                    f"states {connectors.links}, with no connector count stated: a component of "
                    f"{largest} ports keeps at least {kept} reached, each with a connector of its "
                    f"own, and the {reached_high} reached ports at most allow at most "
                    f"{reached_high - 1}",
                )
            ]
        # Only R = 0 is left, and the links inequality needs a connector.
        need = "none, no port being reached"
    elif most is not None and most < low:
        need = f"at least {low}"
    elif least > high and capacity is not None:
        need = f"at most {high}"
    else:
        return []
    allowed = _span(least, most) if most is not None else f"at least {least}"
    return [
        Refusal(
            LINKS,
            LINKS_CLASS,
            f"states {connectors.links}: with {spec.n} components, {spec.k} of them hubs, the "
            f"connectors number {allowed}; the port, fan-out and unconnected leaves need {need}",
        )
    ]


def _configurations(
    stated: Mapping[str, Any], spec: PortSpec, choosable: Sequence[int] | None
) -> Iterator[_Configuration]:
    most = PLACEMENTS_TRIED if spec.top is None else 1
    on_one = connector_spec(stated).max_on_one
    for total in _totals(stated, spec, choosable):
        trial = PortSpec(spec.n, spec.hubs, spec.top, total)
        placements: list[Placement | None] = [None]
        if spec.n:
            chosen = choose_ports(trial)
            if isinstance(chosen, Refusal):
                yield total, None, None
                continue
            placements = list(port_placements(trial, most, on_one))
        parts: list[int | None] = list(tail_parts(stated, total)) or [None]
        for placement in placements:
            for count in parts:
                yield total, placement, count


def _try(
    stated: Mapping[str, Any],
    spec: PortSpec,
    connectors: ConnectorSpec,
    configuration: _Configuration,
    search: Search,
) -> Shape | list[Refusal] | NoLayout:
    """The shape of one configuration, its refusals, or `NoLayout` when no layout exists."""
    total, placement, parts = configuration
    trial = PortSpec(spec.n, spec.hubs, spec.top, total)
    counts: tuple[int, ...] = ()
    if spec.n:
        if placement is None:
            chosen = choose_ports(trial)
            if isinstance(chosen, Refusal):
                return [chosen]
            raise GenerationError("a placed total yielded no placement")
        counts = place_ports(trial, placement)
    fan_outs = split_fan_outs(stated, sum(counts), parts)
    if isinstance(fan_outs, Refusal):
        return [fan_outs]
    # The stubs carry no connector: the stars go into the SR + CS interfaces alone.
    connectable = connectable_fan_outs(stated, fan_outs)
    delegations = delegation_count(stated)
    if delegations:
        return _try_delegated(stated, connectors, counts, fan_outs, connectable, search)
    refusals = connector_refusals(connectors, counts, connectable, search)
    if refusals:
        return refusals
    solution = configuration_solution(connectors, counts, connectable, search)
    if solution is None:
        raise GenerationError("an accepted connector configuration has no connector count")
    # With D = 0 the search chose the placement; the layout records it.
    recorded = Layout.of_solution(
        counts,
        solution.unconnected,
        solution,
        0,
        composition_count(stated),
        source=NO_DELEGATION,
        step=UNPLANNED_STEP,
    )
    return Shape(counts, fan_outs, search, recorded)


def _try_delegated(
    stated: Mapping[str, Any],
    connectors: ConnectorSpec,
    counts: Sequence[int],
    fan_outs: tuple[int, ...],
    connectable: Sequence[int],
    search: Search,
) -> Shape | list[Refusal] | NoLayout:
    """One configuration with D > 0: the closed forms and layout bounds, the preferred candidate, the planner.

    An unstated A takes the counts `names.admits` (every count with no names) from 0 to min(R, the
    SR + CS ports), not `ConnectorNames.choosable`: its domain ignores the delegations, and with
    D > 0 a layout may have fewer connectors than R / 2.
    """
    delegations = delegation_count(stated)
    compositions = composition_count(stated)
    count = connectors.connectors
    reached = sum(counts) - connectors.unconnected
    phi = sum(connectable)
    refusals = [
        refusal
        for refusal in (
            unconnected_refusal(
                connectors,
                max(counts, default=0),
                sum(min(c, connectors.max_on_one) for c in counts),
                "the components",
            ),
            links_refusal(connectors, len(counts), hub_count(counts)),
            capacity_refusal(connectors, sum(f - 1 for f in connectable)),
        )
        if refusal is not None
    ]
    if refusals:
        return refusals
    if count and reach_range(reached, count, phi) is None:
        allowed = min(reached, phi) - 1
        return [
            Refusal(
                connectors.connector_leaf,
                CONNECTORS_CLASS,
                f"states {count}; the {reached} reached ports on the {phi} ports of the "
                f"sender-receiver and client-server interfaces allow at most {max(0, allowed)}, "
                "since a provide port carries no require port's connector",
            )
        ]
    # The delegations every layout admits, before any placement is drawn or solved.
    names = connectors.names
    choosable: tuple[int, ...] = ()
    if count is None:
        choosable = tuple(a for a in range(max(0, min(reached, phi)) + 1) if names is None or names.admits(a))
    bounds = layout_bounds(reached, count, compositions, phi, choosable)
    if not bounds[0] <= delegations <= bounds[1]:
        return [delegation_refusal(delegations, reached, count, compositions, bounds)]
    # The preferred candidate, one per configuration.
    units = next(iter(unconnected_placements(connectors, counts)), None)
    if units is not None:
        entry = EntryOrder.of([c - u for c, u in zip(counts, units, strict=True)])
        reach = admitted_reach(entry, count, delegations, compositions, phi)
        if reach:
            connected = reach[0]
            split = entry.split(connected)
            open_units = tuple(u + d for u, d in zip(units, split.delegated, strict=True))
            most = None if count is not None else most_connectors(entry, delegations, connected, compositions)
            solution = solve_open(connectors, counts, open_units, connectable, search, most)
            if solution is not None:
                preferred = Layout.of_solution(
                    counts,
                    units,
                    solution,
                    delegations,
                    compositions,
                    source=PREFERRED,
                    step=UNPLANNED_STEP,
                )
                return Shape(tuple(counts), fan_outs, search, preferred)
    # The planner.
    configuration = Configuration.of_spec(connectors, counts, connectable, delegations, compositions)
    planned = layout.plan(configuration, choosable)
    if planned.layout is None:
        return NoLayout(configuration)
    return Shape(tuple(counts), fan_outs, FULL_SEARCH, planned.layout)


def _named(derived: Sequence[str], counts_total: int | None, capped: bool) -> list[str]:
    """The derived leaves as a refusal names them (`leaf derived`, the total with its value)."""
    ceiling = f" (the totals tried stop at the count ceiling, {COUNT_CEILING:,})" if capped else ""
    return [
        f"{leaf} derived as {counts_total}{ceiling}"
        if leaf == PORTS_TOTAL and counts_total is not None
        else f"{leaf} derived"
        for leaf in derived
    ]


def _derived(refusal: Refusal, derived: list[str], counts_total: int | None, capped: bool = False) -> Refusal:
    return derived_refusal(refusal, _named(derived, counts_total, capped))


def derived_refusal(refusal: Refusal, named: Sequence[str]) -> Refusal:
    """`refusal` with the class `derived`, naming the derived leaves (`named`).

    Each of `named` reads `leaf derived` or `leaf derived as value`.
    """
    return Refusal(
        refusal.leaf,
        DERIVED,
        f"{refusal.sentence}; with {' and '.join(named)}, no derivation the specimen generator tried met the "
        "stated leaves, which pass the closed-form inequalities they decide alone",
    )


@functools.lru_cache(maxsize=16)
def _shape_cached(key: str) -> Shape | tuple[Refusal, ...]:
    stated: dict[str, Any] = json.loads(key)
    spec = port_spec(stated)
    assembly_sum = assembly_sum_refusal(connector_spec(stated))
    if assembly_sum is not None:
        return (assembly_sum,)
    derived = _derives(stated, spec)
    if derived:
        stated_refusals = _stated_refusals(stated, spec)
        if stated_refusals:
            return tuple(stated_refusals)
    construction = construction_spec(stated)
    names = construction.names
    found = _search(stated, spec, construction, None if names is None else names.choosable)
    if isinstance(found, Shape):
        return found
    first, first_total = found
    if first is None:
        chosen = choose_ports(spec)
        if isinstance(chosen, Refusal):
            return (chosen,)
        raise GenerationError("a placed port specification gave no configuration to try")
    if not derived:
        return tuple(first)
    capped = _ceiling_binds(stated, spec, None if names is None else names.choosable)
    if names is not None and names.constrains:
        # No count the search fits meets the stated identifier leaves: the fewest it fits is
        # A, and the identifier check refuses it with its own class when the total is stated.
        # With delegations the planner takes every count of the domain.
        unmet = _search(stated, spec, replace(construction, names=None), names.counts)
        if isinstance(unmet, Shape) and names.refused_at(unmet.connectors):
            named = tuple(_named(derived, sum(unmet.counts), capped)) if PORTS_TOTAL in derived else ()
            return replace(unmet, names_unmet=True, derived=named)
    return tuple(
        refusal
        if refusal.leaf.startswith("identifiers.") and PORTS_TOTAL not in derived
        else _derived(_with_totals(refusal, derived), derived, first_total, capped)
        for refusal in first
    )


def _with_totals(refusal: Refusal, derived: Sequence[str]) -> Refusal:
    """`no-layout` with the derivation's own clause when the total is derived."""
    if refusal.refusal_class != NO_LAYOUT or PORTS_TOTAL not in derived:
        return refusal
    return Refusal(
        refusal.leaf,
        refusal.refusal_class,
        f"{refusal.sentence}; {derived_total_clause(SHAPE_BUDGET)}",
    )


def _search(
    stated: Mapping[str, Any],
    spec: PortSpec,
    construction: ConnectorSpec,
    choosable: Sequence[int] | None,
) -> Shape | tuple[list[Refusal] | None, int | None]:
    """The first configuration the construction accepts, or the first one's refusals and total.

    The totals tried are those that admit a count of `choosable` (ascending; `None` when A is
    stated). The total is `None` when none was tried. Each configuration costs one unit of `SHAPE_BUDGET`.
    When the first one has no layout, the refusal is `no-layout`, naming every configuration tried.
    """
    first: list[Refusal] | NoLayout | None = None
    first_total: int | None = None
    tried = 0
    budget = Budget(SHAPE_BUDGET)
    for configuration in _configurations(stated, spec, choosable):
        if budget.left <= 0:
            break
        search = FULL_SEARCH if first is None else QUICK_SEARCH
        budget.spend()
        result = _try(stated, spec, construction, configuration, search)
        if isinstance(result, Shape):
            return result
        tried += 1
        if first is None:
            first, first_total = result, configuration[0]
    if isinstance(first, NoLayout):
        return [no_layout_refusal(first.configuration, tried)], first_total
    return first, first_total


def specimen_shape(stated: Mapping[str, Any]) -> Shape | list[Refusal]:
    """The shape of a stated passport, or the refusals of its fan-out and connector leaves.

    The passport's port and fan-out order leaves must not be refused. One cached search per
    passport, shared by the feasibility check and the builder. The feasibility check asks it only of
    a passport within the count ceiling; asked directly of one whose placed ports exceed it, it states
    them as the port leaves' range.
    """
    result = _shape_cached(json.dumps(dict(stated), sort_keys=True))
    return result if isinstance(result, Shape) else list(result)
