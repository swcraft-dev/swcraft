"""The scenarios' locator: the passport's ``Locator`` and the questions only a scenario asks.

:class:`ScenarioLocator` delegates every passport question to the ``Locator`` it wraps and answers
the questions the passport pass does not read -- data accesses, levels, memory fits, resources and the
architecture metrics' figures -- from the model's files through ``load_model_state``, never through
the read kernel, ``resource_analysis`` or ``metrics``, so each figure is an oracle the answer under
test can disagree with. Ties break by name, ascending, except
:meth:`ScenarioLocator.commonest_level`'s, which break in ``ASIL_LEVELS`` order; a question with no
answer is a :class:`LookupError`. The fit arithmetic is exact: the threshold is the decimal the file
spells (``0.8`` is 4/5) and the use is the declared bytes of every component that is not an external
stub.

The model is read on the first scenario-only question, never at construction, so a project asked
only the passport's questions (the large bench) pays no second load.

Does not reach:

- A connector with an end on a composition: :meth:`ScenarioLocator.sender_receiver_rows` and
  :meth:`ScenarioLocator.model_dependencies` refuse one rather than miscount it (the generator
  writes none).
- The **effective** level: the level questions count **declared** levels, so a scenario comparing
  a recipe's count with :meth:`ScenarioLocator.declared_level_count` holds only on a model where
  every component declares its level or no requirement carries one.
- A memory's **enforcement**: the fit questions skip a memory with an undeclared consumer, so they
  never ask an ``advisory`` memory with one.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from functools import cached_property
from pathlib import Path
from typing import Any

from swcraft_tools.model_registry import current_registry
from swcraft_tools.model_state import load_model_state
from swcraft_tools.passport import Locator
from swcraft_tools.safety import ASIL_LEVELS, parse_asil

_MEMORY_KIND = "memory"
_RESOURCE_KINDS = ("core", _MEMORY_KIND)


@dataclass(frozen=True)
class _MemoryFigures:
    """One memory's figures for the fit questions, read off the files."""

    name: str
    size: int
    #: The threshold as the decimal the file spells, exactly.
    threshold: Fraction
    #: The declared bytes of every consumer (every component that is not an external stub).
    used: int
    #: Whether every consumer declares a budget for the memory (``0`` declares no use).
    declared: bool

    @property
    def limit(self) -> Fraction:
        """The threshold limit, ``size x threshold``."""
        return self.size * self.threshold


@dataclass(frozen=True)
class _ModelIndex:
    """What the scenarios' questions read off the model, built once by :func:`_index_model`."""

    #: sender-receiver interface -> its data elements, in order.
    sender_receiver: Mapping[str, tuple[str, ...]]
    #: (interface, data element) -> the reads of it through a port naming the interface.
    read_counts: Mapping[tuple[str, str], int]
    #: component -> port -> its direction and interface.
    ports: Mapping[str, Mapping[str, tuple[str, str | None]]]
    #: component -> its runnables' reads, as (port, data element) pairs, one per access.
    reads: Mapping[str, list[tuple[str, str]]]
    #: component -> its runnables' writes, as (port, data element) pairs, one per access.
    writes: Mapping[str, list[tuple[str, str]]]
    #: declared rating -> the components declaring it.
    levels: Counter[str]
    connectors: list[Any]
    #: the memories, by name.
    memories: tuple[_MemoryFigures, ...]
    #: every core and memory name, ascending.
    resources: tuple[str, ...]
    #: the components that are external stubs, by name.
    external: frozenset[str]
    #: the components that are not stubs, and their ports.
    declared_components: int
    declared_ports: int
    #: every component's runnables, stubs included.
    runnables: int
    compositions: int


def _index_model(model_root: Path) -> _ModelIndex:
    state = load_model_state(model_root, registry=current_registry())
    swcs = list(state["swc"])
    sender_receiver = {
        interface.name: tuple(element.name for element in interface.data_elements)
        for interface in state["interfaces"]
        if interface.kind == "sender_receiver"
    }
    read_counts: Counter[tuple[str, str]] = Counter()
    ports_of: dict[str, dict[str, tuple[str, str | None]]] = {}
    reads: dict[str, list[tuple[str, str]]] = {}
    writes: dict[str, list[tuple[str, str]]] = {}
    for swc in swcs:
        ports = {port.name: (port.direction, port.interface) for port in swc.ports}
        ports_of[swc.name] = ports
        reads[swc.name] = [(read.port, read.element) for run in swc.runnables for read in run.reads]
        writes[swc.name] = [(write.port, write.element) for run in swc.runnables for write in run.writes]
        for port, element in reads[swc.name]:
            interface = ports.get(port, (None, None))[1]
            if interface in sender_receiver:
                read_counts[(interface, element)] += 1
    consumers = [swc for swc in swcs if not swc.external]
    memories = tuple(
        sorted(
            (
                _MemoryFigures(
                    name=element.name,
                    size=element.size,
                    threshold=Fraction(repr(element.threshold)),
                    used=sum(swc.memory.get(element.name, 0) for swc in consumers),
                    declared=all(element.name in swc.memory for swc in consumers),
                )
                for element in state["hardware"]
                if getattr(element, "kind", None) == _MEMORY_KIND
            ),
            key=lambda memory: memory.name,
        )
    )
    return _ModelIndex(
        sender_receiver=sender_receiver,
        read_counts=read_counts,
        ports=ports_of,
        reads=reads,
        writes=writes,
        levels=Counter(parse_asil(swc.asil)[0] for swc in swcs if swc.asil is not None),
        connectors=list(state["connectors"]),
        memories=memories,
        resources=tuple(
            sorted(element.name for element in state["hardware"] if getattr(element, "kind", None) in _RESOURCE_KINDS)
        ),
        external=frozenset(swc.name for swc in swcs if swc.external),
        declared_components=len(consumers),
        declared_ports=sum(len(swc.ports) for swc in consumers),
        runnables=sum(len(swc.runnables) for swc in swcs),
        compositions=len(list(state["compositions"])),
    )


class ScenarioLocator:
    """A passport ``Locator`` and the scenarios' own questions, read off the same model.

    ``repr`` is the class name alone, so a locator printed by accident names nothing. The passport's
    questions are explicit delegations, not a ``__getattr__`` forward, so a question the ``Locator``
    lost is a static mismatch rather than an ``AttributeError`` mid-scenario.
    """

    def __init__(self, base: Locator, model_root: str | Path) -> None:
        self._base = base
        self._model_root = Path(model_root)

    @cached_property
    def _model(self) -> _ModelIndex:
        return _index_model(self._model_root)

    def __repr__(self) -> str:
        return "ScenarioLocator()"

    # -- the passport's questions, delegated -------------------------------------------------------

    def largest_component(self) -> str:
        return self._base.largest_component()

    def largest_components(self) -> tuple[str, ...]:
        return self._base.largest_components()

    def heaviest_interface(self) -> str:
        return self._base.heaviest_interface()

    def heaviest_sender_receiver_interfaces(self) -> tuple[str, ...]:
        return self._base.heaviest_sender_receiver_interfaces()

    def most_unconnected_component(self) -> str:
        return self._base.most_unconnected_component()

    def port_count(self, component: str) -> int:
        return self._base.port_count(component)

    def connector_count(self, component: str) -> int:
        return self._base.connector_count(component)

    def unconnected_ports(self, component: str) -> tuple[str, ...]:
        return self._base.unconnected_ports(component)

    def unconnected_pairs(self) -> tuple[tuple[str, str], ...]:
        return self._base.unconnected_pairs()

    def ports_naming(self, interface: str) -> tuple[tuple[str, str], ...]:
        return self._base.ports_naming(interface)

    # -- the scenarios' questions --------------------------------------------------------------------

    def _most_read_element(self, interface: str) -> tuple[str, int]:
        """*interface*'s most read data element and its reads, ties by name; ``("", 0)`` when none is read."""
        ranked = sorted(
            (
                (element, self._model.read_counts.get((interface, element), 0))
                for element in self._model.sender_receiver[interface]
            ),
            key=lambda pair: (-pair[1], pair[0]),
        )
        return ranked[0] if ranked and ranked[0][1] else ("", 0)

    def most_read_sender_receiver_interface(self) -> str:
        """The sender-receiver interface with the most reads of one of its data elements, ties by name."""
        counts = {name: self._most_read_element(name)[1] for name in self._model.sender_receiver}
        ranked = sorted((name for name, count in counts.items() if count), key=lambda name: (-counts[name], name))
        if not ranked:
            raise LookupError("no runnable reads a data element of a sender-receiver interface")
        return ranked[0]

    def most_read_data_element(self) -> str:
        """The most read data element of :meth:`most_read_sender_receiver_interface`, ties by name."""
        return self._most_read_element(self.most_read_sender_receiver_interface())[0]

    def commonest_level(self) -> str:
        """The most frequent declared level; ties in ``ASIL_LEVELS`` order.

        No component declaring one is the all-zero tie, which ``ASIL_LEVELS`` order gives to ``QM``.
        """
        levels = self._model.levels
        return max(ASIL_LEVELS, key=lambda level: (levels.get(level, 0), -ASIL_LEVELS.index(level)))

    def declared_level_count(self, level: str) -> int:
        """The components that declare *level* (by its rating: ``B(D)`` is ``B``); not the effective count.

        A ``KeyError`` for a name that is no level of ``ASIL_LEVELS``.
        """
        if level not in ASIL_LEVELS:
            raise KeyError(level)
        return self._model.levels.get(level, 0)

    def readers_count(self, interface: str) -> int:
        """The reads of *interface*'s most read data element through ports naming *interface*.

        A ``KeyError`` for a name that is no sender-receiver interface of the model.
        """
        return self._most_read_element(interface)[1]

    def sender_receiver_rows(self) -> int:
        """The sender-receiver table's rows: per connector, each write and matching read across it.

        A row is a write by the provider through its port paired with a read by the requester
        through its port of the same data element. A connector with an end on a composition is a
        ``ValueError``; one whose ends are not one provide and one require port has no row.
        """
        rows = 0
        for connector in self._model.connectors:
            ends = (connector.from_, connector.to)
            if any(end.swc is None for end in ends):
                raise ValueError(
                    f"connector {connector.name} has an end on a composition: the figure does not follow delegations"
                )
            directions = [self._model.ports.get(end.swc, {}).get(end.port, (None, None))[0] for end in ends]
            if sorted(directions, key=str) != ["provide", "require"]:
                continue
            provider, requester = ends if directions[0] == "provide" else ends[::-1]
            written = [element for port, element in self._model.writes.get(provider.swc, []) if port == provider.port]
            read = Counter(
                element for port, element in self._model.reads.get(requester.swc, []) if port == requester.port
            )
            rows += sum(read[element] for element in written)
        return rows

    def _threshold_gap(self) -> _MemoryFigures:
        """The memory :meth:`threshold_gap_memory` names, or a :class:`LookupError`."""
        for memory in self._model.memories:
            if memory.declared and memory.used <= memory.limit and math.floor(memory.limit) + 1 <= memory.size:
                return memory
        raise LookupError(
            "no memory whose every consumer declares a budget leaves a whole amount between its threshold "
            "limit and its size"
        )

    def threshold_gap_memory(self) -> str:
        """The first memory by name with a whole amount over its threshold limit and within its size."""
        return self._threshold_gap().name

    def threshold_gap_bytes(self) -> int:
        """The smallest whole amount over :meth:`threshold_gap_memory`'s threshold limit; within its size."""
        memory = self._threshold_gap()
        return math.floor(memory.limit) - memory.used + 1

    def over_size_bytes(self) -> int:
        """The smallest whole amount over :meth:`threshold_gap_memory`'s size."""
        memory = self._threshold_gap()
        return memory.size - memory.used + 1

    def declared_resources(self) -> tuple[str, ...]:
        """Every core and memory name the hardware layer declares, ascending; empty when it declares none."""
        return self._model.resources

    # -- the architecture metrics' figures of the whole model ------------------------------------------

    def model_components(self) -> int:
        """The components that are not external stubs."""
        return self._model.declared_components

    def model_compositions(self) -> int:
        """The compositions the model declares."""
        return self._model.compositions

    def model_component_ports(self) -> int:
        """The ports of the components that are not external stubs."""
        return self._model.declared_ports

    def model_connectors(self) -> int:
        """The connectors the model declares."""
        return len(self._model.connectors)

    def model_runnables(self) -> int:
        """The runnables of every component, external stubs included."""
        return self._model.runnables

    def model_dependencies(self) -> int:
        """The directed dependencies between components, each pair once.

        Per connector joining a ``require`` and a ``provide`` port of two different components, the
        pair *(requirer, provider)*; a pair of two external stubs is left out. A connector with an
        end on a composition is a ``ValueError``.
        """
        pairs: set[tuple[str, str]] = set()
        for connector in self._model.connectors:
            ends = (connector.from_, connector.to)
            if any(end.swc is None for end in ends):
                raise ValueError(
                    f"connector {connector.name} has an end on a composition: the figure does not follow delegations"
                )
            directions = [self._model.ports.get(end.swc, {}).get(end.port, (None, None))[0] for end in ends]
            if sorted(directions, key=str) != ["provide", "require"] or ends[0].swc == ends[1].swc:
                continue
            provider, requirer = ends if directions[0] == "provide" else ends[::-1]
            if {provider.swc, requirer.swc} <= self._model.external:
                continue
            pairs.add((requirer.swc, provider.swc))
        return len(pairs)
