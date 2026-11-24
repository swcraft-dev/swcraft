"""The baseline token, unconditional staging, `stale-baseline` and materialization into the shadow.

The properties pinned, each one the design would fail silently without:

1. The token frames what ``model_fingerprint`` computed and nothing else; equality is the only
   operation on it.
2. The order is fingerprint -> copy -> snapshot-the-copy, so a write landing mid-copy makes the
   commit refuse; only the refusal is asserted, since the ``(fingerprint, snapshot)`` pair is not
   claimed to be consistent (``session.py``'s module docstring).
3. Every change set is staged, scoped or not, so a set that fails halfway leaves nothing behind;
   two writers on one baseline resolve first-commit-wins.
4. A stale staged result is never re-based: the loser is refused whole, not merged.

Materialization reaches the shadow tree only, never the real model or ``dist/``.
"""

from __future__ import annotations

import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import swcraft_tools.editing.staging as staging_mod
import swcraft_tools.model_state as model_state_mod
import swcraft_tools.session as session_mod
import swcraft_tools.writer.elements as elements_mod
import yaml
from conftest import model_element_path
from element_identity_support import declared_identity
from mode_bit_support import read_denied
from rename_pairing_support import spy_on_the_two_pairings
from swcraft_tools.auditor import AuditLog
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.apply import StagedModel, TouchedElement, apply_operations
from swcraft_tools.editing.audit import AUDIT_FILENAME
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.engine import ChangeSetProposal, handle_request
from swcraft_tools.editing.passes import pass_a
from swcraft_tools.editing.protocol import REVISION_TOKEN_PATTERN, ChangeSetRequest
from swcraft_tools.editing.review import render_delta_review
from swcraft_tools.editing.staging import (
    STALE_BASELINE,
    PlacementRefusal,
    RewrittenFile,
    StagedChangeSet,
    StaleBaselineError,
    TargetOccupied,
    TargetVanished,
    baseline_token,
    stage,
    token_for_fingerprint,
)
from swcraft_tools.model_registry import production_registry
from swcraft_tools.session import model_fingerprint
from swcraft_tools.snapshot import load_snapshot
from swcraft_tools.writer.elements import TargetExistsError, TargetMissingError
from swcraft_tools.yaml_load import load_yaml_document
from swcraft_tools.yaml_text_edit import FormRefused

# Fixtures and helpers


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


def _change_set(project_root: Path, *operations: dict[str, Any], baseline: str | None = None) -> ChangeSetRequest:
    """One parsed change-set request against *project_root*'s current baseline."""
    request = {
        "operation": "change_set",
        "baseline": baseline if baseline is not None else baseline_token(_model_root(project_root)),
        "intent": "a test change set",
        "operations": list(operations),
    }
    parsed = pass_a(request)
    assert isinstance(parsed, ChangeSetRequest)
    return parsed


def _audit_operations(project_root: Path) -> list[dict[str, Any]]:
    """The lifecycle records one change set left in the real audit log."""
    return AuditLog(str(project_root / "dist" / AUDIT_FILENAME)).get_entries()


def _apply_into(staging: StagedChangeSet, request: ChangeSetRequest) -> list[str]:
    """Bind, apply and materialize *request* into *staging*'s shadow."""
    outcome = apply_operations(staging.snapshot, request.operations)
    assert not outcome.violations, [violation.message for violation in outcome.violations]
    assert not outcome.inapplicable, [miss.message for miss in outcome.inapplicable]
    return staging.materialize(outcome.touched)


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

#: A file whose name is not the name it declares, in its filesystem-independent form
#: (the storage-target vectors' seed).
_VEHICLE_SPEED_KPH = "name: VehicleSpeed_kph\nkind: value_type\nbase: float32_base\nunit: kph\n"


def _create_alias(name: str) -> dict[str, Any]:
    return {
        "operation": "create",
        "address": {"layer": "implementation_types", "element": name},
        "value": {"name": name, "kind": "value_type", "base": "float32_base", "unit": "kph"},
    }


_CREATE_SPEED_KPH = _create_alias("Speed_kph")
_UPDATE_VEHICLE_SPEED_UNIT = {
    "operation": "update",
    "address": {"layer": "implementation_types", "element": "VehicleSpeed_kph", "path": [{"field": "unit"}]},
    "value": "mps",
}


def _approve(proposal: Any) -> ApprovalDecision:
    """A review boundary that approves the delta (and any weakening it carries)."""
    return ApprovalDecision(
        change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=True
    )


# The token


class TestTheBaselineToken:
    def test_it_is_the_framed_fingerprint_and_nothing_else(self, project: Path) -> None:
        # Derived, so the framing rules stay internal; and derived from THIS
        # digest, never from `server.py`'s cached `_compute_model_hash`.
        model_root = _model_root(project)
        assert baseline_token(model_root) == token_for_fingerprint(model_fingerprint(str(model_root)))

    def test_it_is_framed_as_the_contract_states(self, project: Path) -> None:
        token = baseline_token(_model_root(project))
        assert REVISION_TOKEN_PATTERN.match(token), token
        assert token.startswith("rev1:")

    def test_it_is_not_the_fingerprint(self, project: Path) -> None:
        # A client that could recompute it would turn "what is hashed" into
        # contract. Equality is the only operation on a token.
        model_root = _model_root(project)
        assert model_fingerprint(str(model_root)) not in baseline_token(model_root)

    def test_it_moves_with_any_file_under_the_model_root(self, project: Path) -> None:
        model_root = _model_root(project)
        before = baseline_token(model_root)
        (model_root / "a-note-no-reader-parses.txt").write_text("hand-written", encoding="utf-8")
        assert baseline_token(model_root) != before

    def test_it_is_computed_fresh_on_every_call(self, project: Path) -> None:
        model_root = _model_root(project)
        first = baseline_token(model_root)
        (model_root / "tasks" / "Task_10ms.yaml").write_text("name: Task_10ms\nperiod: 0.02\n", encoding="utf-8")
        assert baseline_token(model_root) != first

    def test_a_missing_model_root_is_an_error_not_a_token_over_nothing(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            baseline_token(tmp_path / "nowhere")


# Staging: the order, and its unconditionality


class TestStaging:
    def test_it_needs_no_scope(self, project: Path) -> None:
        # `--scoped` adds the approval requirement, not the isolation.
        with stage(_manifest(project)) as staging:
            assert Path(staging.model_root).is_dir()
            assert not Path(staging.model_root).is_relative_to(project.resolve())

    def test_it_leaves_the_real_project_untouched(self, project: Path) -> None:
        before = _tree_state(project)
        with stage(_manifest(project)) as staging:
            assert staging.baseline == baseline_token(_model_root(project))
        assert _tree_state(project) == before

    def test_the_baseline_snapshot_is_read_from_the_copy(self, project: Path) -> None:
        with stage(_manifest(project)) as staging:
            shadow_names = [swc.name for swc in staging.snapshot.swcs]
            assert shadow_names == [swc.name for swc in load_snapshot(str(_model_root(project))).swcs]

    def test_the_shadow_is_dropped_on_exit(self, project: Path) -> None:
        with stage(_manifest(project)) as staging:
            shadow = Path(staging.model_root)
        assert not shadow.exists()

    def test_a_write_landing_mid_copy_refuses_the_commit(self, project: Path, monkeypatch: Any) -> None:
        # Pins fingerprint-BEFORE-copy; only the refusal is asserted (see the
        # module docstring).
        model_root = _model_root(project)
        original_copy = session_mod._copy_model_tree

        def racing_copy(source: str, destination: str) -> None:
            (model_root / "tasks" / "Task_20ms.yaml").write_text(
                "name: Task_20ms\nperiod: 0.02\npriority: 4\npartition: PARTITION0\nwcet: 900us\n", encoding="utf-8"
            )
            original_copy(source, destination)

        monkeypatch.setattr(session_mod, "_copy_model_tree", racing_copy)

        with stage(_manifest(project)) as staging:
            _apply_into(staging, _change_set(project, _UPDATE_PRIORITY, baseline=staging.baseline))
            before = _tree_state(model_root)
            with pytest.raises(StaleBaselineError):
                staging.commit(staging.session.diff())
            assert _tree_state(model_root) == before


# The two token checks


class TestTheFailFastCheck:
    def test_a_current_baseline_passes(self, project: Path) -> None:
        with stage(_manifest(project)) as staging:
            staging.require_baseline(staging.baseline)

    def test_the_token_it_compares_against_is_the_one_open_took(self, project: Path) -> None:
        # Not a second, independently sampled fingerprint: a write landing
        # between two samples would leave the client's claim matching a state
        # the session never staged, and the commit would then succeed against it.
        with stage(_manifest(project)) as staging:
            claimed = baseline_token(_model_root(project))
            (_model_root(project) / "tasks" / "Task_10ms.yaml").write_text("name: Task_10ms\n", encoding="utf-8")
            staging.require_baseline(claimed)  # still the token `open()` framed

    def test_a_moved_model_is_refused(self, project: Path) -> None:
        stale = baseline_token(_model_root(project))
        (_model_root(project) / "hardware" / "RAM0.yaml").write_text(
            "name: RAM0\nkind: memory\ntype: RAM\nsize: 65536\naccessible:\n  - CORE0\nthreshold: 0.8\n",
            encoding="utf-8",
        )
        with stage(_manifest(project)) as staging:
            with pytest.raises(StaleBaselineError):
                staging.require_baseline(stale)

    def test_the_refusal_does_not_disclose_the_current_token(self, project: Path) -> None:
        with stage(_manifest(project)) as staging:
            with pytest.raises(StaleBaselineError) as refused:
                staging.require_baseline("rev1:notthetokenatall")
            assert staging.baseline not in str(refused.value)


class TestTheAuthoritativeCheck:
    def test_two_writers_racing_on_one_baseline_resolve_first_commit_wins(self, project: Path) -> None:
        model_root = _model_root(project)
        with stage(_manifest(project)) as first, stage(_manifest(project)) as second:
            assert first.baseline == second.baseline  # one baseline, two writers

            _apply_into(first, _change_set(project, _UPDATE_PRIORITY, baseline=first.baseline))
            _apply_into(second, _change_set(project, _UPDATE_WCET, baseline=second.baseline))

            first.commit(first.session.diff())
            won = _tree_state(model_root)

            with pytest.raises(StaleBaselineError):
                second.commit(second.session.diff())

            # The loser's model is byte-identical to the winner's outcome: its
            # own operation is absent, not merged in.
            assert _tree_state(model_root) == won
            assert "priority: 6" in (model_root / "tasks" / "Task_10ms.yaml").read_text(encoding="utf-8")
            assert "wcet: 1300us" not in (model_root / "tasks" / "Task_10ms.yaml").read_text(encoding="utf-8")

    def test_a_stale_staged_result_is_never_re_based(self, project: Path) -> None:
        # Re-basing would produce a delta nobody reviewed, so a second attempt
        # is refused exactly like the first.
        model_root = _model_root(project)
        with stage(_manifest(project)) as staging:
            _apply_into(staging, _change_set(project, _UPDATE_WCET, baseline=staging.baseline))
            (model_root / "a-concurrent-write.txt").write_text("another writer", encoding="utf-8")
            before = _tree_state(model_root)

            for _ in range(2):
                with pytest.raises(StaleBaselineError):
                    staging.commit(staging.session.diff())
                assert _tree_state(model_root) == before

    def test_an_unmoved_model_commits(self, project: Path) -> None:
        model_root = _model_root(project)
        with stage(_manifest(project)) as staging:
            _apply_into(staging, _change_set(project, _UPDATE_PRIORITY, baseline=staging.baseline))
            applied = staging.commit(staging.session.diff())
        assert [Path(path).name for path in applied] == ["Task_10ms.yaml"]
        assert "priority: 6" in (model_root / "tasks" / "Task_10ms.yaml").read_text(encoding="utf-8")


# Materialization


class TestMaterialization:
    def test_an_update_rewrites_the_element_in_the_shadow_only(self, project: Path) -> None:
        before = _tree_state(project)
        with stage(_manifest(project)) as staging:
            written = _apply_into(staging, _change_set(project, _UPDATE_PRIORITY, baseline=staging.baseline))
            assert written == [str(Path(staging.model_root) / "tasks" / "Task_10ms.yaml")]
            assert "priority: 6" in Path(written[0]).read_text(encoding="utf-8")
        assert _tree_state(project) == before

    def test_a_create_lands_at_the_registry_composed_path(self, project: Path) -> None:
        operation = {
            "operation": "create",
            "address": {"layer": "implementation_types", "element": "Ratio_pct"},
            "value": {
                "name": "Ratio_pct",
                "kind": "value_type",
                "base": "uint8",
                "unit": "%",
                "value_min": 0,
                "value_max": 100,
            },
        }
        with stage(_manifest(project)) as staging:
            written = _apply_into(staging, _change_set(project, operation, baseline=staging.baseline))
            assert written == [str(Path(staging.model_root) / "data_types" / "implementation" / "Ratio_pct.yaml")]
            assert Path(written[0]).is_file()

    def test_a_created_swc_lands_under_its_placement_directory(self, project: Path) -> None:
        # `group` is a placement field, so it reaches the writer as a path
        # component and never as a key in the file.
        operation = {
            "operation": "create",
            "address": {"layer": "swc", "element": "SensorSWC"},
            "value": {
                "name": "SensorSWC",
                "group": "sensors",
                "type": "application",
                "description": "A sensor component",
            },
        }
        with stage(_manifest(project)) as staging:
            written = _apply_into(staging, _change_set(project, operation, baseline=staging.baseline))
            assert written == [str(Path(staging.model_root) / "swc" / "sensors" / "SensorSWC.yaml")]
            assert "group" not in Path(written[0]).read_text(encoding="utf-8")

    def test_a_remove_deletes_the_file_and_leaves_its_directory(self, project: Path) -> None:
        # The protocol never removes a directory, so the layer stays present.
        operation = {"operation": "remove", "address": {"layer": "hardware", "element": "CORE0"}}
        with stage(_manifest(project)) as staging:
            removed = _apply_into(staging, _change_set(project, operation, baseline=staging.baseline))
            assert removed == [str(Path(staging.model_root) / "hardware" / "CORE0.yaml")]
            assert not Path(removed[0]).exists()
            assert Path(staging.model_root, "hardware").is_dir()

    def test_only_touched_elements_are_written(self, project: Path) -> None:
        # `touched` is "the value actually changed"; this keeps
        # `UnexplainedChangeError` unreachable from a well-formed request.
        no_op = {
            "operation": "update",
            "address": {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "priority"}]},
            "value": 5,
        }
        with stage(_manifest(project)) as staging:
            shadow_before = _tree_state(Path(staging.model_root))
            assert _apply_into(staging, _change_set(project, no_op, baseline=staging.baseline)) == []
            assert _tree_state(Path(staging.model_root)) == shadow_before

    def test_an_untouched_element_is_reproduced_byte_for_byte(self, project: Path) -> None:
        # What makes the commit's per-file delta the change set's delta and
        # nothing more.
        with stage(_manifest(project)) as staging:
            shadow = Path(staging.model_root)
            before = _tree_state(shadow)
            _apply_into(staging, _change_set(project, _UPDATE_PRIORITY, baseline=staging.baseline))
            after = _tree_state(shadow)
            changed = [path for path in after if after[path] != before.get(path)]
            assert changed == ["tasks/Task_10ms.yaml"]

    def test_dist_is_untouched(self, project: Path) -> None:
        # A committed change makes the generated ARXML stale; regenerating it
        # stays `build`/`validate`'s job.
        with stage(_manifest(project)) as staging:
            _apply_into(staging, _change_set(project, _UPDATE_PRIORITY, baseline=staging.baseline))
            dist = Path(staging.model_root).parent / "dist"
            assert list(dist.iterdir()) == []

    def test_a_traversal_placement_value_never_reaches_materialization(self, project: Path) -> None:
        # A client path value is refused at bind time: pass B answers
        # `schema-violation` at `group` and the writers are never called. Their
        # own guards in `writer/` are the last line, not the first.
        with stage(_manifest(project)) as staging:
            shadow_before = _tree_state(Path(staging.model_root))
            outcome = apply_operations(
                staging.snapshot,
                _change_set(
                    project,
                    {
                        "operation": "create",
                        "address": {"layer": "swc", "element": "Sensor_SWC"},
                        "value": {"name": "Sensor_SWC", "group": "../escape", "type": "application"},
                    },
                    baseline=staging.baseline,
                ).operations,
            )
            (violation,) = outcome.violations
            assert violation.error == "schema-violation"
            assert violation.addresses[0].path[0].to_json() == {"field": "group"}
            assert outcome.touched == ()
            assert _tree_state(Path(staging.model_root)) == shadow_before

    def test_a_baseline_placement_value_that_is_not_a_segment_is_a_typed_refusal(self, project: Path) -> None:
        # The residue pass B cannot see: a hand-authored flat `model/swc/Foo.yaml`
        # reads as `group: ""`, not a segment. Typed so the engine can answer
        # with a code and an address.
        source = _model_root(project) / "swc" / "example" / "ExampleSWC.yaml"
        (_model_root(project) / "swc" / "FooSWC.yaml").write_text(
            source.read_text(encoding="utf-8").replace("ExampleSWC", "FooSWC"), encoding="utf-8"
        )
        with stage(_manifest(project)) as staging:
            shadow_before = _tree_state(Path(staging.model_root))
            outcome = apply_operations(
                staging.snapshot,
                _change_set(
                    project,
                    {"operation": "remove", "address": {"layer": "swc", "element": "FooSWC"}},
                    baseline=staging.baseline,
                ).operations,
            )
            with pytest.raises(PlacementRefusal) as raised:
                staging.materialize(outcome.touched)
            assert (raised.value.layer, raised.value.element, raised.value.field) == ("swc", "FooSWC", "group")
            assert _tree_state(Path(staging.model_root)) == shadow_before

    def test_a_baseline_identity_that_is_not_a_segment_is_a_typed_refusal(self, project: Path) -> None:
        # A `remove` binds no payload, so its identity comes straight from the
        # baseline; `Task.name` has no traversal guard, so `Sys..A` is a
        # consistent model. Guards the defect where it left `handle_request` as
        # an uncaught `PathSegmentError` with no lifecycle record.
        (_model_root(project) / "tasks" / "Sys..A.yaml").write_text(
            "name: Sys..A\nperiod: 0.01\npriority: 1\npartition: PARTITION0\ndescription: hand-authored\n",
            encoding="utf-8",
        )
        with stage(_manifest(project)) as staging:
            shadow_before = _tree_state(Path(staging.model_root))
            outcome = apply_operations(
                staging.snapshot,
                _change_set(
                    project,
                    {"operation": "remove", "address": {"layer": "tasks", "element": "Sys..A"}},
                    baseline=staging.baseline,
                ).operations,
            )
            with pytest.raises(PlacementRefusal) as raised:
                staging.materialize(outcome.touched)
            # The identity is what the element IS: no field, no path.
            assert (raised.value.layer, raised.value.element, raised.value.field) == ("tasks", "Sys..A", None)
            assert _tree_state(Path(staging.model_root)) == shadow_before

    # Where the protocol writes: a `create` whose plain file name another
    # element's file holds takes the nearest free `~N`; an `update` or `remove`
    # finds its file by the identity inside. The seed is the vectors' own
    # (`Speed_kph.yaml` declaring `VehicleSpeed_kph`), filesystem-independent.

    def test_a_create_whose_plain_file_another_element_holds_takes_the_nearest_free_suffix(self, project: Path) -> None:
        types = _model_root(project) / "data_types" / "implementation"
        (types / "Speed_kph.yaml").write_text(_VEHICLE_SPEED_KPH, encoding="utf-8")
        with stage(_manifest(project)) as staging:
            shadow = Path(staging.model_root)
            occupant = (shadow / "data_types" / "implementation" / "Speed_kph.yaml").read_bytes()
            written = _apply_into(staging, _change_set(project, _CREATE_SPEED_KPH, baseline=staging.baseline))

            assert [Path(path).relative_to(shadow).as_posix() for path in written] == [
                "data_types/implementation/Speed_kph~1.yaml"
            ]
            # The suffix is the file's; the element inside is the one asked for.
            assert declared_identity(shadow / "data_types" / "implementation" / "Speed_kph~1.yaml") == "Speed_kph"
            assert (shadow / "data_types" / "implementation" / "Speed_kph.yaml").read_bytes() == occupant

    def test_a_remove_finds_its_element_by_the_identity_inside_the_file(self, project: Path) -> None:
        types = _model_root(project) / "data_types" / "implementation"
        (types / "Speed_kph.yaml").write_text(_VEHICLE_SPEED_KPH, encoding="utf-8")
        with stage(_manifest(project)) as staging:
            shadow = Path(staging.model_root)
            removed = _apply_into(
                staging,
                _change_set(
                    project,
                    {
                        "operation": "remove",
                        "address": {"layer": "implementation_types", "element": "VehicleSpeed_kph"},
                    },
                    baseline=staging.baseline,
                ),
            )
            assert [Path(path).relative_to(shadow).as_posix() for path in removed] == [
                "data_types/implementation/Speed_kph.yaml"
            ]
            assert not (shadow / "data_types" / "implementation" / "Speed_kph.yaml").exists()
            assert not (shadow / "data_types" / "implementation" / "VehicleSpeed_kph.yaml").exists()

    def test_an_update_lands_in_the_file_that_declares_the_element(self, project: Path) -> None:
        # The write goes to the file the element is IN, never to a path
        # recomputed from its name, so no second declaring file is created.
        types = _model_root(project) / "data_types" / "implementation"
        (types / "Speed_kph.yaml").write_text(_VEHICLE_SPEED_KPH, encoding="utf-8")
        with stage(_manifest(project)) as staging:
            shadow = Path(staging.model_root)
            written = _apply_into(staging, _change_set(project, _UPDATE_VEHICLE_SPEED_UNIT, baseline=staging.baseline))
            assert [Path(path).relative_to(shadow).as_posix() for path in written] == [
                "data_types/implementation/Speed_kph.yaml"
            ]
            assert declared_identity(shadow / "data_types" / "implementation" / "Speed_kph.yaml") == "VehicleSpeed_kph"
            assert "unit: mps" in (shadow / "data_types" / "implementation" / "Speed_kph.yaml").read_text(
                encoding="utf-8"
            )
            assert not (shadow / "data_types" / "implementation" / "VehicleSpeed_kph.yaml").exists()

    def test_a_rewrite_whose_plain_file_holds_another_element_writes_its_own_file(self, project: Path) -> None:
        # The edited element's plain file name holds a DIFFERENT element;
        # writing there would delete it.
        types = _model_root(project) / "data_types" / "implementation"
        (types / "Speed_kph.yaml").write_text(
            "name: Other_kph\nkind: value_type\nbase: float32_base\nunit: kph\n", encoding="utf-8"
        )
        (types / "Alias.yaml").write_text(
            "name: Speed_kph\nkind: value_type\nbase: float32_base\nunit: kph\n", encoding="utf-8"
        )
        with stage(_manifest(project)) as staging:
            shadow = Path(staging.model_root)
            other = (shadow / "data_types" / "implementation" / "Speed_kph.yaml").read_bytes()
            written = _apply_into(
                staging,
                _change_set(
                    project,
                    {
                        "operation": "update",
                        "address": {
                            "layer": "implementation_types",
                            "element": "Speed_kph",
                            "path": [{"field": "unit"}],
                        },
                        "value": "mps",
                    },
                    baseline=staging.baseline,
                ),
            )
            assert [Path(path).relative_to(shadow).as_posix() for path in written] == [
                "data_types/implementation/Alias.yaml"
            ]
            assert (shadow / "data_types" / "implementation" / "Speed_kph.yaml").read_bytes() == other

    def test_a_removal_whose_plain_file_holds_another_element_removes_its_own_file(self, project: Path) -> None:
        types = _model_root(project) / "data_types" / "implementation"
        (types / "Speed_kph.yaml").write_text(
            "name: Other_kph\nkind: value_type\nbase: float32_base\nunit: kph\n", encoding="utf-8"
        )
        (types / "Alias.yaml").write_text(
            "name: Speed_kph\nkind: value_type\nbase: float32_base\nunit: kph\n", encoding="utf-8"
        )
        with stage(_manifest(project)) as staging:
            shadow = Path(staging.model_root)
            other = (shadow / "data_types" / "implementation" / "Speed_kph.yaml").read_bytes()
            removed = _apply_into(
                staging,
                _change_set(
                    project,
                    {"operation": "remove", "address": {"layer": "implementation_types", "element": "Speed_kph"}},
                    baseline=staging.baseline,
                ),
            )
            assert [Path(path).relative_to(shadow).as_posix() for path in removed] == [
                "data_types/implementation/Alias.yaml"
            ]
            assert (shadow / "data_types" / "implementation" / "Speed_kph.yaml").read_bytes() == other

    def test_two_creates_differing_only_in_case_take_the_plain_name_and_the_first_suffix(self, project: Path) -> None:
        # Decided at the write, against the shadow as it stands: in a pre-pass
        # both would choose the plain name, one file on APFS and NTFS. The
        # listing is compared case-insensitively, never asked of the filesystem,
        # so the answer is the same everywhere.
        with stage(_manifest(project)) as staging:
            shadow = Path(staging.model_root)
            written = _apply_into(
                staging,
                _change_set(project, _create_alias("Speed_kph"), _create_alias("speed_kph"), baseline=staging.baseline),
            )
            relative = sorted(Path(path).relative_to(shadow).as_posix() for path in written)
            assert relative == [
                "data_types/implementation/Speed_kph.yaml",
                "data_types/implementation/speed_kph~1.yaml",
            ]
            assert declared_identity(shadow / "data_types" / "implementation" / "Speed_kph.yaml") == "Speed_kph"
            assert declared_identity(shadow / "data_types" / "implementation" / "speed_kph~1.yaml") == "speed_kph"

    def test_a_create_takes_a_plain_name_the_same_change_set_frees_first(self, project: Path) -> None:
        # Removals come before writes and a create is located at its write.
        types = _model_root(project) / "data_types" / "implementation"
        (types / "Speed_kph.yaml").write_text(_VEHICLE_SPEED_KPH, encoding="utf-8")
        with stage(_manifest(project)) as staging:
            shadow = Path(staging.model_root)
            _apply_into(
                staging,
                _change_set(
                    project,
                    {
                        "operation": "remove",
                        "address": {"layer": "implementation_types", "element": "VehicleSpeed_kph"},
                    },
                    _CREATE_SPEED_KPH,
                    baseline=staging.baseline,
                ),
            )
            assert declared_identity(shadow / "data_types" / "implementation" / "Speed_kph.yaml") == "Speed_kph"
            assert not (shadow / "data_types" / "implementation" / "Speed_kph~1.yaml").exists()

    def test_a_file_taken_between_the_listing_and_the_write_is_a_typed_refusal_naming_no_file(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The writer's race refusal names the shadow path; translated, storage
        # never crosses the boundary.
        def taken(model_root: str, target: str, *_args: Any, **_kwargs: Any) -> str:
            raise TargetExistsError(target, f"{Path(model_root) / target} already exists")

        monkeypatch.setattr(staging_mod, "write_element", taken)
        with stage(_manifest(project)) as staging:
            outcome = apply_operations(
                staging.snapshot, _change_set(project, _CREATE_SPEED_KPH, baseline=staging.baseline).operations
            )
            shadow = str(staging.model_root)
            with pytest.raises(TargetOccupied) as raised:
                staging.materialize(outcome.touched)
        assert (raised.value.layer, raised.value.element) == ("implementation_types", "Speed_kph")
        assert ".yaml" not in str(raised.value), "storage never crosses the boundary"
        assert shadow not in str(raised.value)
        assert "taken before it could be written" in str(raised.value)

    def test_a_file_gone_between_the_lookup_and_the_removal_is_a_typed_refusal_naming_no_file(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def gone(model_root: str, target: str, *_args: Any, **_kwargs: Any) -> str:
            raise TargetMissingError(target, f"{Path(model_root) / target} is not there")

        monkeypatch.setattr(staging_mod, "remove_element", gone)
        remove = {"operation": "remove", "address": {"layer": "tasks", "element": "Task_10ms"}}
        with stage(_manifest(project)) as staging:
            outcome = apply_operations(
                staging.snapshot, _change_set(project, remove, baseline=staging.baseline).operations
            )
            shadow = str(staging.model_root)
            with pytest.raises(TargetVanished) as raised:
                staging.materialize(outcome.touched)
        assert (raised.value.layer, raised.value.element) == ("tasks", "Task_10ms")
        assert ".yaml" not in str(raised.value), "storage never crosses the boundary"
        assert shadow not in str(raised.value)
        assert "no file to remove it from" in str(raised.value)

    @pytest.mark.parametrize("verb", ["update", "remove"])
    def test_of_two_files_declaring_the_element_the_one_at_its_composed_name_is_acted_on(
        self, project: Path, verb: str
    ) -> None:
        # The copy lists FIRST, so listing order is not the answer: the plain
        # composed name is preferred, as `free_storage_path` prefers the exact
        # spelling.
        operation = (
            _UPDATE_PRIORITY
            if verb == "update"
            else {
                "operation": "remove",
                "address": {"layer": "tasks", "element": "Task_10ms"},
            }
        )
        with stage(_manifest(project)) as staging:
            tasks = Path(staging.model_root) / "tasks"
            outcome = apply_operations(
                staging.snapshot, _change_set(project, operation, baseline=staging.baseline).operations
            )
            copy = tasks / "Task_0_copy.yaml"
            copy.write_bytes((tasks / "Task_10ms.yaml").read_bytes())
            original = (tasks / "Task_10ms.yaml").read_bytes()
            staging.materialize(outcome.touched)
            assert copy.read_bytes() == original, "the other declaring file is untouched"
            if verb == "update":
                assert (tasks / "Task_10ms.yaml").read_bytes() != original
            else:
                assert not (tasks / "Task_10ms.yaml").exists()

    @pytest.mark.parametrize("verb", ["update", "remove"])
    def test_of_two_files_declaring_the_element_under_other_names_the_first_listed_is_acted_on(
        self, project: Path, verb: str
    ) -> None:
        operation = (
            _UPDATE_PRIORITY
            if verb == "update"
            else {
                "operation": "remove",
                "address": {"layer": "tasks", "element": "Task_10ms"},
            }
        )
        with stage(_manifest(project)) as staging:
            tasks = Path(staging.model_root) / "tasks"
            outcome = apply_operations(
                staging.snapshot, _change_set(project, operation, baseline=staging.baseline).operations
            )
            original = (tasks / "Task_10ms.yaml").read_bytes()
            (tasks / "Task_10ms.yaml").rename(tasks / "b_second.yaml")
            (tasks / "a_first.yaml").write_bytes(original)
            staging.materialize(outcome.touched)
            assert (tasks / "b_second.yaml").read_bytes() == original, "the later-listed file is untouched"
            if verb == "update":
                assert (tasks / "a_first.yaml").read_bytes() != original
            else:
                assert not (tasks / "a_first.yaml").exists()
            assert not (tasks / "Task_10ms.yaml").exists(), "no file is created at the composed name"

    @pytest.mark.parametrize("verb", ["update", "remove"])
    def test_an_element_no_file_of_its_layer_declares_is_still_a_typed_refusal(self, project: Path, verb: str) -> None:
        # Reachable only through an engine defect (the shadow is private); the
        # answer is still a code and an address, never a write creating the
        # element anew.
        operation: dict[str, Any] = (
            _UPDATE_PRIORITY
            if verb == "update"
            else {"operation": "remove", "address": {"layer": "tasks", "element": "Task_10ms"}}
        )
        with stage(_manifest(project)) as staging:
            outcome = apply_operations(
                staging.snapshot, _change_set(project, operation, baseline=staging.baseline).operations
            )
            (Path(staging.model_root) / "tasks" / "Task_10ms.yaml").unlink()
            shadow_before = _tree_state(Path(staging.model_root))
            with pytest.raises(TargetVanished) as raised:
                staging.materialize(outcome.touched)
            assert (raised.value.layer, raised.value.element) == ("tasks", "Task_10ms")
            assert str(staging.model_root) not in str(raised.value)
            assert ".yaml" not in str(raised.value), "storage never crosses the boundary"
            assert _tree_state(Path(staging.model_root)) == shadow_before

    def test_a_create_whose_plain_file_already_declares_its_identity_is_still_a_typed_refusal(
        self, project: Path
    ) -> None:
        # Occupied by self: the fail-closed twin of pass B's check. A create
        # never becomes an overwrite.
        with stage(_manifest(project)) as staging:
            outcome = apply_operations(
                staging.snapshot, _change_set(project, _CREATE_SPEED_KPH, baseline=staging.baseline).operations
            )
            (model_element_path(Path(staging.model_root), "Speed_kph", kind="value_type")).write_text(
                "name: Speed_kph\nkind: value_type\nbase: float32_base\nunit: kph\n", encoding="utf-8"
            )
            with pytest.raises(TargetOccupied) as raised:
                staging.materialize(outcome.touched)
            assert (raised.value.layer, raised.value.element) == ("implementation_types", "Speed_kph")
            assert str(staging.model_root) not in str(raised.value)
            assert ".yaml" not in str(raised.value), "storage never crosses the boundary"

    def test_a_collision_suffix_that_does_not_fit_is_a_typed_refusal_at_the_element(self, project: Path) -> None:
        # At the 198-byte bound the plain file name fits with the commit's
        # staging suffix and `~1` does not; answered like an over-long identity.
        identity = "S" * 198
        tasks = _model_root(project) / "tasks"
        (tasks / f"{identity}.yaml").write_text(
            "name: OtherTask\nperiod: 0.01\npriority: 1\npartition: PARTITION0\n", encoding="utf-8"
        )
        with stage(_manifest(project)) as staging:
            shadow_before = _tree_state(Path(staging.model_root))
            outcome = apply_operations(
                staging.snapshot,
                _change_set(
                    project,
                    {
                        "operation": "create",
                        "address": {"layer": "tasks", "element": identity},
                        "value": {"name": identity, "period": 0.01, "priority": 1, "partition": "PARTITION0"},
                    },
                    baseline=staging.baseline,
                ).operations,
            )
            with pytest.raises(PlacementRefusal) as raised:
                staging.materialize(outcome.touched)
            assert (raised.value.layer, raised.value.element, raised.value.field) == ("tasks", identity, None)
            assert str(staging.model_root) not in str(raised.value)
            assert ".yaml" not in str(raised.value), "storage never crosses the boundary"
            assert _tree_state(Path(staging.model_root)) == shadow_before

    def test_a_layer_is_listed_and_parsed_once_however_many_of_its_elements_are_located(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # One identity -> file index per layer per materialization: each file
        # is read once, and an untouched layer not at all.
        read: list[str] = []
        original = model_state_mod.declared_identity

        def counting(path: Path, identity_field: str) -> str | None:
            read.append(path.name)
            return original(path, identity_field)

        monkeypatch.setattr(model_state_mod, "declared_identity", counting)
        names = ("float32", "uint8", "uint16")
        with stage(_manifest(project)) as staging:
            files = sorted(
                path.name for path in (Path(staging.model_root) / "data_types" / "implementation").glob("*.yaml")
            )
            _apply_into(
                staging,
                _change_set(
                    project,
                    *(
                        {
                            "operation": "update",
                            "address": {
                                "layer": "implementation_types",
                                "element": name,
                                "path": [{"field": "description"}],
                            },
                            "value": f"{name}, described",
                        }
                        for name in names
                    ),
                    baseline=staging.baseline,
                ),
            )
        assert sorted(read) == files

    def test_a_healthy_layer_is_not_refused(self, project: Path) -> None:
        with stage(_manifest(project)) as staging:
            assert _apply_into(staging, _change_set(project, _UPDATE_PRIORITY, baseline=staging.baseline))

    def test_a_misplaced_unrelated_file_does_not_refuse_the_change_set(self, project: Path) -> None:
        # The blast radius is the change set's own writes; a layer-wide sweep
        # made one such file make the whole layer uneditable.
        types = _model_root(project) / "data_types" / "implementation"
        (types / "Speed_kph.yaml").write_text(
            "name: VehicleSpeed_kph\nkind: value_type\nbase: float32_base\nunit: kph\n", encoding="utf-8"
        )
        with stage(_manifest(project)) as staging:
            assert _apply_into(
                staging,
                _change_set(
                    project,
                    {
                        "operation": "update",
                        "address": {
                            "layer": "implementation_types",
                            "element": "float32",
                            "path": [{"field": "description"}],
                        },
                        "value": "IEEE 754 single precision",
                    },
                    baseline=staging.baseline,
                ),
            )

    @pytest.mark.parametrize("relocation", ["", "a/b"])
    def test_an_uncomposable_baseline_placement_does_not_poison_its_layer(self, project: Path, relocation: str) -> None:
        # `group: ""` and `group: "a/b"` are not single segments; composing them
        # raised an uncaught `PathSegmentError` when the whole layer was asked.
        # The change set writes a DIFFERENT, correctly grouped SWC.
        swc_dir = _model_root(project) / "swc"
        misplaced = next(swc_dir.rglob("ExampleConsumerSWC.yaml"))
        moved = swc_dir / relocation / misplaced.name if relocation else swc_dir / misplaced.name
        moved.parent.mkdir(parents=True, exist_ok=True)
        misplaced.rename(moved)
        with stage(_manifest(project)) as staging:
            assert _apply_into(
                staging,
                _change_set(
                    project,
                    {
                        "operation": "update",
                        "address": {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "description"}]},
                        "value": "rewritten",
                    },
                    baseline=staging.baseline,
                ),
            )

    def test_removals_are_applied_before_writes(self, project: Path) -> None:
        # The writers' reserved-name guards glob the tree, so with writes first
        # they judge the baseline plus the new file rather than the outcome, and
        # refused this legal pair. The shadow is private, so no observer sees an
        # intermediate state.
        with stage(_manifest(project)) as staging:
            outcome = apply_operations(
                staging.snapshot,
                _change_set(
                    project,
                    {"operation": "remove", "address": {"layer": "implementation_types", "element": "float32"}},
                    {
                        "operation": "create",
                        "address": {"layer": "base_types", "element": "float32_AppView"},
                        "value": {
                            "name": "float32_AppView",
                            "kind": "base_type",
                            "size": 8,
                            "encoding": "NONE",
                            "native_declaration": "uint8_t",
                        },
                    },
                    baseline=staging.baseline,
                ).operations,
            )
            paths = staging.materialize(outcome.touched)
            assert [Path(path).name for path in paths] == ["float32.yaml", "float32_AppView.yaml"]

    def test_the_target_path_comes_from_the_registry(self, project: Path) -> None:
        # Composition is one-way and lives in exactly one place; staging never
        # spells a directory of its own.
        with stage(_manifest(project)) as staging:
            written = _apply_into(staging, _change_set(project, _UPDATE_PRIORITY, baseline=staging.baseline))
            composed = reg.REGISTRY.compose_path("tasks", "Task_10ms")
            assert written == [str(Path(staging.model_root) / composed)]


# Through the engine


class TestThroughTheEngine:
    def test_a_stale_change_set_is_the_protocol_code(self, project: Path) -> None:
        stale = baseline_token(_model_root(project))
        fixtures.make_stale(project)
        response = fixtures.dispatch(
            {
                "operation": "change_set",
                "baseline": stale,
                "intent": "Raise the priority of Task_10ms.",
                "operations": [_UPDATE_PRIORITY],
            },
            project=project,
            approved_scope=None,
        )
        assert response["response"] == "error"
        assert response["error"] == STALE_BASELINE
        assert response["failure_class"] == "stale"
        # The failure is about the model as a whole, not about any element.
        assert "addresses" not in response
        assert "additional" not in response

    def test_an_update_of_an_element_stored_under_another_name_commits_into_its_own_file(self, project: Path) -> None:
        # No response carries the file.
        (model_element_path(_model_root(project), "Speed_kph", kind="value_type")).write_text(
            _VEHICLE_SPEED_KPH, encoding="utf-8"
        )
        response = fixtures.dispatch(
            {
                "operation": "change_set",
                "baseline": baseline_token(_model_root(project)),
                "intent": "record vehicle speed in metres per second",
                "operations": [_UPDATE_VEHICLE_SPEED_UNIT],
            },
            project=project,
            approved_scope=None,
            review=_approve,
        )
        assert response["status"] == "committed", response
        types = _model_root(project) / "data_types" / "implementation"
        assert "unit: mps" in (types / "Speed_kph.yaml").read_text(encoding="utf-8")
        assert not (types / "VehicleSpeed_kph.yaml").exists()
        assert "Speed_kph.yaml" not in str(response)

    def test_an_unscoped_set_that_fails_halfway_leaves_nothing_behind(self, project: Path) -> None:
        # The model root, not the project root: the rejection record lands in
        # `dist/audit.jsonl` in every run.
        before = _tree_state(_model_root(project))
        response = fixtures.dispatch(
            {
                "operation": "change_set",
                "baseline": baseline_token(_model_root(project)),
                "intent": "one good operation and one that cannot bind",
                "operations": [
                    _UPDATE_PRIORITY,
                    {
                        "operation": "update",
                        "address": {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "wcet"}]},
                        "value": "-1us",
                    },
                ],
            },
            project=project,
            approved_scope=None,
        )
        assert response["response"] == "error"
        assert response["error"] == "schema-violation"
        assert _tree_state(_model_root(project)) == before

    def test_an_accepted_set_is_staged_and_the_real_model_is_untouched(self, project: Path) -> None:
        # Without a review boundary an accepted set ends `staged`.
        before = _tree_state(_model_root(project))
        response = fixtures.dispatch(
            {
                "operation": "change_set",
                "baseline": baseline_token(_model_root(project)),
                "intent": "a well-formed change set",
                "operations": [_UPDATE_PRIORITY],
            },
            project=project,
            approved_scope=None,
        )
        assert response["status"] == "staged"
        assert "audit_ref" not in response
        assert _tree_state(_model_root(project)) == before

    def test_a_baseline_identity_that_is_not_a_segment_is_answered_not_raised(self, project: Path) -> None:
        # Every change set produces a lifecycle record whatever its outcome;
        # the raised `PathSegmentError` this guards left none.
        (_model_root(project) / "tasks" / "Sys..A.yaml").write_text(
            "name: Sys..A\nperiod: 0.01\npriority: 1\npartition: PARTITION0\ndescription: hand-authored\n",
            encoding="utf-8",
        )
        before = _tree_state(_model_root(project))

        response = fixtures.dispatch(
            {
                "operation": "change_set",
                "baseline": baseline_token(_model_root(project)),
                "intent": "remove a hand-authored task",
                "operations": [{"operation": "remove", "address": {"layer": "tasks", "element": "Sys..A"}}],
            },
            project=project,
            approved_scope=None,
        )

        assert response["error"] == "schema-violation"
        # No path: the identity is what the element IS, as for the `update`.
        assert response["addresses"] == [{"layer": "tasks", "element": "Sys..A"}]
        assert [entry["operation"] for entry in _audit_operations(project)] == ["change_set_rejected"]
        assert _tree_state(_model_root(project)) == before

    def test_an_identity_whose_suffix_composes_an_illegal_segment_is_answered_not_raised(self, project: Path) -> None:
        # `A.` passes the identity and segment guards but composes `A..yaml`;
        # the registry holds the composed file name to the segment rule, so two
        # composers cannot disagree mid-materialization.
        before = _tree_state(_model_root(project))

        response = fixtures.dispatch(
            {
                "operation": "change_set",
                "baseline": baseline_token(_model_root(project)),
                "intent": "create a task whose identity ends in a dot",
                "operations": [
                    {
                        "operation": "create",
                        "address": {"layer": "tasks", "element": "A."},
                        "value": {
                            "name": "A.",
                            "period": 0.01,
                            "priority": 1,
                            "partition": "PARTITION0",
                            "description": "trailing dot",
                        },
                    }
                ],
            },
            project=project,
            approved_scope=None,
        )

        assert response["error"] == "schema-violation"
        assert response["addresses"] == [{"layer": "tasks", "element": "A."}]
        assert [entry["operation"] for entry in _audit_operations(project)] == ["change_set_rejected"]
        assert _tree_state(_model_root(project)) == before

    def test_a_create_whose_plain_file_another_element_holds_commits_and_answers_as_any_create(
        self, project: Path
    ) -> None:
        # A suffixed create answers as a plain one: storage never crosses the
        # boundary.
        types = _model_root(project) / "data_types" / "implementation"
        (types / "Speed_kph.yaml").write_text(_VEHICLE_SPEED_KPH, encoding="utf-8")
        occupant = (types / "Speed_kph.yaml").read_bytes()
        request = {
            "operation": "change_set",
            "baseline": baseline_token(_model_root(project)),
            "intent": "add a shorter alias",
            "operations": [_CREATE_SPEED_KPH],
        }

        response = fixtures.dispatch(request, project=project, approved_scope=None, review=_approve)

        assert response["status"] == "committed", response
        assert declared_identity(types / "Speed_kph~1.yaml") == "Speed_kph"
        assert (types / "Speed_kph.yaml").read_bytes() == occupant
        assert "~1" not in str(response) and ".yaml" not in str(response)
        # The same exchange against a model where nothing holds the plain name
        # answers the same delta.
        clean = fixtures.build_fixture(project.parent / "clean", "read-swc-element")
        request["baseline"] = baseline_token(_model_root(clean))
        plain = fixtures.dispatch(request, project=clean, approved_scope=None, review=_approve)
        assert plain["delta"] == response["delta"]

    def test_two_creates_differing_only_in_case_commit_to_two_files(self, project: Path) -> None:
        response = fixtures.dispatch(
            {
                "operation": "change_set",
                "baseline": baseline_token(_model_root(project)),
                "intent": "two aliases whose names differ only in case",
                # The pair shares `/DataTypes` ignoring case
                # (`rule:name.name-space-collision`), so the second goes to a
                # sub-package; the two files stay flat.
                "operations": [
                    _create_alias("Speed_kph"),
                    {
                        **_create_alias("speed_kph"),
                        "value": {**_create_alias("speed_kph")["value"], "package": "Legacy"},
                    },
                ],
            },
            project=project,
            approved_scope=None,
            review=_approve,
        )
        assert response["status"] == "committed", response
        types = _model_root(project) / "data_types" / "implementation"
        assert declared_identity(types / "Speed_kph.yaml") == "Speed_kph"
        assert declared_identity(types / "speed_kph~1.yaml") == "speed_kph"

    def test_a_collision_suffix_that_does_not_fit_is_answered_at_the_element(self, project: Path) -> None:
        identity = "S" * 198
        tasks = _model_root(project) / "tasks"
        (tasks / f"{identity}.yaml").write_text(
            "name: OtherTask\nperiod: 0.01\npriority: 1\npartition: PARTITION0\n", encoding="utf-8"
        )
        before = _tree_state(_model_root(project))

        response = fixtures.dispatch(
            {
                "operation": "change_set",
                "baseline": baseline_token(_model_root(project)),
                "intent": "create a task whose plain file name another task holds",
                "operations": [
                    {
                        "operation": "create",
                        "address": {"layer": "tasks", "element": identity},
                        "value": {"name": identity, "period": 0.01, "priority": 1, "partition": "PARTITION0"},
                    }
                ],
            },
            project=project,
            approved_scope=None,
        )

        assert response["error"] == "schema-violation", response
        assert response["addresses"] == [{"layer": "tasks", "element": identity}]
        assert ".yaml" not in response["message"] and "~1" not in response["message"]
        assert [entry["operation"] for entry in _audit_operations(project)] == ["change_set_rejected"]
        assert _tree_state(_model_root(project)) == before

    def test_a_whole_map_value_change_reaches_staged(self, project: Path) -> None:
        # The key set does not move, so this is a content change, not a
        # membership change (comparing the mapping's values refused it).
        response = fixtures.dispatch(
            {
                "operation": "change_set",
                "baseline": baseline_token(_model_root(project)),
                "intent": "raise the RAM budget",
                "operations": [
                    {
                        "operation": "update",
                        "address": {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "memory"}]},
                        "value": {"RAM0": 4096},
                    }
                ],
            },
            project=project,
            approved_scope=None,
        )

        assert response["status"] == "staged"
        assert [entry["after"] for entry in response["delta"]] == [4096]

    def test_no_shadow_survives_a_change_set(self, project: Path, monkeypatch: Any) -> None:
        created: list[str] = []
        original = session_mod.tempfile.mkdtemp

        def recording_mkdtemp(*args: Any, **kwargs: Any) -> str:
            path = original(*args, **kwargs)
            created.append(path)
            return path

        monkeypatch.setattr(session_mod.tempfile, "mkdtemp", recording_mkdtemp)
        fixtures.dispatch(
            {
                "operation": "change_set",
                "baseline": baseline_token(_model_root(project)),
                "intent": "a well-formed change set",
                "operations": [_UPDATE_PRIORITY],
            },
            project=project,
            approved_scope=None,
        )
        assert created and all(not Path(path).exists() for path in created)

    def test_discover_and_read_still_need_no_shadow(self, project: Path, monkeypatch: Any) -> None:
        # Staging is the change set's, not the protocol's.
        def refuse(*args: Any, **kwargs: Any) -> str:
            raise AssertionError("a read operation must not stage a shadow")

        monkeypatch.setattr(session_mod.tempfile, "mkdtemp", refuse)
        assert fixtures.dispatch({"operation": "discover"}, project=project, approved_scope=None)["response"] == (
            "discover"
        )
        assert (
            fixtures.dispatch(
                {"operation": "read", "address": {"layer": "tasks", "element": "Task_10ms"}},
                project=project,
                approved_scope=None,
            )["response"]
            == "read"
        )


class TestTheProjectSeam:
    def test_a_change_set_reaches_the_manifest_of_a_preloaded_project(self, project: Path) -> None:
        # `SwcraftProject` carries no manifest path, so the seam uses the
        # conventional one inside the project root.
        from swcraft_tools.project import require_project

        loaded = require_project(_manifest(project))
        response = fixtures.dispatch(
            {
                "operation": "change_set",
                "baseline": baseline_token(_model_root(project)),
                "intent": "a well-formed change set",
                "operations": [_UPDATE_PRIORITY],
            },
            project=loaded,
            approved_scope=None,
        )
        assert response["status"] == "staged"

    def test_a_missing_manifest_is_answered_with_a_code_and_names_no_path(self, tmp_path: Path) -> None:
        # The single entry point owes every condition a code and names no
        # stored path. The client did not author the project, so the code is
        # `consistency-violation`, as `unparseable_element_violation` decides.
        missing = tmp_path / "nowhere"
        response = fixtures.dispatch({"operation": "discover"}, project=missing, approved_scope=None)
        assert response["error"] == "consistency-violation"
        # No element is at fault, so none is named.
        assert "addresses" not in response
        assert str(missing) not in response["message"]
        assert "swcraft.yaml" in response["message"]

    #: The three request shapes, minimal and well-formed. Every seam fault is
    #: asked of all three: each reached a different exception for one condition.
    _SHAPES: tuple[tuple[str, dict[str, Any]], ...] = (
        ("discover", {"operation": "discover"}),
        ("read", {"operation": "read", "address": {"layer": "swc"}}),
        (
            "change_set",
            {
                "operation": "change_set",
                # Shape-valid, not real: `_open` answers before staleness is asked.
                "baseline": "rev1:" + "0" * 32,
                "intent": "a well-formed change set",
                "operations": [_UPDATE_PRIORITY],
            },
        ),
    )

    @pytest.mark.parametrize(("shape", "request_"), _SHAPES, ids=[s[0] for s in _SHAPES])
    def test_no_manifest_is_answered_for_every_request_shape(
        self, tmp_path: Path, shape: str, request_: dict[str, Any]
    ) -> None:
        response = fixtures.dispatch(request_, project=tmp_path / "nowhere", approved_scope=None)

        assert response["error"] == "consistency-violation", shape
        assert response["failure_class"] == "invalid", shape

    @pytest.mark.parametrize(("shape", "request_"), _SHAPES, ids=[s[0] for s in _SHAPES])
    @pytest.mark.parametrize("manifest", ["not: [a\n", "just_a_scalar\n", "unrelated_key: 1\n"])
    def test_a_project_that_is_not_one_is_answered_for_every_request_shape(
        self, tmp_path: Path, shape: str, request_: dict[str, Any], manifest: str
    ) -> None:
        # YAML that does not parse, a document that is not a mapping, a mapping
        # that is not a manifest.
        root = tmp_path / "not-a-project"
        root.mkdir()
        (root / "swcraft.yaml").write_text(manifest, encoding="utf-8")

        response = fixtures.dispatch(request_, project=root, approved_scope=None)

        assert response["error"] == "consistency-violation", (shape, manifest)
        assert str(root) not in str(response), (shape, manifest)

    @pytest.mark.parametrize(("shape", "request_"), _SHAPES, ids=[s[0] for s in _SHAPES])
    def test_an_absent_model_root_is_answered_for_every_request_shape(
        self, project: Path, shape: str, request_: dict[str, Any]
    ) -> None:
        shutil.rmtree(_model_root(project))

        response = fixtures.dispatch(request_, project=project, approved_scope=None)

        assert response["error"] == "consistency-violation", shape
        assert "model root" in response["message"], shape
        assert str(project) not in str(response), shape

    @pytest.mark.parametrize(("shape", "request_"), _SHAPES, ids=[s[0] for s in _SHAPES])
    def test_an_unreadable_model_root_is_answered_for_every_request_shape(
        self, project: Path, shape: str, request_: dict[str, Any]
    ) -> None:
        # `os.scandir` and not `is_dir()`: a directory that exists and cannot be
        # listed is the same condition for every caller downstream.
        root = _model_root(project)
        with read_denied(root):
            response = fixtures.dispatch(request_, project=project, approved_scope=None)

        assert response["error"] == "consistency-violation", shape
        assert "model root" in response["message"], shape

    @pytest.mark.parametrize(
        "scope",
        [
            "not a list",
            5,
            True,
            {"operation": "update", "address": {"layer": "swc"}},
            [{"operation": "update"}],
            [{"address": {"layer": "swc"}}],
            [{"operation": "sideways", "address": {"layer": "swc"}}],
            [{"operation": "update", "address": "swc"}],
            ["not an object"],
        ],
    )
    def test_a_malformed_approved_scope_is_answered_rather_than_raised(self, project: Path, scope: Any) -> None:
        # The shape refusal pass A makes, so pass A's code; fail-closed: a scope
        # that cannot be parsed authorizes nothing.
        response = fixtures.dispatch(
            {
                "operation": "change_set",
                "baseline": baseline_token(_model_root(project)),
                "intent": "a well-formed change set",
                "operations": [_UPDATE_PRIORITY],
            },
            project=project,
            approved_scope=scope,
        )

        assert response["error"] == "schema-violation", scope
        assert response.get("status") != "staged"

    def test_a_malformed_scope_still_writes_the_lifecycle_record(self, project: Path) -> None:
        # Every rejection writes its lifecycle record; a bare `raise` skipped it.
        fixtures.dispatch(
            {
                "operation": "change_set",
                "baseline": baseline_token(_model_root(project)),
                "intent": "a well-formed change set",
                "operations": [_UPDATE_PRIORITY],
            },
            project=project,
            approved_scope="not a list",
        )

        operations = [entry["operation"] for entry in _audit_operations(project)]
        assert operations.count("change_set_rejected") == 1

    def test_handle_requests_raises_list_is_true_of_the_seams_own_arguments(self, tmp_path: Path) -> None:
        # No argument of `handle_request` raises, as a sweep so a new argument
        # fault either answers with a code or fails here.
        faults: list[tuple[str, dict[str, Any]]] = []
        for name, request_ in self._SHAPES:
            for label, kwargs in (
                ("no manifest", {"project": tmp_path / "nowhere", "approved_scope": None}),
                ("malformed scope", {"project": tmp_path / "nowhere", "approved_scope": "not a list"}),
            ):
                try:
                    answer = fixtures.dispatch(request_, **kwargs)
                except Exception as raised:  # noqa: BLE001 - the property under test
                    faults.append((f"{name}/{label}", {"raised": repr(raised)}))
                    continue
                if "error" not in answer:
                    faults.append((f"{name}/{label}", answer))
        assert faults == []


class TestLocateSpellsTheFileTheStoreWrites:
    """`_locate` asks its own question and derives its own verb, against the spelling the store writes.

    Called directly: a component directly under ``swc/`` (placement ``""``) is refused before
    materialization, and the answer must not depend on that refusal standing.
    """

    _PUBLISHED = {"name": "Foo", "type": "application", "group": ""}

    def _flat_model(self, tmp_path: Path) -> Path:
        model_root = tmp_path / "model"
        (model_root / "swc").mkdir(parents=True)
        (model_root / "swc" / "Foo.yaml").write_text("name: Foo\ntype: application\n", encoding="utf-8", newline="\n")
        return model_root

    def _locate(self, model_root: Path, element: Any) -> str:
        return staging_mod._locate(
            reg.build_registry(model_registry=production_registry()),
            element,
            {"group": ""},
            shadow=model_root,
            kernel=production_registry(),
            stored={},
        )

    def test_each_verb_answers_the_file_and_none_of_them_is_named(self, tmp_path: Path) -> None:
        model_root = self._flat_model(tmp_path)

        removal = TouchedElement("swc", "Foo", self._PUBLISHED, None)
        update = TouchedElement("swc", "Foo", self._PUBLISHED, self._PUBLISHED)
        create = TouchedElement("swc", "Bar", None, {**self._PUBLISHED, "name": "Bar"})

        assert self._locate(model_root, removal) == "swc/Foo.yaml"
        assert self._locate(model_root, update) == "swc/Foo.yaml"
        assert self._locate(model_root, create) == "swc/Bar.yaml"


class TestStagingIsNotAWayIntoTheRealModel:
    def test_the_shadow_model_root_is_never_the_real_one(self, project: Path) -> None:
        with stage(_manifest(project)) as staging:
            assert Path(staging.model_root).resolve() != _model_root(project).resolve()

    def test_abandon_is_idempotent(self, project: Path) -> None:
        staging = stage(_manifest(project))
        staging.abandon()
        staging.abandon()

    def test_a_project_whose_model_root_vanished_cannot_be_staged(self, tmp_path: Path) -> None:
        root = fixtures.build_fixture(tmp_path / "project", "read-swc-element")
        shutil.rmtree(root / "model")
        with pytest.raises(FileNotFoundError):
            stage(_manifest(root))


#: The fixture's task file in a layout SWCraft never writes, holding what a whole
#: write would drop. Comments only: pass D refuses every change set while a file
#: holds an undeclared key; that key is held in `test_change_set_text_oracle.py`
#: and refused below.
_HAND_TASK = (
    "# the control cycle\n"
    "priority: 5   # above diagnostics\n"
    "name: Task_10ms\n"
    "period: 0.01\n"
    "partition: PARTITION0\n"
    "wcet: 1200us\n"
    "# reviewed by someone\n"
    "description: 10 ms application task\n"
    "runnables:\n"
    "- swc: ExampleConsumerSWC\n"
    "  runnable: ExampleConsumerRun\n"
)
_FALLBACK_REASON = "the file has mixed line ends"


def _task_file(project_root: Path) -> Path:
    return _model_root(project_root) / "tasks" / "Task_10ms.yaml"


def _swc_file(project_root: Path) -> Path:
    return _model_root(project_root) / "swc" / "example" / "ExampleSWC.yaml"


def _writer_form_of_the_task(project_root: Path) -> str:
    """The fixture's task file as the writer would write it, stated apart from the module's dump."""
    data = load_yaml_document(_task_file(project_root).read_text(encoding="utf-8"))
    return yaml.safe_dump(data, default_flow_style=False, sort_keys=False)


def _mixed_line_ends(path: Path) -> None:
    """Give *path* a first line ending in CRLF and the rest in LF, a form the line edit refuses."""
    text = path.read_bytes().decode("utf-8")
    path.write_bytes(text.replace("\n", "\r\n", 1).encode("utf-8"))


_UPDATE_SWC_DESCRIPTION = {
    "operation": "update",
    "address": {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "description"}]},
    "value": "A described example component",
}


class TestAnUpdateChangesOnlyItsLines:
    @staticmethod
    def _committed(project_root: Path, *operations: dict[str, Any], seen: list[Any] | None = None) -> dict[str, Any]:
        def review(proposal: Any) -> ApprovalDecision:
            if seen is not None:
                seen.append(proposal)
            return _approve(proposal)

        request = {
            "operation": "change_set",
            "baseline": baseline_token(_model_root(project_root)),
            "operations": list(operations),
        }
        return handle_request(request, project=project_root, approved_scope=None, review=review)

    def test_a_committed_update_changes_its_line_and_no_other(self, project: Path) -> None:
        _task_file(project).write_bytes(_HAND_TASK.encode("utf-8"))
        seen: list[Any] = []

        response = self._committed(project, _UPDATE_PRIORITY, seen=seen)

        assert response["status"] == "committed", response
        assert _task_file(project).read_bytes().decode("utf-8") == _HAND_TASK.replace("priority: 5", "priority: 6")
        assert seen[0].rewritten == ()

    def test_a_file_the_edit_cannot_handle_is_written_whole_and_recorded(self, project: Path) -> None:
        _mixed_line_ends(_task_file(project))
        seen: list[Any] = []

        response = self._committed(project, _UPDATE_PRIORITY, seen=seen)

        assert response["status"] == "committed", response
        assert seen[0].rewritten == (RewrittenFile("tasks", "Task_10ms", _FALLBACK_REASON),)
        written = _task_file(project).read_bytes()
        assert b"\r" not in written
        assert b"priority: 6\n" in written

    def test_the_reviewed_text_names_the_fallback(self, project: Path) -> None:
        _mixed_line_ends(_task_file(project))
        seen: list[Any] = []

        self._committed(project, _UPDATE_PRIORITY, seen=seen)

        assert f"  - tasks/Task_10ms: {_FALLBACK_REASON}" in render_delta_review(seen[0]).splitlines()

    def test_a_fallback_and_an_edit_in_one_change_set(self, project: Path) -> None:
        _mixed_line_ends(_task_file(project))
        swc_before = _swc_file(project).read_bytes().decode("utf-8")
        seen: list[Any] = []

        response = self._committed(project, _UPDATE_PRIORITY, _UPDATE_SWC_DESCRIPTION, seen=seen)

        assert response["status"] == "committed", response
        assert [(entry.layer, entry.identity) for entry in seen[0].rewritten] == [("tasks", "Task_10ms")]
        assert _swc_file(project).read_bytes().decode("utf-8") == swc_before.replace(
            "Example software component for testing and demonstration", "A described example component"
        )

    def test_a_file_in_the_writers_form_is_no_row_of_the_warning(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The fixture's task file stands as the writer writes it, comment-free.
        def refuse(*_args: Any) -> str:
            raise FormRefused("the change touches too many places in the file")

        monkeypatch.setattr(elements_mod, "edit_element_text", refuse)
        before = _task_file(project).read_bytes().decode("utf-8")
        assert before == _writer_form_of_the_task(project)
        seen: list[Any] = []

        response = self._committed(project, _UPDATE_PRIORITY, seen=seen)

        assert response["status"] == "committed", response
        assert seen[0].rewritten == ()
        assert "WARNING" not in render_delta_review(seen[0])
        assert _task_file(project).read_bytes().decode("utf-8") == before.replace("priority: 5", "priority: 6")
        entries = AuditLog(str(project / "dist" / AUDIT_FILENAME)).get_entries()
        (written,) = [entry for entry in entries if entry["operation"] == "write_element"]
        assert written["args"]["form"] == "whole"
        assert written["args"]["reason"] == "the change touches too many places in the file"

    def test_two_updates_of_one_element_in_a_foreign_layout(self, project: Path) -> None:
        _task_file(project).write_bytes(_HAND_TASK.encode("utf-8"))
        description = {
            "operation": "update",
            "address": {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "description"}]},
            "value": "the fast cycle",
        }
        seen: list[Any] = []

        response = self._committed(project, _UPDATE_PRIORITY, description, seen=seen)

        assert response["status"] == "committed", response
        expected = _HAND_TASK.replace("priority: 5", "priority: 6").replace(
            "description: 10 ms application task", "description: the fast cycle"
        )
        assert _task_file(project).read_bytes().decode("utf-8") == expected
        assert seen[0].rewritten == ()

    def test_an_update_edits_the_suffixed_file_that_declares_the_element(self, project: Path) -> None:
        suffixed = _task_file(project).with_name("Task_10ms~1.yaml")
        _task_file(project).unlink()
        suffixed.write_bytes(_HAND_TASK.encode("utf-8"))

        response = self._committed(project, _UPDATE_PRIORITY)

        assert response["status"] == "committed", response
        assert suffixed.read_bytes().decode("utf-8") == _HAND_TASK.replace("priority: 5", "priority: 6")
        assert not _task_file(project).exists(), "a storage name never changes"

    def test_a_new_element_is_a_whole_file_and_no_fallback(self, project: Path) -> None:
        seen: list[Any] = []

        response = self._committed(project, _create_alias("Speed_kph"), seen=seen)

        assert response["status"] == "committed", response
        assert seen[0].rewritten == ()

    def test_a_declined_change_set_moves_no_byte(self, project: Path) -> None:
        _mixed_line_ends(_task_file(project))
        before = _tree_state(_model_root(project))

        def decline(proposal: Any) -> ApprovalDecision:
            return ApprovalDecision(change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=False)

        request = {
            "operation": "change_set",
            "baseline": baseline_token(_model_root(project)),
            "operations": [_UPDATE_PRIORITY, _UPDATE_SWC_DESCRIPTION],
        }
        response = handle_request(request, project=project, approved_scope=None, review=decline)

        assert response["status"] == "abandoned"
        assert _tree_state(_model_root(project)) == before

    def test_a_refused_change_set_moves_no_byte(self, project: Path) -> None:
        _task_file(project).write_bytes(_HAND_TASK.encode("utf-8"))
        before = _tree_state(_model_root(project))
        dangling = {
            "operation": "update",
            "address": {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "partition"}]},
            "value": "NO_SUCH_PARTITION",
        }

        response = self._committed(project, dangling)

        assert response["response"] == "error", response
        assert _tree_state(_model_root(project)) == before

    def test_materialize_records_the_fallback_on_the_staged_set(self, project: Path) -> None:
        _mixed_line_ends(_task_file(project))
        request = _change_set(project, _UPDATE_PRIORITY)

        with stage(_manifest(project)) as staging:
            assert staging.rewritten == ()
            _apply_into(staging, request)
            assert staging.rewritten == (RewrittenFile("tasks", "Task_10ms", _FALLBACK_REASON),)

    def test_a_real_guard_answers_with_its_code_on_an_edited_text(self, project: Path) -> None:
        # `edited_text` computes a text for the illegal value; the guard still
        # refuses it.
        path = _swc_file(project)
        path.write_bytes(b"# a comment\n" + path.read_bytes())
        before = _tree_state(_model_root(project))
        ports = load_yaml_document(path.read_text(encoding="utf-8"))["ports"]
        illegal = {
            "operation": "create",
            "address": {
                "layer": "swc",
                "element": "ExampleSWC",
                "path": [{"field": "ports"}, {"member": "a b"}],
            },
            "value": {**ports[-1], "name": "a b"},
        }

        response = self._committed(project, illegal)

        assert response.get("error") == "invalid-identifier", response
        assert _tree_state(_model_root(project)) == before

    def test_the_writer_refusals_keep_their_path(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # `edited_text` raises nothing, so a guard's `ValueError` still comes from
        # `write_element`, on an edited text as on a whole write.
        def refuse(*_args: Any, **_kwargs: Any) -> str:
            raise ValueError("a guard refused it")

        monkeypatch.setattr(staging_mod, "write_element", refuse)
        request = _change_set(project, _UPDATE_PRIORITY)
        with stage(_manifest(project)) as staging, pytest.raises(ValueError, match="a guard refused it"):
            _apply_into(staging, request)


# The rename pairing in staging


def _published_element(project_root: Path, layer: str, identity: str) -> dict[str, Any]:
    """One baseline element in the staged tree's published vocabulary, placement included."""
    staged = StagedModel.from_snapshot(load_snapshot(str(_model_root(project_root))))
    element = staged.get(staged.registry.layer(layer), identity)
    assert element is not None, (layer, identity)
    return deepcopy(element)


def _identity_field(layer: str) -> str:
    return "id" if layer == "requirements" else "name"


def _renamed(layer: str, before: dict[str, Any], now: str, **changes: Any) -> tuple[TouchedElement, TouchedElement]:
    """A removal of *before* and the creation of its copy under *now*, with *changes* applied to the copy."""
    field = _identity_field(layer)
    after = {**deepcopy(before), field: now, **changes}
    return TouchedElement(layer, before[field], deepcopy(before), None), TouchedElement(layer, now, None, after)


def _pairs(*touched: TouchedElement) -> Any:
    return staging_mod._rename_pairs(touched, reg.or_current(None))


def _with_init(interface: dict[str, Any], value: Any) -> dict[str, Any]:
    """*interface* with its one data element's ``init`` set to *value*."""
    changed = deepcopy(interface)
    changed["data_elements"][0]["init"] = value
    return changed


class TestTheRenamePairing:
    """One definition of a rename, over the published values the delta carries."""

    def test_a_plain_rename_pairs(self, project: Path) -> None:
        renames = _pairs(*_renamed("swc", _published_element(project, "swc", "ExampleSWC"), "SpeedSensorSWC"))

        assert renames.pairs == (("swc", "ExampleSWC", "SpeedSensorSWC"),)
        assert renames.unpaired_removals == renames.unpaired_creations == ()

    def test_a_requirement_rename_pairs_on_its_id(self, project: Path) -> None:
        before = _published_element(project, "requirements", "REQ_EXAMPLE_001")

        renames = _pairs(*_renamed("requirements", before, "REQ_EXAMPLE_002"))

        assert renames.pairs == (("requirements", "REQ_EXAMPLE_001", "REQ_EXAMPLE_002"),)

    def test_a_rename_across_groups_does_not_pair(self, project: Path) -> None:
        before = _published_element(project, "swc", "ExampleSWC")
        assert before["group"] == "example"

        renames = _pairs(*_renamed("swc", before, "SpeedSensorSWC", group="other"))

        assert renames.pairs == ()
        assert renames.unpaired_removals == (("swc", "ExampleSWC"),)
        assert renames.unpaired_creations == (("swc", "SpeedSensorSWC"),)

    def test_a_rename_with_an_edit_does_not_pair(self, project: Path) -> None:
        before = _published_element(project, "swc", "ExampleSWC")

        renames = _pairs(*_renamed("swc", before, "SpeedSensorSWC", description="Renamed and reworded"))

        assert renames.pairs == ()
        assert renames.ambiguities == ()

    def test_twins_renamed_together_do_not_pair(self, project: Path) -> None:
        body = _published_element(project, "swc", "ExampleSWC")
        a, c = _renamed("swc", {**body, "name": "A"}, "C")
        b, d = _renamed("swc", {**body, "name": "B"}, "D")

        renames = _pairs(a, b, c, d)

        assert renames.pairs == ()
        (group,) = renames.ambiguities
        assert (group.layer, group.removals, group.creations) == ("swc", ("A", "B"), ("C", "D"))
        assert renames.unpaired_removals == (("swc", "A"), ("swc", "B"))

    @pytest.mark.parametrize(
        ("old", "new"),
        [(False, 0), (1, 1.0), (0.0, -0.0)],
        ids=["false-and-0", "1-and-1.0", "0.0-and-minus-0.0"],
    )
    def test_a_difference_the_json_spells_does_not_pair(self, project: Path, old: Any, new: Any) -> None:
        interface = _published_element(project, "interfaces", "IExample")
        removal = TouchedElement("interfaces", "IExample", _with_init(interface, old), None)
        creation = TouchedElement("interfaces", "IRenamed", None, {**_with_init(interface, new), "name": "IRenamed"})

        assert _pairs(removal, creation).pairs == ()

    @pytest.mark.parametrize("value", [False, 0, 1, 1.0, 0.0, -0.0], ids=repr)
    def test_the_same_init_on_both_sides_pairs(self, project: Path, value: Any) -> None:
        # The positive control: the init value itself never stops a pairing.
        interface = _with_init(_published_element(project, "interfaces", "IExample"), value)

        assert _pairs(*_renamed("interfaces", interface, "IRenamed")).pairs == (("interfaces", "IExample", "IRenamed"),)

    def test_a_clients_json_spelling_is_published_before_it_is_compared(self, project: Path) -> None:
        # A float field the client wrote as an integer publishes as the float the
        # removed element published: the comparison is over published values.
        before = _published_element(project, "swc", "ExampleSWC")
        before["runnables"][0]["events"][0]["period"] = 1.0
        removal, creation = _renamed("swc", before, "SpeedSensorSWC")
        assert creation.after is not None
        creation.after["runnables"][0]["events"][0]["period"] = 1

        assert _pairs(removal, creation).pairs == (("swc", "ExampleSWC", "SpeedSensorSWC"),)

    def test_an_update_is_never_a_side_of_a_pair(self, project: Path) -> None:
        before = _published_element(project, "swc", "ExampleSWC")
        update = TouchedElement("swc", "ExampleSWC", before, {**before, "description": "changed"})

        renames = _pairs(update)

        assert (renames.pairs, renames.unpaired_removals, renames.unpaired_creations) == ((), (), ())


_REMOVE_EXAMPLE_SWC = {"operation": "remove", "address": {"layer": "swc", "element": "ExampleSWC"}}


def _create_swc_from(project_root: Path, name: str, **changes: Any) -> dict[str, Any]:
    """The creation of ``ExampleSWC``'s published value under *name*, with *changes*."""
    value = {**_published_element(project_root, "swc", "ExampleSWC"), "name": name, **changes}
    return {"operation": "create", "address": {"layer": "swc", "element": name}, "value": value}


def _in_writer_form(project_root: Path, layer: str, identity: str, path: Path) -> None:
    """Re-write *path* as the writer writes the element it holds."""
    staged = StagedModel.from_snapshot(load_snapshot(str(_model_root(project_root))))
    model = staging_mod._instantiate(
        staged.registry, staged.registry.layer(layer), _published_element(project_root, layer, identity)
    )
    path.write_bytes(elements_mod._dump_element(elements_mod.element_to_dict(model)).encode("utf-8"))
    assert elements_mod.stands_in_writer_form(path.read_bytes().decode("utf-8"), model)


class TestMaterializationReadsTheOldTextBeforeTheRemoval:
    """The pairs and the old files' text are taken before any file is removed."""

    def test_a_renamed_elements_old_text_is_read_before_its_file_goes(self, project: Path) -> None:
        old = _swc_file(project)
        old.write_bytes(b"# kept by the rename\n" + old.read_bytes())
        text = old.read_bytes().decode("utf-8")
        request = _change_set(project, _REMOVE_EXAMPLE_SWC, _create_swc_from(project, "SpeedSensorSWC"))

        with stage(_manifest(project)) as staging:
            _apply_into(staging, request)
            shadow_old = Path(staging.model_root) / "swc" / "example" / "ExampleSWC.yaml"

            assert staging.renames == (("swc", "ExampleSWC", "SpeedSensorSWC"),)
            assert not shadow_old.exists()
            assert {key: source.text for key, source in staging._rename_sources.items()} == {
                ("swc", "SpeedSensorSWC"): text
            }
            assert staging._rename_sources["swc", "SpeedSensorSWC"].was == "ExampleSWC"
            # The pair is no cost row: its text is carried.
            assert staging.rewritten == ()

    def test_crlf_line_ends_are_read_as_they_stand(self, project: Path) -> None:
        old = _swc_file(project)
        text = old.read_bytes().decode("utf-8").replace("\n", "\r\n")
        old.write_bytes(text.encode("utf-8"))
        request = _change_set(project, _REMOVE_EXAMPLE_SWC, _create_swc_from(project, "SpeedSensorSWC"))

        with stage(_manifest(project)) as staging:
            _apply_into(staging, request)

            assert staging._rename_sources["swc", "SpeedSensorSWC"].text == text

    def test_a_rename_with_an_edit_names_the_one_creation_beside_it(self, project: Path) -> None:
        # The fixture's component file has blank lines between its blocks, so it
        # is not in the writer's form and a whole write of the new file loses them.
        request = _change_set(
            project, _REMOVE_EXAMPLE_SWC, _create_swc_from(project, "SpeedSensorSWC", description="Reworded")
        )

        with stage(_manifest(project)) as staging:
            _apply_into(staging, request)

            assert staging.renames == ()
            assert staging.rewritten == (
                RewrittenFile(
                    "swc",
                    "ExampleSWC",
                    "not a pure rename to SpeedSensorSWC: removed beside a created element; "
                    "its comments and layout are not carried",
                    removed=True,
                ),
            )

    def test_two_creations_beside_a_removal_claim_no_correspondence(self, project: Path) -> None:
        request = _change_set(
            project,
            _REMOVE_EXAMPLE_SWC,
            _create_swc_from(project, "First", description="One"),
            _create_swc_from(project, "Second", description="Two"),
        )

        with stage(_manifest(project)) as staging:
            _apply_into(staging, request)

            assert staging.rewritten == (
                RewrittenFile(
                    "swc",
                    "ExampleSWC",
                    "removed beside a created element; its comments and layout are not carried",
                    removed=True,
                ),
            )

    def test_a_rename_across_groups_is_named(self, project: Path) -> None:
        request = _change_set(project, _REMOVE_EXAMPLE_SWC, _create_swc_from(project, "SpeedSensorSWC", group="other"))

        with stage(_manifest(project)) as staging:
            _apply_into(staging, request)

            assert [(row.layer, row.identity, row.renamed_from) for row in staging.rewritten] == [
                ("swc", "ExampleSWC", None)
            ]

    def test_a_removed_file_in_the_writers_form_is_no_row(self, project: Path) -> None:
        _in_writer_form(project, "swc", "ExampleSWC", _swc_file(project))
        request = _change_set(
            project, _REMOVE_EXAMPLE_SWC, _create_swc_from(project, "SpeedSensorSWC", description="Reworded")
        )

        with stage(_manifest(project)) as staging:
            _apply_into(staging, request)

            assert staging.rewritten == ()

    def test_a_plain_removal_is_no_row(self, project: Path) -> None:
        old = _swc_file(project)
        old.write_bytes(b"# a comment the removal takes with it\n" + old.read_bytes())
        request = _change_set(project, _REMOVE_EXAMPLE_SWC)

        with stage(_manifest(project)) as staging:
            _apply_into(staging, request)

            assert staging.rewritten == ()
            assert staging.renames == ()

    def test_a_creation_in_another_layer_is_not_beside_the_removal(self, project: Path) -> None:
        request = _change_set(project, _REMOVE_EXAMPLE_SWC, _create_alias("Speed_kph"))

        with stage(_manifest(project)) as staging:
            _apply_into(staging, request)

            assert staging.rewritten == ()


# The carried text

_REMOVE_REQUIREMENT = {"operation": "remove", "address": {"layer": "requirements", "element": "REQ_EXAMPLE_001"}}


def _requirement_file(project_root: Path) -> Path:
    return _model_root(project_root) / "requirements" / "REQ_EXAMPLE_001.yaml"


def _create_requirement_from(project_root: Path, identity: str) -> dict[str, Any]:
    """The creation of ``REQ_EXAMPLE_001``'s published value under *identity*."""
    value = {**_published_element(project_root, "requirements", "REQ_EXAMPLE_001"), "id": identity}
    return {"operation": "create", "address": {"layer": "requirements", "element": identity}, "value": value}


def _differing_lines(old: str, new: str) -> list[tuple[str, str]]:
    """The line pairs at which *old* and *new*, of one line count, differ."""
    old_lines, new_lines = old.splitlines(keepends=True), new.splitlines(keepends=True)
    assert len(old_lines) == len(new_lines), (old, new)
    return [(a, b) for a, b in zip(old_lines, new_lines, strict=True) if a != b]


def _whole_write(project_root: Path, layer: str, value: dict[str, Any]) -> bytes:
    """The writer's whole write of *value*, an element of *layer*."""
    registry = StagedModel.from_snapshot(load_snapshot(str(_model_root(project_root)))).registry
    model = staging_mod._instantiate(registry, registry.layer(layer), value)
    return elements_mod._dump_element(elements_mod.element_to_dict(model)).encode("utf-8")


class TestARenameCarriesItsText:
    """A paired rename's new file is the old one with its identity line changed."""

    def test_a_renamed_component_keeps_its_comments_and_layout(self, project: Path) -> None:
        old = _swc_file(project)
        old.write_bytes(b"# The example component.\n" + old.read_bytes().replace(b"\nports:", b"  # kept\nports:", 1))
        text = old.read_bytes().decode("utf-8")
        request = _change_set(project, _REMOVE_EXAMPLE_SWC, _create_swc_from(project, "SpeedSensorSWC"))

        with stage(_manifest(project)) as staging:
            _apply_into(staging, request)
            new = Path(staging.model_root) / "swc" / "example" / "SpeedSensorSWC.yaml"

            assert _differing_lines(text, new.read_bytes().decode("utf-8")) == [
                ("name: ExampleSWC\n", "name: SpeedSensorSWC\n")
            ]
            assert staging.rewritten == ()

    def test_a_renamed_requirement_keeps_everything_but_its_id(self, project: Path) -> None:
        old = _requirement_file(project)
        old.write_bytes(old.read_bytes().replace(b"\ntitle:", b"  # the stable id\n# a note\ntitle:", 1))
        text = old.read_bytes().decode("utf-8")
        request = _change_set(project, _REMOVE_REQUIREMENT, _create_requirement_from(project, "REQ_EXAMPLE_002"))

        with stage(_manifest(project)) as staging:
            _apply_into(staging, request)
            new = Path(staging.model_root) / "requirements" / "REQ_EXAMPLE_002.yaml"

            assert _differing_lines(text, new.read_bytes().decode("utf-8")) == [
                ("id: REQ_EXAMPLE_001  # the stable id\n", "id: REQ_EXAMPLE_002  # the stable id\n")
            ]
            assert staging.rewritten == ()

    def test_crlf_line_ends_are_carried(self, project: Path) -> None:
        old = _swc_file(project)
        old.write_bytes(old.read_bytes().replace(b"\n", b"\r\n"))
        text = old.read_bytes().decode("utf-8")
        request = _change_set(project, _REMOVE_EXAMPLE_SWC, _create_swc_from(project, "SpeedSensorSWC"))

        with stage(_manifest(project)) as staging:
            _apply_into(staging, request)
            new = Path(staging.model_root) / "swc" / "example" / "SpeedSensorSWC.yaml"

            assert new.read_bytes().decode("utf-8") == text.replace("name: ExampleSWC\r\n", "name: SpeedSensorSWC\r\n")

    def test_a_file_in_the_writers_form_gives_the_whole_write_byte_for_byte(self, project: Path) -> None:
        _in_writer_form(project, "swc", "ExampleSWC", _swc_file(project))
        creation = _create_swc_from(project, "SpeedSensorSWC")
        request = _change_set(project, _REMOVE_EXAMPLE_SWC, creation)

        with stage(_manifest(project)) as staging:
            _apply_into(staging, request)
            new = Path(staging.model_root) / "swc" / "example" / "SpeedSensorSWC.yaml"

            assert new.read_bytes() == _whole_write(project, "swc", creation["value"])
            assert staging.rewritten == ()

    def test_the_suffixed_file_is_used_where_the_plain_name_is_taken(self, project: Path) -> None:
        types = _model_root(project) / "data_types" / "implementation"
        (types / "Speed_kph.yaml").write_text(_VEHICLE_SPEED_KPH, encoding="utf-8")
        carried = "# the old alias\nname: Kph\nkind: value_type\nbase: float32_base  # metric\nunit: kph\n"
        (types / "Kph.yaml").write_bytes(carried.encode("utf-8"))
        request = _change_set(
            project,
            {"operation": "remove", "address": {"layer": "implementation_types", "element": "Kph"}},
            _create_alias("Speed_kph"),
        )

        with stage(_manifest(project)) as staging:
            shadow = Path(staging.model_root)
            occupant = (shadow / "data_types" / "implementation" / "Speed_kph.yaml").read_bytes()
            written = _apply_into(staging, request)

            assert staging.renames == (("implementation_types", "Kph", "Speed_kph"),)
            assert [Path(path).relative_to(shadow).as_posix() for path in written] == [
                "data_types/implementation/Kph.yaml",
                "data_types/implementation/Speed_kph~1.yaml",
            ]
            suffixed = shadow / "data_types" / "implementation" / "Speed_kph~1.yaml"
            assert suffixed.read_bytes().decode("utf-8") == carried.replace("name: Kph\n", "name: Speed_kph\n")
            assert (shadow / "data_types" / "implementation" / "Speed_kph.yaml").read_bytes() == occupant


class TestARenameThatCannotBeCarriedIsWrittenWhole:
    """Where ``edited_text`` has no text, the whole write, its row naming the old identity."""

    @pytest.mark.parametrize(
        ("old", "new", "reason"),
        [
            (
                b"id: REQ_EXAMPLE_001",
                b"id: &i REQ_EXAMPLE_001",
                "the file writes a value through an alias, an anchor or a tag",
            ),
            (b"\n", b"\r\n", "the file has mixed line ends"),
        ],
        ids=["an-anchor", "mixed-line-ends"],
    )
    def test_an_old_file_in_a_refused_form_is_written_whole_with_its_row(
        self, project: Path, old: bytes, new: bytes, reason: str
    ) -> None:
        stored = _requirement_file(project)
        stored.write_bytes(stored.read_bytes().replace(old, new, 1))
        creation = _create_requirement_from(project, "REQ_EXAMPLE_002")
        request = _change_set(project, _REMOVE_REQUIREMENT, creation)

        with stage(_manifest(project)) as staging:
            _apply_into(staging, request)
            written = Path(staging.model_root) / "requirements" / "REQ_EXAMPLE_002.yaml"

            assert staging.renames == (("requirements", "REQ_EXAMPLE_001", "REQ_EXAMPLE_002"),)
            assert written.read_bytes() == _whole_write(project, "requirements", creation["value"])
            assert staging.rewritten == (
                RewrittenFile("requirements", "REQ_EXAMPLE_002", reason, renamed_from="REQ_EXAMPLE_001"),
            )

    def test_an_old_file_in_the_writers_form_gives_no_row(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Forced past the line edit: a writer-form file the edit refuses loses nothing written whole.
        def refuse(*_args: Any) -> str:
            raise FormRefused("the change touches too many places in the file")

        monkeypatch.setattr(elements_mod, "edit_element_text", refuse)
        _in_writer_form(project, "swc", "ExampleSWC", _swc_file(project))
        creation = _create_swc_from(project, "SpeedSensorSWC")
        request = _change_set(project, _REMOVE_EXAMPLE_SWC, creation)

        with stage(_manifest(project)) as staging:
            _apply_into(staging, request)
            new = Path(staging.model_root) / "swc" / "example" / "SpeedSensorSWC.yaml"

            assert new.read_bytes() == _whole_write(project, "swc", creation["value"])
            assert staging.rewritten == ()

    def test_an_old_file_that_cannot_be_read_as_text_is_written_whole_with_its_row(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(staging_mod, "_stored_text", lambda _path: None)
        creation = _create_swc_from(project, "SpeedSensorSWC")
        request = _change_set(project, _REMOVE_EXAMPLE_SWC, creation)

        with stage(_manifest(project)) as staging:
            _apply_into(staging, request)
            new = Path(staging.model_root) / "swc" / "example" / "SpeedSensorSWC.yaml"

            assert new.read_bytes() == _whole_write(project, "swc", creation["value"])
            assert staging.rewritten == (
                RewrittenFile(
                    "swc",
                    "SpeedSensorSWC",
                    "the renamed element's old file could not be read as text",
                    renamed_from="ExampleSWC",
                ),
            )

    def test_a_plain_create_is_never_asked_for_an_edit(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        asked: list[Any] = []
        monkeypatch.setattr(staging_mod, "edited_text", lambda *args, **kwargs: asked.append((args, kwargs)))

        with stage(_manifest(project)) as staging:
            _apply_into(staging, _change_set(project, _CREATE_SPEED_KPH, baseline=staging.baseline))

        assert asked == []


def _example_vectors_with_an_element_rename_shape() -> list[str]:
    """Every example change set that removes and creates a whole element in one layer."""
    names = []
    for name, vector in sorted(fixtures.VECTORS.items()):
        request = vector.get("input")
        if vector["kind"] != "example" or not isinstance(request, dict) or request.get("operation") != "change_set":
            continue
        whole = [
            (operation["operation"], operation["address"]["layer"])
            for operation in request["operations"]
            if not operation["address"].get("path")
        ]
        removed = {layer for verb, layer in whole if verb == "remove"}
        if any(verb == "create" and layer in removed for verb, layer in whole):
            names.append(name)
    return names


class TestThePairingAgreesWithTheGuardOnTheVectors:
    """On every rename-shaped example vector, staging pairs what the safety guard pairs."""

    def test_the_rename_vector_is_among_them(self) -> None:
        assert "rename-swc-via-remove-create-and-reference-updates" in _example_vectors_with_an_element_rename_shape()

    @pytest.mark.parametrize("vector_name", _example_vectors_with_an_element_rename_shape())
    def test_staging_pairs_what_the_guard_pairs(
        self, vector_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vector = fixtures.VECTORS[vector_name]
        project = fixtures.build_fixture(tmp_path / "project", vector_name)
        request = fixtures.prepare_request(vector, fixtures.current_baseline_token(project))
        staged, guarded = spy_on_the_two_pairings(monkeypatch)

        fixtures.dispatch(
            request,
            project=project,
            approved_scope=fixtures.approved_scope(vector),
            review=fixtures.host_review(vector),
        )

        assert len(staged) == 1, "the change set never reached materialization"
        assert guarded, "the change set never reached the safety guard"
        assert all(decided == staged[0] for decided in guarded), (staged, guarded)
        if vector_name == "rename-swc-via-remove-create-and-reference-updates":
            assert staged[0] == {("swc", "ExampleSWC"): "SpeedSensorSWC"}


# The warning's rename and dropped-key lines, and a stray key meeting the rename

_PLANTED = b"zz_undeclared: kept\n"


def _interface_file(project_root: Path) -> Path:
    return _model_root(project_root) / "interfaces" / "IDiagnosticService.yaml"


def _rename_the_interface(project_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """``IDiagnosticService``, which nothing references, removed and created as ``IDiagnostics``, nothing else moved."""
    value = _published_element(project_root, "interfaces", "IDiagnosticService")
    return (
        {"operation": "remove", "address": {"layer": "interfaces", "element": "IDiagnosticService"}},
        {
            "operation": "create",
            "address": {"layer": "interfaces", "element": "IDiagnostics"},
            "value": {**value, "name": "IDiagnostics"},
        },
    )


def _staged_review(project_root: Path, request: ChangeSetRequest) -> tuple[tuple[RewrittenFile, ...], list[str]]:
    """*request* materialized in a staging, its rows, and the first question's lines rendered from them."""
    with stage(_manifest(project_root)) as staging:
        _apply_into(staging, request)
        rows = staging.rewritten
    proposal = ChangeSetProposal(change_set_id="cs-1", baseline="rev1:token", intent="i", delta=(), rewritten=rows)
    return rows, render_delta_review(proposal).splitlines()


def _warning_rows(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("  - ")]


class TestTheWarningNamesTheRenameAndTheDroppedKeys:
    """The review's lines for a rename's fallback, a rename with an edit and a dropped key."""

    def test_a_rename_fallback_names_the_old_identity_and_the_question_counts_it(self, project: Path) -> None:
        _mixed_line_ends(_interface_file(project))
        seen: list[Any] = []

        response = TestAnUpdateChangesOnlyItsLines._committed(project, *_rename_the_interface(project), seen=seen)

        assert response["status"] == "committed", response
        lines = render_delta_review(seen[0]).splitlines()
        assert _warning_rows(lines) == [
            f"  - interfaces/IDiagnostics (renamed from IDiagnosticService): {_FALLBACK_REASON}"
        ]
        assert lines[-1].endswith(" Commit exactly this delta and rewrite 1 file(s) whole?")

    def test_a_carried_rename_adds_nothing_to_the_review(self, project: Path) -> None:
        seen: list[Any] = []

        response = TestAnUpdateChangesOnlyItsLines._committed(project, *_rename_the_interface(project), seen=seen)

        assert response["status"] == "committed", response
        assert seen[0].rewritten == ()
        assert "WARNING" not in render_delta_review(seen[0])

    def test_a_rename_with_an_edit_unique_in_its_layer_is_not_a_pure_rename(self, project: Path) -> None:
        request = _change_set(
            project, _REMOVE_EXAMPLE_SWC, _create_swc_from(project, "SpeedSensorSWC", description="Reworded")
        )

        _rows, lines = _staged_review(project, request)

        assert _warning_rows(lines) == [
            "  - swc/ExampleSWC: not a pure rename to SpeedSensorSWC: removed beside a created element; "
            "its comments and layout are not carried"
        ]

    def test_a_removed_row_has_its_own_lead_and_is_not_counted_as_rewritten(self, project: Path) -> None:
        # The file is deleted, not rewritten: the rewrite sentence would state something false of it,
        # and so would a question counting it among the files rewritten whole.
        request = _change_set(
            project, _REMOVE_EXAMPLE_SWC, _create_swc_from(project, "SpeedSensorSWC", description="Reworded")
        )

        _rows, lines = _staged_review(project, request)

        start = lines.index("WARNING: more than the delta will change in 1 file(s).")
        assert lines[start + 1].startswith("The element(s) below are removed beside a created element")
        assert not any("rewritten whole" in line for line in lines)
        assert lines[-1].endswith(" Commit exactly this delta?")

    def test_twins_renamed_together_get_one_line_per_removed_file_and_no_correspondence(self, project: Path) -> None:
        # An ambiguity pairs neither, so each removed file gets its own line naming no creation.
        twin = _published_element(project, "swc", "ExampleSWC")
        twin_file = _swc_file(project).with_name("ExampleTwinSWC.yaml")
        twin_file.write_bytes(_swc_file(project).read_bytes().replace(b"name: ExampleSWC", b"name: ExampleTwinSWC", 1))
        assert _published_element(project, "swc", "ExampleTwinSWC") == {**twin, "name": "ExampleTwinSWC"}
        request = _change_set(
            project,
            _REMOVE_EXAMPLE_SWC,
            {"operation": "remove", "address": {"layer": "swc", "element": "ExampleTwinSWC"}},
            _create_swc_from(project, "First"),
            _create_swc_from(project, "Second"),
        )

        rows, lines = _staged_review(project, request)

        assert [row.identity for row in rows] == ["ExampleSWC", "ExampleTwinSWC"]
        assert _warning_rows(lines) == [
            f"  - swc/{name}: removed beside a created element; its comments and layout are not carried"
            for name in ("ExampleSWC", "ExampleTwinSWC")
        ]
        assert "rename to" not in "\n".join(lines)

    def test_a_rename_across_groups_shows_its_line(self, project: Path) -> None:
        request = _change_set(project, _REMOVE_EXAMPLE_SWC, _create_swc_from(project, "SpeedSensorSWC", group="other"))

        _rows, lines = _staged_review(project, request)

        assert _warning_rows(lines) == [
            "  - swc/ExampleSWC: not a pure rename to SpeedSensorSWC: removed beside a created element; "
            "its comments and layout are not carried"
        ]

    def test_a_plain_removal_shows_nothing(self, project: Path) -> None:
        old = _swc_file(project)
        old.write_bytes(b"# a comment the removal takes with it\n" + old.read_bytes() + _PLANTED)

        rows, lines = _staged_review(project, _change_set(project, _REMOVE_EXAMPLE_SWC))

        assert rows == ()
        assert not any(line.startswith("WARNING") for line in lines)

    def test_a_stray_key_in_an_unpaired_removed_file_is_listed_in_its_line(self, project: Path) -> None:
        _swc_file(project).write_bytes(_swc_file(project).read_bytes() + _PLANTED)
        request = _change_set(
            project, _REMOVE_EXAMPLE_SWC, _create_swc_from(project, "SpeedSensorSWC", description="Reworded")
        )

        rows, lines = _staged_review(project, request)

        assert [row.dropped_keys for row in rows] == [("zz_undeclared",)]
        assert _warning_rows(lines) == [
            "  - swc/ExampleSWC: not a pure rename to SpeedSensorSWC: removed beside a created element; "
            "its comments and layout are not carried; drops the undeclared key(s) 'zz_undeclared'"
        ]

    def test_an_update_fallback_on_a_file_holding_a_stray_key_lists_it(self, project: Path) -> None:
        # The whole write drops the key, so pass D sees none and the change set commits: the review is
        # the one place the loss is named before the approval.
        task = _task_file(project)
        task.write_bytes(task.read_bytes() + _PLANTED)
        _mixed_line_ends(task)
        seen: list[Any] = []

        response = TestAnUpdateChangesOnlyItsLines._committed(project, _UPDATE_PRIORITY, seen=seen)

        assert response["status"] == "committed", response
        assert seen[0].rewritten == (
            RewrittenFile("tasks", "Task_10ms", _FALLBACK_REASON, dropped_keys=("zz_undeclared",)),
        )
        assert _warning_rows(render_delta_review(seen[0]).splitlines()) == [
            f"  - tasks/Task_10ms: {_FALLBACK_REASON}; drops the undeclared key(s) 'zz_undeclared'"
        ]
        assert b"zz_undeclared" not in task.read_bytes()

    def test_a_rename_fallback_lists_the_key_it_drops(self, project: Path) -> None:
        _interface_file(project).write_bytes(_interface_file(project).read_bytes() + _PLANTED)
        _mixed_line_ends(_interface_file(project))
        seen: list[Any] = []

        response = TestAnUpdateChangesOnlyItsLines._committed(project, *_rename_the_interface(project), seen=seen)

        assert response["status"] == "committed", response
        assert _warning_rows(render_delta_review(seen[0]).splitlines()) == [
            "  - interfaces/IDiagnostics (renamed from IDiagnosticService): "
            f"{_FALLBACK_REASON}; drops the undeclared key(s) 'zz_undeclared'"
        ]

    def test_no_dropped_keys_when_the_file_holds_none(self, project: Path) -> None:
        _mixed_line_ends(_task_file(project))
        seen: list[Any] = []

        TestAnUpdateChangesOnlyItsLines._committed(project, _UPDATE_PRIORITY, seen=seen)

        assert [row.dropped_keys for row in seen[0].rewritten] == [()]
        assert "drops the undeclared" not in render_delta_review(seen[0])


class TestAStrayKeyMeetsTheRename:
    """A rename carries a stray key as written, so pass D refuses it until the key is removed by hand."""

    def test_a_carried_rename_keeps_the_key_and_pass_d_refuses_it_on_the_new_element(self, project: Path) -> None:
        _interface_file(project).write_bytes(_interface_file(project).read_bytes() + _PLANTED)
        before = _tree_state(_model_root(project))

        response = TestAnUpdateChangesOnlyItsLines._committed(project, *_rename_the_interface(project))

        assert response["error"] == "consistency-violation", response
        assert response["addresses"] == [
            {"layer": "interfaces", "element": "IDiagnostics", "path": [{"key": "zz_undeclared"}]}
        ]
        assert "'zz_undeclared'" in response["message"]
        assert "additional" not in response
        assert _tree_state(_model_root(project)) == before

    def test_the_same_rename_after_the_key_is_removed_by_hand_commits_by_lines(self, project: Path) -> None:
        stored = _interface_file(project)
        clean = stored.read_bytes()
        stored.write_bytes(clean + _PLANTED)
        assert TestAnUpdateChangesOnlyItsLines._committed(project, *_rename_the_interface(project))["response"] == (
            "error"
        )
        stored.write_bytes(b"# kept by the rename\n" + clean)

        response = TestAnUpdateChangesOnlyItsLines._committed(project, *_rename_the_interface(project))

        assert response["status"] == "committed", response
        renamed = (_model_root(project) / "interfaces" / "IDiagnostics.yaml").read_bytes().decode("utf-8")
        old = (b"# kept by the rename\n" + clean).decode("utf-8")
        assert _differing_lines(old, renamed) == [("name: IDiagnosticService\n", "name: IDiagnostics\n")]
        entries = AuditLog(str(project / "dist" / AUDIT_FILENAME)).get_entries()
        (written,) = [entry for entry in entries if entry["operation"] == "write_element"]
        assert written["args"]["form"] == "lines"


class TestAStrayKeyRefusesEveryChangeSet:
    """Pass D waives nothing, so a stray key in a file no operation touches refuses the change set."""

    def test_an_untouched_files_stray_key_refuses_a_valid_change_set_and_every_key_is_named(
        self, project: Path
    ) -> None:
        _interface_file(project).write_bytes(_interface_file(project).read_bytes() + _PLANTED)
        _swc_file(project).write_bytes(_swc_file(project).read_bytes() + b"zz_second: kept\n")
        before = _tree_state(_model_root(project))

        response = TestAnUpdateChangesOnlyItsLines._committed(project, _UPDATE_PRIORITY)

        assert response["error"] == "consistency-violation", response
        named = [response["addresses"], *(extra["addresses"] for extra in response["additional"])]
        assert {"layer": "interfaces", "element": "IDiagnosticService", "path": [{"key": "zz_undeclared"}]} in [
            address for addresses in named for address in addresses
        ]
        assert {"layer": "swc", "element": "ExampleSWC", "path": [{"key": "zz_second"}]} in [
            address for addresses in named for address in addresses
        ]
        assert _tree_state(_model_root(project)) == before

    def test_a_real_client_error_stays_the_primary_and_the_key_is_additional(self, project: Path) -> None:
        _interface_file(project).write_bytes(_interface_file(project).read_bytes() + _PLANTED)
        unknown_partition = {
            "operation": "update",
            "address": {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "partition"}]},
            "value": "NO_SUCH_PARTITION",
        }

        response = TestAnUpdateChangesOnlyItsLines._committed(project, unknown_partition)

        assert response["error"] == "broken-reference", response
        assert response["addresses"][0] == {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "partition"}]}
        assert [(extra["error"], extra["addresses"]) for extra in response["additional"]] == [
            (
                "consistency-violation",
                [{"layer": "interfaces", "element": "IDiagnosticService", "path": [{"key": "zz_undeclared"}]}],
            )
        ]
