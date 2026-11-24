"""The composition rules, the external stubs' rules and the port interface references.

Holds :func:`check_composition_consistency` (the ``composition.*`` structure rules and the
``swc.external-*`` stub rules), :func:`check_port_interface_references` (``port.undeclared-interface``,
``composition.port-undeclared-interface``, ``interface.external-incomplete-reference``), the delegation
clause choice :func:`delegation_context`, and the external interface stub's member lists.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from swcraft_tools.data_description import DataDescription
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.scheduling_identity import composition_index as _composition_index
from swcraft_tools.schema import (
    STUB_MEMBER_DEST,
    Composition,
    CompositionMember,
    CompositionPort,
    ExternalInterface,
    Interface,
    Port,
    SWComponent,
)
from swcraft_tools.validator.compatibility import interface_compatibility, interface_index
from swcraft_tools.validator.connectors import COMPOSITION_LAYER, endpoint_type_field
from swcraft_tools.validator.data_types import REF_PATH, external_pair_problems, own_package_problem
from swcraft_tools.validator.description_elements import article, duplicates
from swcraft_tools.validator.model_view import ModelInput, ModelView, as_view
from swcraft_tools.validator.results import ConsistencyIssue, ConsistencyResult, consistency_result, deduplicated
from swcraft_tools.validator.rules import EXTERNAL_INTERFACE_DESTS


def delegation_context(outer_direction: str) -> str:
    """Which of the two delegation clauses an outer port's direction selects.

    ``subset`` for a ``require`` delegation (``[constr_1070]``/``[constr_1080]``
    1(a)) and ``shared-member`` for a ``provide`` one (clause 2(a)). The validator
    and the writer both call it so they choose the same clause.
    """
    return "subset" if outer_direction == "require" else "shared-member"


#: The shape of an AUTOSAR reference ``DEST``: an upper-case, hyphen-separated
#: token, not the enumeration, since a stub's ``DEST`` is copied verbatim and a
#: tool export can carry values this exporter never generates.
DEST_TOKEN = re.compile(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*")


def _member_type_field(member: CompositionMember) -> tuple[str, str]:
    """``(field, type name)`` of what *member* instantiates.

    ``type`` and ``composition`` spell the AUTOSAR ``TYPE-TREF``'s ``DEST``; a
    loaded member sets exactly one, so this is total.
    """
    if member.type is not None:
        return "type", member.type
    return "composition", str(member.composition)


def _type_ports(
    field: str,
    name: str,
    swc_map: Mapping[str, SWComponent],
    composition_map: Mapping[str, Composition],
) -> dict[str, Port | CompositionPort] | None:
    """The declared ports of the component type *name*, or ``None`` when it does not resolve.

    ``None`` (another rule's finding; abstain) differs from ``{}`` (a type
    declaring no port).
    """
    if field == "type":
        swc = swc_map.get(name)
        return None if swc is None else {port.name: port for port in swc.ports}
    composition = composition_map.get(name)
    return None if composition is None else {port.name: port for port in composition.ports}


def check_composition_consistency(model_root: ModelInput) -> ConsistencyResult:
    """Check the ``compositions/`` layer's shape and everything that resolves into it.

    Mints ``composition.duplicate-name``, ``composition.no-root``, ``composition.multiple-roots``,
    ``composition.duplicate-port``, ``composition.duplicate-member``, ``composition.duplicate-delegation``,
    ``composition.duplicate-delegation-link``, ``composition.member-unknown-type``,
    ``composition.member-unknown-composition``, ``composition.member-type-repeated``, ``composition.cycle``,
    ``composition.delegation-unknown-member``, ``composition.delegation-unknown-port``,
    ``composition.delegation-port-direction-mismatch``, ``composition.delegation-interface-mismatch``,
    ``composition.connector-endpoint-not-a-member``, ``composition.delegation-connector-name-collision``,
    ``swc.external-with-ports``, ``swc.external-incomplete-reference``, ``swc.external-reference-without-stub`` and
    ``swc.external-reference-inside-generated-package``; the model contract describes each.

    Beyond the contract: a missing root is reported even for an empty layer; every composition on a cycle is a
    subject; the interface rule is judged only where the directions agree, by :func:`interface_compatibility` with
    the inner port as the required side; a connector naming no composition is checked against the root. A rule
    abstains where another owns the finding. Not checked: a boundary port's ``interface`` reference, name
    collisions with components, ``SHORT-NAME`` legality, reachability from the root, and delegation fan-out.

    Args:
        model_root: The model root, or a view of it.

    Returns:
        ConsistencyResult with consistent=True only when all checks pass.
    """
    view = as_view(model_root)
    index = _build_composition_index(view)
    interface_layers = _LazyInterfaceLayers(view)

    records = _composition_identity_records(index)
    for composition in index.compositions:
        records.extend(_sibling_duplicate_records(composition))
        records.extend(_delegation_link_records(composition))
        records.extend(_member_type_records(composition, index))
        records.extend(_delegation_records(composition, index, interface_layers))
    records.extend(_composition_cycle_records(index))
    endpoint_records, drawn_inside = _connector_endpoint_records(view, index)
    records.extend(endpoint_records)
    records.extend(_delegation_connector_collision_records(index, drawn_inside))
    for swc in index.swcs:
        if not swc.external:
            records.extend(_stray_reference_records(swc))
            continue
        records.extend(_stub_declaration_records(swc))
        records.extend(_stub_reference_records(swc))
    return consistency_result(deduplicated(records))


# ---------------------------------------------------------------------------
# check_composition_consistency's phases
# ---------------------------------------------------------------------------
#
# Each phase holds the literal of every rule it emits: `binding.json` binds each
# rule to the phase that mints it.


@dataclass(frozen=True)
class _CompositionIndex:
    """What :func:`check_composition_consistency`'s phases resolve against, built before its first finding.

    Attributes:
        compositions: the ``compositions`` layer, in reader order.
        composition_map: the compositions by name, deduplicated.
        swcs: the ``swc`` layer, in reader order.
        swc_map: the components by name.
        roots: the names of the compositions declaring ``root: true``, deduplicated in reader order,
            so a name declared in two files is one claimant.
    """

    compositions: Sequence[Composition]
    composition_map: Mapping[str, Composition]
    swcs: Sequence[SWComponent]
    swc_map: Mapping[str, SWComponent]
    roots: Sequence[str]


def _build_composition_index(view: ModelView) -> _CompositionIndex:
    """Read the two layers :func:`check_composition_consistency` starts from and index them."""
    compositions = view.compositions
    swcs = view.swcs
    return _CompositionIndex(
        compositions=compositions,
        composition_map=_composition_index(compositions),
        swcs=swcs,
        swc_map={swc.name: swc for swc in swcs},
        roots=list(dict.fromkeys(composition.name for composition in compositions if composition.root)),
    )


class _LazyInterfaceLayers:
    """The ``interfaces`` index and the data description, read at most once and only when first asked for.

    Only a delegation whose two ports name different interfaces asks.
    """

    def __init__(self, view: ModelView) -> None:
        self._view = view
        self._layers: tuple[dict[str, Interface], DataDescription] | None = None

    def layers(self) -> tuple[dict[str, Interface], DataDescription]:
        """``(interfaces by name, data description)``, read on the first call."""
        if self._layers is None:
            self._layers = (interface_index(self._view.interfaces), self._view.data_description)
        return self._layers


def _composition_identity_records(index: _CompositionIndex) -> list[ConsistencyIssue]:
    """Checks 1 and 2: one identity per name across files, and exactly one root."""
    records: list[ConsistencyIssue] = []
    for name in duplicates([composition.name for composition in index.compositions]):
        records.append(
            ConsistencyIssue(
                rule="composition.duplicate-name",
                message=f"Duplicate composition name '{name}' declared in multiple files.",
                subjects=(Subject(COMPOSITION_LAYER, name),),
            )
        )
    roots = index.roots
    if not roots:
        records.append(
            ConsistencyIssue(
                rule="composition.no-root",
                message=(
                    "No composition declares 'root: true'; exactly one must, because it is the top of the "
                    "emitted hierarchy and the composition a connector that names none belongs to."
                ),
                subjects=(Subject(COMPOSITION_LAYER),),
            )
        )
    elif len(roots) > 1:
        records.append(
            ConsistencyIssue(
                rule="composition.multiple-roots",
                message=(
                    f"Compositions {', '.join(sorted(roots))} all declare 'root: true'; exactly one may, "
                    "because the root is the top of the emitted hierarchy."
                ),
                # Every claimant, so the client can choose which keeps the flag.
                subjects=tuple(Subject(COMPOSITION_LAYER, name) for name in sorted(roots)),
            )
        )
    return records


def _sibling_duplicate_records(composition: Composition) -> list[ConsistencyIssue]:
    """Check 3: two boundary ports, two members or two delegations of one composition sharing a name.

    All three are sibling SHORT-NAMEs of one COMPOSITION-SW-COMPONENT-TYPE, in
    its PORTS, COMPONENTS and CONNECTORS scopes.
    """
    composition_element = Subject(COMPOSITION_LAYER, composition.name)
    records: list[ConsistencyIssue] = []
    for name in duplicates([port.name for port in composition.ports]):
        records.append(
            ConsistencyIssue(
                rule="composition.duplicate-port",
                message=f"Composition '{composition.name}' declares boundary port '{name}' more than once.",
                subjects=(composition_element.at(field_step("ports"), member_step(name)),),
            )
        )
    for name in duplicates([member.name for member in composition.members]):
        records.append(
            ConsistencyIssue(
                rule="composition.duplicate-member",
                message=f"Composition '{composition.name}' declares member '{name}' more than once.",
                subjects=(composition_element.at(field_step("members"), member_step(name)),),
            )
        )
    for name in duplicates([delegation.name for delegation in composition.delegations]):
        records.append(
            ConsistencyIssue(
                rule="composition.duplicate-delegation",
                message=f"Composition '{composition.name}' declares delegation '{name}' more than once.",
                subjects=(composition_element.at(field_step("delegations"), member_step(name)),),
            )
        )
    return records


def _delegation_link_records(composition: Composition) -> list[ConsistencyIssue]:
    """Check 3's second key over the delegations: what a delegation WIRES, not what it is called.

    Two delegations differing only in ``name`` emit two ``DELEGATION-SW-CONNECTOR``s
    identical but for their ``SHORT-NAME``, which the licensed XSD accepts at
    4.2.2 and at 4.3.0. The later claimant is the subject. One triple in two
    compositions is legal.
    """
    composition_element = Subject(COMPOSITION_LAYER, composition.name)
    records: list[ConsistencyIssue] = []
    seen_delegation_links: dict[tuple[str, str, str], str] = {}
    for delegation in composition.delegations:
        link = (delegation.port, delegation.member, delegation.member_port)
        first = seen_delegation_links.get(link)
        if first is not None:
            records.append(
                ConsistencyIssue(
                    rule="composition.duplicate-delegation-link",
                    message=(
                        f"Composition '{composition.name}' delegation '{delegation.name}' joins "
                        f"'{delegation.port}' to '{delegation.member}.{delegation.member_port}', which "
                        f"delegation '{first}' already joins; a delegation is identified by what it "
                        "wires, and two of them emit the same DELEGATION-SW-CONNECTOR twice."
                    ),
                    subjects=(composition_element.at(field_step("delegations"), member_step(delegation.name)),),
                )
            )
            continue
        seen_delegation_links[link] = delegation.name
    return records


def _member_type_records(composition: Composition, index: _CompositionIndex) -> list[ConsistencyIssue]:
    """Checks 4 and 5: the member's type field and what it resolves to, then one type per composition."""
    composition_element = Subject(COMPOSITION_LAYER, composition.name)
    records: list[ConsistencyIssue] = []
    placements: dict[tuple[str, str], list[str]] = {}
    for member in composition.members:
        member_address = composition_element.at(field_step("members"), member_step(member.name))
        field_name, type_name = _member_type_field(member)
        placements.setdefault((field_name, type_name), []).append(member.name)
        if field_name == "type" and type_name not in index.swc_map:
            records.append(
                ConsistencyIssue(
                    rule="composition.member-unknown-type",
                    message=(
                        f"Composition '{composition.name}' member '{member.name}' names type "
                        f"'{type_name}', which does not exist in model/swc/."
                    ),
                    subjects=(member_address.at(field_step("type")),),
                    target=type_name,
                )
            )
        elif field_name == "composition" and type_name not in index.composition_map:
            records.append(
                ConsistencyIssue(
                    rule="composition.member-unknown-composition",
                    message=(
                        f"Composition '{composition.name}' member '{member.name}' names composition "
                        f"'{type_name}', which does not exist in model/compositions/."
                    ),
                    subjects=(member_address.at(field_step("composition")),),
                    target=type_name,
                )
            )

    # Check 5: one type per composition.
    for (_field_name, type_name), member_names in placements.items():
        if len(member_names) > 1:
            records.append(
                ConsistencyIssue(
                    rule="composition.member-type-repeated",
                    message=(
                        f"Composition '{composition.name}' places type '{type_name}' as members "
                        f"{', '.join(member_names)}; a type may be placed at most once per composition, "
                        "because a connector endpoint names the type and is resolved to its member."
                    ),
                    # Every member of the repeated type: the repair drops all
                    # but one of them, and no single one is the offender.
                    subjects=tuple(
                        composition_element.at(field_step("members"), member_step(name)) for name in member_names
                    ),
                )
            )
    return records


def _delegation_records(
    composition: Composition, index: _CompositionIndex, interface_layers: _LazyInterfaceLayers
) -> list[ConsistencyIssue]:
    """Check 7: each delegation's two ports resolve, face the same way and carry compatible interfaces."""
    composition_element = Subject(COMPOSITION_LAYER, composition.name)
    records: list[ConsistencyIssue] = []
    members_by_name = {member.name: member for member in composition.members}
    outer_ports = {port.name: port for port in composition.ports}
    for delegation in composition.delegations:
        delegation_address = composition_element.at(field_step("delegations"), member_step(delegation.name))
        outer = outer_ports.get(delegation.port)
        if outer is None:
            records.append(
                ConsistencyIssue(
                    rule="composition.delegation-unknown-port",
                    message=(
                        f"Delegation '{delegation.name}' of composition '{composition.name}' names port "
                        f"'{delegation.port}', which that composition does not declare."
                    ),
                    subjects=(delegation_address.at(field_step("port")),),
                    target=delegation.port,
                )
            )
        member = members_by_name.get(delegation.member)
        if member is None:
            records.append(
                ConsistencyIssue(
                    rule="composition.delegation-unknown-member",
                    message=(
                        f"Delegation '{delegation.name}' of composition '{composition.name}' names member "
                        f"'{delegation.member}', which that composition does not declare."
                    ),
                    subjects=(delegation_address.at(field_step("member")),),
                    target=delegation.member,
                )
            )
            continue  # the member's type is what the inner port is looked up on
        typed = _member_type_field(member)
        inner_ports = _type_ports(typed[0], typed[1], index.swc_map, index.composition_map)
        if inner_ports is None:
            continue  # check 4 owns the unresolvable type
        inner = inner_ports.get(delegation.member_port)
        if inner is None:
            records.append(
                ConsistencyIssue(
                    rule="composition.delegation-unknown-port",
                    message=(
                        f"Delegation '{delegation.name}' of composition '{composition.name}' names port "
                        f"'{delegation.member_port}' on member '{member.name}' of type '{typed[1]}', "
                        "which that type does not declare."
                    ),
                    subjects=(delegation_address.at(field_step("member_port")),),
                    target=f"{typed[1]}.{delegation.member_port}",
                )
            )
        if outer is not None and inner is not None:
            if outer.direction != inner.direction:
                records.append(
                    ConsistencyIssue(
                        rule="composition.delegation-port-direction-mismatch",
                        message=(
                            f"Delegation '{delegation.name}' of composition '{composition.name}' joins "
                            f"'{delegation.port}' ({outer.direction}) to "
                            f"'{member.name}.{delegation.member_port}' ({inner.direction}); a delegation's "
                            "two ports must declare the same direction."
                        ),
                        # Both ports: either one is a legal repair.
                        subjects=(
                            delegation_address.at(field_step("port")),
                            delegation_address.at(field_step("member_port")),
                        ),
                    )
                )
            elif outer.interface != inner.interface and outer.interface and inner.interface:
                # `elif`: a direction mismatch's repair decides the interface question too, and
                # agreeing directions let the outer port's direction name the clause. The inner
                # port is the required side of both clauses -- `[constr_1070]`/`[constr_1080]`
                # 1(a) for a `require` delegation, clause 2(a) for a `provide` one.
                interface_map, data_description = interface_layers.layers()
                incompatibility = interface_compatibility(
                    inner.interface,
                    outer.interface,
                    context=delegation_context(outer.direction),
                    interfaces=interface_map,
                    data_types=data_description,
                )
                if incompatibility is not None:
                    records.append(
                        ConsistencyIssue(
                            rule="composition.delegation-interface-mismatch",
                            message=(
                                f"Delegation '{delegation.name}' of composition '{composition.name}' joins "
                                f"'{delegation.port}' ({outer.interface}) to "
                                f"'{member.name}.{delegation.member_port}' ({inner.interface}): "
                                f"{incompatibility.subject}."
                            ),
                            # Both ports, for the direction rule's reason.
                            subjects=(
                                delegation_address.at(field_step("port")),
                                delegation_address.at(field_step("member_port")),
                            ),
                        )
                    )
    return records


def _walk_members(
    start: str, path: list[str], graph: Mapping[str, list[str]], colour: dict[str, str], cycles: list[list[str]]
) -> None:
    """Depth-first from *start*, appending to *cycles* each cycle path a grey neighbour closes."""
    colour[start] = "gray"
    for neighbour in graph.get(start, []):
        if colour.get(neighbour) == "gray":
            cycles.append(path[path.index(neighbour) :] + [neighbour])
        elif colour.get(neighbour, "white") == "white":
            path.append(neighbour)
            _walk_members(neighbour, path, graph, colour, cycles)
            path.pop()
    colour[start] = "black"


def _composition_cycle_records(index: _CompositionIndex) -> list[ConsistencyIssue]:
    """Check 6: the hierarchy is acyclic.

    Every composition on a cycle is a subject, since no single one is the
    offender. Only resolvable ``composition``-typed members are edges.
    """
    graph: dict[str, list[str]] = {
        name: [
            str(member.composition)
            for member in composition.members
            if member.composition is not None and str(member.composition) in index.composition_map
        ]
        for name, composition in index.composition_map.items()
    }
    colour: dict[str, str] = {}
    cycles: list[list[str]] = []
    for name in sorted(graph):
        if colour.get(name, "white") == "white":
            _walk_members(name, [name], graph, colour, cycles)
    return [
        ConsistencyIssue(
            rule="composition.cycle",
            message=f"Composition cycle detected: {' -> '.join(cycle_path)}",
            subjects=tuple(Subject(COMPOSITION_LAYER, name) for name in dict.fromkeys(cycle_path)),
        )
        for cycle_path in cycles
    ]


def _connector_endpoint_records(
    view: ModelView, index: _CompositionIndex
) -> tuple[list[ConsistencyIssue], dict[str, list[str]]]:
    """Check 8: every connector endpoint names a type its connector's composition places.

    A connector naming no composition belongs to the root. Abstains where the
    owner or the endpoint's type does not resolve (other rules report those).

    Returns:
        The findings, and the names of the connectors drawn inside each
        composition, which check 9 compares the delegations with.
    """
    roots = index.roots
    root = (
        next((composition for composition in index.compositions if composition.root), None) if len(roots) == 1 else None
    )
    records: list[ConsistencyIssue] = []
    drawn_inside: dict[str, list[str]] = {}
    for connector in view.connectors:
        connector_element = Subject("connectors", connector.name)
        owner = index.composition_map.get(connector.composition) if connector.composition is not None else root
        if owner is None:
            continue
        drawn_inside.setdefault(owner.name, []).append(connector.name)
        placed = {_member_type_field(member) for member in owner.members}
        for side, endpoint in (("from", connector.from_), ("to", connector.to)):
            endpoint_field, endpoint_name = endpoint_type_field(endpoint)
            if endpoint_name not in (index.composition_map if endpoint_field == "composition" else index.swc_map):
                continue  # the connector pass reports the unresolvable endpoint
            if ("composition" if endpoint_field == "composition" else "type", endpoint_name) in placed:
                continue
            records.append(
                ConsistencyIssue(
                    rule="composition.connector-endpoint-not-a-member",
                    message=(
                        f"Connector '{connector.name}' {side} endpoint names type '{endpoint_name}', which "
                        f"is not a member of composition '{owner.name}' the connector is drawn inside."
                    ),
                    subjects=(connector_element.at(field_step(side)),),
                )
            )
    return records, drawn_inside


def _delegation_connector_collision_records(
    index: _CompositionIndex, drawn_inside: Mapping[str, list[str]]
) -> list[ConsistencyIssue]:
    """Check 9: a delegation and an assembly connector drawn inside one composition share no name.

    They are siblings in the composition's single CONNECTORS block. The subject
    is the delegation, declared in the composition file that holds the ambiguity.
    """
    records: list[ConsistencyIssue] = []
    for composition in index.compositions:
        connector_names = set(drawn_inside.get(composition.name, ()))
        composition_element = Subject(COMPOSITION_LAYER, composition.name)
        for delegation in composition.delegations:
            if delegation.name not in connector_names:
                continue
            records.append(
                ConsistencyIssue(
                    rule="composition.delegation-connector-name-collision",
                    message=(
                        f"Delegation '{delegation.name}' of composition '{composition.name}' has the name of "
                        f"a connector drawn inside it; a DELEGATION-SW-CONNECTOR and an "
                        f"ASSEMBLY-SW-CONNECTOR are siblings in one CONNECTORS block and cannot share a name."
                    ),
                    subjects=(composition_element.at(field_step("delegations"), member_step(delegation.name)),),
                    target=delegation.name,
                )
            )
    return records


def _stray_reference_records(swc: SWComponent) -> list[ConsistencyIssue]:
    """Check 10 for a component that is not a stub: the reference pair it must not carry.

    Without the flag the pair is never emitted, so the author's expected reference would be silently dropped.
    """
    stray = [
        name
        for name, value in (("external_ref", swc.external_ref), ("external_dest", swc.external_dest))
        if value.strip()
    ]
    if not stray:
        return []
    swc_element = Subject("swc", swc.name)
    return [
        ConsistencyIssue(
            rule="swc.external-reference-without-stub",
            message=(
                f"SWC '{swc.name}' declares {' and '.join(stray)} without 'external: true'; "
                "the reference pair is only emitted for an external stub, so nothing reads it here."
            ),
            subjects=tuple(swc_element.at(field_step(name)) for name in stray),
        )
    ]


def _stub_declaration_records(swc: SWComponent) -> list[ConsistencyIssue]:
    """Check 10 for an external stub: what it may not declare.

    A stub emits no component type or behaviour, so it may not declare
    ``runnables``, ``variables``, ``exclusive_areas``, ``data_type_mappings``,
    ``handle_termination_and_restart``, ``behavior_name``, ``memory`` or
    ``package``. Its ports carry a name and a direction only, never an
    ``interface``, ``measures`` or ``com_specs``; ``observed_interface`` is not read.

    Attributes are read by name, never ``getattr``, so the coverage oracle can bind each read.
    """
    offending_ports = [port for port in swc.ports if port.interface is not None or port.measures or port.com_specs]
    declared = [
        name
        for name, members in (
            ("ports", offending_ports),
            ("runnables", swc.runnables),
            ("variables", swc.variables),
            ("exclusive_areas", swc.exclusive_areas),
            ("memory", swc.memory),
            ("data_type_mappings", swc.data_type_mappings),
            ("handle_termination_and_restart", swc.handle_termination_and_restart),
            ("package", swc.package),
            ("behavior_name", swc.behavior_name),
        )
        if members
    ]
    if not declared:
        return []
    swc_element = Subject("swc", swc.name)
    port_detail = ", ".join(
        f"port '{port.name}' declares "
        + " and ".join(
            what
            for what, present in (
                ("an interface", port.interface is not None),
                ("measures", port.measures),
                ("com_specs", port.com_specs),
            )
            if present
        )
        for port in offending_ports
    )
    return [
        ConsistencyIssue(
            rule="swc.external-with-ports",
            message=(
                f"SWC '{swc.name}' declares 'external: true' and also {' and '.join(declared)}; "
                "an external stub is a reference the exporter writes verbatim and emits no component "
                "type, so nothing it declares would be emitted."
                + (
                    " A stub's port is a name and a direction the connectors to it reference, and its "
                    f"type is outside the project: {port_detail}."
                    if offending_ports
                    else ""
                )
            ),
            subjects=tuple(swc_element.at(field_step(name)) for name in declared),
        )
    ]


def _stub_reference_records(swc: SWComponent) -> list[ConsistencyIssue]:
    """Check 10 for an external stub: the reference pair it must carry, and its shape.

    Refuses a blank half, an ``external_ref`` that is not a reference path or
    lies inside the generated package, and an ``external_dest`` outside the
    REF-DEST token grammar (not the enumeration: see ``DEST_TOKEN``). A
    malformed value is otherwise caught only where a licensed XSD is installed.
    """
    swc_element = Subject("swc", swc.name)
    missing = [
        name
        for name, value in (("external_ref", swc.external_ref), ("external_dest", swc.external_dest))
        if not value.strip()
    ]
    if missing:
        return [
            ConsistencyIssue(
                rule="swc.external-incomplete-reference",
                message=(
                    f"SWC '{swc.name}' declares 'external: true' and leaves {' and '.join(missing)} "
                    "blank; a stub is emitted as its source's TYPE-TREF and DEST verbatim, and both "
                    "must be there."
                ),
                subjects=tuple(swc_element.at(field_step(name)) for name in missing),
            )
        ]
    # The two shapes are independent findings: a document can be wrong in both.
    records: list[ConsistencyIssue] = []
    if not REF_PATH.fullmatch(swc.external_ref):
        records.append(
            ConsistencyIssue(
                rule="swc.external-incomplete-reference",
                message=(
                    f"SWC '{swc.name}' declares external_ref '{swc.external_ref}', which is not an "
                    "AUTOSAR reference path: a reference is Identifier-shaped segments joined by "
                    "'/', such as '/Foreign/Pkg/ComponentTypes/Stub'."
                ),
                subjects=(swc_element.at(field_step("external_ref")),),
                target=swc.external_ref,
            )
        )
    elif (inside := own_package_problem(swc.external_ref)) is not None:
        records.append(
            ConsistencyIssue(
                rule="swc.external-reference-inside-generated-package",
                message=f"SWC '{swc.name}' {inside}.",
                subjects=(swc_element.at(field_step("external_ref")),),
                target=swc.external_ref,
            )
        )
    if not DEST_TOKEN.fullmatch(swc.external_dest):
        records.append(
            ConsistencyIssue(
                rule="swc.external-incomplete-reference",
                message=(
                    f"SWC '{swc.name}' declares external_dest '{swc.external_dest}', which is not an "
                    "AUTOSAR reference DEST: a DEST is an upper-case, hyphen-separated token such as "
                    "'APPLICATION-SW-COMPONENT-TYPE' or 'COMPOSITION-SW-COMPONENT-TYPE'."
                ),
                subjects=(swc_element.at(field_step("external_dest")),),
                target=swc.external_dest,
            )
        )
    return records


# ---------------------------------------------------------------------------
# check_port_interface_references
# ---------------------------------------------------------------------------


def check_port_interface_references(model_root: ModelInput) -> ConsistencyResult:
    """Check that every SWC port names an interface declared in ``interfaces/``.

    Rules:
    - ``port.undeclared-interface`` — a port of a component that is not an
      external stub names an undeclared interface, or none.
    - ``composition.port-undeclared-interface`` — the same for a boundary port.
    - ``interface.external-incomplete-reference`` — an external interface stub
      (which a port may name) has a blank half, a path the schemas' ``Ref``
      grammar refuses, or a ``DEST`` outside :data:`EXTERNAL_INTERFACE_DESTS`;
      reported once, on the stub.

    The ports of an external component stub are skipped (``swc.external-with-ports``
    owns them). Unconnected ports are covered too, which the connector check does not reach.

    Args:
        model_root: The model root, or a view of it.

    Returns:
        ConsistencyResult with consistent=True only when every port's interface
        is declared.
    """
    view = as_view(model_root)
    swcs = view.swcs
    interfaces = view.interfaces
    declared = {iface.name for iface in interfaces}

    # `interface` is optional at load and required here; a port naming none gets no `target`.
    records = [
        ConsistencyIssue(
            rule="port.undeclared-interface",
            message=(
                f"SWC '{swc.name}' port '{port.name}' references interface '{port.interface}' "
                "which is not declared in model/interfaces/."
                if port.interface is not None
                else f"SWC '{swc.name}' port '{port.name}' declares no interface; a port of a component that is "
                "not an external stub must name one declared in model/interfaces/."
            ),
            # The referrer, never the missing interface: every port naming it has to change.
            subjects=(
                Subject("swc", swc.name, (field_step("ports"), member_step(port.name), field_step("interface"))),
            ),
            target=port.interface,
        )
        for swc in swcs
        if not swc.external
        for port in swc.ports
        if port.interface not in declared
    ]
    # A rule of its own: it is reported at another layer than `port.undeclared-interface`.
    records.extend(
        ConsistencyIssue(
            rule="composition.port-undeclared-interface",
            message=(
                f"Composition '{composition.name}' boundary port '{port.name}' references interface "
                f"'{port.interface}' which is not declared in model/interfaces/."
            ),
            subjects=(
                Subject(
                    COMPOSITION_LAYER,
                    composition.name,
                    (field_step("ports"), member_step(port.name), field_step("interface")),
                ),
            ),
            target=port.interface,
        )
        for composition in view.compositions
        for port in composition.ports
        if port.interface not in declared
    )
    # The `DEST` is checked as a set, not a token grammar: a port interface admits few, known values.
    for iface in interfaces:
        if isinstance(iface, ExternalInterface):
            stub = Subject("interfaces", iface.name)
            for problem, fields in external_pair_problems(
                iface.external_ref, iface.external_dest, EXTERNAL_INTERFACE_DESTS, "a port interface DEST"
            ):
                records.append(
                    ConsistencyIssue(
                        rule="interface.external-incomplete-reference",
                        message=f"External interface stub '{iface.name}' {problem}.",
                        subjects=tuple(stub.at(field_step(name)) for name in fields),
                    )
                )
    return consistency_result(records)


#: For a member field (``operation`` or ``element``): the external interface
#: stub's list, its noun, and the one ``DEST`` that admits the list, read off
#: ``schema.STUB_MEMBER_DEST`` so the schema and this table cannot disagree.
STUB_MEMBER_LISTS: dict[str, tuple[str, str, str]] = {
    "operation": ("operations", "operation", STUB_MEMBER_DEST["operations"]),
    "element": ("data_elements", "data element", STUB_MEMBER_DEST["data_elements"]),
}


def stub_member_names(stub: ExternalInterface, member_field: str) -> frozenset[str]:
    """The names *stub* lists for *member_field*: its ``operations`` or its ``data_elements``.

    A stub holds names only. A stub whose ``DEST`` admits neither list lists nothing.
    """
    return frozenset(stub_member_list(stub, member_field))


def stub_member_list(stub: ExternalInterface, member_field: str) -> list[str]:
    """*stub*'s list for *member_field* as it stands, in its own order.

    Read by attribute name, never ``getattr``, so the coverage oracle can bind the read.
    """
    if member_field == "operation":
        return stub.operations
    return stub.data_elements


def stub_member_miss(stub: ExternalInterface, member_field: str, member: str) -> str:
    """Why *member* resolves to nothing through *stub*, with the repair: one clause, no final stop.

    A stub of any other ``DEST`` than the admitting one is told it lists no such
    member. The member kind across a stub is not judged.
    """
    field, noun, admitting = STUB_MEMBER_LISTS[member_field]
    if stub.external_dest != admitting:
        return (
            f"external interface stub '{stub.name}' has external_dest '{stub.external_dest}', which lists no "
            f"{noun}: only a {admitting} stub lists `{field}`"
        )
    return (
        f"external interface stub '{stub.name}' does not list {noun} '{member}' in its `{field}`: list it there, "
        f"or name {article(noun)} the stub lists"
    )
