"""The unresolved-reference policy: the element is kept and the reference is recorded.

When a reference names nothing the import carries -- a port whose interface is not in the file, a value type whose
base type is not -- the dependent element is **kept**, the reference is written into the model as the export
spelled it, and the fact is reported. A full AUTOSAR path is never bound by accident to an unrelated element of the
same short name; ``check-model`` reports it and ``generate arxml`` refuses it. A reference is staged until the
mapper commits its element (:meth:`UnresolvedReferences.keep`, :meth:`UnresolvedReferences.commit`).

The three causes, and only these: ``CAUSE_ABSENT``, the target is declared by no element of this export;
``CAUSE_SELECTION``, it was excluded by a ``--packages`` selection; ``CAUSE_CONFLICT``, it was declared more than
once with different definitions. A reference whose target the export declares and SWCraft did not import is none
of these and keeps its chain skip. A ``SW-COMPONENT-PROTOTYPE``'s ``TYPE-TREF`` (an ``external`` component stub)
and a connector's or delegation's endpoint port (skipped) are not this policy's.

A ``CAUSE_ABSENT`` target named in a role of :data:`STUB_ROLE_LAYERS` becomes one stub per path, named
``<leaf>_ext<X>`` (:func:`registry.stub_names`), and every committed occurrence is re-pointed at it. Stubs are
decided after mapping and before ``--packages``, so a stub never takes a name an imported element or a conflicting
group claims. A target stays a path when its cause is ``CAUSE_SELECTION`` or ``CAUSE_CONFLICT``, the occurrence is
in another role, its occurrences disagree on ``DEST``, or ``validator.external_pair_problems`` refuses the pair.
``import component`` resolves a port's interface late, against the target project
(:meth:`UnresolvedReferences.for_component_import`). A data element kept with an unresolved type gets no ``init``.
"""

from __future__ import annotations

import dataclasses
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Iterator
from dataclasses import dataclass, field, replace

from swcraft_tools.arxml_import.merge import (
    CONFLICTING,
    LAYER_IMPLEMENTATION_TYPES,
    LAYER_INTERFACES,
    SELECTED,
    MergeDecisions,
    declared_paths,
    record_chained_to,
)
from swcraft_tools.arxml_import.registry import PathRegistry, stub_names
from swcraft_tools.arxml_reader import ImportModel, SkipCategory, SkipEffect, SkipEntry, SkipLog, local_tag
from swcraft_tools.identifier import check_identifier, short_name_key
from swcraft_tools.schema import STUB_MEMBER_DEST, ExternalDataType, ExternalInterface
from swcraft_tools.validator import EXTERNAL_DATA_TYPE_DESTS, EXTERNAL_INTERFACE_DESTS
from swcraft_tools.validator.data_types import external_pair_problems

#: The target is declared by no element of this export.
CAUSE_ABSENT = "absent"
#: The target was excluded by the ``--packages`` selection.
CAUSE_SELECTION = "outside-selection"
#: The target was declared more than once with different definitions, so
#: nothing of it was imported.
CAUSE_CONFLICT = "conflict"

#: The roles whose ``CAUSE_ABSENT`` target is imported as a stub, with the layer
#: the stub is written to. A value type's base, a record field's and an array
#: element's type, a mapping set's two sides and a component's set list are not
#: here: a stub is never one of those, so their occurrences keep the path. A
#: variable's implementation type is a data element's twin; an application type
#: declared nowhere has no stub kind and stays a skip.
STUB_ROLE_LAYERS: dict[str, str] = {
    "swc.port.interface": LAYER_INTERFACES,
    "compositions.composition_port.interface": LAYER_INTERFACES,
    "interfaces.data_element.implementation_type": LAYER_IMPLEMENTATION_TYPES,
    "interfaces.argument.implementation_type": LAYER_IMPLEMENTATION_TYPES,
    "implementation_types.reference_type.base": LAYER_IMPLEMENTATION_TYPES,
    "implementation_types.implementation_element.type": LAYER_IMPLEMENTATION_TYPES,
    "implementation_types.implementation_array.element_type": LAYER_IMPLEMENTATION_TYPES,
    "implementation_types.pointer.target": LAYER_IMPLEMENTATION_TYPES,
    "swc.variable.implementation_type": LAYER_IMPLEMENTATION_TYPES,
}

#: The one role ``import component``'s policy admits. A component export declares
#: its own closure, so the only reference the target project may answer is a
#: port's interface; a type, a mapping-set side and a component's set list keep
#: that path's own refusals.
COMPONENT_IMPORT_ROLES: frozenset[str] = frozenset({"swc.port.interface"})

#: Which member list an interface stub of each ``DEST`` fills -- the operations of
#: a client-server interface, the data elements of a sender-receiver one. Every
#: other ``DEST`` lists nothing: the mode-switch com-spec classes carry attributes
#: the standard makes mandatory and an imported file need not state. The schema's
#: own element-local rule, inverted, so the two cannot disagree.
STUB_LIST_BY_DEST: dict[str, str] = {dest: kind for kind, dest in STUB_MEMBER_DEST.items()}


def direct_member(target: str, member_ref: str) -> str | None:
    """The one segment *member_ref* names directly under *target*, or ``None`` when it is no such reference.

    The segment is returned as it stands; whether it is a legal identifier is the caller's.
    """
    parent, slash, name = member_ref.rpartition("/")
    return name if slash and parent == target else None


def referrer_count(records: Iterable[UnresolvedReference]) -> int:
    """Every reference *records* represents -- not the record count.

    One target may be referenced many times, so the two figures differ.
    """
    return sum(len(record.referrers) for record in records)


def _owner_kept(referrer_path: str, kept_paths: frozenset[str]) -> bool:
    """True when the element that made a reference is one a selection kept.

    The referrer's own path, or its parent's: a port is recorded under its own
    path and the element written is the component or composition one segment
    above it.
    """
    if referrer_path in kept_paths:
        return True
    parent, _, _ = referrer_path.rpartition("/")
    return bool(parent) and parent in kept_paths


def without_owners(records: Iterable[UnresolvedReference], dropped_paths: frozenset[str]) -> list[UnresolvedReference]:
    """*records* less every referrer made by an element whose declaration path is in *dropped_paths*.

    For an element dropped after the selection (the reserved device-name skip), so
    no record names a referrer in no written file. A referrer is the dropped
    element's when its own path or its parent's is (a port); a record left with no
    referrer is gone.
    """
    kept: list[UnresolvedReference] = []
    for record in records:
        referrers = [referrer for referrer in record.referrers if not _owner_kept(referrer.path, dropped_paths)]
        if len(referrers) == len(record.referrers):
            kept.append(record)
        elif referrers:
            kept.append(replace(record, referrers=referrers))
    return kept


def owned_by(declaration_paths: frozenset[str]) -> Callable[[str], bool]:
    """A predicate: whether a referrer path was made by one of *declaration_paths*.

    The ownership rule of :func:`without_owners`: the referrer's own path or its parent's (a port).
    """
    return lambda referrer_path: _owner_kept(referrer_path, declaration_paths)


def stubs_narrowed(stubs: Iterable[ExternalStub], keep: Callable[[str], bool]) -> list[ExternalStub]:
    """*stubs*, each with only the referrers whose path *keep* admits.

    Never drops a stub (which stubs are written is decided by the element lists);
    it keeps a referrer in no written file out of each stub's record.
    """
    return [replace(stub, referrers=tuple(r for r in stub.referrers if keep(r.path))) for stub in stubs]


def disagreements_narrowed(records: Iterable[DestDisagreement], keep: Callable[[str], bool]) -> list[DestDisagreement]:
    """*records* narrowed to the referrers *keep* admits; a record left with none is gone.

    What this does **not** do: re-decide. The verdict is taken over every committed
    occurrence of the whole export, before any selection, so a narrowed record keeps
    its verdict and its :attr:`DestDisagreement.dests`.
    """
    narrowed: list[DestDisagreement] = []
    for record in records:
        referrers = tuple(r for r in record.referrers if keep(r.path))
        if referrers:
            narrowed.append(replace(record, referrers=referrers))
    return narrowed


@dataclass(frozen=True)
class UnresolvedReferrer:
    """One element that made an unresolved reference, and where it made it.

    ``path`` and ``tag`` are the **declaration**'s -- the interface, the record, the
    implementation type, the port -- the element the user searches the export for.
    ``member`` names the data element, record field, array element or argument
    inside it, empty when the declaration itself makes the reference. ``attribute``
    is the reference's ARXML attribute and ``field`` the SWCraft field the path was
    written into; together they are the *role*. ``occurrence`` and ``dest``, filled
    by :meth:`UnresolvedReferences.keep`, are the AUTOSAR path of the node the
    reference sits on and the ``DEST`` the export wrote there; neither takes part
    in equality.
    """

    path: str
    tag: str
    attribute: str
    field: str
    member: str = ""
    # Through the module: mypy reads a bare `field` in this body as the attribute above, not the function.
    occurrence: str = dataclasses.field(default="", compare=False)
    dest: str = dataclasses.field(default="", compare=False)

    def __str__(self) -> str:
        where = f"{self.path}/{self.member}" if self.member else self.path
        return f"{where} ({self.tag} {self.attribute} -> {self.field})"


@dataclass
class UnresolvedReference:
    """Every reference to one target path that did not resolve, for one cause.

    One record per ``(target, cause)`` pair, which is the grouping the report
    renders: a user repairs a *target*, not a referrer, and the cause is what
    tells them how.
    """

    target: str
    dest: str
    cause: str
    referrers: list[UnresolvedReferrer] = field(default_factory=list)
    #: The conflicting group the target belongs to -- set only for
    #: :data:`CAUSE_CONFLICT`, so a line can name the conflict record.
    conflict_group: str | None = None


@dataclass(frozen=True)
class ExternalStub:
    """One stub the import minted: what it is called, what it stands for, who names it.

    ``layer`` is ``interfaces`` or ``data_types``; ``target`` and ``dest`` are the
    reference pair the stub carries verbatim; ``referrers`` are the committed
    occurrences re-pointed at ``name``, which the report lists instead of
    unresolved references.
    """

    layer: str
    name: str
    target: str
    dest: str
    referrers: tuple[UnresolvedReferrer, ...]


@dataclass(frozen=True)
class _MemberCandidate:
    """One member a construct names under a target that may become an interface stub.

    ``kind`` is the list it would join (``operations`` or ``data_elements``),
    ``member_ref`` the construct's own reference as the export spelled it and
    ``port_path`` the port the construct names it through: the candidate counts
    only once that port is committed (:meth:`UnresolvedReferences.commit`).
    """

    target: str
    kind: str
    member_ref: str
    port_path: str


@dataclass(frozen=True)
class _ChainedSkip:
    """One :meth:`UnresolvedReferences.chain_skip` entry, and how to re-word it once *target* is a stub."""

    entry: SkipEntry
    target: str
    layer: str
    once_stubbed: Callable[[str], str]


@dataclass(frozen=True)
class DestDisagreement:
    """A ``CAUSE_ABSENT`` path left without a stub because its occurrences disagree on ``DEST``.

    Every committed occurrence, in every role, with the ``DEST`` each one wrote
    (:attr:`UnresolvedReferrer.dest`). The occurrences keep the path and stay in the
    unresolved list; this record is the report's reason for not minting the stub.
    """

    target: str
    referrers: tuple[UnresolvedReferrer, ...]
    #: The distinct ``DEST`` values of the export's occurrences, in the order first
    #: met. Set where the record is minted, so a narrowing that drops referrers
    #: leaves it naming the disagreement that decided it.
    dests: tuple[str, ...]


@dataclass(frozen=True)
class MintedStubs:
    """What :meth:`UnresolvedReferences.mint_stubs` decided, for the finalization pass.

    ``interfaces`` and ``data_types`` are the elements to add to the two layers;
    ``stubs`` the per-stub record the report renders, in the order minted (the
    target paths' order); ``interface_names`` and ``type_names`` the
    ``path -> stub name`` maps the caller re-points each layer's occurrences
    with; ``disagreements`` the paths left without a stub for a ``DEST`` disagreement.
    """

    interfaces: tuple[ExternalInterface, ...] = ()
    data_types: tuple[ExternalDataType, ...] = ()
    stubs: tuple[ExternalStub, ...] = ()
    interface_names: dict[str, str] = field(default_factory=dict)
    type_names: dict[str, str] = field(default_factory=dict)
    disagreements: tuple[DestDisagreement, ...] = ()


class UnresolvedReferences:
    """The unresolved-reference policy: what the export declares, and every reference that missed.

    Constructed once per import from the read model and the merge verdict, and
    handed to the mapping functions as a parameter; ``None`` there takes no policy.
    ``import component`` passes :meth:`for_component_import`'s: *roles* filters which
    roles :meth:`keep` and :meth:`answer` admit (``None`` admits every role), and
    *resolves_late* tells the mappers that a kept port is decided after the target
    project is read, so they hold its com-specs whole rather than judging them
    against a stub that may never be minted.
    """

    def __init__(
        self,
        model: ImportModel,
        merges: MergeDecisions | None = None,
        *,
        roles: Collection[str] | None = None,
        resolves_late: bool = False,
    ) -> None:
        self._model = model
        self._merges = merges
        self._roles = frozenset(roles) if roles is not None else None
        #: Whether a kept port is decided after the target project is read.
        self.resolves_late = resolves_late
        #: The target paths the target project answered with a declared interface
        #: (:meth:`withdraw`) and with a stub of the same ``external_ref`` (:meth:`reuse`).
        self._withdrawn: set[str] = set()
        self._reused: dict[str, ExternalStub] = {}
        # The reader's path index plus the elements the reader refused, so a port
        # naming a `MODE-SWITCH-INTERFACE` is not reported as naming nothing. Shared
        # with the merge verdict's port comparison, so the two cannot disagree.
        self._declared = declared_paths(model)
        self._records: dict[tuple[str, str], UnresolvedReference] = {}
        #: The referring declarations whose element really reached the mapped
        #: model. :meth:`keep` STAGES a referrer and :meth:`commit` promotes it;
        #: nothing outside this class sees a staged one.
        self._committed: set[str] = set()
        #: Target path -> the stub :meth:`mint_stubs` minted for it. Its
        #: occurrences in that stub's layer's roles are no longer unresolved, and
        #: :attr:`references` leaves them out.
        self._stubbed: dict[str, ExternalStub] = {}
        #: The chained skip entries whose reason names a target that may become
        #: a stub. See :meth:`chain_skip`.
        self._chained: list[_ChainedSkip] = []
        #: The members constructs name under a target that may
        #: become an interface stub (:meth:`stage_member`), and, once
        #: :meth:`mint_stubs` has run, each minted interface stub's lists by kind.
        self._members: list[_MemberCandidate] = []
        self._listed: dict[str, dict[str, frozenset[str]]] = {}

    @classmethod
    def for_component_import(cls, model: ImportModel) -> UnresolvedReferences:
        """The policy ``import component`` maps its file with.

        Filtered to :data:`COMPONENT_IMPORT_ROLES` and resolved late: a port whose
        interface the file declares nowhere is kept with the path, everything through
        it is held, and the late pass (``component_exchange.dependencies``) answers
        the path against the target project -- its declared interface of the same
        last segment and of the reference's kind (:meth:`withdraw`), else a target
        stub of the same ``external_ref`` and ``DEST`` (:meth:`reuse`), else a stub
        of its own. No merge verdict: a component export declares one closure.
        """
        return cls(model, roles=COMPONENT_IMPORT_ROLES, resolves_late=True)

    def admits(self, role: str) -> bool:
        """Whether this policy keeps a reference in *role*; every role without a filter."""
        return self._roles is None or role in self._roles

    # -- the question the mappers ask ------------------------------------
    def cause(self, target: str) -> str | None:
        """Why *target* does not resolve, or ``None`` when no cause applies.

        ``None`` answers every path the export **declares** and SWCraft did not
        import for a reason of its own: that element has a skip entry, and the
        reference chained to it keeps its chain skip.
        """
        if not target:
            return None
        if target not in self._declared:
            return CAUSE_ABSENT
        if self._merges is not None:
            group = self._merges.group_of(target)
            if group is not None:
                if group.verdict == CONFLICTING:
                    return CAUSE_CONFLICT
                if group.verdict == SELECTED and target != group.primary:
                    # A selection kept another member of this group. The two
                    # differ, so this reference may not be answered with the kept
                    # one; the user decides whether to widen the selection.
                    return CAUSE_SELECTION
        return None

    def answer(self, target: str, *, role: str | None = None) -> str | None:
        """What :meth:`keep` would write for *target*, **without** staging anything.

        The target path, or ``None`` when the reference is not one of the three
        causes, for a caller that must know what it would write before it knows
        whether the referring member survives (``map_mapping_sets``,
        ``swc_mapping.data_type_mappings``); each calls :meth:`keep` only for what
        it writes. *role* is the role the caller will stage under: a role the filter
        does not admit (:meth:`admits`), or no role under a filtered policy, answers
        ``None``.
        """
        if self._roles is not None and (role is None or not self.admits(role)):
            return None
        return target if self.cause(target) is not None else None

    def keep(self, target: str, referrer: UnresolvedReferrer, *, occurrence: str | None = None) -> str | None:
        """**Stage** *target* against *referrer* and answer what to write.

        Returns the target path -- written into the model exactly as the export
        spelled it -- or ``None`` when the reference is not one of the three causes,
        in which case the caller keeps its own skip.

        Staged, not recorded: a later member of the same declaration can still
        abandon the whole of it (a data element typed by a base type refuses its
        interface), and a record would then name an element in no written file.
        Only referrers :meth:`commit` promoted are :attr:`references`'. A
        mapping-set row and a component's set list decide through :meth:`answer`
        and call this only once the member is certain to be written.

        *occurrence* is the AUTOSAR path of the node the reference sits on, when it
        is neither the referrer's own path nor ``path/member`` (an argument's, whose
        operation sits between). The ``DEST`` is read there and carried on the
        staged referrer, so two occurrences of one target each keep their own.
        """
        if not self.admits(referrer.field):
            # A role the filter does not admit keeps the caller's own skip.
            return None
        cause = self.cause(target)
        if cause is None:
            return None
        if occurrence is None:
            occurrence = f"{referrer.path}/{referrer.member}" if referrer.member else referrer.path
        referrer = replace(
            referrer,
            occurrence=occurrence,
            dest=self._occurrence_dest(occurrence, referrer, target),
        )
        record = self._records.get((target, cause))
        if record is None:
            record = UnresolvedReference(
                target=target,
                dest=referrer.dest,
                cause=cause,
                conflict_group=self._conflict_group(target) if cause == CAUSE_CONFLICT else None,
            )
            self._records[(target, cause)] = record
        record.referrers.append(referrer)
        return target

    def chain_skip(
        self,
        skip_log: SkipLog,
        path: str,
        tag: str,
        reason: str,
        *,
        target: str,
        layer: str,
        once_stubbed: Callable[[str], str],
        effect: SkipEffect,
    ) -> None:
        """Record a skip chained to *target*, and re-word it if *target* becomes a stub.

        A chained skip (an access entry or a port COM-SPEC through a kept port, an
        ``init`` against a kept type reference) is recorded while the export is
        mapped, before the stub decision. It is recorded now with *reason*, which
        describes the path, and remembered: if a stub of *layer* is minted for
        *target* and written, its reason becomes ``once_stubbed(stub name)``
        (:meth:`name_written_stubs`). A record naming a member the stub lists is
        kept instead (:meth:`listed_member`), so the mappers call this only for one
        whose member is not listed.

        The category is decided here by :meth:`cause`, never by the caller:
        :data:`CAUSE_ABSENT` is :attr:`SkipCategory.OUTSIDE_FILE`;
        :data:`CAUSE_CONFLICT` and :data:`CAUSE_SELECTION` are
        :attr:`SkipCategory.NOT_SUPPORTED` (SWCraft's one-name-per-layer rule, and
        the user's own ``--packages`` choice); ``None`` (a target the file declares
        and SWCraft skipped) is chained to it through :func:`merge.record_chained_to`,
        searching *skip_log* and then the reader's ``model.skip_log``, and takes its
        root's effect.

        What this does **not** reach: for ``None``, a target skipped by a mapper
        whose log is neither of those two (``map_data_types``' for a type) takes the
        fallback :attr:`SkipCategory.NOT_SUPPORTED`; no caller passes one, since
        each passes a kept target, which has a cause.

        *effect* has no default: what the entry does to ``import component`` when it
        has a cause of its own.
        """
        cause = self.cause(target)
        if cause == CAUSE_ABSENT:
            entry = skip_log.record(path, tag, reason, category=SkipCategory.OUTSIDE_FILE, effect=effect)
        elif cause is not None:
            entry = skip_log.record(path, tag, reason, category=SkipCategory.NOT_SUPPORTED, effect=effect)
        else:
            entry = record_chained_to(
                skip_log, self._declared, path, tag, reason, cause_path=target, upstream=(self._model.skip_log,)
            )
        self._chained.append(_ChainedSkip(entry, target, layer, once_stubbed))

    def stage_member(self, target: str, *, kind: str, member_ref: str, port_path: str) -> None:
        """Stage *member_ref*, a member of *target* a construct names through the port at *port_path*.

        The stub decision lists, on every interface stub it mints, each member a
        carried construct names under it: an operation-invoked event, a call point,
        a read or a write, a server, client, or non-queued sender or receiver
        com-spec, of a component or a composition -- never a construct the mapper
        drops for its port's direction. *kind* is the list the member would join.
        Only a :data:`CAUSE_ABSENT` target can become a stub, and a candidate counts
        once its port is committed.
        """
        self._members.append(_MemberCandidate(target=target, kind=kind, member_ref=member_ref, port_path=port_path))

    def listed_member(self, target: str, kind: str, member_ref: str) -> str | None:
        """The name *member_ref* is listed under on *target*'s interface stub, or ``None``.

        A name only when :meth:`mint_stubs` minted an interface stub for *target*,
        *member_ref* is exactly one segment under it, and the stub's *kind* list
        holds that segment; ``None`` before :meth:`mint_stubs` has run.
        """
        name = direct_member(target, member_ref)
        return name if name is not None and name in self.listed_members(target, kind) else None

    def listed_members(self, target: str, kind: str) -> frozenset[str]:
        """Every name the interface stub minted for *target* lists in *kind*; empty when none was minted."""
        return self._listed.get(target, {}).get(kind, frozenset())

    def _member_lists(self, target: str, dest: str) -> dict[str, list[str]]:
        """The member lists of the interface stub minted for *target* with *dest*.

        Every committed candidate of the one kind *dest* admits
        (:data:`STUB_LIST_BY_DEST`) that is exactly one segment under *target* and a
        legal AUTOSAR identifier, so ``check-model`` gets no finding the import made.
        Names that differ only in case (``[constr_2508]``) are listed by none of
        them: the source cannot hold them apart either. Sorted.
        What this does not reach: a member only an element a ``--packages`` selection
        later drops names stays listed, since the lists are decided before the selection.
        """
        kind = STUB_LIST_BY_DEST.get(dest)
        if kind is None:
            return {}
        names: set[str] = set()
        for candidate in self._members:
            if candidate.target != target or candidate.kind != kind or candidate.port_path not in self._committed:
                continue
            name = direct_member(target, candidate.member_ref)
            if name is not None and check_identifier(name) is None:
                names.add(name)
        keys = {name: short_name_key(name) for name in names}
        alike = Counter(keys.values())
        return {kind: sorted(name for name, key in keys.items() if alike[key] == 1)}

    def commit(self, referrer_path: str) -> None:
        """*referrer_path*'s staged references are facts about the mapped model.

        Called beside the append that puts the element into the mapper's result,
        once per declaration. Idempotent; a path that staged nothing is a no-op.

        What this does **not** reach: it is keyed by the referrer's own declaration
        path, so a port commits where it is decided, not where its owner is
        appended -- correct only because neither ``map_swcs`` nor
        ``map_compositions`` refuses an owner after mapping its ports. Nor can it
        express a member dropped while its declaration is kept: an argument stages
        under its interface's path, so ``map_interfaces`` decides an operation's
        drop before any of its arguments stages anything.
        """
        self._committed.add(referrer_path)

    def occurrence_dests(self, target: str) -> frozenset[str]:
        """The ``DEST`` of every committed occurrence of *target* in a port's interface role.

        ``import component``'s late pass compares a target project's declared
        interface's kind with it before binding a port to it by its leaf. Empty when
        *target* staged no such occurrence.
        """
        return frozenset(referrer.dest for referrer in self._committed_interface_referrers(target))

    def withdraw(self, target: str) -> UnresolvedReference | None:
        """*target* is answered by the target project's declared interface: no stub, no record.

        :attr:`references` leaves its record out, :meth:`mint_stubs` never ranks it
        and nothing staged under it is listed. Returns the record as staged, ``None``
        when *target* staged nothing (with several causes, the last one removed);
        the value is an observation hook only. Idempotent.
        """
        self._withdrawn.add(target)
        self._members = [candidate for candidate in self._members if candidate.target != target]
        withdrawn: UnresolvedReference | None = None
        for key in [key for key in self._records if key[0] == target]:
            withdrawn = self._records.pop(key)
        return withdrawn

    def reuse(self, target: str, interface: ExternalInterface) -> ExternalStub | None:
        """*target* is answered by the target project's stub *interface*, whose ``external_ref`` is *target*.

        A target ``ExternalInterface`` is never bound by its leaf, only by the same
        ``external_ref``, and then it is **reused**: the target takes no rank,
        nothing is minted, every committed occurrence is re-pointed at its name, and
        nothing staged under it is listed (its members are an earlier import's
        inference).

        The stub safeguards hold: the occurrences agree on one ``DEST``, the pair is
        one an interface stub admits (``validator.external_pair_problems``), and
        the ``DEST`` is the stub's ``external_dest``, so a port is never re-pointed
        at a stub of another kind. Returns the reused stub's record, or ``None`` when
        no committed occurrence is in an interface-stub role or a safeguard
        declines; the caller then takes the minting decision.
        """
        referrers = self._committed_interface_referrers(target)
        if not referrers:
            return None
        dest = interface.external_dest
        if {referrer.dest for referrer in referrers} != {dest} or external_pair_problems(
            target, dest, EXTERNAL_INTERFACE_DESTS, "stub"
        ):
            return None
        stub = ExternalStub(layer=LAYER_INTERFACES, name=interface.name, target=target, dest=dest, referrers=referrers)
        self._reused[target] = stub
        self._stubbed[target] = stub
        self._members = [candidate for candidate in self._members if candidate.target != target]
        return stub

    @property
    def withdrawn(self) -> frozenset[str]:
        """Every target path :meth:`withdraw` answered with the target project's declared interface.

        An observation hook; the late pass acts on the binding it made.
        """
        return frozenset(self._withdrawn)

    @property
    def reused(self) -> tuple[ExternalStub, ...]:
        """Every target stub :meth:`reuse` re-pointed occurrences at, in the order reused."""
        return tuple(self._reused.values())

    def _committed_interface_referrers(self, target: str) -> tuple[UnresolvedReferrer, ...]:
        """*target*'s committed ``CAUSE_ABSENT`` occurrences in a role an interface stub may take.

        The one filter :meth:`occurrence_dests` and :meth:`reuse` read, so the two
        never disagree. Empty when *target* staged no such occurrence.
        """
        record = self._records.get((target, CAUSE_ABSENT))
        if record is None:
            return ()
        return tuple(
            referrer
            for referrer in record.referrers
            if referrer.path in self._committed and STUB_ROLE_LAYERS.get(referrer.field) == LAYER_INTERFACES
        )

    # -- what the report is handed ---------------------------------------
    @property
    def references(self) -> tuple[UnresolvedReference, ...]:
        """Every **committed** record, in the order the first reference to each target was met.

        A record carries its committed referrers alone, and its ``dest`` is its
        first committed occurrence's; a record with none is absent. Once
        :meth:`mint_stubs` has run, an occurrence re-pointed at a stub is left out,
        so a target stubbed in every role has no record -- the report lists it as a
        stub.
        """
        committed: list[UnresolvedReference] = []
        for record in self._records.values():
            referrers = [
                referrer
                for referrer in record.referrers
                if referrer.path in self._committed and not self._is_stubbed(record, referrer)
            ]
            if not referrers:
                continue
            if len(referrers) == len(record.referrers) and record.dest == referrers[0].dest:
                committed.append(record)
            else:
                committed.append(replace(record, dest=referrers[0].dest, referrers=referrers))
        return tuple(committed)

    def mint_stubs(
        self,
        registry: PathRegistry,
        *,
        interface_names: Collection[str],
        type_names: Collection[str],
    ) -> MintedStubs:
        """Decide and mint every external stub, once, after mapping.

        The module's stub decision over the **committed** occurrences of every
        ``CAUSE_ABSENT`` target: a target named in one of :data:`STUB_ROLE_LAYERS`'
        roles, whose occurrences all carry one ``DEST`` that the role's layer admits
        beside a well-formed path outside the generated root packages, gets one
        stub, named by :func:`~swcraft_tools.arxml_import.registry.stub_names` over
        every decided stub before any drop.

        *interface_names* and *type_names* are every name the stub may not take in
        its layer -- every name the import writes or claims; a name the export
        declares and SWCraft did not import is not among them. Every stub gets a
        rename line, ``<leaf> -> <leaf>_ext<X>`` (``moved=False``). A type stub is
        registered in *registry*'s path map (:meth:`PathRegistry.register_external`),
        an interface stub in ``registry.interface_source_paths``.

        Called at most once per import; afterwards :attr:`references` leaves the
        stubbed occurrences out. A leaf Windows reserves for a device (``CON``)
        needs no case of its own: ``CON_ext0`` is no device name.
        """
        admitted = {LAYER_INTERFACES: EXTERNAL_INTERFACE_DESTS, LAYER_IMPLEMENTATION_TYPES: EXTERNAL_DATA_TYPE_DESTS}
        claimed = {LAYER_INTERFACES: set(interface_names), LAYER_IMPLEMENTATION_TYPES: set(type_names)}
        decided: list[tuple[UnresolvedReference, str, str]] = []
        disagreements: list[DestDisagreement] = []
        for record in sorted(self.references, key=lambda candidate: candidate.target):
            if record.cause != CAUSE_ABSENT or record.target in self._reused:
                # A reused target stub (`reuse`) takes no rank.
                continue
            layers = {STUB_ROLE_LAYERS[r.field] for r in record.referrers if r.field in STUB_ROLE_LAYERS}
            if not layers:
                continue
            if len({referrer.dest for referrer in record.referrers}) > 1:
                disagreements.append(
                    DestDisagreement(
                        target=record.target,
                        referrers=tuple(record.referrers),
                        dests=tuple(dict.fromkeys(referrer.dest for referrer in record.referrers)),
                    )
                )
                continue
            dest = record.referrers[0].dest
            layer = next((each for each in sorted(layers) if dest in admitted[each]), None)
            if layer is None or external_pair_problems(record.target, dest, admitted[layer], "stub"):
                continue
            decided.append((record, layer, dest))

        interfaces: list[ExternalInterface] = []
        data_types: list[ExternalDataType] = []
        stubs: list[ExternalStub] = []
        names: dict[str, dict[str, str]] = {LAYER_INTERFACES: {}, LAYER_IMPLEMENTATION_TYPES: {}}
        # Ranked over every decided stub of a layer, before any drop: a refused
        # record (a disagreement, an `external_pair_problems` finding) takes no rank.
        ranked = {
            layer: stub_names(
                {record.target: record.target.rsplit("/", 1)[-1] for record, each, _ in decided if each == layer},
                claimed[layer],
            )
            for layer in claimed
        }
        for record, layer, dest in decided:
            leaf = record.target.rsplit("/", 1)[-1]
            # `external_pair_problems` admitted the path, so its last segment is
            # a non-empty identifier, which also means the schema's name guard
            # cannot refuse the minted name -- a `ValidationError` from either
            # constructor would be a defect, and it propagates.
            minted = ranked[layer][record.target]
            name = minted.name
            if layer == LAYER_INTERFACES:
                lists = self._member_lists(record.target, dest)
                interfaces.append(
                    ExternalInterface(
                        name=name,
                        kind="external_interface",
                        external_ref=record.target,
                        external_dest=dest,
                        operations=lists.get("operations", []),
                        data_elements=lists.get("data_elements", []),
                    )
                )
                self._listed[record.target] = {kind: frozenset(members) for kind, members in lists.items()}
                registry.interface_source_paths[name] = record.target
            else:
                data_types.append(
                    ExternalDataType(name=name, kind="external_type", external_ref=record.target, external_dest=dest)
                )
                registry.register_external(record.target, name)
            claimed[layer].add(name)
            names[layer][record.target] = name
            registry.record_rename(
                leaf, name, f"{minted.reason} -- the stub of {record.target}", layer=layer, moved=False
            )
            stub = ExternalStub(
                layer=layer,
                name=name,
                target=record.target,
                dest=dest,
                referrers=tuple(r for r in record.referrers if STUB_ROLE_LAYERS.get(r.field) == layer),
            )
            stubs.append(stub)
            self._stubbed[record.target] = stub

        return MintedStubs(
            interfaces=tuple(interfaces),
            data_types=tuple(data_types),
            stubs=tuple(stubs),
            interface_names=names[LAYER_INTERFACES],
            type_names=names[LAYER_IMPLEMENTATION_TYPES],
            disagreements=tuple(disagreements),
        )

    def name_written_stubs(self, *, interface_names: Collection[str], type_names: Collection[str]) -> None:
        """Re-word every :meth:`chain_skip` entry whose target is a stub the import **writes**.

        Called once, after :meth:`mint_stubs` and every drop that can follow it,
        with the names of the interfaces and data types the project receives; an
        entry for a stub minted and then dropped keeps its path wording. Only the
        text changes, never the entry's ``category`` or ``rule``.

        What this does **not** reach: an entry is re-worded by the stub's minted
        name, so a stub a later pass renamed would keep the path wording; no pass
        renames one (``reconcile.reserved_*_names`` skip stubs).
        """
        written = {LAYER_INTERFACES: set(interface_names), LAYER_IMPLEMENTATION_TYPES: set(type_names)}
        for chained in self._chained:
            stub = self._stubbed.get(chained.target)
            if stub is not None and stub.layer == chained.layer and stub.name in written[chained.layer]:
                chained.entry.reason = chained.once_stubbed(stub.name)

    @property
    def total_referrers(self) -> int:
        """Every reference represented -- the sum over the records, not the record count.

        Test-only: the report calls :func:`referrer_count` on the records it renders.
        """
        return referrer_count(self.references)

    def narrowed(self, kept_paths: frozenset[str]) -> tuple[UnresolvedReference, ...]:
        """:attr:`references`, re-measured against the elements a selection kept.

        A ``--packages`` selection is a dependency closure taken after mapping, so a
        record whose referring elements the selection dropped is about a file this
        import did not write; it is filtered out here. *kept_paths* is the
        **declaration** paths of the written elements; a referrer is kept when its
        own path or its parent's (a port) is one of them.

        What this does **not** reach:

        * **It narrows, and never re-causes**: :data:`CAUSE_SELECTION` is minted by
          :meth:`cause`, where the reference is made.
        * **It is not called without a selection**; it composes with the
          :meth:`commit` staging, so a referrer the mapping layer abandoned is gone
          on both paths.
        * **A referrer whose parent is a kept element's child** -- no such role
          exists: every referrer is a declaration or a port.
        """
        narrowed: list[UnresolvedReference] = []
        for record in self.references:
            referrers = [referrer for referrer in record.referrers if _owner_kept(referrer.path, kept_paths)]
            if not referrers:
                continue
            narrowed.append(replace(record, referrers=referrers))
        return tuple(narrowed)

    # -- internals --------------------------------------------------------
    def _is_stubbed(self, record: UnresolvedReference, referrer: UnresolvedReferrer) -> bool:
        """Whether *referrer*'s occurrence was re-pointed at a stub by :meth:`mint_stubs`."""
        if record.cause != CAUSE_ABSENT:
            return False
        stub = self._stubbed.get(record.target)
        return stub is not None and STUB_ROLE_LAYERS.get(referrer.field) == stub.layer

    def _occurrence_dest(self, occurrence: str, referrer: UnresolvedReferrer, target: str) -> str:
        """The ``DEST`` the export wrote on this one reference occurrence, or ``""``.

        The first reference element below the node at *occurrence* whose local tag
        is the referrer's ``attribute`` and whose text is *target*, without entering
        a child with a ``SHORT-NAME`` of its own (another occurrence). A node the
        reader did not index, or one holding no such reference, falls back to
        :meth:`_dest_of` over the referring declaration.
        """
        raw = self._model.path_index.get(occurrence)
        if raw is not None:
            for node in _own_descendants(raw.node):
                if local_tag(node.tag) == referrer.attribute and (node.text or "").strip() == target:
                    return node.get("DEST") or ""
        return self._dest_of(referrer.path, target)

    def _conflict_group(self, target: str) -> str | None:
        if self._merges is None:
            return None
        group = self._merges.group_of(target)
        return str(group.key) if group is not None else None

    def _dest_of(self, referring_path: str, target: str) -> str:
        """The ``DEST`` the referring element wrote on the reference, or ``""``.

        Read off the live node, since the reader models most references as the path
        string alone. The first reference below the referring element whose text is
        *target* is the one read.
        """
        raw = self._model.path_index.get(referring_path)
        if raw is None:
            return ""
        for node in raw.node.iter():
            if (node.text or "").strip() == target:
                return node.get("DEST") or ""
        return ""


def _own_descendants(node: ET.Element) -> Iterator[ET.Element]:
    """Every element below *node*, in document order, without entering a named child.

    A child carrying a ``SHORT-NAME`` is an element of its own -- another data
    element, another argument, another port -- and a reference inside it is its
    occurrence, not *node*'s.
    """
    for child in node:
        if any(local_tag(grandchild.tag) == "SHORT-NAME" for grandchild in child):
            continue
        yield child
        yield from _own_descendants(child)
