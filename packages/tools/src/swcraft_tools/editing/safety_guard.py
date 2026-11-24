"""The safety guard: whether a change set weakens a check the project relies on.

Nothing here does I/O, prompts, or decides anything a human decides: it answers
*does this weaken a check*, and the host owns *may it proceed*. The change kinds
are :mod:`swcraft_tools.authorization`'s four classifiers, never a fork.

This module owns the **trigger** (:func:`is_safety_relevant`), the **inputs** and
the **comparison**: outside ``hardware`` :func:`_decide` compares the safety
readings of the baseline and the staged model **owner by owner**, and every owner
that lost protection is a question. The one exemption is a uniquely paired
``remove``/``create`` identical up to identity, a rename (:func:`_detect_renames`).
No count of anything is consulted.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from swcraft_tools.authorization import (
    SafetyChange,
    SafetyChangeKind,
    SafetyClassificationError,
    SafetyVerdict,
    classify_allocation_act,
    classify_argument_act,
    classify_safety_act,
    classify_trust_act,
)
from swcraft_tools.editing.address import (
    Address,
    FieldStep,
    KeyStep,
    MemberStep,
)
from swcraft_tools.editing.authorization import (
    DEFAULT_TRUST,
    DELETE_ELEMENT,
    HARDWARE_LAYER,
    LINKING_MEMBERS,
    MAPPING_ARGUMENTS_BY_LAYER,
    MEMBER_UNLINK_DEPTH,
    OWN_UNLINK_DEPTH,
    SAFETY_ALLOCATION_FIELDS,
    SAFETY_ARGUMENT_FIELDS,
    SAFETY_ARGUMENT_KEYS,
    SAFETY_FIELDS,
    SAFETY_MEMBER_FIELDS,
    SAFETY_UNLINK_FIELDS,
    SCALAR_ARGUMENTS_BY_LAYER,
    UNDECLARED,
    UPDATE_RESOURCE,
)
from swcraft_tools.editing.protocol import DeltaEntry
from swcraft_tools.editing.renames import element_rename_pairs, identity_stripped, unique_pairing
from swcraft_tools.safety import NO_ASIL, asil_rank, max_asil
from swcraft_tools.scheduling_identity import identities_from
from swcraft_tools.schema import ExternalInterface, Hardware, Partition
from swcraft_tools.snapshot import ModelSnapshot
from swcraft_tools.trace import allocated_requirement_ids, component_requirement_ids, parse_link
from swcraft_tools.validator import SafetyReading, safety_reading

__all__ = [
    "is_safety_relevant",
    "safety_verdict",
]

# ---------------------------------------------------------------------------
# The safety guard: the trigger, re-expressed as an address predicate
# ---------------------------------------------------------------------------


def is_safety_relevant(operation: str, address: Address) -> bool:
    """Whether this ``{operation, address}`` reaches the safety classifier at all.

    For ``hardware``: an ``update`` entering ``threshold``, ``size``, ``enforcement``
    or ``asil_capability``; a ``create`` of a member at a
    :data:`~swcraft_tools.editing.authorization.SAFETY_MEMBER_FIELDS` field (a core
    admitted to ``accessible``); a ``remove`` of the element. Other layers dispatch
    over :data:`_LAYER_BRANCHES`.

    **Firing is not a verdict**: removing a processor fires too, and the classifier
    answers an empty verdict.
    """
    if address.element is None:
        return False
    branch = _LAYER_BRANCHES.get(address.layer)
    if branch is None:
        return False
    return branch.fires(operation, address)


def _hardware_branch(operation: str, address: Address) -> bool:
    """The ``hardware`` trigger: a budget or capability field updated, a core admitted, the element removed."""
    if operation == "remove":
        # The element itself. A `remove` INSIDE it — a member of `accessible`
        # included — narrows something and is never a weakening.
        return not address.path
    if operation == "create":
        return _is_admitted_member(address)
    if operation != "update" or not address.path:
        return False
    first = address.path[0]
    return isinstance(first, FieldStep) and first.field in SAFETY_FIELDS


def _swc_branch(operation: str, address: Address) -> bool:
    """What a port carries or is, the component's own level, and the whole element.

    Fires on a ``measures`` entry created at its key; a port created or removed whole;
    the component's own ``asil`` in every direction; the component created or removed
    whole (an added element is one ``create`` with no member entries); and **every**
    update inside ``ports/<port>`` except the measure's own text, since a
    ``direction`` or ``interface`` change can make an inert ``measures`` map live.
    """
    if operation in {"create", "remove"} and not address.path:
        return True
    if operation == "create":
        return _is_declared_argument_key(address) or _is_element_member(address, "ports")
    if operation == "remove":
        return _is_element_member(address, "ports") or _is_unlink(address)
    if operation != "update" or not address.path:
        return False
    first = address.path[0]
    if isinstance(first, FieldStep) and first.field in SAFETY_ALLOCATION_FIELDS:
        return True
    # Anything inside one port, bar the measure's own text: rewording it removes
    # no check, since the consumer rule reads the key.
    return _is_inside_element_member(address, "ports") and not _is_declared_argument_key(address)


def _tasks_branch(operation: str, address: Address) -> bool:
    """An ``update`` entering ``coexistence``, or the task created or removed whole.

    The direction is the classifier's in all three, as for ``enforcement``.
    """
    if operation in {"create", "remove"}:
        return not address.path
    if operation != "update" or not address.path:
        return False
    first = address.path[0]
    return isinstance(first, FieldStep) and first.field in SAFETY_ARGUMENT_FIELDS


def _requirements_branch(operation: str, address: Address) -> bool:
    """The ``asil`` a requirement allocates, updated, or the requirement removed whole.

    Removing one ``satisfies`` member is the ``swc`` branch's (:func:`_is_unlink`):
    the link lives in the component.
    """
    if operation == "remove" and not address.path:
        return True
    if not address.path:
        return False
    first = address.path[0]
    if not isinstance(first, FieldStep):
        return False
    if operation == "update":
        return first.field in SAFETY_ALLOCATION_FIELDS
    return False


def _interfaces_branch(operation: str, address: Address) -> bool:
    """A data element's ``asil`` updated, the data element removed, or the interface removed whole.

    The level sits on a member, so this branch reads the whole path; removing the
    interface takes every rated element it declares with it.
    """
    if operation == "remove":
        return not address.path or _is_element_member(address, "data_elements")
    if operation != "update" or len(address.path) != 3:
        return False
    field, member, leaf = address.path
    return (
        isinstance(field, FieldStep)
        and field.field == "data_elements"
        and isinstance(member, MemberStep)
        and isinstance(leaf, FieldStep)
        and leaf.field in SAFETY_ALLOCATION_FIELDS
    )


def _is_unlink(address: Address) -> bool:
    """A ``satisfies`` member of a component: ``satisfies/<m>`` or ``ports|runnables/<x>/satisfies/<m>``.

    These are the sites :func:`~swcraft_tools.trace.component_requirement_ids` reads
    the allocation from; a link on another kind allocates nothing.
    """
    path = address.path
    if len(path) == OWN_UNLINK_DEPTH:
        field, member = path
        return isinstance(field, FieldStep) and field.field in SAFETY_UNLINK_FIELDS and isinstance(member, MemberStep)
    if len(path) == MEMBER_UNLINK_DEPTH:
        owner, owned, field, member = path
        return (
            isinstance(owner, FieldStep)
            and owner.field in LINKING_MEMBERS
            and isinstance(owned, MemberStep)
            and isinstance(field, FieldStep)
            and field.field in SAFETY_UNLINK_FIELDS
            and isinstance(member, MemberStep)
        )
    return False


def _unlink_resource(element: str, address: Address) -> str:
    """The resource an unlink is rendered on: ``Ctl``, ``Ctl.ports[pIn]`` or ``Ctl.runnables[Run10ms]``.

    The site rides in the resource, so the classifier's field is always ``satisfies``.
    """
    if len(address.path) == MEMBER_UNLINK_DEPTH:
        owner, owned = address.path[0], address.path[1]
        assert isinstance(owner, FieldStep) and isinstance(owned, MemberStep)
        return f"{element}.{owner.field}[{owned.member}]"
    return element


def _is_element_member(address: Address, field_name: str) -> bool:
    """``{field: field_name, member: m}``: one member of one named collection.

    Kept apart from :func:`_is_admitted_member`, which is keyed on a set the
    classifier also reads.
    """
    if len(address.path) != 2:
        return False
    field, member = address.path
    return isinstance(field, FieldStep) and field.field == field_name and isinstance(member, MemberStep)


def _is_inside_element_member(address: Address, field_name: str) -> bool:
    """``{field: field_name, member: m}, ...``: at or below one member of one collection."""
    if len(address.path) < 2:
        return False
    field, member = address.path[0], address.path[1]
    return isinstance(field, FieldStep) and field.field == field_name and isinstance(member, MemberStep)


def _is_declared_argument_key(address: Address) -> bool:
    """``{field: f, member: m, field: a, key: k}`` where ``(f, a)`` is admitted.

    Exactly four steps ending in a **key**: a mapping entry is addressed by its key
    and a list member by its identity, so reading one as the other would admit an
    address the differ never emits.
    """
    if len(address.path) != 4:
        return False
    field, member, argument, key = address.path
    return (
        isinstance(field, FieldStep)
        and isinstance(member, MemberStep)
        and isinstance(argument, FieldStep)
        and isinstance(key, KeyStep)
        and (field.field, argument.field) in SAFETY_ARGUMENT_KEYS
    )


def _is_admitted_member(address: Address) -> bool:
    """``{field: f, member: m}`` where *f* is a list-shaped safety field.

    A deeper path is not a member of ``accessible`` (it holds bare core names), and
    the one-step field address is a reordering ``update`` that never reaches here.
    """
    if len(address.path) != 2:
        return False
    field, member = address.path
    return isinstance(field, FieldStep) and field.field in SAFETY_MEMBER_FIELDS and isinstance(member, MemberStep)


def safety_verdict(
    delta: Sequence[DeltaEntry],
    baseline: ModelSnapshot,
    *,
    staged: ModelSnapshot | None = None,
    change_set_id: str = "change_set",
) -> SafetyVerdict:
    """Every way this change set would weaken a check.

    The guard is **always on**, scoped or not; no changes means not a safety act. In
    ``hardware`` the element's own values are compared; everywhere else, and for a
    partition, the two models decide (:func:`_decide`) and the delta only attributes.

    Args:
        staged: the model the change set produces; required outside ``hardware`` and
            for a partition or an element the baseline lacks.
        change_set_id: recorded as the verdict's ``tool``.

    Returns:
        One verdict carrying **every** weakening, never the first found. Order: the
        attributed non-``hardware`` weakenings in delta order, then unattributed
        owners in :func:`_decide`'s order, then the ``hardware`` ones per element in
        first-reached order.

    Raises:
        SafetyClassificationError: a triggering ``hardware`` entry names an element
            the baseline lacks; an ``after`` value cannot be compared; a layer or
            change kind has no classifier or owner; or *staged* is missing where it is
            required; never an empty verdict instead. An unresolved non-hardware
            element allocates nothing and does not raise.
    """
    triggering = [entry for entry in delta if is_safety_relevant(entry.operation, entry.address)]
    # Every entry outside `hardware`, whether or not a shape arm fires for it:
    # the comparison decides, and the entry that made an argument live need not
    # be one an arm looks at (a QM runnable joining an argued task). `hardware`
    # skips the comparison soundly: its one rule-read value, `asil_capability`,
    # is decided by the fold from the element alone.
    outside_hardware = [entry for entry in delta if entry.address.layer != HARDWARE_LAYER]
    # A partition is a `hardware` element the comparison decides (families 7-9),
    # so an entry naming one pays for the comparison too.
    # Materialized once: the layer may be a one-shot iterable, read three times.
    hardware = tuple(baseline.hardware)
    staged_hardware = tuple(staged.hardware) if staged is not None else None
    partition_touched = _touches_a_partition(delta, hardware, staged_hardware)
    changes: list[SafetyChange] = []
    notes: tuple[str, ...] = ()
    # Cores and memories alone pay for no comparison; every other change set
    # does, since only the comparison can tell whether it moved an owner.
    if outside_hardware or partition_touched:
        if staged is None:
            # Fail closed: a guard that cannot read the values after the change
            # must not report "not weakening".
            raise SafetyClassificationError(
                "the safety guard needs the staged model to classify a non-hardware entry; "
                f"{len(outside_hardware)} entr{'y' if len(outside_hardware) == 1 else 'ies'} "
                f"outside '{HARDWARE_LAYER}' reached it without one"
            )
        renames = _detect_renames(delta)
        assert staged_hardware is not None  # materialized above, from the same `staged`
        decided, witnessed = _decide(baseline, staged, renames, hardware, staged_hardware)
        # After the comparison: a note is scoped to an ambiguity that reached a
        # reported owner. The connection notes follow, one per family-6 owner.
        notes = _borne_notes(renames.ambiguities, decided) + witnessed
        attributed: set[_Owner] = set()
        for entry in triggering:
            if entry.address.layer == HARDWARE_LAYER:
                continue
            for change in _classify_outside_hardware(entry, baseline, staged):
                owners = [owner for owner in _owners_of(entry, change, baseline, staged) if owner in decided]
                if owners:
                    changes.append(change)
                    attributed.update(owners)
        # The unattributed pass, in `_decide`'s order; partition owners
        # (families 7-9) are always here, since the loop above skips `hardware`.
        changes.extend(change for owner, change in decided.items() if owner not in attributed)
    updates, removals, order = _group_by_element(
        [entry for entry in triggering if entry.address.layer == HARDWARE_LAYER], hardware
    )
    for name in order:
        if name in removals:
            changes.extend(classify_safety_act(DELETE_ELEMENT, _delete_args(name), hardware).changes)
        if name in updates:
            changes.extend(classify_safety_act(UPDATE_RESOURCE, {"name": name, **updates[name]}, hardware).changes)
    return SafetyVerdict(change_set_id, tuple(changes), notes)


def _touches_a_partition(
    delta: Sequence[DeltaEntry], baseline: Sequence[Hardware], staged: Sequence[Hardware] | None
) -> bool:
    """Whether a ``hardware`` entry of this delta names a partition, in either model.

    A partition's level, argument and trust are :func:`_decide`'s owners, which the
    outside-``hardware`` trigger does not see. Any element of the entry's name that is
    a :class:`~swcraft_tools.schema.Partition` counts, so a duplicate name cannot hide
    one behind a core. *baseline* and *staged* are the two ``hardware`` layers;
    *staged* is ``None`` without a staged model, which is enough for an entry naming a
    core, memory or processor the baseline holds.

    Raises:
        SafetyClassificationError: a ``hardware`` entry names a baseline partition, or
            an element the baseline lacks, and *staged* is ``None``.
    """
    entries = [entry for entry in delta if entry.address.layer == HARDWARE_LAYER and entry.address.element]
    if not entries:
        return False

    def partition_names(elements: Sequence[Hardware]) -> set[str]:
        """The names some element of which is a partition."""
        return {element.name for element in elements if isinstance(element, Partition)}

    in_baseline = {element.name for element in baseline}
    baseline_partitions = partition_names(baseline)
    staged_partitions = partition_names(staged) if staged is not None else set()
    touched = False
    for entry in entries:
        name = entry.address.element
        assert name is not None  # filtered above
        if staged is None:
            if name not in in_baseline or name in baseline_partitions:
                raise SafetyClassificationError(
                    f"the safety guard needs the staged model to classify the hardware entry at '{name}': "
                    "it names a partition, or an element the baseline does not hold"
                )
            continue
        touched = touched or name in baseline_partitions or name in staged_partitions
    return touched


#: One thing a question can be asked about: the family first, then the owner's
#: names. Families: ``component``, ``element`` (interface, data element),
#: ``measure-in``/``measure-out`` and ``stubbed-source``/``stubbed-connection``
#: (component, port), ``coexistence-in``/``coexistence-out`` (task), and
#: ``partition``, ``partition-coexistence-in``/``-out``, ``partition-trust``.
#:
#: **The owner is the whole of what decides.** A model-wide count (of findings,
#: or of arguments keyed without the owner) can be spent by an unrelated element
#: carrying the same key, cancelling a real weakening; a per-owner comparison
#: cannot. Nothing here is a count.
_Owner = tuple[Any, ...]


#: Which :func:`_decide` owner families a rename in a given shape could move,
#: keyed by layer and, for a collection member, its field.
#:
#: Renames are paired in any layer, but only these shapes are consulted by a
#: :class:`_Renames` accessor; ``requirements`` is read by content, so its names
#: move nothing, and ``hardware`` is consulted for partitions alone. A shape
#: absent here suppresses nothing, so :func:`_borne_notes` tells the reader
#: nothing about its ambiguity. A test holds this table equal to the accessors'
#: literal arguments in this module.
_AMBIGUITY_OWNER_FAMILIES: Mapping[tuple[str, ...], tuple[str, ...]] = MappingProxyType(
    {
        ("element", "swc"): ("component", "measure-out", "measure-in", "stubbed-source"),
        ("element", "interfaces"): ("element",),
        ("element", "tasks"): ("coexistence-out", "coexistence-in"),
        ("element", "hardware"): (
            "partition",
            "partition-coexistence-out",
            "partition-coexistence-in",
            "partition-trust",
        ),
        ("member", "swc", "ports"): ("measure-out", "measure-in", "stubbed-source"),
        ("member", "interfaces", "data_elements"): ("element",),
    }
)


@dataclass(frozen=True)
class _Ambiguity:
    """One content shape whose ``remove``/``create`` pairing was not unique.

    Carried rather than rendered, because whether it is worth a sentence depends on
    what :func:`_decide` reports; :func:`_borne_notes` decides that.
    """

    #: The :func:`_detect_renames` shape: ``("element", layer, content)`` or
    #: ``("member", layer, element, field, content)``.
    shape: _Owner
    #: The baseline names removed under it, in delta order.
    removals: tuple[str, ...]
    #: The staged names created under it, in delta order.
    creations: tuple[str, ...]

    def owner_prefixes(self) -> tuple[_Owner, ...]:
        """Every :func:`_decide` owner prefix this ambiguity could have suppressed.

        A prefix, because one name reaches several owners (a component's level and each
        measured port). Both removals and creations are read. Empty for every shape
        :data:`_AMBIGUITY_OWNER_FAMILIES` does not name.
        """
        if self.shape[0] == "element":
            key: tuple[str, ...] = ("element", self.shape[1])
            head: tuple[Any, ...] = ()
        else:
            key = ("member", self.shape[1], self.shape[3])
            head = (self.shape[2],)
        return tuple(
            (family, *head, name)
            for family in _AMBIGUITY_OWNER_FAMILIES.get(key, ())
            for name in (*self.removals, *self.creations)
        )

    def bore_on(self, owners: Iterable[_Owner]) -> bool:
        """Whether this ambiguity actually reached one of the owners being reported."""
        prefixes = self.owner_prefixes()
        if not prefixes:
            return False
        reported = tuple(owners)
        return any(owner[: len(prefix)] == prefix for prefix in prefixes for owner in reported)

    def note(self) -> str:
        """Why the reader is asked about what they may read as a rename.

        The names are the client's; ``editing/review.py`` escapes them where the prompt is
        composed. The sentence points **above** itself because
        :func:`~swcraft_tools.editing.review.render_safety_review` puts it below the
        weakenings.
        """
        if self.shape[0] == "element":
            where = f"layer {self.shape[1]!r}"
        else:
            where = f"{self.shape[2]!r}.{self.shape[3]} in layer {self.shape[1]!r}"
        return (
            f"in {where}, {', '.join(self.removals)} were removed and {', '.join(self.creations)} created "
            "with identical content, so which became which cannot be told from the change set; no rename "
            "was recognised and each removal is judged on its own, which is why one of these names is "
            "asked about above"
        )


def _borne_notes(ambiguities: Sequence[_Ambiguity], decided: Iterable[_Owner]) -> tuple[str, ...]:
    """The sentences for the ambiguities that bore on a reported owner.

    **What this does not reach.** It does not prove the group *caused* the owner's
    question, so it may over-tell, the safe direction.
    """
    reported = tuple(decided)
    return tuple(ambiguity.note() for ambiguity in ambiguities if ambiguity.bore_on(reported))


@dataclass(frozen=True)
class _Renames:
    """The ``remove`` + ``create`` pairs of this delta that are renames.

    A pair whose content is **identical up to identity** is a rename, and it is the
    **only** thing that exempts anything. Renames are decided from the delta, never
    from a count, which cannot tell "the bearer moved" from "another element carries
    the same value". ``canonical_*`` reads a staged name back to the baseline name;
    ``staged_*`` reads a baseline name forward.

    **What this does not reach.** A rename bundled with any edit is a removal and a
    creation, and the removal asks. A rename of a data element a ``measures`` key
    names reads as the measure withdrawn and declared again. A pairing that is not
    unique is no rename: none is exempted and :attr:`ambiguities` records it. All
    three fail safe.
    """

    #: ``(kind, ...)`` at the BASELINE name -> the staged name.
    forward: Mapping[_Owner, str]
    #: ``(kind, ...)`` at the STAGED name -> the baseline name.
    backward: Mapping[_Owner, str]
    #: One entry per content shape whose pairing was ambiguous, in delta order;
    #: :func:`_borne_notes` renders the ones that bore on a reported owner.
    ambiguities: tuple[_Ambiguity, ...] = ()

    def canonical_element(self, layer: str, name: str) -> str:
        """*name* as the staged model files it, read back to the baseline's name."""
        return self.backward.get(("element", layer, name), name)

    def staged_element(self, layer: str, name: str) -> str:
        """*name* as the baseline files it, read forward to the staged model's name."""
        return self.forward.get(("element", layer, name), name)

    def canonical_member(self, layer: str, element: str, field_name: str, member: str) -> str:
        """*member* as the staged model files it, read back to the baseline's name.

        *element* is the baseline name: a renamed element carries its members inside its
        own value, so this is only asked of a member of an element that kept its name.
        """
        return self.backward.get(("member", layer, element, field_name, member), member)

    def staged_member(self, layer: str, element: str, field_name: str, member: str) -> str:
        """*member* as the baseline files it, read forward to the staged model's name."""
        return self.forward.get(("member", layer, element, field_name, member), member)


def _member_rename_key(entry: DeltaEntry, value: Any) -> tuple[_Owner, str] | None:
    """This entry's member rename shape and the name it carries, or ``None``.

    One member of one collection inside an element that is not itself renamed. The
    whole-element shape is :func:`~swcraft_tools.editing.renames.element_rename_pairs`,
    shared with staging.
    """
    content = identity_stripped(value)
    if content is None:
        return None
    address = entry.address
    if address.element is None or len(address.path) != 2:
        return None
    field_step, member = address.path
    if not isinstance(field_step, FieldStep) or not isinstance(member, MemberStep):
        return None
    if not isinstance(member.member, str):
        return None
    return ("member", address.layer, address.element, field_step.field, content), member.member


def _element_triple(entry: DeltaEntry, value: Any) -> tuple[str, str, Any] | None:
    """A whole-element entry as :func:`~swcraft_tools.editing.renames.element_rename_pairs` takes it."""
    address = entry.address
    if address.element is None or address.path:
        return None
    return address.layer, address.element, value


def _detect_renames(delta: Sequence[DeltaEntry]) -> _Renames:
    """The ``remove``/``create`` pairs whose content is identical up to identity.

    **A pairing that is not unique is not a rename.** A shape pairs only when exactly
    one removal and one creation carry it; otherwise every removal asks. A greedy
    pairing in delta order was silent on a crosswise rename of two otherwise
    identical components at different allocated levels.

    Moved references are deliberately not read as corroboration: a ``satisfies``
    member leaving ``A`` and appearing on ``D`` is equally a link re-pointed while
    ``A`` was deleted, and a wrong resolution is silence. Uniqueness cannot be wrong
    in that direction, so the ambiguous case over-asks and never goes quiet.

    The ambiguity is recorded, not phrased (:meth:`_Ambiguity.note`). The
    whole-element half is :func:`~swcraft_tools.editing.renames.element_rename_pairs`,
    the function staging also uses, so the verdict and the carried file text cannot
    disagree about what a rename is; the member half is here, under the same rule.
    """
    element_removed: list[tuple[str, str, Any]] = []
    element_created: list[tuple[str, str, Any]] = []
    removed: dict[_Owner, list[str]] = {}
    created: dict[_Owner, list[str]] = {}
    # The delta order of first creation per shape, element and member shapes
    # alike, so the ambiguities of the two halves are recorded in one order.
    first_creation: dict[_Owner, int] = {}
    for entry in delta:
        if entry.operation == "remove":
            triple = _element_triple(entry, entry.before)
            if triple is not None:
                element_removed.append(triple)
                continue
            keyed = _member_rename_key(entry, entry.before)
            if keyed is not None:
                removed.setdefault(keyed[0], []).append(keyed[1])
        elif entry.operation == "create":
            triple = _element_triple(entry, entry.after)
            if triple is not None:
                element_created.append(triple)
                content = identity_stripped(entry.after)
                if content is not None:
                    first_creation.setdefault(("element", triple[0], content), len(first_creation))
                continue
            keyed = _member_rename_key(entry, entry.after)
            if keyed is not None:
                created.setdefault(keyed[0], []).append(keyed[1])
                first_creation.setdefault(keyed[0], len(first_creation))
    forward: dict[_Owner, str] = {}
    backward: dict[_Owner, str] = {}
    elements = element_rename_pairs(element_removed, element_created)
    for layer, was, now in elements.pairs:
        forward[("element", layer, was)] = now
        backward[("element", layer, now)] = was
    pairs, member_ambiguities = unique_pairing(removed, created)
    for shape, was, now in pairs:
        forward[(*shape[:-1], was)] = now
        backward[(*shape[:-1], now)] = was
    ambiguities = [
        _Ambiguity(("element", group.layer, group.content), group.removals, group.creations)
        for group in elements.ambiguities
    ] + [_Ambiguity(shape, removals, creations) for shape, removals, creations in member_ambiguities]
    ambiguities.sort(key=lambda ambiguity: first_creation[ambiguity.shape])
    return _Renames(MappingProxyType(forward), MappingProxyType(backward), tuple(ambiguities))


def _record_decision(decided: dict[_Owner, SafetyChange], owner: _Owner, verdict: SafetyVerdict) -> None:
    """The first change *verdict* carries for *owner*, kept in *decided*.

    Raises:
        SafetyClassificationError: *verdict* carries no change, which contradicts
            the comparison that decided *owner* loses protection.
    """
    if not verdict.changes:
        raise SafetyClassificationError(
            f"the safety guard decided that {owner!r} loses protection and the classifier "
            "produced no change to say so; a decided weakening may not be dropped"
        )
    decided[owner] = verdict.changes[0]


def _decide(
    baseline: ModelSnapshot,
    staged: ModelSnapshot,
    renames: _Renames,
    hardware: Sequence[Hardware],
    staged_hardware: Sequence[Hardware],
) -> tuple[dict[_Owner, SafetyChange], tuple[str, ...]]:
    """Every owner the two models disagree about, with the sentence to ask it in.

    **This is the whole of what decides.** The safety rules' readings of the two
    models are compared **per owner**, never as a total, in nine families:

    1. the component's effective or allocated level lowered, or the bearer removed;
    2. a data element's level lowered, cleared or removed;
    3. a measured port, both directions (:func:`_decide_measured_ports`);
    4. a task's coexistence argument declared, going live, or withdrawn while mixed;
    5. a rated consumer's source withdrawn into a stub (:func:`_stubbed_sources`);
    6. the same loss witnessed by a surviving connection
       (:func:`_stubbed_connections`); family 5 wins on the same owner;
    7. a rated partition's level lowered, cleared or removed;
    8. a partition's argument, family 4's shapes;
    9. a partition's trust class moved across ``trusted``.

    A raise, or an owner only the staged model has, asks nothing; a bearer the
    staged model lacks, and not a rename, asks as removed.

    Returns:
        The owners in family order, each mapped to the change to render if no delta
        entry is attributed to it; and one note per family-6 owner, in that order.

    Raises:
        SafetyClassificationError: a decided owner produces no change from its
            classifier; a decided weakening is never dropped.
    """
    before = safety_reading(
        baseline.swcs, baseline.interfaces, baseline.tasks, baseline.requirements, hardware, baseline.compositions
    )
    after = safety_reading(
        staged.swcs, staged.interfaces, staged.tasks, staged.requirements, staged_hardware, staged.compositions
    )
    decided: dict[_Owner, SafetyChange] = {}
    _decide_components(before, after, renames, decided)
    _decide_data_elements(before, after, renames, decided)
    _decide_measured_ports(before, after, renames, decided)
    staged_tasks = {renames.canonical_element("tasks", task.name) for task in staged.tasks}
    _decide_task_arguments(before, after, renames, staged_tasks, decided)
    # 5 -- the consumed source withdrawn into a stub. Only where the staged
    # model holds a stub of either kind, so a project without one pays nothing.
    stubs = frozenset(iface.name for iface in staged.interfaces if isinstance(iface, ExternalInterface))
    external = frozenset(swc.name for swc in staged.swcs if swc.external)
    witnessed: list[str] = []
    if stubs or external:
        for owner, verdict in _stubbed_sources(baseline, staged, before, stubs, renames, external):
            _record_decision(decided, owner, verdict)
        # 6 -- the same loss witnessed by a surviving connection, after family 5
        # and never instead of it: the helper reads `decided` to let family 5 win.
        for owner, verdict, note in _stubbed_connections(baseline, staged, before, stubs, external, decided):
            _record_decision(decided, owner, verdict)
            witnessed.append(note)
    _decide_partition_levels(before, after, renames, decided)
    staged_partitions = {
        renames.canonical_element("hardware", element.name)
        for element in staged_hardware
        if isinstance(element, Partition)
    }
    _decide_partition_arguments(before, after, renames, staged_partitions, decided)
    _decide_partition_trust(before, after, renames, staged_partitions, decided)
    return decided, tuple(witnessed)


def _decide_components(
    before: SafetyReading,
    after: SafetyReading,
    renames: _Renames,
    decided: dict[_Owner, SafetyChange],
) -> None:
    """Family 1: the component's level, per component the baseline reads.

    Both readings are compared.
    :attr:`~swcraft_tools.validator.SafetyReading.effective` is what
    ``safety.consumer-above-element`` and ``safety.task-mixed-asil`` read;
    :attr:`~swcraft_tools.validator.SafetyReading.allocated` is what
    ``safety.component-below-requirements`` compares against, and it moves where the
    effective level does not (a self-declaring component whose requirement is lowered
    or unlinked). An unlink, a requirement lowered or removed and the component's own
    lowering are one movement at one place here, so bundling cannot cancel them.
    """
    # Only the baseline's components: one the staged model gained carries nothing away.
    effective_after = {renames.canonical_element("swc", name): level for name, level in after.effective.items()}
    allocated_after = {renames.canonical_element("swc", name): level for name, level in after.allocated.items()}
    for name, was_effective in before.effective.items():
        was_allocated = before.allocated.get(name, NO_ASIL)
        now_effective = effective_after.get(name)
        if now_effective is None:
            if asil_rank(was_effective) > 0 or asil_rank(was_allocated) > 0:
                _record_decision(
                    decided, ("component", name), classify_allocation_act(name, "asil", was_effective, None)
                )
            continue
        now_allocated = allocated_after.get(name, NO_ASIL)
        if asil_rank(now_effective) < asil_rank(was_effective):
            _record_decision(
                decided, ("component", name), classify_allocation_act(name, "asil", was_effective, now_effective)
            )
        elif asil_rank(now_allocated) < asil_rank(was_allocated):
            _record_decision(
                decided, ("component", name), classify_allocation_act(name, "asil", was_allocated, now_allocated)
            )


def _decide_data_elements(
    before: SafetyReading,
    after: SafetyReading,
    renames: _Renames,
    decided: dict[_Owner, SafetyChange],
) -> None:
    """Family 2: the data element's level, per ``(interface, element)``.

    ``safety.provider-below-element``'s bearer: a rated element lowered, cleared,
    or removed and not renamed. One newly rated asks nothing.
    """
    # Walked over the nested map rather than flattened `(interface, element)`
    # keys, which would build one tuple per declared element on large models.
    for interface, declared in before.elements.items():
        staged_interface = renames.staged_element("interfaces", interface)
        staged_declared = after.elements.get(staged_interface, {})
        for element, held in declared.items():
            if held.asil is None:
                continue
            staged_data_element = renames.staged_member("interfaces", interface, "data_elements", element)
            staged_held = staged_declared.get(staged_data_element)
            resource = f"{interface}.{element}"
            if staged_held is None or staged_held.asil is None:
                _record_decision(
                    decided, ("element", interface, element), classify_allocation_act(resource, "asil", held.asil, None)
                )
            elif asil_rank(staged_held.asil) < asil_rank(held.asil):
                _record_decision(
                    decided,
                    ("element", interface, element),
                    classify_allocation_act(resource, "asil", held.asil, staged_held.asil),
                )


def _decide_measured_ports(
    before: SafetyReading,
    after: SafetyReading,
    renames: _Renames,
    decided: dict[_Owner, SafetyChange],
) -> None:
    """Family 3: the measured port, per ``(component, port)``, in both directions.

    What stops being readable is withdrawn protection (port removed, map dropped,
    direction flipped, interface re-pointed); what is newly declared or newly
    answering is the declaring direction, including an existing argument going live
    because the element it names was rated.
    """
    for owner in _measured_owners(before, after, renames):
        component, port = owner
        renamed = (renames.staged_element("swc", component), renames.staged_member("swc", component, "ports", port))
        resource = f"{component}.{port}"
        gone = sorted(before.readable(owner) - after.readable(renamed))
        if gone:
            _record_decision(
                decided, ("measure-out", *owner), classify_allocation_act(resource, "measures", gone, None)
            )
        declared_after = after.measures.get(renamed, {})
        # Newly readable is declaring; newly answering is an existing map going
        # live. A key arriving unreadable (a `provide` port, an unknown element)
        # declares nothing: other rules report it.
        fresh = (after.readable(renamed) - before.readable(owner)) | (
            after.answering(renamed) - before.answering(owner)
        )
        if fresh:
            _record_decision(
                decided,
                ("measure-in", *owner),
                classify_argument_act(
                    f"{renamed[0]}.{renamed[1]}",
                    "measures",
                    None,
                    {key: declared_after[key] for key in sorted(fresh)},
                ),
            )


def _decide_task_arguments(
    before: SafetyReading,
    after: SafetyReading,
    renames: _Renames,
    staged_tasks: set[str],
    decided: dict[_Owner, SafetyChange],
) -> None:
    """Family 4: the task's coexistence argument, per task, in both directions.

    Asked when the argument is declared (``""`` to a text, or a task created with
    one) or goes live (mixed only after, under a standing text). Inside a task
    already mixed and argued, a member raised, replaced or joining is silent.
    *decided* is extended in place.
    """
    for name in _task_owners(before, after, renames):
        elsewhere = renames.staged_element("tasks", name)
        text_before = before.coexistence.get(name, "")
        text_after = after.coexistence.get(elsewhere, "")
        # `mixed_tasks` is the rule's own condition: an unmixed argued task answers nothing.
        answered_before = bool(text_before) and name in before.mixed_tasks
        went_live = bool(text_before) and elsewhere in after.mixed_tasks and name not in before.mixed_tasks
        if answered_before and name not in staged_tasks:
            _record_decision(
                decided, ("coexistence-out", name), classify_allocation_act(name, "coexistence", text_before, None)
            )
        if text_after and (not text_before or went_live):
            _record_decision(
                decided, ("coexistence-in", name), classify_argument_act(elsewhere, "coexistence", "", text_after)
            )


def _decide_partition_levels(
    before: SafetyReading,
    after: SafetyReading,
    renames: _Renames,
    decided: dict[_Owner, SafetyChange],
) -> None:
    """Family 7: the partition's declared level, per rated partition.

    Lowered, cleared, or removed and not renamed (``LOWER_ALLOCATION``).

    **What this does not reach.** A raise, and a partition's ``core`` re-pointed onto
    an unrated core, which closes ``safety.partition-on-unqualified-core`` unasked.
    """
    # Only the baseline's rated partitions. Unrated after (cleared, or gone and
    # not renamed) reads as `None`, the level withdrawn.
    for name, was in before.partition_levels.items():
        now = after.partition_levels.get(renames.staged_element("hardware", name))
        if now is None or asil_rank(now) < asil_rank(was):
            _record_decision(decided, ("partition", name), classify_allocation_act(name, "asil", was, now))


def _decide_partition_arguments(
    before: SafetyReading,
    after: SafetyReading,
    renames: _Renames,
    staged_partitions: set[str],
    decided: dict[_Owner, SafetyChange],
) -> None:
    """Family 8: the partition's argument, per partition, in family 4's two shapes.

    Declared, or going live (a task re-pointed into an argued rated partition), and
    the argued, mixed partition removed whole. "Mixed" is
    :attr:`~swcraft_tools.validator.SafetyReading.mixed_partitions`, the rule's own
    condition, so the guard and the rule cannot disagree.

    Args:
        before: the safety reading of the baseline.
        after: the safety reading of the staged model.
        renames: the change set's renames.
        staged_partitions: the canonical names of the staged model's partitions.
        decided: the owners decided so far, extended in place.
    """
    for name in _partition_owners(before.partition_coexistence, after.partition_coexistence, renames):
        elsewhere = renames.staged_element("hardware", name)
        text_before = before.partition_coexistence.get(name, "")
        text_after = after.partition_coexistence.get(elsewhere, "")
        answered_before = bool(text_before) and name in before.mixed_partitions
        went_live = bool(text_before) and elsewhere in after.mixed_partitions and name not in before.mixed_partitions
        if answered_before and name not in staged_partitions:
            _record_decision(
                decided,
                ("partition-coexistence-out", name),
                classify_allocation_act(name, "coexistence", text_before, None),
            )
        if text_after and (not text_before or went_live):
            _record_decision(
                decided,
                ("partition-coexistence-in", name),
                classify_argument_act(elsewhere, "coexistence", "", text_after, layer=HARDWARE_LAYER),
            )


def _decide_partition_trust(
    before: SafetyReading,
    after: SafetyReading,
    renames: _Renames,
    staged_partitions: set[str],
    decided: dict[_Owner, SafetyChange],
) -> None:
    """Family 9: the partition's trust, per partition.

    A move up to ``trusted`` is ``GRANT_PARTITION_TRUST``, down from it
    ``DECLARE_PARTITION_PROTECTION``; a partition the baseline lacks reads as
    ``non_trusted``, and ``non_trusted`` <-> ``trusted_with_protection`` asks nothing.
    *decided* is extended in place.

    **What this does not reach.** A removed partition asks nothing (an unrated
    ``trusted`` one closes ``safety.trusted-partition-unrated`` unasked), nor does a
    ``trusted`` partition replaced under another name; a ``core`` re-pointed to an
    undeclared one is left to pass D.
    """
    # Whether the move crossed `trusted` is the classifier's decision; an empty
    # verdict asks nothing and is not recorded.
    for name in _partition_owners(before.partition_trust, after.partition_trust, renames):
        if name not in staged_partitions:
            continue
        elsewhere = renames.staged_element("hardware", name)
        was = before.partition_trust.get(name, DEFAULT_TRUST)
        now = after.partition_trust.get(elsewhere, DEFAULT_TRUST)
        trust_verdict = classify_trust_act(elsewhere, was, now)
        if trust_verdict.changes:
            _record_decision(decided, ("partition-trust", name), trust_verdict)


def _measured_owners(before: SafetyReading, after: SafetyReading, renames: _Renames) -> list[tuple[str, str]]:
    """Every ``(component, port)`` either model declares a ``measures`` map on, canonically keyed.

    Baseline order first, then the owners the staged model gained.
    """
    owners = list(before.measures)
    seen = set(owners)
    for component, port in after.measures:
        canonical = (
            renames.canonical_element("swc", component),
            renames.canonical_member("swc", component, "ports", port),
        )
        if canonical not in seen:
            seen.add(canonical)
            owners.append(canonical)
    return owners


def _task_owners(before: SafetyReading, after: SafetyReading, renames: _Renames) -> list[str]:
    """Every task either model declares a ``coexistence`` text on, canonically keyed."""
    owners = list(before.coexistence)
    seen = set(owners)
    for name in after.coexistence:
        canonical = renames.canonical_element("tasks", name)
        if canonical not in seen:
            seen.add(canonical)
            owners.append(canonical)
    return owners


def _partition_owners(before: Mapping[str, str], after: Mapping[str, str], renames: _Renames) -> list[str]:
    """Every partition either of two per-partition readings names, canonically keyed.

    Baseline order first, then the staged model's additions. A copy of
    :func:`_task_owners` rather than one helper taking the layer, because each
    :class:`_Renames` accessor call must name its layer as a literal, which a test
    reads off the source. A partition ``non_trusted`` on both sides is in neither
    trust reading and could not have crossed ``trusted``.
    """
    owners = list(before)
    seen = set(owners)
    for name in after:
        canonical = renames.canonical_element("hardware", name)
        if canonical not in seen:
            seen.add(canonical)
            owners.append(canonical)
    return owners


def _stubbed_sources(
    baseline: ModelSnapshot,
    staged: ModelSnapshot,
    before: SafetyReading,
    stubs: frozenset[str],
    renames: _Renames,
    external: frozenset[str],
) -> Iterator[tuple[_Owner, SafetyVerdict]]:
    """Every rated consumer's port whose rated source became opaque: an interface stub, or none on a stub.

    The consumer rule abstains on a stub, so this closes a
    ``safety.consumer-above-element`` finding unasked. Asked for a rated component's
    ``require`` port whose baseline interface carries a rated data element and whose
    staged port (through a rename) is still ``require`` and names a stub, or no
    interface on a component now ``external``; not when that interface itself became
    a stub. The ``observed_interface`` hint is not read.

    **What this does not reach.** A ``provide`` port re-pointed to a stub. A port that
    already named a stub. A port recreated under another name on a stub, unless a
    surviving connection witnesses it (:func:`_stubbed_connections`). A stub's
    ``external_dest``.
    """
    for swc in _first_by_name(baseline.swcs).values():
        level = before.effective.get(swc.name, NO_ASIL)
        if asil_rank(level) <= 0:
            continue
        component = renames.staged_element("swc", swc.name)
        seen: set[str] = set()
        for port in swc.ports:
            if port.name in seen:
                continue
            seen.add(port.name)
            if port.direction != "require":
                continue
            if not any(element.asil is not None for element in before.elements.get(port.interface, {}).values()):
                continue
            after = _port_of(staged, component, renames.staged_member("swc", swc.name, "ports", port.name))
            if after is None or after.direction != "require":
                continue
            if after.interface not in stubs and not (after.interface is None and component in external):
                continue
            if renames.staged_element("interfaces", port.interface) in stubs:
                continue
            yield (
                ("stubbed-source", swc.name, port.name),
                classify_allocation_act(f"{swc.name}.{port.name}", "interface", port.interface, after.interface),
            )


#: How long one connection note may be, in characters: ``editing/review.py``'s
#: ``_SUMMARY_LIMIT``, repeated because that module cannot be imported here (a
#: test holds the two equal), so the prompt never elides a note of plain names.
_CONNECTION_NOTE_LIMIT = 400


@dataclass(frozen=True)
class _Witness:
    """One surviving connection whose consumer end moved onto an opaque port."""

    #: How the note names it: ``connector <name>`` or ``delegation <Composition>.<name>``.
    label: str
    #: The staged consumer port the connection now ends on.
    port: str
    #: That port's staged interface: a stub's name, or ``None`` on an external component.
    interface: str | None


def _stubbed_connections(
    baseline: ModelSnapshot,
    staged: ModelSnapshot,
    before: SafetyReading,
    stubs: frozenset[str],
    external: frozenset[str],
    decided: Mapping[_Owner, SafetyChange],
) -> Iterator[tuple[_Owner, SafetyVerdict, str]]:
    """Every rated consumer port a surviving connection moved off onto an opaque port.

    Family 6: a port recreated on a stub under another name, or a connector rewired
    to an existing stub port, escapes family 5, so a surviving connection is the
    evidence. The witness is an assembly connector (same name, ``composition``,
    ``from`` and ``to.swc``, ``to.port`` moved) or a delegation (same name,
    composition, outer ``port`` and ``member`` type, ``member_port`` moved), matched
    by name with no rename reading; the port conditions are family 5's, and family 5
    wins an owner it already holds. Several witnesses give one owner.

    **What this does not reach.** An unconnected port; a connection renamed and
    edited; one whose provider end, consumer component, ``composition`` or outer port
    changed; ``composition`` endpoints and nested compositions; a change split across
    commits; a producer's ``provide`` side.

    Yields:
        ``(owner, verdict, note)`` in first-witness order: connectors, then delegations.
    """
    # One index per model (first wins, as in `_port_of`): one pass over the
    # ports instead of a walk per connection.
    ports_before = _first_ports(baseline)
    ports_after = _first_ports(staged)
    losses: dict[tuple[str, str], tuple[str, list[_Witness]]] = {}

    def witness(label: str, component: str, was: str, now: str) -> None:
        if ("stubbed-source", component, was) in decided:
            return
        if asil_rank(before.effective.get(component, NO_ASIL)) <= 0:
            return
        port = ports_before.get((component, was))
        if port is None or port.direction != "require" or port.interface in stubs:
            return
        if not any(element.asil is not None for element in before.elements.get(port.interface, {}).values()):
            return
        after = ports_after.get((component, now))
        if after is None or after.direction != "require":
            return
        if after.interface not in stubs and not (after.interface is None and component in external):
            return
        losses.setdefault((component, was), (port.interface, []))[1].append(_Witness(label, now, after.interface))

    staged_connectors = _first_by_name(staged.connectors)
    for connector in _first_by_name(baseline.connectors).values():
        now = staged_connectors.get(connector.name)
        if now is None or now.composition != connector.composition or now.from_ != connector.from_:
            continue
        if connector.to.swc is None or now.to.swc != connector.to.swc:
            continue
        witness(f"connector {connector.name}", connector.to.swc, connector.to.port, now.to.port)

    staged_compositions = _first_by_name(staged.compositions)
    for composition in _first_by_name(baseline.compositions).values():
        counterpart = staged_compositions.get(composition.name)
        if counterpart is None:
            continue
        members_before = _first_by_name(composition.members)
        members_after = _first_by_name(counterpart.members)
        delegations_after = _first_by_name(counterpart.delegations)
        for delegation in _first_by_name(composition.delegations).values():
            now = delegations_after.get(delegation.name)
            if now is None or now.port != delegation.port or now.member != delegation.member:
                continue
            member_before = members_before.get(delegation.member)
            member_after = members_after.get(now.member)
            if member_before is None or member_after is None or member_before.type is None:
                continue
            if member_after.type != member_before.type:
                continue
            witness(
                f"delegation {composition.name}.{delegation.name}",
                member_before.type,
                delegation.member_port,
                now.member_port,
            )

    # Every witness is read before the first yield, so `_decide` recording an
    # owner into `decided` while it consumes this cannot change a verdict here.
    for (component, port), (interface, witnesses) in losses.items():
        yield (
            ("stubbed-connection", component, port),
            classify_allocation_act(f"{component}.{port}", "interface", interface, witnesses[0].interface),
            _connection_note(component, port, witnesses),
        )


def _connection_note(component: str, port: str, witnesses: Sequence[_Witness]) -> str:
    """The sentence naming what witnesses one loss, within :data:`_CONNECTION_NOTE_LIMIT` where one witness fits.

    States only what the model carries, never *the same signal*. Where not every
    witness fits, it names the first ones and counts the rest; one that cannot fit a
    single witness is returned over the limit for the prompt's own bound to cut.
    *witnesses* is never empty.
    """
    assert witnesses, "a loss is recorded only with a witness"

    def spelled(shown: Sequence[_Witness], rest: int) -> str:
        groups: dict[tuple[str, str | None], list[str]] = {}
        for item in shown:
            groups.setdefault((item.port, item.interface), []).append(item.label)
        parts = []
        for (new_port, interface), names in groups.items():
            listed = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
            verb = "now ends" if len(names) == 1 else "now end"
            why = (
                f"whose interface {interface} is an external interface stub"
                if interface is not None
                else "which declares no interface on an external component"
            )
            parts.append(f"{listed} {verb} on port {new_port}, {why}")
        counted = f"; and {rest} more connection{'s' if rest != 1 else ''} moved off it" if rest else ""
        ending = (
            "this connection's consumer interface became uncheckable"
            if len(witnesses) == 1
            else "these connections' consumer interfaces became uncheckable"
        )
        return f"{component}.{port}: {'; '.join(parts)}{counted} — {ending}"

    text = ""
    for shown in range(len(witnesses), 0, -1):
        text = spelled(witnesses[:shown], len(witnesses) - shown)
        if len(text) <= _CONNECTION_NOTE_LIMIT:
            break
    return text


def _first_ports(snapshot: ModelSnapshot) -> dict[tuple[str, str], Any]:
    """Every ``(component, port)`` of *snapshot* to its port, first wins on both — :func:`_port_of`'s reading."""
    return {
        (swc.name, port.name): port
        for swc in _first_by_name(snapshot.swcs).values()
        for port in _first_by_name(swc.ports).values()
    }


def _first_by_name(items: Iterable[Any]) -> dict[str, Any]:
    """*items* keyed by ``name``, the first of each name only — the reading every lookup here makes."""
    first: dict[str, Any] = {}
    for item in items:
        first.setdefault(item.name, item)
    return first


def _owners_of(
    entry: DeltaEntry, change: SafetyChange, baseline: ModelSnapshot, staged: ModelSnapshot
) -> tuple[_Owner, ...]:
    """Which owners of :func:`_decide` this candidate is about.

    Attribution only: it chooses the prompt text and never decides. A candidate whose
    owners cannot be named is dropped, and its owner then asks with :func:`_decide`'s
    weaker sentence, so the question survives an attribution that answers nothing.

    Raises:
        SafetyClassificationError: a change kind or layer it has no branch for, so a
            new kind or layer fails closed instead of falling through.
    """
    kind = change.kind
    if kind is SafetyChangeKind.DECLARE_MEASURE:
        return (("measure-in", *_port_of_resource(change.resource)),)
    if kind is SafetyChangeKind.DECLARE_COEXISTENCE:
        return (("coexistence-in", change.resource),)
    if kind is not SafetyChangeKind.LOWER_ALLOCATION:
        raise SafetyClassificationError(f"no owner for safety change kind '{kind}'")
    if change.field == "measures":
        return (("measure-out", *_port_of_resource(change.resource)),)
    if change.field == "coexistence":
        return (("coexistence-out", change.resource),)
    layer = entry.address.layer
    if change.field in SAFETY_UNLINK_FIELDS:
        # The component the address names: the resource carries the site
        # (`Ctl.ports[pIn]`).
        return (("component", str(entry.address.element)),)
    if layer == "swc":
        return (("component", change.resource),)
    if layer == "interfaces":
        interface, _, element = change.resource.partition(".")
        return (("element", interface, element),)
    if layer == "requirements":
        return tuple(("component", link) for link in _allocated_to(baseline, staged, change.resource))
    raise SafetyClassificationError(f"no owner for a lowered allocation in layer '{layer}'")


def _port_of_resource(resource: str) -> tuple[str, str]:
    """``Component.port`` or ``Component.port[key]`` back to its two names.

    The first ``.`` splits, because an AUTOSAR ``Identifier`` carries none, and the
    trailing ``[key]`` is dropped: the owner is the port.
    """
    component, _, rest = resource.partition(".")
    port, _, _ = rest.partition("[")
    return component, port


def _allocated_to(baseline: ModelSnapshot, staged: ModelSnapshot, requirement_id: str) -> list[str]:
    """Every component that satisfies this requirement, in either model.

    Both models, because the movement attributed may be a bearer leaving. Baseline
    order, then the staged model's additions.
    """
    names: list[str] = []
    for snapshot in (baseline, staged):
        for swc in snapshot.swcs:
            if requirement_id in component_requirement_ids(swc) and swc.name not in names:
                names.append(swc.name)
    return names


def _classify_outside_hardware(
    entry: DeltaEntry, baseline: ModelSnapshot, staged: ModelSnapshot
) -> tuple[SafetyChange, ...]:
    """One triggering entry of ``swc``, ``tasks``, ``requirements`` or ``interfaces``.

    Per entry, unlike the per-element hardware fold. These arms **attribute** and do
    not decide: a defect in one costs a worse prompt, never a missing question.

    Raises:
        SafetyClassificationError: the layer has no classifier, so a layer added to one
            half of :data:`_LAYER_BRANCHES` only fails closed.
    """
    branch = _LAYER_BRANCHES.get(entry.address.layer)
    if branch is None or branch.classify is None:
        raise SafetyClassificationError(f"no safety classifier for layer '{entry.address.layer}'")
    return branch.classify(entry, baseline, staged)


def _classify_swc(entry: DeltaEntry, baseline: ModelSnapshot, staged: ModelSnapshot) -> tuple[SafetyChange, ...]:
    """A declared measure, the port that carries or becomes one, the component's own level, and the element."""
    address = entry.address
    element = address.element
    assert element is not None  # is_safety_relevant already refused an element-less address
    if not address.path:
        if entry.operation == "create":
            # The whole component arrives; walked so no nesting depth escapes.
            return _declared_argument_acts(entry.after, element, layer="swc")
        return _withdrawn_by_removing_swc(element, entry.before, baseline)
    if entry.operation == "update" and _is_inside_element_member(address, "ports"):
        return _classify_port_interior(entry, baseline, staged)
    if _is_declared_argument_key(address):
        # `ports/<port>/measures/<element>`: the resource names port and signal.
        # The direction is not read here: a key's entry does not carry it, and
        # the baseline's may be stale if the same delta flips the port.
        # Over-asking about a `provide` port's map is harmless, since pass D
        # refuses it first; missing a live argument would not be.
        port, key = address.path[1], address.path[3]
        assert isinstance(port, MemberStep) and isinstance(key, KeyStep)
        return classify_argument_act(f"{element}.{port.member}[{key.key}]", "measures", None, entry.after).changes
    if _is_element_member(address, "ports"):
        member = address.path[1]
        assert isinstance(member, MemberStep)
        resource = f"{element}.{member.member}"
        if entry.operation == "create":
            # The argument arrives inside its port's `create`; `after` is the
            # whole port, so its direction is read, not assumed.
            return _declared_argument_acts(entry.after, element, layer="swc", resource=resource)
        # A measured `require` port removed takes the consumer rule's question
        # away with no finding in exchange; a `provide` port carries none.
        carried = _mapping_field(entry.before, "measures")
        if not carried or not _reads_the_argument("measures", entry.before):
            return ()
        return classify_allocation_act(resource, "measures", sorted(carried), None).changes
    if entry.operation == "remove" and _is_unlink(address):
        # A `satisfies` member removed, checked before the `asil` fall-through.
        # Parsed to its id: `REQ_1@1` would name no requirement.
        member = address.path[-1]
        assert isinstance(member, MemberStep)
        requirement_id = parse_link(str(member.member)).id
        if not _unlinking_lowers_the_allocation(baseline, requirement_id, element, staged):
            return ()
        return classify_allocation_act(_unlink_resource(element, address), "satisfies", member.member, None).changes
    # The component's own `asil`, compared as the EFFECTIVE level: a declaration
    # withdrawn falls back to what the requirements allocate, which is not a
    # lowering when they allocate the same level or more.
    derived = _derived_component_level(baseline, element)
    return classify_allocation_act(
        element,
        "asil",
        _effective_level(entry.before, derived),
        _effective_level(entry.after, derived),
    ).changes


def _classify_port_interior(
    entry: DeltaEntry, baseline: ModelSnapshot, staged: ModelSnapshot
) -> tuple[SafetyChange, ...]:
    """An ``update`` inside a port: what a rule reads off it before, and what after.

    Attribution only; :func:`_decide` owns the decision. A ``measures`` map answers
    ``safety.consumer-above-element`` only on a ``require`` port whose interface
    declares the key, so an update that declares nothing (a direction flipped, an
    interface re-pointed) can make it live; the importer refuses the same on the other
    write path. Keys newly readable are a ``DECLARE_MEASURE``; keys no longer readable
    a ``LOWER_ALLOCATION``, the same withdrawal as removing the port. An update that
    changes neither is silent.
    """
    address = entry.address
    element = address.element
    member = address.path[1]
    assert element is not None and isinstance(member, MemberStep)
    before = _port_of(baseline, element, member.member)
    if before is None:
        return ()
    after = _port_of(staged, element, member.member)
    readable_before = _readable_measures(before, baseline)
    readable_after = _readable_measures(after, staged)
    resource = f"{element}.{member.member}"
    changes: list[SafetyChange] = []
    newly = {key: text for key, text in readable_after.items() if key not in readable_before}
    if newly:
        changes.extend(classify_argument_act(resource, "measures", None, newly).changes)
    gone = sorted(key for key in readable_before if key not in readable_after)
    if gone:
        changes.extend(classify_allocation_act(resource, "measures", gone, None).changes)
    return tuple(changes)


def _port_of(snapshot: ModelSnapshot, element: str, port: str) -> Any:
    """The named port of the named component, as the model object, or ``None``.

    First wins on both lookups, as ``check_safety_coexistence`` reads a duplicated
    name, so the guard and the rule resolve the same object.
    """
    for swc in snapshot.swcs:
        if swc.name != element:
            continue
        for candidate in swc.ports:
            if candidate.name == port:
                return candidate
        return None
    return None


def _readable_measures(port: Any, snapshot: ModelSnapshot) -> Mapping[str, Any]:
    """The entries of *port*'s ``measures`` map that ``safety.consumer-above-element`` reads.

    A ``require`` port's keys that name a data element of its interface. A second
    spelling of :meth:`~swcraft_tools.validator.SafetyReading.readable` on purpose:
    it is asked of one port to name a prompt's keys, where a whole reading per entry
    would cost a model-wide pass; a disagreement costs wording, never the question.
    """
    if port is None or getattr(port, "direction", None) != "require":
        return {}
    declared = _data_element_names(snapshot, getattr(port, "interface", ""))
    measures = getattr(port, "measures", None)
    if not isinstance(measures, Mapping):
        return {}
    return {key: text for key, text in measures.items() if key in declared}


def _data_element_names(snapshot: ModelSnapshot, interface: str) -> frozenset[str]:
    """The data elements *interface* declares in *snapshot*; empty for a client-server one.

    Empty for an external interface stub too: its ``data_elements`` are names, and a
    stub holds no level, as ``validator.SafetyReading.readable`` also reads it.
    """
    for candidate in snapshot.interfaces:
        if candidate.name == interface:
            if isinstance(candidate, ExternalInterface):
                return frozenset()
            return frozenset(element.name for element in getattr(candidate, "data_elements", ()))
    return frozenset()


def _withdrawn_by_removing_swc(element: str, before: Any, baseline: ModelSnapshot) -> tuple[SafetyChange, ...]:
    """What deleting a whole component takes away: nothing, for most components.

    Its **effective** level where that is a real ASIL (``QM`` is not one,
    ISO 26262-1:2018 3.6 Note 1), and every ``require`` port with a non-empty
    ``measures`` map.

    **What this does not reach.** Deleting a ``QM``-effective component can still
    silence ``safety.provider-below-element`` or ``safety.task-mixed-asil``.
    """
    changes: list[SafetyChange] = []
    effective = _effective_level(_scalar_field(before, "asil"), _derived_component_level(baseline, element))
    if asil_rank(effective) > asil_rank(NO_ASIL):
        changes.extend(classify_allocation_act(element, "asil", effective, None).changes)
    for resource, field, declared in _declared_arguments(before, element, "swc"):
        if field == "measures":
            changes.extend(classify_allocation_act(resource, field, sorted(declared), None).changes)
    return tuple(changes)


def _classify_tasks(entry: DeltaEntry, baseline: ModelSnapshot, staged: ModelSnapshot) -> tuple[SafetyChange, ...]:
    """A ``coexistence`` argument, written into an existing task, arriving with a new one, or leaving with it."""
    element = entry.address.element
    assert element is not None
    if entry.operation == "create":
        return _declared_argument_acts(entry.after, element, layer="tasks")
    if entry.operation == "remove":
        return _withdrawn_by_removing_task(element, entry.before, baseline)
    return classify_argument_act(element, "coexistence", entry.before, entry.after).changes


def _withdrawn_by_removing_task(element: str, before: Any, baseline: ModelSnapshot) -> tuple[SafetyChange, ...]:
    """The argued task deleted: the one task removal that withdraws an answer.

    A bearer carried a ``coexistence`` text over runnables spanning more than one
    effective level.

    **What this does not reach.** Deleting a mixed task with no argument is silent
    though it silences a finding. The span is measured on the baseline, so re-rating
    components in the same change set is not considered.
    """
    text = _scalar_field(before, "coexistence")
    if not isinstance(text, str) or not text:
        return ()
    if len(_levels_spanned(before, baseline)) < 2:
        return ()
    return classify_allocation_act(element, "coexistence", text, None).changes


def _levels_spanned(task: Any, baseline: ModelSnapshot) -> set[int]:
    """The distinct effective levels, by rank, of the components a published task's runnables name.

    ``check_safety_coexistence``'s reading over a delta value: a reference naming no
    declared component contributes nothing, ``swc`` and ``instance`` references are
    both resolved through :mod:`swcraft_tools.scheduling_identity`, and levels are
    counted by rank, so ``B`` and ``B(D)`` are one level, as the rule counts them.
    """
    identities = identities_from(baseline.compositions)
    declared: dict[str, Any] = {}
    for candidate in baseline.swcs:
        # First wins, as the rule reads a duplicated name.
        declared.setdefault(candidate.name, candidate)
    levels: set[int] = set()
    for reference in task.get("runnables", []) if isinstance(task, Mapping) else []:
        if not isinstance(reference, Mapping):
            continue
        selector = reference.get("swc")
        instance = reference.get("instance")
        swc = identities.component_of(
            selector if isinstance(selector, str) else None, instance if isinstance(instance, str) else None, declared
        )
        if swc is None:
            continue
        levels.add(asil_rank(_effective_level(swc.asil, _derived_component_level(baseline, swc.name))))
    return levels


def _classify_requirements(
    entry: DeltaEntry, baseline: ModelSnapshot, staged: ModelSnapshot
) -> tuple[SafetyChange, ...]:
    """The level a requirement allocates: lowered, or removed whole.

    Removing it is a weakening exactly where unlinking one of its bearers would be
    (:func:`_unlinking_lowers_the_allocation`): the same outcome must get the same
    verdict at either address. A requirement with no level allocates nothing, so
    neither shape asks on a project without ASIL.
    """
    address = entry.address
    element = address.element
    assert element is not None
    if not address.path:
        bearers = [swc.name for swc in baseline.swcs if element in component_requirement_ids(swc)]
        if not any(_unlinking_lowers_the_allocation(baseline, element, bearer, staged) for bearer in bearers):
            return ()
        return classify_allocation_act(element, "asil", _scalar_field(entry.before, "asil"), None).changes
    field = address.path[0]
    assert isinstance(field, FieldStep)
    return classify_allocation_act(element, field.field, entry.before, entry.after).changes


def _unlinking_lowers_the_allocation(
    baseline: ModelSnapshot,
    requirement_id: str,
    component: str,
    staged: ModelSnapshot,
) -> bool:
    """Whether withdrawing this requirement from a component lowers what a rule reads about it.

    Whether the allocated maximum drops. **No** when the requirement allocates no
    level, the component is absent from the baseline or the staged model, or the
    staged allocation still reaches the level (so a confirmation edit, ``@3`` to
    ``@4``, is silent).

    **What this does not reach.** It does not re-run the rules, so a component
    declaring its own level at or above the withdrawn one still asks.
    """
    withdrawn = _requirement_level(baseline, requirement_id)
    if withdrawn is None:
        return False
    if not any(swc.name == component for swc in baseline.swcs):
        return False
    if not any(swc.name == component for swc in staged.swcs):
        return False
    remaining = _derived_component_level(staged, component)
    return asil_rank(remaining) < asil_rank(withdrawn)


def _classify_interfaces(entry: DeltaEntry, baseline: ModelSnapshot, staged: ModelSnapshot) -> tuple[SafetyChange, ...]:
    """The level a DATA ELEMENT declares: lowered, removed with the element, or removed with the interface."""
    address = entry.address
    element = address.element
    assert element is not None
    if not address.path:
        return _withdrawn_by_removing_interface(element, entry.before)
    member = address.path[1]
    assert isinstance(member, MemberStep)
    resource = f"{element}.{member.member}"
    if entry.operation == "remove":
        return classify_allocation_act(resource, "asil", _scalar_field(entry.before, "asil"), None).changes
    leaf = address.path[2]
    assert isinstance(leaf, FieldStep)
    return classify_allocation_act(resource, leaf.field, entry.before, entry.after).changes


def _withdrawn_by_removing_interface(element: str, before: Any) -> tuple[SafetyChange, ...]:
    """The whole interface deleted: one change per rated data element it declared.

    The same resource spelling as removing the element alone.

    **What this does not reach.** An interface with only unrated elements is silent
    however many ports consume it, and a port left naming it is pass D's.
    """
    changes: list[SafetyChange] = []
    for member in before.get("data_elements", []) if isinstance(before, Mapping) else []:
        if not isinstance(member, Mapping):
            continue
        level = member.get("asil")
        if level is None:
            continue
        changes.extend(classify_allocation_act(f"{element}.{member.get('name')}", "asil", level, None).changes)
    return tuple(changes)


def _mapping_field(value: Any, field_name: str) -> Mapping[str, Any]:
    """One mapping field of a delta entry's whole-value side, ``{}`` for anything else.

    A delta value is the published wire form; every other shape is tolerated because
    a guard rendering an approval prompt must not raise on one.
    """
    if isinstance(value, Mapping):
        held = value.get(field_name)
        if isinstance(held, Mapping):
            return held
    return {}


def _scalar_field(value: Any, field_name: str) -> Any:
    """One scalar field of a delta entry's whole-value side, ``None`` for anything else."""
    return value.get(field_name) if isinstance(value, Mapping) else None


def _derived_component_level(model: ModelSnapshot, name: str) -> str:
    """The highest level *model*'s requirements allocate to *name*, ``"QM"`` for none.

    Read through the validator's own helpers, so the guard and the validator agree.
    """
    satisfied = allocated_requirement_ids(model.swcs).get(name, set())
    levels = [
        requirement.asil
        for requirement in model.requirements
        if requirement.asil is not None and requirement.id in satisfied
    ]
    return max_asil(levels)


def _requirement_level(baseline: ModelSnapshot, requirement_id: str) -> str | None:
    """The level the baseline's requirement *requirement_id* allocates, or ``None``."""
    for requirement in baseline.requirements:
        if requirement.id == requirement_id:
            return requirement.asil
    return None


def _effective_level(declared: Any, derived: str) -> str:
    """A component's effective level: its own declaration, else what it derives."""
    return declared if isinstance(declared, str) else derived


def _declared_argument_acts(
    value: Any, element: str, *, layer: str, resource: str | None = None
) -> tuple[SafetyChange, ...]:
    """The classification of every declared argument *value* carries, wherever it carries it.

    *layer* is passed through to :func:`classify_argument_act` as well as to the
    walk, so a future ``hardware`` walk would get the partition's kind.
    """
    changes: list[SafetyChange] = []
    for found, field, declared in _declared_arguments(value, resource if resource is not None else element, layer):
        changes.extend(classify_argument_act(found, field, UNDECLARED[field], declared, layer=layer).changes)
    return tuple(changes)


def _declared_arguments(value: Any, resource: str, layer: str) -> Iterator[tuple[str, str, Any]]:
    """Every declared safety argument inside *value*, at whatever depth it sits.

    Yields ``(resource, field, declared)``: a scalar argument is a non-empty string, a
    ``measures`` map a non-empty mapping on a ``require`` port. *resource* gains
    ``.{name}`` per collection member descended into. Only *layer*'s own argument
    fields are looked for.

    **What this does not reach.** Only ``swc`` and ``tasks`` admit an element-level
    ``create``; a layer gaining an argument needs its predicate widened. The walk reads
    the published delta value, so a field a publication elides is not found.
    """
    if isinstance(value, Mapping):
        arguments = MAPPING_ARGUMENTS_BY_LAYER.get(layer, ())
        for field in SCALAR_ARGUMENTS_BY_LAYER.get(layer, ()):
            declared = value.get(field)
            if isinstance(declared, str) and declared:
                yield resource, field, declared
        for field in arguments:
            declared = value.get(field)
            if isinstance(declared, Mapping) and declared and _reads_the_argument(field, value):
                yield resource, field, dict(declared)
        for name, held in value.items():
            if name in arguments:
                continue  # a declared argument is a LEAF: its keys and its text are the user's data
            if isinstance(held, (Mapping, list)):
                yield from _declared_arguments(held, resource, layer)
    elif isinstance(value, list):
        for member in value:
            yield from _declared_arguments(member, _member_resource(resource, member), layer)


def _reads_the_argument(field: str, owner: Any) -> bool:
    """Whether any rule reads *field* off *owner*, given what the owner declares.

    **What it does not ask.** Whether a measure's key names a data element of the
    interface; pass D refuses an unknown key before any prompt.
    """
    if field != "measures":
        return True
    return _scalar_field(owner, "direction") == "require"


def _member_resource(resource: str, member: Any) -> str:
    """*resource* plus the member's own name, where a collection member has one."""
    name = member.get("name") if isinstance(member, Mapping) else None
    return f"{resource}.{name}" if isinstance(name, str) and name else resource


@dataclass(frozen=True)
class _LayerBranch:
    """One layer's half of the guard: what fires, and what classifies it.

    One table, so a layer cannot be added to the trigger and not to the classifier.
    ``classify`` is ``None`` for ``hardware`` alone, whose entries go through
    :func:`_group_by_element`'s per-element fold.
    """

    fires: Callable[[str, Address], bool]
    classify: Callable[[DeltaEntry, ModelSnapshot, ModelSnapshot], tuple[SafetyChange, ...]] | None


#: The layer dispatch. A layer absent from it reaches no classifier at all.
_LAYER_BRANCHES: Mapping[str, _LayerBranch] = MappingProxyType(
    {
        HARDWARE_LAYER: _LayerBranch(_hardware_branch, None),
        "swc": _LayerBranch(_swc_branch, _classify_swc),
        "tasks": _LayerBranch(_tasks_branch, _classify_tasks),
        "requirements": _LayerBranch(_requirements_branch, _classify_requirements),
        "interfaces": _LayerBranch(_interfaces_branch, _classify_interfaces),
    }
)


def _group_by_element(
    delta: Sequence[DeltaEntry],
    hardware: Iterable[Hardware],
) -> tuple[dict[str, dict[str, Any]], set[str], list[str]]:
    """The triggering ``hardware`` entries split into per-element field updates and removals.

    Per element: raising a memory's ``threshold`` and enlarging its ``size`` is one
    question about it, not two. A list-shaped field is folded back into its whole
    value (today's list plus the admitted members) using *hardware*, so the
    classifier compares two lists. An ``update`` is stored as its ``after``, ``None``
    included, so a cleared ``asil_capability`` arrives as a key present with ``None``.
    """
    updates: dict[str, dict[str, Any]] = {}
    admitted: dict[str, dict[str, list[Any]]] = {}
    removals: set[str] = set()
    order: list[str] = []
    for entry in delta:
        name = entry.address.element
        assert name is not None  # is_safety_relevant already refused an element-less address
        if name not in order:
            order.append(name)
        if entry.operation == "remove":
            removals.add(name)
            continue
        field = entry.address.path[0]
        assert isinstance(field, FieldStep)  # ditto
        if entry.operation == "create":
            # `is_safety_relevant` admits a `create` only at a member of a
            # list-shaped safety field, so this is one admitted member.
            admitted.setdefault(name, {}).setdefault(field.field, []).append(entry.after)
            continue
        updates.setdefault(name, {})[field.field] = entry.after
    for name, by_field in admitted.items():
        for field_name, members in by_field.items():
            updates.setdefault(name, {})[field_name] = _staged_list(hardware, name, field_name, members)
    return updates, removals, order


def _staged_list(hardware: Iterable[Hardware], name: str, field: str, admitted: Sequence[Any]) -> list[Any]:
    """*name*'s staged *field* list: what it holds today, plus *admitted*.

    Removals in the same change set are not subtracted: that could only hide an
    admission beside them.
    """
    current = next((getattr(element, field, None) for element in hardware if element.name == name), None)
    members = list(current) if isinstance(current, list) else []
    return [*members, *(member for member in admitted if member not in members)]


def _delete_args(name: str) -> dict[str, Any]:
    """The classifier's argument shape for deleting a hardware element.

    ``element_type`` is spelled here rather than looked up in ``DELETE_ELEMENT_LAYERS``,
    which would import the tool-name scheme this module supersedes.
    """
    return {"element_type": "hardware", "name": name}
