"""The scheduling identity: which component occurrence a task entry or a sequence step names.

A task's ``runnables`` entry and a sequence step name their component by **exactly one** of ``swc`` (a type)
and ``instance`` (a ``/``-joined path of member names from the root composition down, without the root's own
name). :meth:`SchedulingIdentities.resolve` is the one answer:

=================  ===========================================  ==================================
Entry              Root-reachable occurrences                   Resolves to
=================  ===========================================  ==================================
``swc: A``         exactly one, at path ``p``                   ``('instance', p)``, type ``A``
``swc: A``         none                                         ``('unplaced-type', A)``, type ``A``
``swc: A``         more than one                                refused: ``instance-ambiguous``
``instance: p``    ``p`` walks to a member whose ``type`` is a  ``('instance', p)``, that type
                   component
``instance: p``    a segment names no member, or descends into  refused: ``instance-unknown``
                   a type member or an undeclared composition
``instance: p``    ``p`` ends on a member instantiating a       refused: ``instance-not-a-component``
                   composition
=================  ===========================================  ==================================

**When the walk is undefined** (no root composition, several, or a cycle the root reaches), every entry's
identity is its syntactic triple ``('syntactic', swc, instance)``, nothing is refused, and an ``instance`` entry
has no type. A refused entry keeps its syntactic triple too, so it is never merged with another.

The walk keeps at most :data:`PATH_CAP` paths per type, a cap that decides nothing;
:meth:`SchedulingIdentities.occurrences` gives every path. An ``instance`` value is no declared reference, so
``impact`` and ``query --dependents`` do not follow it.

A leaf: it imports only :mod:`swcraft_tools.schema` and :func:`swcraft_tools.reader.read_compositions`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Literal, TypeVar

from swcraft_tools.reader import read_compositions
from swcraft_tools.schema import Composition

#: The most paths the walk keeps per type. The count is exact whatever this is.
PATH_CAP = 64

#: The separator of an ``instance`` path's member names.
PATH_SEPARATOR = "/"

#: The three refusals the resolver can answer, as the suffix of a rule id:
#: ``task.instance-…`` for a task entry, ``sequence.step-instance-…`` for a step.
Refusal = Literal["instance-ambiguous", "instance-unknown", "instance-not-a-component"]

#: ``('instance', path)``, ``('unplaced-type', type)`` or ``('syntactic', swc, instance)``.
Identity = tuple[str | None, ...]

#: A task entry's or a sequence step's ``(swc, instance, runnable)``, as written:
#: exactly one of the two selectors is set, ``runnable`` always is.
EntryTriple = tuple[str | None, str | None, str]

_Component = TypeVar("_Component")


def composition_index(compositions: Sequence[Composition]) -> dict[str, Composition]:
    """Declared composition name -> composition, **first** declaration winning.

    A duplicate name across two files is ``composition.duplicate-name``'s to report.
    """
    index: dict[str, Composition] = {}
    for composition in compositions:
        index.setdefault(composition.name, composition)
    return index


@dataclass(frozen=True)
class Placement:
    """One component type's root-reachable occurrences: the exact count and the first paths."""

    count: int
    #: At most :data:`PATH_CAP` paths, in walk order (member order, depth first).
    paths: tuple[str, ...]


def _walk(root: str, index: Mapping[str, Composition]) -> dict[str, Placement] | None:
    """Every component type placed below *root*, over the first-wins *index*, with its exact count and paths.

    Each composition's answer is memoised, so a diamond of any depth is walked
    once; the path lists are capped at :data:`PATH_CAP`, the counts are not. A
    type absent from the answer is placed nowhere. ``None`` on a cycle met on the
    walk; a cycle the root does not reach is not looked for. A member naming an
    undeclared composition is not descended into.
    """
    memo: dict[str, dict[str, Placement]] = {}
    on_path: set[str] = set()

    def walk(name: str) -> dict[str, Placement] | None:
        if name in memo:
            return memo[name]
        if name in on_path:
            return None
        on_path.add(name)
        counts: dict[str, int] = {}
        paths: dict[str, list[str]] = {}
        for member in index[name].members:
            if member.type is not None:
                counts[member.type] = counts.get(member.type, 0) + 1
                kept = paths.setdefault(member.type, [])
                if len(kept) < PATH_CAP:
                    kept.append(member.name)
            elif member.composition is not None and member.composition in index:
                below = walk(member.composition)
                if below is None:
                    return None
                for component, placement in below.items():
                    counts[component] = counts.get(component, 0) + placement.count
                    kept = paths.setdefault(component, [])
                    for path in placement.paths:
                        if len(kept) >= PATH_CAP:
                            break
                        kept.append(f"{member.name}{PATH_SEPARATOR}{path}")
        on_path.discard(name)
        answer = {component: Placement(counts[component], tuple(paths[component])) for component in counts}
        memo[name] = answer
        return answer

    return walk(root)


def _the_root(compositions: Sequence[Composition]) -> str | None:
    """The one root composition's name, or ``None`` when there is none or several."""
    roots = list(dict.fromkeys(composition.name for composition in compositions if composition.root))
    return roots[0] if len(roots) == 1 else None


@dataclass(frozen=True)
class Resolution:
    """What one entry names: its identity, its component type, or the refusal."""

    identity: Identity
    #: The component type, or ``None`` for a refused path or an ``instance`` entry
    #: under an undefined walk. An ambiguous ``swc`` entry keeps its type.
    type: str | None
    refusal: Refusal | None = None
    #: For ``instance-ambiguous``: the exact count and the first paths.
    count: int = 0
    paths: tuple[str, ...] = ()

    def component(self, by_name: Mapping[str, _Component]) -> _Component | None:
        """The declared component the entry resolves to, looked up in *by_name*, or ``None``.

        ``None`` for an entry resolving to no type and for a type *by_name* does not declare.
        """
        return by_name.get(self.type) if self.type is not None else None


@dataclass(frozen=True)
class SchedulingIdentities:
    """The resolver over one composition layer: :meth:`resolve` is the one answer."""

    #: ``None`` when the walk is undefined (no root, several, or a reachable cycle).
    placements: Mapping[str, Placement] | None
    index: Mapping[str, Composition] = field(default_factory=dict)
    root: str | None = None

    #: Why no ``instance`` path is resolved while :attr:`defined` is false.
    undefined_reason: ClassVar[str] = (
        "the composition walk is undefined (no root composition, several, or a cycle the root reaches)"
    )

    @property
    def defined(self) -> bool:
        """Whether the walk is defined, and identities are therefore resolved."""
        return self.placements is not None

    def resolve(self, swc: str | None, instance: str | None) -> Resolution:
        """The entry ``{swc, instance}``'s identity, resolved by the module docstring's table."""
        if self.placements is None:
            return Resolution(identity=syntactic_identity(swc, instance), type=swc)
        if swc is not None:
            placement = self.placements.get(swc)
            if placement is None:
                return Resolution(identity=("unplaced-type", swc), type=swc)
            if placement.count == 1:
                return Resolution(identity=("instance", placement.paths[0]), type=swc)
            return Resolution(
                identity=syntactic_identity(swc, instance),
                type=swc,
                refusal="instance-ambiguous",
                count=placement.count,
                paths=placement.paths,
            )
        # The loader refuses an entry with neither selector.
        assert instance is not None, "an entry names its component by exactly one of swc and instance"
        return self._walk_path(instance)

    def occurrences(self) -> list[tuple[str, str]]:
        """Every root-reachable occurrence as ``(path, type)``, uncapped, in walk order.

        The order matches :attr:`Placement.paths`, and :meth:`resolve` answers
        ``('instance', path)`` with the same type for every path.

        **What it does not reach.** ``[]`` when the walk is undefined. A cycle
        the root does not reach changes nothing, and a composition the root
        does not reach contributes nothing. Not memoised: the cost is proportional to the
        number of occurrences, which a diamond of compositions holding components
        makes exponential in its depth; that output size is inherent and not
        capped. Compositions with no component below them are never descended into.
        """
        if self.placements is None or self.root is None:
            return []
        bearing = _bearing(self.root, self.index)
        answer: list[tuple[str, str]] = []
        # An explicit stack, so a deep nesting meets no recursion limit.
        stack = [("", iter(self.index[self.root].members))]
        while stack:
            prefix, members = stack[-1]
            member = next(members, None)
            if member is None:
                stack.pop()
                continue
            path = f"{prefix}{member.name}"
            if member.type is not None:
                answer.append((path, member.type))
            elif member.composition is not None and bearing.get(member.composition, False):
                stack.append((f"{path}{PATH_SEPARATOR}", iter(self.index[member.composition].members)))
        return answer

    def type_of(self, swc: str | None, instance: str | None) -> str | None:
        """The component type the entry resolves to."""
        return self.resolve(swc, instance).type

    def component_of(
        self, swc: str | None, instance: str | None, by_name: Mapping[str, _Component]
    ) -> _Component | None:
        """The declared component the entry resolves to (:meth:`Resolution.component`)."""
        return self.resolve(swc, instance).component(by_name)

    def _walk_path(self, instance: str) -> Resolution:
        refused = syntactic_identity(None, instance)
        assert self.root is not None  # a defined walk has one root
        current = self.index[self.root]
        segments = instance.split(PATH_SEPARATOR)
        for position, segment in enumerate(segments):
            member = next((m for m in current.members if m.name == segment), None)
            if member is None:
                return Resolution(identity=refused, type=None, refusal="instance-unknown")
            last = position == len(segments) - 1
            if last:
                if member.type is not None:
                    return Resolution(identity=("instance", instance), type=member.type)
                if member.composition is not None:
                    return Resolution(identity=refused, type=None, refusal="instance-not-a-component")
                return Resolution(identity=refused, type=None, refusal="instance-unknown")
            if member.composition is None or member.composition not in self.index:
                return Resolution(identity=refused, type=None, refusal="instance-unknown")
            current = self.index[member.composition]
        return Resolution(identity=refused, type=None, refusal="instance-unknown")  # pragma: no cover - no segment


def syntactic_identity(swc: str | None, instance: str | None) -> Identity:
    """The identity an entry has when nothing is resolved: its own selectors, as written."""
    return ("syntactic", swc, instance)


def identities_from(compositions: Sequence[Composition]) -> SchedulingIdentities:
    """The resolver over a composition layer already in memory.

    The walk is undefined — every identity syntactic — with no root or several
    (root names deduplicated), or with a cycle the root reaches.
    """
    root = _the_root(compositions)
    if root is None:
        return SchedulingIdentities(placements=None)
    index = composition_index(compositions)
    placements = _walk(root, index)
    if placements is None:
        return SchedulingIdentities(placements=None)
    return SchedulingIdentities(placements=placements, index=index, root=root)


def identities_for(model_root: str) -> SchedulingIdentities:
    """The resolver over the composition layer under *model_root*."""
    return identities_from(read_compositions(model_root))


def identity_sort_key(identity: Sequence[str | None]) -> tuple[str, ...]:
    """The one ordering of identities and entry triples: element-wise, ``None`` sorting as ``""``."""
    return tuple("" if part is None else part for part in identity)


def written_selector(swc: str | None, instance: str | None) -> str:
    """The selector as the entry writes it."""
    return swc if swc is not None else instance or ""


def _bearing(root: str, index: Mapping[str, Composition]) -> dict[str, bool]:
    """Whether each composition the *root* reaches has a component member at any depth below it.

    Computed bottom-up by an explicit stack (no recursion limit); a member naming
    an undeclared composition bears nothing. Only asked of a defined walk, so the
    stack ends. A composition the root does not reach is absent from the answer.
    """
    bearing: dict[str, bool] = {}
    stack = [root]
    while stack:
        name = stack[-1]
        if name in bearing:
            stack.pop()
            continue
        members = index[name].members
        pending = [
            m.composition
            for m in members
            if m.composition is not None and m.composition in index and m.composition not in bearing
        ]
        if pending:
            stack.extend(pending)
            continue
        stack.pop()
        bearing[name] = any(
            m.type is not None or (m.composition is not None and bearing.get(m.composition, False)) for m in members
        )
    return bearing
