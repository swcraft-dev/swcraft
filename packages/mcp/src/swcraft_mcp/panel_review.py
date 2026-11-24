"""The panel review: one review question handed to the SWCraft VS Code panel.

`SWCRAFT_REVIEW=panel` is the review channel for an MCP client that declines
elicitation. The server writes the question as a **pending record** into
``<SWCraft home>/reviews/`` (the home must lie outside the project folder) and
waits for the **decision record** the extension's review tab writes beside it.
Nothing listens on a port.

``<change_set_id>.<question>.pending.json`` is written by this module only, and
``<change_set_id>.<question>.decision.json`` by the panel only, each exclusively
(a temporary file linked to the final name, which refuses an existing target).
Every ask draws a fresh 128-bit **wait token** that the panel copies into the
decision. A decision is accepted only when its ``format``, ``change_set_id``,
``question``, ``wait`` and ``digest`` (the sha256 of the text shown) all equal
the pending record's; any other file at that name can only deny, never approve.

The wait has no deadline: it ends when the question is answered, the client
cancels the call, or the server ends. The pending record's mtime is a heartbeat
refreshed every :data:`HEARTBEAT_SECONDS`; a record older than
:data:`STALE_SECONDS` is swept. The sweep never removes a decision.

:func:`ask` fails closed and never raises: it answers ``True`` only for an
``approve`` bound to the question. A rejection, a cancelled call, a closed
session, a malformed or mismatched decision, a SWCraft home inside the project
folder, a lost name and a failed hand-off are ``False``, all but the rejection
with a note that names no path. ``BaseException`` other than the cancellation
propagates.

Not reached: the decision carries no secret, so the mode needs tools confined to
the workspace; each wait holds one of anyio's worker threads (about 40 per
process), so later calls queue, unbounded; with no VS Code window, or a client
timeout that sends no cancel, the wait lasts until the session ends; a killed
server leaves its change set at ``change_set_staged``, its record swept at the
next ask; orphan decisions stay until the user empties the directory; a record
swept before its answer was read is restored and the user answers again; a
workspace opened at an ancestor of the home is invisible; a failing heartbeat
lets the record go stale; a sweep or another link can fall between the stat and
the unlink of the final cleanup or of the clearing after a link; a reused inode
can fake a record's identity; a change-set id drawn twice abandons the later
change set; a filesystem without hard links cannot hold the mode.
"""

from __future__ import annotations

import concurrent.futures
import errno
import hashlib
import json
import os
import secrets
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import anyio.from_thread
from swcraft_tools.editing.audit import CHANGE_SET_ID_PATTERN
from swcraft_tools.editing.engine import ChangeSetProposal, NoteSink
from swcraft_tools.editing.review import flatten
from swcraft_tools.exclusive_create import open_new
from swcraft_tools.swcraft_home import reviews_location

#: How often the wait looks for the decision, a cancelled call and a closed session.
POLL_SECONDS = 0.25

#: Seconds of the monotonic clock between heartbeats (the pending record's mtime);
#: a wall-clock jump by as much refreshes it at once.
HEARTBEAT_SECONDS = 15

#: Age of a pending record's mtime past which it belongs to no live wait: four
#: missed heartbeats. ``reviewCore.ts``'s ``STALE_MS`` is the same bound.
STALE_SECONDS = 60

#: Seconds of the monotonic clock between progress reports to the client.
PROGRESS_SECONDS = 60

#: Age below which the sweep leaves a temporary file alone (a write in flight).
SWEEP_GRACE_SECONDS = 60

#: The record format both halves write. Any other value is refused.
FORMAT = 2

#: The bound the intent is flattened to, as the question itself shows it.
INTENT_LIMIT = 200

#: The bound the MCP client's own name is flattened to: a label in the tab's
#: meta line, never a sentence.
CLIENT_LIMIT = 80

PENDING_SUFFIX = ".pending.json"
DECISION_SUFFIX = ".decision.json"
TMP_SUFFIX = ".tmp"

_DECISION_KEYS = frozenset({"format", "change_set_id", "question", "wait", "digest", "decision"})
_DECISIONS = frozenset({"approve", "reject"})

_ABANDONED_UNANSWERED = "the change set was abandoned without a human answer: SWCRAFT_REVIEW=panel is set, and "
_NOTHING_APPLIED = "Nothing was applied and the model is byte-identical"


def reviews_dir() -> Path:
    """``<SWCraft home>/reviews``, created on first use with mode ``0o700``.

    No environment variable overrides the location: an override is one more value
    a client's configuration could point into a workspace an agent's file tools
    reach. Windows ignores the mode.

    Raises:
        OSError: the directory cannot be created or its mode set.
        RuntimeError: :meth:`Path.home` cannot determine the home directory.
    """
    directory = reviews_location()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory


def text_digest(text: str) -> str:
    """``sha256:<hex>`` of *text* as UTF-8: what an approval is bound to."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def pending_name(change_set_id: str, kind: str) -> str:
    """The pending record's file name."""
    return f"{change_set_id}.{kind}{PENDING_SUFFIX}"


def decision_name(change_set_id: str, kind: str) -> str:
    """The decision record's file name."""
    return f"{change_set_id}.{kind}{DECISION_SUFFIX}"


def _utc(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_wait_token() -> str:
    """A fresh wait token: 32 lowercase hex digits, drawn once per :func:`ask`; it tells waits apart, no secret."""
    return secrets.token_hex(16)


def pending_record(
    question: str,
    *,
    kind: str,
    wait: str,
    proposal: ChangeSetProposal,
    project_name: str,
    manifest: str,
    now: float,
    client: str | None = None,
    warning_lines: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """The pending record of one question, in its key order. Reads no file.

    Args:
        manifest: the resolved POSIX spelling of the served manifest; the panel shows
            a record only in the window whose workspace manifest it names.
        wait: this ask's wait token, which a decision must carry.
        client: the MCP client's ``clientInfo.name``, flattened and bounded (``null``
            when unknown); shown for information only, nothing decides on it.
        warning_lines: the warning's line range in *question*, written only when set.
    """
    record: dict[str, Any] = {
        "format": FORMAT,
        "change_set_id": proposal.change_set_id,
        "question": kind,
        "wait": wait,
        "baseline": proposal.baseline,
        "project": {"name": project_name, "manifest": manifest},
        "client": flatten(client, CLIENT_LIMIT) if client else None,
        "intent": flatten(proposal.intent, INTENT_LIMIT) if proposal.intent else None,
        "text": question,
        "digest": text_digest(question),
        "created": _utc(now),
    }
    if warning_lines is not None:
        record["warning_lines"] = list(warning_lines)
    return record


def record_json(document: Mapping[str, Any]) -> str:
    """*document* as the text a record file holds: two-space JSON, non-ASCII kept, one trailing newline.

    The extension's ``decisionRecord`` writes the same shape.
    """
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def write_exclusively(path: Path, document: Mapping[str, Any]) -> os.stat_result | None:
    """Create *path* holding *document* as JSON, unless a file of that name exists.

    The JSON goes to a temporary ``.<name>.<pid>.<random hex>.tmp`` (mode ``0o600``,
    :func:`~swcraft_tools.exclusive_create.open_new`: ``O_EXCL``, no link followed),
    which is then **linked** to *path*; :func:`os.link` refuses an existing target,
    so a reader never sees a partial file and the first writer keeps the name. The
    temporary name is unique per write: a linked temporary file *is* the record, so
    a shared name could truncate a record already in place. The temporary file is
    removed afterwards, only when this call created it. No fallback for a
    filesystem without hard links: the panel has none either.

    The identity returned is the ``fstat`` of this call's own descriptor: a ``stat``
    of *path* after the link could already see another wait's file.

    Returns:
        The created file's ``os.stat_result`` when *path* was created, ``None``
        when a file already held the name.

    Raises:
        OSError: the temporary file could not be created or written (one
            already at its name included), or the link failed for any reason
            other than an existing target.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}{TMP_SUFFIX}")
    text = record_json(document)
    created = False
    try:
        # `open_new`'s descriptor is binary: a CRT text-mode one would turn "\n" into "\r\n" on Windows.
        new_file = open_new(tmp, 0o600)
        created = True
        with new_file as handle:
            handle.write(text)
            identity = os.fstat(handle.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return None
        return identity
    finally:
        if created:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


class DecisionMismatch(Exception):
    """A decision file that is not an answer to the pending question."""


def read_decision(path: Path, pending: Mapping[str, Any]) -> bool | None:
    """The decision at *path* as ``True``/``False``, or ``None`` when there is none yet.

    Raises:
        DecisionMismatch: a file is there and is not a decision on *pending*:
            not UTF-8, not JSON (a nesting too deep for the parser included),
            the wrong keys or types, or another change set, question, wait,
            digest or format. The caller abandons.
        OSError: the file could not be read (a Windows sharing violation while
            the panel renames); the caller retries on the next tick.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        decision = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as malformed:
        # `RecursionError`: a file nested deeper than the parser's recursion
        # limit (`[` repeated) is no decision either, and must not leave `ask`.
        raise DecisionMismatch from malformed
    if not isinstance(decision, dict) or set(decision) != _DECISION_KEYS:
        raise DecisionMismatch
    # `type(...) is int`, never `==`: `True == 1`, and a boolean is not a format.
    if type(decision["format"]) is not int or decision["format"] != FORMAT:
        raise DecisionMismatch
    for key in ("change_set_id", "question", "wait", "digest"):
        if not isinstance(decision[key], str) or decision[key] != pending[key]:
            raise DecisionMismatch
    if not isinstance(decision["decision"], str) or decision["decision"] not in _DECISIONS:
        raise DecisionMismatch
    return decision["decision"] == "approve"


def _clear_a_foreign_decision(path: Path, pending: Mapping[str, Any]) -> None:
    """Remove a decision at *path* that does not answer *pending*; keep one that does.

    Called right after *pending* is linked, once :func:`_settle` found the name still
    holding it. *pending*'s wait token is fresh, so a decision bound to it is this
    wait's answer, already given; removing it would leave a wait with no deadline
    hanging forever. A read that fails leaves the file for the tick's read, which
    fails closed.

    Raises:
        OSError: the removal of a foreign decision failed.
    """
    try:
        read_decision(path, pending)
    except DecisionMismatch:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _older_than_grace(path: Path, now: float) -> bool:
    return now - path.stat().st_mtime > SWEEP_GRACE_SECONDS


def sweep(directory: Path, now: float) -> None:
    """Remove what no live review needs: stale records and old temporaries. Never a decision.

    - a pending record whose mtime is more than :data:`STALE_SECONDS` before *now*;
    - a ``*.tmp`` older than :data:`SWEEP_GRACE_SECONDS`.

    A decision is never touched: the sweep owns no record, so any judgment of a
    decision could hit an answer to a question restored meanwhile. What this does
    not reach: a decision whose wait is over and whose record is gone stays on
    disk. Liveness is the mtime alone; no record is opened. A file that cannot be
    stat-ed or removed is skipped, so an unrelated question's file never aborts
    this ask.

    Raises:
        OSError: the listing failed; the caller abandons.
    """
    for name in sorted(entry.name for entry in os.scandir(directory)):
        path = directory / name
        try:
            if name.endswith(PENDING_SUFFIX):
                if now - path.stat().st_mtime > STALE_SECONDS:
                    path.unlink(missing_ok=True)
            elif name.endswith(TMP_SUFFIX) and _older_than_grace(path, now):
                path.unlink(missing_ok=True)
        except OSError:
            continue


def _errno_name(error: OSError) -> str:
    """The error's symbolic name, never ``str(error)``, which carries the path."""
    if error.errno is not None and error.errno in errno.errorcode:
        return errno.errorcode[error.errno]
    return type(error).__name__


CANCELLED_NOTE = (
    f"{_ABANDONED_UNANSWERED}the review was withdrawn because the client cancelled the call. {_NOTHING_APPLIED}."
)

MISMATCH_NOTE = (
    "the change set was abandoned: SWCRAFT_REVIEW=panel is set, and the decision found for this change set "
    f"did not match the question shown, so it was not taken as an answer. {_NOTHING_APPLIED}."
)


SESSION_CLOSED_NOTE = (
    f"{_ABANDONED_UNANSWERED}the review was withdrawn because the client closed its session with the server. "
    f"{_NOTHING_APPLIED}."
)


NAME_TAKEN_NOTE = (
    f"{_ABANDONED_UNANSWERED}another review waiting in the SWCraft panel already holds this change set's "
    f"question, so this one was not handed over. {_NOTHING_APPLIED}; propose the change set again."
)


HOME_INSIDE_THE_PROJECT_NOTE = (
    f"{_ABANDONED_UNANSWERED}the project folder contains the SWCraft home, so the review in VS Code cannot be "
    f"kept out of the assistant's reach. {_NOTHING_APPLIED}; keep the project in a folder that does not contain "
    "the SWCraft home to review its changes in VS Code."
)


def contains_the_reviews(project_dir: Path, reviews: Path) -> bool:
    """Whether *reviews* is *project_dir* or lies under it, both already resolved.

    Decided by path components, then by **filesystem identity** (``samestat`` of
    *reviews* and each parent against the project folder): on a case-insensitive
    volume two spellings differing in case name one folder. A component that does
    not exist yet is skipped and its nearest existing ancestor decides.

    What this does not reach: the same folder through a second mount reporting
    another device, and the client's workspace, which the server cannot see.

    Raises:
        OSError: the project folder cannot be ``stat``-ed, or a parent of
            *reviews* fails with anything but a missing component. The caller
            treats it as a hand-off failure, never as "not contained".
    """
    if reviews == project_dir or reviews.is_relative_to(project_dir):
        return True
    project_stat = project_dir.stat()
    for candidate in (reviews, *reviews.parents):
        try:
            candidate_stat = candidate.stat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        if os.path.samestat(candidate_stat, project_stat):
            return True
    return False


def handoff_note(reason: str) -> str:
    """The note for a review that could not be handed to the panel at all.

    *reason* is a short label naming no path: an errno or exception class name,
    or a phrase such as ``"invalid change-set id"``.
    """
    return (
        "the change set was abandoned without a human answer: SWCRAFT_REVIEW=panel is set, but the server "
        f"could not hand the review to the SWCraft panel ({reason}). {_NOTHING_APPLIED}."
    )


#: The question each kind's progress text names.
_PROGRESS_SUBJECT = {"delta": "change", "safety": "safety question"}

#: Resolved by ``ask`` at call time, so a test can patch them.
_monotonic = time.monotonic
_wall = time.time
_sleep = time.sleep


def progress_text(kind: str, minutes: int) -> str:
    """The progress text of a wait on *kind*'s question, *minutes* after it was asked; it names no path."""
    subject = _PROGRESS_SUBJECT[kind]
    return f"Waiting for your answer in the SWCraft review tab in VS Code ({subject}), {minutes} min."


def cancellation_classes(cancelled_exc: type[BaseException]) -> tuple[type[BaseException], ...]:
    """The classes a cancelled call can raise out of ``anyio.from_thread.run`` in a worker.

    *cancelled_exc* (the backend's class) is what anyio 4.15.1 raises;
    :class:`concurrent.futures.CancelledError` is what a later anyio may raise, so
    both are handled.
    """
    return (cancelled_exc, concurrent.futures.CancelledError)


def _closed(session_closed: Callable[[], bool] | None) -> bool:
    """Whether the client's session is gone; any failure of the probe answers "not closed"."""
    if session_closed is None:
        return False
    try:
        return bool(session_closed())
    except Exception:  # noqa: BLE001 -- protocol: a probe that cannot be read answers "not closed"
        return False


def _restore(
    path: Path, record: dict[str, Any]
) -> tuple[Literal["restored", "lost", "retry"], dict[str, Any], os.stat_result | None]:
    """Put this ask's question back at *path* under a fresh wait token.

    Nothing at the decision name is touched before the link, when this ask owns
    nothing there; :func:`_settle` clears a stale answer right after it.

    Returns ``("restored", record, identity)`` with the restored record and the
    created file's identity, ``("lost", …)`` when another file took the name first,
    and ``("retry", …)`` on an ``OSError`` or ``RuntimeError``, which the next tick
    retries.
    """
    restored = {**record, "wait": new_wait_token()}
    try:
        # The SWCraft home may have been removed during the wait.
        reviews_dir()
        identity = write_exclusively(path, restored)
    except (OSError, RuntimeError):
        return "retry", record, None
    return ("restored", restored, identity) if identity is not None else ("lost", record, None)


def _settle(
    pending_path: Path, decision_path: Path, record: Mapping[str, Any], own: os.stat_result
) -> Literal["held", "lost", "unknown"]:
    """Right after a link: clear a foreign decision, only while the name is still *own*.

    *own* is the identity of the file this ask created, never a stat of the name.
    Another file at the name is ``"lost"``; a name gone or not stat-able is
    ``"unknown"``; in both cases nothing is touched, and the tick restores or
    judges the name, failing closed on a foreign decision.

    What this does not reach: a sweep and another wait's link between the stat
    and the clearing (a suspension of more than :data:`STALE_SECONDS`).

    Raises:
        OSError: the removal of a foreign decision failed.
    """
    try:
        current = os.stat(pending_path)
    except OSError:
        return "unknown"
    if not os.path.samestat(current, own):
        return "lost"
    _clear_a_foreign_decision(decision_path, record)
    return "held"


def _hold(
    pending_path: Path,
    decision_path: Path,
    record: dict[str, Any],
    own: os.stat_result,
) -> tuple[Literal["held", "retry", "lost"], dict[str, Any], os.stat_result]:
    """Check that the file at *pending_path* is still the one :func:`ask` linked.

    Returns the tick's state with the record and identity to carry on with:
    ``"held"`` (own file, or a removed record restored under a fresh token),
    ``"retry"`` (the name cannot be judged this tick), or ``"lost"`` (another file
    holds the name).
    """
    try:
        current = os.stat(pending_path)
    except FileNotFoundError:
        outcome, restored, identity = _restore(pending_path, record)
        if outcome != "restored" or identity is None:
            return ("lost" if outcome == "lost" else "retry"), record, own
        # A failed removal is swallowed: the tick's read abandons on the foreign decision.
        try:
            settled = _settle(pending_path, decision_path, restored, identity)
        except OSError:
            settled = "held"
        state: Literal["held", "retry", "lost"] = "retry" if settled == "unknown" else settled
        return state, restored, identity
    except OSError:
        return "retry", record, own  # an access error, a delete-pending name: the next tick retries
    if not os.path.samestat(current, own):
        return "lost", record, own
    return "held", record, own


def _heartbeat(
    pending_path: Path,
    now: tuple[float, float],
    last: tuple[float, float],
) -> tuple[float, float]:
    """Refresh the record's mtime when :data:`HEARTBEAT_SECONDS` passed on either clock.

    *now* and *last* are ``(monotonic, wall)`` pairs; the wall clock counts a jump in
    either direction. The marks move only when ``os.utime`` succeeds.
    """
    mono_now, wall_now = now
    last_mono, last_wall = last
    if mono_now - last_mono >= HEARTBEAT_SECONDS or abs(wall_now - last_wall) >= HEARTBEAT_SECONDS:
        try:
            os.utime(pending_path)
        except OSError:
            return last
        return now
    return last


def ask(
    question: str,
    *,
    kind: Literal["delta", "safety"],
    proposal: ChangeSetProposal,
    project_name: str,
    manifest: Path,
    notes: NoteSink,
    cancelled_exc: type[BaseException],
    client: str | None = None,
    warning_lines: tuple[int, int] | None = None,
    progress: Callable[[str], None] | None = None,
    session_closed: Callable[[], bool] | None = None,
    clock: Callable[[], float] | None = None,
    wall: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> bool:
    """Put one yes/no *question* to the human through the SWCraft panel and wait for the answer. Fails closed.

    Call it from the engine's **worker thread**: only there does ``check_cancelled()`` answer. Resolve
    *cancelled_exc* **on the event loop**: ``anyio.get_cancelled_exc_class()`` raises in a worker thread.

    The wait has **no deadline**: it ends on a decision, a cancelled call, *session_closed* answering true, or a
    lost name. *session_closed* is probed under ``except Exception``, which answers "not closed". Each tick it
    checks the call and the session, then that the pending name is still its own file (restoring a swept record),
    reads the decision, refreshes the heartbeat, and calls *progress* every :data:`PROGRESS_SECONDS`. Whichever
    way the wait ends, it withdraws its own files while the name is still its own.

    A :func:`cancellation_classes` class raised out of *progress* ends the wait as a cancelled call; any other
    ``Exception`` loses one notification. *clock*, *wall* and *sleep* default to ``_monotonic``, ``_wall`` and
    ``_sleep``, resolved at call time.

    Returns:
        ``True`` only for an ``approve`` bound to the change-set id, the question, this ask's own wait token and
        the digest of *question*; ``False`` for everything else, including a hand-off that failed.
    """
    clock = clock or _monotonic
    wall = wall or _wall
    sleep = sleep or _sleep
    cancellations = cancellation_classes(cancelled_exc)
    pending_path: Path | None = None
    decision_path: Path | None = None
    # The identity of the file this ask linked, never a stat of the name; `None` until the first link.
    own: os.stat_result | None = None
    try:
        try:
            if not CHANGE_SET_ID_PATTERN.match(proposal.change_set_id):
                # The id becomes a file name; one the engine did not allocate is never composed into a path.
                notes(handoff_note("invalid change-set id"))
                return False
            manifest_resolved = manifest.resolve()
            # The decision carries no secret, so the hand-off directory must lie outside the folder an agent's
            # file tools reach; a containment check that cannot stat raises OSError, a hand-off failure.
            if contains_the_reviews(manifest_resolved.parent, reviews_location().resolve()):
                notes(HOME_INSIDE_THE_PROJECT_NOTE)
                return False
            directory = reviews_dir()
            sweep(directory, wall())
            candidate_pending = directory / pending_name(proposal.change_set_id, kind)
            candidate_decision = directory / decision_name(proposal.change_set_id, kind)
            manifest_posix = manifest_resolved.as_posix()
            asked_at = wall()
            record = pending_record(
                question,
                kind=kind,
                wait=new_wait_token(),
                proposal=proposal,
                project_name=project_name,
                manifest=manifest_posix,
                now=asked_at,
                client=client,
                warning_lines=warning_lines,
            )
            linked = write_exclusively(candidate_pending, record)
            if linked is None:
                # Another live wait holds the name; nothing here touches its files.
                notes(NAME_TAKEN_NOTE)
                return False
            # Only now are the files composed for the cleanup, which removes them only while the name is `own`.
            pending_path, decision_path, own = candidate_pending, candidate_decision, linked
            # An older decision at this name answers nothing, and the sweep never removes one: the owner clears it.
            if _settle(pending_path, decision_path, record, own) == "lost":
                pending_path = decision_path = None
                notes(NAME_TAKEN_NOTE)
                return False
        except OSError as failed:
            notes(handoff_note(_errno_name(failed)))
            return False
        except (UnicodeError, RuntimeError) as failed:
            # A lone surrogate and an undeterminable home are no OSError and must not leave this function either.
            notes(handoff_note(type(failed).__name__))
            return False

        last_mono = last_progress = clock()
        last_wall = wall()
        while True:
            try:
                anyio.from_thread.check_cancelled()
            except cancelled_exc:
                notes(CANCELLED_NOTE)
                return False
            if _closed(session_closed):
                notes(SESSION_CLOSED_NOTE)
                return False

            state, record, own = _hold(pending_path, decision_path, record, own)
            if state == "lost":
                pending_path = decision_path = None
                notes(NAME_TAKEN_NOTE)
                return False

            mono_now, wall_now = clock(), wall()
            if state == "held":
                try:
                    answer = read_decision(decision_path, record)
                except DecisionMismatch:
                    notes(MISMATCH_NOTE)
                    return False
                except OSError:
                    answer = None  # a transient read failure; the next tick retries
                if answer is not None:
                    return answer

                last_mono, last_wall = _heartbeat(pending_path, (mono_now, wall_now), (last_mono, last_wall))

            # Progress goes to the client, never to a model.
            if progress is not None and mono_now - last_progress >= PROGRESS_SECONDS:
                last_progress = mono_now
                minutes = max(0, int((wall_now - asked_at) // 60))
                try:
                    progress(progress_text(kind, minutes))
                except cancellations:
                    notes(CANCELLED_NOTE)
                    return False
                except Exception:  # noqa: BLE001 -- protocol: the wait goes on without the notification
                    pass  # a lost progress notification, nothing more

            sleep(POLL_SECONDS)
    finally:
        # Withdraw only while the name is this ask's file. The decision goes first: once the pending record is
        # gone another server may take the name. Nothing raised here replaces the answer already decided.
        if pending_path is not None and decision_path is not None and own is not None:
            try:
                still_own = os.path.samestat(os.stat(pending_path), own)
            except OSError:
                still_own = False
            if still_own:
                for path in (decision_path, pending_path):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass


def live_pending_records(reviews: Path, now: float, manifest: str | None = None) -> list[dict[str, Any]]:
    """The pending records in *reviews* the review tab lists at *now* (wall-clock seconds), in file-name order.

    The panel's rule (``reviewCore.ts``): a ``*`` :data:`PENDING_SUFFIX` file not starting with ``.``, younger than
    :data:`STALE_SECONDS`, decoded as a JSON object; with *manifest* (resolved, POSIX-spelled), only a record whose
    ``project.manifest`` equals it. A file that cannot be read or decoded is not listed, and a directory that cannot
    be read lists nothing. What this does not reach: the extension's own
    canonicalisation of the manifest (``realPath``), and which VS Code window lists the record.
    """
    try:
        names = sorted(path.name for path in reviews.iterdir())
    except OSError:
        return []
    listed: list[dict[str, Any]] = []
    for name in names:
        if not name.endswith(PENDING_SUFFIX) or name.startswith("."):
            continue
        path = reviews / name
        try:
            fresh = now - path.stat().st_mtime < STALE_SECONDS
            record = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, ValueError):
            continue
        if not fresh or not isinstance(record, dict):
            continue
        project = record.get("project")
        if manifest is not None and not (isinstance(project, dict) and project.get("manifest") == manifest):
            continue
        listed.append(record)
    return listed
