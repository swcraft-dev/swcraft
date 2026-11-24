"""The derived values a read may ask for.

Holds :data:`DERIVED`, the closed vocabulary of computed values the editing
protocol's ``read`` may ask for under ``derived``, and :func:`derive_rows`, which
computes them over a :class:`ModelState` with the validator's own functions. Also
:func:`data_element_base_type` and :func:`not_judged_reason`. It reports no rule.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple

from swcraft_tools.data_description import DataDescription
from swcraft_tools.model_state import ModelState
from swcraft_tools.safety import parse_asil
from swcraft_tools.scheduling_identity import composition_index as _composition_index
from swcraft_tools.scheduling_identity import identities_from
from swcraft_tools.schema import (
    Connector,
    Hardware,
    Interface,
    MappingSet,
    Partition,
    Port,
    Requirement,
    SRInterface,
    SWComponent,
    Task,
    core_of_partition,
    hardware_by_name,
)
from swcraft_tools.trace import (
    is_element_form,
    link_sites_of_state,
    parse_link,
    requirement_stubs,
    requirement_versions,
    site_kind,
    stale_link,
)
from swcraft_tools.validator.asil import components_by_name, effective_asil_levels, partition_runs, partitions_among
from swcraft_tools.validator.compatibility import (
    PortEnd,
    interface_index,
    not_judged_component,
    not_judged_stub,
    resolve_type,
)
from swcraft_tools.validator.connectors import component_ends
from swcraft_tools.validator.data_types import application_type_pairings
from swcraft_tools.validator.description_elements import duplicates
from swcraft_tools.validator.traceability import requirement_allocated

# ---------------------------------------------------------------------------
# Derived values: the closed vocabulary a read may ask for (`derived`)
# ---------------------------------------------------------------------------

_DerivedRows = Literal["one", "one per value"]


_ONE_PER_VALUE: _DerivedRows = "one per value"


def _no_base_type() -> dict[str, str | None]:
    """``base_type``'s full abstention: neither an implementation nor a base type."""
    return {"implementation": None, "base": None}


@dataclass(frozen=True)
class DerivedValue:
    """One entry of :data:`DERIVED`: a value this module already computes, published by name.

    Each field is published by ``discover`` as it stands (:meth:`as_json`):

    - ``name`` -- what a request's ``derived`` names;
    - ``at`` -- where it applies: ``{"layer": L}``, or ``{"layer": L,
      "members": [path, ...]}`` for a value of a member row; the reader refuses
      a request for it anywhere else;
    - ``rows`` -- ``"one"``, or ``"one per value"`` for a set-valued name, whose
      every value is a row of its own;
    - ``shape`` -- the value's nested keys, each leaf one of ``"string"``,
      ``"string|null"``, ``"integer"``, ``"integer|null"`` or ``"boolean|null"``
      (a ``null`` is an abstention); a join path into ``derived`` is checked
      against it.
    """

    name: str
    at: Mapping[str, object]
    rows: _DerivedRows
    shape: Mapping[str, object]

    def as_json(self) -> dict[str, object]:
        """The entry as ``discover`` publishes it, a fresh copy every call."""
        return deepcopy({"name": self.name, "at": self.at, "rows": self.rows, "shape": self.shape})


#: The closed vocabulary of computed values a read may ask for, each computed by the
#: validator function that already owns it, never a second formula. ``discover``
#: publishes it as ``reads.derived``; adding a name is a protocol change.
DERIVED: tuple[DerivedValue, ...] = (
    DerivedValue(
        name="connection_ends",
        at={"layer": "connectors"},
        rows=_ONE_PER_VALUE,
        shape={
            "provider": {"swc": "string", "port": "string"},
            "requester": {"swc": "string", "port": "string"},
        },
    ),
    DerivedValue(
        name="base_type",
        at={"layer": "swc", "members": ["runnables.reads", "runnables.writes"]},
        rows="one",
        shape={"implementation": "string|null", "base": "string|null"},
    ),
    DerivedValue(
        name="effective_level",
        at={"layer": "swc"},
        rows="one",
        shape={"level": "string", "rating": "string"},
    ),
    DerivedValue(
        name="partitions",
        at={"layer": "swc"},
        rows=_ONE_PER_VALUE,
        shape={"partition": "string", "core": "string|null"},
    ),
    DerivedValue(
        name="crossing_ends",
        at={"layer": "connectors"},
        rows=_ONE_PER_VALUE,
        shape={
            "provider": {"swc": "string", "port": "string", "partition": "string"},
            "requester": {"swc": "string", "port": "string", "partition": "string"},
        },
    ),
    DerivedValue(
        name="unconnected",
        at={"layer": "swc", "members": ["ports"]},
        rows=_ONE_PER_VALUE,
        shape={"direction": "string", "interface": "string|null"},
    ),
    DerivedValue(name="core", at={"layer": "tasks"}, rows="one", shape={"partition": "string", "core": "string|null"}),
    DerivedValue(
        name="realised_by",
        at={"layer": "requirements"},
        rows=_ONE_PER_VALUE,
        shape={
            "layer": "string",
            "element": "string",
            "member": "string|null",
            "kind": "string",
            "stated": "integer|null",
        },
    ),
    DerivedValue(
        name="allocation",
        at={"layer": "requirements"},
        rows="one",
        shape={"sites": "integer", "elements": "integer", "allocated": "boolean|null"},
    ),
    DerivedValue(
        name="stale_links",
        at={"layer": "requirements"},
        rows=_ONE_PER_VALUE,
        shape={
            "layer": "string",
            "element": "string",
            "member": "string|null",
            "kind": "string",
            "stated": "integer|null",
            "current": "integer",
        },
    ),
)


def data_element_base_type(
    swc: SWComponent,
    port: str,
    element: str,
    *,
    interfaces: Mapping[str, Interface],
    mapping_sets: Sequence[MappingSet],
    data_types: DataDescription,
) -> dict[str, str | None]:
    """The implementation type and the base type the data element *element* of *swc*'s port *port* resolves to.

    ``{"implementation": I, "base": B}``, the value of ``derived: ["base_type"]``. An
    ``application_type`` takes the one implementation type *swc*'s
    ``data_type_mappings`` pair it with; ``base`` is the base type a ``value_type``,
    reached directly or through reference types, stands on.

    It judges nothing: every case another rule owns is an abstention, a ``None``:

    - no port, interface or data element of that name, a client-server interface,
      or an external interface stub: both ``None``;
    - an application type paired with no or two implementation types: both ``None``;
    - *swc* names a set *mapping_sets* declares twice: both ``None`` for every element
      of *swc*, as :func:`check_application_type_double_mapping` skips the component;
    - an implementation that resolves to no base type (a record, an array, a pointer,
      an external type stub, an undeclared name or a cycle): ``base`` is ``None``.

    *mapping_sets* is the layer's original sequence: a name-keyed map would already
    have lost the duplicate.

    What it does **not** reach: the port's direction, and the members of a composite
    type, which have no base type of their own here.
    """
    absent = _no_base_type()
    declared_port = next((candidate for candidate in swc.ports if candidate.name == port), None)
    interface = None if declared_port is None else interfaces.get(declared_port.interface or "")
    if not isinstance(interface, SRInterface):
        return absent
    data_element = next((candidate for candidate in interface.data_elements if candidate.name == element), None)
    if data_element is None:
        return absent
    duplicated_sets = frozenset(duplicates([mapping_set.name for mapping_set in mapping_sets]))
    if any(name in duplicated_sets for name in swc.data_type_mappings):
        return absent
    implementation = data_element.implementation_type
    if implementation is None and data_element.application_type is not None:
        set_map = {mapping_set.name: mapping_set for mapping_set in mapping_sets}
        onto = application_type_pairings(swc.data_type_mappings, set_map).get(data_element.application_type, {})
        if len(onto) != 1:
            return absent
        implementation = next(iter(onto))
    if implementation is None:
        return absent
    resolved = resolve_type(implementation, data_types, {}, application=False)
    base = resolved.base.name if resolved is not None and resolved.base is not None else None
    return {"implementation": implementation, "base": base}


#: The data description layers, read only for ``base_type``.
_DERIVED_DATA_DESCRIPTION_LAYERS = (
    "application_types",
    "implementation_types",
    "base_types",
    "units",
    "compu_methods",
    "data_constraints",
)


def derive_rows(rows: Sequence[Mapping[str, Any]], names: Sequence[str], state: ModelState) -> list[dict[str, Any]]:
    """*rows*, each carrying the values *names* asks for under ``"derived"``.

    *rows* are identity rows as ``read`` publishes them at ``form: "index"``: ``{"element": E}`` for a layer,
    ``{"element": E, "through": [...], "member": M}`` for a member path. Each output row is a copy of its input row
    with ``"derived": {name: value, ...}`` added, *names* in the order given and a name given twice applied once. A
    ``"one per value"`` name yields one row per value and none where it has no value; a ``"one"`` name yields its
    one value. Several names are applied as the uncorrelated **product** of their values, in *names*' order, so a
    set-valued name with no value drops the row. Identical rows are not collapsed. Nothing is read from disk.

    - ``connection_ends`` (``connectors``): every provider end :func:`component_ends` reaches from ``from`` times
      every requester end from ``to``, as ``{"provider": {"swc", "port"}, "requester": {"swc", "port"}}``. A stub's
      end is an end, an unresolved one no value; components resolve last-declared-wins, the rest first-wins.
    - ``base_type`` (``swc`` at ``runnables.reads`` / ``runnables.writes``): :func:`data_element_base_type`.
    - ``effective_level`` (``swc``): ``{"level": L, "rating": R}``, ``L`` being :func:`effective_asil_levels` at the
      row's component and ``R`` its plain level (``"B"`` for both ``"B"`` and ``"B(D)"``).
    - ``partitions`` (``swc``): one ``{"partition": P, "core": C}`` per partition whose tasks run a runnable of the
      row's component type, in partition-name order (only a partition that is the first-wins resolution of its
      name; two instances of one type are not told apart); ``C`` is ``None`` where the core does not resolve.
    - ``crossing_ends`` (``connectors``): ``connection_ends``' value with ``"partition"`` added to each end, one per
      pair of differing provider and requester partitions; a connector crossing none has no value.
    - ``unconnected``, ``core`` (:func:`_port_and_task_arms`) and ``realised_by``, ``allocation``,
      ``stale_links`` (:func:`_requirement_arms`).

    The caller checks that each name applies where *rows* were read. A row of another shape, or (for
    ``effective_level``) naming no declared component, raises ``KeyError`` or ``TypeError``.

    Raises:
        ValueError: for a name outside :data:`DERIVED` -- a defect of the caller.
    """
    entries = {entry.name: entry for entry in DERIVED}
    unknown = [name for name in names if name not in entries]
    if unknown:
        raise ValueError(f"no derived value is named {', '.join(repr(name) for name in unknown)}")
    asked = list(dict.fromkeys(names))
    layers = state.layers
    swc_map = {swc.name: swc for swc in layers.get("swc", [])}
    arms = _connector_and_partition_arms(asked, layers, swc_map)
    arms.update(_base_type_and_level_arms(asked, layers, swc_map))
    arms.update(_port_and_task_arms(asked, layers, swc_map))
    arms.update(_requirement_arms(asked, layers))
    return _applied_rows(rows, asked, arms, entries)


_Arms = dict[str, Callable[[Mapping[str, Any]], Any]]


def _connector_and_partition_arms(
    asked: Sequence[str], layers: Mapping[str, Sequence[Any]], swc_map: Mapping[str, SWComponent]
) -> _Arms:
    """The arms of :func:`derive_rows` for ``connection_ends``, ``partitions`` and ``crossing_ends``."""
    arms: _Arms = {}
    if "connection_ends" in asked or "crossing_ends" in asked:
        composition_map = _composition_index(list(layers.get("compositions", [])))
        connector_map: dict[str, Connector] = {}
        for connector in layers.get("connectors", []):
            connector_map.setdefault(connector.name, connector)

        def end_pairs(row: Mapping[str, Any]) -> list[tuple[SWComponent, Port, SWComponent, Port]]:
            connector = connector_map.get(row["element"])
            if connector is None:
                return []
            return [
                (provider, provider_port, requester, requester_port)
                for provider, provider_port in component_ends(connector.from_, swc_map, composition_map)
                for requester, requester_port in component_ends(connector.to, swc_map, composition_map)
            ]

        def connection_ends(row: Mapping[str, Any]) -> list[dict[str, dict[str, str]]]:
            return [
                {
                    "provider": {"swc": provider.name, "port": provider_port.name},
                    "requester": {"swc": requester.name, "port": requester_port.name},
                }
                for provider, provider_port, requester, requester_port in end_pairs(row)
            ]

        arms["connection_ends"] = connection_ends

    partitions_of = _partition_index(asked, layers)

    if "partitions" in asked:

        def partitions(row: Mapping[str, Any]) -> list[dict[str, str | None]]:
            return [{"partition": name, "core": core} for name, core in partitions_of.get(row["element"], [])]

        arms["partitions"] = partitions

    if "crossing_ends" in asked:

        def crossing_ends(row: Mapping[str, Any]) -> list[dict[str, dict[str, str]]]:
            return [
                {
                    "provider": {"swc": provider.name, "port": provider_port.name, "partition": provided_in},
                    "requester": {"swc": requester.name, "port": requester_port.name, "partition": required_in},
                }
                for provider, provider_port, requester, requester_port in end_pairs(row)
                for provided_in, _provider_core in partitions_of.get(provider.name, [])
                for required_in, _requester_core in partitions_of.get(requester.name, [])
                if provided_in != required_in
            ]

        arms["crossing_ends"] = crossing_ends

    return arms


def _partition_index(
    asked: Sequence[str], layers: Mapping[str, Sequence[Any]]
) -> dict[str, list[tuple[str, str | None]]]:
    """The partition index ``partitions`` and ``crossing_ends`` read; empty when neither is asked."""
    partitions_of: dict[str, list[tuple[str, str | None]]] = {}
    if "partitions" in asked or "crossing_ends" in asked:
        hardware: list[Hardware] = list(layers.get("hardware", []))
        by_hw_name = hardware_by_name(hardware)
        tasks: list[Task] = list(layers.get("tasks", []))
        by_swc_name = components_by_name(list(layers.get("swc", [])))
        identities = identities_from(list(layers.get("compositions", [])))
        resolved = [
            partition for partition in partitions_among(hardware) if by_hw_name.get(partition.name) is partition
        ]
        for partition in sorted(resolved, key=lambda partition: partition.name):
            core = core_of_partition(partition, by_hw_name)
            # Only the keys are read; the level each maps to is not this value's.
            for component in partition_runs(partition.name, tasks, by_swc_name, lambda _swc: "", identities):
                partitions_of.setdefault(component, []).append((partition.name, None if core is None else core.name))
    return partitions_of


def _base_type_and_level_arms(
    asked: Sequence[str], layers: Mapping[str, Sequence[Any]], swc_map: Mapping[str, SWComponent]
) -> _Arms:
    """The arms of :func:`derive_rows` for ``base_type`` and ``effective_level``."""
    arms: _Arms = {}
    if "base_type" in asked:
        interface_map = interface_index(list(layers.get("interfaces", [])))
        mapping_sets: list[MappingSet] = list(layers.get("mapping_sets", []))
        data_types = DataDescription(
            [element for layer in _DERIVED_DATA_DESCRIPTION_LAYERS for element in layers.get(layer, [])]
        )

        def base_type(row: Mapping[str, Any]) -> dict[str, str | None]:
            swc = swc_map.get(row["element"])
            member: Mapping[str, str] = row["member"]
            if swc is None:
                return _no_base_type()
            return data_element_base_type(
                swc,
                member["port"],
                member["element"],
                interfaces=interface_map,
                mapping_sets=mapping_sets,
                data_types=data_types,
            )

        arms["base_type"] = base_type

    if "effective_level" in asked:
        levels = effective_asil_levels(list(layers.get("swc", [])), list(layers.get("requirements", [])))

        def effective_level(row: Mapping[str, Any]) -> dict[str, str]:
            level = levels[row["element"]]
            return {"level": level, "rating": parse_asil(level)[0]}

        arms["effective_level"] = effective_level

    return arms


def _applied_rows(
    rows: Sequence[Mapping[str, Any]], asked: Sequence[str], arms: _Arms, entries: Mapping[str, DerivedValue]
) -> list[dict[str, Any]]:
    """*rows*, each multiplied by the values *arms* answer for the *asked* names (:func:`derive_rows`)."""
    # Copied key by key: the coverage oracle abstains on a `**` spread over an unannotated row.
    derived_rows: list[dict[str, Any]] = []
    for row in rows:
        combinations: list[list[tuple[str, Any]]] = [[]]
        for name in asked:
            value = arms[name](row)
            values = list(value) if entries[name].rows == _ONE_PER_VALUE else [value]
            combinations = [[*pairs, (name, item)] for pairs in combinations for item in values]
        for pairs in combinations:
            derived_row: dict[str, Any] = {key: row[key] for key in row}
            # Not `dict(pairs)`: the coverage oracle cannot bind that receiver.
            derived_row["derived"] = {name: item for name, item in pairs}
            derived_rows.append(derived_row)
    return derived_rows


def not_judged_reason(
    ends: Sequence[PortEnd],
    required: str | None,
    provided: str | None,
    *,
    context: str,
    interfaces: Mapping[str, Interface],
) -> str | None:
    """The validator's *not judged* answer for one pair of ports, or ``None`` when the pair is judged.

    :func:`not_judged_component` over *ends*, then :func:`not_judged_stub` over
    the two interfaces, in :func:`check_port_compatibility`'s order. The caller
    judges the directions first: a pair whose directions disagree is a finding,
    never *not judged*. An unset interface is passed as ``None``.
    **What it does not reach:** an overview's bundle, which can mix judged and unjudged connectors,
    and the sequence view, which asks nothing.
    """
    return not_judged_component(ends) or not_judged_stub(required, provided, context=context, interfaces=interfaces)


def _port_and_task_arms(
    asked: Sequence[str], layers: Mapping[str, Sequence[Any]], swc_map: Mapping[str, SWComponent]
) -> dict[str, Callable[[Mapping[str, Any]], Any]]:
    """The arms of :func:`derive_rows` for ``unconnected`` and ``core``.

    - ``unconnected`` (``swc`` at ``ports``): one ``{"direction", "interface"}``
      for a port that **no connector end and no delegation reaches**, and no row
      for every other port. ``interface`` is ``None`` on a stub's port, whose
      ``observed_interface`` this value does not read. Component TYPES, as connectors name
      them: an instance is not told apart from another of its type.
    - ``core`` (``tasks``): ``{"partition", "core"}`` -- the task's partition and
      the core it is on, ``None`` where the core does not resolve. The partition
      resolves first-wins over the hardware layer.

    What this does not reach: a composition's own ports (``unconnected`` is
    declared at the ``swc`` ports only), and whether a port's endpoint is
    *compatible* -- the connector rules own that.
    """
    arms: dict[str, Callable[[Mapping[str, Any]], Any]] = {}
    if "unconnected" in asked:
        reached: set[tuple[str, str]] = set()
        for connector in layers.get("connectors", []):
            for end in (connector.from_, connector.to):
                if end.swc is not None:
                    reached.add((end.swc, end.port))
        for composition in layers.get("compositions", []):
            member_types = {member.name: member.type for member in composition.members if member.type is not None}
            for delegation in composition.delegations:
                member_type = member_types.get(delegation.member)
                if member_type is not None:
                    reached.add((member_type, delegation.member_port))

        def unconnected(row: Mapping[str, Any]) -> list[dict[str, str | None]]:
            component = swc_map.get(row["element"])
            member = row["member"]
            name = member if isinstance(member, str) else member.get("name")
            if component is None or (component.name, name) in reached:
                return []
            for port in component.ports:
                if port.name == name:
                    return [{"direction": port.direction, "interface": port.interface}]
            return []

        arms["unconnected"] = unconnected

    if "core" in asked:
        by_hw_name = hardware_by_name(list(layers.get("hardware", [])))
        task_map: dict[str, Task] = {}
        for task in layers.get("tasks", []):
            task_map.setdefault(task.name, task)

        def core(row: Mapping[str, Any]) -> dict[str, str | None]:
            task = task_map[row["element"]]
            partition = by_hw_name.get(task.partition)
            if not isinstance(partition, Partition):
                return {"partition": task.partition, "core": None}
            found = core_of_partition(partition, by_hw_name)
            return {"partition": task.partition, "core": None if found is None else found.name}

        arms["core"] = core

    return arms


class _Realisation(NamedTuple):
    """One link site naming a requirement, as the requirements layer's computed values read it."""

    layer: str
    element: str
    member: str | None
    kind: str
    stated: int | None


def _requirement_arms(
    asked: Sequence[str], layers: Mapping[str, Sequence[Any]]
) -> dict[str, Callable[[Mapping[str, Any]], Any]]:
    """The arms of :func:`derive_rows` for ``realised_by``, ``allocation`` and ``stale_links``.

    The sites are every ``satisfies`` member the state holds, in the contract's
    layer order, then element order, then file order. An element-form member is no
    site, and a member one list holds twice is **one** site. Two members of one
    requirement with identical rows (``R@x`` and ``R@y``, or ``R@x`` and ``R``) are
    one row, but each member is classified on its own, so the bare id's
    ``stale_links`` row is kept.

    - ``realised_by`` (``requirements``): one ``{"layer", "element", "member",
      "kind", "stated"}`` per link site naming the requirement -- ``member`` the
      port or runnable holding it, ``None`` for an element's own list, ``kind``
      :func:`~swcraft_tools.trace.site_kind`'s vocabulary, ``stated`` the version
      the member states, ``None`` for a bare id and for a malformed suffix (still
      a site). A requirement nothing realises has no row.
    - ``allocation`` (``requirements``): ``{"sites", "elements", "allocated"}``
      -- the number of ``realised_by`` rows, the distinct top-level elements among them
      (a port's or a runnable's site counts as its component's, by layer and
      name), and :func:`requirement_allocated` over them.
    - ``stale_links`` (``requirements``): ``realised_by``'s row with ``current``
      added, for each site :func:`stale_link` classifies -- the sites
      ``trace.link-needs-confirmation`` and ``trace.link-version-not-stated``
      report, their abstentions kept.

    **What it does not reach:** a requirement's ``derived_from`` members, which are
    no link, and a requirement whose id two files declare has no ``stale_links`` row.
    """
    arms: dict[str, Callable[[Mapping[str, Any]], Any]] = {}
    if not {"realised_by", "allocation", "stale_links"} & set(asked):
        return arms

    requirements: list[Requirement] = list(layers.get("requirements", []))
    versions = requirement_versions(requirements)
    stubs = requirement_stubs(requirements)
    realised: dict[str, list[_Realisation]] = {}
    stale: dict[str, list[tuple[_Realisation, int]]] = {}
    seen: set[tuple[str, _Realisation]] = set()
    seen_stale: set[tuple[str, _Realisation]] = set()
    for layer, element, site in link_sites_of_state(layers):
        if is_element_form(site.value):
            continue
        parsed = parse_link(site.value)
        member = None if site.owner is None else site.owner[1]
        found = _Realisation(layer, element.name, member, site_kind(layer, element, site), parsed.version)
        if (parsed.id, found) not in seen:
            seen.add((parsed.id, found))
            realised.setdefault(parsed.id, []).append(found)
        # `R@x` and a bare `R` share one row (`stated` None), and only the bare one is stale.
        classified = stale_link(site.value, versions)
        if classified is not None and (classified.requirement_id, found) not in seen_stale:
            seen_stale.add((classified.requirement_id, found))
            stale.setdefault(classified.requirement_id, []).append((found, classified.current))

    def _site_row(realisation: _Realisation) -> dict[str, Any]:
        # Key by key: the coverage oracle abstains on a `**` spread.
        return {
            "layer": realisation.layer,
            "element": realisation.element,
            "member": realisation.member,
            "kind": realisation.kind,
            "stated": realisation.stated,
        }

    def realised_by(row: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [_site_row(realisation) for realisation in realised.get(row["element"], [])]

    def allocation(row: Mapping[str, Any]) -> dict[str, Any]:
        sites = realised.get(row["element"], [])
        return {
            "sites": len(sites),
            "elements": len({(realisation.layer, realisation.element) for realisation in sites}),
            "allocated": requirement_allocated(row["element"], realised.keys(), stubs),
        }

    def stale_links(row: Mapping[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for realisation, current in stale.get(row["element"], []):
            entry = _site_row(realisation)
            entry["current"] = current
            rows.append(entry)
        return rows

    arms["realised_by"] = realised_by
    arms["allocation"] = allocation
    arms["stale_links"] = stale_links
    return arms
