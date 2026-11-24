"""The project's audit log names no location outside the project's own names.

A location is recorded project-relative in POSIX spelling (``model/swc/Foo.yaml``), and one outside the
project as ``(outside the project)``. The gate drives every writer of the real log this package owns
through its real entry point and asks of every string the log holds whether it is an absolute path on
either platform, or carries the home directory or the project's location.

What the gate does not reach:

- the agent host's records, which ``packages/agent/`` writes (``packages/agent/tests/test_scoped_turn.py``
  holds their one location, ``call_refused``'s ``target``);
- a relative location escaping the project (``../x``) inside ``args``, which are recorded as given;
- a ``result`` that is a sentence, not a path, which ``AuditLog`` records as given (no live writer
  produces one);
- the ``mutations.py`` writers other than ``create_swc``: they share one ``audit`` helper and result
  shape, so one stands for the class;
- a log written by an earlier release, which nothing converts: only the commit's rebase reads a
  ``result``, from the shadow log a session creates empty.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
from swcraft_tools.editing.audit import AUDIT_FILENAME, project_audit_log
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.staging import baseline_token
from swcraft_tools.project import is_absolute_anywhere, require_project
from swcraft_tools.writer import create_swc


def _approve(proposal: Any) -> ApprovalDecision:
    return ApprovalDecision(
        change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=True
    )


def _decline(proposal: Any) -> ApprovalDecision:
    return ApprovalDecision(change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=False)


def _change_set(project: Path, *operations: dict[str, Any], review: Any) -> dict[str, Any]:
    request = {
        "operation": "change_set",
        "baseline": baseline_token(project / "model"),
        "intent": "the audit-location gate",
        "operations": list(operations),
    }
    return handle_request(request, project=project, approved_scope=None, review=review)


_UPDATE = {
    "operation": "update",
    "address": {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "priority"}]},
    "value": 6,
}
_CREATE = {
    "operation": "create",
    "address": {"layer": "implementation_types", "element": "Speed_kph"},
    "value": {"name": "Speed_kph", "kind": "value_type", "base": "float32_base", "unit": "kph"},
}
_REMOVE = {"operation": "remove", "address": {"layer": "interfaces", "element": "IDiagnosticService"}}
_UNKNOWN = {"operation": "update", "address": {"layer": "tasks", "element": "NoSuchTask"}, "value": 1}


def _strings(value: Any) -> Iterator[str]:
    """Every string in one parsed record: keys and values, at any depth."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_no_record_any_writer_writes_carries_an_absolute_path_or_the_home(tmp_path: Path) -> None:
    project = fixtures.build_fixture(tmp_path / "project", "read-swc-element")

    committed = _change_set(project, _UPDATE, _CREATE, _REMOVE, review=_approve)
    assert committed.get("status") == "committed", committed
    declined = _change_set(project, _UPDATE, review=_decline)
    assert declined.get("status") == "abandoned", declined
    rejected = _change_set(project, _UNKNOWN, review=_approve)
    assert rejected.get("response") == "error", rejected
    log = project_audit_log(require_project(str(project / "swcraft.yaml")))
    create_swc(str(project / "model"), "AuditedSwc", group="audited", swc_type="application", auditor=log)

    lines = (project / "dist" / AUDIT_FILENAME).read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    operations = {record["operation"] for record in records}
    assert {"write_element", "remove_element", "create_swc"} <= operations, operations
    assert {"change_set_committed", "change_set_abandoned", "change_set_rejected"} <= operations, operations

    forbidden = {str(Path.home()), str(tmp_path), str(project.resolve()), Path.home().as_posix()}
    for record in records:
        for text in _strings(record):
            assert not is_absolute_anywhere(text), (record["operation"], text)
            assert not any(place in text for place in forbidden), (record["operation"], text)


def test_a_writer_entry_names_the_file_by_its_project_relative_posix_name(tmp_path: Path) -> None:
    """The name the architect sees in the repository: ``model/<target>``."""
    project = fixtures.build_fixture(tmp_path / "project", "read-swc-element")

    committed = _change_set(project, _UPDATE, _CREATE, _REMOVE, review=_approve)
    assert committed.get("status") == "committed", committed

    lines = (project / "dist" / AUDIT_FILENAME).read_text(encoding="utf-8").splitlines()
    writers = [
        record
        for record in (json.loads(line) for line in lines if line.strip())
        if record["operation"] in {"write_element", "remove_element"}
    ]
    assert sorted((record["operation"], record["result"]) for record in writers) == sorted(
        (record["operation"], f"model/{record['args']['target']}") for record in writers
    )
    assert {record["result"] for record in writers} >= {"model/interfaces/IDiagnosticService.yaml"}
    for record in writers:
        assert (record["operation"] == "remove_element") != (project / record["result"]).is_file()
