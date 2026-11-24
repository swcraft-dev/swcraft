"""Append-only audit log for swcraft_tools write operations."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from swcraft_tools.project import is_absolute_anywhere, path_contains, resolved_path

#: What the log records in place of a location outside the project directory:
#: the log names no place outside the project, the user's home above all.
OUTSIDE_THE_PROJECT = "(outside the project)"

#: What a log opened without a project directory records in place of a
#: location; not a refusal, because the write it records has already happened.
NO_PROJECT_DIRECTORY = "(not recorded: the log was opened without a project directory)"


def project_location(project_root: str, location: str) -> str:
    """*location* as the log records it: relative to *project_root*, in POSIX spelling.

    Both sides are resolved (so a symlinked directory compares as its target);
    the project directory itself is ``"."``.

    :data:`OUTSIDE_THE_PROJECT` for a *location* that is not absolute **on this
    host** (another platform's spelling would resolve against the current
    directory), for one holding ``;`` (several joined paths), for one outside
    *project_root*, and for one whose relative name still carries the project's
    location.
    """
    if not Path(location).is_absolute() or ";" in location:
        return OUTSIDE_THE_PROJECT
    root = resolved_path(Path(project_root))
    resolved = resolved_path(Path(location))
    if not path_contains(root, resolved):
        return OUTSIDE_THE_PROJECT
    relative = Path(resolved).relative_to(root).as_posix()
    if root in relative or Path(root).as_posix() in relative or is_absolute_anywhere(relative):
        return OUTSIDE_THE_PROJECT
    return relative


#: One lock per log file, shared by every :class:`AuditLog` naming it in this
#: process. On Windows the C runtime emulates ``O_APPEND`` as a seek and a write,
#: so two threads appending at once can tear a line. It does not reach another
#: *process* appending to the same file: that is still unserialized on Windows.
_APPEND_LOCKS: dict[str, threading.Lock] = {}
_APPEND_LOCKS_GUARD = threading.Lock()


def _append_lock(path: Path) -> threading.Lock:
    key = path.resolve().as_posix()
    with _APPEND_LOCKS_GUARD:
        return _APPEND_LOCKS.setdefault(key, threading.Lock())


def _write_line(path: Path, line: str) -> None:
    # `newline="\n"`: one log file appended to from several hosts must not mix line ends.
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)


class AuditLog:
    """Append-only JSONL audit log.

    Each entry is a JSON object on its own line::

        {"timestamp": "2026-06-27T12:00:00+00:00", "operation": "add_port",
         "args": {...}, "result": "..."}

    Canonical location: ``dist/audit.jsonl``.

    **A recorded location is project-relative.** An absolute ``result`` is
    recorded as the name relative to *project_root* in POSIX spelling
    (``model/swc/Foo.yaml``), or :data:`OUTSIDE_THE_PROJECT`; through a log
    constructed without *project_root* it is recorded as
    :data:`NO_PROJECT_DIRECTORY`. What this does not reach: ``args`` are recorded
    as given, and so is a ``result`` that is not one absolute path.

    An **optional** fifth key, ``scope_id``, correlates an entry with the scoped
    session that produced it; it is emitted only when supplied, so an entry
    without it carries exactly the four keys, in order.
    """

    #: The four keys every entry carries, in emission order.
    ENTRY_KEYS = ("timestamp", "operation", "args", "result")

    def __init__(self, log_path: str, *, project_root: str | None = None) -> None:
        """Open the audit log at *log_path*.

        Args:
            log_path: the JSONL file.
            project_root: the project directory every recorded location is made
                relative to. ``None`` only for a log opened to be read, or written
                with no location in ``result``.
        """
        self._path = Path(log_path)
        self._project_root = None if project_root is None else resolved_path(Path(project_root))

    def log_write(
        self,
        operation: str,
        args: dict[str, Any],
        result: str,
        scope_id: str | None = None,
    ) -> None:
        """Append one entry to the log, creating parent directories as needed.

        Args:
            scope_id: correlation id of the scoped session this write belongs
                to. Emitted as a fifth key only when not ``None``.
        """
        entry = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "operation": operation,
            "args": args,
            "result": result,
        }
        self._append(entry, scope_id)

    def replay_entry(self, entry: Any, scope_id: str | None = None) -> None:
        """Re-append an already-recorded entry, keeping its original timestamp.

        A scoped session's shadow log lands in the project's log this way at
        commit, with the session's ``scope_id`` added. *entry*'s shape is checked,
        not assumed: it comes from a file this process does not own.

        Raises:
            ValueError: *entry* is not a JSON object, or is missing one of
                :data:`ENTRY_KEYS` (never defaulted).
        """
        if not isinstance(entry, Mapping):
            raise ValueError(f"replay_entry: an audit entry is an object, not {type(entry).__name__}")
        missing = [key for key in self.ENTRY_KEYS if key not in entry]
        if missing:
            raise ValueError(f"replay_entry: audit entry is missing {', '.join(missing)}")
        self._append({key: entry[key] for key in self.ENTRY_KEYS}, scope_id)

    def _append(self, entry: dict[str, Any], scope_id: str | None) -> None:
        result = entry["result"]
        if isinstance(result, str) and is_absolute_anywhere(result):
            entry["result"] = (
                NO_PROJECT_DIRECTORY if self._project_root is None else project_location(self._project_root, result)
            )
        if scope_id is not None:
            entry["scope_id"] = scope_id
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry) + "\n"
        with _append_lock(self._path):
            _write_line(self._path, line)

    def get_entries(self) -> list[Any]:
        """Return all logged entries in insertion order, **as they parsed**.

        A line may be a bare ``null``, a number or an array; a non-object line is
        returned rather than dropped, so a corrupted log never looks complete.
        Every caller checks the shape itself.

        Raises:
            json.JSONDecodeError: a line is not valid JSON.
        """
        if not self._path.exists():
            return []
        entries: list[Any] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
        return entries
