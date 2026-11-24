"""The change-set lifecycle end to end through ``handle_request``, and its three terminal states.

1. Commit and abandon are host-side acts: the review boundary is injected, and with
   none the lifecycle stops at ``staged``.
2. The reviewed ``ModelDiff`` is the one committed, never recomputed.
3. ``committed`` carries the post-commit token; ``staged`` and ``abandoned`` the one
   the request carried.
4. ``audit_ref`` is present on ``committed`` only, and omitted when the commit's
   record block could not be completed.
5. Faults (``UnexplainedChangeError``, ``AuditReplayError``, ``CommitPreflightError``)
   carry no protocol code and leave the model byte-identical; the first is answered
   as ``abandoned``.
6. A no-op ``update`` writes no file, scoped or not, so ``UnexplainedChangeError`` is
   unreachable from a well-formed request.
"""

from __future__ import annotations

import ast
import itertools
import json
import re
import shutil
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import swcraft_tools.editing.audit as audit_mod
import swcraft_tools.editing.engine as engine_mod
import swcraft_tools.session as session_mod
import yaml
from conftest import model_element_path
from swcraft_tools.auditor import AuditLog
from swcraft_tools.authorization import SafetyChangeKind
from swcraft_tools.differ import ModelDiff
from swcraft_tools.editing.address import Address
from swcraft_tools.editing.audit import (
    AUDIT_FILENAME,
    CHANGE_SET_ABANDONED,
    CHANGE_SET_APPROVED,
    CHANGE_SET_COMMIT_INTENT,
    CHANGE_SET_COMMITTED,
    CHANGE_SET_ID_PATTERN,
    CHANGE_SET_REJECTED,
    CHANGE_SET_STAGED,
    LIFECYCLE_OPERATIONS,
    block_is_complete,
    commit_block,
    written_element_addresses,
)
from swcraft_tools.editing.authorization import ApprovalDecision, ApprovalMismatchError
from swcraft_tools.editing.engine import ChangeSetProposal, handle_request
from swcraft_tools.editing.review import render_address, render_delta_review
from swcraft_tools.editing.staging import StagedChangeSet, baseline_token
from swcraft_tools.session import AuditReplayError, CommitPreflightError, ScopedSession
from swcraft_tools.yaml_load import load_yaml_document, parse_cache_info


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


def _position(entries: list[dict[str, Any]], operation: str, change_set_id: str) -> int:
    """The index of one lifecycle delimiter, by operation and change-set id."""
    return next(
        index
        for index, entry in enumerate(entries)
        if entry["operation"] == operation and entry.get("args", {}).get("change_set_id") == change_set_id
    )


def _element_addresses(delta: list[dict[str, Any]]) -> frozenset[tuple[str, str]]:
    """A response delta's element-level addresses — what its writer entries must cover."""
    return frozenset((entry["address"]["layer"], entry["address"]["element"]) for entry in delta)


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
_UPDATE_SWC_DESCRIPTION = {
    "operation": "update",
    "address": {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "description"}]},
    "value": "A described example component",
}


def _approver(approved: bool, *, seen: list[ChangeSetProposal] | None = None) -> Callable[..., ApprovalDecision]:
    """A host's review boundary, scripted — the only thing that decides anything."""

    def review(proposal: ChangeSetProposal) -> ApprovalDecision:
        if seen is not None:
            seen.append(proposal)
        return ApprovalDecision(change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=approved)

    return review


def _sabotage_shadow(monkeypatch: pytest.MonkeyPatch, action: Callable[[StagedChangeSet], None]) -> None:
    """Run *action* against the shadow right after the touched elements land in it.

    The review boundary gets no handle to the shadow, so this is the one seam inside it.
    """
    original = StagedChangeSet.materialize

    def patched(self: StagedChangeSet, touched: Any, **kwargs: Any) -> list[str]:
        written = original(self, touched, **kwargs)
        action(self)
        return written

    monkeypatch.setattr(StagedChangeSet, "materialize", patched)


def _fail_log_write_for(monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    """Make one audit ``operation`` unwritable, at the real I/O boundary."""
    original = AuditLog.log_write

    def patched(self: AuditLog, name: str, args: dict[str, Any], result: str, scope_id: str | None = None) -> None:
        if name == operation:
            raise OSError("injected: dist/ is read-only")
        original(self, name, args, result, scope_id)

    monkeypatch.setattr(AuditLog, "log_write", patched)


class TestTheThreeTerminalStates:
    def test_no_review_boundary_stops_at_staged(self, project: Path) -> None:
        before = _tree_state(_model_root(project))
        request = _request(project, _UPDATE_PRIORITY)

        response = handle_request(request, project=project)

        assert response["status"] == "staged"
        assert response["delta"], "an accepted change set answers with the delta it produced"
        # The model has not moved, so the token comes back unchanged.
        assert response["baseline"] == request["baseline"]
        assert "audit_ref" not in response
        assert _tree_state(_model_root(project)) == before

    def test_a_declining_boundary_abandons(self, project: Path) -> None:
        before = _tree_state(_model_root(project))
        request = _request(project, _UPDATE_PRIORITY)

        response = handle_request(request, project=project, review=_approver(False))

        assert response["status"] == "abandoned"
        assert response["baseline"] == request["baseline"]
        assert "audit_ref" not in response
        assert _tree_state(_model_root(project)) == before
        assert _operations(project) == [CHANGE_SET_STAGED, CHANGE_SET_APPROVED, CHANGE_SET_ABANDONED]

    def test_an_approving_boundary_commits(self, project: Path) -> None:
        before = _tree_state(_model_root(project))
        request = _request(project, _UPDATE_PRIORITY)

        response = handle_request(request, project=project, review=_approver(True))

        assert response["status"] == "committed"
        assert response["baseline"] != request["baseline"]
        assert response["baseline"] == baseline_token(_model_root(project))
        assert CHANGE_SET_ID_PATTERN.fullmatch(response["audit_ref"])
        assert _tree_state(_model_root(project)) != before

    def test_the_committed_response_carries_the_delta_that_was_reviewed(self, project: Path) -> None:
        seen: list[ChangeSetProposal] = []
        response = handle_request(
            _request(project, _UPDATE_PRIORITY, _UPDATE_SWC_DESCRIPTION),
            project=project,
            review=_approver(True, seen=seen),
        )
        assert [entry.to_json() for entry in seen[0].delta] == response["delta"]

    def test_the_audit_ref_names_the_change_set_the_log_recorded(self, project: Path) -> None:
        response = handle_request(_request(project, _UPDATE_PRIORITY), project=project, review=_approver(True))
        identifiers = {
            entry["args"]["change_set_id"] for entry in _entries(project) if "change_set_id" in entry.get("args", {})
        }
        assert identifiers == {response["audit_ref"]}

    def test_the_commit_block_is_contiguous_and_closed(self, project: Path) -> None:
        handle_request(_request(project, _UPDATE_PRIORITY), project=project, review=_approver(True))
        operations = _operations(project)
        start = operations.index(CHANGE_SET_COMMIT_INTENT)
        end = operations.index(CHANGE_SET_COMMITTED)
        assert operations[:start] == [CHANGE_SET_STAGED, CHANGE_SET_APPROVED]
        assert operations[start + 1 : end] == ["write_element"]


class TestTheReviewBoundaryIsInjected:
    PACKAGE = Path(engine_mod.__file__).parent

    @pytest.mark.parametrize(
        "module", sorted(PACKAGE.glob("*.py"), key=lambda path: path.as_posix()), ids=lambda path: path.name
    )
    def test_nothing_in_the_package_prompts_or_renders(self, module: Path) -> None:
        # The engine stays host-neutral: the host owns asking and rendering.
        tree = ast.parse(module.read_text(encoding="utf-8"))
        called = {
            node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert not called & {"input", "print"}, f"{module.name} prompts or renders"

    def test_the_proposal_carries_the_delta_and_no_handle(self, project: Path) -> None:
        seen: list[ChangeSetProposal] = []
        request = _request(project, _UPDATE_PRIORITY)
        handle_request(request, project=project, review=_approver(False, seen=seen))

        (proposal,) = seen
        assert proposal.baseline == request["baseline"]
        assert proposal.intent == request["intent"]
        assert proposal.delta
        assert CHANGE_SET_ID_PATTERN.fullmatch(proposal.change_set_id)
        # No handle that could name the staged transaction: `safety` is the guard's
        # verdict, `rewritten` names elements and fixed reasons, never a file.
        assert set(vars(proposal)) == {"change_set_id", "baseline", "intent", "delta", "safety", "rewritten"}
        assert proposal.safety.changes == ()  # a priority update weakens no check

    def test_a_boundary_that_declines_leaves_a_declined_record(self, project: Path) -> None:
        handle_request(_request(project, _UPDATE_PRIORITY), project=project, review=_approver(False))
        (approved,) = [entry for entry in _entries(project) if entry["operation"] == CHANGE_SET_APPROVED]
        assert approved["args"]["decision"] == "declined"
        assert approved["result"] == "declined"

    def test_a_decision_bound_to_another_change_set_is_a_fault(self, project: Path) -> None:
        # No protocol operation approves anything, so this is a host defect.
        before = _tree_state(_model_root(project))

        def replayed(proposal: ChangeSetProposal) -> ApprovalDecision:
            return ApprovalDecision(
                change_set_id="cs-20260815T102241Z-4f19c2", baseline=proposal.baseline, approved=True
            )

        with pytest.raises(ApprovalMismatchError):
            handle_request(_request(project, _UPDATE_PRIORITY), project=project, review=replayed)
        assert _tree_state(_model_root(project)) == before
        assert CHANGE_SET_APPROVED not in _operations(project)


class TestTheReviewedDeltaReachesTheCommit:
    def test_the_diff_handed_to_the_commit_is_the_one_the_delta_came_from(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        projected: list[ModelDiff] = []
        committed: list[ModelDiff] = []

        original_diff = engine_mod.diff_snapshots

        def counted(*args: Any, **kwargs: Any) -> ModelDiff:
            result = original_diff(*args, **kwargs)
            projected.append(result)
            return result

        original_commit = ScopedSession.commit

        def captured(self: ScopedSession, diff: ModelDiff, **kwargs: Any) -> list[str]:
            committed.append(diff)
            return original_commit(self, diff, **kwargs)

        monkeypatch.setattr(engine_mod, "diff_snapshots", counted)
        monkeypatch.setattr(ScopedSession, "commit", captured)

        handle_request(_request(project, _UPDATE_PRIORITY), project=project, review=_approver(True))

        assert len(projected) == 1
        assert committed[0] is projected[0]

    def test_every_entry_the_commit_applied_was_on_the_prompt_the_human_answered(self, project: Path) -> None:
        """Every committed entry was shown on the prompt; no display cap hides one.

        Through the real engine: the engine decides what commits, the renderer what is shown.
        """
        count = 60
        operations = [
            {
                "operation": "create",
                "address": {"layer": "implementation_types", "element": f"Probe{index}"},
                "value": {"name": f"Probe{index}", "kind": "value_type", "base": "float32_base", "unit": "kph"},
            }
            for index in range(count)
        ]
        questions: list[str] = []

        def review(proposal: ChangeSetProposal) -> ApprovalDecision:
            questions.append(render_delta_review(proposal))
            return ApprovalDecision(proposal.change_set_id, proposal.baseline, approved=True)

        response = handle_request(_request(project, *operations), project=project, review=review)

        assert response["status"] == "committed"
        assert len(response["delta"]) == count
        message = questions[0]
        # Addresses catch an unshown entry; headlines catch fewer lines than entries.
        for entry in response["delta"]:
            rendered = render_address(Address.from_json(entry["address"]))
            assert rendered in message, f"{rendered} committed but never shown"
        headlines = [line for line in message.splitlines() if re.match(r"^  \d+\. create ", line)]
        assert len(headlines) == count, f"{len(headlines)} entries shown, {count} committed"
        assert "not shown" not in message
        assert (model_element_path(_model_root(project), f"Probe{count - 1}", kind="value_type")).is_file()

    def test_a_stale_commit_is_a_rejection_and_leaves_no_intent_record(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An intent record written before the stale check would look like a crash.
        def move_the_model(_staging: StagedChangeSet) -> None:
            (_model_root(project) / "swc" / "example" / "note.txt").write_text("moved", encoding="utf-8")

        _sabotage_shadow(monkeypatch, move_the_model)

        response = handle_request(_request(project, _UPDATE_PRIORITY), project=project, review=_approver(True))

        assert response["error"] == "stale-baseline"
        assert response["failure_class"] == "stale"
        assert CHANGE_SET_COMMIT_INTENT not in _operations(project)
        assert CHANGE_SET_COMMITTED not in _operations(project)


class TestFaultsAreNotRejections:
    def test_an_unexplained_byte_difference_is_abandoned_and_noted(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An engine defect, so no protocol code; answered as `abandoned` because the
        # tripwire fires before the first write.
        before = _tree_state(_model_root(project))
        _sabotage_shadow(
            monkeypatch, lambda staging: Path(staging.model_root, "notes.txt").write_text("stray", encoding="utf-8")
        )
        notes: list[str] = []

        response = handle_request(
            _request(project, _UPDATE_PRIORITY), project=project, review=_approver(True), notes=notes.append
        )

        assert response["status"] == "abandoned"
        assert "audit_ref" not in response
        assert _tree_state(_model_root(project)) == before
        assert notes and "notes.txt" in notes[0]
        assert CHANGE_SET_ABANDONED in _operations(project)
        assert CHANGE_SET_COMMITTED not in _operations(project)

    def test_an_unrebasable_audit_entry_is_a_fault(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Distinct from CommitPreflightError: the two need different words for the user.
        before = _tree_state(_model_root(project))
        _sabotage_shadow(
            monkeypatch,
            lambda staging: staging.session.shadow_audit_log.log_write(
                "write_element", {"target": "tasks/Task_10ms.yaml"}, "/elsewhere/Task_10ms.yaml"
            ),
        )

        with pytest.raises(AuditReplayError):
            handle_request(_request(project, _UPDATE_PRIORITY), project=project, review=_approver(True))
        assert _tree_state(_model_root(project)) == before

    def test_a_fault_in_the_delta_projection_still_leaves_a_lifecycle_record(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A change set leaves a lifecycle record whatever its outcome, a projection
        # fault included. The fault is injected to pin the guard.
        def exploded(*_args: Any, **_kwargs: Any) -> ModelDiff:
            raise ValueError("the differ could not key /somewhere/private/model")

        monkeypatch.setattr(engine_mod, "diff_snapshots", exploded)
        before = _tree_state(_model_root(project))

        with pytest.raises(ValueError):
            handle_request(_request(project, _UPDATE_PRIORITY), project=project, review=_approver(True))

        assert _tree_state(_model_root(project)) == before
        assert CHANGE_SET_ABANDONED in _operations(project)
        assert CHANGE_SET_STAGED not in _operations(project)
        # The class name only: a fault's message may name a path.
        reasons = [entry["args"]["reason"] for entry in _entries(project) if entry["operation"] == CHANGE_SET_ABANDONED]
        assert reasons == [
            "an internal fault (ValueError) ended this change set before it was staged; "
            "nothing was applied and the model is byte-identical"
        ]

    def test_an_unwritable_intent_record_lands_pre_apply(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        before = _tree_state(_model_root(project))
        _fail_log_write_for(monkeypatch, CHANGE_SET_COMMIT_INTENT)

        with pytest.raises(CommitPreflightError):
            handle_request(_request(project, _UPDATE_PRIORITY), project=project, review=_approver(True))
        assert _tree_state(_model_root(project)) == before
        assert CHANGE_SET_COMMITTED not in _operations(project)


class TestACommitWhoseTrailIsIncomplete:
    def test_an_unwritable_completion_record_is_still_a_commit(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = _tree_state(_model_root(project))
        notes: list[str] = []
        _fail_log_write_for(monkeypatch, CHANGE_SET_COMMITTED)

        response = handle_request(
            _request(project, _UPDATE_PRIORITY), project=project, review=_approver(True), notes=notes.append
        )

        # Committed in full; only the trail is incomplete.
        assert response["status"] == "committed"
        assert response["baseline"] == baseline_token(_model_root(project))
        assert _tree_state(_model_root(project)) != before
        # `audit_ref` asserts a complete record block, false after a failed append.
        assert "audit_ref" not in response
        assert notes and "committed in full" in notes[0]

    def test_an_unreplayable_writer_entry_is_still_a_commit(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = _tree_state(_model_root(project))
        notes: list[str] = []

        def patched(self: AuditLog, entry: dict[str, Any], scope_id: str | None = None) -> None:
            raise OSError("injected: dist/ filled up")

        monkeypatch.setattr(AuditLog, "replay_entry", patched)

        response = handle_request(
            _request(project, _UPDATE_PRIORITY), project=project, review=_approver(True), notes=notes.append
        )

        assert response["status"] == "committed"
        assert "audit_ref" not in response
        assert _tree_state(_model_root(project)) != before
        assert notes

    def test_a_note_sink_is_optional(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _fail_log_write_for(monkeypatch, CHANGE_SET_COMMITTED)
        response = handle_request(_request(project, _UPDATE_PRIORITY), project=project, review=_approver(True))
        assert response["status"] == "committed" and "audit_ref" not in response


def _current(project_root: Path, address: dict[str, Any]) -> Any:
    """The value at *address* today, read through the protocol itself."""
    return handle_request({"operation": "read", "address": address}, project=project_root)["value"]


class TestANoOpUpdateWritesNothing:
    """An ``update`` to the current value touches no element, so no file is rewritten.

    ``write_element`` has no "value unchanged" short-circuit: a rewrite without a delta
    entry would refuse the change set as ``UnexplainedChangeError``.
    """

    ADDRESS = {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "priority"}]}
    SCOPE = [{"operation": "update", "address": ADDRESS}]

    def _no_op(self, project_root: Path) -> dict[str, Any]:
        return {"operation": "update", "address": self.ADDRESS, "value": _current(project_root, self.ADDRESS)}

    @pytest.mark.parametrize("scoped", [False, True], ids=["unscoped", "in-scope"])
    def test_it_stages_with_an_empty_delta_and_writes_no_file(self, project: Path, scoped: bool) -> None:
        before = _tree_state(_model_root(project))
        response = handle_request(
            _request(project, self._no_op(project)), project=project, approved_scope=self.SCOPE if scoped else None
        )
        assert response["status"] == "staged"
        assert response["delta"] == []
        assert _tree_state(_model_root(project)) == before

    @pytest.mark.parametrize("scoped", [False, True], ids=["unscoped", "in-scope"])
    def test_it_commits_without_reaching_the_unexplained_change_tripwire(self, project: Path, scoped: bool) -> None:
        before = _tree_state(_model_root(project))
        response = handle_request(
            _request(project, self._no_op(project)),
            project=project,
            approved_scope=self.SCOPE if scoped else None,
            review=_approver(True),
        )
        # No `UnexplainedChangeError` is the assertion.
        assert response["status"] == "committed"
        assert response["delta"] == []
        assert _tree_state(_model_root(project)) == before
        assert response["baseline"] == baseline_token(_model_root(project))


class TestAbandonIsIdempotentAndAlwaysSucceeds:
    """Abandon is an ``rmtree`` of a private ``mkdtemp``; every lifecycle exit drops the shadow."""

    @pytest.fixture
    def staged(self, project: Path) -> Iterator[StagedChangeSet]:
        from swcraft_tools.editing.staging import stage

        staging = stage(str(project / "swcraft.yaml"))
        yield staging
        staging.abandon()

    def test_abandoning_twice_is_not_an_error(self, staged: StagedChangeSet) -> None:
        staged.abandon()
        staged.abandon()

    def test_abandoning_after_a_commit_is_not_an_error(self, project: Path, staged: StagedChangeSet) -> None:
        staged.commit(staged.session.diff())
        staged.abandon()

    def test_the_lifecycle_drops_the_shadow_on_every_exit(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        roots: list[Path] = []
        _sabotage_shadow(monkeypatch, lambda staging: roots.append(Path(staging.model_root).parent))

        handle_request(_request(project, _UPDATE_PRIORITY), project=project, review=_approver(True))
        handle_request(_request(project, _UPDATE_SWC_DESCRIPTION), project=project, review=_approver(False))
        handle_request(
            _request(project, _UPDATE_PRIORITY, baseline=baseline_token(_model_root(project))), project=project
        )

        assert len(roots) == 3
        assert not [root for root in roots if root.exists()]


# The safety guard, always on

_WEAKEN_ENFORCEMENT = {
    "operation": "update",
    "address": {"layer": "hardware", "element": "RAM0", "path": [{"field": "enforcement"}]},
    "value": "advisory",
}


def _safety_approver(*, approved: bool, safety_approved: bool) -> Callable[..., ApprovalDecision]:
    def review(proposal: ChangeSetProposal) -> ApprovalDecision:
        return ApprovalDecision(
            change_set_id=proposal.change_set_id,
            baseline=proposal.baseline,
            approved=approved,
            safety_approved=safety_approved,
        )

    return review


class TestTheSafetyGuardIsAlwaysOn:
    """The always-on safety guard, reached from the protocol: a weakening needs a second answer.

    ``update_resource`` asks the same separate question; without it a change set would bypass it.
    """

    def test_the_proposal_carries_the_weakening(self, project: Path) -> None:
        seen: list[ChangeSetProposal] = []

        def review(proposal: ChangeSetProposal) -> ApprovalDecision:
            seen.append(proposal)
            return ApprovalDecision(proposal.change_set_id, proposal.baseline, approved=False)

        handle_request(_request(project, _WEAKEN_ENFORCEMENT), project=project, review=review)

        (proposal,) = seen
        (change,) = proposal.safety.changes
        assert (change.resource, change.field, change.old, change.new) == ("RAM0", "enforcement", "strict", "advisory")

    def test_approving_the_delta_is_not_approving_the_weakening(self, project: Path) -> None:
        before = _tree_state(_model_root(project))
        notes: list[str] = []
        response = handle_request(
            _request(project, _WEAKEN_ENFORCEMENT),
            project=project,
            review=_safety_approver(approved=True, safety_approved=False),
            notes=notes.append,
        )
        # `abandoned`, not `unauthorized-delta`: the client's authority was never the obstacle.
        assert response["status"] == "abandoned"
        assert "error" not in response
        assert _tree_state(_model_root(project)) == before
        # The response carries no reason, so the note says which answer was no.
        (note,) = notes
        assert "RAM0" in note and "strict" in note and "advisory" in note

    def test_the_declined_weakening_is_named_in_the_abandonment_record(self, project: Path) -> None:
        handle_request(
            _request(project, _WEAKEN_ENFORCEMENT),
            project=project,
            review=_safety_approver(approved=True, safety_approved=False),
        )
        (abandoned,) = [entry for entry in _entries(project) if entry["operation"] == CHANGE_SET_ABANDONED]
        assert "RAM0" in abandoned["args"]["reason"]
        assert "byte-identical" in abandoned["args"]["reason"]

    def test_a_separately_approved_weakening_commits(self, project: Path) -> None:
        response = handle_request(
            _request(project, _WEAKEN_ENFORCEMENT),
            project=project,
            review=_safety_approver(approved=True, safety_approved=True),
        )
        assert response["status"] == "committed"
        assert "advisory" in (_model_root(project) / "hardware" / "RAM0.yaml").read_text(encoding="utf-8")

    def test_a_declined_weakening_is_recorded_as_an_abandonment(self, project: Path) -> None:
        handle_request(
            _request(project, _WEAKEN_ENFORCEMENT),
            project=project,
            review=_safety_approver(approved=True, safety_approved=False),
        )
        # The trail of a declined delta; `change_set_approved.args` tells the two apart.
        assert _operations(project) == [CHANGE_SET_STAGED, CHANGE_SET_APPROVED, CHANGE_SET_ABANDONED]
        approved = next(entry for entry in _entries(project) if entry["operation"] == CHANGE_SET_APPROVED)
        assert approved["args"]["decision"] == "approved"
        assert approved["args"]["safety_decision"] == "declined"

    def test_a_committed_weakening_records_that_the_second_prompt_was_approved(self, project: Path) -> None:
        handle_request(
            _request(project, _WEAKEN_ENFORCEMENT),
            project=project,
            review=_safety_approver(approved=True, safety_approved=True),
        )
        approved = next(entry for entry in _entries(project) if entry["operation"] == CHANGE_SET_APPROVED)
        assert approved["args"]["safety_decision"] == "approved"

    def test_a_removal_of_a_resource_is_a_weakening_too(self, project: Path) -> None:
        # The referrers are cleared too, so only the guard stands before a commit.
        response = handle_request(
            _request(
                project,
                {
                    "operation": "remove",
                    "address": {
                        "layer": "swc",
                        "element": "ExampleSWC",
                        "path": [{"field": "memory"}, {"key": "RAM0"}],
                    },
                },
                {
                    "operation": "remove",
                    "address": {
                        "layer": "swc",
                        "element": "ExampleConsumerSWC",
                        "path": [{"field": "memory"}, {"key": "RAM0"}],
                    },
                },
                {"operation": "remove", "address": {"layer": "hardware", "element": "RAM0"}},
            ),
            project=project,
            review=_safety_approver(approved=True, safety_approved=False),
        )
        assert response["status"] == "abandoned"

    def test_admitting_a_core_to_a_memory_is_a_weakening_at_this_seam_too(self, project: Path) -> None:
        """`WIDEN_ACCESS`, triggered by a member `create`, is a weakening at the protocol seam.

        `CORE1` is created alongside, or `hardware.accessible-undeclared-core` refuses first;
        creating an element is no safety act (one change only).
        """
        seen: list[ChangeSetProposal] = []

        def review(proposal: ChangeSetProposal) -> ApprovalDecision:
            seen.append(proposal)
            return ApprovalDecision(proposal.change_set_id, proposal.baseline, approved=True, safety_approved=False)

        response = handle_request(
            _request(
                project,
                {
                    "operation": "create",
                    "address": {"layer": "hardware", "element": "CORE1"},
                    "value": {
                        "name": "CORE1",
                        "kind": "core",
                        "threshold": 0.7,
                        "enforcement": "strict",
                        "description": "Application core 1",
                    },
                },
                {
                    "operation": "create",
                    "address": {
                        "layer": "hardware",
                        "element": "RAM0",
                        "path": [{"field": "accessible"}, {"member": "CORE1"}],
                    },
                    "value": "CORE1",
                },
            ),
            project=project,
            review=review,
        )

        (proposal,) = seen
        (change,) = proposal.safety.changes
        assert change.kind is SafetyChangeKind.WIDEN_ACCESS
        assert (change.resource, change.field) == ("RAM0", "accessible")
        assert change.describe().startswith("hardware element 'RAM0': accessible admits CORE1 — ")
        assert response["status"] == "abandoned"
        assert "CORE1" not in (_model_root(project) / "hardware" / "RAM0.yaml").read_text(encoding="utf-8")

    def test_a_change_set_that_weakens_nothing_needs_no_second_answer(self, project: Path) -> None:
        response = handle_request(
            _request(project, _UPDATE_PRIORITY),
            project=project,
            review=_safety_approver(approved=True, safety_approved=False),
        )
        assert response["status"] == "committed"


# Every rejection is one errorResult with a code from the closed enum


def _create_swc(name: str, group: str = "example") -> dict[str, Any]:
    return {
        "operation": "create",
        "address": {"layer": "swc", "element": name},
        "value": {"name": name, "group": group, "type": "application", "description": "x"},
    }


def _create_primitive(name: str) -> dict[str, Any]:
    return {
        "operation": "create",
        "address": {"layer": "base_types", "element": name},
        "value": {"name": name, "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"},
    }


class TestWriterRefusalsBecomeProtocolCodes:
    """A writer refusal is one ``errorResult`` with a code from the closed enum, never a traceback.

    ``write_element``'s guards run during materialization, before pass D; their
    ``ValueError``/``PathSegmentError`` does not leave the seam uncaught.
    """

    @pytest.mark.parametrize(
        ("operations", "expected_address"),
        [
            (
                (_create_swc("ExampleSWC_Implementation"),),
                {"layer": "swc", "element": "ExampleSWC_Implementation"},
            ),
            # Two operations: the base model declares no enum, and the guard judges
            # the outcome. A form collides only with a type of its owner's package,
            # so the occupant is an application type.
            (
                (
                    {
                        "operation": "create",
                        "address": {"layer": "application_types", "element": "Gear"},
                        "value": {"name": "Gear", "kind": "enum", "literals": [{"name": "PARK", "value": 0}]},
                    },
                    {
                        "operation": "create",
                        "address": {"layer": "application_types", "element": "Gear_CompuMethod"},
                        "value": {"name": "Gear_CompuMethod", "kind": "application_type"},
                    },
                ),
                {"layer": "application_types", "element": "Gear_CompuMethod"},
            ),
        ],
        ids=["swc-implementation", "compu-method"],
    )
    def test_a_reserved_name_is_a_consistency_violation(
        self, project: Path, operations: tuple[dict[str, Any], ...], expected_address: dict[str, Any]
    ) -> None:
        before = _tree_state(_model_root(project))
        response = handle_request(_request(project, *operations), project=project, review=_approver(True))
        assert response["error"] == "consistency-violation"
        assert response["addresses"] == [expected_address]
        assert _tree_state(_model_root(project)) == before

    @pytest.mark.parametrize("group", ["a/b", "..", "../../../../tmp/evil", "", "."], ids=repr)
    def test_a_traversal_placement_value_is_a_schema_violation(self, project: Path, group: str) -> None:
        # Answered at the field the client wrote, never sanitized.
        before = _tree_state(_model_root(project))
        response = handle_request(_request(project, _create_swc("NewSWC", group)), project=project)
        assert response["error"] == "schema-violation"
        assert response["addresses"] == [{"layer": "swc", "element": "NewSWC", "path": [{"field": "group"}]}]
        assert _tree_state(_model_root(project)) == before

    def test_a_refused_change_set_still_leaves_a_rejection_record(self, project: Path) -> None:
        handle_request(_request(project, _create_swc("ExampleSWC_Implementation")), project=project)
        assert _operations(project) == ["change_set_rejected"]

    def test_a_baseline_placement_value_that_is_not_a_segment_is_answered_not_raised(self, project: Path) -> None:
        # A hand-authored flat `model/swc/Foo.yaml` reads as `group: ""`, a model
        # defect still owed a code and an address.
        source = _model_root(project) / "swc" / "example" / "ExampleSWC.yaml"
        (_model_root(project) / "swc" / "FooSWC.yaml").write_text(
            source.read_text(encoding="utf-8").replace("ExampleSWC", "FooSWC"), encoding="utf-8"
        )
        response = handle_request(
            _request(project, {"operation": "remove", "address": {"layer": "swc", "element": "FooSWC"}}),
            project=project,
        )
        assert response["error"] == "schema-violation"
        assert response["addresses"] == [{"layer": "swc", "element": "FooSWC", "path": [{"field": "group"}]}]

    def test_the_reserved_name_guard_judges_the_outcome(self, project: Path) -> None:
        # Removals apply before writes, so the refusal is the dangling `float32`
        # reference, not a collision with the removed type.
        response = handle_request(
            _request(
                project,
                {"operation": "remove", "address": {"layer": "implementation_types", "element": "float32"}},
                _create_primitive("float32_AppView"),
            ),
            project=project,
        )
        assert response["error"] == "broken-reference"


class TestReadAndDiscoverMisses:
    """The two "not found" codes, from the two unaudited operations."""

    @pytest.mark.parametrize(
        ("address", "code"),
        [
            ({"layer": "nosuchlayer"}, "unknown-field"),
            ({"layer": "swc", "element": "ExampleSWC", "path": [{"field": "nope"}]}, "unknown-field"),
            ({"layer": "swc", "element": "NoSuchSWC"}, "broken-reference"),
            (
                {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "ports"}, {"member": "pNope"}]},
                "broken-reference",
            ),
            (
                {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "memory"}, {"key": "ROM"}]},
                "broken-reference",
            ),
        ],
        ids=["layer", "field", "element", "member", "key"],
    )
    def test_a_read_miss_is_an_error_result(self, project: Path, address: dict[str, Any], code: str) -> None:
        response = handle_request({"operation": "read", "address": address}, project=project)
        assert response["response"] == "error"
        assert response["error"] == code
        # The address the client sent is the one it has to change.
        assert response["addresses"] == [address]

    def test_a_discover_at_an_undeclared_layer_is_an_error_result(self, project: Path) -> None:
        response = handle_request({"operation": "discover", "layer": "nosuchlayer"}, project=project)
        assert response["error"] == "unknown-field"
        assert response["addresses"] == [{"layer": "nosuchlayer"}]

    def test_a_refused_read_writes_no_audit_record(self, project: Path) -> None:
        handle_request({"operation": "read", "address": {"layer": "swc", "element": "NoSuchSWC"}}, project=project)
        handle_request({"operation": "discover", "layer": "nosuchlayer"}, project=project)
        assert not (project / "dist" / AUDIT_FILENAME).exists()


class TestTheScopeIdSeam:
    """``scope_id``, the host's turn correlation id, is a parameter of the seam, never of the protocol.

    Every lifecycle record of a scoped request carries it; no request or response
    does, so a client cannot forge the correlation.
    """

    SCOPE = [{"operation": "update", "address": {"layer": "tasks", "element": "Task_10ms"}}]
    TURN = "turn-7"

    def _lifecycle(self, project_root: Path) -> list[dict[str, Any]]:
        return [entry for entry in _entries(project_root) if entry["operation"] in LIFECYCLE_OPERATIONS]

    def test_every_lifecycle_record_of_a_scoped_request_carries_it(self, project: Path) -> None:
        response = handle_request(
            _request(project, _UPDATE_PRIORITY),
            project=project,
            approved_scope=self.SCOPE,
            scope_id=self.TURN,
            review=_approver(True),
        )

        assert response["status"] == "committed"
        records = self._lifecycle(project)
        assert [record["operation"] for record in records] == [
            CHANGE_SET_STAGED,
            CHANGE_SET_APPROVED,
            CHANGE_SET_COMMIT_INTENT,
            CHANGE_SET_COMMITTED,
        ]
        assert [record.get("scope_id") for record in records] == [self.TURN] * 4
        assert all(list(record) == [*AuditLog.ENTRY_KEYS, "scope_id"] for record in records)

    def test_a_declined_change_set_records_it_too(self, project: Path) -> None:
        handle_request(
            _request(project, _UPDATE_PRIORITY),
            project=project,
            approved_scope=self.SCOPE,
            scope_id=self.TURN,
            review=_approver(False),
        )

        records = self._lifecycle(project)
        assert [record["operation"] for record in records] == [
            CHANGE_SET_STAGED,
            CHANGE_SET_APPROVED,
            CHANGE_SET_ABANDONED,
        ]
        assert {record["scope_id"] for record in records} == {self.TURN}

    def test_a_pre_staging_rejection_carries_it(self, project: Path) -> None:
        # The one record a refused turn leaves must be findable by the turn's id.
        response = handle_request(
            _request(project, _UPDATE_PRIORITY),
            project=project,
            approved_scope=[],
            scope_id=self.TURN,
        )

        assert response["error"] == "unauthorized-delta"
        records = self._lifecycle(project)
        assert [record["operation"] for record in records] == [CHANGE_SET_REJECTED]
        assert records[0]["scope_id"] == self.TURN

    def test_a_malformed_scope_rejection_carries_it(self, project: Path) -> None:
        # The seam's own argument fault is answered, and recorded under the turn.
        response = handle_request(
            _request(project, _UPDATE_PRIORITY),
            project=project,
            approved_scope="[not json",
            scope_id=self.TURN,
        )

        assert response["error"] == "schema-violation"
        records = self._lifecycle(project)
        assert [record["operation"] for record in records] == [CHANGE_SET_REJECTED]
        assert records[0]["scope_id"] == self.TURN

    def test_an_unscoped_run_writes_the_four_historical_keys_and_no_fifth(self, project: Path) -> None:
        handle_request(_request(project, _UPDATE_PRIORITY), project=project, review=_approver(True))

        for record in self._lifecycle(project):
            assert list(record) == list(AuditLog.ENTRY_KEYS)

    @pytest.mark.parametrize(
        "request_",
        [
            {"operation": "discover"},
            {"operation": "read", "address": {"layer": "tasks", "element": "Task_10ms"}},
        ],
        ids=["discover", "read"],
    )
    def test_discover_and_read_take_the_seam_and_still_write_nothing(
        self, project: Path, request_: dict[str, Any]
    ) -> None:
        # Both are unscoped and unaudited; a host hands the seam values to every
        # call, and an empty scope must not refuse them.
        response = handle_request(request_, project=project, approved_scope=[], scope_id=self.TURN)

        assert "error" not in response
        assert not (project / "dist" / AUDIT_FILENAME).exists()

    def test_the_correlation_id_reaches_no_response(self, project: Path) -> None:
        response = handle_request(
            _request(project, _UPDATE_PRIORITY),
            project=project,
            approved_scope=self.SCOPE,
            scope_id=self.TURN,
            review=_approver(True),
        )

        assert self.TURN not in json.dumps(response)


class TestConcurrentCommitsAreSerialized:
    """`_commit` is the one serialized region of the lifecycle, asserted at `handle_request`.

    `ScopedSession.commit()` re-fingerprints and applies as separate steps; without a
    lock over both, two threads pass one baseline check and both apply.
    """

    @staticmethod
    def _approve(proposal: ChangeSetProposal) -> ApprovalDecision:
        return ApprovalDecision(proposal.change_set_id, proposal.baseline, approved=True)

    def _race(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        """Two approved change sets, submitted from two threads at one baseline."""
        baseline = baseline_token(_model_root(project))
        both_applying = threading.Event()
        arrivals = itertools.count(1)
        real_apply = session_mod._apply_model_delta

        def observed_apply(delta: Any, shadow_files: Any, real_model_root: Any) -> Any:
            if next(arrivals) >= 2:
                both_applying.set()
            else:
                # Hold the first apply open; serialized, this times out, the passing shape.
                both_applying.wait(timeout=1.0)
            return real_apply(delta, shadow_files, real_model_root)

        monkeypatch.setattr(session_mod, "_apply_model_delta", observed_apply)

        start = threading.Barrier(2, timeout=30)
        responses: dict[int, dict[str, Any]] = {}

        def submit(index: int, operation: dict[str, Any]) -> None:
            start.wait()
            responses[index] = handle_request(
                _request(project, operation, baseline=baseline), project=project, review=self._approve
            )

        threads = [
            threading.Thread(target=submit, args=(0, _UPDATE_PRIORITY)),
            threading.Thread(target=submit, args=(1, _UPDATE_SWC_DESCRIPTION)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            assert not thread.is_alive(), "a change set never finished; the commit region deadlocked"
        return [responses[0], responses[1]]

    def test_the_second_commit_is_refused_as_stale_rather_than_applied(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        responses = self._race(project, monkeypatch)

        assert sorted(response.get("status") or response["error"] for response in responses) == [
            "committed",
            "stale-baseline",
        ], responses

    def test_the_log_holds_one_commit_block_and_only_this_change_sets_writes(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `block_is_complete` alone is too weak; assert the writer entries by name.
        responses = self._race(project, monkeypatch)

        assert _operations(project).count(CHANGE_SET_COMMIT_INTENT) == 1, _operations(project)
        committed = next(response for response in responses if response.get("status") == "committed")
        block = commit_block(_entries(project), committed["audit_ref"])
        assert block is not None
        assert [entry["operation"] for entry in block.entries] == ["write_element"]
        assert written_element_addresses(block) == _element_addresses(committed["delta"])
        assert block_is_complete(block)

    def test_a_second_change_sets_staging_record_may_land_in_the_span_and_is_not_part_of_the_block(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Staging is outside `commit_lock` (a lock would be held across a human's
        # prompt), so a record lands mid-block and is excluded by its id.
        real_apply = session_mod._apply_model_delta
        staged: list[dict[str, Any]] = []

        def observed_apply(delta: Any, shadow_files: Any, real_model_root: Any) -> Any:
            other = threading.Thread(
                target=lambda: staged.append(
                    handle_request(_request(project, _UPDATE_SWC_DESCRIPTION), project=project)
                )
            )
            other.start()
            other.join(timeout=30)
            assert not other.is_alive(), "the staging request never finished; staging is not inside the lock"
            return real_apply(delta, shadow_files, real_model_root)

        monkeypatch.setattr(session_mod, "_apply_model_delta", observed_apply)

        response = handle_request(_request(project, _UPDATE_PRIORITY), project=project, review=self._approve)
        assert response["status"] == "committed"
        assert staged and staged[0]["status"] == "staged"

        entries = _entries(project)
        start = _position(entries, CHANGE_SET_COMMIT_INTENT, response["audit_ref"])
        end = _position(entries, CHANGE_SET_COMMITTED, response["audit_ref"])
        span = entries[start + 1 : end]
        foreign = [entry for entry in span if entry["operation"] == CHANGE_SET_STAGED]
        assert len(foreign) == 1, [entry["operation"] for entry in span]
        assert foreign[0]["args"]["change_set_id"] != response["audit_ref"]
        block = commit_block(entries, response["audit_ref"])
        assert block is not None
        assert [entry["operation"] for entry in block.entries] == ["write_element"]
        assert block_is_complete(block)

    def test_the_span_reaches_past_the_apply_to_the_committed_record(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The closing delimiter is written after the session returns; a lock ending
        # at the apply would let a second commit nest inside the first block.
        finished = threading.Event()
        entered = threading.Event()
        second: list[dict[str, Any]] = []
        original = audit_mod.ChangeSetAuditor.committed

        def submit_second() -> None:
            second.append(
                handle_request(_request(project, _UPDATE_SWC_DESCRIPTION), project=project, review=self._approve)
            )
            finished.set()

        def parked(auditor: Any, **kwargs: Any) -> None:
            if not entered.is_set():
                entered.set()
                thread = threading.Thread(target=submit_second)
                thread.start()
                # Serialized, this times out, the passing shape.
                finished.wait(timeout=1.0)
            original(auditor, **kwargs)

        monkeypatch.setattr(audit_mod.ChangeSetAuditor, "committed", parked)

        response = handle_request(_request(project, _UPDATE_PRIORITY), project=project, review=self._approve)
        assert finished.wait(timeout=60), "the second change set never finished; the commit region deadlocked"
        assert response["status"] == "committed" and second[0]["status"] == "committed"

        entries = _entries(project)
        first = commit_block(entries, response["audit_ref"])
        assert first is not None
        assert [entry["operation"] for entry in first.entries] == ["write_element"]
        assert written_element_addresses(first) == _element_addresses(response["delta"])
        assert block_is_complete(first)
        assert _position(entries, CHANGE_SET_COMMITTED, response["audit_ref"]) < _position(
            entries, CHANGE_SET_COMMIT_INTENT, second[0]["audit_ref"]
        )

    def test_the_lock_names_the_root_the_apply_writes_not_the_one_the_request_opened(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `_open` and `stage` read the manifest twice; a `model_root` retargeted
        # between them must not key the lock on a tree this commit never writes.
        shutil.copytree(_model_root(project), project / "model2")
        real_stage = engine_mod.stage

        def retargeting_stage(manifest: Any) -> Any:
            manifest_path = Path(manifest)
            declared = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            declared["model_root"] = "model2"
            manifest_path.write_text(yaml.safe_dump(declared, sort_keys=False), encoding="utf-8")
            return real_stage(manifest)

        keys: list[str] = []
        real_commit_lock = engine_mod.commit_lock

        def recording_commit_lock(root: Any) -> Any:
            keys.append(str(Path(root).resolve()))
            return real_commit_lock(root)

        monkeypatch.setattr(engine_mod, "stage", retargeting_stage)
        monkeypatch.setattr(engine_mod, "commit_lock", recording_commit_lock)
        before = _tree_state(_model_root(project))

        response = handle_request(_request(project, _UPDATE_PRIORITY), project=project, review=self._approve)

        assert response["status"] == "committed"
        assert keys == [str((project / "model2").resolve())], keys
        assert response["baseline"] == baseline_token(str(project / "model2"))
        assert _tree_state(_model_root(project)) == before

    def test_the_records_land_in_the_log_the_writer_entries_land_in(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A `dist_root` retargeted between the two manifest reads must not split one
        # commit across two logs while `audit_ref` claims a complete block.
        real_stage = engine_mod.stage

        def retargeting_stage(manifest: Any) -> Any:
            manifest_path = Path(manifest)
            declared = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            declared["dist_root"] = "dist2"
            manifest_path.write_text(yaml.safe_dump(declared, sort_keys=False), encoding="utf-8")
            return real_stage(manifest)

        monkeypatch.setattr(engine_mod, "stage", retargeting_stage)

        response = handle_request(_request(project, _UPDATE_PRIORITY), project=project, review=self._approve)

        assert response["status"] == "committed"
        opened = AuditLog(str(project / "dist" / AUDIT_FILENAME)).get_entries()
        written = AuditLog(str(project / "dist2" / AUDIT_FILENAME)).get_entries()
        assert [entry["operation"] for entry in opened] == []
        assert [entry["operation"] for entry in written] == [
            CHANGE_SET_STAGED,
            CHANGE_SET_APPROVED,
            CHANGE_SET_COMMIT_INTENT,
            "write_element",
            CHANGE_SET_COMMITTED,
        ]
        block = commit_block(written, response["audit_ref"])
        assert block is not None
        assert [entry["operation"] for entry in block.entries] == ["write_element"]
        assert block_is_complete(block)


class TestASoleEditBetweenTwoWideIntegersIsAnEdit:
    """A bound is an ``int | float`` held exactly, so two 64-bit bounds differ by one.

    Keyed through ``float()``, the sole update between them would answer with an empty delta.
    Does not reach: the same edit as a YAML float literal, rounded by the loader first.
    """

    WIDE = 18446744073709551615
    ADDRESS = {"layer": "implementation_types", "element": "uint64", "path": [{"field": "value_max"}]}

    def _bounded(self, project_root: Path, value: int | float) -> Path:
        path = model_element_path(_model_root(project_root), "uint64", kind="value_type")
        declared = yaml.safe_load(path.read_text(encoding="utf-8"))
        declared["value_max"] = value
        path.write_text(yaml.safe_dump(declared, sort_keys=False), encoding="utf-8", newline="\n")
        return path

    def _update(self, value: int | float) -> dict[str, Any]:
        return {"operation": "update", "address": self.ADDRESS, "value": value}

    def test_it_yields_one_delta_entry_and_a_written_file(self, project: Path) -> None:
        path = self._bounded(project, self.WIDE)

        response = handle_request(
            _request(project, self._update(self.WIDE - 1)), project=project, review=_approver(True)
        )

        assert response["status"] == "committed"
        assert len(response["delta"]) == 1
        assert _element_addresses(response["delta"]) == {("implementation_types", "uint64")}
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["value_max"] == self.WIDE - 1
        assert _current(project, self.ADDRESS) == self.WIDE - 1

    def test_staged_alone_it_already_reports_the_entry(self, project: Path) -> None:
        self._bounded(project, self.WIDE)
        response = handle_request(_request(project, self._update(self.WIDE - 1)), project=project)
        assert response["status"] == "staged"
        assert len(response["delta"]) == 1

    def test_the_same_wide_value_is_still_no_edit(self, project: Path) -> None:
        path = self._bounded(project, self.WIDE)
        before = path.read_bytes()
        response = handle_request(_request(project, self._update(self.WIDE)), project=project, review=_approver(True))
        assert response["delta"] == []
        assert path.read_bytes() == before

    def test_an_integer_and_the_float_that_holds_it_are_still_one_number(self, project: Path) -> None:
        # The rule the float bucket exists for: JSON `1024` reaches `1024.0`.
        path = self._bounded(project, 1024.0)
        before = path.read_bytes()
        response = handle_request(_request(project, self._update(1024)), project=project, review=_approver(True))
        assert response["delta"] == []
        assert path.read_bytes() == before


class TestAStagedFileTheReaderRefusesIsAnsweredInTheClosedEnum:
    """A value holding a lone surrogate is written escaped (``"\\uD800"``), and libyaml refuses the escape.

    Pass D's re-read raises ``ElementParseError``; it is answered as
    ``consistency-violation`` from ``{layer, element}``, never the reader's message (a shadow path).
    Does not reach: the MCP stdio transport, which refuses ``\\ud800`` at the JSON layer first.
    """

    @pytest.mark.parametrize(
        ("layer", "element"), [("swc", "ExampleSWC"), ("interfaces", "IExample")], ids=["swc", "interface"]
    )
    def test_the_staged_re_read_refusal_is_a_consistency_violation_naming_the_element(
        self, project: Path, layer: str, element: str
    ) -> None:
        before = _tree_state(_model_root(project))
        update = {
            "operation": "update",
            "address": {"layer": layer, "element": element, "path": [{"field": "description"}]},
            "value": "a\ud800b",
        }

        response = handle_request(_request(project, update), project=project, review=_approver(True))

        assert response["response"] == "error"
        assert response["error"] == "consistency-violation"
        assert response["addresses"] == [{"layer": layer, "element": element}]
        assert response["message"].startswith(f"the model cannot be read: the '{layer}' element '{element}'")
        assert str(project) not in json.dumps(response, ensure_ascii=False)
        assert _tree_state(_model_root(project)) == before
        assert _operations(project)[-1] == CHANGE_SET_REJECTED


class TestAModelFileTooDeepToReturnIsAnsweredInTheClosedEnum:
    """A model file nested 700 levels is answered like any unparseable file; no ``RecursionError`` escapes."""

    def test_read_answers_consistency_violation_naming_the_element(self, project: Path) -> None:
        [element_file] = _model_root(project).glob("swc/**/ExampleSWC.yaml")
        deep = "deep: " + "[" * 700 + "]" * 700 + "\n"
        element_file.write_text(element_file.read_text(encoding="utf-8") + deep, encoding="utf-8", newline="\n")

        response = handle_request(
            {"operation": "read", "address": {"layer": "swc", "element": "ExampleSWC"}}, project=project
        )

        assert response["response"] == "error"
        assert response["error"] == "consistency-violation"
        assert response["addresses"] == [{"layer": "swc", "element": "ExampleSWC"}]
        assert response["message"].startswith("the model cannot be read: the 'swc' element 'ExampleSWC'")
        assert "recursion" not in response["message"]
        assert str(project) not in json.dumps(response, ensure_ascii=False)


class TestEveryRequestStartsFromAnEmptyParseCache:
    """``handle_request`` clears the YAML parse cache first, so one request's working set is its own.

    Does not reach: server tools calling ``swcraft_tools`` outside the engine, which never
    clear, and concurrent requests, where a clear may evict another's entries (a re-parse,
    never a wrong value).
    """

    def test_handle_request_clears_the_parse_cache_first(self, project: Path) -> None:
        held_over = "name: HeldOverFromAnEarlierRequest\n"
        load_yaml_document(held_over)

        response = handle_request({"operation": "discover"}, project=project)

        assert response["response"] == "discover"
        misses = parse_cache_info().misses
        load_yaml_document(held_over)
        assert parse_cache_info().misses == misses + 1
        hits = parse_cache_info().hits
        load_yaml_document((project / "swcraft.yaml").read_text(encoding="utf-8"))
        assert parse_cache_info().hits == hits + 1


class TestADecomposedLevelThroughTheProtocol:
    """A decomposed ``X(Y)`` is committed and read back as written; ``A(A)`` is refused at the field."""

    ADDRESS = {"layer": "requirements", "element": "REQ_EXAMPLE_001", "path": [{"field": "asil"}]}

    def test_a_decomposed_level_commits_and_reads_back_as_written(self, project: Path) -> None:
        response = handle_request(
            _request(project, {"operation": "update", "address": self.ADDRESS, "value": "B(D)"}),
            project=project,
            review=_approver(True),
        )

        assert response["status"] == "committed"
        read = handle_request({"operation": "read", "address": self.ADDRESS}, project=project)
        assert read["value"] == "B(D)"

    def test_the_form_the_standard_leaves_out_is_a_schema_violation(self, project: Path) -> None:
        before = _tree_state(_model_root(project))

        response = handle_request(
            _request(project, {"operation": "update", "address": self.ADDRESS, "value": "A(A)"}),
            project=project,
            review=_approver(True),
        )

        assert response["error"] == "schema-violation"
        assert response["addresses"] == [self.ADDRESS]
        assert _tree_state(_model_root(project)) == before
