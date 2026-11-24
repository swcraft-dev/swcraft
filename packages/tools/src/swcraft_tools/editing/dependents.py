"""What refers to a removed component: the operations a ``remove`` ``with_dependents`` adds.

The server's operations run after the client's, each a delta row marked
``origin: "server"``:

* every **connector** with an endpoint on the component, removed whole;
* every **composition member** typed by it, with every **delegation** naming it;
* every **task runnable entry** naming the component, by ``swc`` or by an
  ``instance`` path ending at one of those members;
* every **sequence step** naming it the same way, or the whole **sequence** when
  the change set leaves it no step (a sequence needs one).

**A referrer is left only when the client moved it off the component**; one the
client already removes is not removed twice.

**What it does not reach.** Only the removal of a whole component (``layer: swc``,
no ``path``); the flag elsewhere is refused ``schema-violation``. A requirement's
``derived_from`` naming the component is not unlinked, and an ``instance`` path is
followed from the root composition only. A referrer the client's own ``create`` or
``update`` puts on the component, or keeps there under a changed identity, is not
in the baseline and is not unlinked; pass D refuses it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.address import Address, FieldStep, MemberStep, reduce_address
from swcraft_tools.editing.apply import StagedModel, apply_operations
from swcraft_tools.editing.protocol import ChangeOperation, RemoveOperation
from swcraft_tools.scheduling_identity import PATH_SEPARATOR

__all__ = ["DependentsNotApplicable", "dependent_removals"]


class DependentsNotApplicable(ValueError):
    """``with_dependents`` on a remove it does not apply to (anything but a whole component)."""

    def __init__(self, address: Address) -> None:
        super().__init__(
            "with_dependents applies to the removal of a whole component ({'layer': 'swc', 'element': ...}, no "
            "path); remove the referrers of anything else by their own operations"
        )
        self.address = address


def _member(field: str, identity: Any) -> tuple[FieldStep, MemberStep]:
    return (FieldStep(field), MemberStep(identity))


def _entry_identity(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {"swc": entry.get("swc"), "instance": entry.get("instance"), "runnable": entry.get("runnable")}


def _composition_index(model: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Mapping[str, Any]]:
    """Composition name -> composition in *model*, **first** declaration winning, as ``composition_index`` does."""
    index: dict[str, Mapping[str, Any]] = {}
    for composition in model.get("compositions", ()):
        index.setdefault(composition["name"], composition)
    return index


def _instance_end(instance: str, compositions: Mapping[str, Mapping[str, Any]]) -> tuple[str, str] | None:
    """``(composition, member)`` an ``instance`` path ends at, walked from the root composition.

    Follows :mod:`swcraft_tools.scheduling_identity`'s rules: one root composition
    (none, or several, resolves nothing), the path split on ``PATH_SEPARATOR``, a
    member naming an undeclared composition descending nowhere. A cycle is not looked
    for: a path has finitely many segments, so the walk ends either way.
    """
    roots = [name for name, composition in compositions.items() if composition.get("root")]
    current = compositions[roots[0]] if len(roots) == 1 else None
    segments = instance.split(PATH_SEPARATOR)
    for position, segment in enumerate(segments):
        if current is None:
            return None
        member = next((member for member in current.get("members") or () if member.get("name") == segment), None)
        if member is None:
            return None
        if position == len(segments) - 1:
            return (current["name"], member["name"])
        current = compositions.get(member["composition"]) if member.get("composition") else None
    return None


def _referrers(
    model: Mapping[str, Sequence[Mapping[str, Any]]], component: str
) -> list[tuple[RemoveOperation, tuple[str, str] | None]]:
    """The removals that unlink every referrer of *component* in *model*, each beside the member it hangs on.

    *model* is in ``read``'s published vocabulary (layer name -> elements); removals
    come in layer order. Sequence steps are always listed as step removals; whether a
    sequence goes whole, and which referrers the client moved, is decided by
    :func:`dependent_removals`.

    A delegation hangs on the member it names, an entry or step reached by an
    ``instance`` path on the member the path ends at; the rest hang on none (``None``).
    """
    operations: list[tuple[RemoveOperation, tuple[str, str] | None]] = []
    for connector in model.get("connectors", ()):
        ends = (connector.get("from") or {}, connector.get("to") or {})
        if any(end.get("swc") == component for end in ends):
            operations.append((RemoveOperation(Address(layer="connectors", element=connector["name"])), None))

    compositions = _composition_index(model)
    removed_members: set[tuple[str, str]] = set()
    for composition in compositions.values():
        members = [member["name"] for member in composition.get("members") or () if member.get("type") == component]
        for member in members:
            removed_members.add((composition["name"], member))
            operations.append(
                (
                    RemoveOperation(
                        Address(layer="compositions", element=composition["name"], path=_member("members", member))
                    ),
                    None,
                )
            )
        for delegation in composition.get("delegations") or ():
            if delegation.get("member") in members:
                operations.append(
                    (
                        RemoveOperation(
                            Address(
                                layer="compositions",
                                element=composition["name"],
                                path=_member("delegations", delegation["name"]),
                            )
                        ),
                        (composition["name"], delegation["member"]),
                    )
                )

    def hangs_on(entry: Mapping[str, Any]) -> tuple[bool, tuple[str, str] | None]:
        if entry.get("swc") is not None:
            return entry["swc"] == component, None
        instance = entry.get("instance")
        end = _instance_end(instance, compositions) if isinstance(instance, str) else None
        return end in removed_members, end

    for task in model.get("tasks", ()):
        for entry in task.get("runnables") or ():
            names, end = hangs_on(entry)
            if names:
                operations.append(
                    (
                        RemoveOperation(
                            Address(
                                layer="tasks", element=task["name"], path=_member("runnables", _entry_identity(entry))
                            )
                        ),
                        end,
                    )
                )
    for sequence in model.get("sequences", ()):
        for step in sequence.get("steps") or ():
            names, end = hangs_on(step)
            if names:
                operations.append(
                    (
                        RemoveOperation(
                            Address(
                                layer="sequences",
                                element=sequence["name"],
                                path=_member("steps", _entry_identity(step)),
                            )
                        ),
                        end,
                    )
                )
    return operations


def _held_member(compositions: Mapping[str, Mapping[str, Any]], member: tuple[str, str]) -> Mapping[str, Any] | None:
    """The member ``(composition, member)`` in *compositions*, or ``None`` when it or its composition is gone."""
    composition = compositions.get(member[0])
    if composition is None:
        return None
    return next((held for held in composition.get("members") or () if held.get("name") == member[1]), None)


def _left_hanging(
    address: Address,
    member: tuple[str, str],
    component: str,
    after: Mapping[str, Sequence[Mapping[str, Any]]],
    compositions: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Whether a baseline referrer hanging on *member* is left on the component by the client's operations.

    *address* is the referrer's unreduced baseline address (a delegation, or an entry
    or step reached by an ``instance`` path); *after* is the model the client's
    operations leave and *compositions* its compositions by name. The referrer is left
    hanging when *member* is not held in *after* under another type and the referrer
    is still held and unmoved: the delegation still names *member*, or the entry's
    path now walks to no member at all.

    Raises:
        RuntimeError: *address* is not an element with a member step, which no caller builds.
    """
    held = _held_member(compositions, member)
    if held is not None and held.get("type") != component:
        return False
    step = address.path[1]
    if not isinstance(step, MemberStep) or address.element is None:
        raise RuntimeError("a referrer hanging on a member is addressed as an element and a member step")
    identity = step.member
    if address.layer == "compositions":
        composition = compositions.get(address.element) or {}
        delegation = next(
            (other for other in composition.get("delegations") or () if other.get("name") == identity), None
        )
        return delegation is not None and delegation.get("member") == member[1]
    collection = "runnables" if address.layer == "tasks" else "steps"
    element = next((other for other in after.get(address.layer, ()) if other.get("name") == address.element), None)
    entry = next((other for other in (element or {}).get(collection) or () if _entry_identity(other) == identity), None)
    return entry is not None and _instance_end(entry["instance"], compositions) is None


def covered(address: Address, removed: Sequence[Address]) -> bool:
    """Whether a removal of *removed* already takes *address* away (itself or an element holding it)."""
    return any(
        (other.layer, other.element) == (address.layer, address.element)
        and tuple(other.path) == tuple(address.path)[: len(other.path)]
        for other in removed
    )


def dependent_removals(
    snapshot: Any, operations: Sequence[ChangeOperation], *, registry: reg.Registry | None = None
) -> tuple[ChangeOperation, ...]:
    """The server's removals for every ``with_dependents`` remove among *operations*, in their order.

    Referrers are read off the baseline *snapshot* and judged against the model the
    client's operations leave: **a referrer is skipped only when the client moved it
    off the component**; what hangs on a member or composition the client removes is
    still unlinked. If pass B or C refuses the client's operations, the baseline's
    referrers stand. Nothing is removed twice.

    **A sequence losing its last step goes whole**, as one removal at the place of
    its first step. A sequence whose steps the client alone removes is the client's,
    and pass D answers it.

    Raises:
        DependentsNotApplicable: the flag on a remove that is not of a whole component.
    """
    registry = reg.or_current(registry)
    flagged = [
        operation for operation in operations if isinstance(operation, RemoveOperation) and operation.with_dependents
    ]
    components: list[str] = []
    for operation in flagged:
        component = operation.address.element
        if operation.address.layer != "swc" or component is None or operation.address.path:
            raise DependentsNotApplicable(operation.address)
        components.append(component)
    if not flagged:
        return ()
    before = StagedModel.from_snapshot(snapshot, registry=registry).elements
    outcome = apply_operations(
        snapshot,
        [replace(operation, address=reduce_address(operation.address, registry=registry)) for operation in operations],
        registry=registry,
    )
    after = before if outcome.violations or outcome.inapplicable else outcome.staged.elements
    removed = [
        reduce_address(operation.address, registry=registry)
        for operation in operations
        if isinstance(operation, RemoveOperation)
    ]
    compositions = _composition_index(after)
    added: list[RemoveOperation] = []
    for component in components:
        still = {reduce_address(dependent.address, registry=registry) for dependent, _ in _referrers(after, component)}
        for dependent, member in _referrers(before, component):
            reduced = reduce_address(dependent.address, registry=registry)
            if covered(reduced, removed):
                continue
            if reduced not in still and (
                member is None or not _left_hanging(dependent.address, member, component, after, compositions)
            ):
                continue
            removed.append(reduced)
            added.append(RemoveOperation(reduced))
    return tuple(_whole_sequences(after, added, registry))


def _whole_sequences(
    after: Mapping[str, Sequence[Mapping[str, Any]]],
    added: list[RemoveOperation],
    registry: reg.Registry,
) -> list[RemoveOperation]:
    """*added* with the step removals of every sequence left with no step replaced by the sequence's own removal.

    *after* is the model the client's operations leave.
    """
    emptied: set[str] = set()
    for sequence in after.get("sequences", ()):
        name = sequence["name"]
        server_steps = [
            operation.address
            for operation in added
            if operation.address.layer == "sequences" and operation.address.element == name
        ]
        if not server_steps:
            continue
        steps = [
            reduce_address(
                Address(layer="sequences", element=name, path=_member("steps", _entry_identity(step))),
                registry=registry,
            )
            for step in sequence.get("steps") or ()
        ]
        if all(covered(step, server_steps) for step in steps):
            emptied.add(name)
    result: list[RemoveOperation] = []
    for operation in added:
        address = operation.address
        if address.layer == "sequences" and address.element in emptied:
            whole = Address(layer="sequences", element=address.element)
            if whole not in (other.address for other in result):
                result.append(RemoveOperation(whole))
            continue
        result.append(operation)
    return result
