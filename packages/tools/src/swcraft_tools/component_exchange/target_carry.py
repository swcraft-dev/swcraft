"""What ``import component`` keeps of the target's own declarations of a component it replaces or reuses.

Each carry runs onto the incoming component and returns the new component and what it kept or
dropped, for the report. Three run before the conflict comparison, for any component the target
holds, so a value the export cannot or does not state is not a difference: the termination value,
the runnables' budgets and the descriptions. Three run after it, only for an ``updated`` component
and before its preview: the ports' measures, the trace links and the target's spelling of a silent
com-spec value. :func:`untaken_description_lines` lists the incoming descriptions a reused shared
definition does not take. Nothing here raises or reads the target project.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from swcraft_tools.schema import (
    CSInterface,
    DataDescriptionElement,
    DataType,
    EnumDataType,
    ImplementationRecordDataType,
    Interface,
    Port,
    PortComSpec,
    RecordDataType,
    SRInterface,
    SWComponent,
    TextTable,
)
from swcraft_tools.writer.arxml_generation import (
    COM_SPEC_SILENT_VALUES as _COM_SPEC_SILENT_VALUES,
)
from swcraft_tools.writer.arxml_generation import (
    com_spec_value as _writer_com_spec_value,
)

from .divergence import com_spec_silent_spelling


def kept_termination_handling(existing: SWComponent, incoming: SWComponent) -> str | None:
    """The target's ``handle_termination_and_restart`` an import keeps, or ``None``.

    Kept exactly when the export states no ``HANDLE-TERMINATION-AND-RESTART`` and the target
    declares a value: the element's absence is never a request to clear the field. No capability
    marker is consulted: the SWCraft marker group attests the features it names, not this field.
    """
    if incoming.handle_termination_and_restart is not None:
        return None
    return existing.handle_termination_and_restart


def _described_parts(swc: SWComponent) -> list[tuple[str, str]]:
    """The component's own documented parts, as ``(label, description)``, in file order.

    The component, each port, each runnable: the parts the writer writes a ``DESC`` for. The label
    is the part's identity in a report line.
    """
    return [
        (f"component '{swc.name}'", swc.description),
        *((f"port '{port.name}'", port.description) for port in swc.ports),
        *((f"runnable '{runnable.name}'", runnable.description) for runnable in swc.runnables),
    ]


def incoming_descriptions_differ(existing: SWComponent, incoming: SWComponent) -> list[str]:
    """The labels of the parts whose non-empty incoming description differs from the target's.

    An empty incoming text is no difference (a file written before 0.14.0 carries no ``DESC``).
    The labels go into the conflict line so its reader need not find the difference themselves.
    """
    target = dict(_described_parts(existing))
    return [label for label, text in _described_parts(incoming) if label in target and text and text != target[label]]


def carry_target_costs(existing: SWComponent, incoming: SWComponent) -> tuple[SWComponent, dict[str, str]]:
    """The target's budget for each file runnable that states none, by runnable name.

    ``wcet`` and ``wcet_type`` travel together. A runnable that states a ``wcet`` keeps its own; one
    that states none takes the target's pair, because that silence is not a request to clear it.
    Applied before the comparison, so such a file compares ``unchanged``. Returns the incoming SWC
    and ``{runnable: budget as written}`` for each budget carried.

    **Does not reach:** a runnable the file renames is a new runnable; no cost crosses the rename.
    """
    costs = {runnable.name: runnable for runnable in existing.runnables if runnable.wcet is not None}
    carried = {
        runnable.name: costs[runnable.name]
        for runnable in incoming.runnables
        if runnable.name in costs and runnable.wcet is None
    }
    if not carried:
        return incoming, {}
    runnables = [
        runnable.model_copy(update={"wcet": carried[runnable.name].wcet, "wcet_type": carried[runnable.name].wcet_type})
        if runnable.name in carried
        else runnable
        for runnable in incoming.runnables
    ]
    return incoming.model_copy(update={"runnables": runnables}), {
        name: str(runnable.wcet) for name, runnable in carried.items()
    }


def carry_target_descriptions(existing: SWComponent, incoming: SWComponent) -> tuple[SWComponent, list[str]]:
    """The ``memory`` rule for the component's own descriptions, by part.

    An incoming part (the component, a port by name, a runnable by name) whose description is empty
    keeps the target's text: an empty text is a file written before 0.14.0 or a text never set, never
    an instruction to erase. A non-empty incoming text keeps its own. Applied before the comparison.
    Returns the incoming SWC with the carried texts and the carried labels, in file order.
    """
    ports = {port.name: port.description for port in existing.ports}
    runnables = {runnable.name: runnable.description for runnable in existing.runnables}
    carried: list[str] = []
    update: dict[str, Any] = {}
    if not incoming.description and existing.description:
        update["description"] = existing.description
        carried.append(f"component '{incoming.name}'")
    new_ports = []
    for port in incoming.ports:
        kept = ports.get(port.name, "")
        if not port.description and kept:
            new_ports.append(port.model_copy(update={"description": kept}))
            carried.append(f"port '{port.name}'")
        else:
            new_ports.append(port)
    new_runnables = []
    for runnable in incoming.runnables:
        kept = runnables.get(runnable.name, "")
        if not runnable.description and kept:
            new_runnables.append(runnable.model_copy(update={"description": kept}))
            carried.append(f"runnable '{runnable.name}'")
        else:
            new_runnables.append(runnable)
    update["ports"] = new_ports
    update["runnables"] = new_runnables
    return incoming.model_copy(update=update), carried


def untaken_description_lines(
    existing: DataType | Interface | DataDescriptionElement, incoming: DataType | Interface | DataDescriptionElement
) -> list[str]:
    """One line per non-empty incoming description a reused shared definition does not take.

    A shared data type or interface is reused, never replaced, and the reuse comparison projects
    every description out, so an incoming text that differs from the target's is written nowhere;
    each one (the element's own and every member's) is named. Called only after the projections
    compared equal, so the member lists agree; ``strict=True`` states that rather than trusting it.
    """
    pairs: list[tuple[str, str, str]] = [(existing.name, existing.description, incoming.description)]

    def members(path: str, current: Sequence[Any], arriving: Sequence[Any]) -> None:
        for mine, theirs in zip(current, arriving, strict=True):
            pairs.append((f"{path}/{theirs.name}", mine.description, theirs.description))

    if isinstance(existing, SRInterface) and isinstance(incoming, SRInterface):
        members(incoming.name, existing.data_elements, incoming.data_elements)
    elif isinstance(existing, CSInterface) and isinstance(incoming, CSInterface):
        for mine, theirs in zip(existing.operations, incoming.operations, strict=True):
            pairs.append((f"{incoming.name}/{theirs.name}", mine.description, theirs.description))
            members(f"{incoming.name}/{theirs.name}", mine.arguments, theirs.arguments)
    elif isinstance(existing, EnumDataType) and isinstance(incoming, EnumDataType):
        # An enumeration naming a texttable states no literal.
        members(incoming.name, existing.literals or [], incoming.literals or [])
    elif isinstance(existing, RecordDataType) and isinstance(incoming, RecordDataType):
        members(incoming.name, existing.fields, incoming.fields)
    elif isinstance(existing, ImplementationRecordDataType) and isinstance(incoming, ImplementationRecordDataType):
        members(incoming.name, existing.elements, incoming.elements)
    elif isinstance(existing, TextTable) and isinstance(incoming, TextTable):
        members(incoming.name, existing.entries, incoming.entries)
    return [
        f"DESC of {where} not taken: the target definition is kept"
        for where, current, arriving in pairs
        if arriving and arriving != current
    ]


def carry_target_measures(
    existing: SWComponent, imported: SWComponent
) -> tuple[SWComponent, dict[str, dict[str, str]], list[str]]:
    """The ``memory`` rule one level down again, for a port's ``measures``.

    An exported ARXML carries no measure, so an imported port's empty map means **unknown**. A map
    is carried only where its port is still the same port: nothing for a port the export no longer
    declares, re-points to another interface, or flips in direction either way. **Does not reach:**
    an incoming port's own measure would be overwritten.

    Returns the imported SWC with the carried maps in place, the carried entries per port, and one
    line per dropped map.
    """
    target_ports = {port.name: port for port in existing.ports if port.measures}
    incoming = {port.name: port for port in imported.ports}
    preserved: dict[str, dict[str, str]] = {}
    dropped: list[str] = []
    for name in sorted(target_ports):
        target = target_ports[name]
        port = incoming.get(name)
        count = len(target.measures)
        if port is None:
            dropped.append(
                f"port '{name}': the export no longer declares it, so its {count} declared measure(s) go with it"
            )
        elif port.interface != target.interface:
            dropped.append(
                f"port '{name}': the export re-points it from '{target.interface}' to "
                f"'{port.interface}', so its {count} declared measure(s) are NOT carried "
                "(a measure names a data element of the port's own interface)"
            )
        elif port.direction != target.direction:
            dropped.append(
                f"port '{name}': the export flips it from '{target.direction}' to "
                f"'{port.direction}', so its {count} declared measure(s) are NOT carried "
                "(a measure is read on a require port and on no other, so a flip either "
                "way changes what the map means)"
            )
        else:
            preserved[name] = dict(target.measures)
    ports = [
        port.model_copy(update={"measures": dict(preserved[port.name])}) if port.name in preserved else port
        for port in imported.ports
    ]
    return imported.model_copy(update={"ports": ports}), preserved, dropped


def carry_target_links(
    existing: SWComponent, imported: SWComponent
) -> tuple[SWComponent, dict[str, list[str]], list[str]]:
    """The ``asil`` rule for the trace links: the target's own, carried.

    An exported ARXML carries no link, so an imported ``satisfies: []`` means **unknown**; replacing
    the target's component would erase the links and the level they allocate. The component's own
    links are carried, each port's by port name and each runnable's by runnable name. A link on a
    member the export no longer declares is dropped and named. A link naming no requirement is
    ``check-model``'s finding after the import.

    Returns the imported SWC, the carried links per site (``component``, ``port '<p>'``,
    ``runnable '<r>'``) and one line per dropped site.
    """
    dropped: list[str] = []
    port_links: dict[str, list[str]] = {}
    runnable_links: dict[str, list[str]] = {}
    for kind, members, incoming, kept in (
        ("port", existing.ports, {port.name for port in imported.ports}, port_links),
        ("runnable", existing.runnables, {runnable.name for runnable in imported.runnables}, runnable_links),
    ):
        for member in members:
            if not member.satisfies:
                continue
            if member.name in incoming:
                kept[member.name] = list(member.satisfies)
            else:
                dropped.append(
                    f"{kind} '{member.name}': the export no longer declares it, so its "
                    f"{len(member.satisfies)} link(s) go with it ({', '.join(member.satisfies)})"
                )
    # Each list was copied once, above or here, and the carried model and the
    # report share that copy: neither is mutated after this point.
    ports = [
        port.model_copy(update={"satisfies": port_links[port.name]}) if port.name in port_links else port
        for port in imported.ports
    ]
    runnables = [
        runnable.model_copy(update={"satisfies": runnable_links[runnable.name]})
        if runnable.name in runnable_links
        else runnable
        for runnable in imported.runnables
    ]
    own = list(existing.satisfies)
    preserved: dict[str, list[str]] = {"component": own} if own else {}
    preserved.update({f"port '{name}'": links for name, links in port_links.items()})
    preserved.update({f"runnable '{name}'": links for name, links in runnable_links.items()})
    update: dict[str, Any] = {"ports": ports, "runnables": runnables, "satisfies": own}
    return imported.model_copy(update=update), preserved, dropped


def keep_target_com_spec_spelling(existing: SWComponent, imported: SWComponent) -> SWComponent:
    """The imported component with the target's own spelling of its ``com_specs`` kept (``--update``).

    Keeps the update from normalising away a silent value the target states. Per port the export
    still declares with the same direction and interface (otherwise nothing is kept), and per data
    element:

    * an attribute the target states at its silent value and the file does not state stays; one the
      file states otherwise takes the file's value;
    * an entry stating no attribute stays, and so does the rest of an entry the file strips;
    * the target's entries keep their order, and the file's new ones follow.

    Nothing is reported: both spellings say the same thing, so the preview shows no change for it.
    """
    target_ports = {port.name: port for port in existing.ports if port.com_specs}
    ports: list[Port] = []
    for port in imported.ports:
        target = target_ports.get(port.name)
        if target is None or target.direction != port.direction or target.interface != port.interface:
            ports.append(port)
            continue
        arriving = {entry.data_element: entry for entry in port.com_specs}
        kept: list[PortComSpec] = []
        for entry in target.com_specs:
            spelling = com_spec_silent_spelling(entry)
            bare = PortComSpec(data_element=entry.data_element)
            incoming = arriving.pop(entry.data_element, None)
            if incoming is None:
                if spelling or entry == bare:
                    kept.append(bare.model_copy(update=spelling))
                continue
            unstated = {
                attribute.field
                for tag, attribute in _COM_SPEC_SILENT_VALUES.items()
                if _writer_com_spec_value(incoming, tag) is None
            }
            kept.append(incoming.model_copy(update={name: spelling[name] for name in spelling if name in unstated}))
        ports.append(port.model_copy(update={"com_specs": [*kept, *arriving.values()]}))
    return imported.model_copy(update={"ports": ports})
