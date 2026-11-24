"""The six change-set lifecycle records, the id that correlates them, and the commit-completeness check.

The id (:data:`CHANGE_SET_ID_PATTERN`) is allocated on receipt, before pass A,
and lives in ``args.change_set_id``. All six records go to the real project's log
in every run, including for change sets that never land.

**The commit block.** A commit appends ``change_set_commit_intent``, the rebased
writer entries, then ``change_set_committed``; the writer entries of a change set
are the ones between its two delimiters. Another change set's lifecycle record and
a host record (:data:`HOST_OPERATIONS`) are excluded from the span; everything else
stays in the block, so a foreign write stays visible.

**Completeness** (:func:`block_is_complete`) is an equality over element-level
``{layer, element}`` addresses; both sides refuse an unclassifiable item rather
than omit it.

**The log is untrusted input**: every shape is checked with :func:`isinstance`.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from swcraft_tools.auditor import AuditLog
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.address import Address, AddressError, ScopeEntry
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.protocol import ChangeOperation, DeltaEntry, ErrorResult
from swcraft_tools.model_registry import longest_directory_match
from swcraft_tools.project import SwcraftProject
from swcraft_tools.rendering import path_free
from swcraft_tools.session import CommitLogError, CommitPreflightError

__all__ = [
    "AUDIT_FILENAME",
    "CHANGE_SET_ABANDONED",
    "CHANGE_SET_APPROVED",
    "CHANGE_SET_COMMITTED",
    "CHANGE_SET_COMMIT_INTENT",
    "CHANGE_SET_ID_PATTERN",
    "CHANGE_SET_REJECTED",
    "CHANGE_SET_STAGED",
    "ELEMENT_WRITER_OPERATIONS",
    "HOST_OPERATIONS",
    "LIFECYCLE_OPERATIONS",
    "SAFETY_NOT_ASKED",
    "ChangeSetAuditor",
    "CommitBlock",
    "auditor_for",
    "block_is_complete",
    "claims_change_set",
    "commit_block",
    "committed_element_addresses",
    "element_address_of_target",
    "incomplete_commits",
    "new_change_set_id",
    "project_audit_log",
    "written_element_addresses",
]

# ---------------------------------------------------------------------------
# The operation names
# ---------------------------------------------------------------------------

#: Steps 1-4 completed: applied, validated, delta computed.
CHANGE_SET_STAGED = "change_set_staged"

#: Any of the ten change-set codes, at any pass, including before staging.
#: ``over-budget`` is not one: it refuses a read, which writes no record.
CHANGE_SET_REJECTED = "change_set_rejected"

#: The review boundary answered, from an injected decision: nothing here
#: prompts, renders or decides.
CHANGE_SET_APPROVED = "change_set_approved"

#: Written **before the first** ``os.replace()``. A record written after the
#: apply cannot describe a failure that prevented its own writing.
CHANGE_SET_COMMIT_INTENT = "change_set_commit_intent"

#: Written after the apply. The closing delimiter of the commit block.
CHANGE_SET_COMMITTED = "change_set_committed"

#: The staging tree was dropped.
CHANGE_SET_ABANDONED = "change_set_abandoned"

#: ``change_set_approved.args.safety_decision`` when there was no second
#: question: no safety change, or the delta was declined. Three-valued because
#: the fail-closed ``safety_approved`` reads ``False`` for both "declined" and
#: "never asked".
SAFETY_NOT_ASKED = "not-applicable"

#: The six, in lifecycle order. A seventh name would be a contract change.
LIFECYCLE_OPERATIONS: tuple[str, ...] = (
    CHANGE_SET_STAGED,
    CHANGE_SET_REJECTED,
    CHANGE_SET_APPROVED,
    CHANGE_SET_COMMIT_INTENT,
    CHANGE_SET_COMMITTED,
    CHANGE_SET_ABANDONED,
)

#: The two generic element writers: ``args.target`` is the file written or
#: removed, ``args.layer`` / ``args.identity`` the address it was for. The only
#: writers the protocol applies with, so the only ones a commit block may hold.
ELEMENT_WRITER_OPERATIONS: frozenset[str] = frozenset({"write_element", "remove_element"})

#: The records the agent host appends to the same log from the process that
#: spawned the MCP server (``SwcraftAgent._record``). They carry no change-set id
#: and can land inside a commit block, so the block excludes them.
#:
#: Excluded by name, never as "neither a writer nor a lifecycle record": that open
#: rule would drop a foreign write, such as a narrow ``mutations.py`` writer
#: logging ``create_swc`` from another process, which completeness must surface.
#: A host record not named here stays in the block and refuses completeness, the
#: fail-closed direction. The agent's tests pin this list against the host's
#: constants, because ``packages/agent/`` depends on this package and not the reverse.
HOST_OPERATIONS: frozenset[str] = frozenset({"scope_proposed", "scope_activated", "call_refused"})

#: The real log's filename under ``dist_root``, matching ``session.py``'s.
AUDIT_FILENAME = "audit.jsonl"


# ---------------------------------------------------------------------------
# The change-set id
# ---------------------------------------------------------------------------

#: ``cs-<UTC compact timestamp>-<6 hex>``, e.g. ``cs-20260815T102241Z-4f19c2``.
CHANGE_SET_ID_PATTERN = re.compile(r"^cs-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")

_ID_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"

#: 3 bytes -> 6 hex characters. The timestamp already separates change sets a
#: second apart; this separates the ones inside one second.
_ID_ENTROPY_BYTES = 3


def _now() -> datetime:
    """The clock, behind one name so a test can freeze it."""
    return datetime.now(tz=UTC)


def new_change_set_id() -> str:
    """One new change-set id, allocated on receipt of the request and before pass A.

    Returns:
        A string matching :data:`CHANGE_SET_ID_PATTERN`. The suffix is random
        rather than a counter because two processes may receive a change set in
        the same second.
    """
    return f"cs-{_now().strftime(_ID_TIMESTAMP_FORMAT)}-{secrets.token_hex(_ID_ENTROPY_BYTES)}"


def claims_change_set(request: Any) -> bool:
    """Whether *request* claims to be a change set, whatever pass A then makes of it.

    Asked of the raw request because a malformed change set still has a
    lifecycle to record, while ``discover`` and ``read`` write no record at all.
    """
    return isinstance(request, Mapping) and request.get("operation") == "change_set"


# ---------------------------------------------------------------------------
# Where the records go
# ---------------------------------------------------------------------------


def project_audit_log(project: SwcraftProject) -> AuditLog:
    """The real project's append-only log, never the shadow's.

    A refused or uncommitted change set still leaves its evidence where a human
    will look.
    """
    return AuditLog(str(Path(project.resolved_dist_root) / AUDIT_FILENAME), project_root=project.resolved_project_root)


def auditor_for(
    project: SwcraftProject,
    change_set_id: str,
    scope_id: str | None = None,
) -> ChangeSetAuditor:
    """A :class:`ChangeSetAuditor` writing *change_set_id*'s records to *project*'s real log."""
    return ChangeSetAuditor(project_audit_log(project), change_set_id, scope_id)


# ---------------------------------------------------------------------------
# The six records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangeSetAuditor:
    """One change set's lifecycle records, all carrying its id.

    Every method appends exactly one entry, with ``scope_id`` when the run is
    scoped. Nothing here decides: :meth:`approved` records a decision it is given.
    """

    log: AuditLog
    change_set_id: str
    scope_id: str | None = None

    def for_log(self, log: AuditLog) -> ChangeSetAuditor:
        """The same change set and the same scope, writing to *log*.

        The manifest is read twice (by the engine and by the staging session),
        and the session's read decides where the replayed writer entries go.
        Rebinding the delimiters to the session's log keeps one commit block in
        one file even if ``dist_root`` was retargeted between the two reads.
        """
        return ChangeSetAuditor(log, self.change_set_id, self.scope_id)

    # -- the two records a request can reach on its own -------------------

    def staged(
        self,
        *,
        baseline: str,
        intent: str | None,
        operations: Sequence[ChangeOperation],
        delta: Sequence[DeltaEntry],
        server_operations: Sequence[ChangeOperation] = (),
        complete: bool = False,
    ) -> None:
        """Steps 1-4 completed: applied, validated, delta computed.

        ``operations`` records what the request stated; the delta (carried by the
        intent and committed records) is what the model became. Operations the
        server added to a ``complete: true`` change set go under
        ``server_operations``, so the client is never credited with them. Both
        keys are always present (``[]`` and ``false`` when unused).
        """
        self._append(
            CHANGE_SET_STAGED,
            {
                "baseline": baseline,
                "intent": intent,
                "operations": [
                    {"operation": operation.operation, "address": operation.address.to_json()}
                    for operation in operations
                ],
                "server_operations": [
                    {"operation": operation.operation, "address": operation.address.to_json()}
                    for operation in server_operations
                ],
                "complete": complete,
            },
            f"staged: {len(operations)} operation(s), "
            + (f"{len(server_operations)} added by the server, " if server_operations else "")
            + f"{len(delta)} delta entr(ies)",
        )

    def rejected(self, result: ErrorResult, *, baseline: str | None) -> None:
        """Any of the ten change-set codes, at any pass.

        Args:
            result: the error; every address it names, primary and
                ``additional``, is recorded, deduplicated in order.
            baseline: the token the request carried, or ``None`` when it never
                parsed far enough to name one (recorded as ``null``).
        """
        addresses = [
            *result.primary.addresses,
            *(address for violation in result.additional for address in violation.addresses),
        ]
        self._append(
            CHANGE_SET_REJECTED,
            {
                "baseline": baseline,
                "error": result.error,
                "failure_class": result.failure_class,
                "addresses": _unique_addresses(addresses),
            },
            result.error,
        )

    # -- the review boundary ----------------------------------------------

    def approved(
        self,
        decision: ApprovalDecision,
        scope: Sequence[ScopeEntry] | None,
        *,
        baseline: str,
        weakening: bool,
    ) -> None:
        """The review boundary answered, from an injected decision, never a prompt.

        Records ``decision`` (the delta's answer), ``safety_decision`` (the
        safety question's answer, or ``not-applicable`` when none was put) and
        ``channel`` (the host's review mode, or ``null``).

        Args:
            decision: the answer, bound to one change-set id and one baseline.
            scope: the approved scope entries, or ``None`` for an unscoped run
                (``[]`` means nothing was approved).
            baseline: the token the decision was taken at.
            weakening: whether the safety guard asked a second question at all
                (a non-empty verdict, which includes a granted partition trust).

        Raises:
            ApprovalMismatchError: *decision* was taken over another change set
                or at another baseline (a fault, not a rejection).
        """
        decision.require_binding(self.change_set_id, baseline)
        second_question = weakening and decision.approved
        self._append(
            CHANGE_SET_APPROVED,
            {
                "approved_scope": None if scope is None else [entry.to_json() for entry in scope],
                "decision": _decision_word(decision.approved),
                "safety_decision": _decision_word(decision.safety_approved) if second_question else SAFETY_NOT_ASKED,
                "channel": decision.channel,
            },
            _decision_word(decision.approved),
        )

    # -- the commit block's two delimiters --------------------------------

    def commit_intent(self, *, baseline: str, delta: Sequence[DeltaEntry]) -> None:
        """The opening delimiter, written before the first ``os.replace()``.

        An intent record with no completion record is the crash signature
        :func:`incomplete_commits` detects.

        Raises:
            CommitPreflightError: the record could not be appended. Nothing was
                applied, so this is a pre-apply failure, never a "may be
                partially applied" one.
        """
        try:
            self._append(
                CHANGE_SET_COMMIT_INTENT,
                {"baseline": baseline, "addresses": _delta_addresses(delta)},
                "intent",
            )
        except OSError as exc:
            raise CommitPreflightError(
                f"the commit-intent record could not be written ({path_free(exc)}); nothing was applied and the model "
                "is byte-identical"
            ) from exc

    def committed(
        self,
        *,
        baseline_before: str,
        baseline_after: str,
        delta: Sequence[DeltaEntry],
        files: Sequence[str],
    ) -> None:
        """The closing delimiter, written after the apply.

        Args:
            files: the real-model paths the apply wrote or removed. Only their
                count is recorded: paths are storage.

        Raises:
            CommitLogError: the record could not be appended. The model is
                committed in full; only the trail is incomplete.
        """
        try:
            self._append(
                CHANGE_SET_COMMITTED,
                {
                    "baseline_before": baseline_before,
                    "baseline_after": baseline_after,
                    "addresses": _delta_addresses(delta),
                },
                f"{len(files)} file(s)",
            )
        except OSError as exc:
            raise CommitLogError(
                f"the model was committed in full, but the completion record could not be written ({path_free(exc)})",
                list(files),
            ) from exc

    # -- the end of a transaction that did not land -----------------------

    def abandoned(self, reason: str) -> None:
        """The change set ended in the ``abandoned`` terminal state, and why.

        Records a host-visible end (the review declined, the commit's byte-level
        tripwire refused, or an internal fault before staging), not the dropping
        of the shadow, which every exit path does.
        """
        if not reason:
            raise ValueError("an abandoned record states its reason; it is the record's whole content")
        self._append(CHANGE_SET_ABANDONED, {"reason": reason}, reason)

    # -- the one append ----------------------------------------------------

    def _append(self, operation: str, args: dict[str, Any], result: str) -> None:
        """One entry, with ``change_set_id`` first in ``args`` and nothing added to the four keys."""
        self.log.log_write(operation, {"change_set_id": self.change_set_id, **args}, result, self.scope_id)


def _decision_word(approved: bool) -> str:
    return "approved" if approved else "declined"


def _unique_addresses(addresses: Iterable[Address]) -> list[dict[str, Any]]:
    """The addresses as ``$defs/address`` JSON, deduplicated in the order found."""
    seen: list[dict[str, Any]] = []
    for address in addresses:
        rendered = address.to_json()
        if rendered not in seen:
            seen.append(rendered)
    return seen


def _delta_addresses(delta: Sequence[DeltaEntry]) -> list[dict[str, Any]]:
    """The reviewed delta's addresses at full depth, leaf paths included.

    The record is evidence a human reads; the element-level truncation is derived
    from it by :func:`committed_element_addresses`.
    """
    return _unique_addresses(entry.address for entry in delta)


# ---------------------------------------------------------------------------
# The commit block, and its completeness equality
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommitBlock:
    """One change set's commit block: its two delimiters and the entries between them.

    ``entries`` is everything in the span that no other party positively claims
    (:func:`commit_block` drops only another change set's lifecycle records and
    host records). For a well-formed block that is this change set's writer
    entries exactly as replayed; anything else kept is evidence, and
    :func:`block_is_complete` says which case holds.

    Contiguity is a claim about writer entries only, delivered by
    :func:`~swcraft_tools.session.commit_lock`: every writer entry in the span is
    this change set's, and all of them are there.
    """

    change_set_id: str
    intent: Mapping[str, Any]
    committed: Mapping[str, Any]
    entries: tuple[Mapping[str, Any], ...]


def commit_block(entries: Sequence[Mapping[str, Any]], change_set_id: str) -> CommitBlock | None:
    """The commit block of *change_set_id* in *entries*, or ``None`` if there is none.

    Args:
        entries: the log, in insertion order —
            :meth:`~swcraft_tools.auditor.AuditLog.get_entries`'s output.
        change_set_id: the id the two delimiters carry in ``args.change_set_id``.

    Returns:
        The block, or ``None`` when the log holds no intent record for that id or
        an intent record with no completion record (the crash signature,
        reported by :func:`incomplete_commits`).

    Only two foreign shapes inside the span are skipped: another change set's
    lifecycle record (by id) and a host record (:data:`HOST_OPERATIONS`).
    A repeated ``change_set_id`` is not defended against: the first intent and the
    first completion record are paired.
    """
    start = _index_of(entries, CHANGE_SET_COMMIT_INTENT, change_set_id)
    end = _index_of(entries, CHANGE_SET_COMMITTED, change_set_id)
    if start is None or end is None or end < start:
        return None
    return CommitBlock(
        change_set_id=change_set_id,
        intent=entries[start],
        committed=entries[end],
        entries=tuple(
            entry for entry in entries[start + 1 : end] if not _is_another_partys_record(entry, change_set_id)
        ),
    )


def _is_another_partys_record(entry: Mapping[str, Any], change_set_id: str) -> bool:
    """An entry in the span that positively identifies itself as somebody else's.

    Tested by declared name, never "not a writer, therefore foreign", which would
    drop the entry that must break the block. An unreadable entry stays.
    """
    operation = _operation_of(entry)
    if operation in HOST_OPERATIONS:
        return True
    identifier = _change_set_id(entry)
    return operation in LIFECYCLE_OPERATIONS and identifier is not None and identifier != change_set_id


def incomplete_commits(entries: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Every change-set id with an intent record and no completion record.

    The crash signature, in log order. It does not distinguish a mid-apply crash
    (the model may be partially applied) from a failed completion append (the
    model is committed in full); either way the intent record's addresses are the
    elements to inspect.
    """
    committed = {_change_set_id(entry) for entry in entries if _operation_of(entry) == CHANGE_SET_COMMITTED}
    return tuple(
        identifier
        for entry in entries
        if _operation_of(entry) == CHANGE_SET_COMMIT_INTENT
        and (identifier := _change_set_id(entry)) is not None
        and identifier not in committed
    )


def committed_element_addresses(
    block: CommitBlock, *, registry: reg.Registry | None = None
) -> frozenset[tuple[str, str]]:
    """The element-level ``(layer, element)`` addresses of a block's committed delta.

    For reporting only: an unclassifiable recorded address is dropped here,
    while :func:`block_is_complete` refuses the block for it.
    """
    registry = reg.or_current(registry)
    return frozenset(address for address in _committed_addresses(block, registry) if address is not None)


def written_element_addresses(
    block: CommitBlock, *, registry: reg.Registry | None = None
) -> frozenset[tuple[str, str]]:
    """Which elements a block's writer entries wrote, as a set, for reporting.

    Read by :func:`_writer_entry_address`. Lossy: a repeated element collapses
    and an unclassifiable entry is dropped, so :func:`block_is_complete` does not
    decide from this.
    """
    registry = reg.or_current(registry)
    return frozenset(address for address in _writer_addresses(block, registry) if address is not None)


def block_is_complete(block: CommitBlock, *, registry: reg.Registry | None = None) -> bool:
    """Whether every committed element has exactly one writer entry, and nothing else is in the block.

    Complete when every entry and every committed address classifies, no element
    is written twice, and the elements written equal the elements committed.
    ``True`` does not mean nothing else touched the log: the two foreign shapes
    :func:`commit_block` skips are not seen here.
    """
    registry = reg.or_current(registry)
    written = _writer_addresses(block, registry)
    committed = _committed_addresses(block, registry)
    if any(address is None for address in (*written, *committed)):
        return False
    if len(written) != len(set(written)):
        return False
    return set(committed) == set(written)


def _writer_addresses(block: CommitBlock, registry: reg.Registry) -> list[tuple[str, str] | None]:
    """One item per block entry, in order: its element address, or ``None`` if unclassifiable.

    A list, not a set: the caller needs the count and the unclassifiable items.
    """
    return [_writer_entry_address(entry, registry) for entry in block.entries]


def _writer_entry_address(entry: Any, registry: reg.Registry) -> tuple[str, str] | None:
    """One writer entry's ``(layer, element)``: the address it recorded, or its target composed back.

    ``None`` when the entry does not classify: a record carrying only one of
    ``args.layer``/``args.identity``, an unusable value, or a target not stored
    under the recorded layer. An entry carrying neither key is composed back from
    its target; no collision suffix is parsed, so a suffixed target fails closed.

    **Does not reach**: a target whose file name is not a composable identity (a
    hand-authored ``a..b.yaml`` edited in place) does not classify, so such a
    commit reads as incomplete although it was not.
    """
    target = _writer_target(entry)
    if target is None:
        return None
    args = entry["args"]
    if "layer" not in args and "identity" not in args:
        return element_address_of_target(target, registry=registry)
    layer, identity = args.get("layer"), args.get("identity")
    if not isinstance(layer, str) or not isinstance(identity, str):
        return None
    if layer not in registry.layer_names or registry.identity_refusal(layer, identity) is not None:
        return None
    stored_under = element_address_of_target(target, registry=registry)
    if stored_under is None or stored_under[0] != layer:
        return None
    return layer, identity


def _committed_addresses(block: CommitBlock, registry: reg.Registry) -> list[tuple[str, str] | None]:
    """One item per address the committed record names, in order: it, or ``None`` if unclassifiable.

    A record with no readable ``args.addresses`` list yields a single ``None``:
    it is unreadable, not a commit of nothing. Repeats are expected (the delta is
    per leaf).
    """
    recorded = _recorded_addresses(block.committed)
    if recorded is None:
        return [None]
    return [_recorded_element_address(address, registry=registry) for address in recorded]


def _recorded_element_address(address: Any, *, registry: reg.Registry | None = None) -> tuple[str, str] | None:
    """One recorded ``$defs/address`` as ``(layer, element)``, or ``None`` for anything else.

    Parsed with the producer's own parser (:meth:`Address.from_json`), so a
    malformed ``path`` or an extra key is refused rather than classified from the
    two fields returned. On top, the layer must be declared and the element must
    pass :meth:`~reg.Registry.identity_refusal`, because ``$defs/identity`` admits
    values such as ``a..b`` and ``A.`` that storage refuses. The ``path`` is then
    dropped: a field-level address classifies as its element.
    """
    registry = reg.or_current(registry)
    try:
        parsed = Address.from_json(address)
    except AddressError:
        return None
    if not parsed.is_element_address:
        return None
    element = parsed.element
    assert element is not None  # `is_element_address` is exactly this question
    if parsed.layer not in registry.layer_names:
        return None
    if registry.identity_refusal(parsed.layer, element) is not None:
        return None
    return parsed.layer, element


def element_address_of_target(target: str, *, registry: reg.Registry | None = None) -> tuple[str, str] | None:
    """``swc/sensors/Foo.yaml`` -> ``("swc", "Foo")``, or ``None`` for a path no layer stores.

    What the file name spells, which is the identity only when nothing collided;
    used for older entries with no recorded address and for the layer a recorded
    target is stored under. Applied only to targets this package wrote to its own
    log; the placement component is dropped, not recovered.

    Accepts exactly what :meth:`~reg.Registry.compose_path` can compose: the
    parts are read back into ``(layer, identity, placement)``, composed again,
    and must reproduce the target byte for byte. So the registry's own rules
    decide, and paths such as ``swc/example/nested/ExampleSWC.yaml``,
    ``tasks/.yaml``, ``tasks/a..b.yaml`` and ``tasks/A..yaml`` do not classify.
    An empty or ``.`` placement is refused, never normalised, so a component
    filed directly under ``swc/`` is not classified by its target.
    """
    registry = reg.or_current(registry)
    by_directory = _layer_of_directory(registry)
    match = longest_directory_match(by_directory, target)
    if match is None:
        return None
    directory, remainder = match
    layer = by_directory[directory]
    record = registry.storage(layer)
    parts = remainder.split("/")
    if len(parts) != (2 if record.is_recursive else 1):
        return None
    filename = parts[-1]
    if not filename.endswith(record.file_suffix):
        return None
    identity = filename[: -len(record.file_suffix)]
    placement = {record.nesting: parts[0]} if record.is_recursive else None
    try:
        composed = registry.compose_path(layer, identity, placement)
    except reg.PathSegmentError:
        return None
    return (layer, identity) if composed == target else None


def _layer_of_directory(registry: reg.Registry) -> dict[str, str]:
    """directory -> layer, built from the same storage records ``compose_path`` uses.

    A directory may have two segments, so a target is matched against the
    longest key that prefixes it, never by its first segment.
    """
    return {registry.storage(name).directory: name for name in registry.layer_names}


def _writer_target(entry: Any) -> str | None:
    """The composed target of one generic-writer entry, or ``None`` for anything else."""
    if _operation_of(entry) not in ELEMENT_WRITER_OPERATIONS:
        return None
    args = entry.get("args")
    if not isinstance(args, Mapping):
        return None
    target = args.get("target")
    return target if isinstance(target, str) else None


def _operation_of(entry: Any) -> str | None:
    """The **declared** operation name of one log entry, or ``None`` for any other shape.

    A hand-edited or truncated line can be a list, ``null``, or an object whose
    ``operation`` is ``[]``; reading those naively would crash the forensic
    readers on the corruption they exist to surface. An entry with no readable
    operation stays in a block and makes it incomplete.
    """
    if not isinstance(entry, Mapping):
        return None
    operation = entry.get("operation")
    return operation if isinstance(operation, str) else None


def _recorded_addresses(entry: Any) -> list[Any] | None:
    """The raw, unfiltered ``args.addresses`` list of a record, or ``None`` when there is none to read."""
    if not isinstance(entry, Mapping):
        return None
    args = entry.get("args")
    if not isinstance(args, Mapping):
        return None
    addresses = args.get("addresses")
    return list(addresses) if isinstance(addresses, list) else None


def _change_set_id(entry: Any) -> str | None:
    if not isinstance(entry, Mapping):
        return None
    args = entry.get("args")
    if not isinstance(args, Mapping):
        return None
    identifier = args.get("change_set_id")
    return identifier if isinstance(identifier, str) else None


def _index_of(entries: Sequence[Mapping[str, Any]], operation: str, change_set_id: str) -> int | None:
    for position, entry in enumerate(entries):
        if _operation_of(entry) == operation and _change_set_id(entry) == change_set_id:
            return position
    return None
