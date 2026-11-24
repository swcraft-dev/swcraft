"""The connector rules: endpoint resolution, port compatibility and duplicate links.

Holds :func:`check_connector_compatibility` (one connector: ``connector.from-not-provide``,
``connector.to-not-require``, ``connector.interface-mismatch``), :func:`check_connector_consistency`
(the whole layer: the endpoint, composition, duplicate and ``[constr_1295]`` pointer rules) and the
endpoint helpers the composition checks share.
"""

from __future__ import annotations

from collections.abc import Mapping

from swcraft_tools.data_description import DataDescription
from swcraft_tools.diagnostics import Subject, field_step
from swcraft_tools.scheduling_identity import composition_index as _composition_index
from swcraft_tools.schema import (
    Composition,
    CompositionPort,
    Connector,
    ConnectorEndpoint,
    CSArgument,
    CSInterface,
    DataElement,
    Interface,
    PointerDataType,
    Port,
    SRInterface,
    SWComponent,
)
from swcraft_tools.validator.compatibility import interface_compatibility, interface_index, interface_mismatch_reason
from swcraft_tools.validator.description_elements import chain_text, implementation_end
from swcraft_tools.validator.model_view import ModelInput, as_view
from swcraft_tools.validator.results import CompatibilityResult, ConsistencyIssue, ConsistencyResult, consistency_result


class _UnresolvedEndpoint(ValueError):
    """A connector endpoint :func:`_resolve_endpoint_port` could not resolve.

    A ``ValueError``, so a caller catching that sees only the message; the
    attributes address the value for the layer check.

    Attributes:
        side: ``from`` or ``to`` -- the connector field the subject starts at.
        field: ``swc`` / ``composition`` / ``port`` -- the endpoint field the
            subject ends at, and the key of :data:`_UNRESOLVED_ENDPOINT_RULES`.
        target: the value that did not resolve, for the issue's ``target``.
    """

    def __init__(self, side: str, field: str, target: str, message: str) -> None:
        super().__init__(message)
        self.side = side
        self.field = field
        self.target = target


def _resolve_endpoint_port(
    endpoint: ConnectorEndpoint,
    swc_map: dict[str, SWComponent],
    composition_map: dict[str, Composition],
    side: str = "from",
) -> Port | CompositionPort:
    """Resolve a connector endpoint's port, whichever layer its type lives in.

    An endpoint names a component (``swc``) or a composition, the model's
    spelling of the ``TYPE-TREF``'s ``DEST``; exactly one is set on a loaded element.

    Args:
        endpoint: the endpoint to resolve.
        swc_map: the ``swc`` layer by name.
        composition_map: the ``compositions`` layer by name.
        side: ``from`` or ``to``, carried into the refusal's address.

    Raises:
        _UnresolvedEndpoint: the type or the port does not resolve.
    """
    if endpoint.swc is not None:
        if endpoint.swc not in swc_map:
            raise _UnresolvedEndpoint(side, "swc", endpoint.swc, f"SWC not found: {endpoint.swc}")
        ports: dict[str, Port | CompositionPort] = {port.name: port for port in swc_map[endpoint.swc].ports}
        if endpoint.port not in ports:
            raise _UnresolvedEndpoint(
                side,
                "port",
                f"{endpoint.swc}.{endpoint.port}",
                f"Port not found: {endpoint.port} on SWC {endpoint.swc}",
            )
        return ports[endpoint.port]
    name = str(endpoint.composition)
    if name not in composition_map:
        raise _UnresolvedEndpoint(side, "composition", name, f"Composition not found: {name}")
    ports = {port.name: port for port in composition_map[name].ports}
    if endpoint.port not in ports:
        raise _UnresolvedEndpoint(
            side,
            "port",
            f"{name}.{endpoint.port}",
            f"Port not found: {endpoint.port} on composition {name}",
        )
    return ports[endpoint.port]


def _endpoint_label(endpoint: ConnectorEndpoint) -> str:
    """``"{type}.{port}"`` for an endpoint, whichever of its two fields is set."""
    name = endpoint.swc if endpoint.swc is not None else str(endpoint.composition)
    return f"{name}.{endpoint.port}"


def check_connector_compatibility(
    model_root: ModelInput,
    connector: Connector,
    swc_map: dict[str, SWComponent] | None = None,
    composition_map: dict[str, Composition] | None = None,
    interface_map: dict[str, Interface] | None = None,
    data_description: DataDescription | None = None,
) -> CompatibilityResult:
    """Check whether a persisted connector is valid for port connection.

    Compatible means:
    - ``from`` references an existing provide port.
    - ``to`` references an existing require port.
    - the interface the ``to`` port **requires** is satisfied by the one the
      ``from`` port **provides**, which is :func:`interface_compatibility` in
      ``subset`` context — ``[constr_1069]`` 1(a) for a sender-receiver pair and
      ``[constr_1079]`` 1(a) for a client-server one. The same interface name
      passes with no read.

    An endpoint naming a composition resolves to that composition's boundary ``ports``.

    The maps are the ``swc``, ``compositions``, ``interfaces`` and implementation
    type layers, read-only; one left ``None`` is read here (the interface-side two
    only when the ports name different interfaces). A caller checking every
    connector passes them read once.

    Raises:
        ValueError: an endpoint's type or port does not resolve.
    """
    view = as_view(model_root)
    if swc_map is None:
        swc_map = {swc.name: swc for swc in view.swcs}
    if composition_map is None:
        composition_map = _composition_index(view.compositions)

    from_port = _resolve_endpoint_port(connector.from_, swc_map, composition_map, "from")
    to_port = _resolve_endpoint_port(connector.to, swc_map, composition_map, "to")

    if from_port.direction != "provide":
        return CompatibilityResult(
            compatible=False,
            reason=(
                f"Connector '{connector.name}' from endpoint {_endpoint_label(connector.from_)} must be a provide port."
            ),
            rule="connector.from-not-provide",
        )

    if to_port.direction != "require":
        return CompatibilityResult(
            compatible=False,
            reason=(
                f"Connector '{connector.name}' to endpoint {_endpoint_label(connector.to)} must be a require port."
            ),
            rule="connector.to-not-require",
        )

    if from_port.interface != to_port.interface and from_port.interface and to_port.interface:
        if interface_map is None:
            interface_map = interface_index(view.interfaces)
        if data_description is None:
            data_description = view.data_description
        incompatibility = interface_compatibility(
            to_port.interface,
            from_port.interface,
            context="subset",
            interfaces=interface_map,
            data_types=data_description,
        )
        if incompatibility is not None:
            return CompatibilityResult(
                compatible=False,
                reason=interface_mismatch_reason(
                    f"Connector '{connector.name}' interface mismatch",
                    to_port.interface,
                    _endpoint_label(connector.to),
                    from_port.interface,
                    _endpoint_label(connector.from_),
                    incompatibility.subject,
                ),
                rule="connector.interface-mismatch",
            )

    return CompatibilityResult(compatible=True, reason="")


#: The two endpoint sides, and the connector field each one's subject ends at.
_CONNECTOR_ENDPOINT_SUBJECTS: dict[str, tuple[str, ...]] = {
    "connector.from-not-provide": ("from",),
    "connector.to-not-require": ("to",),
    "connector.interface-mismatch": ("from", "to"),
}


#: The endpoint field a :class:`_UnresolvedEndpoint` names -> the rule that
#: reports it; an undeclared type is one rule per layer, so a finding names the right one.
_UNRESOLVED_ENDPOINT_RULES: dict[str, str] = {
    "swc": "connector.endpoint-unknown-swc",
    "composition": "connector.endpoint-unknown-composition",
    "port": "connector.endpoint-unknown-port",
}


def check_connector_consistency(model_root: ModelInput) -> ConsistencyResult:
    """Check all persisted connectors for valid endpoints and duplicate links.

    Reads the interface and data-type layers before the first connector, so an
    unparseable file there raises.
    """
    view = as_view(model_root)
    connectors = view.connectors
    records: list[ConsistencyIssue] = []
    seen_names: set[str] = set()
    seen_links: set[tuple[str | None, str, str, str, str, str, str]] = set()
    # Read each layer once so `check_connector_compatibility` never reads one per connector.
    swc_map = {swc.name: swc for swc in view.swcs}
    compositions = view.compositions
    composition_map = _composition_index(compositions)
    interface_map = interface_index(view.interfaces)
    data_description = view.data_description
    # The owner of a connector naming no composition, for the duplicate-link key; `None`
    # without exactly one root (`composition.no-root` / `composition.multiple-roots` report that).
    roots = [composition.name for composition in compositions if composition.root]
    root_name = roots[0] if len(roots) == 1 else None

    for connector in connectors:
        element = Subject("connectors", connector.name)

        # `None` means the root and is not judged here.
        if connector.composition is not None and connector.composition not in composition_map:
            records.append(
                ConsistencyIssue(
                    rule="connector.unknown-composition",
                    message=(
                        f"Connector '{connector.name}' names composition '{connector.composition}', "
                        "which does not exist in model/compositions/."
                    ),
                    subjects=(element.at(field_step("composition")),),
                    target=connector.composition,
                )
            )

        if connector.name in seen_names:
            records.append(
                ConsistencyIssue(
                    rule="connector.duplicate-name",
                    message=f"Duplicate connector name '{connector.name}'.",
                    subjects=(element,),
                )
            )
        seen_names.add(connector.name)

        # A link is keyed by its resolved owner (the implicit and the named root are one)
        # and each side's field, type and port: `swc: X` and `composition: X` differ, and
        # one type may be placed in several compositions, each wiring its own copy.
        link_key = (
            connector.composition if connector.composition is not None else root_name,
            *endpoint_type_field(connector.from_),
            connector.from_.port,
            *endpoint_type_field(connector.to),
            connector.to.port,
        )
        if link_key in seen_links:
            records.append(
                ConsistencyIssue(
                    rule="connector.duplicate-link",
                    message=(
                        f"Duplicate connector link "
                        f"{_endpoint_label(connector.from_)} -> "
                        f"{_endpoint_label(connector.to)}."
                    ),
                    subjects=(element,),
                )
            )
        seen_links.add(link_key)

        try:
            result = check_connector_compatibility(
                view, connector, swc_map, composition_map, interface_map, data_description
            )
        except _UnresolvedEndpoint as exc:
            # `_resolve_endpoint_port`'s order (`from` first, type before port) gives one stable issue.
            records.append(
                ConsistencyIssue(
                    rule=_UNRESOLVED_ENDPOINT_RULES[exc.field],
                    message=f"Connector '{connector.name}' invalid: {exc}",
                    subjects=(element.at(field_step(exc.side), field_step(exc.field)),),
                    target=exc.target,
                )
            )
            continue

        if not result.compatible:
            records.append(
                ConsistencyIssue(
                    rule=result.rule,
                    message=result.reason,
                    subjects=tuple(
                        element.at(field_step(side), field_step("port"))
                        for side in _CONNECTOR_ENDPOINT_SUBJECTS[result.rule]
                    ),
                )
            )

        # `[constr_1295]` (R4.3.0 `:4516`, normative), its `typed by` half; independent
        # of the interface rule: a compatible pair may still carry a pointer.
        crossing = _pointer_crossing(connector, swc_map, composition_map, interface_map, data_description)
        if crossing is not None:
            provider, requester, member, chain = crossing
            through = f" through {chain_text(chain)}" if len(chain) > 1 else ""
            records.append(
                ConsistencyIssue(
                    rule="connector.pointer-between-application-components",
                    message=(
                        f"Connector '{connector.name}' joins the application components '{provider}' and "
                        f"'{requester}', and {member} is typed by the pointer '{chain[-1]}'{through}; "
                        f"[constr_1295] lets a pointer cross a port only where the provider or the requester is a "
                        f"service or an ECU-abstraction component."
                    ),
                    subjects=(
                        element.at(field_step("from"), field_step("port")),
                        element.at(field_step("to"), field_step("port")),
                    ),
                )
            )

    return consistency_result(records)


def component_ends(
    endpoint: ConnectorEndpoint,
    swc_map: Mapping[str, SWComponent],
    composition_map: Mapping[str, Composition],
) -> list[tuple[SWComponent, Port]]:
    """The components and ports a connector endpoint reaches, through delegations, in declaration order.

    A composition-typed endpoint is followed down every delegation of its
    boundary port, into nested compositions. A name, member or port that does
    not resolve reaches nothing (its own rule's finding); a cycle stops the walk.
    """
    ends: list[tuple[SWComponent, Port]] = []
    pending: list[tuple[str | None, str | None, str]] = [(endpoint.swc, endpoint.composition, endpoint.port)]
    seen: set[tuple[str, str]] = set()
    while pending:
        swc_name, composition_name, port_name = pending.pop(0)
        if swc_name is not None:
            swc = swc_map.get(swc_name)
            port = None if swc is None else next((p for p in swc.ports if p.name == port_name), None)
            if swc is not None and port is not None:
                ends.append((swc, port))
            continue
        if composition_name is None or (composition_name, port_name) in seen:
            continue
        seen.add((composition_name, port_name))
        composition = composition_map.get(composition_name)
        if composition is None:
            continue
        members = {member.name: member for member in composition.members}
        for delegation in composition.delegations:
            member = members.get(delegation.member) if delegation.port == port_name else None
            if member is not None:
                pending.append((member.type, member.composition, delegation.member_port))
    return ends


def _pointer_typed_member(
    interface: Interface | None, data_types: DataDescription
) -> tuple[str, tuple[str, ...]] | None:
    """The first data element or argument of *interface* typed by a pointer, and the chain to it — or ``None``.

    Directly or through reference types, as ``[constr_1295]`` reads *after
    potential indirections via TYPE_REFERENCE are resolved*. A pointer **inside**
    a record or array the member is typed by is not reached: the constraint names
    the prototype's own type.
    """
    members: list[tuple[str, DataElement | CSArgument]] = []
    if isinstance(interface, SRInterface):
        members = [
            (f"interface '{interface.name}' data element '{element.name}'", element)
            for element in interface.data_elements
        ]
    elif isinstance(interface, CSInterface):
        members = [
            (f"interface '{interface.name}' operation '{operation.name}' argument '{argument.name}'", argument)
            for operation in interface.operations
            for argument in operation.arguments
        ]
    for description, member in members:
        # Only an implementation type can name a pointer.
        if member.application_type is not None or member.implementation_type is None:
            continue
        end = implementation_end(member.implementation_type, data_types)
        if end is not None and isinstance(end[1], PointerDataType):
            return description, end[0]
    return None


def _pointer_crossing(
    connector: Connector,
    swc_map: Mapping[str, SWComponent],
    composition_map: Mapping[str, Composition],
    interface_map: Mapping[str, Interface],
    data_types: DataDescription,
) -> tuple[str, str, str, tuple[str, ...]] | None:
    """``[constr_1295]``'s *typed by* half on one connector, where it is judged — or ``None``.

    Judged only where both ends are declared application components: the
    constraint permits the link at a ``service`` or ``ecu_abstraction`` end. Not judged: an
    external component stub, or an end no delegation resolves to a component.
    The interfaces read are the two leaf ports' own. The *mapped to* half is
    ``mapping-set.pointer-mapped``'s finding and is not repeated here.

    Returns:
        ``(provider, requester, member, chain)`` for the first judged pair of
        ends whose leaf interfaces carry a pointer-typed member, in endpoint
        order.
    """
    for provider, provider_port in component_ends(connector.from_, swc_map, composition_map):
        for requester, requester_port in component_ends(connector.to, swc_map, composition_map):
            if any(swc.external or swc.type != "application" for swc in (provider, requester)):
                continue
            for port in (provider_port, requester_port):
                found = _pointer_typed_member(interface_map.get(port.interface or ""), data_types)
                if found is not None:
                    return provider.name, requester.name, found[0], found[1]
    return None


# ---------------------------------------------------------------------------
# check_composition_consistency
# ---------------------------------------------------------------------------


#: The ``compositions`` layer, for the two rules that have no element to point at.
COMPOSITION_LAYER = "compositions"


def endpoint_type_field(endpoint: ConnectorEndpoint) -> tuple[str, str]:
    """``(field, type name)`` of what *endpoint* names; total, since a loaded endpoint sets exactly one."""
    if endpoint.swc is not None:
        return "swc", endpoint.swc
    return "composition", str(endpoint.composition)
