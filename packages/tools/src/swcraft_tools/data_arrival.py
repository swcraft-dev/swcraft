"""Where a data-received runnable's data comes from.

For each data-received event of each root-reachable occurrence of its component, :func:`arrivals`
follows the model's wiring back to the runnables that write the element and to their tasks, and
answers one :class:`Arrival`: a **bound** (the shortest period of a writer's task), **unknown** (a
source outside the model, with the first case of :data:`OUTSIDE_CASES` in walk order), or **not
derived** (one of :data:`NOT_DERIVED_CASES`). The answer is reported only: no ``check-model`` rule
reads it, nothing is written to ARXML and no YAML field holds it. A model with no data-received
event yields an empty tuple.

The walk follows the element **by name** through the connectors whose ``to`` endpoint is the
receiving port, down through a source composition's delegations to a component, and up one level
through a delegation of a boundary port; a delegation out of the root ends outside the model. Only
an ``external: true`` component is outside. The writers are the runnables of the source type whose
``writes`` hold the port and element; a writer's task is the one whose entry resolves to that
instance (the shortest period, then reader order, wins), whatever starts the writer.

What it does not reach:

- The bound is the senders' task period, not a measured rate; the receiving runnable's own task is
  not compared with it.
- A connector or delegation naming something undeclared, and an event that does not resolve, are
  ``check-model``'s findings; this walk abstains on them.
- A source port no delegation reaches inside, or a sender type no file declares, reads *no runnable
  of the sender writes the element*; a writer whose task entry does not resolve to one occurrence
  reads *a writer runs in no task*.
- A component placed more than :data:`~swcraft_tools.scheduling_identity.PATH_CAP` times is walked
  at its first paths only, and :data:`PAST_THE_CAP` counts the occurrences left unwalked in
  ``more``. The upward walk is paid per event and per occurrence, each step scanning the connectors.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from swcraft_tools.reader import (
    ModelRootNotFoundError,
    read_connectors,
    read_interfaces,
    read_swc_model,
    read_tasks,
)
from swcraft_tools.scheduling_identity import PATH_CAP, PATH_SEPARATOR, SchedulingIdentities, identities_for
from swcraft_tools.schema import (
    STUB_MEMBER_DEST,
    Composition,
    CompositionMember,
    Connector,
    ConnectorEndpoint,
    DataEvent,
    ExternalInterface,
    Interface,
    SRInterface,
    SWComponent,
    Task,
)

#: The three verdicts an :class:`Arrival` carries.
BOUNDED = "bounded"
UNKNOWN = "unknown"
NOT_DERIVED = "not-derived"

#: The closed list of the cases that put a source outside the model, each the text a line names.
NO_CONNECTOR = "no connector reaches the port"
DELEGATED_OUT_OF_ROOT = "delegated out of the top composition"
EXTERNAL_SENDER = "the sender is an external component"
NO_WRITER = "no runnable of the sender writes the element"
WRITER_IN_NO_TASK = "a writer runs in no task"
OUTSIDE_CASES = (NO_CONNECTOR, DELEGATED_OUT_OF_ROOT, EXTERNAL_SENDER, NO_WRITER, WRITER_IN_NO_TASK)

#: Why no verdict is derived, each the text a line names after ``not derived:``.
NO_SINGLE_ROOT = "the composition layer has no single root"
UNPLACED = "the component is placed in no composition"
UNRESOLVED = "the event does not resolve"
#: A component placed more than ``PATH_CAP`` times: the line's ``more`` counts the occurrences not walked.
PAST_THE_CAP = f"the occurrences past the first {PATH_CAP} are not walked"
NOT_DERIVED_CASES = (NO_SINGLE_ROOT, UNPLACED, UNRESOLVED, PAST_THE_CAP)


@dataclass(frozen=True)
class Arrival:
    """Where one data-received event's data comes from, for one occurrence of its component.

    ``occurrence`` is the instance path, or the component's type name for a
    not-derived line that names no occurrence. ``period`` (seconds, as the model holds it), ``writer``
    (``path.runnable``) and ``task`` are the bound's, set only for
    :data:`BOUNDED`; ``case`` is the outside or not-derived case; ``more``
    counts the further writers of a bound, the further outside sources of an
    unknown, or the occurrences past the cap.
    """

    occurrence: str
    runnable: str
    port: str
    element: str
    verdict: str
    period: float | None = None
    writer: str | None = None
    task: str | None = None
    case: str | None = None
    more: int = 0


@dataclass(frozen=True)
class _Bound:
    period: float
    writer: str
    task: str


@dataclass(frozen=True)
class _Sources:
    """What the verdict reads of the sources a walk reaches, in walk order: never the sources themselves."""

    #: The first case, in walk order, that puts a source outside the model.
    outside: str | None = None
    outside_count: int = 0
    #: The shortest period's bound, the first in walk order among equals.
    bound: _Bound | None = None
    bound_count: int = 0

    def then(self, later: _Sources) -> _Sources:
        """These sources followed, in walk order, by *later*."""
        bound = self.bound
        if bound is None or (later.bound is not None and later.bound.period < bound.period):
            bound = later.bound
        return _Sources(
            self.outside if self.outside is not None else later.outside,
            self.outside_count + later.outside_count,
            bound,
            self.bound_count + later.bound_count,
        )


@dataclass
class _Model:
    """The layers one :func:`arrivals` call reads, each read once, and the call's downward memo."""

    identities: SchedulingIdentities
    components: Mapping[str, SWComponent]
    connectors: Sequence[Connector]
    #: ``(instance path, runnable)`` -> the task with the shortest period whose entry resolves to it.
    tasks: Mapping[tuple[str, str], Task]
    #: Every instance path some mapped writer's path lies strictly below.
    mapped_below: frozenset[str]
    #: A sender composition member's answer, by ``(composition or instance path, port, element)``.
    memo: dict[tuple[str | None, str | None, str, str], _Sources] = field(default_factory=dict)


def _resolves(component: SWComponent, event: DataEvent, interfaces: Mapping[str, Interface]) -> bool:
    """Whether *event* names a require port of *component* whose sender/receiver interface or stub has the element."""
    port = next((p for p in component.ports if p.name == event.port), None)
    if port is None or port.direction != "require" or port.interface is None:
        return False
    interface = interfaces.get(port.interface)
    if isinstance(interface, SRInterface):
        return any(element.name == event.element for element in interface.data_elements)
    if isinstance(interface, ExternalInterface):
        return interface.external_dest == STUB_MEMBER_DEST["data_elements"] and event.element in interface.data_elements
    return False


def _frames(identities: SchedulingIdentities, path: str) -> list[tuple[Composition, CompositionMember]]:
    """Each segment of *path* with the composition holding it, by the public ``index`` walked from ``root``."""
    assert identities.root is not None  # a defined walk has one root
    current = identities.index[identities.root]
    frames: list[tuple[Composition, CompositionMember]] = []
    for segment in path.split(PATH_SEPARATOR):
        member = next(m for m in current.members if m.name == segment)
        frames.append((current, member))
        if member.composition is not None:
            current = identities.index[member.composition]
    return frames


def _names(endpoint: ConnectorEndpoint, member: CompositionMember) -> bool:
    """Whether *endpoint* names *member*'s type, a component's or a composition's."""
    if member.type is not None:
        return endpoint.swc == member.type
    return endpoint.composition is not None and endpoint.composition == member.composition


def _drawn_in(connector: Connector, composition: Composition, root: str) -> bool:
    """Whether *connector* is drawn in *composition*: one naming no composition is the root's."""
    return (connector.composition or root) == composition.name


def _sources_into(
    model: _Model,
    frames: list[tuple[Composition, CompositionMember]],
    depth: int,
    port: str,
    element: str,
    upward: dict[tuple[int, str], _Sources],
) -> _Sources:
    """Steps 2 and 4: the sources that reach ``(frames[depth]'s member, port)``, memoised in *upward*."""
    if (depth, port) not in upward:
        upward[(depth, port)] = _sources_reaching(model, frames, depth, port, element, upward)
    return upward[(depth, port)]


def _sources_reaching(
    model: _Model,
    frames: list[tuple[Composition, CompositionMember]],
    depth: int,
    port: str,
    element: str,
    upward: dict[tuple[int, str], _Sources],
) -> _Sources:
    """:func:`_sources_into`'s computation: the connectors drawn to the port, else its delegations upward."""
    composition, member = frames[depth]
    root = model.identities.root
    assert root is not None
    prefix = [m.name for _, m in frames[:depth]]
    sources = _Sources()
    reached = False
    for connector in model.connectors:
        if not _drawn_in(connector, composition, root):
            continue
        if connector.to.port != port or not _names(connector.to, member):
            continue
        source = next((m for m in composition.members if _names(connector.from_, m)), None)
        if source is None:
            continue  # a type this composition does not place: the composition rules' finding
        reached = True
        sources = sources.then(_sender(model, [*prefix, source.name], source, connector.from_.port, element))
    if reached:
        return sources
    delegations = [d for d in composition.delegations if d.member == member.name and d.member_port == port]
    if not delegations:
        return _outside(NO_CONNECTOR)
    if depth == 0:  # each root boundary port is fed from outside on its own: one source per delegation
        return _Sources(outside=DELEGATED_OUT_OF_ROOT, outside_count=len(delegations))
    for delegation in delegations:
        sources = sources.then(_sources_into(model, frames, depth - 1, delegation.port, element, upward))
    return sources


def _sender(model: _Model, path: list[str], member: CompositionMember, port: str, element: str) -> _Sources:
    """Steps 3 and 5: down through a composition's delegations to the writers of ``(port, element)``, memoised."""
    joined = PATH_SEPARATOR.join(path)
    if member.type is not None:
        return _writers(model, joined, member.type, port, element)
    # Below a path no task entry reaches, every writer is in no task whatever the path: one answer per composition.
    key = (joined, None, port, element) if joined in model.mapped_below else (None, member.composition, port, element)
    if key not in model.memo:
        model.memo[key] = _sender_below(model, path, member, port, element)
    return model.memo[key]


def _sender_below(model: _Model, path: list[str], member: CompositionMember, port: str, element: str) -> _Sources:
    """:func:`_sender`'s computation for a composition member: each inner delegation of *port*, followed down."""
    inner = model.identities.index.get(member.composition) if member.composition is not None else None
    sources = _Sources()
    if inner is not None:
        for delegation in inner.delegations:
            if delegation.port != port:
                continue
            target = next((m for m in inner.members if m.name == delegation.member), None)
            if target is not None:
                sources = sources.then(_sender(model, [*path, target.name], target, delegation.member_port, element))
    if sources.outside_count + sources.bound_count == 0:
        return _outside(NO_WRITER)
    return sources


def _writers(model: _Model, path: str, component_type: str, port: str, element: str) -> _Sources:
    """The runnables of a placed component that write ``(port, element)``, each bound by its task or outside."""
    component = model.components.get(component_type)
    if component is None:
        return _outside(NO_WRITER)
    if component.external:
        return _outside(EXTERNAL_SENDER)
    writers = [r for r in component.runnables if any(w.port == port and w.element == element for w in r.writes)]
    if not writers:
        return _outside(NO_WRITER)
    sources = _Sources()
    for writer in writers:
        task = model.tasks.get((path, writer.name))
        if task is None:
            sources = sources.then(_outside(WRITER_IN_NO_TASK))
        else:
            bound = _Bound(task.period, f"{path}.{writer.name}", task.name)
            sources = sources.then(_Sources(bound=bound, bound_count=1))
    return sources


def _outside(case: str) -> _Sources:
    return _Sources(outside=case, outside_count=1)


def _verdict(occurrence: str, runnable: str, event: DataEvent, sources: _Sources) -> Arrival:
    """Step 6: unknown when any source is outside the model, else the shortest period, first in walk order."""
    if sources.outside is not None:
        return _not_bounded(occurrence, runnable, event, UNKNOWN, sources.outside, sources.outside_count - 1)
    assert sources.bound is not None  # every walk reaches a source: a writer, or a case outside the model
    return Arrival(
        occurrence,
        runnable,
        event.port,
        event.element,
        verdict=BOUNDED,
        period=sources.bound.period,
        writer=sources.bound.writer,
        task=sources.bound.task,
        more=sources.bound_count - 1,
    )


def _not_bounded(occurrence: str, runnable: str, event: DataEvent, verdict: str, case: str, more: int = 0) -> Arrival:
    """An unknown or a not-derived line: the case, and what it counts."""
    return Arrival(occurrence, runnable, event.port, event.element, verdict=verdict, case=case, more=more)


def _task_map(identities: SchedulingIdentities, tasks: Sequence[Task]) -> dict[tuple[str, str], Task]:
    """Each mapped writer's task: of two that map it, the shorter period, the first in reader order among equals."""
    answer: dict[tuple[str, str], Task] = {}
    for task in tasks:
        for entry in task.runnables:
            identity = identities.resolve(entry.swc, entry.instance).identity
            if identity[0] == "instance" and isinstance(identity[1], str):
                key = (identity[1], entry.runnable)
                if key not in answer or task.period < answer[key].period:
                    answer[key] = task
    return answer


def _mapped_below(tasks: Mapping[tuple[str, str], Task]) -> frozenset[str]:
    """Every proper prefix of a mapped writer's instance path."""
    segments = [path.split(PATH_SEPARATOR) for path, _ in tasks]
    return frozenset(PATH_SEPARATOR.join(parts[:end]) for parts in segments for end in range(1, len(parts)))


def arrivals(model_root: str) -> tuple[Arrival, ...]:
    """Every data-received event's arrival, per root-reachable occurrence of its component, in reader order.

    With no model root the answer is ``()`` and nothing is read; with no
    data-received event it is ``()`` and only the component layer is read;
    otherwise each other layer is read once.

    Raises:
        ValueError: if a file of a layer it reads is malformed (the reader's).
    """
    try:
        components = read_swc_model(model_root)
    except ModelRootNotFoundError:
        # No model, so no data-received event; the caller reports the missing root itself.
        return ()
    receiving = [
        (component, runnable.name, event)
        for component in components
        if not component.external
        for runnable in component.runnables
        for event in runnable.events
        if isinstance(event, DataEvent)
    ]
    if not receiving:
        return ()
    interfaces: dict[str, Interface] = {}
    for interface in read_interfaces(model_root):
        interfaces.setdefault(interface.name, interface)
    by_name: dict[str, SWComponent] = {}
    for component in components:
        by_name.setdefault(component.name, component)
    identities = identities_for(model_root)
    tasks = _task_map(identities, read_tasks(model_root))
    model = _Model(identities, by_name, read_connectors(model_root), tasks, _mapped_below(tasks))

    answer: list[Arrival] = []
    for component, runnable, event in receiving:
        if not _resolves(component, event, interfaces):
            answer.append(_not_bounded(component.name, runnable, event, NOT_DERIVED, UNRESOLVED))
            continue
        if identities.placements is None:
            answer.append(_not_bounded(component.name, runnable, event, NOT_DERIVED, NO_SINGLE_ROOT))
            continue
        placement = identities.placements.get(component.name)
        if placement is None:
            answer.append(_not_bounded(component.name, runnable, event, NOT_DERIVED, UNPLACED))
            continue
        for path in placement.paths:
            frames = _frames(identities, path)
            sources = _sources_into(model, frames, len(frames) - 1, event.port, event.element, {})
            answer.append(_verdict(path, runnable, event, sources))
        if placement.count > len(placement.paths):
            unwalked = placement.count - len(placement.paths)
            answer.append(_not_bounded(component.name, runnable, event, NOT_DERIVED, PAST_THE_CAP, unwalked))
    return tuple(answer)
