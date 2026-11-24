"""The one entry point of the editing protocol: request in, response out.

``handle_request`` is the seam every consumer uses. Pass A runs first for every
shape, so a malformed request is refused before a project is opened; an unknown
operation is a ``schema-violation``.

The change-set lifecycle: gate 1 over the stated operations; unconditional
staging with the fail-fast staleness check; pass B (bind and apply in memory)
and pass C (classify what B could not apply); materialization into the shadow;
pass D over the result; the delta projection; gate 2 over the delta; the review
boundary; commit or abandon. Violations are reported in pass order and, within
a pass, in operation order: the first is the primary, the rest ``additional``.

A client can neither commit nor approve: the review boundary is an injected
:data:`ReviewBoundary`, and this engine never prompts. Terminal states: ``staged``
(no boundary; the request's baseline, no ``audit_ref``), ``abandoned`` (the
boundary declined the delta or the safety change; the request's baseline) and
``committed`` (the post-commit baseline; ``audit_ref`` is withheld when the
audit trail is incomplete).

:class:`~swcraft_tools.session.UnexplainedChangeError` and
:class:`~swcraft_tools.session.AuditReplayError` are faults with no protocol
code: the first fires before any write and is answered ``abandoned``, the
second propagates. ``discover`` and ``read`` take no scope and write no audit record.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from swcraft_tools.authorization import SafetyVerdict
from swcraft_tools.differ import ModelDiff, diff_snapshots
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.address import (
    Address,
    AddressError,
    ScopeEntry,
    reduce_address,
)
from swcraft_tools.editing.audit import (
    ChangeSetAuditor,
    auditor_for,
    claims_change_set,
    element_address_of_target,
    new_change_set_id,
)
from swcraft_tools.editing.authorization import (
    ApprovalDecision,
    gate_delta,
    gate_requested_operations,
    parse_approved_scope,
)
from swcraft_tools.editing.delta import project_delta
from swcraft_tools.editing.dependents import DependentsNotApplicable, dependent_removals
from swcraft_tools.editing.diagnosis import CONSISTENCY_VIOLATION, emitted_identifier_violations, subject_address
from swcraft_tools.editing.discovery import LayerNotDeclaredError, discover
from swcraft_tools.editing.link_confirmation import LinkConfirmationNotApplicable, link_confirmations
from swcraft_tools.editing.passes import (
    BROKEN_REFERENCE,
    SCHEMA_VIOLATION,
    ambiguous_baseline_violations,
    ambiguous_member_violations,
    derived_not_applicable_violation,
    filter_not_applicable_violation,
    join_path_outside_row_violation,
    malformed_scope_violation,
    members_not_applicable_violation,
    miss_violation,
    over_budget_violation,
    page_cursor_violation,
    page_not_applicable_violation,
    pass_a,
    pass_b,
    pass_c,
    pass_d_findings,
    placement_violation,
    reserved_name_violation,
    retired_storage_violation,
    summary_not_derivable_violation,
    target_occupied_violation,
    target_vanished_violation,
    undeclared_layer_violation,
    unknown_derived_value_violation,
    unknown_filter_field_violation,
    unknown_member_collection_violation,
    unopenable_project_violation,
    unparseable_element_violation,
    unreadable_model_root_violation,
)
from swcraft_tools.editing.protocol import (
    ChangeOperation,
    ChangeSetRequest,
    ChangeSetResponse,
    CountRequest,
    CountResponse,
    CreateOperation,
    DeltaEntry,
    DeltaOrigin,
    DiscoverForm,
    DiscoverRequest,
    ErrorResult,
    FindForRequirementRequest,
    FindRequest,
    FindResponse,
    FindTextRequest,
    FindTextResponse,
    Measured,
    ModelMapRequest,
    ProtocolError,
    ReadManyRequest,
    ReadRequest,
    ReadResponse,
    RemoveOperation,
    UpdateOperation,
    Violation,
)
from swcraft_tools.editing.read import (
    RESPONSE_BUDGET_WIRE_BYTES,
    DerivedNotApplicable,
    FilterNotApplicable,
    JoinPathOutsideRow,
    MembersNotApplicable,
    OverBudget,
    PageCursorNotFound,
    PageNotApplicable,
    PlacementNotPublishable,
    ReadMissError,
    SummaryNotDerivable,
    UnknownDerivedValue,
    UnknownFilterField,
    UnknownMemberCollection,
    measure,
    model_map,
    read,
    read_many,
)
from swcraft_tools.editing.resolve import address_candidates, append_sentence, find_text, resolve_names
from swcraft_tools.editing.safety_guard import safety_verdict
from swcraft_tools.editing.staging import (
    STALE_BASELINE,
    PlacementRefusal,
    RewrittenFile,
    StagedChangeSet,
    StaleBaselineError,
    TargetOccupied,
    TargetVanished,
    baseline_token,
    stage,
)
from swcraft_tools.editing.trace_candidates import (
    CandidatesRefused,
    RequirementIsStub,
    RequirementNotFound,
    candidates_for,
)
from swcraft_tools.model_state import RetiredStorageError, refuse_retired_storage
from swcraft_tools.project import SwcraftProject, require_project
from swcraft_tools.reader import ElementParseError
from swcraft_tools.session import CommitLogError, UnexplainedChangeError, commit_lock
from swcraft_tools.snapshot import ModelSnapshot, load_snapshot
from swcraft_tools.validator import APPLICATION_TYPE_UNMAPPED, ConsistencyIssue, unmapped_type_repairs
from swcraft_tools.writer.mutations import ReservedNameError
from swcraft_tools.yaml_load import clear_parse_cache

__all__ = ["ChangeSetProposal", "NoteSink", "ReviewBoundary", "handle_request"]

_MANIFEST_FILENAME = "swcraft.yaml"

#: The reason recorded when the review boundary declines a change set's delta.
_DECLINED_REASON = "the review boundary declined the change set"


@dataclass(frozen=True)
class ChangeSetProposal:
    """What a host is asked to review: one change set's reviewed delta.

    It carries no model root, shadow path or session handle, so the staged
    transaction stays unnameable. A host answers with
    ``ApprovalDecision(proposal.change_set_id, proposal.baseline, approved=...)``,
    which binds the answer to this change set at this baseline.

    :attr:`safety` is a second, independent question, answered with
    ``safety_approved``; empty ``changes`` means there is none. :attr:`rewritten`
    lists the files this change set writes whole, which no delta entry can show.
    """

    change_set_id: str
    baseline: str
    intent: str | None
    delta: tuple[DeltaEntry, ...]
    safety: SafetyVerdict = SafetyVerdict("change_set")
    rewritten: tuple[RewrittenFile, ...] = ()


#: The host's review boundary. ``None`` means no boundary was offered, and the
#: lifecycle stops at ``staged``.
ReviewBoundary = Callable[[ChangeSetProposal], ApprovalDecision]

#: A host-level note sink: prose for a person that the closed
#: ``changeSetResponse`` cannot carry — an incomplete audit trail, a tripwire
#: abandonment, a declined safety change, or (from the host) a review the client
#: could not be asked.
NoteSink = Callable[[str], None]


def handle_request(
    request: Any,
    *,
    project: str | Path | SwcraftProject,
    approved_scope: list[dict[str, Any]] | None = None,
    scope_id: str | None = None,
    review: ReviewBoundary | None = None,
    notes: NoteSink | None = None,
    registry: reg.Registry | None = None,
    discover_default: DiscoverForm | None = None,
) -> dict[str, Any]:
    """The protocol response to *request* against *project*.

    Args:
        project: the project root, the manifest path, or a loaded project.
        approved_scope: ``None`` for an unscoped run, else a list of
            ``{operation, address}`` entries; an empty list approves nothing.
            Unused by ``discover`` and ``read``.
        scope_id: the host's correlation id for the turn scope, written into
            every lifecycle record. No request shape carries it.
        review: the host's review boundary; ``None`` stops at ``staged``.
        notes: where a host-level note goes.
        registry: the model projection to serve the request with; ``None``
            resolves the current one once.
        discover_default: the form a project-wide ``discover`` naming none is
            served in on this channel.

    Returns:
        The response as plain JSON data, or the ``errorResult`` of a refused request.

    Raises:
        UndiagnosedInconsistency, AuditReplayError, CommitPreflightError, OSError:
            the faults :func:`_change_set` documents; none has a protocol code.

    A malformed ``approved_scope``, an unopenable project or an unreadable model
    root is answered with a code, never a raw exception (which would name a path).
    """
    # One request's documents: the parse cache is keyed by the text, so a held-over
    # entry is never a wrong value, and the clear keeps the working set this request's.
    clear_parse_cache()
    # Allocated on receipt: pass A, the staleness check and gate 1 all reject
    # before staging, and their records need the id.
    change_set_id = new_change_set_id()
    # Resolved once and threaded, so one request is served by one registry.
    registry = reg.or_current(registry)
    # Bind its kernel registry too: the snapshot loader, the differ, the
    # validator, the writer and the commit tripwire take no registry and resolve
    # it themselves.
    with reg.kernel_context(registry):
        loaded: SwcraftProject | None = None
        try:
            # Pass A refuses before `_open`, so a malformed request never reaches the file system.
            parsed = pass_a(request)
            loaded, manifest = _open(project)
            # One preflight for every request shape, so no branch catches `OSError`
            # broadly: the commit's own `OSError` must never be swallowed.
            _require_model_root(loaded)
            _refuse_retired_layout(loaded, registry)
            if isinstance(parsed, DiscoverRequest):
                return _answer_discover(loaded, parsed, registry, discover_default)
            if isinstance(parsed, ReadRequest):
                return _answer_read(loaded, parsed, registry)
            if isinstance(parsed, ReadManyRequest):
                return _answer_reads(loaded, parsed, registry)
            if isinstance(parsed, CountRequest):
                return _answer_count(loaded, parsed, registry)
            if isinstance(parsed, ModelMapRequest):
                return _answer_map(loaded, registry)
            if isinstance(parsed, (FindRequest, FindTextRequest, FindForRequirementRequest)):
                return _answer_find(loaded, parsed, registry)
            scope = _approved_scope(approved_scope)
            auditor = auditor_for(loaded, change_set_id, scope_id)
            dependents = _dependents(loaded, parsed, scope, registry)
            try:
                return _change_set(
                    loaded,
                    manifest,
                    parsed,
                    scope,
                    auditor,
                    review=review,
                    notes=notes,
                    registry=registry,
                    dependents=dependents,
                )
            except _CompletionFound as found:
                # `complete: true` and pass D's findings are repairable: rerun with
                # the server's repairs after the client's operations, through every
                # gate and the review. A refusal of the completed set answers with
                # the first run's refusal (the client is owed its own finding),
                # except `stale-baseline`, which no completion caused.
                try:
                    return _change_set(
                        loaded,
                        manifest,
                        parsed,
                        scope,
                        auditor,
                        review=review,
                        notes=notes,
                        registry=registry,
                        dependents=dependents,
                        completion=found.operations,
                    )
                except ProtocolError as second:
                    if second.result.error == STALE_BASELINE:
                        raise
                    raise found.refusal from second
        except ProtocolError as refusal:
            _record_rejection(request, refusal, change_set_id, scope_id, project, loaded)
            return refusal.result.to_json()


def _answer_discover(
    project: SwcraftProject,
    request: DiscoverRequest,
    registry: reg.Registry,
    discover_default: DiscoverForm | None,
) -> dict[str, Any]:
    """The answer to a ``discover`` request: the layer it names, or the whole project in the form asked.

    Raises:
        ProtocolError: the layer is undeclared, or the answer is over the response budget.
    """
    try:
        return discover(
            project,
            layer=request.layer,
            form=request.form,
            registry=registry,
            discover_default=discover_default,
        )
    except LayerNotDeclaredError as undeclared:
        # Pass A has already held `request.layer` to `$defs/layerName`.
        assert request.layer is not None  # discover only narrows when a layer was named
        raise ProtocolError(ErrorResult(primary=undeclared_layer_violation(request.layer))) from undeclared
    except OverBudget as over_budget:
        # The same composition as a read's over-budget refusal, with no address.
        raise _over_budget(over_budget) from over_budget


def _answer_read(
    project: SwcraftProject,
    request: ReadRequest,
    registry: reg.Registry,
    *,
    unpageable_subject: str = PageNotApplicable.LIMIT_SUBJECT,
    bounded: bool = True,
) -> dict[str, Any]:
    """One ``read``, or the :class:`ProtocolError` its miss or refusal composes.

    *unpageable_subject* words the refusal of a page at an address naming no
    set; *bounded* ``False`` skips the budget measurement (:func:`_answer_count`'s
    page of one). A miss's nearest names come from the snapshot the read loaded.
    """
    # A read needs no scope and writes no audit record; a miss is still a
    # rejection and is answered with its protocol code.
    try:
        return read(
            project,
            request.address,
            form=request.form,
            where=request.where,
            limit=request.limit,
            after=request.after,
            members=request.members,
            derived=request.derived,
            join=request.join,
            registry=registry,
            bounded=bounded,
        )
    except ReadMissError as missed:
        violation = miss_violation(missed.miss)
        if missed.snapshot is not None:
            (violation,) = _with_nearest((violation,), missed.snapshot, registry)
        raise ProtocolError(ErrorResult(primary=violation)) from missed
    except PageCursorNotFound as cursor:
        raise _page_cursor_not_found(cursor) from cursor
    except UnknownFilterField as undeclared_field:
        raise _unknown_filter_field(undeclared_field) from undeclared_field
    except FilterNotApplicable as inapplicable:
        raise _filter_not_applicable(inapplicable) from inapplicable
    except PageNotApplicable as unpageable:
        raise _page_not_applicable(unpageable, unpageable_subject) from unpageable
    except UnknownMemberCollection as undeclared_collection:
        raise _unknown_member_collection(undeclared_collection) from undeclared_collection
    except MembersNotApplicable as unsliceable:
        raise _members_not_applicable(unsliceable) from unsliceable
    except UnknownDerivedValue as undeclared_value:
        raise _unknown_derived_value(undeclared_value) from undeclared_value
    except DerivedNotApplicable as inapplicable_value:
        raise _derived_not_applicable(inapplicable_value) from inapplicable_value
    except JoinPathOutsideRow as outside_row:
        raise _join_path_outside_row(outside_row) from outside_row
    except ElementParseError as unparseable:
        raise _unparseable_model(unparseable, registry) from unparseable
    except PlacementNotPublishable as unpublishable:
        raise _unpublishable_placement(unpublishable) from unpublishable
    except SummaryNotDerivable as underivable:
        raise _summary_not_derivable(request.address, underivable) from underivable
    except OverBudget as over_budget:
        raise _over_budget(over_budget) from over_budget


def _answer_reads(project: SwcraftProject, request: ReadManyRequest, registry: reg.Registry) -> dict[str, Any]:
    """A batched ``read`` (:func:`~swcraft_tools.editing.read.read_many`), each address served by :func:`_answer_read`.

    A refusal of one address is that entry's ``errorResult``; an over-budget
    batch is the usual refusal, composed here as every read's is.
    """

    def answer(single: ReadRequest) -> ReadResponse | ErrorResult:
        try:
            return ReadResponse.from_json(_answer_read(project, single, registry))
        except ProtocolError as refusal:
            return refusal.result

    try:
        return read_many(request.addresses, request.form, answer)
    except OverBudget as over_budget:
        raise _over_budget(over_budget) from over_budget


def _answer_map(project: SwcraftProject, registry: reg.Registry) -> dict[str, Any]:
    """A ``read`` with no address: the bounded model map (:func:`~swcraft_tools.editing.read.model_map`)."""
    try:
        return model_map(project, baseline_token(Path(project.resolved_model_root)), registry)
    except ElementParseError as unparseable:
        raise _unparseable_model(unparseable, registry) from unparseable
    except OverBudget as over_budget:
        raise _over_budget(over_budget) from over_budget


# Named for its first expansion; it returns the link confirmations as well.
def _dependents(
    project: SwcraftProject,
    request: ChangeSetRequest,
    scope: tuple[ScopeEntry, ...] | None,
    registry: reg.Registry,
) -> tuple[ChangeOperation, ...]:
    """The operations the server adds for each ``with_dependents`` remove and ``confirm_links`` update of *request*.

    Dependents first, links second, over one read of the baseline snapshot. Either
    flag where it does not apply is refused ``schema-violation`` before staging.
    Gate 1 over the client's own operations runs first, so an uncovered request is
    refused unread; the server's rows are judged by :func:`_change_set`'s gate 1.
    """
    if not any(
        (isinstance(operation, RemoveOperation) and operation.with_dependents)
        or (isinstance(operation, UpdateOperation) and operation.confirm_links)
        for operation in request.operations
    ):
        return ()
    unauthorized = gate_requested_operations(_reduced(request.operations, registry), _reduced_scope(scope, registry))
    if unauthorized is not None:
        raise ProtocolError(ErrorResult(primary=unauthorized))
    try:
        snapshot = load_snapshot(str(project.resolved_model_root))  # under the request's bound registry
        removals = dependent_removals(snapshot, request.operations, registry=registry)
        return (*removals, *link_confirmations(snapshot, request.operations, registry=registry))
    except ElementParseError as unparseable:
        # The same answer every other reader of the model gives a file it cannot parse,
        # composed from `{layer, element}`, never the reader's message with its storage name.
        raise _unparseable_model(unparseable, registry) from unparseable
    except (DependentsNotApplicable, LinkConfirmationNotApplicable) as inapplicable:
        raise ProtocolError(
            ErrorResult(
                primary=Violation(error=SCHEMA_VIOLATION, message=str(inapplicable), addresses=(inapplicable.address,))
            )
        ) from inapplicable


def _with_nearest(
    violations: tuple[Violation, ...],
    snapshot: Any,
    registry: reg.Registry,
    operations: tuple[ChangeOperation, ...] = (),
) -> tuple[Violation, ...]:
    """*violations* with each address ``broken-reference`` naming the nearest names the model holds.

    *snapshot* is the baseline, so a change set passes its *operations*: the names
    they create are candidates too, never named missing.
    """
    completed = []
    for violation in violations:
        if violation.error == BROKEN_REFERENCE and violation.addresses:
            sentence = address_candidates(snapshot, violation.addresses[0], registry, operations=operations)
            if sentence:
                violation = replace(violation, message=append_sentence(violation.message, sentence))
        completed.append(violation)
    return tuple(completed)


def _answer_find(
    project: SwcraftProject,
    request: FindRequest | FindTextRequest | FindForRequirementRequest,
    registry: reg.Registry,
) -> dict[str, Any]:
    """``find``: each name's candidates across every layer and the member collections, and the baseline token.

    The ``text`` form answers the requirements whose text holds every word of the
    phrase (:func:`~swcraft_tools.editing.resolve.find_text`); the
    ``for_requirement`` form, the trace search's ranking (:func:`_answer_candidates`).
    Every answer echoes the client's input, so it is measured and refused
    ``over-budget`` above the response budget (:func:`_within_budget`).

    The token is taken before the snapshot is loaded, so an edit landing between
    the two makes a change set stated against it ``stale-baseline`` rather than
    accepted against a state the search never saw. There is no lock.
    """
    baseline = baseline_token(Path(project.resolved_model_root))
    try:
        snapshot = load_snapshot(str(project.resolved_model_root))
    except ElementParseError as unparseable:
        raise _unparseable_model(unparseable, registry) from unparseable
    if isinstance(request, FindForRequirementRequest):
        return _answer_candidates(project, snapshot, baseline, request, registry)
    if isinstance(request, FindTextRequest):
        candidates, more = find_text(snapshot, request.text, registry)
        found = FindTextResponse(
            baseline=baseline,
            text=request.text,
            candidates=candidates if isinstance(candidates, str) else tuple(candidates),
            more=more,
        )
        return _within_budget(found.to_json(), None, 0 if isinstance(candidates, str) else len(candidates))
    response = FindResponse(baseline=baseline, results=tuple(resolve_names(snapshot, request.names, registry)))
    return _within_budget(response.to_json(), None, len(request.names))


def _answer_candidates(
    project: SwcraftProject,
    snapshot: ModelSnapshot,
    baseline: str,
    request: FindForRequirementRequest,
    registry: reg.Registry,
) -> dict[str, Any]:
    """``find`` with ``for_requirement``: the trace search's places for one requirement, or the refusal it is owed.

    An unknown id is ``broken-reference`` naming the nearest ids; a stub is
    ``schema-violation``; an ambiguous identity or an unreadable decisions file is
    ``consistency-violation`` with no address, since what needs repair is the
    project. The answer is measured like every ``find``; places and listed links
    are bounded (``candidates``, ``FIND_EXISTING_LINKS_MAX``), so a heavily linked
    requirement still gets a bounded reply.
    """
    try:
        found = candidates_for(project, snapshot, baseline, request.for_requirement, request.candidates)
    except RequirementNotFound as missing:
        violation = Violation(error=BROKEN_REFERENCE, message=str(missing), addresses=(missing.address,))
        (violation,) = _with_nearest((violation,), snapshot, registry)
        raise ProtocolError(ErrorResult(primary=violation)) from missing
    except RequirementIsStub as stub:
        violation = Violation(error=SCHEMA_VIOLATION, message=str(stub), addresses=(stub.address,))
        raise ProtocolError(ErrorResult(primary=violation)) from stub
    except CandidatesRefused as refused:
        violation = Violation(error=CONSISTENCY_VIOLATION, message=str(refused))
        raise ProtocolError(ErrorResult(primary=violation)) from refused
    except ElementParseError as unparseable:
        # The ambiguity check's second read of the model root: a file that stopped parsing
        # after the snapshot was loaded is refused as the snapshot's own load refuses it.
        raise _unparseable_model(unparseable, registry) from unparseable
    places = 0 if isinstance(found.places, str) else len(found.places)
    return _within_budget(found.to_json(), None, places + len(found.existing_links))


def _answer_count(project: SwcraftProject, request: CountRequest, registry: reg.Registry) -> dict[str, Any]:
    """``read`` with ``form: "count"``: the size of the set the same read selects.

    Answered as the read's first ``index`` page of one, whose ``page.total`` is the
    set's size. That page is not measured against the response budget, so a row
    too wide to publish never refuses a count; a ``join`` is still held to its row
    ceiling. The count's own response, which echoes the request, is measured
    (:func:`_within_budget`).
    """
    # A count at an address naming no set is refused for what it counts, never
    # for a `limit` the request does not carry.
    answer = ReadResponse.from_json(
        _answer_read(
            project,
            replace(request.read, limit=1),
            registry,
            unpageable_subject="a count counts a set",
            bounded=False,
        )
    )
    assert answer.page is not None  # a read with a `limit` always answers its page
    response = CountResponse(
        address=answer.address,
        count=answer.page.total,
        where=answer.where,
        members=answer.members,
        derived=answer.derived,
        join=answer.join,
    )
    return _within_budget(response.to_json(), answer.address, 1)


def _within_budget(response: dict[str, Any], address: Address | None, elements: int) -> dict[str, Any]:
    """*response* as it stands, or the ``over-budget`` refusal it is owed.

    The final response is measured whole, echo included; the refusal carries no
    hints. *address* is the count's (``None`` for ``find``).
    """
    size = measure(response)
    if size <= RESPONSE_BUDGET_WIRE_BYTES:
        return response
    measured = Measured(wire_bytes=size, budget=RESPONSE_BUDGET_WIRE_BYTES, elements=elements)
    over_budget = OverBudget(address=address, measured=measured, hints=())
    raise _over_budget(over_budget) from over_budget


def _reduced(operations: tuple[ChangeOperation, ...], registry: reg.Registry) -> tuple[ChangeOperation, ...]:
    """*operations* with every address parsed against *registry* (``address.reduce_address``)."""
    return tuple(
        replace(operation, address=reduce_address(operation.address, registry=registry)) for operation in operations
    )


def _reduced_scope(scope: tuple[ScopeEntry, ...] | None, registry: reg.Registry) -> tuple[ScopeEntry, ...] | None:
    """*scope* with every entry's address parsed against *registry*; ``None`` (unscoped) stays ``None``."""
    if scope is None:
        return None
    return tuple(
        ScopeEntry(operation=entry.operation, address=reduce_address(entry.address, registry=registry))
        for entry in scope
    )


def _approved_scope(approved_scope: list[dict[str, Any]] | None) -> tuple[ScopeEntry, ...] | None:
    """The seam's scope argument, parsed, or refused as a :class:`ProtocolError`.

    A malformed scope carries pass A's code
    (:func:`~swcraft_tools.editing.passes.malformed_scope_violation`) and is raised
    inside the seam's ``try``, so its rejection record is written like every other.
    """
    try:
        return parse_approved_scope(approved_scope)
    except AddressError as malformed:
        raise ProtocolError(ErrorResult(primary=malformed_scope_violation(str(malformed)))) from malformed


def _require_model_root(project: SwcraftProject) -> None:
    """The refusal of a project whose ``model_root`` this process cannot list.

    Asked once before any request shape runs, so every shape answers the same code
    instead of a path-naming exception. ``os.scandir`` rather than ``is_dir()``: a
    directory that cannot be listed is the same condition. A permission change
    after this check propagates as the fault it is.
    """
    try:
        with os.scandir(Path(project.resolved_model_root)) as entries:
            next(iter(entries), None)
    except OSError as unreadable:
        raise ProtocolError(ErrorResult(primary=unreadable_model_root_violation())) from unreadable


def _refuse_retired_layout(project: SwcraftProject, registry: reg.Registry) -> None:
    """The refusal of a model that keeps an element file in a directory the contract retired.

    Asked for every request shape, ``discover`` included (it parses no element, so
    it would never meet the layer reader's refusal), after :func:`_require_model_root`.
    """
    try:
        refuse_retired_storage(project.resolved_model_root, registry=registry.kernel_model)
    except RetiredStorageError as retired:
        raise _retired_refusal(retired) from retired


def _retired_refusal(retired: RetiredStorageError) -> ProtocolError:
    """A retired layout as its code: no address, the kernel's own path-free sentence."""
    return ProtocolError(ErrorResult(primary=retired_storage_violation(str(retired))))


def _unpublishable_placement(unpublishable: PlacementNotPublishable) -> ProtocolError:
    """A placement value on disk that a read may not answer with, as its code.

    The same ``schema-violation`` at the same address the write side gives the
    identical value, so read and write agree on what a legal ``group`` is.
    """
    return ProtocolError(
        ErrorResult(
            primary=placement_violation(
                unpublishable.layer, unpublishable.element, unpublishable.field, str(unpublishable)
            )
        )
    )


def _summary_not_derivable(address: Address, underivable: SummaryNotDerivable) -> ProtocolError:
    """A contract that cannot state a kind's summary identity, as its code.

    ``summary`` is a layer read's default form, so an untranslated ``ValueError``
    here would reach the host as a transport error.
    """
    return ProtocolError(ErrorResult(primary=summary_not_derivable_violation(address, str(underivable))))


def _unknown_filter_field(undeclared: UnknownFilterField) -> ProtocolError:
    """A ``where`` clause on a field nothing at the address declares, as its code.

    Translated so a misspelled field name never leaves the seam as a transport error.
    """
    return ProtocolError(ErrorResult(primary=unknown_filter_field_violation(undeclared.address, str(undeclared))))


def _filter_not_applicable(inapplicable: FilterNotApplicable) -> ProtocolError:
    """A ``where`` the address cannot be filtered by, as its code."""
    return ProtocolError(ErrorResult(primary=filter_not_applicable_violation(inapplicable.address, str(inapplicable))))


def _page_not_applicable(unpageable: PageNotApplicable, subject: str) -> ProtocolError:
    """A ``limit`` at an address with no set to window, as its code, worded for *subject*.

    Kept apart from the filter refusal so a client that paged and did not filter is
    never told its filter did not apply.
    """
    return ProtocolError(
        ErrorResult(primary=page_not_applicable_violation(unpageable.address, unpageable.refusal(subject)))
    )


def _unknown_member_collection(undeclared: UnknownMemberCollection) -> ProtocolError:
    """A ``members`` naming a collection nothing at the layer declares, as its code."""
    return ProtocolError(ErrorResult(primary=unknown_member_collection_violation(undeclared.address, str(undeclared))))


def _members_not_applicable(unsliceable: MembersNotApplicable) -> ProtocolError:
    """A ``members`` this address or this field cannot answer, as its code.

    ``schema-violation``, kept apart from the filter and page refusals so each
    message names the parameter that did not apply.
    """
    return ProtocolError(ErrorResult(primary=members_not_applicable_violation(unsliceable.address, str(unsliceable))))


def _page_cursor_not_found(cursor: PageCursorNotFound) -> ProtocolError:
    """An ``after`` the selected set does not carry, as ``broken-reference``.

    Met in normal operation when a concurrent edit removed the cursor element; the
    client re-reads the first page rather than getting a silently different window.
    """
    return ProtocolError(ErrorResult(primary=page_cursor_violation(cursor.address, str(cursor))))


def _unknown_derived_value(undeclared: UnknownDerivedValue) -> ProtocolError:
    """A ``derived`` name outside the computed-value vocabulary, as ``unknown-field``."""
    return ProtocolError(ErrorResult(primary=unknown_derived_value_violation(undeclared.address, str(undeclared))))


def _derived_not_applicable(inapplicable: DerivedNotApplicable) -> ProtocolError:
    """A ``derived`` name at a set its entry's ``at`` does not name, as its code.

    ``schema-violation``, kept apart from :func:`_unknown_derived_value` so a
    published value named at the wrong set is not reported as unknown.
    """
    return ProtocolError(ErrorResult(primary=derived_not_applicable_violation(inapplicable.address, str(inapplicable))))


def _join_path_outside_row(outside: JoinPathOutsideRow) -> ProtocolError:
    """A ``join`` path that names no value of its declared row, as its code.

    ``schema-violation``, decided from the declarations before any row is computed,
    at the request's own address.
    """
    return ProtocolError(ErrorResult(primary=join_path_outside_row_violation(outside.address, str(outside))))


def _over_budget(over_budget: OverBudget) -> ProtocolError:
    """A response that does not fit, as the one rejection shape.

    An ``errorResult`` whose violation carries ``hints`` and ``measured``;
    ``addresses`` is omitted when ``over_budget.address`` is ``None``.
    """
    return ProtocolError(
        ErrorResult(
            primary=over_budget_violation(
                over_budget.address, str(over_budget), over_budget.hints, over_budget.measured
            )
        )
    )


def _record_rejection(
    request: Any,
    refusal: ProtocolError,
    change_set_id: str,
    scope_id: str | None,
    project: str | Path | SwcraftProject,
    loaded: SwcraftProject | None,
) -> None:
    """The ``change_set_rejected`` record of a refused change set, written.

    Written for every rejection of a change set, at every pass, including the three
    before staging. ``discover`` and ``read`` are unaudited, so the raw request is
    asked whether it claims to be a change set. The record is skipped only when the
    project cannot be opened (there is no log), which leaves the client's answer
    unchanged. An unreadable model root is not that case: the manifest parsed, so
    the log is reachable.
    """
    if not claims_change_set(request):
        return
    if loaded is None:
        try:
            loaded, _ = _open(project)
        except ProtocolError:
            return
    baseline = request.get("baseline")
    auditor_for(loaded, change_set_id, scope_id).rejected(
        refusal.result, baseline=baseline if isinstance(baseline, str) else None
    )


def _change_set(
    project: SwcraftProject,
    manifest: str,
    request: ChangeSetRequest,
    scope: tuple[ScopeEntry, ...] | None,
    auditor: ChangeSetAuditor,
    *,
    review: ReviewBoundary | None,
    notes: NoteSink | None,
    registry: reg.Registry,
    dependents: tuple[ChangeOperation, ...] = (),
    completion: tuple[ChangeOperation, ...] | None = None,
) -> dict[str, Any]:
    """The change-set lifecycle, steps 0-6.

    *dependents* are the server's expansions (:func:`_dependents`); *completion* is
    ``None`` on the first run and the repairs on the second, which never completes
    again. Server operations run after the client's. Lifecycle records go to the
    real audit log; the reviewed :class:`~swcraft_tools.differ.ModelDiff` itself is
    what is committed, and the shadow is dropped on every exit path.

    Raises:
        ProtocolError: a gate, the staleness check or a pass refused; the real
            model is byte-identical.
        ValueError: a writer refused a name pass D's identifier rule does not explain.
        UndiagnosedInconsistency: the validator refused with a rule
            ``diagnosis.RULE_CODES`` does not name, an engine defect.
        AuditReplayError, CommitPreflightError: faults with no protocol code; the
            model is byte-identical.
        OSError: the apply itself failed and the model may be partially applied;
            an intent record with no completion record beside it says so.
    """
    # Every address is reduced against this request's registry before anything
    # reads it, so gate 1, the apply, the identity checks and gate 2 compare
    # whole-value member steps in one form.
    client_operations = _reduced(request.operations, registry)
    server_operations = _reduced((*dependents, *(completion or ())), registry)
    request = replace(request, operations=(*client_operations, *server_operations))
    scope = _reduced_scope(scope, registry)
    unauthorized = gate_requested_operations(request.operations, scope)
    if unauthorized is not None:
        raise ProtocolError(ErrorResult(primary=unauthorized))

    # `stage` reads the baseline from the copy; an unparseable file is refused by
    # `{layer, element}`, never by the reader's message, which names a shadow path.
    try:
        staged = stage(manifest)
    except ElementParseError as unparseable:
        raise _unparseable_model(unparseable, registry) from unparseable

    with staged as staging:
        # Rebound onto the session's own real audit log: a `dist_root` retargeted
        # between the two manifest reads would otherwise split this commit's block
        # across two files.
        auditor = auditor.for_log(staging.session.real_audit_log)
        _refuse_a_stale_or_ambiguous_baseline(staging, request, registry)
        _apply_the_operations(staging, request, registry)
        _validate_the_staged_result(staging, project, request, completion, client_operations, registry)
        model_diff, delta, safety = _judge_the_delta(
            staging, scope, auditor, server_operations, client_operations, registry
        )

        auditor.staged(
            baseline=request.baseline,
            intent=request.intent,
            operations=client_operations,
            server_operations=server_operations,
            complete=request.complete,
            delta=delta,
        )
        if review is None:
            # No `audit_ref`: there is no commit request for a client to name it in.
            return ChangeSetResponse(status="staged", baseline=request.baseline, delta=tuple(delta)).to_json()

        decision = review(
            ChangeSetProposal(
                change_set_id=auditor.change_set_id,
                baseline=request.baseline,
                intent=request.intent,
                delta=tuple(delta),
                safety=safety,
                rewritten=staging.rewritten,
            )
        )
        auditor.approved(decision, scope, baseline=request.baseline, weakening=bool(safety.changes))
        if not decision.approved:
            auditor.abandoned(_DECLINED_REASON)
            return ChangeSetResponse(status="abandoned", baseline=request.baseline, delta=tuple(delta)).to_json()

        declined = _safety_decline_reason(safety, decision)
        if declined is not None:
            # `abandoned` alone cannot say which question was answered no, so the
            # reason goes to the record and to the host.
            auditor.abandoned(declined)
            if notes is not None:
                notes(declined)
            return ChangeSetResponse(status="abandoned", baseline=request.baseline, delta=tuple(delta)).to_json()

        return _commit(staging, request, auditor, model_diff, delta, notes)


def _refuse_a_stale_or_ambiguous_baseline(
    staging: StagedChangeSet,
    request: ChangeSetRequest,
    registry: reg.Registry,
) -> None:
    """The refusal of a change set authored against a model that has since moved, or over an unaddressable baseline.

    The element-level ambiguity check is global: an identity two files declare
    makes ``{layer, element}`` name two elements, and the last-wins index would
    answer an operation over it with an empty delta. The member-level check is per
    addressed element, so the ``remove`` of that element stays available as a repair.

    Raises:
        ProtocolError: ``stale-baseline``, or the first ambiguity as the primary
            violation and the rest as ``additional``.
    """
    try:
        staging.require_baseline(request.baseline)
    except StaleBaselineError as stale:
        raise ProtocolError.single(STALE_BASELINE, str(stale)) from stale

    ambiguous = (
        *ambiguous_baseline_violations(staging.snapshot, registry=registry),
        *ambiguous_member_violations(staging.snapshot, request.operations, registry=registry),
    )
    if ambiguous:
        raise ProtocolError(ErrorResult(primary=ambiguous[0], additional=ambiguous[1:]))


def _apply_the_operations(
    staging: StagedChangeSet,
    request: ChangeSetRequest,
    registry: reg.Registry,
) -> None:
    """The request's operations bound and applied in memory, then the touched elements materialized.

    Pass B and pass C both run, and their violations are reported together in pass
    order, then operation order. The writers record into the shadow audit log,
    which the commit rebases between this change set's delimiters. ``dist/`` is
    left untouched.

    Raises:
        ProtocolError: pass B or pass C refused at least one operation.
        ValueError: a writer refused a name pass D's identifier rule does not explain.
    """
    outcome = pass_b(request.operations, staging.snapshot, registry=registry)
    violations = _with_nearest(
        (*outcome.violations, *pass_c(request.operations, outcome.inapplicable)),
        staging.snapshot,
        registry,
        request.operations,
    )
    if violations:
        raise ProtocolError(ErrorResult(primary=violations[0], additional=violations[1:]))

    try:
        staging.materialize(outcome.touched, registry=registry, auditor=staging.session.shadow_audit_log)
    except ValueError as refused:
        raise _materialization_refusal(refused, outcome.staged) from refused


def _validate_the_staged_result(
    staging: StagedChangeSet,
    project: SwcraftProject,
    request: ChangeSetRequest,
    completion: tuple[ChangeOperation, ...] | None,
    client_operations: tuple[ChangeOperation, ...],
    registry: reg.Registry,
) -> None:
    """Pass D: validate the whole staged result and waive nothing.

    The baseline may be invalid; what is refused is a change set that leaves the
    model invalid, so the same change set may repair a pre-existing violation.

    Args:
        completion: ``None`` on the first run, the repairs on the second.
        client_operations: a repair completes only a component these create or update.

    Raises:
        ProtocolError: pass D found the assembled result invalid.
        _CompletionFound: a ``complete: true`` first run whose findings the server can repair.
        UndiagnosedInconsistency: the validator refused with a rule
            ``diagnosis.RULE_CODES`` does not name.
    """
    # A staged file the reader refuses, or a retired layout that appeared after the
    # door check, gets the door's answer rather than a fault with no record.
    try:
        # The manifest's ARXML version, which `[constr_2006]` is judged at.
        invalid, findings = pass_d_findings(staging.model_root, project.resolved_arxml_version)
    except ElementParseError as unparseable:
        raise _unparseable_model(unparseable, registry) from unparseable
    except RetiredStorageError as retired:
        raise _retired_refusal(retired) from retired
    if invalid:
        refusal = ProtocolError(ErrorResult(primary=invalid[0], additional=invalid[1:]))
        if request.complete and completion is None:
            repairs = _completion_operations(findings, client_operations, staging.model_root)
            if repairs:
                raise _CompletionFound(repairs, refusal)
        raise refusal


def _judge_the_delta(
    staging: StagedChangeSet,
    scope: tuple[ScopeEntry, ...] | None,
    auditor: ChangeSetAuditor,
    server_operations: tuple[ChangeOperation, ...],
    client_operations: tuple[ChangeOperation, ...],
    registry: reg.Registry,
) -> tuple[ModelDiff, list[DeltaEntry], SafetyVerdict]:
    """The delta projected, held to the approved scope, and put to the safety guard.

    Step 4 projects the diff onto the response's ``deltaEntry`` list. Step 5 is
    gate 2, the authority: every delta entry must be covered by the scope, every
    uncovered entry is reported, and there is no partial approval. A fault here is
    recorded as an abandonment and still propagates.

    Returns:
        The reviewed :class:`~swcraft_tools.differ.ModelDiff`, the delta projected
        from it, and the safety verdict over that delta.

    Raises:
        ProtocolError: gate 2 refused an entry of the delta.
    """
    # Every change set leaves a lifecycle record whatever its outcome: a fault
    # here (such as `differ.build_key_map` over a duplicated identity) is recorded
    # as an abandonment and propagates. The commit stays outside this guard, since
    # after the intent record "nothing was applied" may no longer be true.
    try:
        model_diff, delta, after = _delta(staging, registry)
        delta = _mark_server_entries(delta, server_operations, registry, client_operations)
        uncovered = gate_delta(delta, scope)
        if uncovered is not None:
            raise ProtocolError(ErrorResult(primary=uncovered))

        # Judged on the outcome, against the baseline and the staged model, so a
        # weakening bundled with other edits is still asked and a rename asks nothing.
        safety = safety_verdict(delta, staging.snapshot, staged=after, change_set_id=auditor.change_set_id)
    except ProtocolError:
        raise  # a rejection; `handle_request` writes its `change_set_rejected`
    except Exception as fault:
        # The class name only: a fault's message may name a path — the
        # reader's does — and this record goes to the real project's log.
        auditor.abandoned(
            f"an internal fault ({type(fault).__name__}) ended this change set before it was staged; "
            "nothing was applied and the model is byte-identical"
        )
        raise
    return model_diff, delta, safety


class _CompletionFound(Exception):
    """Pass D refused a ``complete: true`` change set the server can complete.

    Carries the operations that complete it and the first run's refusal, which
    is what the client is answered with when the completed set is refused too.
    """

    def __init__(self, operations: tuple[ChangeOperation, ...], refusal: ProtocolError) -> None:
        super().__init__(f"{len(operations)} completing operation(s)")
        self.operations = operations
        self.refusal = refusal


def _completion_operations(
    findings: tuple[ConsistencyIssue, ...], operations: tuple[ChangeOperation, ...], model_root: str | Path
) -> tuple[ChangeOperation, ...]:
    """The operations that complete a refused change set, or none when it is not completable.

    Completable means every finding is
    :data:`~swcraft_tools.validator.APPLICATION_TYPE_UNMAPPED` on an element the
    client's own operations create or update, and each subject has a unique,
    conflict-free repair (:func:`~swcraft_tools.validator.unmapped_type_repairs`);
    each repair is one ``create``. Otherwise the first refusal stands: the server
    completes the client's own change, never the rest of the model, and never
    reverses what the client stated.
    """
    if not findings or any(finding.rule != APPLICATION_TYPE_UNMAPPED for finding in findings):
        return ()
    touched = frozenset(
        (operation.address.layer, operation.address.element)
        for operation in operations
        if operation.address.path or not isinstance(operation, RemoveOperation)
    )
    subjects = frozenset(subject for finding in findings for subject in finding.subjects)
    if any((subject.layer, subject.element) not in touched for subject in subjects):
        return ()
    repairs = unmapped_type_repairs(str(model_root), subjects)
    if set(repairs) != subjects:
        return ()
    completion = tuple(
        CreateOperation(address=subject_address(repair.subject), value=repair.value) for repair in repairs.values()
    )
    # A client `remove` of the member, or an `update`/`remove` of the whole
    # collection, states what the collection holds: a server create under it
    # would reverse the client's own operation (a remove and a create of one
    # member cancel to an empty delta), so the first refusal stands.
    stated = [
        operation.address
        for operation in operations
        if not isinstance(operation, CreateOperation) and operation.address.path
    ]
    if any(
        (address.layer, address.element, address.path)
        == (repair.address.layer, repair.address.element, repair.address.path[: len(address.path)])
        for repair in completion
        for address in stated
    ):
        return ()
    return completion


def _mark_server_entries(
    delta: list[DeltaEntry],
    server_operations: tuple[ChangeOperation, ...],
    registry: reg.Registry,
    client_operations: tuple[ChangeOperation, ...],
) -> list[DeltaEntry]:
    """*delta* with each entry a server operation produced, or added to, marked by ``origin``.

    An entry at a server operation's own address is the server's; an entry
    whose address *contains* one — the element the client created in the same
    change set, which the server's member was added into — is both parties' when
    a client operation touches that element, and the server's alone otherwise (a
    task whose runnable entry a ``with_dependents`` remove took out).

    A ``satisfies`` list is a multiset, so the client and the server can each
    remove one occurrence of the same member (``confirm_links`` over a list
    holding it twice): the two delta entries are identical, and as many of them
    as the client's own operations of that verb at that address are left the
    client's, the rest the server's. The client's counts are **net**: a client
    ``remove`` and ``create`` of one member cancel in the delta, so the pair
    leaves no entry the client's (a list holding ``R@1`` twice, of which the
    client removes and re-creates one, has the server's removal of the other
    marked the server's).
    """
    if not server_operations:
        return delta
    addresses = [reduce_address(operation.address, registry=registry) for operation in server_operations]
    client_elements = {(operation.address.layer, operation.address.element) for operation in client_operations}
    client_own = Counter(
        (operation.operation, reduce_address(operation.address, registry=registry)) for operation in client_operations
    )
    for verb, address in [key for key in client_own if key[0] == "remove"]:
        cancelled = min(client_own[(verb, address)], client_own[("create", address)])
        client_own[(verb, address)] -= cancelled
        client_own[("create", address)] -= cancelled
    marked = []
    for entry in delta:
        origin: DeltaOrigin | None = None
        for address in addresses:
            if (address.layer, address.element) != (entry.address.layer, entry.address.element):
                continue
            if address.path == entry.address.path:
                own = (entry.operation, address)
                if client_own[own] > 0:
                    client_own[own] -= 1
                else:
                    origin = "server"
                break
            if address.path[: len(entry.address.path)] == entry.address.path:
                touched_by_client = (entry.address.layer, entry.address.element) in client_elements
                origin = "client_and_server" if touched_by_client else "server"
        marked.append(replace(entry, origin=origin) if origin else entry)
    return marked


def _unparseable_model(unparseable: ElementParseError, registry: reg.Registry) -> ProtocolError:
    """A model file that does not parse, as the code the client is owed.

    The reader's message never travels (it names storage). Limit: the address comes
    from the file name, so a suffixed file (``Speed_kph~1.yaml``) answers
    ``Speed_kph~1``.
    """
    address = element_address_of_target(unparseable.target, registry=registry)
    return ProtocolError(
        ErrorResult(primary=unparseable_element_violation(None if address is None else Address(*address)))
    )


def _materialization_refusal(refused: ValueError, staged: Any) -> ProtocolError:
    """One writer refusal during materialization, as the code that answers it.

    Translated, never re-raised:

    * a placement value the registry cannot compose: ``schema-violation`` at that
      placement field, as at bind time;
    * an element identity that is not a path segment: ``schema-violation`` at the
      element, with no path;
    * a reserved name: ``consistency-violation`` at the element;
    * a ``create`` whose plain file name is held by a file declaring that identity:
      ``identity-conflict`` at the element;
    * a ``remove`` or a re-write of an element no file of its layer declares:
      ``consistency-violation`` at the element;
    * an illegal emitted ``SHORT-NAME``: ``invalid-identifier``, asked of pass D's
      identifier rule over the staged tree.

    A writer fault none of these explains is an engine defect and is re-raised.
    """
    if isinstance(refused, PlacementRefusal):
        return ProtocolError(
            ErrorResult(
                primary=placement_violation(refused.layer, refused.element, refused.field, str(refused)),
            )
        )
    if isinstance(refused, TargetOccupied):
        return ProtocolError(
            ErrorResult(primary=target_occupied_violation(refused.layer, refused.element, str(refused)))
        )
    if isinstance(refused, TargetVanished):
        return ProtocolError(
            ErrorResult(primary=target_vanished_violation(refused.layer, refused.element, str(refused)))
        )
    if isinstance(refused, ReservedNameError):
        return ProtocolError(ErrorResult(primary=reserved_name_violation(refused.layer, refused.element, str(refused))))
    # Pass D's identifier rule, asked over the in-memory staged tree: the writer's
    # guards run before pass D, so the condition arrives as a `ValueError`.
    identifiers = emitted_identifier_violations(staged)
    if not identifiers:
        raise refused
    return ProtocolError(ErrorResult(primary=identifiers[0], additional=identifiers[1:]))


def _safety_decline_reason(safety: SafetyVerdict, decision: ApprovalDecision) -> str | None:
    """Why an approved change set whose safety change was not approved is abandoned.

    The safety guard is always on, scoped or not, and orthogonal to the scope: the
    scope says which addresses may be touched, this asks whether a touch removes
    protection. ``safety_approved`` fails closed, so a host unaware of the field
    cannot approve a weakening by omission.

    A declined safety change is an abandonment, not a rejection: the review
    boundary said no and the model is byte-identical. ``unauthorized-delta`` would
    tell the client to narrow a scope that was never the obstacle.
    """
    if not safety.changes or decision.safety_approved:
        return None
    # No layer clause: a `SafetyChange` carries a resource, not an address, and
    # `describe()` already names what each one is. "Safety change" rather than
    # "weakening": a grant of a partition's trust is asked and weakens no check.
    named = ", ".join(dict.fromkeys(change.resource for change in safety.changes))
    # Every note travels with the lines it explains, as in the prompt: without it
    # a connection-witnessed line can name an interface change the port did not make.
    noted = "".join(f" Note: {note}." for note in safety.notes)
    return (
        "the review boundary approved the delta and did not approve the safety change it carries: "
        + "; ".join(change.describe() for change in safety.changes)
        + f" (in {named}).{noted} The safety guard is always on and is orthogonal to the approved scope: "
        "approving the delta is not approving the safety change. Nothing was applied and the model is "
        "byte-identical"
    )


def _commit(
    staging: StagedChangeSet,
    request: ChangeSetRequest,
    auditor: ChangeSetAuditor,
    model_diff: ModelDiff,
    delta: list[DeltaEntry],
    notes: NoteSink | None,
) -> dict[str, Any]:
    """The approved change set applied, and the answer carrying the token the commit produced.

    *model_diff* is the reviewed diff: the files applied are exactly those it
    accounts for. A stale baseline becomes ``stale-baseline``; a
    :class:`~swcraft_tools.session.CommitLogError` is still ``committed``, without
    ``audit_ref``, with the message as a note.

    Runs under :func:`~swcraft_tools.session.commit_lock` (the fingerprint check is a
    check-then-act), never across the review; the lock bounds this process only.
    """
    before = request.baseline
    # The tree the apply writes, and therefore the only root worth locking,
    # fingerprinting or tokenizing here.
    real_model_root = staging.session.real_model_root
    with commit_lock(real_model_root):
        try:
            applied = staging.commit(
                model_diff, before_apply=lambda: auditor.commit_intent(baseline=before, delta=delta)
            )
        except StaleBaselineError as stale:
            raise ProtocolError.single(STALE_BASELINE, str(stale)) from stale
        except UnexplainedChangeError as unexplained:
            return _abandoned_unexplained(request, auditor, delta, unexplained, notes)
        except CommitLogError as log_failure:
            return _committed_without_trail(real_model_root, before, delta, log_failure, notes)

        after = baseline_token(real_model_root)
        try:
            auditor.committed(baseline_before=before, baseline_after=after, delta=delta, files=applied)
        except CommitLogError as log_failure:
            return _committed_without_trail(real_model_root, before, delta, log_failure, notes, after=after)
    return ChangeSetResponse(
        status="committed", baseline=after, delta=tuple(delta), audit_ref=auditor.change_set_id
    ).to_json()


def _abandoned_unexplained(
    request: ChangeSetRequest,
    auditor: ChangeSetAuditor,
    delta: list[DeltaEntry],
    failure: UnexplainedChangeError,
    notes: NoteSink | None,
) -> dict[str, Any]:
    """A commit the byte-level tripwire refused: answered as the abandonment it is.

    Nothing was applied; the reason goes to the host as a note and into the
    ``change_set_abandoned`` record. Reaching this is an engine defect.
    """
    reason = f"the commit's byte-level tripwire refused it: {failure}"
    auditor.abandoned(reason)
    if notes is not None:
        notes(reason)
    return ChangeSetResponse(status="abandoned", baseline=request.baseline, delta=tuple(delta)).to_json()


def _committed_without_trail(
    real_model_root: str,
    before: str,
    delta: list[DeltaEntry],
    failure: CommitLogError,
    notes: NoteSink | None,
    *,
    after: str | None = None,
) -> dict[str, Any]:
    """A committed model whose audit trail is incomplete.

    Answered as the successful commit it is, so a user neither looks for damage nor
    redoes a change that landed. The post-commit token is read from
    *real_model_root*, the staging session's root, for :func:`_commit`'s reason.
    """
    if notes is not None:
        notes(
            f"the change set was committed in full and its audit trail is incomplete ({failure}); "
            f"{len(failure.applied)} file(s) were applied and `audit_ref` is therefore withheld"
        )
    return ChangeSetResponse(
        status="committed",
        baseline=after if after is not None else baseline_token(real_model_root),
        delta=tuple(delta),
    ).to_json()


def _delta(staging: StagedChangeSet, registry: reg.Registry) -> tuple[ModelDiff, list[DeltaEntry], ModelSnapshot]:
    """Step 4: the reviewable delta between the baseline and the staged result.

    The staged snapshot is loaded once and returned with the
    :class:`~swcraft_tools.differ.ModelDiff` and the delta, so the commit, the delta
    and the safety guard all describe one comparison.
    """
    staged = load_snapshot(staging.model_root)
    model_diff = diff_snapshots(staging.snapshot, staged)
    return model_diff, project_delta(model_diff, baseline=staging.snapshot, staged=staged, registry=registry), staged


def _open(project: str | Path | SwcraftProject) -> tuple[SwcraftProject, str]:
    """*project* resolved to a loaded manifest, and the path it was loaded from.

    A directory is the project root with ``swcraft.yaml`` inside; there is no
    walk-up search.

    Raises:
        ProtocolError: the project could not be opened (``OSError``,
            ``yaml.YAMLError``, ``ValueError``); none of their messages travels.
    """
    if isinstance(project, SwcraftProject):
        return project, str(Path(project.resolved_project_root) / _MANIFEST_FILENAME)
    try:
        path = Path(project)
        manifest = str(path / _MANIFEST_FILENAME if path.is_dir() else path)
        return require_project(manifest), manifest
    except (OSError, ValueError, yaml.YAMLError) as unopenable:
        raise ProtocolError(ErrorResult(primary=unopenable_project_violation())) from unopenable
