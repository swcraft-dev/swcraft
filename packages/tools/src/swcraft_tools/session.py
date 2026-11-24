"""ScopedSession — an isolated shadow copy of a project, plus the framed model fingerprint.

A scoped session materializes a ``tempfile``-backed project (``<tmp>/swcraft.yaml`` +
``<tmp>/model/`` + ``<tmp>/dist/`` + ``<tmp>/views/``) holding a copy of the real model, so every
write, every generated artifact and the session's own ``dist/audit.jsonl`` land inside the shadow.
The editing protocol stages every change set through it (``swcraft_tools.editing.staging`` wraps
this class); :class:`CommitConflictError` is what the protocol publishes as ``stale-baseline``.

:meth:`ScopedSession.open` fingerprints the real model *first* and reads the baseline from the copy,
so any concurrent change to the real model, one landing mid-copy included, makes
:meth:`ScopedSession.commit` refuse. There is no lock: a write during the copy can be captured
partially in the shadow.

**Races.** Two in-process commits against one real model are serialized by :func:`commit_lock`, not
held across the review. An external writer (another process, an editor) is neither detected nor
prevented between the final fingerprint read and ``os.replace()``.

**Crashes.** Before the apply the real model is byte-identical; a crash during a multi-file apply can
leave a partially applied model, with no journal or recovery.

**A commit applies exactly what the reviewed diff accounts for.** Writers re-serialize the whole
file and the schema drops unknown keys, so a byte difference need not be a semantic one:
:meth:`ScopedSession.commit` refuses the whole commit (:class:`UnexplainedChangeError`, before any
file is touched) when a byte-differing file is not backed by an element the diff reports. The unit
is the **file**: an accounted file is applied whole by one ``os.replace``, and a whole write in the
shadow drops its comments and unknown keys.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from swcraft_tools.auditor import OUTSIDE_THE_PROJECT, AuditLog, project_location
from swcraft_tools.differ import ModelDiff, diff_snapshots
from swcraft_tools.model_registry import LayerFacts, ModelRegistry, current_registry, longest_directory_match
from swcraft_tools.project import SwcraftProject, require_project
from swcraft_tools.rendering import path_free
from swcraft_tools.snapshot import ModelSnapshot, load_snapshot
from swcraft_tools.yaml_load import load_yaml_document

#: Environment variables that make the MCP server's ``build()``/``validate()`` shell out to a project
#: command that knows nothing about the shadow; a session's subprocess environment neutralizes both.
ESCAPE_ENV_VARS = ("SWCRAFT_MCP_BUILD_COMMAND", "SWCRAFT_MCP_VALIDATE_COMMAND")

#: What :func:`session_env` sets each :data:`ESCAPE_ENV_VARS` entry to.
#:
#: An empty string, not a deletion: the spawned server's ``load_dotenv(override=False)`` fills only
#: keys that are absent, so a deleted key can be repopulated from the project's own ``.env``. A key
#: present and empty is left alone by ``load_dotenv`` and read as unset by the server.
NEUTRALIZED_ESCAPE_VALUE = ""

_MANIFEST_FILENAME = "swcraft.yaml"
_SHADOW_MODEL_DIRNAME = "model"
_SHADOW_DIST_DIRNAME = "dist"
_SHADOW_VIEWS_DIRNAME = "views"

_AUDIT_FILENAME = "audit.jsonl"

_TEMP_PREFIX = "swcraft-session-"

#: A shadow writer entry's ``result`` is the written file's name relative to the shadow root
#: (``model/swc/Foo.yaml``), because the shadow log's project directory is the shadow root
#: (:attr:`ScopedSession.shadow_audit_log`). A result in any other form is refused before the apply
#: (:class:`AuditReplayError`).
_SHADOW_MODEL_LOCATION = PurePosixPath(_SHADOW_MODEL_DIRNAME)

#: The retired separator of several paths in one result, refused by name: joined
#: absolute paths would otherwise be read as one path whose later segments still
#: carry the shadow's location.
_RETIRED_SEPARATOR = "; "

#: Suffix of the sibling file a write is staged at before its atomic ``os.replace``, always combined
#: with a random component (:func:`_staging_path`): a fixed ``<target>.tmp`` is a legal filename under
#: ``model_root``, copied byte-identically into the shadow, and staging over it would destroy it
#: unreviewed.
_STAGING_SUFFIX = ".swcraft-commit.tmp"


class CommitConflictError(RuntimeError):
    """The real model changed after the session took its baseline fingerprint.

    Raised by :meth:`ScopedSession.commit` **before** anything is applied. "Changed" is
    :func:`model_fingerprint`'s answer, which covers **every** file under the model root.
    """


class UnexplainedChangeError(RuntimeError):
    """A file the apply would touch is not accounted for by the reviewed diff.

    Raised by :meth:`ScopedSession.commit` **before** anything is applied. The *whole* commit is
    refused rather than the accounted-for subset applied: the surplus file is evidence of a change the
    user was not shown. The unit is the file, not the byte: a file whose element the diff reports is
    applied whole.
    """


class CommitPreflightError(RuntimeError):
    """A commit could not be attempted at all; nothing was applied.

    Raised when a step before the first ``os.replace()`` cannot read its inputs (a truncated shadow
    log, a model root that vanished), so the caller can tell it from a mid-apply ``OSError``, which
    may leave the model partially applied.
    """


class AuditReplayError(RuntimeError):
    """A shadow audit entry cannot be rebased onto the real model root.

    Raised by :meth:`ScopedSession.commit` **before** anything is applied: the whole shadow log is
    validated up front, so the real model and the real log are left untouched.
    """


class CommitLogError(RuntimeError):
    """The model was applied in full; appending the rebased entries then failed.

    The audit append is the one step after the apply, so an ``OSError`` there is not a partial apply:
    every approved file is on disk and only the audit trail is missing. A class of its own so the user
    is not sent looking for damage or redoing a change that landed. :attr:`applied` carries what the
    apply wrote.
    """

    def __init__(self, message: str, applied: list[str]) -> None:
        super().__init__(message)
        #: The real-model paths the (successful) apply wrote or removed.
        self.applied = applied


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


def _iter_model_files(model_root: Path) -> Iterator[Path]:
    """Every regular file under *model_root*, in no particular order.

    A symlinked directory is not descended into, while the copy materializes its contents in the
    shadow, so every commit in a project containing one is refused as :class:`UnexplainedChangeError`.
    That is the fail-closed choice: following the link would let the fingerprint and the apply reach
    outside ``model_root``, and preserving it in the copy would let a shadow writer write through it
    into the real tree.
    """
    for path in model_root.rglob("*"):
        if path.is_file():
            yield path


def model_fingerprint(model_root: str) -> str:
    """Return a **framed** sha256 over the whole model tree.

    The digest covers sorted ``(relative_path, byte_length, sha256(content))`` records, each
    length-framed, so file *names* and file *boundaries* participate: a rename with unchanged content
    changes the fingerprint, and ``['ab', 'c']`` / ``['a', 'bc']`` do not collide.

    **Every** regular file under *model_root* counts, not only layer YAML, because the commit writes
    and unlinks every file whatever its name: a stray ``.DS_Store`` or editor swap file dropped under
    ``model/`` after ``open()`` refuses the commit rather than being deleted by it. Computed fresh on
    every call, never cached.

    Raises:
        FileNotFoundError: *model_root* does not exist or is not a directory; never a hash over
            nothing.
    """
    root = Path(model_root)
    if not root.is_dir():
        raise FileNotFoundError("model_fingerprint: the model root was not found or is not a directory")

    records: list[tuple[str, int, bytes]] = []
    for path in _iter_model_files(root):
        content = path.read_bytes()
        records.append((path.relative_to(root).as_posix(), len(content), hashlib.sha256(content).digest()))
    records.sort(key=lambda record: record[0])

    digest = hashlib.sha256()
    for relative_path, length, content_digest in records:
        path_bytes = relative_path.encode("utf-8")
        digest.update(f"{len(path_bytes)}:".encode("ascii"))
        digest.update(path_bytes)
        digest.update(f"{length}:".encode("ascii"))
        digest.update(content_digest)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Subprocess environment
# ---------------------------------------------------------------------------


def session_env(shadow_manifest: str, base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the environment an MCP subprocess for a session must run with.

    Points ``SWCRAFT_MANIFEST`` at the shadow manifest and sets both :data:`ESCAPE_ENV_VARS` to
    :data:`NEUTRALIZED_ESCAPE_VALUE`. *base_env* defaults to ``os.environ`` and is never mutated. Kept
    for a caller driving :class:`ScopedSession` from Python; nothing in this repository calls it.
    """
    env = dict(os.environ if base_env is None else base_env)
    env["SWCRAFT_MANIFEST"] = str(shadow_manifest)
    for name in ESCAPE_ENV_VARS:
        env[name] = NEUTRALIZED_ESCAPE_VALUE
    return env


def pinned_escape_env(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """*base_env* with both :data:`ESCAPE_ENV_VARS` guaranteed **present**.

    For a server spawned against the real project, scoped or not. The host decides whether a
    ``build``/``validate`` would shell out from the environment it spawned the server with, and the
    server's ``load_dotenv(override=False)`` sets any *absent* key, so a project ``.env`` could hand
    the child an override the host never saw. A variable the host has keeps its value; one it lacks is
    passed present-and-empty, which ``load_dotenv`` leaves alone and the server reads as unset.

    *base_env* defaults to ``os.environ`` and is never mutated.
    """
    env = dict(os.environ if base_env is None else base_env)
    for name in ESCAPE_ENV_VARS:
        env.setdefault(name, NEUTRALIZED_ESCAPE_VALUE)
    return env


# ---------------------------------------------------------------------------
# The per-model commit lock
# ---------------------------------------------------------------------------

#: One reentrant lock per real model root, created on first use and keyed by the **resolved** path,
#: so two spellings of one project cannot each get a lock of their own and serialize nothing.
#:
#: No eviction, deliberately: a weak mapping, or evicting an unlocked entry, would let a caller build
#: a second lock for the same root between lookup and acquisition; the key set is bounded by the
#: process (one manifest per server or CLI run, and no request names a project); and an entry costs a
#: path string and an ``RLock``.
_COMMIT_LOCKS: dict[str, threading.RLock] = {}

#: Guards :data:`_COMMIT_LOCKS` itself. Held only for the dictionary lookup, so
#: it is never held while a commit runs.
_COMMIT_LOCKS_GUARD = threading.Lock()


def commit_lock(real_model_root: str | Path) -> threading.RLock:
    """The lock that serializes commits against *real_model_root* in this process.

    Reentrant: :meth:`ScopedSession.commit` takes it around its own body, and the editing engine takes
    the same lock over a wider span (from the fingerprint check through its ``change_set_committed``
    record), so no **writer** entry of another change set lands inside that block. It does not
    serialize the lifecycle or host records around a commit; those are excluded from the block by
    :func:`~swcraft_tools.editing.audit.commit_block`. It bounds this process only, never an external
    writer.

    Args:
        real_model_root: the **real** project's model root, never a shadow's; resolved here.

    Returns:
        A reentrant lock, usable as a context manager; the same object for every caller naming the
        same model root.
    """
    key = str(Path(real_model_root).resolve())
    with _COMMIT_LOCKS_GUARD:
        lock = _COMMIT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _COMMIT_LOCKS[key] = lock
        return lock


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


def _copy_model_tree(source: str, destination: str) -> None:
    """Copy the real model tree into the shadow."""
    shutil.copytree(source, destination)


class ScopedSession:
    """A shadow project a turn's mutations are confined to until committed."""

    def __init__(self, manifest_path: str, scope_id: str | None = None) -> None:
        self._manifest_path = manifest_path
        self._scope_id = scope_id or uuid.uuid4().hex
        self._project: SwcraftProject | None = None
        self._shadow_root: Path | None = None
        self._baseline_fingerprint: str | None = None
        self._baseline_snapshot: ModelSnapshot | None = None

    # -- state ---------------------------------------------------------

    @property
    def scope_id(self) -> str:
        """Correlation id every audit record this session produces carries."""
        return self._scope_id

    @property
    def is_open(self) -> bool:
        return self._shadow_root is not None

    def _require_open(self) -> Path:
        if self._shadow_root is None:
            raise RuntimeError("ScopedSession: the session is not open")
        return self._shadow_root

    @property
    def project(self) -> SwcraftProject:
        """The real project this session shadows (available after ``open()``)."""
        if self._project is None:
            raise RuntimeError("ScopedSession: the session is not open")
        return self._project

    @property
    def real_model_root(self) -> str:
        return self.project.resolved_model_root

    @property
    def shadow_root(self) -> str:
        return str(self._require_open())

    @property
    def shadow_manifest(self) -> str:
        return str(self._require_open() / _MANIFEST_FILENAME)

    @property
    def shadow_model_root(self) -> str:
        return str(self._require_open() / _SHADOW_MODEL_DIRNAME)

    @property
    def shadow_audit_log(self) -> AuditLog:
        """The log the session's own writers record into, its locations relative to the shadow root."""
        shadow_root = self._require_open()
        return AuditLog(str(shadow_root / _SHADOW_DIST_DIRNAME / _AUDIT_FILENAME), project_root=str(shadow_root))

    @property
    def real_audit_log(self) -> AuditLog:
        """The real project's log — host records land here even on abandon."""
        return AuditLog(
            str(Path(self.project.resolved_dist_root) / _AUDIT_FILENAME),
            project_root=self.project.resolved_project_root,
        )

    @property
    def baseline_fingerprint(self) -> str:
        """The real model's fingerprint as of ``open()`` — kept after ``abandon()``."""
        if self._baseline_fingerprint is None:
            raise RuntimeError("ScopedSession: the session was never opened")
        return self._baseline_fingerprint

    @property
    def baseline_snapshot(self) -> ModelSnapshot:
        if self._baseline_snapshot is None:
            raise RuntimeError("ScopedSession: the session was never opened")
        return self._baseline_snapshot

    # -- lifecycle -----------------------------------------------------

    def open(self) -> None:
        """Fingerprint the real model, copy it into a temp project, snapshot the copy, in that order."""
        if self._shadow_root is not None:
            raise RuntimeError("ScopedSession.open: the session is already open")

        project = require_project(self._manifest_path)
        real_model_root = project.resolved_model_root

        # 1. The real model's fingerprint, FIRST.
        baseline_fingerprint = model_fingerprint(real_model_root)

        # .resolve(): on macOS the temp root reaches through a /var -> /private/var
        # symlink, and require_project() reports the resolved form. The two must
        # compare equal, or a host cannot tell a shadow path from a real one.
        shadow_root = Path(tempfile.mkdtemp(prefix=_TEMP_PREFIX)).resolve()
        try:
            # 2. The copy.
            shadow_model_root = shadow_root / _SHADOW_MODEL_DIRNAME
            _copy_model_tree(real_model_root, str(shadow_model_root))
            (shadow_root / _SHADOW_DIST_DIRNAME).mkdir(exist_ok=True)
            (shadow_root / _SHADOW_VIEWS_DIRNAME).mkdir(exist_ok=True)
            (shadow_root / _MANIFEST_FILENAME).write_text(
                yaml.safe_dump(
                    {
                        "name": project.name,
                        "schema_version": project.schema_version,
                        "model_root": _SHADOW_MODEL_DIRNAME,
                        "dist_root": _SHADOW_DIST_DIRNAME,
                        "views_root": _SHADOW_VIEWS_DIRNAME,
                    },
                    default_flow_style=False,
                    sort_keys=False,
                ),
                # `newline="\n"`: the shadow manifest stands in for the real
                # one, and the model tree beside it is fingerprinted and
                # byte-compared against the real tree when the session commits.
                encoding="utf-8",
                newline="\n",
            )
            # 3. The baseline snapshot, read from the COPY.
            baseline_snapshot = load_snapshot(str(shadow_model_root))
        except BaseException:
            shutil.rmtree(shadow_root, ignore_errors=True)
            raise

        self._project = project
        self._shadow_root = shadow_root
        self._baseline_fingerprint = baseline_fingerprint
        self._baseline_snapshot = baseline_snapshot

    def abandon(self) -> None:
        """Drop the temp tree. Idempotent; the real model is left byte-identical."""
        if self._shadow_root is not None:
            shutil.rmtree(self._shadow_root, ignore_errors=True)
        self._shadow_root = None

    def diff(self) -> ModelDiff:
        """The semantic diff between this session's baseline and the shadow *now*.

        This is what the review gates and the user see before committing. The caller closes the
        session's MCP client first, so nothing writes into the shadow while this runs.
        """
        return diff_snapshots(self.baseline_snapshot, load_snapshot(self.shadow_model_root))

    def commit(self, diff: ModelDiff, *, before_apply: Callable[[], None] | None = None) -> list[str]:
        """Apply the shadow model onto the real model, refusing on a changed baseline.

        Serialized per real model root by :func:`commit_lock`, never across the review. In order:
        re-fingerprint the real model; rebase every shadow audit entry; refuse the per-file delta whole
        if a file is not backed by an element *diff* reports (the shadow re-read); call *before_apply*;
        apply the delta and append the rebased entries with this session's ``scope_id``.

        Args:
            diff: the reviewed diff; the files written are exactly those it accounts for.
            before_apply: called after every pre-apply refusal and immediately before the first
                ``os.replace()``; anything it raises propagates pre-apply, the model byte-identical.

        Returns:
            The real-model paths that were written or removed.

        Raises:
            CommitConflictError: the real model's fingerprint no longer matches the baseline taken at
                ``open()``, a commit that waited for another included. Nothing has been applied.
            AuditReplayError: a shadow audit entry is missing one of
                :data:`~swcraft_tools.auditor.AuditLog.ENTRY_KEYS`, or its ``result`` is not one
                project-relative path inside the shadow's model directory. Nothing has been applied.
            UnexplainedChangeError: a file whose bytes differ between the shadow and the real model
                is not accounted for by *diff*. Nothing has been applied.
            CommitPreflightError: a preflight step could not read what it needed (a truncated shadow
                log, a model root that vanished). Nothing has been applied.
            OSError: the apply itself failed. Some files may already have been written -- this is
                the one failure that is not pre-apply.
            CommitLogError: the apply succeeded and appending the rebased entries to the real audit
                log failed. The model is committed in full; only the audit trail is incomplete.
        """
        shadow_root = self._require_open()
        real_model_root = Path(self.real_model_root)
        shadow_model_root = shadow_root / _SHADOW_MODEL_DIRNAME

        # Taken here, not only by the caller, so the guarantee belongs to the session.
        with commit_lock(real_model_root):
            try:
                if model_fingerprint(str(real_model_root)) != self.baseline_fingerprint:
                    raise CommitConflictError(
                        "the real model changed after this session's baseline was taken; nothing was committed "
                        "(every file under the model root counts, including ones no model layer reads)"
                    )

                replayable = _rebase_audit_entries(
                    self.shadow_audit_log.get_entries(),
                    _real_model_location(self.project),
                )

                shadow_files = _index_model_files(shadow_model_root)
                real_files = _index_model_files(real_model_root)
                delta = _delta_paths(shadow_files, real_files)
                _require_explained(delta, shadow_files, real_files, diff)
            except (OSError, ValueError, yaml.YAMLError) as exc:
                # The three refusals are RuntimeErrors and pass through: they are this preflight's
                # answers, not failures to read its inputs.
                raise CommitPreflightError(
                    f"the commit could not be prepared ({path_free(exc, real_model_root, shadow_model_root)}); "
                    "nothing was applied"
                ) from exc

            if before_apply is not None:
                before_apply()

            applied = _apply_model_delta(delta, shadow_files, real_model_root)

            real_log = self.real_audit_log
            try:
                for entry in replayable:
                    real_log.replay_entry(entry, scope_id=self._scope_id)
            except OSError as exc:
                # The apply succeeded: a bare OSError would read as a partial apply.
                raise CommitLogError(
                    f"the model was committed in full, but the audit log could not be updated ({path_free(exc)})",
                    applied,
                ) from exc

            return applied

    def session_env(self, base_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """:func:`session_env` bound to this session's shadow manifest."""
        return session_env(self.shadow_manifest, base_env)


def _real_model_location(project: SwcraftProject) -> PurePosixPath:
    """The real model root's name relative to the project directory (``model``, ``arch/model``).

    The manifest keeps ``model_root`` inside the project; :class:`AuditReplayError` is the fail-closed
    answer if it is not, raised before anything is applied.
    """
    location = project_location(project.resolved_project_root, project.resolved_model_root)
    if location == OUTSIDE_THE_PROJECT:
        raise AuditReplayError("the real model root is outside the project directory; nothing was applied")
    return PurePosixPath(location)


def _rebase_result(result: Any, real_model_location: PurePosixPath) -> str:
    """Rewrite a recorded ``result`` so it names the **real** project's file.

    Only the leading ``model`` component changes; ``args`` are inputs and are left as recorded.

    Raises:
        AuditReplayError: *result* is not a string, or not one relative POSIX path below the
            shadow's model directory (an absolute path, a ``..`` component, a backslash, the retired
            ``"; "`` separator). The offending value is not quoted: the message reaches a host's
            log, and a path inside the private staging copy must not be written there.
    """
    if not isinstance(result, str):
        raise AuditReplayError(f"shadow audit entry has a non-string result: {type(result).__name__}")
    recorded = PurePosixPath(result)
    # `relative_to` refuses an absolute spelling of either platform too: neither
    # starts with the relative component `model`. A backslash is refused outright:
    # every writer's name is `as_posix()`, and `model/..\..\x` is one POSIX
    # component here and a climb out of the project on Windows.
    try:
        if ".." in recorded.parts or "\\" in result or _RETIRED_SEPARATOR in result:
            raise ValueError(result)
        relative = recorded.relative_to(_SHADOW_MODEL_LOCATION)
    except ValueError:
        raise AuditReplayError(
            "shadow audit entry records a result outside the session's model root; nothing was applied"
        ) from None
    return (real_model_location / relative).as_posix()


def _rebase_audit_entries(
    entries: list[Any],
    real_model_location: PurePosixPath,
) -> list[dict[str, Any]]:
    """Validate and rebase the whole shadow log **before** any file is applied.

    Makes every check :meth:`AuditLog.replay_entry` makes, because that runs after the apply: a
    malformed entry is refused pre-apply instead of failing after the model was written.
    """
    return [_rebase_entry(entry, real_model_location) for entry in entries]


def _rebase_entry(entry: Any, real_model_location: PurePosixPath) -> dict[str, Any]:
    """One shadow entry, validated and rebased — or :class:`AuditReplayError`.

    A JSONL line may be ``null``, a number, an array or a string, so the mapping check comes first:
    ``key not in entry`` over those raises ``TypeError`` or answers by substring, and a ``TypeError``
    would escape the commit's pre-apply refusal.
    """
    if not isinstance(entry, Mapping):
        raise AuditReplayError(f"shadow audit entry is not an object: {type(entry).__name__}")
    missing = [key for key in AuditLog.ENTRY_KEYS if key not in entry]
    if missing:
        raise AuditReplayError(f"shadow audit entry is missing {', '.join(missing)}")
    return {**entry, "result": _rebase_result(entry["result"], real_model_location)}


def _index_model_files(model_root: Path) -> dict[str, Path]:
    """Relative posix path -> file, for every regular file under *model_root*."""
    return {path.relative_to(model_root).as_posix(): path for path in _iter_model_files(model_root)}


def _delta_paths(shadow_files: Mapping[str, Path], real_files: Mapping[str, Path]) -> list[str]:
    """The relative paths the apply would write or unlink, writes first.

    A file present on both sides with identical bytes is not in the delta — the
    session copied the whole tree, so an untouched file is byte-identical and
    rewriting it would only churn mtimes.
    """
    writes = [
        relative_path
        for relative_path, source in sorted(shadow_files.items())
        if relative_path not in real_files or real_files[relative_path].read_bytes() != source.read_bytes()
    ]
    removals = sorted(relative_path for relative_path in real_files if relative_path not in shadow_files)
    return [*writes, *removals]


def _declared_identity(path: Path, identity_field: str) -> str | None:
    """The identity the model file at *path* declares, or ``None`` if it declares none.

    A plain document parse: a file too malformed to answer is unexplained anyway, which is the
    fail-closed direction.
    """
    try:
        data = load_yaml_document(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    identity = data.get(identity_field)
    return identity if isinstance(identity, str) else None


def _touched_key(facts: LayerFacts) -> str:
    """The key a layer's identities are filed under: the adapter's name (``swcs``), else the contract's."""
    return facts.snapshot_attribute or facts.name


def _touched_identities(diff: ModelDiff, *, registry: ModelRegistry | None = None) -> dict[str, set[str]]:
    """layer -> every element identity *diff* reports as added, removed or changed.

    Reads :attr:`~swcraft_tools.differ.ModelDiff.by_layer`, so a layer the adapter has no field for
    is still accounted for; a layer the diff does not carry contributes an empty set, which explains
    no file.

    Args:
        diff: the reviewed diff.
        registry: the kernel registry to enumerate layers against; ``None`` resolves the current one
            at call time.
    """
    registry = registry or current_registry()
    by_layer = diff.by_layer
    touched: dict[str, set[str]] = {}
    for name in registry.layer_names:
        facts = registry.layer(name)
        if name not in by_layer:
            touched[_touched_key(facts)] = set()
            continue
        layer_diff = by_layer[name]
        identity_attr = facts.identity_field
        identities = {getattr(item, identity_attr) for item in (*layer_diff.added, *layer_diff.removed)}
        for changed in layer_diff.changed:
            # A layer's changed element is keyed by its identity string; a tuple
            # key belongs to a collection item and never reaches this level.
            if not isinstance(changed.key, str):
                raise TypeError(f"layer '{name}': a changed element is keyed {changed.key!r}, not by its identity")
            identities.add(changed.key)
        touched[_touched_key(facts)] = identities
    return touched


def _layer_of_directory(relative_path: str, registry: ModelRegistry) -> tuple[LayerFacts, str] | None:
    """``(the layer, the remainder)`` for the file at *relative_path*, or ``None``.

    The layer is the one whose directory is the **longest** declared directory prefixing the path
    (:func:`~swcraft_tools.model_registry.longest_directory_match`), so ``data_types/base/X.yaml``
    belongs to the layer stored in ``data_types/base``. ``None`` for a path no layer's directory
    prefixes, or one with nothing below the directory.
    """
    by_directory = {facts.storage.directory: facts for facts in registry.layers}
    match = longest_directory_match(by_directory, relative_path)
    if match is None:
        return None
    directory, remainder = match
    return by_directory[directory], remainder


def _is_explained(
    relative_path: str,
    source: Path | None,
    touched: Mapping[str, set[str]],
    *,
    registry: ModelRegistry | None = None,
) -> bool:
    """Whether *diff* (as ``touched``) accounts for a change to this file.

    The path must be one the layer's storage would glob: the layer's file suffix, directly inside the
    layer's directory unless the layer nests (``swc/{group}/{name}.yaml``). A file no layer reads (an
    ``interfaces/sub/ISpeed.yaml``, a wrong extension) is never explained, even when the name it
    declares is in the diff: that would explain a file on the strength of another file's element.

    Args:
        relative_path: the model-root-relative POSIX path of the file.
        source: the file to read the declared identity from, or ``None``.
        touched: :func:`_touched_identities`' answer, keyed the same way.
        registry: the kernel registry the directory is resolved against; ``None`` resolves the
            current one.
    """
    if source is None:
        return False
    registry = registry or current_registry()
    located = _layer_of_directory(relative_path, registry)
    if located is None:
        return False
    facts, remainder = located
    if not relative_path.endswith(facts.storage.file_suffix):
        return False
    if "/" in remainder and not facts.storage.is_recursive:
        return False
    identity = _declared_identity(source, facts.identity_field)
    return identity is not None and identity in touched.get(_touched_key(facts), frozenset())


def _require_explained(
    delta: list[str],
    shadow_files: Mapping[str, Path],
    real_files: Mapping[str, Path],
    diff: ModelDiff,
) -> None:
    """Refuse the commit unless every file in *delta* is backed by an element *diff* reports.

    The shadow side answers "which element is this file about" whenever it has the file, so an
    in-place rename resolves through the name the commit would *write*; a removal falls back to the
    real side. The registry is resolved once, so both halves agree on the layer vocabulary.

    Raises:
        UnexplainedChangeError: at least one file is not accounted for.
    """
    registry = current_registry()
    touched = _touched_identities(diff, registry=registry)
    unexplained = [
        relative_path
        for relative_path in delta
        if not _is_explained(
            relative_path,
            shadow_files.get(relative_path) or real_files.get(relative_path),
            touched,
            registry=registry,
        )
    ]
    if not unexplained:
        return
    raise UnexplainedChangeError(
        f"{len(unexplained)} file(s) differ from the real model without appearing in the semantic diff that was "
        f"reviewed, so committing them would apply a change nobody saw: {', '.join(unexplained)}. "
        "Nothing was applied."
    )


def _names_the_same_file(write_target: Path, removal_target: Path) -> bool:
    """Whether writing *write_target* would land on the very file *removal_target* names.

    True only on a case-insensitive filesystem for a case-only rename; a write that overwrites an
    existing file is not the same inode as any removal.
    """
    try:
        return write_target.exists() and os.path.samefile(write_target, removal_target)
    except OSError:
        return False


def _case_collisions(delta: list[str], shadow_files: Mapping[str, Path], real_model_root: Path) -> dict[str, str]:
    """write path -> the removal path that names the same real file, if any.

    Computed **before** the first write, because every probe it makes reads
    filesystem state the apply is about to change.
    """
    writes = [relative_path for relative_path in delta if relative_path in shadow_files]
    removals = [relative_path for relative_path in delta if relative_path not in shadow_files]
    collisions: dict[str, str] = {}
    claimed: set[str] = set()
    for write in writes:
        write_target = real_model_root / write
        for removal in removals:
            if removal in claimed:
                continue
            if _names_the_same_file(write_target, real_model_root / removal):
                collisions[write] = removal
                claimed.add(removal)
                break
    return collisions


def _staging_path(target: Path) -> Path:
    """A sibling path to stage *target*'s new bytes at, naming nothing that exists.

    A sibling because ``os.replace`` is atomic only within one filesystem; unique per write because
    ``<target>.tmp`` can be a real model file. ``lexists``, so a dangling symlink counts as taken.
    """
    while True:
        candidate = target.with_name(f"{target.name}.{uuid.uuid4().hex}{_STAGING_SUFFIX}")
        if not os.path.lexists(candidate):
            return candidate


def _apply_model_delta(delta: list[str], shadow_files: Mapping[str, Path], real_model_root: Path) -> list[str]:
    """Write the shadow's files over the real model and unlink the ones it dropped.

    **Writes come before removals**, so no intermediate state has less content than both states it
    connects: after a crash, what a removal would have dropped is the previous committed state, while
    new content lost before its write would exist nowhere once the shadow is dropped.

    A **case-only rename** on a case-insensitive filesystem is a write and a removal naming the same
    file, so such a pair is resolved together: the new content is staged to its sibling
    (:func:`_staging_path`) first, then the old entry is unlinked, then ``os.replace`` lands it. The
    content is on disk before anything is removed, and the file takes the new casing.
    """
    collisions = _case_collisions(delta, shadow_files, real_model_root)
    resolved_with_a_write = set(collisions.values())

    applied: list[str] = []
    for relative_path in delta:
        target = real_model_root / relative_path
        source = shadow_files.get(relative_path)
        if source is None:
            if relative_path in resolved_with_a_write:
                continue  # already unlinked as part of its colliding write
            target.unlink()
            applied.append(str(target))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = _staging_path(target)
        staged.write_bytes(source.read_bytes())
        collided = collisions.get(relative_path)
        if collided is not None:
            collided_target = real_model_root / collided
            collided_target.unlink()
            applied.append(str(collided_target))
        os.replace(staged, target)
        applied.append(str(target))
    return applied
