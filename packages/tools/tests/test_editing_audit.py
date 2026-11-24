"""The six change-set lifecycle records, their id, and the commit block's completeness.

1. The id is allocated on receipt, so the three rejections before staging (pass A,
   the fail-fast staleness check, gate 1) are identified.
2. Every record reaches the real project's log, scoped or not, also for change sets
   that never land; ``AuditLog``'s four keys are unchanged, ``scope_id`` is optional.
3. The commit-intent record is written before the first ``os.replace()``; failing to
   write it is pre-apply (``CommitPreflightError``, model byte-identical).
4. The commit block is contiguous over its writer entries only: another change set's
   lifecycle record and the agent host's records may land inside it under no lock and
   are excluded by id or name; everything else in the span stays, foreign writes above all.
5. Completeness holds at element level (the delta is per leaf, the writer emits one
   entry per element) and is counted per entry, never as a set equality, which would
   forgive a second write of an accounted element and an unmappable target.

Approvals are injected (a scripted approver), never prompted. Every commit runs through
``handle_request`` (:func:`_commit_through`), because points 3 and 4 are about the
engine's own ordering, not one the test sequences.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import swcraft_tools.editing.audit as audit_mod
import swcraft_tools.editing.registry as reg
import swcraft_tools.session as session_mod
from swcraft_tools.auditor import AuditLog
from swcraft_tools.editing.address import Address, ScopeEntry
from swcraft_tools.editing.audit import (
    AUDIT_FILENAME,
    CHANGE_SET_ABANDONED,
    CHANGE_SET_APPROVED,
    CHANGE_SET_COMMIT_INTENT,
    CHANGE_SET_COMMITTED,
    CHANGE_SET_ID_PATTERN,
    CHANGE_SET_REJECTED,
    CHANGE_SET_STAGED,
    HOST_OPERATIONS,
    LIFECYCLE_OPERATIONS,
    SAFETY_NOT_ASKED,
    ChangeSetAuditor,
    auditor_for,
    block_is_complete,
    claims_change_set,
    commit_block,
    committed_element_addresses,
    element_address_of_target,
    incomplete_commits,
    new_change_set_id,
    project_audit_log,
    written_element_addresses,
)
from swcraft_tools.editing.authorization import ApprovalDecision, ApprovalMismatchError
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.staging import baseline_token
from swcraft_tools.model_registry import ModelRegistryError, compose_storage_path, longest_directory_match
from swcraft_tools.project import require_project
from swcraft_tools.session import CommitPreflightError
from swcraft_tools.yaml_load import load_yaml_document
from synthetic_support import OVERLAPPING_NESTED_LAYERS, SIBLING_NESTED_LAYERS, nested_registry


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A real project on disk — the vector harness's base model, unseeded."""
    return fixtures.build_fixture(tmp_path / "project", "read-swc-element")


def _manifest(project_root: Path) -> str:
    return str(project_root / "swcraft.yaml")


def _model_root(project_root: Path) -> Path:
    return project_root / "model"


def _tree_state(root: Path) -> dict[str, bytes]:
    """Every file under *root* by relative path — byte-identity, not a digest."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


def _entries(project_root: Path) -> list[dict[str, Any]]:
    """The **real** project's log, in insertion order."""
    return AuditLog(str(project_root / "dist" / AUDIT_FILENAME)).get_entries()


def _records(project_root: Path, operation: str) -> list[dict[str, Any]]:
    return [entry for entry in _entries(project_root) if entry["operation"] == operation]


def _one(project_root: Path, operation: str) -> dict[str, Any]:
    records = _records(project_root, operation)
    assert len(records) == 1, f"expected exactly one {operation} record, got {len(records)}"
    return records[0]


def _request(project_root: Path, *operations: dict[str, Any], baseline: str | None = None) -> dict[str, Any]:
    return {
        "operation": "change_set",
        "baseline": baseline if baseline is not None else baseline_token(_model_root(project_root)),
        "intent": "a test change set",
        "operations": list(operations),
    }


_UPDATE_PRIORITY = {
    "operation": "update",
    "address": {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "priority"}]},
    "value": 6,
}
_UPDATE_WCET = {
    "operation": "update",
    "address": {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "wcet"}]},
    "value": "1300us",
}
_UPDATE_SWC_DESCRIPTION = {
    "operation": "update",
    "address": {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "description"}]},
    "value": "A described example component",
}

_TASK_ELEMENT = ("tasks", "Task_10ms")
_SWC_ELEMENT = ("swc", "ExampleSWC")
_SPEED_KPH = ("implementation_types", "Speed_kph")

#: The seed both former storage-target vectors state: `data_types/Speed_kph.yaml`
#: declaring `VehicleSpeed_kph`, so the plain file name of `Speed_kph` is held.
_VEHICLE_SPEED_KPH = "name: VehicleSpeed_kph\nkind: value_type\nbase: float32_base\nunit: kph\n"
_CREATE_SPEED_KPH = {
    "operation": "create",
    "address": {"layer": "implementation_types", "element": "Speed_kph"},
    "value": {"name": "Speed_kph", "kind": "value_type", "base": "float32_base", "unit": "kph"},
}


def _rename_interface(project_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """``IDiagnosticService``, which nothing references, removed and its stored value created as ``IDiagnostics``.

    The two sides differ in the name alone, so they are one rename.
    """
    value = load_yaml_document(_diagnostic_service_file(project_root).read_text(encoding="utf-8"))
    return (
        {"operation": "remove", "address": {"layer": "interfaces", "element": "IDiagnosticService"}},
        {
            "operation": "create",
            "address": {"layer": "interfaces", "element": "IDiagnostics"},
            "value": {**value, "name": "IDiagnostics"},
        },
    )


def _diagnostic_service_file(project_root: Path) -> Path:
    return _model_root(project_root) / "interfaces" / "IDiagnosticService.yaml"


def _approve_everything(proposal: Any) -> ApprovalDecision:
    """A review boundary that approves the delta and any weakening it carries."""
    return ApprovalDecision(
        change_set_id=proposal.change_set_id,
        baseline=proposal.baseline,
        approved=True,
        safety_approved=True,
    )


def _commit_through(
    project_root: Path, *operations: dict[str, Any], review: Any = _approve_everything
) -> tuple[str, list[dict[str, Any]]]:
    """Drive one change set to a real commit through ``handle_request``.

    Never a hand-sequenced lifecycle: the engine writes the intent record from the
    session's ``before_apply`` hook, and that ordering is what this module tests.

    Returns:
        The change-set id (the response's ``audit_ref``) and the delta as JSON entries.
    """
    response = handle_request(
        _request(project_root, *operations),
        project=project_root,
        approved_scope=None,
        review=review,
    )
    assert response.get("status") == "committed", response
    return response["audit_ref"], response["delta"]


def _scripted_approver(*answers: bool) -> Callable[[str, str], ApprovalDecision]:
    """A host's review boundary, scripted. Fails closed when the script runs out."""
    remaining: Iterator[bool] = iter(answers)

    def approve(change_set_id: str, baseline: str) -> ApprovalDecision:
        return ApprovalDecision(change_set_id=change_set_id, baseline=baseline, approved=next(remaining, False))

    return approve


class TestTheChangeSetId:
    def test_it_has_the_form_the_contract_states(self) -> None:
        assert CHANGE_SET_ID_PATTERN.fullmatch(new_change_set_id())

    def test_two_ids_in_the_same_second_differ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        frozen = datetime(2026, 8, 15, 10, 22, 41, tzinfo=UTC)
        monkeypatch.setattr(audit_mod, "_now", lambda: frozen)
        assert new_change_set_id() != new_change_set_id()

    def test_it_is_timestamp_prefixed_so_a_log_sorts_by_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        moments = iter(
            [
                datetime(2026, 8, 15, 10, 22, 41, tzinfo=UTC),
                datetime(2026, 8, 15, 10, 22, 42, tzinfo=UTC),
                datetime(2026, 8, 16, 9, 0, 0, tzinfo=UTC),
            ]
        )
        monkeypatch.setattr(audit_mod, "_now", lambda: next(moments))
        identifiers = [new_change_set_id() for _ in range(3)]
        assert identifiers == sorted(identifiers)

    def test_only_a_change_set_claims_a_lifecycle(self) -> None:
        assert claims_change_set({"operation": "change_set", "baseline": "rev1:x", "operations": []})
        assert not claims_change_set({"operation": "discover"})
        assert not claims_change_set({"operation": "read", "address": {"layer": "swc"}})
        assert not claims_change_set("model/swc/example/ExampleSWC.yaml")
        assert not claims_change_set(None)


class TestRejectionsBeforeStaging:
    """Each of the three carries an id, which is why it is allocated on receipt."""

    def _rejected(self, project_root: Path) -> dict[str, Any]:
        record = _one(project_root, CHANGE_SET_REJECTED)
        assert CHANGE_SET_ID_PATTERN.fullmatch(record["args"]["change_set_id"])
        return record

    def test_pass_a_refuses_and_the_record_is_identified(self, project: Path) -> None:
        response = fixtures.dispatch(
            {
                "operation": "change_set",
                "baseline": baseline_token(_model_root(project)),
                "intent": "a change set pass A cannot admit",
                "operations": [{"operation": "rename", "address": {"layer": "swc", "element": "ExampleSWC"}}],
            },
            project=project,
            approved_scope=None,
        )
        record = self._rejected(project)
        assert response["error"] == "schema-violation"
        assert record["args"]["error"] == "schema-violation"
        assert record["args"]["failure_class"] == "invalid"
        assert record["result"] == "schema-violation"

    def test_the_fail_fast_staleness_check_records_too(self, project: Path) -> None:
        stale = baseline_token(_model_root(project))
        fixtures.make_stale(project)
        fixtures.dispatch(_request(project, _UPDATE_PRIORITY, baseline=stale), project=project, approved_scope=None)
        record = self._rejected(project)
        assert record["args"]["error"] == "stale-baseline"
        assert record["args"]["failure_class"] == "stale"
        # The token the request carried, not the one the model has now.
        assert record["args"]["baseline"] == stale

    def test_gate_1_at_step_0_records_too(self, project: Path) -> None:
        # An empty approved scope means nothing is approved, unlike unscoped.
        fixtures.dispatch(_request(project, _UPDATE_PRIORITY), project=project, approved_scope=[])
        record = self._rejected(project)
        assert record["args"]["error"] == "unauthorized-delta"
        assert record["args"]["failure_class"] == "unauthorized"
        assert record["args"]["addresses"] == [_UPDATE_PRIORITY["address"]]

    def test_a_baseline_the_request_never_named_is_recorded_as_null(self, project: Path) -> None:
        fixtures.dispatch({"operation": "change_set"}, project=project, approved_scope=None)
        assert self._rejected(project)["args"]["baseline"] is None

    def test_every_violation_the_rejection_names_is_recorded(self, project: Path) -> None:
        fixtures.dispatch(
            _request(
                project,
                {
                    "operation": "update",
                    "address": {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "nonesuch"}]},
                    "value": 1,
                },
                {
                    "operation": "update",
                    "address": {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "name"}]},
                    "value": "Renamed",
                },
            ),
            project=project,
            approved_scope=None,
        )
        record = self._rejected(project)
        assert len(record["args"]["addresses"]) == 2

    def test_discover_and_read_write_no_lifecycle_record(self, project: Path) -> None:
        # Both are unscoped and unaudited, refused or not.
        fixtures.dispatch({"operation": "discover", "layer": "tasks"}, project=project, approved_scope=None)
        fixtures.dispatch(
            {"operation": "read", "address": {"layer": "swc", "element": "ExampleSWC"}},
            project=project,
            approved_scope=None,
        )
        fixtures.dispatch(
            {"operation": "read", "address": "model/swc/example/ExampleSWC.yaml"},
            project=project,
            approved_scope=None,
        )
        fixtures.dispatch({"operation": "rename"}, project=project, approved_scope=None)
        assert _entries(project) == []

    def test_a_project_with_no_log_to_write_to_still_answers_the_refusal(self, tmp_path: Path) -> None:
        # A directory with no manifest has no log; the refusal must not become a
        # FileNotFoundError.
        response = fixtures.dispatch({"operation": "change_set"}, project=tmp_path, approved_scope=None)
        assert response["error"] == "schema-violation"


class TestTheStagedRecord:
    def test_an_accepted_change_set_is_recorded_in_the_real_log(self, project: Path) -> None:
        response = fixtures.dispatch(
            _request(project, _UPDATE_PRIORITY, _UPDATE_SWC_DESCRIPTION),
            project=project,
            approved_scope=None,
        )
        assert response["status"] == "staged"
        record = _one(project, CHANGE_SET_STAGED)
        assert CHANGE_SET_ID_PATTERN.fullmatch(record["args"]["change_set_id"])
        assert record["args"]["baseline"] == response["baseline"]
        assert record["args"]["intent"] == "a test change set"
        assert record["args"]["operations"] == [
            {"operation": "update", "address": _UPDATE_PRIORITY["address"]},
            {"operation": "update", "address": _UPDATE_SWC_DESCRIPTION["address"]},
        ]
        assert record["result"] == f"staged: 2 operation(s), {len(response['delta'])} delta entr(ies)"

    def test_the_record_is_written_although_the_response_carries_no_audit_ref(self, project: Path) -> None:
        # `audit_ref` is omitted on `staged` because it would read as a commit handle.
        response = fixtures.dispatch(_request(project, _UPDATE_PRIORITY), project=project, approved_scope=None)
        assert "audit_ref" not in response
        assert _records(project, CHANGE_SET_STAGED)

    def test_a_scoped_run_records_the_same_way(self, project: Path) -> None:
        response = fixtures.dispatch(
            _request(project, _UPDATE_PRIORITY),
            project=project,
            approved_scope=[{"operation": "update", "address": {"layer": "tasks", "element": "Task_10ms"}}],
        )
        assert response["status"] == "staged"
        assert _records(project, CHANGE_SET_STAGED)

    def test_the_model_is_byte_identical_and_only_the_log_moved(self, project: Path) -> None:
        model_before = _tree_state(_model_root(project))
        fixtures.dispatch(_request(project, _UPDATE_PRIORITY), project=project, approved_scope=None)
        assert _tree_state(_model_root(project)) == model_before
        # The log lives under `dist_root`, so appending to it cannot invalidate the
        # fingerprint the commit re-checks.
        assert (project / "dist" / AUDIT_FILENAME).is_file()


# The four keys, and the fifth


class TestTheEntryShape:
    def test_no_key_is_added_and_none_is_repurposed(self, project: Path) -> None:
        fixtures.dispatch(_request(project, _UPDATE_PRIORITY), project=project, approved_scope=None)
        for entry in _entries(project):
            assert list(entry) == list(AuditLog.ENTRY_KEYS)
            assert entry["args"]["change_set_id"]

    def test_scope_id_is_the_turn_scopes_correlation_id(self, project: Path) -> None:
        # The turn scope's correlation id, beside the change set's own.
        loaded = require_project(_manifest(project))
        auditor = ChangeSetAuditor(project_audit_log(loaded), "cs-20260815T102241Z-4f19c2", scope_id="turn-7")
        auditor.abandoned("the reviewer declined")
        entry = _one(project, CHANGE_SET_ABANDONED)
        assert list(entry) == [*AuditLog.ENTRY_KEYS, "scope_id"]
        assert entry["scope_id"] == "turn-7"
        assert entry["args"]["change_set_id"] == "cs-20260815T102241Z-4f19c2"

    def test_auditor_for_carries_the_hosts_correlation_id(self, project: Path) -> None:
        # The engine threads `handle_request`'s `scope_id` through this factory.
        loaded = require_project(_manifest(project))
        assert auditor_for(loaded, "cs-20260815T102241Z-4f19c2", "turn-7").scope_id == "turn-7"
        assert auditor_for(loaded, "cs-20260815T102241Z-4f19c2").scope_id is None

    def test_the_six_operation_names_are_the_contract(self) -> None:
        assert LIFECYCLE_OPERATIONS == (
            CHANGE_SET_STAGED,
            CHANGE_SET_REJECTED,
            CHANGE_SET_APPROVED,
            CHANGE_SET_COMMIT_INTENT,
            CHANGE_SET_COMMITTED,
            CHANGE_SET_ABANDONED,
        )


# `change_set_approved`: injected, never prompted


class TestTheReviewBoundary:
    def test_a_scripted_approver_produces_both_results(self, project: Path) -> None:
        loaded = require_project(_manifest(project))
        approve = _scripted_approver(True, False)
        baseline = baseline_token(_model_root(project))
        scope = (ScopeEntry(operation="update", address=Address(layer="tasks", element="Task_10ms")),)

        for identifier in ("cs-20260815T102241Z-aaaaaa", "cs-20260815T102242Z-bbbbbb"):
            auditor_for(loaded, identifier).approved(
                approve(identifier, baseline), scope, baseline=baseline, weakening=False
            )

        records = _records(project, CHANGE_SET_APPROVED)
        assert [record["result"] for record in records] == ["approved", "declined"]
        assert [record["args"]["decision"] for record in records] == ["approved", "declined"]
        assert records[0]["args"]["approved_scope"] == [entry.to_json() for entry in scope]

    @pytest.mark.parametrize(
        ("weakening", "approved", "safety_approved", "expected"),
        [
            (True, True, True, "approved"),
            (True, True, False, "declined"),
            # No weakening: the second question was never put; recording the
            # fail-closed `False` as `declined` would log a refusal of a commit.
            (False, True, False, SAFETY_NOT_ASKED),
            (False, True, True, SAFETY_NOT_ASKED),
            # A declined delta puts no second question either.
            (True, False, False, SAFETY_NOT_ASKED),
            (True, False, True, SAFETY_NOT_ASKED),
        ],
        ids=["weakening-approved", "weakening-declined", "none-closed", "none-open", "delta-no", "delta-no-safety-yes"],
    )
    def test_the_second_human_answer_is_recorded_beside_the_first(
        self, project: Path, weakening: bool, approved: bool, safety_approved: bool, expected: str
    ) -> None:
        # The only evidence that the separate safety prompt was answered; a record
        # with `decision` alone left a committed weakening unattested.
        loaded = require_project(_manifest(project))
        baseline = baseline_token(_model_root(project))
        identifier = "cs-20260815T102241Z-aaaaaa"
        decision = ApprovalDecision(
            change_set_id=identifier, baseline=baseline, approved=approved, safety_approved=safety_approved
        )

        auditor_for(loaded, identifier).approved(decision, None, baseline=baseline, weakening=weakening)

        assert _one(project, CHANGE_SET_APPROVED)["args"]["safety_decision"] == expected

    @pytest.mark.parametrize("channel", ["elicit", "panel", None], ids=["elicit", "panel", "unnamed"])
    def test_the_record_names_the_channel_the_answer_came_through(self, project: Path, channel: str | None) -> None:
        # Recorded as the host gives it; `null` when the host named none.
        loaded = require_project(_manifest(project))
        baseline = baseline_token(_model_root(project))
        identifier = "cs-20260815T102241Z-aaaaaa"
        decision = ApprovalDecision(change_set_id=identifier, baseline=baseline, approved=True, channel=channel)

        auditor_for(loaded, identifier).approved(decision, None, baseline=baseline, weakening=False)

        args = _one(project, CHANGE_SET_APPROVED)["args"]
        assert "channel" in args
        assert args["channel"] == channel
        assert list(args) == ["change_set_id", "approved_scope", "decision", "safety_decision", "channel"]

    def test_a_decision_names_no_channel_by_default(self) -> None:
        assert ApprovalDecision(change_set_id="cs", baseline="b", approved=True).channel is None

    def test_the_second_answer_is_a_key_of_the_first_record_not_a_seventh_operation(self) -> None:
        # One review with two answers, not a second event.
        assert SAFETY_NOT_ASKED not in LIFECYCLE_OPERATIONS
        assert len(LIFECYCLE_OPERATIONS) == 6

    def test_an_exhausted_script_declines(self, project: Path) -> None:
        loaded = require_project(_manifest(project))
        approve = _scripted_approver()
        baseline = baseline_token(_model_root(project))
        identifier = "cs-20260815T102241Z-aaaaaa"
        auditor_for(loaded, identifier).approved(
            approve(identifier, baseline), None, baseline=baseline, weakening=False
        )
        record = _one(project, CHANGE_SET_APPROVED)
        assert record["result"] == "declined"
        # `None` (no scope required) stays distinct from `[]` (nothing approved).
        assert record["args"]["approved_scope"] is None

    def test_an_approval_cannot_be_replayed_onto_another_change_set(self, project: Path) -> None:
        loaded = require_project(_manifest(project))
        baseline = baseline_token(_model_root(project))
        decision = ApprovalDecision(
            change_set_id="cs-20260815T102241Z-aaaaaa",
            baseline=baseline,
            approved=True,
        )
        with pytest.raises(ApprovalMismatchError):
            auditor_for(loaded, "cs-20260815T102242Z-bbbbbb").approved(
                decision, None, baseline=baseline, weakening=False
            )
        assert _records(project, CHANGE_SET_APPROVED) == []

    def test_an_approval_cannot_be_replayed_onto_a_re_authored_change_set(self, project: Path) -> None:
        # Same id, moved model.
        loaded = require_project(_manifest(project))
        identifier = "cs-20260815T102241Z-aaaaaa"
        decision = ApprovalDecision(change_set_id=identifier, baseline="rev1:aaaaaaaaaaaaaaaa", approved=True)
        with pytest.raises(ApprovalMismatchError):
            auditor_for(loaded, identifier).approved(decision, None, baseline="rev1:bbbbbbbbbbbbbbbb", weakening=False)
        assert _records(project, CHANGE_SET_APPROVED) == []


class TestTheCommitIntentRecord:
    def test_it_is_written_before_the_first_os_replace(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        seen_at_first_replace: list[list[dict[str, Any]]] = []
        original = os.replace

        def recording_replace(source: Any, target: Any) -> None:
            if not seen_at_first_replace:
                seen_at_first_replace.append(_entries(project))
            original(source, target)

        monkeypatch.setattr(session_mod.os, "replace", recording_replace)
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)

        assert seen_at_first_replace, "the apply never reached an os.replace"
        operations = [entry["operation"] for entry in seen_at_first_replace[0]]
        assert CHANGE_SET_COMMIT_INTENT in operations
        assert CHANGE_SET_COMMITTED not in operations
        assert {entry["args"]["change_set_id"] for entry in seen_at_first_replace[0]} == {change_set_id}

    def test_it_carries_the_baseline_and_the_reviewed_deltas_addresses(self, project: Path) -> None:
        change_set_id, delta = _commit_through(project, _UPDATE_PRIORITY, _UPDATE_SWC_DESCRIPTION)
        record = _one(project, CHANGE_SET_COMMIT_INTENT)
        assert record["result"] == "intent"
        assert record["args"]["change_set_id"] == change_set_id
        assert record["args"]["baseline"].startswith("rev1:")
        assert record["args"]["addresses"] == [entry["address"] for entry in delta]

    def test_a_failure_to_write_it_is_pre_apply(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Only the intent record fails: breaking the whole log file would refuse at
        # `change_set_staged`, before the commit is reached.
        before = _tree_state(_model_root(project))
        original = audit_mod.ChangeSetAuditor._append

        def failing(self: Any, operation: str, args: dict[str, Any], result: str) -> None:
            if operation == CHANGE_SET_COMMIT_INTENT:
                raise OSError(28, "No space left on device")
            original(self, operation, args, result)

        monkeypatch.setattr(audit_mod.ChangeSetAuditor, "_append", failing)

        with pytest.raises(CommitPreflightError):
            _commit_through(project, _UPDATE_PRIORITY)

        assert _tree_state(_model_root(project)) == before
        assert not _records(project, CHANGE_SET_COMMITTED)


# The commit block and its completeness

#: The other change set in the interleaving tests below.
_FOREIGN_ID = "cs-20260815T102242Z-bbbbbb"


def _foreign_staged_record() -> dict[str, Any]:
    """Another change set's `change_set_staged`, shaped as `ChangeSetAuditor` writes it."""
    return {
        "timestamp": "2026-08-15T10:22:42Z",
        "operation": CHANGE_SET_STAGED,
        "args": {"change_set_id": _FOREIGN_ID, "baseline": "rev1:aaaaaaaaaaaaaaaa", "intent": "another change set"},
        "result": "staged: 1 operation(s), 1 delta entr(ies)",
    }


def _foreign_writer_record() -> dict[str, Any]:
    """A replayed writer entry (no `change_set_id`) for an element this block never committed."""
    return {
        "timestamp": "2026-08-15T10:22:42Z",
        "operation": "write_element",
        "args": {"target": "swc/example/ExampleSWC.yaml"},
        "result": "swc/example/ExampleSWC.yaml",
    }


def _host_record(operation: str) -> dict[str, Any]:
    """One of the agent host's three records, shaped as ``core.py::SwcraftAgent._record`` writes it.

    No ``change_set_id``, appended under no lock, so one can land inside a commit block.
    """
    return {
        "timestamp": "2026-08-15T10:22:42Z",
        "operation": operation,
        "args": {"tool": "generate_arxml", "code": "export-path", "target": "/tmp/elsewhere"},
        "result": "REFUSED: the output path is outside the project",
        "scope_id": "1f2e3d4c",
    }


def _legacy_writer_record() -> dict[str, Any]:
    """A `mutations.py` writer entry: a model write from outside the protocol, into the same log."""
    return {
        "timestamp": "2026-08-15T10:22:42Z",
        "operation": "create_swc",
        "args": {"name": "SmuggledSWC", "group": "chassis"},
        "result": "model/swc/chassis/SmuggledSWC.yaml",
    }


def _spliced(entries: list[dict[str, Any]], change_set_id: str, record: dict[str, Any]) -> list[dict[str, Any]]:
    """*entries* with *record* inserted immediately after *change_set_id*'s intent record."""
    position = next(
        index
        for index, entry in enumerate(entries)
        if entry["operation"] == CHANGE_SET_COMMIT_INTENT and entry["args"].get("change_set_id") == change_set_id
    )
    return [*entries[: position + 1], record, *entries[position + 1 :]]


def _forged_completion(block: Any, addresses: list[Any]) -> Any:
    """*block* with its completion record's recorded addresses replaced."""
    return type(block)(
        change_set_id=block.change_set_id,
        intent=block.intent,
        committed={**block.committed, "args": {**block.committed["args"], "addresses": addresses}},
        entries=block.entries,
    )


def _with_writer_args(block: Any, args: dict[str, Any]) -> Any:
    """*block* with its one writer entry's ``args`` replaced by *args*."""
    (entry,) = block.entries
    return type(block)(
        change_set_id=block.change_set_id,
        intent=block.intent,
        committed=block.committed,
        entries=({**entry, "args": args},),
    )


class TestTheCommitBlock:
    def test_it_is_contiguous_and_its_delimiters_carry_the_id(self, project: Path) -> None:
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        entries = _entries(project)
        block = commit_block(entries, change_set_id)
        assert block is not None
        start = entries.index(dict(block.intent))
        assert [entry["operation"] for entry in entries[:start]] == [CHANGE_SET_STAGED, CHANGE_SET_APPROVED]
        assert entries.index(dict(block.committed)) == len(entries) - 1
        assert [entry["operation"] for entry in block.entries] == ["write_element"]

    def test_the_replayed_writer_entries_keep_their_four_keys_verbatim(self, project: Path) -> None:
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        block = commit_block(_entries(project), change_set_id)
        assert block is not None
        for entry in block.entries:
            assert set(entry) <= {*AuditLog.ENTRY_KEYS, "scope_id"}
            assert "change_set_id" not in entry["args"]

    def test_the_completion_record_states_the_two_tokens_and_the_file_count(self, project: Path) -> None:
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        record = _one(project, CHANGE_SET_COMMITTED)
        assert record["args"]["change_set_id"] == change_set_id
        assert record["args"]["baseline_before"] != record["args"]["baseline_after"]
        assert record["args"]["baseline_after"] == baseline_token(_model_root(project))
        assert record["result"] == "1 file(s)"

    def test_two_change_sets_do_not_bleed_into_each_others_blocks(self, project: Path) -> None:
        first, _ = _commit_through(project, _UPDATE_PRIORITY)
        second, _ = _commit_through(project, _UPDATE_SWC_DESCRIPTION)
        entries = _entries(project)
        first_block = commit_block(entries, first)
        second_block = commit_block(entries, second)
        assert first_block is not None and second_block is not None
        assert written_element_addresses(first_block) == frozenset({_TASK_ELEMENT})
        assert written_element_addresses(second_block) == frozenset({_SWC_ELEMENT})
        assert block_is_complete(first_block) and block_is_complete(second_block)

    def test_another_change_sets_lifecycle_record_in_the_span_is_not_part_of_the_block(self, project: Path) -> None:
        # Staging is outside `commit_lock` (a lock would be held across a human's
        # prompt), so a concurrent `change_set_staged` can land between the
        # delimiters; it names its own change set and is excluded.
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        entries = _spliced(_entries(project), change_set_id, _foreign_staged_record())

        block = commit_block(entries, change_set_id)
        assert block is not None
        assert [entry["operation"] for entry in block.entries] == ["write_element"]
        assert block_is_complete(block)
        # Excluded from the block, not lost from the log.
        assert commit_block(entries, _FOREIGN_ID) is None
        foreign = [entry for entry in entries if entry["args"].get("change_set_id") == _FOREIGN_ID]
        assert [entry["operation"] for entry in foreign] == [CHANGE_SET_STAGED]

    def test_another_change_sets_writer_entry_in_the_span_is_kept_and_breaks_the_equality(self, project: Path) -> None:
        # A writer entry carries no change-set id, so a foreign one in the span is
        # the unaccounted write the completeness check exists to surface.
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        entries = _spliced(_entries(project), change_set_id, _foreign_writer_record())

        block = commit_block(entries, change_set_id)
        assert block is not None
        assert [entry["operation"] for entry in block.entries] == ["write_element", "write_element"]
        assert written_element_addresses(block) == frozenset({_TASK_ELEMENT, _SWC_ELEMENT})
        assert committed_element_addresses(block) == frozenset({_TASK_ELEMENT})
        assert not block_is_complete(block)

    def test_a_lifecycle_record_carrying_this_blocks_own_id_stays_visible(self, project: Path) -> None:
        # The filter is by id: an own lifecycle record inside the block would be an
        # engine defect, so it stays visible and the block is not complete.
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        own = dict(_foreign_staged_record(), args={"change_set_id": change_set_id})
        entries = _spliced(_entries(project), change_set_id, own)

        block = commit_block(entries, change_set_id)
        assert block is not None
        assert [entry["operation"] for entry in block.entries] == [CHANGE_SET_STAGED, "write_element"]
        assert not block_is_complete(block)

    @pytest.mark.parametrize("operation", sorted(HOST_OPERATIONS))
    def test_a_host_record_in_the_span_is_not_part_of_the_block(self, project: Path, operation: str) -> None:
        # Host records carry no `change_set_id` and are no writes: they are
        # excluded from `entries` by name.
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        entries = _spliced(_entries(project), change_set_id, _host_record(operation))

        block = commit_block(entries, change_set_id)
        assert block is not None
        assert [entry["operation"] for entry in block.entries] == ["write_element"]
        assert block_is_complete(block)
        # Excluded from the block, not lost: its `scope_id` is untouched.
        host = [entry for entry in entries if entry["operation"] == operation]
        assert [entry.get("scope_id") for entry in host] == ["1f2e3d4c"]

    def test_a_record_no_declared_party_claims_stays_in_the_block_and_refuses_it(self, project: Path) -> None:
        # Why host records are excluded by name, not as "neither writer nor
        # lifecycle": a `mutations.py` write in the span is an unaccounted write.
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        entries = _spliced(_entries(project), change_set_id, _legacy_writer_record())

        block = commit_block(entries, change_set_id)
        assert block is not None
        assert [entry["operation"] for entry in block.entries] == ["create_swc", "write_element"]
        assert not block_is_complete(block)

    def test_an_intent_with_no_completion_is_the_crash_signature(self, project: Path) -> None:
        loaded = require_project(_manifest(project))
        auditor = auditor_for(loaded, "cs-20260815T102241Z-4f19c2")
        auditor.commit_intent(baseline=baseline_token(_model_root(project)), delta=())
        entries = _entries(project)
        assert incomplete_commits(entries) == ("cs-20260815T102241Z-4f19c2",)
        # Not a partial block: entries after an unterminated intent are not known to be its.
        assert commit_block(entries, "cs-20260815T102241Z-4f19c2") is None

    def test_a_completed_commit_leaves_no_signature(self, project: Path) -> None:
        _commit_through(project, _UPDATE_PRIORITY)
        assert incomplete_commits(_entries(project)) == ()


class TestACorruptedLogLine:
    """A hand-edited or truncated JSONL line makes its block incomplete, never invisible or an exception."""

    def _corrupted_block(self, project_root: Path, change_set_id: str, record: Any) -> Any:
        block = commit_block(_spliced(_entries(project_root), change_set_id, record), change_set_id)
        assert block is not None
        return block

    def test_an_unhashable_operation_is_kept_and_refuses_the_block(self, project: Path) -> None:
        # A list `operation` refuses the block; no `TypeError: unhashable type` escapes `commit_block`.
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        corrupted = {
            "timestamp": "2026-08-15T10:22:42Z",
            "operation": [],
            "args": {"target": "tasks/Task_10ms.yaml"},
            "result": "?",
        }
        block = self._corrupted_block(project, change_set_id, corrupted)
        assert [type(entry["operation"]).__name__ for entry in block.entries] == ["list", "str"]
        assert not block_is_complete(block)

    @pytest.mark.parametrize("record", [None, [], "a truncated line", 17])
    def test_a_line_that_is_not_an_object_is_kept_and_refuses_the_block(self, project: Path, record: Any) -> None:
        # `AuditLog.get_entries` returns whatever `json.loads` made of each line.
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        entries = _spliced(_entries(project), change_set_id, record)
        assert incomplete_commits(entries) == ()
        block = self._corrupted_block(project, change_set_id, record)
        assert len(block.entries) == 2
        assert not block_is_complete(block)

    def test_a_corrupted_line_does_not_hide_another_change_sets_crash_signature(self, project: Path) -> None:
        # One bad line must not cost every change set's crash signature in the file.
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        entries = [
            *_spliced(_entries(project), change_set_id, {"operation": [], "args": {}}),
            {"operation": CHANGE_SET_COMMIT_INTENT, "args": {"change_set_id": "cs-20260815102242-abc123"}},
        ]
        assert incomplete_commits(entries) == ("cs-20260815102242-abc123",)


class TestDeltaAgainstWriterEntriesCompleteness:
    """The delta-versus-writer-entries completeness check, at element level."""

    def _block_of(self, project_root: Path, change_set_id: str) -> Any:
        block = commit_block(_entries(project_root), change_set_id)
        assert block is not None
        return block

    def test_a_single_field_change(self, project: Path) -> None:
        change_set_id, delta = _commit_through(project, _UPDATE_PRIORITY)
        block = self._block_of(project, change_set_id)
        assert len(delta) == 1
        assert committed_element_addresses(block) == frozenset({_TASK_ELEMENT})
        assert written_element_addresses(block) == frozenset({_TASK_ELEMENT})
        assert block_is_complete(block)

    def test_a_line_edit_keeps_the_block_complete_and_records_its_form(self, project: Path) -> None:
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        block = self._block_of(project, change_set_id)

        assert block.entries[0]["args"]["form"] == "lines"
        assert "reason" not in block.entries[0]["args"]
        assert written_element_addresses(block) == frozenset({_TASK_ELEMENT})
        assert block_is_complete(block)

    def test_a_fallback_keeps_the_block_complete_and_records_its_reason(self, project: Path) -> None:
        task = _model_root(project) / "tasks" / "Task_10ms.yaml"
        task.write_bytes(task.read_bytes().replace(b"\n", b"\r\n", 1))

        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        block = self._block_of(project, change_set_id)

        assert block.entries[0]["args"]["form"] == "whole"
        assert block.entries[0]["args"]["reason"] == "the file has mixed line ends"
        assert written_element_addresses(block) == frozenset({_TASK_ELEMENT})
        assert block_is_complete(block)

    def test_a_create_records_no_form(self, project: Path) -> None:
        change_set_id, _ = _commit_through(project, _CREATE_SPEED_KPH)
        block = self._block_of(project, change_set_id)

        assert "form" not in block.entries[0]["args"]
        assert block_is_complete(block)

    def test_a_carried_rename_records_its_create_form(self, project: Path) -> None:
        # A paired rename's create entry carries `form` as an update's does; its removal's none.
        stored = _diagnostic_service_file(project)
        stored.write_bytes(b"# a note\n" + stored.read_bytes())

        change_set_id, _ = _commit_through(project, *_rename_interface(project))
        block = self._block_of(project, change_set_id)

        args = {entry["operation"]: entry["args"] for entry in block.entries}
        assert args["write_element"]["identity"] == "IDiagnostics"
        assert args["write_element"]["form"] == "lines"
        assert "reason" not in args["write_element"]
        assert "form" not in args["remove_element"]
        assert block_is_complete(block)
        assert (_model_root(project) / "interfaces" / "IDiagnostics.yaml").read_bytes().startswith(b"# a note\n")

    def test_a_rename_written_whole_records_its_form_and_reason(self, project: Path) -> None:
        stored = _diagnostic_service_file(project)
        stored.write_bytes(stored.read_bytes().replace(b"\n", b"\r\n", 1))

        change_set_id, _ = _commit_through(project, *_rename_interface(project))
        block = self._block_of(project, change_set_id)

        (write,) = [entry for entry in block.entries if entry["operation"] == "write_element"]
        assert write["args"]["form"] == "whole"
        assert write["args"]["reason"] == "the file has mixed line ends"
        assert block_is_complete(block)

    def test_a_multi_field_change_inside_one_element(self, project: Path) -> None:
        # The delta is per leaf, the writer emits one entry per touched element.
        change_set_id, delta = _commit_through(project, _UPDATE_PRIORITY, _UPDATE_WCET)
        block = self._block_of(project, change_set_id)
        assert len(delta) == 2
        assert len(block.entries) == 1
        assert committed_element_addresses(block) == frozenset({_TASK_ELEMENT})
        assert block_is_complete(block)

    def test_a_multi_element_change_set(self, project: Path) -> None:
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY, _UPDATE_WCET, _UPDATE_SWC_DESCRIPTION)
        block = self._block_of(project, change_set_id)
        assert len(block.entries) == 2
        assert committed_element_addresses(block) == frozenset({_TASK_ELEMENT, _SWC_ELEMENT})
        assert block_is_complete(block)

    def test_a_removal_is_covered_by_the_same_equality(self, project: Path) -> None:
        # A sequence: nothing references one, so removing it leaves no finding
        # (removing a requirement would commit with two warnings).
        change_set_id, _ = _commit_through(
            project,
            {"operation": "remove", "address": {"layer": "sequences", "element": "task10ms_consumer_cycle"}},
        )
        block = self._block_of(project, change_set_id)
        assert [entry["operation"] for entry in block.entries] == ["remove_element"]
        assert committed_element_addresses(block) == frozenset({("sequences", "task10ms_consumer_cycle")})
        assert block_is_complete(block)

    def test_a_second_writer_entry_for_an_already_committed_element_is_not_complete(self, project: Path) -> None:
        # A second entry for an accounted element is a foreign write that a set
        # equality would absorb; counted per entry, it cannot hide.
        change_set_id, delta = _commit_through(project, _UPDATE_PRIORITY)
        assert len(delta) == 1
        duplicate = {
            "timestamp": "2026-08-15T10:22:42Z",
            "operation": "write_element",
            "args": {"target": "tasks/Task_10ms.yaml"},
            "result": "tasks/Task_10ms.yaml",
        }
        block = commit_block(_spliced(_entries(project), change_set_id, duplicate), change_set_id)
        assert block is not None
        assert [entry["operation"] for entry in block.entries] == ["write_element", "write_element"]
        assert not block_is_complete(block)

    @pytest.mark.parametrize(
        ("label", "args"),
        [
            ("a target no layer stores", {"target": "somewhere/else/Thing.txt"}),
            ("a target outside every storage shape", {"target": "tasks/nested/Task_10ms.yaml"}),
            ("no target at all", {}),
            ("a target that is not a string", {"target": 17}),
            # The writer refuses the composed segment `A..yaml`, so no writer
            # could have logged this entry; it never classifies as ("tasks", "A.").
            ("a target the writer refuses but the composer produced", {"target": "tasks/A..yaml"}),
        ],
    )
    def test_a_writer_entry_the_check_cannot_classify_is_not_complete(
        self, project: Path, label: str, args: dict[str, Any]
    ) -> None:
        # An unmappable write is refused, never counted as no write.
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        unclassifiable = {
            "timestamp": "2026-08-15T10:22:42Z",
            "operation": "write_element",
            "args": args,
            "result": "?",
        }
        block = commit_block(_spliced(_entries(project), change_set_id, unclassifiable), change_set_id)
        assert block is not None
        assert len(block.entries) == 2, label
        assert not block_is_complete(block), label

    def test_the_reported_address_set_forgives_what_the_check_refuses(self, project: Path) -> None:
        # The reporting function forgives the unmappable entry; the check must not,
        # so `block_is_complete` is never `committed == written`.
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        unmappable = {
            "timestamp": "2026-08-15T10:22:42Z",
            "operation": "write_element",
            "args": {"target": "somewhere/else/Thing.txt"},
            "result": "?",
        }
        block = commit_block(_spliced(_entries(project), change_set_id, unmappable), change_set_id)
        assert block is not None
        assert written_element_addresses(block) == frozenset({_TASK_ELEMENT})
        assert committed_element_addresses(block) == written_element_addresses(block)
        assert not block_is_complete(block)

    @pytest.mark.parametrize(
        ("label", "address"),
        [
            ("no element at all", {"layer": "swc"}),
            ("a null element", {"layer": "swc", "element": None}),
            ("a storage path where an address belongs", "swc/example/ExampleSWC.yaml"),
            ("an item that is not an object", 17),
        ],
    )
    def test_a_committed_address_the_check_cannot_classify_is_not_complete(
        self, project: Path, label: str, address: Any
    ) -> None:
        # The committed side: dropping an unclassifiable address would let a
        # forged record's valid subset pass. Refuse, never omit.
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        block = self._block_of(project, change_set_id)
        forged = _forged_completion(block, [*block.committed["args"]["addresses"], address])
        assert not block_is_complete(forged), label
        assert committed_element_addresses(forged) == frozenset({_TASK_ELEMENT}), label

    @pytest.mark.parametrize(
        ("label", "address"),
        [
            ("an element that is no path segment", {"layer": "swc", "element": "a/b"}),
            ("an element the segment guard refuses", {"layer": "swc", "element": ".."}),
            ("an element that is not a string", {"layer": "swc", "element": 17}),
            ("an element past the segment byte bound", {"layer": "swc", "element": "x" * 199}),
            ("a layer this registry does not declare", {"layer": "somewhere", "element": "ExampleSWC"}),
            ("no layer at all", {"element": "ExampleSWC"}),
        ],
    )
    def test_a_committed_address_no_composition_could_have_produced_is_not_reported(
        self, project: Path, label: str, address: dict[str, Any]
    ) -> None:
        # No composition could produce these, so they are never reported as
        # committed elements.
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        block = self._block_of(project, change_set_id)
        forged = _forged_completion(block, [*block.committed["args"]["addresses"], address])
        assert committed_element_addresses(forged) == frozenset({_TASK_ELEMENT}), label
        assert not block_is_complete(forged), label

    @pytest.mark.parametrize(
        ("label", "address"),
        [
            ("a path that is not an array", {"layer": "swc", "element": "ExampleSWC", "path": "not-a-list"}),
            ("a path spelled as an explicit null", {"layer": "swc", "element": "ExampleSWC", "path": None}),
            ("an empty path (minItems: 1)", {"layer": "swc", "element": "ExampleSWC", "path": []}),
            ("a path step that is not an object", {"layer": "swc", "element": "ExampleSWC", "path": [17]}),
            ("a path step with no known form", {"layer": "swc", "element": "ExampleSWC", "path": [{"index": 0}]}),
            (
                "a path step carrying two forms",
                {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "ports", "key": "x"}]},
            ),
            (
                "an extra key (additionalProperties: false)",
                {"layer": "swc", "element": "ExampleSWC", "storage": "swc/example/ExampleSWC.yaml"},
            ),
            ("a path without an element (dependentRequired)", {"layer": "swc", "path": [{"field": "ports"}]}),
        ],
    )
    def test_a_committed_address_no_delta_entry_could_have_emitted_is_not_reported(
        self, project: Path, label: str, address: Any
    ) -> None:
        # Checking `layer` and `element` alone is not enough; `$defs/address`
        # refuses each of these, and the reader parses with `Address.from_json`.
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        block = self._block_of(project, change_set_id)
        forged = _forged_completion(block, [*block.committed["args"]["addresses"], address])
        assert committed_element_addresses(forged) == frozenset({_TASK_ELEMENT}), label
        assert not block_is_complete(forged), label

    def test_a_field_level_address_still_classifies_as_its_element(self, project: Path) -> None:
        # Parsing the whole address must keep the truncation to `{layer, element}`.
        legal = {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "runnables"}, {"member": "Run"}]}
        assert audit_mod._recorded_element_address(legal) == _SWC_ELEMENT
        assert audit_mod._recorded_element_address({"layer": "swc", "element": "ExampleSWC"}) == _SWC_ELEMENT

    def test_a_completion_record_with_no_readable_delta_is_not_complete(self, project: Path) -> None:
        # An unreadable delta beside no writer entries is not a commit of nothing.
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        block = self._block_of(project, change_set_id)
        without_addresses = {key: value for key, value in block.committed["args"].items() if key != "addresses"}
        for args in (without_addresses, {**without_addresses, "addresses": "tasks/Task_10ms.yaml"}):
            forged = type(block)(
                change_set_id=block.change_set_id,
                intent=block.intent,
                committed={**block.committed, "args": args},
                entries=(),
            )
            assert committed_element_addresses(forged) == frozenset()
            assert not block_is_complete(forged), args

    def test_every_address_an_ordinary_commit_records_classifies(self, project: Path) -> None:
        # A fail-closed check must not refuse legitimate commits.
        change_set_id, delta = _commit_through(project, _UPDATE_PRIORITY, _UPDATE_WCET, _UPDATE_SWC_DESCRIPTION)
        block = self._block_of(project, change_set_id)
        recorded = block.committed["args"]["addresses"]
        assert len(recorded) == len(delta) == 3
        assert all("path" in address for address in recorded)
        # `block_is_complete`'s private reader takes the registry explicitly.
        assert audit_mod._committed_addresses(block, reg.current()) == [_SWC_ELEMENT, _TASK_ELEMENT, _TASK_ELEMENT]
        assert block_is_complete(block)

    # -- the address beside the target ------------------------------------------

    def test_a_writer_entry_records_the_address_beside_the_target(self, project: Path) -> None:
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        block = self._block_of(project, change_set_id)
        assert [dict(entry["args"]) for entry in block.entries] == [
            {"target": "tasks/Task_10ms.yaml", "layer": "tasks", "identity": "Task_10ms", "form": "lines"}
        ]

    def test_a_commit_that_took_a_collision_suffix_is_complete_at_the_address_it_wrote(self, project: Path) -> None:
        # The suffixed target composes back to `Speed_kph~1`, the wrong address;
        # the recorded address is what the block is judged by.
        (_model_root(project) / "data_types" / "implementation" / "Speed_kph.yaml").write_text(
            _VEHICLE_SPEED_KPH, encoding="utf-8"
        )
        change_set_id, _ = _commit_through(project, _CREATE_SPEED_KPH)
        block = self._block_of(project, change_set_id)
        assert [dict(entry["args"]) for entry in block.entries] == [
            {
                "target": "data_types/implementation/Speed_kph~1.yaml",
                "layer": "implementation_types",
                "identity": "Speed_kph",
            }
        ]
        assert written_element_addresses(block) == committed_element_addresses(block) == {_SPEED_KPH}
        assert block_is_complete(block)

    def test_a_removal_from_a_suffixed_file_is_complete_at_the_address_it_removed(self, project: Path) -> None:
        # `remove_element` holds a path and no element, so staging passes the address.
        types = _model_root(project) / "data_types" / "implementation"
        (types / "Speed_kph~1.yaml").write_text(
            "name: Speed_kph\nkind: value_type\nbase: float32_base\nunit: kph\n", encoding="utf-8"
        )
        change_set_id, _ = _commit_through(
            project, {"operation": "remove", "address": {"layer": "implementation_types", "element": "Speed_kph"}}
        )
        block = self._block_of(project, change_set_id)
        assert [entry["operation"] for entry in block.entries] == ["remove_element"]
        assert dict(block.entries[0]["args"]) == {
            "target": "data_types/implementation/Speed_kph~1.yaml",
            "layer": "implementation_types",
            "identity": "Speed_kph",
        }
        assert not (types / "Speed_kph~1.yaml").exists()
        assert written_element_addresses(block) == {_SPEED_KPH}
        assert block_is_complete(block)

    def test_a_record_written_before_the_address_was_recorded_resolves_through_the_composer(
        self, project: Path
    ) -> None:
        # An entry with a target and no address: its target composes back to
        # `(layer, identity)`.
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        block = self._block_of(project, change_set_id)
        old_shape = _with_writer_args(block, {"target": "tasks/Task_10ms.yaml"})
        assert written_element_addresses(old_shape) == {_TASK_ELEMENT}
        assert block_is_complete(old_shape)

    def test_a_suffixed_target_with_no_address_is_incomplete_because_its_composed_address_is_not_the_deltas(
        self, project: Path
    ) -> None:
        # The fallback parses no suffix out of a file name, because `~` and digits
        # may end a real identity; the block is incomplete, fail-closed.
        (_model_root(project) / "data_types" / "implementation" / "Speed_kph.yaml").write_text(
            _VEHICLE_SPEED_KPH, encoding="utf-8"
        )
        change_set_id, _ = _commit_through(project, _CREATE_SPEED_KPH)
        block = self._block_of(project, change_set_id)
        stripped = _with_writer_args(block, {"target": "data_types/implementation/Speed_kph~1.yaml"})
        assert written_element_addresses(stripped) == {("implementation_types", "Speed_kph~1")}
        assert committed_element_addresses(stripped) == {_SPEED_KPH}
        assert not block_is_complete(stripped)

    @pytest.mark.parametrize(
        ("label", "args"),
        [
            ("a layer the target is not stored in", {"layer": "swc", "identity": "Task_10ms"}),
            ("a layer this registry does not declare", {"layer": "somewhere", "identity": "Task_10ms"}),
            ("an identity that is no path segment", {"layer": "tasks", "identity": "a/b"}),
            ("an identity that is not a string", {"layer": "tasks", "identity": 17}),
            ("an identity past the segment byte bound", {"layer": "tasks", "identity": "x" * 199}),
            ("a layer with no identity", {"layer": "tasks"}),
            ("an identity with no layer", {"identity": "Task_10ms"}),
            ("a null layer", {"layer": None, "identity": "Task_10ms"}),
            (
                "a target no layer stores",
                {"target": "elsewhere/Task_10ms.yaml", "layer": "tasks", "identity": "Task_10ms"},
            ),
        ],
    )
    def test_a_recorded_address_the_entry_cannot_support_is_not_classified(
        self, project: Path, label: str, args: dict[str, Any]
    ) -> None:
        # The layer must be declared and hold the target, the identity storable,
        # and a record with one key alone is unreadable, never a target fallback.
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        block = self._block_of(project, change_set_id)
        forged = _with_writer_args(block, {"target": "tasks/Task_10ms.yaml", **args})
        assert written_element_addresses(forged) == frozenset(), label
        assert not block_is_complete(forged), label

    def test_a_missing_writer_entry_breaks_the_equality(self, project: Path) -> None:
        # The check must be able to fail: a dropped writer entry is lost evidence.
        change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
        block = self._block_of(project, change_set_id)
        stripped = type(block)(
            change_set_id=block.change_set_id,
            intent=block.intent,
            committed=block.committed,
            entries=(),
        )
        assert not block_is_complete(stripped)


class TestTheElementAddressOfATarget:
    def test_a_flat_layer(self) -> None:
        assert element_address_of_target("tasks/Task_10ms.yaml") == _TASK_ELEMENT

    def test_the_one_nested_layer(self) -> None:
        # The placement component is dropped, not recovered.
        assert element_address_of_target("swc/example/ExampleSWC.yaml") == _SWC_ELEMENT

    @pytest.mark.parametrize(
        "target",
        [
            "ExampleSWC.yaml",  # no layer directory
            "dist/audit.jsonl",  # not a model layer
            "tasks/nested/Task_10ms.yaml",  # a flat layer does not nest
            "swc/ExampleSWC.yaml",  # the nested layer always does
            "tasks/Task_10ms.json",  # not the layer's suffix
        ],
    )
    def test_a_path_no_layer_stores_is_not_an_address(self, target: str) -> None:
        assert element_address_of_target(target) is None

    @pytest.mark.parametrize(
        ("label", "target"),
        [
            ("a placement is one directory, not two", "swc/example/nested/ExampleSWC.yaml"),
            ("a placement is one directory, not three", "swc/a/b/c/ExampleSWC.yaml"),
            ("an empty identity", "tasks/.yaml"),
            ("an empty placement", "swc//ExampleSWC.yaml"),
            ("a whitespace identity", "tasks/ .yaml"),
            ("'..' inside the identity", "tasks/a..b.yaml"),
            ("'..' as the placement", "swc/../ExampleSWC.yaml"),
            ("a control character in the identity", "tasks/Task\n10ms.yaml"),
            ("an identity past the segment byte bound", "tasks/" + "x" * 199 + ".yaml"),
            # `A.` is a legal identity, but the writer applies the segment rule to
            # the composed name `A..yaml` and refuses it.
            ("a trailing dot composing '..' against the suffix", "tasks/A..yaml"),
            ("a trailing dot in the one nested layer", "swc/example/A..yaml"),
        ],
    )
    def test_a_path_the_registry_could_not_have_composed_is_not_an_address(self, label: str, target: str) -> None:
        # A target `compose_path` could not produce is a hand-edited or forged
        # line, which must not satisfy `block_is_complete`.
        assert element_address_of_target(target) is None, label

    @pytest.mark.parametrize("layer", reg.REGISTRY.layer_names)
    def test_every_path_the_registry_composes_maps_back(self, layer: str) -> None:
        # Derived per declared layer, so a new layer is covered when declared.
        placement = {reg.REGISTRY.storage(layer).nesting: "a_placement"}
        target = reg.REGISTRY.compose_path(layer, "An_Element", placement)
        assert element_address_of_target(target) == (layer, "An_Element")

    @pytest.mark.parametrize("group", ["", "."])
    def test_a_component_stored_with_no_group_is_not_classified_by_its_target(self, group: str) -> None:
        # The unguarded composer spells both groups as the bare file, which fails
        # the depth check; `compose_path` refuses both. No writer records such a
        # target: the protocol refuses both groups before storage is asked.
        record = reg.REGISTRY.storage("swc")
        assert compose_storage_path(record, "ExampleSWC", {"group": group}) == "swc/ExampleSWC.yaml"
        assert element_address_of_target("swc/ExampleSWC.yaml") is None
        assert element_address_of_target(f"swc/{group}/ExampleSWC.yaml") is None
        with pytest.raises(reg.PathSegmentError):
            reg.REGISTRY.compose_path("swc", "ExampleSWC", {"group": group})


class TestTheLongestDeclaredDirectoryNamesTheLayer:
    """A target's layer is the longest declared directory prefixing it; the depth test applies to the rest.

    A layer inside another's directory is refused at build, so that longest match is
    asserted over :func:`longest_directory_match` itself.
    """

    def test_a_layer_inside_another_layers_directory_is_refused_at_build(self) -> None:
        with pytest.raises(ModelRegistryError, match="may not be a prefix of another's"):
            nested_registry(*OVERLAPPING_NESTED_LAYERS)

    @pytest.fixture
    def siblings(self) -> reg.Registry:
        return reg.current(nested_registry(*SIBLING_NESTED_LAYERS))

    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            ("synthetic/alpha/Foo.yaml", ("synthetic/alpha", "Foo.yaml")),
            ("synthetic/Foo.yaml", ("synthetic", "Foo.yaml")),
            # A flat element whose name is the nested directory's last segment.
            ("synthetic/alpha.yaml", ("synthetic", "alpha.yaml")),
            # The longer directory still wins; its remainder is never handed to
            # the shorter one as a placement.
            ("synthetic/alpha/sub/Foo.yaml", ("synthetic/alpha", "sub/Foo.yaml")),
            ("synthetic/alpha/", ("synthetic/alpha", "")),
            ("synthetic/alphabet/Foo.yaml", ("synthetic", "alphabet/Foo.yaml")),
        ],
    )
    def test_a_directory_and_one_inside_it_match_the_longest(
        self, target: str, expected: tuple[str, str] | None
    ) -> None:
        assert longest_directory_match(("synthetic", "synthetic/alpha"), target) == expected

    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            ("nest/alpha/Foo.yaml", ("nest_alpha", "Foo")),
            ("nest/beta/Foo.yaml", ("nest_beta", "Foo")),
            ("nest/Foo.yaml", None),  # the shared parent is no layer's directory
            ("nest/alpha/Foo.json", None),
            ("Nest/alpha/Foo.yaml", None),  # the match is exact, as the readers' globs are
        ],
    )
    def test_two_sibling_nested_layers(
        self, siblings: reg.Registry, target: str, expected: tuple[str, str] | None
    ) -> None:
        assert element_address_of_target(target, registry=siblings) == expected

    @pytest.mark.parametrize("layers", [SIBLING_NESTED_LAYERS])
    def test_every_path_the_registry_composes_maps_back(self, layers: tuple[tuple[str, str], ...]) -> None:
        registry = reg.current(nested_registry(*layers))
        for layer in registry.layer_names:
            placement = {registry.storage(layer).nesting: "a_placement"}
            target = registry.compose_path(layer, "An_Element", placement)
            assert element_address_of_target(target, registry=registry) == (layer, "An_Element"), target


class TestTheAbandonedRecord:
    def test_it_states_its_reason(self, project: Path) -> None:
        loaded = require_project(_manifest(project))
        auditor_for(loaded, "cs-20260815T102241Z-4f19c2").abandoned("the reviewer declined")
        record = _one(project, CHANGE_SET_ABANDONED)
        assert record["args"]["reason"] == "the reviewer declined"
        assert record["result"] == "the reviewer declined"

    def test_a_reasonless_abandon_is_refused(self, project: Path) -> None:
        loaded = require_project(_manifest(project))
        with pytest.raises(ValueError, match="reason"):
            auditor_for(loaded, "cs-20260815T102241Z-4f19c2").abandoned("")


# Where the records go


def test_the_log_is_the_one_the_session_commits_into(project: Path) -> None:
    session = session_mod.ScopedSession(_manifest(project))
    session.open()
    try:
        session.real_audit_log.log_write("write_element", {"target": "tasks/Task_10ms.yaml"}, "/somewhere")
    finally:
        session.abandon()
    loaded = require_project(_manifest(project))
    assert [entry["operation"] for entry in project_audit_log(loaded).get_entries()] == ["write_element"]


def test_every_record_reaches_the_real_log_of_a_change_set_that_never_lands(project: Path) -> None:
    loaded = require_project(_manifest(project))
    before = _tree_state(_model_root(project))
    response = fixtures.dispatch(_request(project, _UPDATE_PRIORITY), project=project, approved_scope=None)
    change_set_id = _one(project, CHANGE_SET_STAGED)["args"]["change_set_id"]
    auditor = auditor_for(loaded, change_set_id)
    auditor.approved(
        ApprovalDecision(change_set_id=change_set_id, baseline=response["baseline"], approved=False),
        None,
        baseline=response["baseline"],
        weakening=False,
    )
    auditor.abandoned("the reviewer declined")

    recorded = [entry["operation"] for entry in _entries(project)]
    assert recorded == [CHANGE_SET_STAGED, CHANGE_SET_APPROVED, CHANGE_SET_ABANDONED]
    assert {entry["args"]["change_set_id"] for entry in _entries(project)} == {change_set_id}
    assert _tree_state(_model_root(project)) == before


def test_the_addresses_recorded_are_the_protocols_vocabulary_not_storage(project: Path) -> None:
    change_set_id, _ = _commit_through(project, _UPDATE_PRIORITY)
    lifecycle = [entry for entry in _entries(project) if entry["operation"].startswith("change_set_")]
    assert lifecycle
    for entry in lifecycle:
        assert str(project) not in str(entry["args"])
        assert entry["args"]["change_set_id"] == change_set_id


def test_a_record_that_cannot_be_written_names_no_audit_log_path(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The OS error names the file; no reply names where the project is stored.
    log = project / "dist" / "audit.jsonl"
    original = audit_mod.ChangeSetAuditor._append

    def failing(self: Any, operation: str, args: dict[str, Any], result: str) -> None:
        if operation == CHANGE_SET_COMMIT_INTENT:
            raise OSError(28, "No space left on device", str(log))
        original(self, operation, args, result)

    monkeypatch.setattr(audit_mod.ChangeSetAuditor, "_append", failing)

    with pytest.raises(CommitPreflightError) as exc:
        _commit_through(project, _UPDATE_PRIORITY)

    assert str(exc.value) == (
        "the commit-intent record could not be written ([Errno 28] No space left on device); nothing was applied "
        "and the model is byte-identical"
    )
