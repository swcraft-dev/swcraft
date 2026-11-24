"""The model passport pass: a deterministic, names-free description of a loaded model's shape.

:func:`compute_passport` reads a model once through the kernel (``load_model_state``) and answers a
:class:`PassportResult` of two things:

- ``passport``: a JSON-ready dict of counts, distributions and lengths, keyed in the order of the
  package's ``model_passport.schema.json`` (v1.0.0, read through :func:`schema_text`), every list
  descending, every leaf but the two version fields a number. It is built by construction and not
  validated against the schema here.
- ``locator``: an in-memory :class:`Locator` answering the questions an expectation needs a name
  for. Nothing serializes it, and its ``repr`` names nothing, so a name cannot leave the machine.

``swcraft passport`` writes ``passport`` alone. The definitions, each a reading of the model:

- *component ports* are the ``swc`` layer's ports; a composition's own ports are not counted.
- *hubs* are the components whose port count exceeds twice the median (``statistics.median``).
- *unconnected* is the ``unconnected`` computed value's reading, over component types.
- *fan-out*: per interface a port names, the component ports naming it.
- *connectors*: ``assembly`` counts the connector layer, ``delegation`` the compositions'
  delegations, and ``links`` is ``max(0, connectors between two non-hub components - non-hub
  components // 2)``.
- *requirements* read every ``satisfies`` member through ``trace.link_sites_of_state`` (an
  element-form member is no link), by ``trace.site_kind`` with the four hardware kinds as one;
  ``unallocated`` is ``validator.requirement_allocated``'s ``False`` (a stub is neither);
  ``stale_links`` counts ``trace.stale_link``'s answers that state a version.
- *identifiers*: per layer the longest and the mean identity field (three places; ``0`` for an
  empty layer), and the longest overall.
- *declared levels*: a component's ``asil``, ``X(Y)`` counted as ``X``; a component with none is
  not counted.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any, NamedTuple

from swcraft_tools.model_registry import current_registry
from swcraft_tools.model_state import load_model_state
from swcraft_tools.safety import ASIL_LEVELS, parse_asil
from swcraft_tools.schema import Core, Memory, Partition, Processor
from swcraft_tools.trace import (
    is_element_form,
    link_sites_of_state,
    parse_link,
    requirement_versions,
    site_kind,
    stale_link,
)
from swcraft_tools.validator import requirement_allocated

#: The passport schema's own version (``x-passport-version``) that this pass writes.
PASSPORT_VERSION = "1.0.0"

#: The passport schema's file name in the package, beside this module.
SCHEMA_RESOURCE = "model_passport.schema.json"

#: How many entries a ``top`` list carries.
TOP = 8

#: How many components :meth:`Locator.largest_components` answers by default.
LARGEST = 16

#: How many link site kinds a requirement on all of them covers: ``trace.site_kind``'s seven, the
#: four hardware kinds counted as one. A count, since a written list would read as a layer table.
SITE_KIND_COUNT = 7

#: The hardware element classes, whose four kinds are one site kind here.
_HARDWARE = (Processor, Core, Memory, Partition)

#: The one key all four hardware kinds share.
_HARDWARE_SITE = "hardware element"


@dataclass(frozen=True, repr=False)
class Locator:
    """The names an expectation asks for, answered locally and never serialized.

    Every answer breaks a tie by name, ascending, so it is deterministic. A question the model has
    no answer to (no component, no port naming an interface, no unconnected port) is a
    :class:`LookupError` saying so, never an ``IndexError``. ``repr`` is the class name alone: a
    locator printed by accident names nothing.
    """

    #: Component name -> its port names, in file order.
    _ports: Mapping[str, tuple[str, ...]]
    #: Component name -> its unconnected port names, in file order.
    _unconnected: Mapping[str, tuple[str, ...]]
    #: Interface name -> the ``(component, port)`` pairs naming it, sorted.
    _naming: Mapping[str, tuple[tuple[str, str], ...]]
    #: The sender-receiver interfaces' names.
    _sender_receiver: frozenset[str]
    #: Component name -> how many connectors have an end on it (a connector with both ends on it
    #: counted once); a component no connector reaches is absent.
    _connectors: Mapping[str, int] = field(default_factory=dict)

    def __repr__(self) -> str:
        return "Locator()"

    @staticmethod
    def _ranked(counts: Mapping[str, int]) -> list[str]:
        return sorted(counts, key=lambda name: (-counts[name], name))

    @staticmethod
    def _first(ranked: Iterable[str], none: str) -> str:
        for name in ranked:
            return name
        raise LookupError(none)

    def largest_components(self, n: int = LARGEST) -> tuple[str, ...]:
        """The *n* components with the most ports, the largest first."""
        return tuple(self._ranked({name: len(ports) for name, ports in self._ports.items()})[:n])

    def largest_component(self) -> str:
        """The component with the most ports."""
        return self._first(self.largest_components(1), "the model has no component")

    def port_count(self, component: str) -> int:
        """*component*'s port count."""
        return len(self._ports[component])

    def heaviest_interface(self) -> str:
        """The interface named by the most component ports."""
        ranked = self._ranked({name: len(pairs) for name, pairs in self._naming.items()})
        return self._first(ranked, "no component port names an interface")

    def fan_outs(self) -> tuple[int, ...]:
        """The number of component ports naming each interface some port names."""
        return tuple(len(pairs) for pairs in self._naming.values())

    def heaviest_sender_receiver_interfaces(self) -> tuple[str, ...]:
        """The sender-receiver interfaces some component port names, the most-named first."""
        named = {name: len(pairs) for name, pairs in self._naming.items() if name in self._sender_receiver}
        return tuple(self._ranked(named))

    def connector_count(self, component: str) -> int:
        """How many connectors have an end on *component* (one with both ends on it counted once)."""
        return self._connectors.get(component, 0)

    def ports_naming(self, interface: str) -> tuple[tuple[str, str], ...]:
        """The ``(component, port)`` pairs whose port names *interface*, sorted; empty when none does."""
        return self._naming.get(interface, ())

    def most_unconnected_component(self) -> str:
        """The component with the most unconnected ports."""
        ranked = self._ranked({name: len(ports) for name, ports in self._unconnected.items() if ports})
        return self._first(ranked, "the model has no unconnected port")

    def unconnected_ports(self, component: str) -> tuple[str, ...]:
        """*component*'s unconnected port names, in file order."""
        return self._unconnected.get(component, ())

    def unconnected_pairs(self) -> tuple[tuple[str, str], ...]:
        """``(component, port)`` of every unconnected port of the model, sorted."""
        return tuple(sorted((name, port) for name, ports in self._unconnected.items() for port in ports))


class PassportResult(NamedTuple):
    """The passport (JSON-ready, names-free) and its locator (local, never serialized)."""

    passport: dict[str, Any]
    locator: Locator


def schema_text() -> str:
    """The passport schema's text, read from the package data exactly as the wheel carries it."""
    return (resource_files("swcraft_tools") / SCHEMA_RESOURCE).read_text(encoding="utf-8")


def _top(counts: Iterable[int]) -> list[int]:
    return sorted(counts, reverse=True)[:TOP]


def _reached_ports(layers: Mapping[str, list[Any]]) -> set[tuple[str, str]]:
    """``(component type, port)`` for every port a connector end or a delegation reaches."""
    reached: set[tuple[str, str]] = set()
    for connector in layers["connectors"]:
        for end in (connector.from_, connector.to):
            if end.swc is not None:
                reached.add((end.swc, end.port))
    for composition in layers["compositions"]:
        member_types = {member.name: member.type for member in composition.members if member.type is not None}
        for delegation in composition.delegations:
            member_type = member_types.get(delegation.member)
            if member_type is not None:
                reached.add((member_type, delegation.member_port))
    return reached


def _components(layers: Mapping[str, list[Any]]) -> tuple[dict[str, Any], Locator, set[str]]:
    swcs = layers["swc"]
    reached = _reached_ports(layers)
    ports = {swc.name: tuple(port.name for port in swc.ports) for swc in swcs}
    unconnected = {
        swc.name: tuple(port.name for port in swc.ports if (swc.name, port.name) not in reached) for swc in swcs
    }
    naming: dict[str, list[tuple[str, str]]] = {}
    for swc in swcs:
        for port in swc.ports:
            if port.interface is not None:
                naming.setdefault(port.interface, []).append((swc.name, port.name))
    counts = [len(swc.ports) for swc in swcs]
    threshold = 2 * statistics.median(counts) if counts else 0
    hub_names = {swc.name for swc in swcs if len(swc.ports) > threshold}
    levels = Counter(parse_asil(swc.asil)[0] for swc in swcs if swc.asil is not None)
    section = {
        "ports": {"total": sum(counts), "top": _top(counts)},
        "hubs": sorted((len(swc.ports) for swc in swcs if swc.name in hub_names), reverse=True),
        "unconnected": {
            "total": sum(len(names) for names in unconnected.values()),
            "max_on_one": max((len(names) for names in unconnected.values()), default=0),
        },
        "by_declared_level": {level: levels.get(level, 0) for level in ASIL_LEVELS},
        "runnables": sum(len(swc.runnables) for swc in swcs),
    }
    locator = Locator(
        _ports=ports,
        _unconnected=unconnected,
        _naming={name: tuple(sorted(pairs)) for name, pairs in naming.items()},
        _sender_receiver=frozenset(
            interface.name for interface in layers["interfaces"] if interface.kind == "sender_receiver"
        ),
        _connectors=Counter(
            name
            for connector in layers["connectors"]
            for name in {connector.from_.swc, connector.to.swc}
            if name is not None
        ),
    )
    return section, locator, hub_names


def _interfaces(layers: Mapping[str, list[Any]], locator: Locator) -> dict[str, Any]:
    kinds = Counter(interface.kind for interface in layers["interfaces"])
    fan_out = _top(locator.fan_outs())
    return {
        "sender_receiver": kinds.get("sender_receiver", 0),
        "client_server": kinds.get("client_server", 0),
        "fan_out": {"max": fan_out[0] if fan_out else 0, "top": fan_out},
    }


def _connectors(layers: Mapping[str, list[Any]], hub_names: set[str]) -> dict[str, Any]:
    connectors = layers["connectors"]
    ordinary = {swc.name for swc in layers["swc"]} - hub_names
    between_ordinary = sum(
        1 for connector in connectors if connector.from_.swc in ordinary and connector.to.swc in ordinary
    )
    return {
        "assembly": len(connectors),
        "delegation": sum(len(composition.delegations) for composition in layers["compositions"]),
        "links": max(0, between_ordinary - len(ordinary) // 2),
    }


def _hardware(layers: Mapping[str, list[Any]]) -> dict[str, Any]:
    hardware = layers["hardware"]
    partitions = [element for element in hardware if isinstance(element, Partition)]
    return {
        "cores": sum(1 for element in hardware if isinstance(element, Core)),
        "partitions": len(partitions),
        "memories": sum(1 for element in hardware if isinstance(element, Memory)),
        "rated_partitions": sum(1 for partition in partitions if partition.asil is not None),
    }


def _requirements(layers: Mapping[str, list[Any]]) -> dict[str, Any]:
    requirements = layers["requirements"]
    versions = requirement_versions(requirements)
    kinds: dict[str, set[str]] = {}
    sites: Counter[str] = Counter()
    on_port: Counter[str] = Counter()
    links = 0
    stale = 0
    for state_site in link_sites_of_state(layers):
        value = state_site.site.value
        if is_element_form(value):
            continue
        links += 1
        requirement_id = parse_link(value).id
        kind = site_kind(state_site.layer, state_site.element, state_site.site)
        key = _HARDWARE_SITE if isinstance(state_site.element, _HARDWARE) else kind
        kinds.setdefault(requirement_id, set()).add(key)
        sites[requirement_id] += 1
        if kind == "port":
            on_port[requirement_id] += 1
        found = stale_link(value, versions)
        if found is not None and found.stated is not None:
            stale += 1
    declared = list(dict.fromkeys(requirement.id for requirement in requirements))
    stubs = {requirement.id for requirement in requirements if requirement.stub}
    satisfied = set(sites)
    return {
        "on_all_site_kinds": sum(1 for rid in declared if len(kinds.get(rid, ())) == SITE_KIND_COUNT),
        "on_one_port": sum(1 for rid in declared if sites[rid] == 1 and on_port[rid] == 1),
        "unallocated": sum(1 for rid in declared if requirement_allocated(rid, satisfied, stubs) is False),
        "stale_links": stale,
        "links": links,
    }


def _identifiers(layers: Mapping[str, list[Any]], identity_fields: Mapping[str, str]) -> dict[str, Any]:
    section: dict[str, Any] = {"max_length": 0}
    longest = 0
    for layer, elements in layers.items():
        lengths = [len(str(getattr(element, identity_fields[layer]))) for element in elements]
        longest = max([longest, *lengths])
        section[layer] = {
            "max_length": max(lengths, default=0),
            "mean_length": round(statistics.fmean(lengths), 3) if lengths else 0,
        }
    section["max_length"] = longest
    return section


def compute_passport(model_root: str | Path) -> PassportResult:
    """The passport of the model at *model_root*, and its locator.

    Reads the model once through ``load_model_state``; parses no YAML of its own. Raises what that
    load raises (``ElementParseError`` naming the element, ``ModelRootNotFoundError``, ...).
    """
    registry = current_registry()
    state = load_model_state(model_root, registry=registry)
    layers = {name: list(state[name]) for name in registry.layer_names}
    identity_fields = {name: registry.layer(name).identity_field for name in registry.layer_names}
    components, locator, hub_names = _components(layers)
    # Built key by key, in the schema's order: several section names are layer names, and a dict
    # literal of them would read as a layer table to the layer-vocabulary scan.
    passport: dict[str, Any] = {}
    passport["passport_version"] = PASSPORT_VERSION
    passport["contract_version"] = registry.contract_version
    passport["layers"] = {name: len(elements) for name, elements in layers.items()}
    passport["components"] = components
    passport["interfaces"] = _interfaces(layers, locator)
    passport["connectors"] = _connectors(layers, hub_names)
    passport["hardware"] = _hardware(layers)
    passport["tasks"] = {"idle": sum(1 for task in layers["tasks"] if not task.runnables)}
    passport["requirements"] = _requirements(layers)
    passport["identifiers"] = _identifiers(layers, identity_fields)
    return PassportResult(passport, locator)
