"""Identifiers, exact per layer, within each layer's length limit.

Each element is named by a neutral stem (`Swc`, `If`, `Conn`, `Vt`, `Bt`, ...; the root composition
is `Root`) and an index zero-padded to the digits of its kind's count, padded with a deterministic
lowercase filler to its length. The stem and the index are an element's **stem minimum**; one index
width per layer keeps the names unique ignoring case. Port, runnable, data element and member names
are not passport identifiers and stay short (`P1`, `R1`, `D1`, `M0001`).

Per layer, with n_L elements, a stated maximum x and mean mu: one element has length x, every other
lies between its stem minimum and min(x, its limit), and the total T satisfies round(T / n_L, 3) = mu,
the mean SWCraft writes (the total nearest mu x n_L, spread as evenly as the bounds allow). An
unstated layer keeps its stem minima; an empty layer states 0 and 0. The overall maximum
`identifiers.max_length` is carried by one element of the first layer that can carry it.

The limits are 128, the AUTOSAR `Identifier` maximum, less the longest suffix SWCraft's writer
derives from that layer's names: `swc` 111 (`{swc}_InternalBehavior`), sender-receiver interfaces
120 (`{Interface}_{Element}_Init` with the data element `D1`), 128 for every other layer.
`SPECIMEN_LIMITS` is the table `model.validate_project` holds a specimen to.

The refusals before generation, each at its leaf:

- `identifier-length` at `identifiers.<layer>.max_length`: a maximum above the layer's limit, below
  an element's stem minimum, or other than 0 for an empty layer;
- `identifier-mean` at `identifiers.<layer>.mean_length`: no total meets the mean within the bounds,
  or a mean other than 0 for an empty layer;
- `identifier-length` at `identifiers.max_length`: below a layer's longest name, or above it with no
  layer to carry it; checked only when no per-layer leaf is refused.

A specimen's names meet the passport's lengths and nothing else of a real model's names.
"""

from __future__ import annotations

import functools
import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..errors import GenerationError
from ..names import MAX_IDENTIFIER_LENGTH
from ..passport import PASSPORT_LAYERS, Refusal
from .common import COUNT_CEILING, ceil_div, level_fill, link_bounds, stated_count
from .components import port_range, port_spec
from .compositions import composition_count
from .hardware import HardwareNames, hardware_spec
from .interfaces import fan_out_bounds, stub_count
from .leaves import MET_IDENTIFIER_LAYERS
from .tasks import sequence_count
from .types import TYPE_LAYERS, TYPE_STEMS, type_count
from .vocabulary import (
    CLIENT_SERVER,
    CONNECTOR_MAX_LENGTH,
    CONNECTOR_MEAN_LENGTH,
    COUNT_DECIDED,
    IDENTIFIER_LENGTH,
    IDENTIFIER_MEAN,
    LAYER_BASE,
    LAYER_IMPLEMENTATION,
    LAYER_REQUIREMENTS,
    LAYER_SWC,
    LAYER_TASKS,
    LINKS,
    MAX_LENGTH,
    MAX_ON_ONE,
    SENDER_RECEIVER,
    UNCONNECTED_TOTAL,
)

#: The data element every specimen sender-receiver interface holds (`interfaces.py`).
DATA_ELEMENT = "D1"
INTERNAL_BEHAVIOR_SUFFIX = "_InternalBehavior"
INIT_CONSTANT_SUFFIX = "_Init"

#: The per-layer limits, keyed as `model.validate_project` reads them.
SPECIMEN_LIMITS: dict[str, int] = {
    "swc": MAX_IDENTIFIER_LENGTH - len(INTERNAL_BEHAVIOR_SUFFIX),
    "interfaces.sender_receiver": (MAX_IDENTIFIER_LENGTH - len(INIT_CONSTANT_SUFFIX) - len("_") - len(DATA_ELEMENT)),
    "interfaces.client_server": MAX_IDENTIFIER_LENGTH,
    "connectors": MAX_IDENTIFIER_LENGTH,
    "implementation_types": MAX_IDENTIFIER_LENGTH,
    "base_types": MAX_IDENTIFIER_LENGTH,
    "interfaces.external": MAX_IDENTIFIER_LENGTH,
    "hardware": MAX_IDENTIFIER_LENGTH,
    "compositions": MAX_IDENTIFIER_LENGTH,
    **{layer: MAX_IDENTIFIER_LENGTH for layer in TYPE_LAYERS},
    "requirements": MAX_IDENTIFIER_LENGTH,
    "tasks": MAX_IDENTIFIER_LENGTH,
    "sequences": MAX_IDENTIFIER_LENGTH,
}

ROOT_COMPOSITION = "Root"
COMPOSITION_STEM = "Cmp"
CONNECTOR_STEM = "Conn"
TASK_STEM = "Ts"
SEQUENCE_STEM = "Sq"
REQUIREMENT_STEM = "Rq"
FILLER = "abcdefghijklmnopqrstuvwxyz"

#: The met identifier layers in the registry order: the carrier of the overall maximum is the
#: first of them whose lengths are unstated.
LAYERS = tuple(layer for layer in PASSPORT_LAYERS if layer in MET_IDENTIFIER_LAYERS)


@dataclass(frozen=True)
class IdentifierCounts:
    """The element counts the names are made for, per kind."""

    components: int
    sender_receiver: int
    client_server: int
    connectors: int
    implementation_types: int
    base_types: int
    cores: int
    memories: int
    partitions: int
    #: The external interface stubs, after the CS interfaces.
    stubs: int
    #: The compositions, the root included.
    compositions: int = 1
    application_types: int = 0
    mapping_sets: int = 0
    units: int = 0
    compu_methods: int = 0
    data_constraints: int = 0
    #: The tasks: busy, then idle.
    tasks: int = 0
    sequences: int = 0
    requirements: int = 0


@dataclass(frozen=True)
class _Slot:
    """One element: its least name (stem and index) and the longest its layer admits."""

    prefix: str
    limit: int

    @property
    def minimum(self) -> int:
        return len(self.prefix)


def identifier_counts(stated: Mapping[str, Any], connectors: int) -> IdentifierCounts:
    """The element counts of a stated passport; the connectors are the construction's."""
    hardware = hardware_spec(stated)
    return IdentifierCounts(
        components=int(stated[LAYER_SWC]),
        sender_receiver=int(stated[SENDER_RECEIVER]),
        client_server=int(stated[CLIENT_SERVER]),
        connectors=connectors,
        implementation_types=int(stated[LAYER_IMPLEMENTATION]),
        base_types=int(stated[LAYER_BASE]),
        cores=hardware.cores,
        memories=hardware.memories,
        partitions=hardware.partitions,
        stubs=stub_count(stated),
        compositions=composition_count(stated),
        **{layer: type_count(stated, layer) for layer in TYPE_LAYERS},
        tasks=stated_count(stated, LAYER_TASKS),
        sequences=sequence_count(stated),
        requirements=stated_count(stated, LAYER_REQUIREMENTS),
    )


def _index_width(count: int) -> int:
    """The digits of every index of a kind of `count` elements: the digits of the count."""
    return len(str(count))


def _connector_minimum(count: int) -> int:
    """The stem minimum of every name of `count` connectors."""
    return len(CONNECTOR_STEM) + _index_width(count)


def _indexed(stem: str, count: int, limit: int) -> list[_Slot]:
    width = _index_width(count)
    return [_Slot(f"{stem}{index:0{width}d}", limit) for index in range(1, count + 1)]


def _slots(counts: IdentifierCounts) -> dict[str, list[_Slot]]:
    """Every met identifier layer's elements, in the order the builder emits them."""
    # One `If...` index runs over the sender-receiver, client-server and stub interfaces.
    kinds = (
        ["interfaces.sender_receiver"] * counts.sender_receiver
        + ["interfaces.client_server"] * counts.client_server
        + ["interfaces.external"] * counts.stubs
    )
    interface_slots = [
        _Slot(slot.prefix, SPECIMEN_LIMITS[key]) for key, slot in zip(kinds, _indexed("If", len(kinds), 0), strict=True)
    ]
    hardware = SPECIMEN_LIMITS["hardware"]
    return {
        "swc": _indexed("Swc", counts.components, SPECIMEN_LIMITS["swc"]),
        "interfaces": interface_slots,
        "connectors": _indexed(CONNECTOR_STEM, counts.connectors, SPECIMEN_LIMITS["connectors"]),
        "implementation_types": _indexed("Vt", counts.implementation_types, SPECIMEN_LIMITS["implementation_types"]),
        "base_types": _indexed("Bt", counts.base_types, SPECIMEN_LIMITS["base_types"]),
        "compositions": _composition_slots(counts.compositions),
        "hardware": [slot for stem, count in _hardware_kinds(counts) for slot in _indexed(stem, count, hardware)],
        **{layer: _indexed(TYPE_STEMS[layer], getattr(counts, layer), SPECIMEN_LIMITS[layer]) for layer in TYPE_LAYERS},
        "tasks": _indexed(TASK_STEM, counts.tasks, SPECIMEN_LIMITS["tasks"]),
        "sequences": _indexed(SEQUENCE_STEM, counts.sequences, SPECIMEN_LIMITS["sequences"]),
        "requirements": _indexed(REQUIREMENT_STEM, counts.requirements, SPECIMEN_LIMITS["requirements"]),
    }


def _composition_slots(count: int) -> list[_Slot]:
    """`Root`, then the chain's `count - 1` nested compositions `Cmp...`; none for no count."""
    limit = SPECIMEN_LIMITS["compositions"]
    if count < 1:
        return []
    return [_Slot(ROOT_COMPOSITION, limit), *_indexed(COMPOSITION_STEM, count - 1, limit)]


def _hardware_kinds(counts: IdentifierCounts) -> tuple[tuple[str, int], ...]:
    """The hardware kinds' stems and counts, in the order their names are emitted.

    The processor, the cores, the memories, the partitions.
    """
    return (
        ("Cp", 1),
        ("Co", counts.cores),
        ("Me", counts.memories),
        ("Pa", counts.partitions),
    )


def hardware_names_by_kind(names: Mapping[str, Sequence[str]], counts: IdentifierCounts) -> HardwareNames:
    """The hardware layer of `identifier_names` per kind (`_hardware_kinds`' order)."""
    layer = list(names["hardware"])
    split: list[tuple[str, ...]] = []
    for _, count in _hardware_kinds(counts):
        split.append(tuple(layer[:count]))
        layer = layer[count:]
    if layer or len(split[0]) != 1:
        raise GenerationError(f"the identifier plan names {len(names['hardware'])} hardware elements")
    (processor,), cores, memories, partitions = split
    return HardwareNames(processor, cores, memories, partitions)


def _mean_of(value: float) -> float:
    """A stated mean as a float, or the integer when it is too large for one.

    A schema-valid integer too large for a float (10**400) stays the integer, which `_total`
    compares exactly and no total meets (`float()` would overflow).
    """
    try:
        return float(value)
    except OverflowError:
        return value


def _total(n: int, mean: float, low: int, high: int) -> int | None:
    """The total T in [low, high] with round(T / n, 3) == mean, nearest mean x n; else `None`."""
    if low > high or not low / n - 0.001 <= mean <= high / n + 0.001:
        # A mean outside the totals' means needs none of them, and a schema-valid mean as
        # large as 1e308 would overflow the window below.
        return None
    lo = max(low, math.floor(n * (mean - 0.0005)) - 1)
    hi = min(high, math.ceil(n * (mean + 0.0005)) + 1)
    totals = [t for t in range(lo, hi + 1) if round(t / n, 3) == mean]
    if not totals:
        return None
    return min(totals, key=lambda t: (abs(t - mean * n), t))


def _layer_lengths(
    layer: str,
    slots: Sequence[_Slot],
    x: int | None,
    mean: float | None,
    count_chosen: bool = False,
) -> tuple[list[int], list[Refusal]]:
    """One layer's lengths for a maximum x and a mean (each `None` when unstated).

    Returns the lengths and no refusal, or no lengths and the layer's refusals. With `count_chosen`
    (the construction chose the connector count), a refused mean names no count.
    """
    max_leaf = f"identifiers.{layer}.max_length"
    mean_leaf = f"identifiers.{layer}.mean_length"
    n = len(slots)
    if n == 0:
        refusals: list[Refusal] = []
        if x:
            refusals.append(Refusal(max_leaf, IDENTIFIER_LENGTH, f"states {x}; the layer has no element"))
        if mean:
            refusals.append(Refusal(mean_leaf, IDENTIFIER_MEAN, f"states {mean}; the layer has no element"))
        return [], refusals
    minima = [slot.minimum for slot in slots]
    limits = [slot.limit for slot in slots]
    if x is None:
        if mean is None:
            return minima, []
        total = _total(n, _mean_of(mean), sum(minima), sum(limits))
        if total is None:
            sentence = (
                _no_count_meets(mean, None)
                if count_chosen
                else f"states {mean}; the {n} names hold between {sum(minima)} and "
                f"{sum(limits)} characters, and no total in between has that mean"
            )
            return [], [Refusal(mean_leaf, IDENTIFIER_MEAN, sentence)]
        return level_fill(minima, limits, total), []
    if x > max(limits):
        sentence = (
            f"states {x}; the layer's names are at most {max(limits)} characters, room for "
            "the names SWCraft derives from them"
        )
        return [], [Refusal(max_leaf, IDENTIFIER_LENGTH, sentence)]
    if x < max(minima):
        sentence = f"states {x}; the layer's least names are {max(minima)} characters"
        return [], [Refusal(max_leaf, IDENTIFIER_LENGTH, sentence)]
    # The longest least name at x gives the least lower bound of T; the upper bound is the same
    # for every choice.
    carrier = max(
        (index for index, limit in enumerate(limits) if limit >= x),
        key=lambda index: (minima[index], -index),
    )
    lows = [x if index == carrier else low for index, low in enumerate(minima)]
    highs = [x if index == carrier else min(x, high) for index, high in enumerate(limits)]
    if mean is None:
        return lows, []
    total = _total(n, _mean_of(mean), sum(lows), sum(highs))
    if total is None:
        sentence = (
            _no_count_meets(mean, x)
            if count_chosen
            else f"states {mean}; with the longest of the {n} names at {x}, they hold between "
            f"{sum(lows)} and {sum(highs)} characters, and no total in between has that mean"
        )
        return [], [Refusal(mean_leaf, IDENTIFIER_MEAN, sentence)]
    return level_fill(lows, highs, total), []


def _no_count_meets(mean: float, x: int | None) -> str:
    longest = "" if x is None else f", with the longest at {x},"
    return (
        f"states {mean}; at no connector count the construction could choose do the "
        f"names{longest} hold a total length with that mean"
    )


def _stated(stated: Mapping[str, Any], layer: str, leaf: str) -> Any:
    return stated.get(f"identifiers.{layer}.{leaf}")


def _carry(
    stated: Mapping[str, Any],
    slots: Mapping[str, Sequence[_Slot]],
    lengths: dict[str, list[int]],
    overall: int,
) -> bool:
    """Give one element the overall maximum.

    Of the first layer in the registry order whose lengths are wholly unstated, that has elements
    and whose limit admits it; failing that, of the first whose maximum is unstated and whose
    stated mean still holds with one name at it.
    """
    for layer in lengths:
        if _stated(stated, layer, "max_length") is not None:
            continue
        if _stated(stated, layer, "mean_length") is not None:
            continue
        index = next((i for i, slot in enumerate(slots[layer]) if slot.limit >= overall), None)
        if index is not None:
            lengths[layer][index] = overall
            return True
    for layer in lengths:
        mean = _stated(stated, layer, "mean_length")
        if _stated(stated, layer, "max_length") is not None or mean is None:
            continue
        if not any(slot.limit >= overall for slot in slots[layer]):
            continue
        carried, refusals = _layer_lengths(layer, slots[layer], overall, mean)
        if not refusals:
            lengths[layer] = carried
            return True
    return False


#: A refusal as the connector count filter compares them: its leaf and its class.
RefusalKey = tuple[str, str]


def _connector_layer(count: int, x: int | None, mean: float | None) -> tuple[int, frozenset[RefusalKey]]:
    """The longest of `count` connector names under x and mean, and the leaves refused at that count.

    Closed form: every connector name has the same stem minimum and limit, so the level fill of a
    total rises to the total over the count, rounded up.
    """
    if count == 0:
        refused = set()
        if x:
            refused.add((CONNECTOR_MAX_LENGTH, IDENTIFIER_LENGTH))
        if mean:
            refused.add((CONNECTOR_MEAN_LENGTH, IDENTIFIER_MEAN))
        return 0, frozenset(refused)
    minimum = _connector_minimum(count)
    limit = SPECIMEN_LIMITS["connectors"]
    if x is None:
        if mean is None:
            return minimum, frozenset()
        total = _total(count, _mean_of(mean), count * minimum, count * limit)
        if total is None:
            return 0, frozenset({(CONNECTOR_MEAN_LENGTH, IDENTIFIER_MEAN)})
        return ceil_div(total, count), frozenset()
    if not minimum <= x <= limit:
        return 0, frozenset({(CONNECTOR_MAX_LENGTH, IDENTIFIER_LENGTH)})
    if mean is not None and _total(count, _mean_of(mean), x + (count - 1) * minimum, count * x) is None:
        return 0, frozenset({(CONNECTOR_MEAN_LENGTH, IDENTIFIER_MEAN)})
    return x, frozenset()


def _connectors_carry(count: int, mean: float | None, overall: int) -> bool:
    """Whether `count` connectors, their maximum unstated, carry the overall maximum (`_carry`).

    One name at it, and with a stated mean the others between their stem minimum and it.
    """
    limit = SPECIMEN_LIMITS["connectors"]
    if count == 0 or overall > limit:
        return False
    if mean is None:
        return True
    minimum = _connector_minimum(count)
    return _total(count, _mean_of(mean), overall + (count - 1) * minimum, count * overall) is not None


def _refusals_at(
    count: int,
    x: int | None,
    mean: float | None,
    overall: int | None,
    others_longest: int,
    others_carry: bool,
) -> frozenset[RefusalKey]:
    """The connector and overall-maximum refusals at `count` connectors, other layers fixed.

    Uncached for the domain scans; `_refused_at` caches single counts.
    """
    longest, refused = _connector_layer(count, x, mean)
    if refused or overall is None:
        return refused
    reach = max(longest, others_longest)
    if overall < reach:
        return frozenset({(MAX_LENGTH, IDENTIFIER_LENGTH)})
    if overall > reach and not (others_carry or (x is None and _connectors_carry(count, mean, overall))):
        return frozenset({(MAX_LENGTH, IDENTIFIER_LENGTH)})
    return frozenset()


_refused_at = functools.lru_cache(maxsize=256)(_refusals_at)


@dataclass(frozen=True)
class _Scan:
    """The identifier check over a domain: some count passes, some fails, the refusals all give."""

    clean: bool
    refused: bool
    common: frozenset[RefusalKey]


@functools.lru_cache(maxsize=256)
def _scan(
    zero: bool,
    domain: range,
    x: int | None,
    mean: float | None,
    overall: int | None,
    others_longest: int,
    others_carry: bool,
) -> _Scan:
    """The `_Scan` of the counts of `domain`, and 0 with `zero`."""
    clean = refused = False
    common: frozenset[RefusalKey] | None = None
    for count in itertools.chain((0,) if zero else (), domain):
        keys = _refusals_at(count, x, mean, overall, others_longest, others_carry)
        clean = clean or not keys
        refused = refused or bool(keys)
        common = keys if common is None else common & keys
        if clean and refused:
            break
    return _Scan(clean, refused, common or frozenset())


@functools.lru_cache(maxsize=64)
def _passing(
    zero: bool,
    domain: range,
    x: int | None,
    mean: float | None,
    overall: int | None,
    others_longest: int,
    others_carry: bool,
) -> tuple[int, ...]:
    """The counts of a domain (0 with `zero`) that pass the identifier check, ascending."""
    return tuple(
        count
        for count in itertools.chain((0,) if zero else (), domain)
        if not _refusals_at(count, x, mean, overall, others_longest, others_carry)
    )


@dataclass(frozen=True)
class ConnectorNames:
    """What the identifier check asks of a connector count the construction chooses (unstated A).

    The stated connector maximum and mean, and the overall maximum where the connectors decide it.
    The domain (`domain`, and 0 with `zero`): for R reached ports, 0 when R = 0, else ceil(R/2) to
    R - 1, within the port, fan-out, `links` and count-ceiling bounds (`_count_domain`).

    **A refusal is the count's only when some count avoids it.** When some count of the domain
    passes, `admits` holds only for the passing counts. When none does, the conflict is the stated
    leaves' own: the identifier check refuses it at the chosen count, and `admits` keeps the counts
    giving exactly the refusals common to all (or every count when none is common). With another
    layer's leaf refused, `overall` is `None` and the overall maximum is not checked.

    What this does not reach: the domain is closed-form bounds; the construction refuses more counts
    (star placement, the hubs' and ordinary components' ends). When every passing count is one it
    refuses, the shape builds the fewest count it fits and the identifier check refuses that: with
    its own class when the total is stated, else `derived`.
    """

    max_length: int | None
    mean_length: float | None
    overall: int | None
    others_longest: int
    others_carry: bool
    domain: range
    zero: bool

    def refused_at(self, count: int) -> frozenset[RefusalKey]:
        """The refusals the identifier check gives at `count` connectors (leaf and class)."""
        return _refused_at(
            count,
            self.max_length,
            self.mean_length,
            self.overall,
            self.others_longest,
            self.others_carry,
        )

    def _scanned(self) -> _Scan:
        return _scan(
            self.zero,
            self.domain,
            self.max_length,
            self.mean_length,
            self.overall,
            self.others_longest,
            self.others_carry,
        )

    @property
    def choosable(self) -> tuple[int, ...]:
        """The counts the construction may choose, ascending.

        The passing ones when the check constrains (some count of the domain passes it and some
        fails), else every count (`counts`).
        """
        if self.constrains:
            return _passing(
                self.zero,
                self.domain,
                self.max_length,
                self.mean_length,
                self.overall,
                self.others_longest,
                self.others_carry,
            )
        return self.counts

    @property
    def counts(self) -> tuple[int, ...]:
        """Every count of the domain (0 with `zero`), ascending."""
        return (*((0,) if self.zero else ()), *self.domain)

    def admits(self, count: int) -> bool:
        refused = self.refused_at(count)
        if not refused:
            return True
        scan = self._scanned()
        return not scan.clean and (not scan.common or refused == scan.common)

    def avoidable(self, refusal: Refusal) -> bool:
        """Whether a connector or overall-maximum refusal is the count's: some count passes."""
        return refusal.leaf in COUNT_DECIDED and self._scanned().clean

    @property
    def constrains(self) -> bool:
        """Whether a stated leaf holds the count: some count of the domain passes and some fails."""
        scan = self._scanned()
        return scan.clean and scan.refused

    def stated(self) -> list[str]:
        """The stated leaves the count is held to, as `leaf = value`."""
        named = [
            f"{CONNECTOR_MAX_LENGTH} = {self.max_length}" if self.max_length is not None else "",
            f"{CONNECTOR_MEAN_LENGTH} = {self.mean_length}" if self.mean_length is not None else "",
            f"{MAX_LENGTH} = {self.overall}" if self.overall is not None and self.max_length is None else "",
        ]
        return [item for item in named if item]

    def refusals_without_connectors(self) -> list[Refusal]:
        """The refusals when no port is reached, so no connector, but the stated leaves need one.

        Each stated nonzero connector leaf, as a stated A = 0 has them, else the overall maximum.
        """
        reason = "the components keep no reached port, so there is no connector to name"
        refusals: list[Refusal] = []
        if self.max_length:
            refusals.append(Refusal(CONNECTOR_MAX_LENGTH, IDENTIFIER_LENGTH, f"states {self.max_length}; {reason}"))
        if self.mean_length:
            refusals.append(Refusal(CONNECTOR_MEAN_LENGTH, IDENTIFIER_MEAN, f"states {self.mean_length}; {reason}"))
        if refusals:
            return refusals
        return [
            Refusal(
                MAX_LENGTH,
                IDENTIFIER_LENGTH,
                f"states {self.overall}, above the longest name {self.others_longest} of every "
                f"layer; {reason}, and no other layer whose maximum is unstated can carry it "
                "within its limit and its stated mean",
            )
        ]


def _count_domain(stated: Mapping[str, Any]) -> tuple[range, bool]:
    """The connector counts the stated leaves admit: the positive ones as a range, and whether 0 is one.

    Each bound is a necessary condition of the stated leaves alone: ceil(R/2) to R - 1 for R reached
    ports (0 when R = 0); the largest stated count keeps its ports beyond u1 reached, each with its
    own connector; and `links` bounds A by n, the hubs and L.
    """
    spec = port_spec(stated)
    port_low, port_high = port_range(spec)
    fan_low, fan_high = fan_out_bounds(stated)
    low, high = max(port_low, fan_low), min(port_high, fan_high)
    if low > high:
        return range(0), False
    unconnected = int(stated[UNCONNECTED_TOTAL])
    reached_low, reached_high = low - unconnected, high - unconnected
    kept = max(0, (spec.largest or 0) - int(stated[MAX_ON_ONE]))
    least, most = link_bounds(spec.n, spec.k, int(stated[LINKS]))
    first = max(1, kept, least, ceil_div(max(reached_low, 2), 2))
    last = min(COUNT_CEILING, reached_high - 1)
    if most is not None:
        last = min(last, most)
    zero = reached_low <= 0 <= reached_high and least == 0
    return range(first, last + 1), zero


def connector_names(stated: Mapping[str, Any]) -> ConnectorNames:
    """The `ConnectorNames` of a stated passport; the other layers' lengths are computed once."""
    x = _stated(stated, "connectors", "max_length")
    mean = _stated(stated, "connectors", "mean_length")
    x = None if x is None else int(x)
    mean = None if mean is None else _mean_of(mean)
    overall = stated.get(MAX_LENGTH)
    slots = _slots(identifier_counts(stated, 0))
    others: dict[str, list[int]] = {}
    refused = False
    for layer in LAYERS:
        if layer == "connectors":
            continue
        values, refusals = _layer_lengths(
            layer,
            slots[layer],
            _stated(stated, layer, "max_length"),
            _stated(stated, layer, "mean_length"),
        )
        refused = refused or bool(refusals)
        others[layer] = values
    longest = max((max(values, default=0) for values in others.values()), default=0)
    carry = False
    if overall is not None and not refused:
        overall = int(overall)
        carry = _carry(stated, {layer: slots[layer] for layer in others}, dict(others), overall)
    domain, zero = _count_domain(stated)
    return ConnectorNames(
        max_length=x,
        mean_length=mean,
        overall=None if refused or overall is None else int(overall),
        others_longest=longest,
        others_carry=carry,
        domain=domain,
        zero=zero,
    )


def _plan(
    stated: Mapping[str, Any],
    counts: IdentifierCounts,
    unchecked: frozenset[str] = frozenset(),
    connectors_derived: bool = False,
) -> tuple[dict[str, list[str]], list[Refusal]]:
    slots = _slots(counts)
    lengths: dict[str, list[int]] = {}
    refusals: list[Refusal] = []
    for layer in LAYERS:
        if layer in unchecked:
            continue
        values, refused = _layer_lengths(
            layer,
            slots[layer],
            _stated(stated, layer, "max_length"),
            _stated(stated, layer, "mean_length"),
            count_chosen=connectors_derived and layer == "connectors",
        )
        refusals.extend(refused)
        if not refused:
            lengths[layer] = values
    overall = stated.get(MAX_LENGTH)
    if not refusals and overall is not None and lengths:
        reaches = [(layer, max(values, default=0)) for layer, values in lengths.items()]
        longest_layer, longest = max(reaches, key=lambda item: item[1])
        if connectors_derived:
            # The connector count is the construction's; name a stated layer that also exceeds it.
            stated_reach = [item for item in reaches if item[0] != "connectors"]
            if stated_reach and max(item[1] for item in stated_reach) > overall:
                longest_layer, longest = max(stated_reach, key=lambda item: item[1])
        if overall < longest:
            refusals.append(
                Refusal(
                    MAX_LENGTH,
                    IDENTIFIER_LENGTH,
                    f"states {overall}; the {longest_layer} names reach {longest}",
                )
            )
        elif overall > longest and not unchecked and not _carry(stated, slots, lengths, overall):
            refusals.append(
                Refusal(
                    MAX_LENGTH,
                    IDENTIFIER_LENGTH,
                    f"states {overall}, above the longest name {longest} of every layer, and "
                    "no layer whose maximum is unstated can carry it within its limit and its "
                    "stated mean",
                )
            )
    if refusals or unchecked:
        return {}, refusals
    names = {
        layer: [
            slot.prefix + _filler(length - slot.minimum)
            for slot, length in zip(slots[layer], lengths[layer], strict=True)
        ]
        for layer in LAYERS
    }
    return names, []


def _filler(length: int) -> str:
    return (FILLER * (length // len(FILLER) + 1))[:length]


def identifier_refusals(
    stated: Mapping[str, Any],
    counts: IdentifierCounts,
    unchecked: frozenset[str] = frozenset(),
    connectors_derived: bool = False,
) -> list[Refusal]:
    """Every identifier refusal of a stated passport with these element counts.

    The refusals are `identifier-length`, at a layer's maximum or the overall maximum, and
    `identifier-mean`, at a layer's mean (the connectors' included).

    The layers in `unchecked` have no count to check against; with any of them the overall maximum
    is checked only for being below a checked layer's longest name. With `connectors_derived`, an
    overall maximum below the names names a layer other than the connectors whenever one exceeds it.
    """
    return _plan(stated, counts, unchecked, connectors_derived)[1]


def identifier_names(stated: Mapping[str, Any], counts: IdentifierCounts) -> dict[str, list[str]]:
    """Every met identifier layer's names, in the builder's order, of a feasible passport."""
    names, refusals = _plan(stated, counts)
    if refusals:
        raise GenerationError(f"the identifier lengths were refused after feasibility: {refusals[0].leaf}")
    return names
