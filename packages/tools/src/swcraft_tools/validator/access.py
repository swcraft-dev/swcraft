"""The runnable access rules.

Checks every runnable's ``reads``, ``writes`` and ``calls`` against its component's
ports and their interfaces, under the five ``swc.access.*`` rules:
``unknown-port``, ``interface-kind-mismatch``, ``direction-mismatch``,
``unknown-element`` and ``unknown-operation``. Holds the in-memory core
:func:`runnable_access_records` and the model check :func:`check_runnable_access`.
"""

from __future__ import annotations

from collections.abc import Sequence

from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.schema import (
    ACCESS_LIST_NEEDS,
    INTERFACE_KIND_PROSE,
    CSInterface,
    ExternalInterface,
    Interface,
    Port,
    Runnable,
    SRInterface,
    SWComponent,
)
from swcraft_tools.validator.compositions import stub_member_miss, stub_member_names
from swcraft_tools.validator.generated_names import access_entries
from swcraft_tools.validator.init_values import quoted_list
from swcraft_tools.validator.model_view import ModelInput, as_view
from swcraft_tools.validator.results import ConsistencyIssue, ConsistencyResult, consistency_result

# ---------------------------------------------------------------------------
# check_runnable_access
# ---------------------------------------------------------------------------


def _interface_members(interface: SRInterface | CSInterface) -> frozenset[str]:
    """The names an access entry can resolve against: data elements or operations.

    Kept apart from ``arxml_import.swc_mapping.interface_members`` on purpose: the
    coverage oracle attributes a field read to the module that makes it.
    """
    if isinstance(interface, SRInterface):
        return frozenset(element.name for element in interface.data_elements)
    return frozenset(operation.name for operation in interface.operations)


def accessing_runnables(names: list[str], verbs: tuple[str, str] = ("accesses", "access")) -> str:
    """``runnable 'A' accesses`` / ``runnables 'A', 'B' and 'C' access``; ``verbs`` names another verb's two forms."""
    if len(names) == 1:
        return f"runnable {quoted_list(names)} {verbs[0]}"
    return f"runnables {quoted_list(names)} {verbs[1]}"


def runnable_access_records(swcs: Sequence[SWComponent], interfaces: Sequence[Interface]) -> list[ConsistencyIssue]:
    """Every runnable's ``reads``, ``writes`` and ``calls`` against the ports and interfaces they name.

    The schema refuses a blank name and a duplicated pair at load; these rules need the rest of the model:

    1. ``swc.access.unknown-port`` — the entry names a port the owning component does not declare. **One finding per
       unresolved port**, carrying every entry of every runnable of that component that names it, and no further
       finding for any of those entries — emitted after that component's other findings.
    2. ``swc.access.interface-kind-mismatch`` — a read or write through a client/server port, or a call through a
       sender/receiver port. Reported instead of, never beside, an element or operation finding.
    3. ``swc.access.direction-mismatch`` — a read or call through a provide port, or a write through a require port,
       whether or not the element resolves.
    4. ``swc.access.unknown-element`` / ``swc.access.unknown-operation`` — the entry names no data element of the
       port's sender/receiver interface, or no operation of its client/server interface — or, through a port on an
       external interface stub, no name the stub lists in its ``data_elements`` or ``operations`` (across a stub this
       rule fires instead of rule 2, and a listed member is not judged further).

    An access through a port whose ``interface`` is not declared is skipped (``port.undeclared-interface``); a
    duplicated port name resolves to the first declared; the receive form ``by`` is no part of any verdict.

    Args:
        swcs: The components whose runnables are judged.
        interfaces: Every interface the components' ports may name.

    Returns:
        One record per finding; empty when every entry resolves.
    """
    # The first declaration wins; `name.duplicate-interface` owns the second.
    # A stub maps to itself: it has no kind to compare.
    declared_interfaces: dict[str, tuple[str, frozenset[str]] | ExternalInterface] = {}
    for interface in interfaces:
        if isinstance(interface, ExternalInterface):
            declared_interfaces.setdefault(interface.name, interface)
        else:
            declared_interfaces.setdefault(interface.name, (interface.kind, _interface_members(interface)))

    records: list[ConsistencyIssue] = []
    for swc in swcs:
        ports: dict[str, Port] = {}
        for declared_port in swc.ports:
            ports.setdefault(declared_port.name, declared_port)
        owner = Subject("swc", swc.name)
        # Emitted after the walk as the one finding per port that rule 1 promises.
        unresolved: dict[str, list[tuple[str, str, dict[str, str]]]] = {}

        for runnable in swc.runnables:
            for list_name, port_name, member_field, member in access_entries(runnable):
                # The registry's published identity for the list, never `by`.
                identity = {"port": port_name, member_field: member}
                port = ports.get(port_name)
                if port is None:
                    unresolved.setdefault(port_name, []).append((runnable.name, list_name, identity))
                    continue
                if port.interface not in declared_interfaces:
                    continue  # port.undeclared-interface
                records.extend(
                    _access_entry_records(
                        swc,
                        runnable,
                        (list_name, member_field, member),
                        port,
                        declared_interfaces[port.interface],
                    )
                )

        for port_name, referrers in unresolved.items():
            runnable_names = list(dict.fromkeys(name for name, _list, _identity in referrers))
            count = len(referrers)
            records.append(
                ConsistencyIssue(
                    rule="swc.access.unknown-port",
                    message=(
                        f"SWC '{swc.name}' declares no port '{port_name}', which "
                        f"{accessing_runnables(runnable_names)} ({count} {'entry' if count == 1 else 'entries'})."
                    ),
                    # One repair is one violation with every referrer's address on it.
                    subjects=tuple(
                        owner.at(
                            field_step("runnables"),
                            member_step(runnable_name),
                            field_step(list_name),
                            member_step(identity),
                            field_step("port"),
                        )
                        for runnable_name, list_name, identity in referrers
                    ),
                    target=f"{swc.name}.{port_name}",
                )
            )

    return records


def _access_entry_records(
    swc: SWComponent,
    runnable: Runnable,
    access: tuple[str, str, str],
    port: Port,
    declared: tuple[str, frozenset[str]] | ExternalInterface,
) -> list[ConsistencyIssue]:
    """Rules 2-4 of :func:`runnable_access_records` for one entry through a declared port of its component.

    *access* is ``(list, member field, member)``; *declared* is the port's
    interface as ``(kind, member names)`` or the external interface stub itself.
    """
    list_name, member_field, member = access
    identity = {"port": port.name, member_field: member}
    needed_kind, needed_direction = ACCESS_LIST_NEEDS[list_name]
    owner = Subject("swc", swc.name)
    entry = owner.at(
        field_step("runnables"),
        member_step(runnable.name),
        field_step(list_name),
        member_step(identity),
    )
    port_subject = owner.at(field_step("ports"), member_step(port.name))
    # Through a stub the kind is never compared: a member of the wrong kind is
    # simply not in the stub's list, so the member rule names it.
    stub: ExternalInterface | None = None
    if isinstance(declared, ExternalInterface):
        stub = declared
        kind, members = needed_kind, stub_member_names(declared, member_field)
    else:
        kind, members = declared
    records: list[ConsistencyIssue] = []
    if kind != needed_kind:
        records.append(
            ConsistencyIssue(
                rule="swc.access.interface-kind-mismatch",
                message=(
                    f"SWC '{swc.name}' runnable '{runnable.name}' {list_name} port '{port.name}', "
                    f"whose interface '{port.interface}' is {INTERFACE_KIND_PROSE[kind]}; "
                    f"`{list_name}` needs a {INTERFACE_KIND_PROSE[needed_kind]} port."
                ),
                # The two legal repairs: move the entry, or retarget the port's interface.
                subjects=(entry, port_subject.at(field_step("interface"))),
            )
        )
        return records
    if port.direction != needed_direction:
        records.append(
            ConsistencyIssue(
                rule="swc.access.direction-mismatch",
                message=(
                    f"SWC '{swc.name}' runnable '{runnable.name}' {list_name} port '{port.name}', "
                    f"which is a {port.direction} port; `{list_name}` needs a {needed_direction} port."
                ),
                subjects=(entry, port_subject.at(field_step("direction"))),
            )
        )
    if member in members:
        return records
    if member_field == "element":
        records.append(
            ConsistencyIssue(
                rule="swc.access.unknown-element",
                message=(
                    f"SWC '{swc.name}' runnable '{runnable.name}' {list_name} element '{member}' "
                    f"of port '{port.name}', which interface '{port.interface}' does not declare."
                    if stub is None
                    else f"SWC '{swc.name}' runnable '{runnable.name}' {list_name} element '{member}' "
                    f"of port '{port.name}', but {stub_member_miss(stub, member_field, member)}."
                ),
                # The referrer, never the interface: the entry is what has to change.
                subjects=(entry.at(field_step("element")),),
                target=f"{port.interface}.{member}",
            )
        )
    else:
        records.append(
            ConsistencyIssue(
                rule="swc.access.unknown-operation",
                message=(
                    f"SWC '{swc.name}' runnable '{runnable.name}' {list_name} operation '{member}' "
                    f"of port '{port.name}', which interface '{port.interface}' does not declare."
                    if stub is None
                    else f"SWC '{swc.name}' runnable '{runnable.name}' {list_name} operation '{member}' "
                    f"of port '{port.name}', but {stub_member_miss(stub, member_field, member)}."
                ),
                subjects=(entry.at(field_step("operation")),),
                target=f"{port.interface}.{member}",
            )
        )
    return records


def check_runnable_access(model_root: ModelInput) -> ConsistencyResult:
    """Check every runnable's ``reads``, ``writes`` and ``calls`` against the ports and interfaces they name.

    Args:
        model_root: The model root, or a view of it.

    Returns:
        ConsistencyResult with consistent=True only when every entry resolves.
    """
    view = as_view(model_root)
    return consistency_result(runnable_access_records(view.swcs, view.interfaces))
