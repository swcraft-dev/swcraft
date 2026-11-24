"""Tests for auditor.py: AuditLog, its optional ``scope_id`` and :meth:`AuditLog.replay_entry`."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from swcraft_tools.auditor import NO_PROJECT_DIRECTORY, OUTSIDE_THE_PROJECT, AuditLog, project_location

_HISTORICAL_KEYS = ["timestamp", "operation", "args", "result"]


def test_log_write_creates_file(tmp_path):
    log = AuditLog(str(tmp_path / "audit.jsonl"))
    log.log_write("create_swc", {"name": "Foo"}, "ok")
    audit_file = tmp_path / "audit.jsonl"
    assert audit_file.exists()
    lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_log_write_entry_has_required_fields(tmp_path):
    log = AuditLog(str(tmp_path / "audit.jsonl"))
    log.log_write("create_swc", {"name": "Foo"}, "ok")
    entries = log.get_entries()
    assert len(entries) == 1
    entry = entries[0]
    assert "timestamp" in entry
    assert "operation" in entry
    assert "args" in entry
    assert "result" in entry
    assert entry["operation"] == "create_swc"
    assert entry["args"] == {"name": "Foo"}
    assert entry["result"] == "ok"


def test_log_write_timestamp_is_iso8601(tmp_path):
    log = AuditLog(str(tmp_path / "audit.jsonl"))
    log.log_write("add_port", {"swc": "FooSWC", "port": "pOut"}, "added")
    entries = log.get_entries()
    dt = datetime.fromisoformat(entries[0]["timestamp"])
    assert dt is not None


def test_log_write_three_calls_produces_three_lines(tmp_path):
    log = AuditLog(str(tmp_path / "audit.jsonl"))
    log.log_write("create_swc", {"name": "Foo"}, "ok")
    log.log_write("add_port", {"name": "pOut"}, "ok")
    log.log_write("add_runnable", {"name": "Run1"}, "ok")
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3


def test_get_entries_returns_list_in_insertion_order(tmp_path):
    log = AuditLog(str(tmp_path / "audit.jsonl"))
    log.log_write("op_a", {"x": 1}, "r1")
    log.log_write("op_b", {"x": 2}, "r2")
    log.log_write("op_c", {"x": 3}, "r3")
    entries = log.get_entries()
    assert len(entries) == 3
    assert entries[0]["operation"] == "op_a"
    assert entries[1]["operation"] == "op_b"
    assert entries[2]["operation"] == "op_c"


def test_log_write_appends_not_overwrites(tmp_path):
    log = AuditLog(str(tmp_path / "audit.jsonl"))
    log.log_write("create_swc", {"name": "Foo"}, "ok")
    log2 = AuditLog(str(tmp_path / "audit.jsonl"))
    log2.log_write("add_port", {"name": "pOut"}, "ok")
    entries = log2.get_entries()
    assert len(entries) == 2


def test_log_write_creates_nonexistent_parent(tmp_path):
    nested = tmp_path / "deep" / "nested" / "audit.jsonl"
    log = AuditLog(str(nested))
    log.log_write("create_swc", {"name": "Bar"}, "ok")
    assert nested.exists()
    entries = log.get_entries()
    assert len(entries) == 1
    assert entries[0]["operation"] == "create_swc"


# Optional scope_id and shadow-entry replay


def test_log_write_without_scope_id_emits_exactly_the_four_historical_keys(tmp_path):
    """Without a ``scope_id`` an entry keeps exactly the four historical keys, in order."""
    log = AuditLog(str(tmp_path / "audit.jsonl"))

    log.log_write("create_swc", {"name": "Foo"}, "model/swc/Foo.yaml")

    raw = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip())
    assert list(raw.keys()) == _HISTORICAL_KEYS


def test_log_write_with_scope_id_appends_a_fifth_key(tmp_path):
    log = AuditLog(str(tmp_path / "audit.jsonl"))

    log.log_write("create_swc", {"name": "Foo"}, "ok", scope_id="scope-1")

    raw = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip())
    assert list(raw.keys()) == [*_HISTORICAL_KEYS, "scope_id"]
    assert raw["scope_id"] == "scope-1"


def test_log_write_explicit_none_scope_id_is_the_historical_shape(tmp_path):
    log = AuditLog(str(tmp_path / "audit.jsonl"))

    log.log_write("create_swc", {"name": "Foo"}, "ok", scope_id=None)

    assert list(log.get_entries()[0].keys()) == _HISTORICAL_KEYS


def test_replay_entry_keeps_the_original_timestamp_and_gains_scope_id(tmp_path):
    shadow = AuditLog(str(tmp_path / "shadow.jsonl"))
    shadow.log_write("create_swc", {"name": "Foo"}, "model/swc/Foo.yaml")
    original = shadow.get_entries()[0]

    real = AuditLog(str(tmp_path / "real.jsonl"))
    real.replay_entry(original, scope_id="scope-1")

    replayed = real.get_entries()[0]
    assert replayed["timestamp"] == original["timestamp"]
    assert replayed["operation"] == "create_swc"
    assert replayed["args"] == {"name": "Foo"}
    assert replayed["result"] == "model/swc/Foo.yaml"
    assert replayed["scope_id"] == "scope-1"
    assert list(replayed.keys()) == [*_HISTORICAL_KEYS, "scope_id"]


def test_replay_entry_without_a_scope_id_stays_the_historical_shape(tmp_path):
    real = AuditLog(str(tmp_path / "real.jsonl"))

    real.replay_entry({"timestamp": "2026-08-05T00:00:00+00:00", "operation": "x", "args": {}, "result": "r"})

    assert list(real.get_entries()[0].keys()) == _HISTORICAL_KEYS


def test_replay_entry_appends_rather_than_overwriting(tmp_path):
    real = AuditLog(str(tmp_path / "real.jsonl"))
    real.log_write("create_swc", {"name": "Existing"}, "ok")

    real.replay_entry(
        {"timestamp": "2026-08-05T00:00:00+00:00", "operation": "create_swc", "args": {}, "result": "r"},
        scope_id="scope-1",
    )

    entries = real.get_entries()
    assert len(entries) == 2
    assert "scope_id" not in entries[0]
    assert entries[1]["scope_id"] == "scope-1"


@pytest.mark.parametrize("missing", _HISTORICAL_KEYS)
def test_replay_entry_rejects_a_malformed_entry(tmp_path, missing):
    """A replayed entry is a *recorded* entry — a missing key is a bug, not a default."""
    entry = {"timestamp": "2026-08-05T00:00:00+00:00", "operation": "x", "args": {}, "result": "r"}
    del entry[missing]
    real = AuditLog(str(tmp_path / "real.jsonl"))

    with pytest.raises(ValueError, match=missing):
        real.replay_entry(entry, scope_id="scope-1")

    assert real.get_entries() == []


@pytest.mark.parametrize(
    ("label", "entry"),
    [
        ("a bare null", None),
        ("a number", 17),
        ("a boolean", True),
        ("an array of the four key names", _HISTORICAL_KEYS),
        # `in` over a string is a SUBSTRING test, so this one would pass a
        # missing-key check and then fail as a non-mapping.
        ("a string carrying all four key names", "timestamp operation args result"),
    ],
)
def test_replay_entry_rejects_an_entry_that_is_not_an_object(tmp_path, label, entry):
    """A non-object entry is refused with ``ValueError`` and nothing is appended.

    Guards ``key not in entry`` as the shape check: it raised ``TypeError`` over a scalar and answered by
    substring over a string.
    """
    real = AuditLog(str(tmp_path / "real.jsonl"))

    with pytest.raises(ValueError, match="object"):
        real.replay_entry(entry, scope_id="scope-1")

    assert real.get_entries() == [], label


def test_get_entries_returns_a_corrupted_line_rather_than_dropping_it(tmp_path):
    """A line that is not an object comes back as it parsed.

    Dropping it would fail open: a silently removed entry makes a tampered commit block look complete.
    """
    path = tmp_path / "audit.jsonl"
    log = AuditLog(str(path))
    log.log_write("write_element", {"target": "tasks/T.yaml"}, "ok")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("null\n17\n[1, 2]\n")

    entries = log.get_entries()

    assert [type(entry) for entry in entries] == [dict, type(None), int, list]
    assert entries[0]["operation"] == "write_element"


def test_concurrent_appends_from_two_logs_on_one_file_never_tear_a_line(tmp_path, monkeypatch):
    # Windows emulates O_APPEND as seek-then-write, so an unserialized append can overwrite another
    # thread's tail; splitting each write with a pause reproduces that tear on every host.
    import threading
    import time

    from swcraft_tools import auditor as auditor_mod

    def split_write(path, line):
        half = len(line) // 2
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line[:half])
            fh.flush()
            time.sleep(0.01)
            fh.write(line[half:])

    monkeypatch.setattr(auditor_mod, "_write_line", split_write)
    log_path = str(tmp_path / "audit.jsonl")

    def append_many(label: str) -> None:
        log = AuditLog(log_path)  # a fresh instance, as each request builds its own
        for i in range(10):
            log.log_write(label, {"i": i}, "ok")

    threads = [threading.Thread(target=append_many, args=(label,)) for label in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    entries = AuditLog(log_path).get_entries()
    assert len(entries) == 20
    assert sorted(entry["operation"] for entry in entries) == ["a"] * 10 + ["b"] * 10


# A recorded location is project-relative


def test_an_absolute_result_inside_the_project_is_recorded_relative_to_it(tmp_path):
    project = tmp_path / "project"
    written = project / "model" / "swc" / "Foo.yaml"
    log = AuditLog(str(project / "dist" / "audit.jsonl"), project_root=str(project))

    log.log_write("write_element", {"target": "swc/Foo.yaml"}, str(written))

    assert log.get_entries()[0]["result"] == "model/swc/Foo.yaml"
    assert str(tmp_path) not in (project / "dist" / "audit.jsonl").read_text(encoding="utf-8")


def test_an_absolute_result_outside_the_project_is_recorded_as_outside_it(tmp_path):
    project = tmp_path / "project"
    log = AuditLog(str(project / "dist" / "audit.jsonl"), project_root=str(project))

    log.log_write("write_element", {}, str(tmp_path / "elsewhere" / "Foo.yaml"))
    log.log_write("write_element", {}, str(tmp_path / "project-sibling" / "Foo.yaml"))

    assert [entry["result"] for entry in log.get_entries()] == [OUTSIDE_THE_PROJECT, OUTSIDE_THE_PROJECT]


def test_a_windows_spelled_absolute_result_is_a_location_too(tmp_path):
    """A drive-letter path is recognised on every host, so it never passes through as text."""
    project = tmp_path / "project"
    log = AuditLog(str(project / "dist" / "audit.jsonl"), project_root=str(project))

    log.log_write("write_element", {}, "C:\\Users\\someone\\model\\Foo.yaml")

    result = log.get_entries()[0]["result"]
    assert "someone" not in result


def test_a_replayed_absolute_result_is_made_relative_as_well(tmp_path):
    """``replay_entry`` is the second writer, and it goes through the same conversion."""
    project = tmp_path / "project"
    real = AuditLog(str(project / "dist" / "audit.jsonl"), project_root=str(project))

    real.replay_entry(
        {"timestamp": "t", "operation": "write_element", "args": {}, "result": str(project / "model" / "A.yaml")}
    )

    assert real.get_entries()[0]["result"] == "model/A.yaml"


def test_a_log_without_a_project_root_records_no_location(tmp_path):
    """Fail closed, and after the fact: the write already happened, so it is recorded, without its location."""
    log = AuditLog(str(tmp_path / "audit.jsonl"))

    log.log_write("write_element", {}, str(tmp_path / "model" / "Foo.yaml"))

    assert log.get_entries()[0]["result"] == NO_PROJECT_DIRECTORY
    assert str(tmp_path) not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


def test_the_other_platforms_absolute_spelling_is_never_resolved_into_the_project(tmp_path, monkeypatch):
    """Resolved against the current directory, the foreign spelling would land inside the project."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    foreign = "/home/someone/out.arxml" if os.name == "nt" else "C:\\Users\\someone\\out.arxml"

    assert project_location(str(project), foreign) == OUTSIDE_THE_PROJECT


@pytest.mark.parametrize("second", ["{project}/model/B.yaml", "{home}/private"], ids=["same-root", "another-root"])
def test_two_absolute_paths_joined_in_one_result_are_recorded_as_outside(tmp_path, second):
    project = tmp_path / "project"
    log = AuditLog(str(project / "dist" / "audit.jsonl"), project_root=str(project))
    other = second.format(project=project, home=Path.home())

    log.log_write("legacy", {}, f"{project / 'model' / 'A.yaml'}; {other}")

    assert log.get_entries()[0]["result"] == OUTSIDE_THE_PROJECT


def test_a_result_that_is_no_location_is_recorded_as_given(tmp_path):
    log = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))

    log.log_write("change_set_committed", {}, "1 file(s)")
    log.log_write("write_element", {}, "model/swc/Foo.yaml")

    assert [entry["result"] for entry in log.get_entries()] == ["1 file(s)", "model/swc/Foo.yaml"]
