"""Interfaces, the fan-out and the types, in closed form.

Notation: P is the component ports in all (`components.py`), I = SR + CS the declared interfaces,
U = `components.unconnected.total`, X = `layers.interfaces` - I the external interface stubs. An
interface's **fan-out** is the component ports naming it.

The checks before generation, each refused at its leaf with a closed class:

- `order`: `interfaces.fan_out.top` is descending; `interfaces.fan_out.max` equals `top[0]`, or 0
  when `top` is empty;
- `fan-out`, at `top` when stated, else at `max`: the fan-outs cannot sum to P (a `top` of fewer
  than eight entries sums to P exactly); a named interface has no port; more interfaces are named
  than I + X; or the ports on stubs and fan-out-1 interfaces number more than the ports no
  connector reaches (refused at `layers.interfaces` when `top` is unstated and stub ports take part);
- `layer-total` at `layers.interfaces` below SR + CS, at `layers.implementation_types` when I > 0
  and no value type exists, and at `layers.base_types` when value types have no base type.

The stated head is `top` (or `max` alone); the remaining ports go to the fewest interfaces beyond
it, split as evenly as possible (or paired where the even split breaks the odd-fan-out bound,
`paired_split`; neither fitting is `fan-out`). Of all fan-outs the X smallest go to the stubs, and
no connector reaches a stub port. Interfaces are declared SR (one data element `D1`), then CS (one
operation `O1` with one `in` argument `A1`), then the stubs (`kind: external_interface`,
`external_dest: CLIENT-SERVER-INTERFACE`, `external_ref: /External/Interfaces/<name>`). Value types
`Vt1`, ... go round-robin to the data elements and arguments, base types `Bt1`, ... (8, 16 and 32
bits) to the value types.

The checks here are necessary conditions on the fan-out alone; whether every port can be connected
is decided by `connectors.py`. A built fan-out that mismatches `max`, `top` or P is a
`GenerationError`, never a refusal.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..errors import GenerationError
from ..model import (
    AnyInterface,
    Argument,
    BaseType,
    DataElement,
    DataType,
    ExternalInterface,
    Interface,
    Operation,
    ValueType,
)
from ..passport import TOP, Refusal
from .common import ceil_div, stated_connector_count
from .delegations import delegation_count
from .vocabulary import (
    ASSEMBLY,
    CLIENT_SERVER,
    FAN_OUT,
    FAN_OUT_MAX,
    FAN_OUT_TOP,
    LAYER_BASE,
    LAYER_CONNECTORS,
    LAYER_IMPLEMENTATION,
    LAYER_INTERFACES,
    LAYER_TOTAL,
    ORDER,
    SENDER_RECEIVER,
    UNCONNECTED_TOTAL,
)

#: The numbers of interfaces beyond the stated head the shape tries for one port total.
TAIL_TRIED = 8

#: The DEST every external interface stub names.
STUB_DEST = "CLIENT-SERVER-INTERFACE"
#: The AUTOSAR package a stub's `external_ref` lies in (none of SWCraft's root package names).
STUB_PACKAGE = "/External/Interfaces"

#: The base types' sizes and native declarations, cycled.
BASE_SHAPES = ((8, "uint8_t"), (16, "uint16_t"), (32, "uint32_t"))


@dataclass(frozen=True)
class InterfacePlan:
    """The interfaces, the types and the fan-outs: `interfaces[i]` holds `fan_outs[i]` ports."""

    interfaces: tuple[AnyInterface, ...]
    data_types: tuple[DataType, ...]
    fan_outs: tuple[int, ...]


def _declared(stated: Mapping[str, Any]) -> int:
    """I = SR + CS, the interfaces a connector may name."""
    return int(stated[SENDER_RECEIVER]) + int(stated[CLIENT_SERVER])


def _layer(stated: Mapping[str, Any]) -> int:
    """The interfaces in all, SR + CS + X stubs, never below I (a lower layer is refused apart)."""
    return max(int(stated[LAYER_INTERFACES]), _declared(stated))


def stub_count(stated: Mapping[str, Any]) -> int:
    """X = `layers.interfaces` - SR - CS, the external interface stubs; 0 below SR + CS."""
    return _layer(stated) - _declared(stated)


def connectable_fan_outs(stated: Mapping[str, Any], fan_outs: Sequence[int]) -> tuple[int, ...]:
    """The fan-outs a connector may name: the first I; the stubs hold the rest and no connector."""
    return tuple(fan_outs[: _declared(stated)])


def forced_ports(fan_outs: Sequence[int], declared: int) -> tuple[int, int]:
    """The ports a descending fan-out list forces unconnected, as (stub ports, fan-out-1 ports)."""
    return sum(fan_outs[declared:]), list(fan_outs[:declared]).count(1)


def forced_sentence(stubs: int, ones: int, unconnected: int, delegated: int) -> str:
    """The `fan-out` sentence of more forced ports than U and `delegated` can take."""
    room = (
        f"unconnected.total states {unconnected}"
        if not delegated
        else (f"unconnected.total states {unconnected}, and at most {delegated} more are reached by a delegation alone")
    )
    if not stubs:
        return f"needs {ones} interfaces of fan-out 1, each a port no connector reaches; {room}"
    return (
        f"needs {stubs + ones} ports on external interface stubs and fan-out-1 interfaces "
        f"({stubs} on stubs), each a port no connector reaches; {room}"
    )


def forced_leaf(stated: Mapping[str, Any], stubs: int) -> str:
    """The leaf a forced-unconnected refusal names: `top`, else `layers.interfaces` with stubs, else `max`."""
    if FAN_OUT_TOP in stated:
        return FAN_OUT_TOP
    return LAYER_INTERFACES if stubs else FAN_OUT_MAX


def _even_split(total: int, parts: int) -> list[int]:
    per, extra = divmod(total, parts) if parts else (0, 0)
    return [per + 1] * extra + [per] * (parts - extra)


def paired_split(total: int, parts: int, most: int) -> list[int] | None:
    """`total` ports in pairs split evenly over `parts` entries, descending, the odd port on the last.

    Returns `None` when an entry would exceed `most` or hold no port.
    """
    if parts <= 0:
        return None
    values = [2 * pairs for pairs in _even_split(total // 2, parts)]
    values[-1] += total % 2
    values.sort(reverse=True)
    if values[0] > most or values[-1] < 1:
        return None
    return values


def _connector_count(stated: Mapping[str, Any]) -> int | None:
    """A, when stated (`common.stated_connector_count`, the one definition of it)."""
    assembly, layer = stated.get(ASSEMBLY), stated.get(LAYER_CONNECTORS)
    return stated_connector_count(None if assembly is None else int(assembly), None if layer is None else int(layer))


def odd_fan_out_room(stated: Mapping[str, Any], ports: int) -> int | None:
    """The most interfaces of odd fan-out `ports` admit, E + U + (R - R_c) at the least R_c.

    E = 2A - R_c. Returns `None` when A is unstated or no R_c is admitted. A necessary bound only.
    """
    count = _connector_count(stated)
    return None if count is None else _odd_room(stated, ports, count)


def _odd_room(stated: Mapping[str, Any], ports: int, count: int) -> int | None:
    """`odd_fan_out_room` with A = `count` stated."""
    unconnected = int(stated[UNCONNECTED_TOTAL])
    reached = ports - unconnected
    if reached < 0:
        return None
    delegated = min(delegation_count(stated), reached)
    least = max(reached - delegated, count + 1 if count else 0)
    if least > reached:
        return None
    return max(0, 2 * count - least) + unconnected + reached - least


def parity_sentence(odd: int, ports: int, room: int, count: int) -> str:
    """The `fan-out` sentence of more interfaces of odd fan-out than the parity bound admits."""
    return (
        f"needs {odd} interfaces of odd fan-out for {ports} ports; an interface of odd fan-out "
        "needs a port no connector reaches or an extra require port of a star, and "
        f"components.unconnected.total, connectors.delegation and the {count} connectors leave "
        f"at most {room}"
    )


def _parity_excess(stated: Mapping[str, Any], ports: int, values: Sequence[int]) -> str | None:
    """The parity `fan-out` sentence when `values` break `odd_fan_out_room`, else `None`."""
    room = odd_fan_out_room(stated, ports)
    odd = _odd(values)
    count = _connector_count(stated)
    if room is None or count is None or odd <= room:
        return None
    return parity_sentence(odd, ports, room, count)


def _order_refusals(stated: Mapping[str, Any]) -> list[Refusal]:
    top = stated.get(FAN_OUT_TOP)
    if top is None:
        return []
    if any(x < y for x, y in zip(top, top[1:], strict=False)):
        return [Refusal(FAN_OUT_TOP, ORDER, "is not descending")]
    first = top[0] if top else 0
    if stated[FAN_OUT_MAX] != first:
        return [
            Refusal(
                FAN_OUT_MAX,
                ORDER,
                f"states {stated[FAN_OUT_MAX]}; the first entry of top is {first}",
            )
        ]
    return []


def _head(stated: Mapping[str, Any]) -> tuple[int, ...]:
    """The stated head of the fan-outs: `top`, or `max` alone when `top` is unstated."""
    top = stated.get(FAN_OUT_TOP)
    return (int(stated[FAN_OUT_MAX]),) if top is None else tuple(int(value) for value in top)


def fan_out_bounds(stated: Mapping[str, Any]) -> tuple[int, int]:
    """The least and the most component ports the stated fan-out leaves admit."""
    head = _head(stated)
    top = stated.get(FAN_OUT_TOP)
    if not head or head[0] == 0:
        return 0, 0
    if top is not None and len(top) < TOP:
        return sum(head), sum(head)
    return sum(head), sum(head) + max(0, _layer(stated) - len(head)) * head[-1]


def fan_out_capacity(stated: Mapping[str, Any], ports: int) -> int:
    """The most connectors `ports` component ports admit, A <= sum(f_i - 1).

    It never falls as P grows, so at the most total the leaves admit it bounds A for every total.
    """
    head = _head(stated)
    if not head or head[-1] == 0:
        return 0
    top = stated.get(FAN_OUT_TOP)
    rest = max(0, ports - sum(head))
    if top is not None and len(top) < TOP:
        return sum(head) - len(head)
    return ports - len(head) - ceil_div(rest, head[-1])


def fan_out_forced(stated: Mapping[str, Any], low: int, high: int) -> tuple[int, int]:
    """The fewest forced-unconnected ports for a total in [`low`, `high`], as (stub, fan-out-1) ports.

    A rest of exactly one beyond the head forces a fan-out-1 interface only when the totals admit
    no other.
    """
    head = _head(stated)
    if not head or head[-1] == 0:
        return 0, 0
    declared = _declared(stated)
    stubs, ones = forced_ports(head, declared)
    top = stated.get(FAN_OUT_TOP)
    if top is not None and len(top) < TOP:
        return stubs, ones
    rest = max(0, low - sum(head))
    room = max(0, declared - len(head))
    if head[-1] == 1:
        beyond = min(rest, room)
        return stubs + rest - beyond, ones + beyond
    beyond = max(0, rest - room * head[-1])
    if beyond:
        return stubs + beyond, ones
    return stubs, ones + (1 if low == high == sum(head) + 1 else 0)


def tail_parts(stated: Mapping[str, Any], ports: int) -> list[int]:
    """The numbers of interfaces beyond the head worth trying for *ports* ports, the fewest first.

    With R the ports the head leaves and c the head's last fan-out: up to `TAIL_TRIED` numbers spread
    evenly from ceil(R / c) to the most (R, or the interfaces the layer leaves beyond the head, whichever
    is fewer), or ceil(R / c) alone when the most is not above it. Empty when the head is empty (a `top`
    of no entries), its last fan-out is 0, or it takes every port.
    """
    head = _head(stated)
    rest = ports - sum(head)
    if not head or head[-1] == 0 or rest <= 0:
        return []
    low = ceil_div(rest, head[-1])
    high = min(rest, _layer(stated) - len(head))
    if high <= low:
        return [low]
    span = high - low
    if span < TAIL_TRIED:
        return list(range(low, high + 1))
    return sorted({low + (span * step) // (TAIL_TRIED - 1) for step in range(TAIL_TRIED)})


def _stub_ports(rest: int, parts: int, room: int, most: int) -> int:
    """The ports the `parts` - `room` stubs take: one each, or what the slots cannot hold."""
    return min(rest, max(parts - room, rest - room * most))


def _tail(rest: int, parts: int, room: int, most: int) -> list[int]:
    """`rest` ports over `parts` interfaces, the first `room` SR + CS slots, the rest stubs; descending."""
    if parts <= room:
        return _even_split(rest, parts)
    stub_ports = _stub_ports(rest, parts, room, most)
    return [*_even_split(rest - stub_ports, room), *_even_split(stub_ports, parts - room)]


def _paired_tail(rest: int, parts: int, room: int, most: int) -> list[int] | None:
    """`_tail` with the slots' ports split in pairs; `None` when that split does not fit."""
    if parts <= room:
        return paired_split(rest, parts, most)
    stub_ports = _stub_ports(rest, parts, room, most)
    slots = paired_split(rest - stub_ports, room, most)
    if slots is None:
        return None
    return [*slots, *_even_split(stub_ports, parts - room)]


def split_fan_outs(stated: Mapping[str, Any], ports: int, parts: int | None = None) -> tuple[int, ...] | Refusal:
    """The named interfaces' fan-outs, descending, or the `fan-out` refusal.

    `parts` is the number of interfaces beyond the head (the fewest when `None`). The first I
    values are the SR + CS interfaces', the rest the stubs'.
    """
    top = stated.get(FAN_OUT_TOP)
    fan_max = int(stated[FAN_OUT_MAX])
    leaf = FAN_OUT_MAX if top is None else FAN_OUT_TOP

    def refuse(sentence: str) -> Refusal:
        return Refusal(leaf, FAN_OUT, sentence)

    head = list(_head(stated))
    if top is not None and 0 in head:
        return refuse("states an interface named by no port")
    if ports == 0:
        if fan_max:
            return refuse(f"states a fan-out of {fan_max}; the components hold no port")
        return ()
    if fan_max == 0:
        return refuse(f"states no fan-out; the components hold {ports} ports")
    rest = ports - sum(head)
    if rest < 0 or (top is not None and len(top) < TOP and rest != 0):
        return refuse(f"sums to {sum(head)}; the fan-outs sum to the {ports} component ports")
    if parts is None:
        parts = ceil_div(rest, head[-1])
    declared = _declared(stated)
    slots = max(0, declared - len(head))
    values = tuple(sorted((*head, *_tail(rest, parts, slots, head[-1])), reverse=True))
    # An even split with more odd fan-outs than the parity bound admits cannot build.
    parity = _parity_excess(stated, ports, values)
    if parity is not None:
        paired = _paired_tail(rest, parts, slots, head[-1])
        if paired is not None:
            values = tuple(sorted((*head, *paired), reverse=True))
            parity = _parity_excess(stated, ports, values)
    layer = _layer(stated)
    if len(values) > layer:
        return refuse(f"needs {len(values)} named interfaces for {ports} ports; {_declares(stated)}")
    stubs, ones = forced_ports(values, declared)
    unconnected = int(stated[UNCONNECTED_TOTAL])
    # The R - R_c ports reached by delegation alone are at most D and at most R.
    delegated = min(delegation_count(stated), max(0, ports - unconnected))
    if stubs + ones > unconnected + delegated:
        return Refusal(
            forced_leaf(stated, stubs),
            FAN_OUT,
            forced_sentence(stubs, ones, unconnected, delegated),
        )
    if parity is not None:
        return refuse(parity)
    return values


def _odd(values: Sequence[int]) -> int:
    return sum(1 for value in values if value % 2)


def _layer_refusals(stated: Mapping[str, Any]) -> list[Refusal]:
    refusals: list[Refusal] = []
    declared = _declared(stated)
    layer = int(stated[LAYER_INTERFACES])
    if layer < declared:
        refusals.append(
            Refusal(
                LAYER_INTERFACES,
                LAYER_TOTAL,
                f"states {layer}, below the {declared} sender_receiver and client_server interfaces",
            )
        )
    value_types = int(stated[LAYER_IMPLEMENTATION])
    if declared and not value_types:
        refusals.append(
            Refusal(
                LAYER_IMPLEMENTATION,
                LAYER_TOTAL,
                f"states 0; the {declared} interfaces need a value type",
            )
        )
    if value_types and not int(stated[LAYER_BASE]):
        refusals.append(Refusal(LAYER_BASE, LAYER_TOTAL, f"states 0; the {value_types} value types need one"))
    return refusals


def _declares(stated: Mapping[str, Any]) -> str:
    """Which leaf bounds the named interfaces, as a refusal sentence states it."""
    declared, layer = _declared(stated), _layer(stated)
    if layer == declared:
        return f"sender_receiver and client_server declare {declared}"
    return f"{LAYER_INTERFACES} states {layer}"


def _named_refusals(stated: Mapping[str, Any]) -> list[Refusal]:
    """The `fan-out` conflicts of the fan-out leaves with the declared interfaces, whatever the ports."""
    top = stated.get(FAN_OUT_TOP)
    leaf = FAN_OUT_MAX if top is None else FAN_OUT_TOP
    head = _head(stated)
    if top is not None and 0 in head:
        return [Refusal(leaf, FAN_OUT, "states an interface named by no port")]
    named = len(head) if head and head[0] else 0
    if named > _layer(stated):
        return [Refusal(leaf, FAN_OUT, f"names {named} interfaces; {_declares(stated)}")]
    return []


def interface_refusals(stated: Mapping[str, Any]) -> list[Refusal]:
    """The interface and type refusals the stated leaves decide alone.

    The fan-out against the component ports is decided in `shape.py`. A fan-out list out of order
    is refused for that alone.
    """
    order = _order_refusals(stated)
    return [*_layer_refusals(stated), *(order or _named_refusals(stated))]


def _base_shape(index: int) -> tuple[int, str, str]:
    size, native = BASE_SHAPES[index % len(BASE_SHAPES)]
    return size, "NONE", native


def _names(names: Mapping[str, Sequence[str]], layer: str, count: int) -> Sequence[str]:
    """The layer's names from the identifier plan."""
    if len(names[layer]) != count:
        raise GenerationError(f"the identifier plan names {len(names[layer])} {layer}, not {count}")
    return names[layer]


def _types(stated: Mapping[str, Any], names: Mapping[str, Sequence[str]]) -> tuple[list[ValueType], list[BaseType]]:
    base_names = _names(names, "base_types", int(stated[LAYER_BASE]))
    value_names = _names(names, "implementation_types", int(stated[LAYER_IMPLEMENTATION]))
    bases = [BaseType(name, *_base_shape(index)) for index, name in enumerate(base_names)]
    values = [ValueType(name, bases[index % len(bases)].name) for index, name in enumerate(value_names)]
    return values, bases


def _interfaces(
    stated: Mapping[str, Any],
    value_types: Sequence[ValueType],
    names: Mapping[str, Sequence[str]],
) -> list[AnyInterface]:
    sr = int(stated[SENDER_RECEIVER])
    declared = _declared(stated)
    interface_names = _names(names, "interfaces", _layer(stated))
    interfaces: list[AnyInterface] = []
    for index, name in enumerate(interface_names):
        if index >= declared:
            interfaces.append(ExternalInterface(name, f"{STUB_PACKAGE}/{name}", STUB_DEST))
            continue
        typed = value_types[index % len(value_types)].name
        if index < sr:
            interfaces.append(Interface(name, "sender_receiver", data_elements=(DataElement("D1", typed),)))
        else:
            interfaces.append(
                Interface(
                    name,
                    "client_server",
                    operations=(Operation("O1", (Argument("A1", "in", typed),)),),
                )
            )
    return interfaces


def build_interfaces(
    stated: Mapping[str, Any],
    fan_outs: Sequence[int],
    names: Mapping[str, Sequence[str]],
) -> InterfacePlan:
    """The interfaces and the types of a feasible stated passport, with the shape's fan-outs.

    The interfaces are SR, CS, then the stubs; `interfaces[i]` holds `fan_outs[i]` ports.
    """
    values = sorted(fan_outs, reverse=True)
    top = stated.get(FAN_OUT_TOP)
    if (values[0] if values else 0) != stated[FAN_OUT_MAX] or (top is not None and values[:TOP] != list(top)):
        raise GenerationError("the fan-outs do not reproduce the stated fan-out leaves")
    value_types, base_types = _types(stated, names)
    interfaces = _interfaces(stated, value_types, names)
    return InterfacePlan(tuple(interfaces), (*base_types, *value_types), tuple(fan_outs))
