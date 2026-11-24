"""Tests for session.py — framed model fingerprint + shadow session substrate.

The fingerprint is the authorization input the commit gate compares against, so its
framing (a rename with unchanged content must change it) is tested directly rather
than through the session.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

import pytest
import yaml
from swcraft_tools import session as session_mod
from swcraft_tools.auditor import AuditLog
from swcraft_tools.differ import LayerDiff, ModelDiff
from swcraft_tools.model_registry import production_registry
from swcraft_tools.project import require_project
from swcraft_tools.reader import read_swc_model
from swcraft_tools.session import (
    _STAGING_SUFFIX,
    ESCAPE_ENV_VARS,
    NEUTRALIZED_ESCAPE_VALUE,
    AuditReplayError,
    CommitConflictError,
    CommitLogError,
    CommitPreflightError,
    ScopedSession,
    UnexplainedChangeError,
    commit_lock,
    model_fingerprint,
    pinned_escape_env,
    session_env,
)
from swcraft_tools.snapshot import ModelSnapshot
from symlink_support import symlink_or_skip

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def _write_swc(model_root: Path, name: str, description: str = "test swc") -> Path:
    target = model_root / "swc" / f"{name}.yaml"
    _write_yaml(target, {"name": name, "type": "application", "description": description})
    return target


def _make_project(root: Path, name: str = "TestProject") -> tuple[Path, Path]:
    """Write a minimal real project; returns (project_dir, model_root)."""
    project_dir = root / "real"
    model_root = project_dir / "model"
    _write_swc(model_root, "SwcOne")
    _write_yaml(project_dir / "swcraft.yaml", {"name": name, "model_root": "model"})
    return project_dir, model_root


def _tree_state(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _shadow_audit(session: ScopedSession) -> AuditLog:
    """The audit log the session's own MCP server writes into."""
    return session.shadow_audit_log


def _real_audit(project_dir: Path) -> AuditLog:
    return AuditLog(str(project_dir / "dist" / "audit.jsonl"), project_root=str(project_dir))


def _empty_diff() -> ModelDiff:
    """A diff reporting nothing — every layer untouched."""
    return ModelDiff(*(LayerDiff() for _ in ModelDiff.__dataclass_fields__))


# ---------------------------------------------------------------------------
# model_fingerprint()
# ---------------------------------------------------------------------------


def test_fingerprint_is_equal_for_byte_identical_trees(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    for root in (left, right):
        _write_swc(root, "SwcOne")
        _write_swc(root, "SwcTwo")

    assert model_fingerprint(str(left)) == model_fingerprint(str(right))


def test_fingerprint_changes_when_content_changes(tmp_path):
    model_root = tmp_path / "model"
    _write_swc(model_root, "SwcOne")
    before = model_fingerprint(str(model_root))

    _write_swc(model_root, "SwcOne", description="edited")

    assert model_fingerprint(str(model_root)) != before


def test_fingerprint_changes_on_rename_with_unchanged_content(tmp_path):
    """Names participate: moving a file without touching a byte changes the hash."""
    model_root = tmp_path / "model"
    original = _write_swc(model_root, "SwcOne")
    content = original.read_bytes()
    before = model_fingerprint(str(model_root))

    original.rename(model_root / "swc" / "Renamed.yaml")
    assert (model_root / "swc" / "Renamed.yaml").read_bytes() == content

    assert model_fingerprint(str(model_root)) != before


def test_fingerprint_is_framed_against_the_ab_c_collision_pair(tmp_path):
    """['ab', 'c'] and ['a', 'bc'] must not hash equal: an unframed digest sees "abc" in both."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    for name in ("ab", "c"):
        (left / name).write_bytes(b"")
    for name in ("a", "bc"):
        (right / name).write_bytes(b"")

    assert model_fingerprint(str(left)) != model_fingerprint(str(right))


def test_fingerprint_missing_model_root_is_an_error(tmp_path):
    """A missing model root is an explicit error, never a hash over nothing."""
    missing = tmp_path / "nope"

    with pytest.raises(FileNotFoundError) as exc:
        model_fingerprint(str(missing))

    # It says what is missing and never where: the message reaches a host's log.
    assert "model root" in str(exc.value)
    assert "nope" not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)

    # ... and specifically not the hash of an empty tree.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert model_fingerprint(str(empty))


def test_fingerprint_is_computed_fresh_on_every_call(tmp_path):
    model_root = tmp_path / "model"
    _write_swc(model_root, "SwcOne")
    first = model_fingerprint(str(model_root))
    assert model_fingerprint(str(model_root)) == first

    _write_swc(model_root, "SwcTwo")

    assert model_fingerprint(str(model_root)) != first


# ---------------------------------------------------------------------------
# ScopedSession.open()
# ---------------------------------------------------------------------------


def test_open_shadow_manifest_resolves_through_require_project(tmp_path):
    project_dir, _ = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()

        shadow = require_project(session.shadow_manifest)

        assert Path(shadow.resolved_model_root) == Path(session.shadow_model_root)
        assert shadow.name == "TestProject"
    finally:
        session.abandon()


def test_open_copies_the_real_model_into_the_shadow(tmp_path):
    project_dir, model_root = _make_project(tmp_path)
    _write_swc(model_root, "SwcTwo")
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()

        assert _tree_state(Path(session.shadow_model_root)) == _tree_state(model_root)
        assert model_fingerprint(session.shadow_model_root) == model_fingerprint(str(model_root))
    finally:
        session.abandon()


def test_open_shadow_dist_and_views_roots_live_inside_the_temp_tree(tmp_path):
    project_dir, _ = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        shadow = require_project(session.shadow_manifest)
        temp_root = Path(session.shadow_root).resolve()

        for path in (shadow.resolved_dist_root, shadow.resolved_views_root, shadow.resolved_model_root):
            assert Path(path).resolve().is_relative_to(temp_root), path
            assert not Path(path).resolve().is_relative_to(project_dir.resolve())
    finally:
        session.abandon()


def test_open_leaves_the_real_project_untouched(tmp_path):
    project_dir, model_root = _make_project(tmp_path)
    before = _tree_state(project_dir)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()

        assert _tree_state(project_dir) == before
        assert session.baseline_fingerprint == model_fingerprint(str(model_root))
    finally:
        session.abandon()


def test_open_takes_the_baseline_snapshot_from_the_shadow(tmp_path):
    project_dir, _ = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()

        assert isinstance(session.baseline_snapshot, ModelSnapshot)
        assert [s.name for s in session.baseline_snapshot.swcs] == ["SwcOne"]
    finally:
        session.abandon()


def test_shadow_paths_are_unavailable_before_open(tmp_path):
    project_dir, _ = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))

    assert session.is_open is False
    with pytest.raises(RuntimeError):
        _ = session.shadow_manifest


# ---------------------------------------------------------------------------
# The fingerprint-before-copy order
# ---------------------------------------------------------------------------


def test_a_real_model_change_during_the_copy_refuses_the_commit(tmp_path, monkeypatch):
    """Pins the fingerprint-before-copy order in open().

    Only the commit refusal is asserted: the shadow may have captured the racing write
    partially, and the (baseline_fingerprint, baseline_snapshot) pair is *not* claimed consistent.
    """
    project_dir, model_root = _make_project(tmp_path)
    original_copy = session_mod._copy_model_tree

    def racing_copy(src: str, dst: str) -> None:
        # A concurrent writer lands in the REAL model partway through the copy.
        _write_swc(model_root, "RaceSwc")
        original_copy(src, dst)

    monkeypatch.setattr(session_mod, "_copy_model_tree", racing_copy)

    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        # A perfectly ordinary in-shadow mutation that must not reach the real model.
        _write_swc(Path(session.shadow_model_root), "ShadowOnly")
        state_before_commit = _tree_state(model_root)

        with pytest.raises(CommitConflictError):
            session.commit(session.diff())

        assert _tree_state(model_root) == state_before_commit
        assert not (model_root / "swc" / "ShadowOnly.yaml").exists()
    finally:
        session.abandon()


# ---------------------------------------------------------------------------
# diff() — what the post-diff gate and the commit prompt are given
# ---------------------------------------------------------------------------


def test_diff_of_an_untouched_session_is_empty(tmp_path):
    project_dir, _ = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    session.open()
    try:
        assert session.diff().is_empty
    finally:
        session.abandon()


def test_diff_reports_what_the_session_changed_in_the_shadow(tmp_path):
    project_dir, _ = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    session.open()
    try:
        _write_swc(Path(session.shadow_model_root), "ShadowOnly")
        diff = session.diff()
        assert [swc.name for swc in diff.swcs.added] == ["ShadowOnly"]
        assert diff.swcs.removed == []
    finally:
        session.abandon()


def test_diff_is_taken_against_the_baseline_not_the_real_model_now(tmp_path):
    """A concurrent real-model edit must not silently appear in the session's diff.

    The commit's fingerprint re-check refuses a changed baseline; folding the edit into
    the diff would show the user a change their turn never made.
    """
    project_dir, model_root = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    session.open()
    try:
        _write_swc(model_root, "Meanwhile")
        assert session.diff().is_empty
    finally:
        session.abandon()


def test_diff_reports_a_removal_the_session_made(tmp_path):
    project_dir, _ = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    session.open()
    try:
        (Path(session.shadow_model_root) / "swc" / "SwcOne.yaml").unlink()
        diff = session.diff()
        assert [swc.name for swc in diff.swcs.removed] == ["SwcOne"]
    finally:
        session.abandon()


def test_diff_before_open_raises(tmp_path):
    project_dir, _ = _make_project(tmp_path)
    with pytest.raises(RuntimeError):
        ScopedSession(str(project_dir / "swcraft.yaml")).diff()


# ---------------------------------------------------------------------------
# abandon()
# ---------------------------------------------------------------------------


def test_abandon_removes_the_temp_tree_and_leaves_the_real_model_identical(tmp_path):
    project_dir, model_root = _make_project(tmp_path)
    before = model_fingerprint(str(model_root))
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    session.open()
    temp_root = Path(session.shadow_root)
    _write_swc(Path(session.shadow_model_root), "ShadowOnly")

    session.abandon()

    assert not temp_root.exists()
    assert model_fingerprint(str(model_root)) == before
    assert session.is_open is False


def test_abandon_is_idempotent(tmp_path):
    project_dir, _ = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    session.open()

    session.abandon()
    session.abandon()  # must not raise

    assert session.is_open is False


def test_abandon_before_open_is_a_no_op(tmp_path):
    project_dir, _ = _make_project(tmp_path)

    ScopedSession(str(project_dir / "swcraft.yaml")).abandon()


# ---------------------------------------------------------------------------
# open() preconditions and its failure cleanup
# ---------------------------------------------------------------------------


def _session_temp_dirs() -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob("swcraft-session-*"))


def test_opening_an_already_open_session_raises_and_leaves_it_usable(tmp_path):
    project_dir, _ = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    session.open()
    try:
        first_root = session.shadow_root

        with pytest.raises(RuntimeError, match="already open"):
            session.open()

        # The refusal must not have swapped or dropped the live shadow.
        assert session.shadow_root == first_root
        assert Path(first_root).is_dir()
    finally:
        session.abandon()


def test_a_failure_inside_open_leaves_no_temp_tree_behind(tmp_path, monkeypatch):
    """A failure inside open() leaks no temp tree, checked where ``tempfile.mkdtemp`` writes.

    ``ScopedSession`` calls ``mkdtemp`` with no ``dir``, so ``tempfile.tempdir`` is pointed at
    a directory private to this test: in the shared temp directory a concurrent session (an
    xdist sibling worker) appears or disappears between the snapshots. The additions form is
    kept because it names the leaked tree in the failure message.
    """
    private_tempdir = tmp_path / "systmp"
    private_tempdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private_tempdir))
    project_dir, model_root = _make_project(tmp_path)
    # Snapshotting the copy is the last step of open(); a malformed SWC makes it
    # raise *after* mkdtemp has already created the tree.
    (model_root / "swc" / "Broken.yaml").write_text("name: [unclosed\n", encoding="utf-8")

    before = _session_temp_dirs()
    session = ScopedSession(str(project_dir / "swcraft.yaml"))

    with pytest.raises(Exception):  # noqa: B017 - the loader's error type is not the claim
        session.open()

    leaked = _session_temp_dirs() - before
    assert leaked == set(), f"a failed open() must not leak its temp tree: {sorted(leaked)}"
    assert not session.is_open


def test_commit_and_session_env_before_open_are_refused(tmp_path):
    project_dir, _ = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))

    with pytest.raises(RuntimeError, match="not open"):
        session.commit(_empty_diff())
    with pytest.raises(RuntimeError, match="not open"):
        session.session_env()


# ---------------------------------------------------------------------------
# session_env()
# ---------------------------------------------------------------------------


def test_session_env_points_the_manifest_at_the_shadow_and_neutralizes_the_escape_hatches(tmp_path):
    project_dir, _ = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        base = {
            "SWCRAFT_MANIFEST": str(project_dir / "swcraft.yaml"),
            "SWCRAFT_MCP_BUILD_COMMAND": "moon run :build",
            "SWCRAFT_MCP_VALIDATE_COMMAND": "moon run :validate",
            "PATH": "/usr/bin",
        }

        env = session.session_env(base)

        assert env["SWCRAFT_MANIFEST"] == session.shadow_manifest
        assert env["SWCRAFT_MCP_BUILD_COMMAND"] == ""
        assert env["SWCRAFT_MCP_VALIDATE_COMMAND"] == ""
        assert env["PATH"] == "/usr/bin"
        # The caller's mapping is never mutated in place.
        assert base["SWCRAFT_MANIFEST"] == str(project_dir / "swcraft.yaml")
        assert base["SWCRAFT_MCP_BUILD_COMMAND"] == "moon run :build"
    finally:
        session.abandon()


def test_session_env_defaults_to_the_process_environment(tmp_path, monkeypatch):
    project_dir, _ = _make_project(tmp_path)
    monkeypatch.setenv("SWCRAFT_MCP_VALIDATE_COMMAND", "moon run :validate")
    monkeypatch.setenv("SWCRAFT_SESSION_MARKER", "kept")
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()

        env = session.session_env()

        assert env["SWCRAFT_MANIFEST"] == session.shadow_manifest
        assert env["SWCRAFT_MCP_VALIDATE_COMMAND"] == ""
        assert env["SWCRAFT_SESSION_MARKER"] == "kept"
        assert os.environ["SWCRAFT_MCP_VALIDATE_COMMAND"] == "moon run :validate"
    finally:
        session.abandon()


def test_session_env_function_is_usable_without_a_session(tmp_path):
    env = session_env(str(tmp_path / "shadow" / "swcraft.yaml"), {"SWCRAFT_MCP_BUILD_COMMAND": "x"})

    assert env == {
        "SWCRAFT_MANIFEST": str(tmp_path / "shadow" / "swcraft.yaml"),
        "SWCRAFT_MCP_BUILD_COMMAND": "",
        "SWCRAFT_MCP_VALIDATE_COMMAND": "",
    }


def test_the_escape_vars_are_present_and_empty_rather_than_absent(tmp_path):
    """An empty value, not a deletion, closes the ``.env`` re-injection hole.

    The spawned server runs ``load_dotenv(override=False)``, which skips only keys already
    *present*, so a deleted key lets the project's ``.env`` put the override back. An empty
    value survives ``load_dotenv`` and reads as unset to ``server.py``'s ``if override_command:``.
    """
    env = session_env(str(tmp_path / "swcraft.yaml"), {})

    for name in ESCAPE_ENV_VARS:
        assert name in env, f"{name} must be PRESENT, or a .env can repopulate it"
        assert env[name] == NEUTRALIZED_ESCAPE_VALUE
        assert not env[name], "an empty value is what server.py reads as 'no override'"


# ---------------------------------------------------------------------------
# pinned_escape_env() — the same rule for the server that is NOT re-pointed
# ---------------------------------------------------------------------------


def test_pinned_escape_env_fills_in_an_absent_override_as_present_and_empty():
    """The long-lived client's twin of the ``.env`` re-injection fix.

    The escape-command gate answers "would this server shell out?" from the spawn
    environment, and ``load_dotenv(override=False)`` fills exactly the *absent* keys.
    """
    env = pinned_escape_env({"PATH": "/usr/bin"})

    for name in ESCAPE_ENV_VARS:
        assert name in env, f"{name} must be PRESENT, or a .env can add it behind the gate's back"
        assert env[name] == NEUTRALIZED_ESCAPE_VALUE
    assert env["PATH"] == "/usr/bin"


def test_pinned_escape_env_keeps_a_configured_override():
    """A variable the host really has is passed on unchanged.

    Emptying it would turn the gate's refusal for the real project's server into a silent
    in-process run.
    """
    base = {"SWCRAFT_MCP_BUILD_COMMAND": "moon run :build"}

    env = pinned_escape_env(base)

    assert env["SWCRAFT_MCP_BUILD_COMMAND"] == "moon run :build"
    assert env["SWCRAFT_MCP_VALIDATE_COMMAND"] == NEUTRALIZED_ESCAPE_VALUE
    assert base == {"SWCRAFT_MCP_BUILD_COMMAND": "moon run :build"}, "the caller's mapping is never mutated"


def test_pinned_escape_env_defaults_to_the_process_environment(monkeypatch):
    monkeypatch.setenv("SWCRAFT_MCP_BUILD_COMMAND", "moon run :build")
    monkeypatch.delenv("SWCRAFT_MCP_VALIDATE_COMMAND", raising=False)
    monkeypatch.setenv("SWCRAFT_SESSION_MARKER", "kept")

    env = pinned_escape_env()

    assert env["SWCRAFT_MCP_BUILD_COMMAND"] == "moon run :build"
    assert env["SWCRAFT_MCP_VALIDATE_COMMAND"] == NEUTRALIZED_ESCAPE_VALUE
    assert env["SWCRAFT_SESSION_MARKER"] == "kept"
    assert "SWCRAFT_MCP_VALIDATE_COMMAND" not in os.environ, "the process environment is never touched"


def test_pinned_escape_env_leaves_the_manifest_alone(tmp_path):
    """Unlike ``session_env``, this env does not re-point the project.

    The long-lived client's own ``project_path`` stays authoritative."""
    env = pinned_escape_env({"SWCRAFT_MANIFEST": str(tmp_path / "swcraft.yaml")})

    assert env["SWCRAFT_MANIFEST"] == str(tmp_path / "swcraft.yaml")


# ---------------------------------------------------------------------------
# commit_lock() — the in-process half of the race boundary
# ---------------------------------------------------------------------------


def test_commit_lock_is_one_object_per_real_model_root(tmp_path):
    """One lock per project, and the *same* one for every spelling of it.

    Two spellings that each got a lock of their own would serialize nothing.
    """
    first = tmp_path / "project-a" / "model"
    second = tmp_path / "project-b" / "model"
    first.mkdir(parents=True)
    second.mkdir(parents=True)

    assert commit_lock(first) is commit_lock(str(first))
    assert commit_lock(first) is commit_lock(first / "." / ".." / "model")
    assert commit_lock(first) is not commit_lock(second)


def test_commit_lock_is_reentrant(tmp_path):
    """`ScopedSession.commit()` takes it inside a caller that already holds it.

    The editing engine holds it through its ``change_set_committed`` record, so a
    non-reentrant lock would deadlock the commit it protects.
    """
    model_root = tmp_path / "model"
    model_root.mkdir()
    lock = commit_lock(model_root)

    with lock, lock:
        assert True


def test_commit_lock_excludes_a_second_thread(tmp_path):
    """The property the whole fix rests on, asserted without the engine."""
    model_root = tmp_path / "model"
    model_root.mkdir()
    held = threading.Event()
    blocked = []

    def contend() -> None:
        blocked.append(commit_lock(model_root).acquire(timeout=0.2))

    with commit_lock(model_root):
        held.set()
        other = threading.Thread(target=contend)
        other.start()
        other.join(timeout=5)

    assert held.is_set()
    assert blocked == [False], "a second thread entered a commit region another thread held"


def test_the_lock_registry_grows_by_root_and_not_by_call(tmp_path):
    """The registry has no eviction; this is the bound that makes that safe.

    A key is a resolved model root, not a commit, request or session, and nothing on the
    wire names a project, so the key set is not client-drivable (``session._COMMIT_LOCKS``).
    """
    model_root = tmp_path / "many-commits" / "model"
    model_root.mkdir(parents=True)
    key = str(model_root.resolve())
    before = dict(session_mod._COMMIT_LOCKS)

    for _ in range(50):
        with commit_lock(model_root):
            pass
    # And every spelling of that one root, which is the other half of the bound.
    commit_lock(str(model_root))
    commit_lock(model_root / "." / ".." / "model")

    added = set(session_mod._COMMIT_LOCKS) - set(before)
    assert added == {key}, added


# ---------------------------------------------------------------------------
# commit() — the per-file apply
# ---------------------------------------------------------------------------


def test_commit_applies_added_modified_and_deleted_files(tmp_path):
    project_dir, model_root = _make_project(tmp_path)
    _write_swc(model_root, "Doomed")
    _write_swc(model_root, "Untouched")
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        shadow_model = Path(session.shadow_model_root)
        _write_swc(shadow_model, "Added")  # added
        _write_swc(shadow_model, "SwcOne", description="edited")  # modified
        (shadow_model / "swc" / "Doomed.yaml").unlink()  # deleted

        applied = session.commit(session.diff())

        assert (model_root / "swc" / "Added.yaml").exists()
        assert "edited" in (model_root / "swc" / "SwcOne.yaml").read_text(encoding="utf-8")
        assert not (model_root / "swc" / "Doomed.yaml").exists()
        assert (model_root / "swc" / "Untouched.yaml").exists()
        assert {Path(p).name for p in applied} == {"Added.yaml", "SwcOne.yaml", "Doomed.yaml"}
    finally:
        session.abandon()


def test_commit_survives_a_case_only_rename(tmp_path):
    """A case-only rename must not destroy the element it renames.

    On a case-insensitive filesystem the write ``swc/swcone.yaml`` and the removal
    ``swc/SwcOne.yaml`` name the SAME file, so "all writes, then all removals" would
    unlink the new content and leave NEITHER element while reporting success.
    """
    project_dir, model_root = _make_project(tmp_path)
    _write_swc(model_root, "Bystander")
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        shadow_model = Path(session.shadow_model_root)
        (shadow_model / "swc" / "SwcOne.yaml").unlink()
        _write_swc(shadow_model, "swcone", description="renamed")

        diff = session.diff()
        assert [item.name for item in diff.swcs.added] == ["swcone"]
        assert [item.name for item in diff.swcs.removed] == ["SwcOne"]

        session.commit(diff)

        # The renamed element survived, under its NEW name...
        assert {swc.name for swc in read_swc_model(str(model_root))} == {"swcone", "Bystander"}
        # ...and the file carries the new casing, so filename still matches `name`.
        assert {p.name for p in (model_root / "swc").iterdir()} == {"swcone.yaml", "Bystander.yaml"}
        assert "renamed" in (model_root / "swc" / "swcone.yaml").read_text(encoding="utf-8")
    finally:
        session.abandon()


def test_commit_of_an_ordinary_rename_still_removes_the_old_file(tmp_path):
    """The collision handling must not swallow a normal (non-case-only) rename."""
    project_dir, model_root = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        shadow_model = Path(session.shadow_model_root)
        (shadow_model / "swc" / "SwcOne.yaml").unlink()
        _write_swc(shadow_model, "SwcTwo")

        session.commit(session.diff())

        assert {p.name for p in (model_root / "swc").iterdir()} == {"SwcTwo.yaml"}
        assert {swc.name for swc in read_swc_model(str(model_root))} == {"SwcTwo"}
    finally:
        session.abandon()


def test_commit_leaves_the_real_model_fingerprinting_equal_to_the_shadow(tmp_path):
    project_dir, model_root = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        _write_swc(Path(session.shadow_model_root), "Added")

        session.commit(session.diff())

        assert model_fingerprint(str(model_root)) == model_fingerprint(session.shadow_model_root)
    finally:
        session.abandon()


def test_commit_leaves_unrelated_real_project_files_alone(tmp_path):
    """Only the model tree is applied — dist/audit.jsonl and friends survive."""
    project_dir, model_root = _make_project(tmp_path)
    _real_audit(project_dir).log_write("create_swc", {"name": "SwcOne"}, str(model_root / "swc" / "SwcOne.yaml"))
    (project_dir / "views").mkdir()
    (project_dir / "views" / "model.c4").write_text("// generated", encoding="utf-8")
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        _write_swc(Path(session.shadow_model_root), "Added")

        session.commit(session.diff())

        assert (project_dir / "views" / "model.c4").read_text(encoding="utf-8") == "// generated"
        historical = _real_audit(project_dir).get_entries()
        assert historical[0]["operation"] == "create_swc"
        assert "scope_id" not in historical[0]
    finally:
        session.abandon()


def test_commit_refuses_when_the_real_model_changed_after_open(tmp_path):
    project_dir, model_root = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        _write_swc(Path(session.shadow_model_root), "ShadowOnly")
        # A concurrent writer that COMPLETED before the final fingerprint read.
        _write_swc(model_root, "Concurrent")
        state_before_commit = _tree_state(model_root)

        with pytest.raises(CommitConflictError):
            session.commit(session.diff())

        assert _tree_state(model_root) == state_before_commit
        assert not (model_root / "swc" / "ShadowOnly.yaml").exists()
    finally:
        session.abandon()


# ---------------------------------------------------------------------------
# commit() — shadow audit replay with path rebasing
# ---------------------------------------------------------------------------


def test_commit_replays_shadow_audit_entries_into_the_real_log(tmp_path):
    project_dir, model_root = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        shadow_file = _write_swc(Path(session.shadow_model_root), "Added")
        _shadow_audit(session).log_write("create_swc", {"name": "Added"}, str(shadow_file))

        session.commit(session.diff())

        entries = _real_audit(project_dir).get_entries()
        assert len(entries) == 1
        assert entries[0]["operation"] == "create_swc"
        assert entries[0]["scope_id"] == session.scope_id
    finally:
        session.abandon()


def test_abandon_does_not_replay_shadow_audit_entries(tmp_path):
    project_dir, _ = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    session.open()
    shadow_file = _write_swc(Path(session.shadow_model_root), "Added")
    _shadow_audit(session).log_write("create_swc", {"name": "Added"}, str(shadow_file))

    session.abandon()

    assert _real_audit(project_dir).get_entries() == []


def test_replayed_result_points_at_the_real_model_file(tmp_path):
    """The real log names the file relative to the project directory, in POSIX spelling."""
    project_dir, model_root = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        temp_root = session.shadow_root
        shadow_file = _write_swc(Path(session.shadow_model_root), "Added")
        _shadow_audit(session).log_write("create_swc", {"name": "Added"}, str(shadow_file))
        assert _shadow_audit(session).get_entries()[0]["result"] == "model/swc/Added.yaml"

        session.commit(session.diff())

        result = _real_audit(project_dir).get_entries()[0]["result"]
        assert result == "model/swc/Added.yaml"
        assert (project_dir / result).is_file()
        assert temp_root not in result
        assert str(tmp_path) not in result
    finally:
        session.abandon()


def test_a_replayed_result_names_a_nested_model_root_by_its_project_relative_name(tmp_path):
    """The shadow's model directory is always ``model``; the real one is whatever the manifest says."""
    project_dir = tmp_path / "real"
    model_root = project_dir / "arch" / "model"
    _write_swc(model_root, "SwcOne")
    _write_yaml(project_dir / "swcraft.yaml", {"name": "nested", "model_root": "arch/model"})
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        shadow_file = _write_swc(Path(session.shadow_model_root), "Added")
        _shadow_audit(session).log_write("create_swc", {"name": "Added"}, str(shadow_file))

        session.commit(session.diff())

        assert _real_audit(project_dir).get_entries()[0]["result"] == "arch/model/swc/Added.yaml"
    finally:
        session.abandon()


@pytest.mark.parametrize(
    "retired",
    [
        pytest.param("DELETED: {shadow}/swc/SwcOne.yaml", id="the-deleted-prefix"),
        pytest.param("{shadow}/swc/A.yaml; {shadow}/swc/B.yaml", id="joined-absolute-paths"),
        pytest.param("model/swc/A.yaml; model/swc/B.yaml", id="joined-relative-paths"),
        pytest.param("(no files written)", id="the-no-files-sentence"),
        pytest.param("model/../outside.yaml", id="a-parent-component"),
        pytest.param("model/..\\..\\..\\outside.yaml", id="a-backslash-climb"),
    ],
)
def test_a_retired_result_form_is_refused_before_any_file_is_applied(tmp_path, retired):
    """A retired result form never reaches the real log.

    The shadow log is created empty by every ``open()`` and no live writer produces these
    forms, so only a tampered log carries one.
    """
    project_dir, model_root = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        shadow_model = Path(session.shadow_model_root)
        audit = _shadow_audit(session)
        audit.log_write("create_swc", {"name": "Added"}, str(_write_swc(shadow_model, "Added")))
        audit.log_write("legacy", {}, retired.format(shadow=shadow_model))
        state_before_commit = _tree_state(model_root)

        with pytest.raises(AuditReplayError) as exc:
            session.commit(session.diff())

        assert str(tmp_path) not in str(exc.value)
        assert _tree_state(model_root) == state_before_commit
        assert _real_audit(project_dir).get_entries() == []
    finally:
        session.abandon()


def test_no_replayed_result_ever_contains_the_temp_directory(tmp_path):
    project_dir, _ = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        temp_root = session.shadow_root
        shadow_model = Path(session.shadow_model_root)
        audit = _shadow_audit(session)
        audit.log_write("create_swc", {"name": "A"}, str(_write_swc(shadow_model, "A")))
        audit.log_write("create_swc", {"name": "B"}, str(_write_swc(shadow_model, "B")))

        session.commit(session.diff())

        for entry in _real_audit(project_dir).get_entries():
            assert temp_root not in entry["result"]
    finally:
        session.abandon()


def test_a_result_outside_the_shadow_model_root_aborts_before_any_file_is_applied(tmp_path):
    """The replay validation runs over ALL entries before the first apply."""
    project_dir, model_root = _make_project(tmp_path)
    outside = tmp_path / "outside" / "Escaped.yaml"
    outside.parent.mkdir()
    outside.write_text("name: Escaped\n", encoding="utf-8")
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        shadow_model = Path(session.shadow_model_root)
        audit = _shadow_audit(session)
        # A perfectly ordinary entry FIRST, so a per-entry apply loop would
        # already have written Added.yaml by the time the bad one is reached.
        audit.log_write("create_swc", {"name": "Added"}, str(_write_swc(shadow_model, "Added")))
        audit.log_write("create_swc", {"name": "Escaped"}, str(outside))
        state_before_commit = _tree_state(model_root)

        with pytest.raises(AuditReplayError) as exc:
            session.commit(session.diff())

        assert "outside the session's model root" in str(exc.value)
        assert str(tmp_path) not in str(exc.value), "the message reaches a host's log, so it names no path"
        assert _tree_state(model_root) == state_before_commit
        assert not (model_root / "swc" / "Added.yaml").exists()
        assert _real_audit(project_dir).get_entries() == []
    finally:
        session.abandon()


def test_a_result_that_is_neither_a_shadow_path_nor_a_known_token_aborts_the_commit(tmp_path):
    project_dir, model_root = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        _write_swc(Path(session.shadow_model_root), "Added")
        _shadow_audit(session).log_write("create_swc", {"name": "Added"}, "ok")
        state_before_commit = _tree_state(model_root)

        with pytest.raises(AuditReplayError):
            session.commit(session.diff())

        assert _tree_state(model_root) == state_before_commit
    finally:
        session.abandon()


@pytest.mark.parametrize("missing", ["timestamp", "operation", "args", "result"])
def test_a_shadow_entry_missing_a_required_key_aborts_before_any_file_is_applied(tmp_path, missing):
    """``commit()``'s up-front validation is EVERY check ``replay_entry`` makes.

    ``replay_entry`` runs *after* ``_apply_model_delta`` and raises a plain ``ValueError``,
    so a malformed entry it alone caught would escape with the real model already written.
    Reachable only via a tampered or truncated shadow log.
    """
    project_dir, model_root = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        shadow_model = Path(session.shadow_model_root)
        log_path = Path(session.shadow_root) / "dist" / "audit.jsonl"
        entry = {
            "timestamp": "2026-08-06T00:00:00+00:00",
            "operation": "create_swc",
            "args": {"name": "Added"},
            "result": str(_write_swc(shadow_model, "Added")),
        }
        del entry[missing]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        state_before_commit = _tree_state(model_root)

        with pytest.raises(AuditReplayError) as exc:
            session.commit(session.diff())

        assert missing in str(exc.value)
        assert _tree_state(model_root) == state_before_commit
        assert not (model_root / "swc" / "Added.yaml").exists()
        assert _real_audit(project_dir).get_entries() == []
    finally:
        session.abandon()


@pytest.mark.parametrize(
    ("label", "line"),
    [
        ("a bare null", "null"),
        ("a number", "17"),
        ("a boolean", "true"),
        # `key not in entry` answers correctly over a list by coincidence, which
        # would hide a missing mapping guard.
        ("an array of the four key names", '["timestamp", "operation", "args", "result"]'),
        # Over a *string* `in` is a SUBSTRING test, so all four keys pass the
        # missing-key check and the subscript fails instead.
        ("a string carrying all four key names", '"timestamp operation args result"'),
    ],
)
def test_a_shadow_entry_that_is_not_an_object_aborts_before_any_file_is_applied(tmp_path, label, line):
    """A reader of a log file must not raise on the corruption it exists to report.

    ``key not in entry`` raises ``TypeError`` over ``null``, a number or a boolean, which
    ``commit()``'s ``except (OSError, ValueError, yaml.YAMLError)`` does not catch; the line
    must refuse as the ``AuditReplayError`` pre-apply failure a malformed-JSON line produces.
    """
    project_dir, model_root = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        _write_swc(Path(session.shadow_model_root), "Added")
        log_path = Path(session.shadow_root) / "dist" / "audit.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(line + "\n", encoding="utf-8")
        state_before_commit = _tree_state(model_root)

        with pytest.raises(AuditReplayError):
            session.commit(session.diff())

        assert _tree_state(model_root) == state_before_commit, label
        assert not (model_root / "swc" / "Added.yaml").exists(), label
        assert _real_audit(project_dir).get_entries() == [], label
    finally:
        session.abandon()


def test_a_non_model_file_appearing_under_the_model_root_refuses_the_commit(tmp_path):
    """The fingerprint's breadth is a decision, not an oversight.

    ``model_fingerprint`` covers every regular file under the model root, the same rule
    ``_apply_model_delta`` applies when it unlinks a real file the shadow lacks. A narrower
    fingerprint would let a ``.DS_Store`` appearing after ``open()`` be silently **deleted**.
    """
    project_dir, model_root = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        _write_swc(Path(session.shadow_model_root), "Added")
        stray = model_root / ".DS_Store"
        stray.write_bytes(b"\x00mac metadata")

        with pytest.raises(CommitConflictError) as exc:
            session.commit(session.diff())

        assert "every file under" in str(exc.value), "the message must name what counts"
        assert str(tmp_path) not in str(exc.value), "and never where: it reaches a client's refusal"
        assert stray.is_file(), "the stray file must survive the refusal"
        assert not (model_root / "swc" / "Added.yaml").exists()
    finally:
        session.abandon()


def test_a_failed_audit_replay_after_the_apply_says_the_model_was_committed(tmp_path, monkeypatch):
    """The one failure that happens *after* the apply must not read as a failed apply.

    An ``OSError`` appending to the real log, commit()'s last step, must not read like a
    mid-apply one ("the model may be partially applied"): the model is applied in full and
    only the audit trail is incomplete.
    """
    project_dir, model_root = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        added = _write_swc(Path(session.shadow_model_root), "Added")
        _shadow_audit(session).log_write("create_swc", {"name": "Added"}, str(added))

        def boom(self, entry, scope_id=None):
            raise OSError("no space left on device")

        monkeypatch.setattr(AuditLog, "replay_entry", boom)

        with pytest.raises(CommitLogError) as exc:
            session.commit(session.diff())

        real_file = model_root / "swc" / "Added.yaml"
        assert real_file.is_file(), "the apply itself succeeded and must stay applied"
        assert exc.value.applied == [str(real_file)], "the error carries what really landed"
        assert "committed in full" in str(exc.value)
        assert "no space left on device" in str(exc.value)
    finally:
        session.abandon()


def test_a_symlinked_directory_under_the_model_refuses_the_commit(tmp_path):
    """A user-planted symlinked directory is fail-closed, and that is the boundary.

    ``rglob`` does not descend a symlinked directory while ``copytree`` copies its
    *contents*, so every scoped commit in such a project refuses. Following the link would
    reach outside ``model_root`` and copying it would route shadow writes into the real
    tree, so the refusal is pinned rather than a repair. Only the user can create the link.
    """
    project_dir, model_root = _make_project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Planted.yaml").write_text("name: Planted\n", encoding="utf-8")
    symlink_or_skip(model_root / "linked", outside)

    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        _write_swc(Path(session.shadow_model_root), "Added")
        state_before_commit = _tree_state(model_root)

        with pytest.raises(UnexplainedChangeError) as exc:
            session.commit(session.diff())

        assert "linked/Planted.yaml" in str(exc.value)
        assert not (model_root / "swc" / "Added.yaml").exists(), "the whole commit is refused"
        assert _tree_state(model_root) == state_before_commit
    finally:
        session.abandon()


def test_commit_without_a_shadow_audit_log_replays_nothing(tmp_path):
    project_dir, model_root = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        _write_swc(Path(session.shadow_model_root), "Added")

        session.commit(session.diff())

        assert (model_root / "swc" / "Added.yaml").exists()
        assert _real_audit(project_dir).get_entries() == []
    finally:
        session.abandon()


# ---------------------------------------------------------------------------
# commit() — the byte delta must be covered by the REVIEWED semantic diff
# ---------------------------------------------------------------------------


def test_a_byte_change_with_no_semantic_change_refuses_the_commit(tmp_path):
    """Nothing is applied on the strength of a diff that does not mention it.

    Writers re-serialize whole files and Pydantic drops keys the schema does not know, so a
    semantically empty change still rewrites a hand-written file's comments and unrecognised keys.
    """
    project_dir, model_root = _make_project(tmp_path)
    hand_written = model_root / "swc" / "SwcTwo.yaml"
    hand_written.write_text(
        "# an architect's note the serializer does not keep\nname: SwcTwo\ntype: application\nowner: team-a\n",
        encoding="utf-8",
    )
    original_bytes = hand_written.read_bytes()
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        shadow_model = Path(session.shadow_model_root)
        # In scope, and reported by the diff.
        _write_swc(shadow_model, "Added")
        # NOT in the diff: same element, same fields, different bytes.
        (shadow_model / "swc" / "SwcTwo.yaml").write_text(
            yaml.safe_dump({"name": "SwcTwo", "type": "application"}, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        diff = session.diff()
        assert [swc.name for swc in diff.swcs.added] == ["Added"]
        assert diff.swcs.changed == [], "the semantic diff really is silent about SwcTwo"

        with pytest.raises(UnexplainedChangeError) as exc:
            session.commit(diff)

        assert "SwcTwo.yaml" in str(exc.value)
        assert hand_written.read_bytes() == original_bytes, "the unreviewed file must survive byte-for-byte"
        assert not (model_root / "swc" / "Added.yaml").exists(), "the whole commit is refused, not the surplus alone"
        assert _real_audit(project_dir).get_entries() == []
    finally:
        session.abandon()


def test_a_file_no_model_layer_reads_refuses_the_commit(tmp_path):
    """A file the readers never parse can never appear in a diff, so it is never applied."""
    project_dir, model_root = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        shadow_model = Path(session.shadow_model_root)
        _write_swc(shadow_model, "Added")
        (shadow_model / "notes.txt").write_text("scratch", encoding="utf-8")

        with pytest.raises(UnexplainedChangeError) as exc:
            session.commit(session.diff())

        assert "notes.txt" in str(exc.value)
        assert not (model_root / "notes.txt").exists()
        assert not (model_root / "swc" / "Added.yaml").exists()
    finally:
        session.abandon()


def test_a_file_nested_under_a_flat_layer_refuses_the_commit(tmp_path):
    """Only ``swc/`` is globbed recursively, so only it may nest.

    ``interfaces/sub/ISpeed.yaml`` is read by no layer; matching it by the *name* it declares
    would explain it by an element another file is about — the fail-open direction.
    """
    project_dir, model_root = _make_project(tmp_path)
    _write_yaml(
        model_root / "interfaces" / "ISpeed.yaml",
        {
            "name": "ISpeed",
            "kind": "sender_receiver",
            "data_elements": [{"name": "speed", "implementation_type": "uint32"}],
        },
    )
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        shadow_model = Path(session.shadow_model_root)
        # A real, diff-reported change to the interface called ISpeed...
        _write_yaml(
            shadow_model / "interfaces" / "ISpeed.yaml",
            {
                "name": "ISpeed",
                "kind": "sender_receiver",
                "description": "reviewed",
                "data_elements": [{"name": "speed", "implementation_type": "uint32"}],
            },
        )
        # ...and a nested file declaring that same name, which no reader parses.
        _write_yaml(
            shadow_model / "interfaces" / "sub" / "ISpeed.yaml",
            {
                "name": "ISpeed",
                "kind": "sender_receiver",
                "data_elements": [{"name": "speed", "implementation_type": "uint32"}],
            },
        )

        diff = session.diff()
        assert [changed.key for changed in diff.interfaces.changed] == ["ISpeed"]

        with pytest.raises(UnexplainedChangeError) as exc:
            session.commit(diff)

        assert "sub/ISpeed.yaml" in str(exc.value)
        assert not (model_root / "interfaces" / "sub").exists()
        assert "reviewed" not in (model_root / "interfaces" / "ISpeed.yaml").read_text(encoding="utf-8"), (
            "the whole commit is refused, not the surplus file alone"
        )
    finally:
        session.abandon()


def test_a_nested_swc_file_is_explained_because_that_layer_globs_recursively(tmp_path):
    """The other half: ``swc/{group}/{name}.yaml`` is the documented layout."""
    project_dir, model_root = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        shadow_model = Path(session.shadow_model_root)
        _write_yaml(
            shadow_model / "swc" / "chassis" / "BrakeController.yaml",
            {"name": "BrakeController", "type": "application"},
        )

        applied = session.commit(session.diff())

        assert (model_root / "swc" / "chassis" / "BrakeController.yaml").is_file()
        assert applied == [str(model_root / "swc" / "chassis" / "BrakeController.yaml")]
    finally:
        session.abandon()


def test_a_neighbouring_file_named_like_a_staging_file_survives_a_commit(tmp_path):
    """The apply may not destroy a file it was never asked to touch.

    ``<target>.tmp`` is a legal name under ``model_root`` (an editor leftover, a backup),
    identical on both sides and so neither reviewed nor refused. Staging at a fixed
    ``SwcOne.yaml.tmp`` would overwrite it and ``os.replace`` would remove it unreported.
    """
    project_dir, model_root = _make_project(tmp_path)
    neighbour = model_root / "swc" / "SwcOne.yaml.tmp"
    neighbour.write_text("name: kept by hand\n", encoding="utf-8")
    original_bytes = neighbour.read_bytes()

    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        _write_swc(Path(session.shadow_model_root), "SwcOne", description="edited")

        applied = session.commit(session.diff())

        assert "edited" in (model_root / "swc" / "SwcOne.yaml").read_text(encoding="utf-8")
        assert neighbour.is_file(), "the commit deleted a model file the diff never mentioned"
        assert neighbour.read_bytes() == original_bytes
        assert applied == [str(model_root / "swc" / "SwcOne.yaml")]
    finally:
        session.abandon()


def test_a_commit_leaves_no_staging_file_behind(tmp_path):
    """The staged sibling is always consumed by its ``os.replace``."""
    project_dir, model_root = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        _write_swc(Path(session.shadow_model_root), "Added")

        session.commit(session.diff())

        assert [p.name for p in model_root.rglob("*") if p.is_file() and p.name.endswith(_STAGING_SUFFIX)] == []
        assert model_fingerprint(str(model_root)) == model_fingerprint(session.shadow_model_root)
    finally:
        session.abandon()


def test_a_reviewed_element_carries_its_whole_file_including_what_the_writers_drop(tmp_path):
    """The granularity of the "only what was reviewed is applied" guarantee: the FILE.

    A file is applied whole once the reviewed diff accounts for its element: inside it the
    guarantee stops at the file boundary. Writers re-serialize whole files and Pydantic's
    ``extra="ignore"`` drops keys the schema does not know, so a comment and an unknown key
    are lost by an *approved* edit, as in an unscoped run; refusing that would refuse every
    scoped edit to a hand-written model.
    """
    project_dir, model_root = _make_project(tmp_path)
    hand_written = model_root / "swc" / "SwcOne.yaml"
    hand_written.write_text(
        "# reviewed by J.Doe, do not edit\nname: SwcOne\ntype: application\nasil_level: D\n",
        encoding="utf-8",
    )
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        _write_swc(Path(session.shadow_model_root), "SwcOne", description="approved edit")

        diff = session.diff()
        assert [changed.key for changed in diff.swcs.changed] == ["SwcOne"]

        session.commit(diff)

        committed = hand_written.read_text(encoding="utf-8")
        assert "approved edit" in committed
        assert "reviewed by J.Doe" not in committed, "the boundary is the file, and this is what that costs"
        assert "asil_level" not in committed
    finally:
        session.abandon()


def test_every_layer_directory_maps_to_a_real_diff_layer():
    """The file -> element mapping is what makes the two sets comparable.

    A layer with no mapping would make every file under its directory unexplained, so the
    mapping, composed from the registry as ``session._is_explained`` does, is pinned against
    ``ModelDiff``'s layers and against the directories ``swcraft init`` scaffolds. The scaffold
    side is ``fixtures/recorded_oracles/scaffold_directories.json``, a frozen recording of a real
    ``swcraft init``, so the equality is not the registry compared with itself. No layer is
    optional: a contract declaring one optional, or a required layer ``swcraft init`` does
    not create, fails here.
    """
    registry = production_registry()
    directories = {
        facts.storage.directory: facts.snapshot_attribute
        for facts in registry.layers
        if facts.snapshot_attribute is not None
    }
    required = {facts.storage.directory for facts in registry.layers if not facts.optional}
    optional = {facts.storage.directory for facts in registry.layers if facts.optional}
    scaffolded = set(
        json.loads(
            (Path(__file__).parent / "fixtures" / "recorded_oracles" / "scaffold_directories.json").read_text(
                encoding="utf-8"
            )
        )["declared"]
    )

    assert set(directories.values()) == set(ModelDiff.__dataclass_fields__)
    assert set(directories) == required | optional
    assert required == scaffolded
    assert optional == set(directories) - scaffolded
    assert optional == set(), "no layer is optional: `hardware` is a scaffolded layer"
    assert scaffolded, "the reconciliation is vacuous unless the scaffold really declares directories"


def test_a_commit_preflight_read_failure_is_typed_and_applies_nothing(tmp_path):
    """A truncated shadow audit log is a ``ValueError`` — and must not escape as one.

    A partial write raises ``json.JSONDecodeError`` before the first ``os.replace()``, so
    the model is untouched and the host has to be able to say so.
    """
    project_dir, model_root = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        _write_swc(Path(session.shadow_model_root), "Added")
        log_path = Path(session.shadow_root) / "dist" / "audit.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text('{"timestamp": "2026-08-06T00:00:00+00:00", "operation": "cre', encoding="utf-8")
        state_before_commit = _tree_state(model_root)

        with pytest.raises(CommitPreflightError):
            session.commit(session.diff())

        assert _tree_state(model_root) == state_before_commit
        assert not (model_root / "swc" / "Added.yaml").exists()
    finally:
        session.abandon()


def test_scope_id_is_stable_and_unique_per_session(tmp_path):
    project_dir, _ = _make_project(tmp_path)
    first = ScopedSession(str(project_dir / "swcraft.yaml"))
    second = ScopedSession(str(project_dir / "swcraft.yaml"))

    captured = first.scope_id
    assert first.scope_id == captured, "the id is generated once, not per access"
    assert first.scope_id != second.scope_id
    assert ScopedSession(str(project_dir / "swcraft.yaml"), scope_id="given").scope_id == "given"


# ---------------------------------------------------------------------------
# The pre-apply refusals name no location
# ---------------------------------------------------------------------------
#
# Each of these propagates out of the engine, so an MCP client gets the masked
# line and the message is the server log's. A log is not the place for a path
# either: the shadow copy's location, the real model root or an entry's recorded
# result path.


def test_a_preflight_os_error_names_the_file_by_its_storage_name(tmp_path, monkeypatch):
    project_dir, model_root = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        _write_swc(Path(session.shadow_model_root), "Added")
        gone = model_root / "swc" / "Gone.yaml"

        def vanished(*_args, **_kwargs):
            # The OSError the OS raises for a file that went away between two reads.
            raise FileNotFoundError(2, "No such file or directory", str(gone))

        monkeypatch.setattr(session_mod, "_delta_paths", vanished)
        state_before_commit = _tree_state(model_root)

        with pytest.raises(CommitPreflightError) as exc:
            session.commit(session.diff())

        assert str(exc.value) == (
            "the commit could not be prepared ([Errno 2] No such file or directory: swc/Gone.yaml); nothing was applied"
        )
        assert str(tmp_path) not in str(exc.value)
        assert _tree_state(model_root) == state_before_commit
    finally:
        session.abandon()


def test_a_replay_failure_names_no_audit_log_path(tmp_path, monkeypatch):
    project_dir, model_root = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        added = _write_swc(Path(session.shadow_model_root), "Added")
        _shadow_audit(session).log_write("create_swc", {"name": "Added"}, str(added))
        log = project_dir / "dist" / "audit.jsonl"

        def full_disk(self, entry, scope_id=None):
            raise OSError(28, "No space left on device", str(log))

        monkeypatch.setattr(AuditLog, "replay_entry", full_disk)

        with pytest.raises(CommitLogError) as exc:
            session.commit(session.diff())

        assert str(exc.value) == (
            "the model was committed in full, but the audit log could not be updated "
            "([Errno 28] No space left on device)"
        )
        assert (model_root / "swc" / "Added.yaml").is_file()
    finally:
        session.abandon()


@pytest.mark.parametrize(
    ("label", "line", "kind"),
    [("a bare null", "null", "NoneType"), ("a path string", '"/somewhere/else/Escaped.yaml"', "str")],
)
def test_a_shadow_entry_that_is_not_an_object_is_named_by_its_type_alone(tmp_path, label, line, kind):
    project_dir, _model_root = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        _write_swc(Path(session.shadow_model_root), "Added")
        log_path = Path(session.shadow_root) / "dist" / "audit.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(line + "\n", encoding="utf-8", newline="\n")

        with pytest.raises(AuditReplayError) as exc:
            session.commit(session.diff())

        assert str(exc.value) == f"shadow audit entry is not an object: {kind}", label
    finally:
        session.abandon()


def test_a_shadow_entry_missing_a_key_is_not_quoted(tmp_path):
    project_dir, _model_root = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        result = str(_write_swc(Path(session.shadow_model_root), "Added"))
        log_path = Path(session.shadow_root) / "dist" / "audit.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"operation": "create_swc", "args": {"name": "Added"}, "result": result}
        log_path.write_text(json.dumps(entry) + "\n", encoding="utf-8", newline="\n")

        with pytest.raises(AuditReplayError) as exc:
            session.commit(session.diff())

        assert str(exc.value) == "shadow audit entry is missing timestamp"
        assert str(session.shadow_root) not in str(exc.value)
    finally:
        session.abandon()


def test_a_shadow_entry_with_a_non_string_result_is_named_by_its_type_alone(tmp_path):
    project_dir, _model_root = _make_project(tmp_path)
    session = ScopedSession(str(project_dir / "swcraft.yaml"))
    try:
        session.open()
        added = str(_write_swc(Path(session.shadow_model_root), "Added"))
        log_path = Path(session.shadow_root) / "dist" / "audit.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"timestamp": "2026-09-13T00:00:00+00:00", "operation": "create_swc", "args": {}, "result": [added]}
        log_path.write_text(json.dumps(entry) + "\n", encoding="utf-8", newline="\n")

        with pytest.raises(AuditReplayError) as exc:
            session.commit(session.diff())

        assert str(exc.value) == "shadow audit entry has a non-string result: list"
    finally:
        session.abandon()
