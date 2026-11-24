"""Failure injection at the commit and the recovery contract.

The crash signature is an intent record with no completion record beside it. It does not
distinguish a mid-apply ``OSError`` (model maybe partially applied) from a ``CommitLogError``
(model committed in full, trail missing): both are answered by inspecting the addresses the intent
record names, and the process that died did not know which it was.

A blind retry cannot deepen the damage: the identical change set is ``stale-baseline``, and
re-authored against a fresh token it is ``identity-conflict``; neither refusal touches the model.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import swcraft_tools.session as session_mod
from conftest import model_element_path
from swcraft_tools.auditor import AuditLog
from swcraft_tools.editing.audit import (
    AUDIT_FILENAME,
    CHANGE_SET_COMMIT_INTENT,
    CHANGE_SET_COMMITTED,
    commit_block,
    incomplete_commits,
)
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.engine import ChangeSetProposal, handle_request
from swcraft_tools.editing.staging import baseline_token

# Fixtures and helpers


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A real project on disk — the vector harness's base model, unseeded."""
    return fixtures.build_fixture(tmp_path / "project", "read-swc-element")


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
    return AuditLog(str(project_root / "dist" / AUDIT_FILENAME)).get_entries()


def _operations(project_root: Path) -> list[str]:
    return [entry["operation"] for entry in _entries(project_root)]


def _approve(proposal: ChangeSetProposal) -> ApprovalDecision:
    return ApprovalDecision(change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True)


def _alias(name: str) -> dict[str, Any]:
    """A ``create`` of one data-type alias — a whole new file, so it either lands or does not."""
    return {
        "operation": "create",
        "address": {"layer": "implementation_types", "element": name},
        "value": {
            "name": name,
            "kind": "value_type",
            "base": "float32_base",
            "unit": "kph",
            "description": f"{name} alias",
        },
    }


#: Two creates, so a failure between them leaves a *partially* applied model —
#: the state the crash signature exists to point at.
_OPERATIONS = (_alias("Speed_kph"), _alias("Accel_mps2"))
_CREATED = ("Speed_kph", "Accel_mps2")


def _request(project_root: Path, baseline: str | None = None) -> dict[str, Any]:
    return {
        "operation": "change_set",
        "baseline": baseline if baseline is not None else baseline_token(_model_root(project_root)),
        "intent": "Declare the two signal aliases.",
        "operations": [dict(operation) for operation in _OPERATIONS],
    }


def _landed(project_root: Path) -> set[str]:
    return {
        name for name in _CREATED if (model_element_path(_model_root(project_root), name, kind="value_type")).exists()
    }


def _fail_mid_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the first file for real, then fail — a disk that filled up mid-apply.

    Injected at ``_apply_model_delta``, the one commit step that is not pre-apply, so every step before it really ran.
    """
    original = session_mod._apply_model_delta

    def partial(delta: list[str], shadow_files: Any, real_model_root: Path) -> list[str]:
        original(delta[:1], shadow_files, real_model_root)
        raise OSError("injected: no space left on device")

    monkeypatch.setattr(session_mod, "_apply_model_delta", partial)


def _fail_log_write_for(monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    original = AuditLog.log_write

    def patched(self: AuditLog, name: str, args: dict[str, Any], result: str, scope_id: str | None = None) -> None:
        if name == operation:
            raise OSError("injected: dist/ is read-only")
        original(self, name, args, result, scope_id)

    monkeypatch.setattr(AuditLog, "log_write", patched)


def _crashed_commit(project_root: Path, monkeypatch: pytest.MonkeyPatch, inject: Callable[..., None]) -> dict[str, Any]:
    """Drive one change set into *inject*'s failure; return the request so a test can re-send the identical one."""
    request = _request(project_root)
    with monkeypatch.context() as injected:
        inject(injected)
        with pytest.raises(OSError):
            handle_request(request, project=project_root, review=_approve)
    return request


# The crash signature


class TestAMidApplyFailureLeavesTheCrashSignature:
    def test_an_intent_record_stands_with_no_completion_record(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _crashed_commit(project, monkeypatch, _fail_mid_apply)

        operations = _operations(project)
        assert operations.count(CHANGE_SET_COMMIT_INTENT) == 1
        assert CHANGE_SET_COMMITTED not in operations

    def test_the_signature_is_machine_detectable(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _crashed_commit(project, monkeypatch, _fail_mid_apply)

        (identifier,) = incomplete_commits(_entries(project))
        # Deliberately not a partial block: the entries after an unterminated
        # intent record are not known to belong to it.
        assert commit_block(_entries(project), identifier) is None

    def test_the_intent_record_names_the_addresses_to_inspect(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _crashed_commit(project, monkeypatch, _fail_mid_apply)

        (intent,) = [entry for entry in _entries(project) if entry["operation"] == CHANGE_SET_COMMIT_INTENT]
        assert {address["element"] for address in intent["args"]["addresses"]} == set(_CREATED)

    def test_the_model_really_is_partially_applied(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The honest half of the contract: this failure is the one that is not
        # pre-apply, and the damage is visible to `git status`.
        _crashed_commit(project, monkeypatch, _fail_mid_apply)

        assert len(_landed(project)) == 1

    def test_a_commit_log_failure_leaves_the_same_signature(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The second producer of the signature, and the reason it does not claim
        # to distinguish the two: here the model is committed **in full**.
        with monkeypatch.context() as injected:
            _fail_log_write_for(injected, CHANGE_SET_COMMITTED)
            response = handle_request(_request(project), project=project, review=_approve)

        assert response["status"] == "committed" and "audit_ref" not in response
        assert _landed(project) == set(_CREATED)
        assert len(incomplete_commits(_entries(project))) == 1


# A blind retry cannot compound the damage


class TestABlindRetryIsRefusedTwiceOver:
    @pytest.fixture
    def crashed(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, Any]]:
        return project, _crashed_commit(project, monkeypatch, _fail_mid_apply)

    def test_resending_the_identical_change_set_is_stale(self, crashed: tuple[Path, dict[str, Any]]) -> None:
        # The very same request, token included: the model moved under it.
        project, request = crashed
        response = handle_request(request, project=project, review=_approve)

        assert response["error"] == "stale-baseline"
        assert response["failure_class"] == "stale"

    def test_reauthoring_it_against_a_fresh_token_conflicts(self, crashed: tuple[Path, dict[str, Any]]) -> None:
        project, _ = crashed
        landed = _landed(project)
        response = handle_request(_request(project), project=project, review=_approve)

        assert response["error"] == "identity-conflict"
        assert response["failure_class"] == "invalid"
        assert {address["element"] for address in response["addresses"]} == landed

    def test_neither_refusal_moves_the_model(self, crashed: tuple[Path, dict[str, Any]]) -> None:
        project, request = crashed
        before = _tree_state(_model_root(project))
        handle_request(request, project=project, review=_approve)
        handle_request(_request(project), project=project, review=_approve)
        assert _tree_state(_model_root(project)) == before

    def test_the_repair_a_client_can_actually_make_is_accepted(self, crashed: tuple[Path, dict[str, Any]]) -> None:
        # The recovery contract is not "start over": the half that did not land
        # is ordinary supported work, at a fresh token.
        project, _ = crashed
        missing = sorted(set(_CREATED) - _landed(project))
        request = {
            "operation": "change_set",
            "baseline": baseline_token(_model_root(project)),
            "intent": "Finish what the interrupted commit did not.",
            "operations": [_alias(name) for name in missing],
        }
        response = handle_request(request, project=project, review=_approve)

        assert response["status"] == "committed"
        assert _landed(project) == set(_CREATED)


class TestAMidApplyFailureLeavesAnEditedFileOldOrNew:
    """The apply is one ``os.replace`` per file whichever form made the text."""

    def test_each_edited_file_is_old_or_new_never_half_edited(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task = _model_root(project) / "tasks" / "Task_10ms.yaml"
        swc = _model_root(project) / "swc" / "example" / "ExampleSWC.yaml"
        # A comment no whole write keeps: the file's new bytes are a line edit's and no dump's.
        task.write_bytes(b"# the control cycle\n" + task.read_bytes())
        swc.write_bytes(b"# the example\n" + swc.read_bytes())
        old = {task: task.read_bytes(), swc: swc.read_bytes()}
        new = {
            task: old[task].replace(b"priority: 5", b"priority: 6"),
            swc: old[swc].replace(b"Example software component", b"A described component"),
        }
        request = {
            "operation": "change_set",
            "baseline": baseline_token(_model_root(project)),
            "operations": [
                {
                    "operation": "update",
                    "address": {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "priority"}]},
                    "value": 6,
                },
                {
                    "operation": "update",
                    "address": {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "description"}]},
                    "value": "A described component for testing and demonstration",
                },
            ],
        }
        with monkeypatch.context() as injected:
            _fail_mid_apply(injected)
            with pytest.raises(OSError):
                handle_request(request, project=project, review=_approve)

        landed = [path for path in old if path.read_bytes() == new[path]]
        assert len(landed) == 1, "the injection applies exactly one file"
        for path in old:
            assert path.read_bytes() in (old[path], new[path])
        assert incomplete_commits(_entries(project))
