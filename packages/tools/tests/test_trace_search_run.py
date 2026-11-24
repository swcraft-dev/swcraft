"""The trace link search's run: what stops it, what it reports, and its three files."""

from __future__ import annotations

import json
import re
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from swcraft_tools import experiment_frame, trace_records, trace_search
from swcraft_tools.project import require_project
from swcraft_tools.trace import parse_link
from swcraft_tools.trace_search import (
    STOPPING_RULES,
    AmbiguousIdentityError,
    TraceExperimentError,
    proposals_document,
    refuse_ambiguous_identities,
    render_markdown,
    run_trace,
)
from swcraft_tools.validator import CONSISTENCY_RULES, ConsistencyIssue, ConsistencyResult
from trace_models import TraceModel, port, runnable, write_project

#: The proposals file's schema, package data beside its reader.
SCHEMA = resource_files("swcraft_tools") / trace_records.SCHEMA_RESOURCE
_LINK = re.compile(r"\[([^\]\[]+)\]\(([^)\s]+)\)")


def _model() -> TraceModel:
    """A model the gate does not pass: a dangling link, a stale one and a safety error, besides real evidence."""
    model = TraceModel()
    model.sr_interface("IBrakeTemp", [("temperature", "the brake temperature")])
    model.component(
        "BrakeUnit",
        description="brake temperature supervision",
        asil="A",
        ports=[port("pBrakeTemp", interface="IBrakeTemp")],
        runnables=[runnable("Supervise", description="supervise the brake temperature")],
        satisfies=["SR-1@1", "SR-404@1"],
    )
    model.component("DoorUnit", description="door lock control", satisfies=["SR-3@1"])
    model.requirement("SR-1", "Brake temperature", "The brake temperature shall be supervised", document="Spec")
    model.get("requirements/SR-1.yaml")["asil"] = "B"
    model.requirement("SR-2", "Brake temperature report", "Report the brake temperature", document="Spec")
    model.requirement("SR-3", "Door lock", "Doors shall lock", version=2, document="Spec")
    model.requirement("SR-4", "Door unlock", "Doors shall unlock on request")
    return model


def _run(tmp_path: Path, model: TraceModel | None = None, **keys: Any):
    manifest, config = write_project(model or _model(), tmp_path / "project", **keys)
    return run_trace(require_project(str(manifest)), config)


def _files(run) -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8") for path in (run.result_path, run.proposals_path, run.report_path)
    }


class TestWhatStopsTheSearch:
    def test_the_stopping_rules_are_exactly_the_nine_identities(self) -> None:
        assert STOPPING_RULES <= set(CONSISTENCY_RULES)
        assert STOPPING_RULES == {
            "name.duplicate-swc",
            "name.duplicate-port",
            "name.duplicate-runnable",
            "name.duplicate-interface",
            "name.duplicate-component-type",
            "name.duplicate-requirement",
            "composition.duplicate-name",
            "task.duplicate-name",
            "hardware.duplicate-name",
        }

    @pytest.mark.parametrize("rule", sorted(STOPPING_RULES))
    def test_each_stopping_rule_refuses(self, rule: str) -> None:
        result = ConsistencyResult(consistent=False, records=(ConsistencyIssue(rule=rule, message="x"),))
        with pytest.raises(AmbiguousIdentityError) as caught:
            refuse_ambiguous_identities(result)
        assert caught.value.rules == (rule,) and caught.value.count == 1

    @pytest.mark.parametrize("duplicate", ["swc", "port", "interface"])
    def test_a_duplicate_identity_stops_the_run_with_nothing_written(self, tmp_path: Path, duplicate: str) -> None:
        model = _model()
        if duplicate == "swc":
            model.component("DoorUnit", group="other")
        elif duplicate == "port":
            model.get("swc/app/DoorUnit.yaml")["ports"] = [port("pDoor"), port("pDoor")]
        else:
            model.put("interfaces/IBrakeTemp~2.yaml", dict(model.get("interfaces/IBrakeTemp.yaml")))
        with pytest.raises(AmbiguousIdentityError, match="ambiguous identit"):
            _run(tmp_path, model)
        assert not (tmp_path / "project" / "dist").exists()

    def test_an_unreadable_project_raises_the_readers_exception(self, tmp_path: Path) -> None:
        model = _model()
        model.put("swc/app/Broken.yaml", {"name": "Broken", "ports": "not a list"})
        with pytest.raises(Exception) as caught:
            _run(tmp_path, model)
        assert not isinstance(caught.value, trace_search.TraceError)
        assert not (tmp_path / "project" / "dist").exists()

    def test_a_configuration_refusal_writes_nothing(self, tmp_path: Path) -> None:
        with pytest.raises(TraceExperimentError, match="'constraints.documents'"):
            _run(tmp_path, documents=["No such document"])
        assert not (tmp_path / "project" / "dist").exists()


class TestWhatIsReported:
    def test_a_model_the_gate_refuses_still_runs_and_reports_each_finding(self, tmp_path: Path) -> None:
        run = _run(tmp_path)
        findings = run.document["findings"]
        rules = {count["rule"]: count for count in findings["counts"]}
        assert rules["trace.link-unknown-requirement"]["class"] == "error"
        assert "trace.link-needs-confirmation" in rules
        assert "safety.component-below-requirements" in rules
        messages = [finding["message"] for finding in findings["trace"]]
        assert any("SR-404" in message for message in messages)
        report = run.report_path.read_text(encoding="utf-8")
        assert "`trace.link-unknown-requirement` (error)" in report


class TestTheThreeFiles:
    def test_the_configuration_is_recorded_in_all_three(self, tmp_path: Path) -> None:
        run = _run(tmp_path, candidates=3)
        files = _files(run)
        result, proposals = json.loads(files["result.json"]), json.loads(files["proposals.json"])
        record = result["fingerprint"]["configuration"]
        assert record["file"] == "experiments/trace.yaml" and record["insideProject"] is True
        assert proposals["configuration"] == record
        assert f"- Configuration: `experiments/trace.yaml`, {record['sha256']}" in files["report.md"]
        assert result["fingerprint"]["parameters"]["search"]["candidates"] == 3

    def test_a_run_on_the_defaults_records_no_configuration(self, tmp_path: Path) -> None:
        run = _run(tmp_path)
        assert run.document["fingerprint"]["configuration"] is None
        assert json.loads(run.proposals_path.read_text(encoding="utf-8"))["configuration"] is None

    def test_proposals_is_valid_and_the_projection_of_the_result(self, tmp_path: Path) -> None:
        run = _run(tmp_path)
        proposals = json.loads(run.proposals_path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8"))).validate(proposals)
        assert proposals == proposals_document(json.loads(run.result_path.read_text(encoding="utf-8")))
        assert proposals["requirements"], "no entry, so the schema proves nothing"
        assert all(entry["chosen"] == [] for entry in proposals["requirements"])
        assert all(c["decision"] is None for entry in proposals["requirements"] for c in entry["candidates"])

    @pytest.mark.parametrize("keys", [{"labelling": ["Spec"]}, {"labelling": "all", "labelling_share": 1.0}])
    def test_a_labelling_run_is_valid_too(self, tmp_path: Path, keys: dict[str, Any]) -> None:
        proposals = json.loads(_run(tmp_path, **keys).proposals_path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8"))).validate(proposals)
        assert any(entry["pass"] == "labelling" and entry["chosenBecause"] for entry in proposals["requirements"])

    def test_the_schema_is_closed(self) -> None:
        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert node.get("additionalProperties") is False, node
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(json.loads(SCHEMA.read_text(encoding="utf-8")))

    def test_no_requirement_target_pair_twice(self, tmp_path: Path) -> None:
        proposals = json.loads(_run(tmp_path).proposals_path.read_text(encoding="utf-8"))
        pairs = [
            (entry["id"], json.dumps(candidate["target"], sort_keys=True))
            for entry in proposals["requirements"]
            for candidate in entry["candidates"]
        ]
        assert len(pairs) == len(set(pairs))

    def test_the_report_with_its_links_reduced_is_the_documents_rendering(self, tmp_path: Path) -> None:
        run = _run(tmp_path)
        report = run.report_path.read_text(encoding="utf-8")
        assert _LINK.search(report), "report.md links nothing, so the reduction proves nothing"
        assert _LINK.sub(r"\1", report) == render_markdown(run.document)
        for heading in trace_search.REPORT_SECTIONS:
            assert f"\n## {heading}\n" in report

    def test_a_failed_write_leaves_the_previous_three_files(self, tmp_path: Path, monkeypatch) -> None:
        run = _run(tmp_path)
        directory = run.report_path.parent
        previous = {path.name: path.read_bytes() for path in directory.iterdir()}
        real_write_text = Path.write_text

        def full_disk(self: Path, *args: Any, **kwargs: Any) -> int:
            if self.name.startswith("report.md.") and self.name.endswith(".partial"):
                raise OSError(28, "No space left on device")
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", full_disk)
        manifest = tmp_path / "project" / "swcraft.yaml"
        (tmp_path / "project" / "experiments").mkdir(exist_ok=True)
        config = tmp_path / "project" / "experiments" / "trace.yaml"
        config.write_bytes(b"search:\n  candidates: 1\n")
        with pytest.raises(OSError, match="No space left"):
            run_trace(require_project(str(manifest)), config)
        assert {path.name: path.read_bytes() for path in directory.iterdir()} == previous

    def test_the_three_renames_run_in_order(self, tmp_path: Path, monkeypatch) -> None:
        renamed: list[str] = []
        real_replace = experiment_frame.os.replace

        def recording(source: Any, target: Any) -> None:
            renamed.append(Path(target).name)
            real_replace(source, target)

        monkeypatch.setattr(experiment_frame.os, "replace", recording)
        _run(tmp_path)
        assert renamed == ["result.json", "proposals.json", "report.md"]

    def test_the_model_is_byte_identical_after_a_run(self, tmp_path: Path) -> None:
        manifest, _config = write_project(_model(), tmp_path / "project")
        model_root = tmp_path / "project" / "model"
        before = {
            path.as_posix(): path.read_bytes()
            for path in sorted(model_root.rglob("*"), key=lambda path: path.as_posix())
            if path.is_file()
        }
        run_trace(require_project(str(manifest)), None)
        after = {
            path.as_posix(): path.read_bytes()
            for path in sorted(model_root.rglob("*"), key=lambda path: path.as_posix())
            if path.is_file()
        }
        assert after == before

    def test_no_requirement_in_scope_is_a_run(self, tmp_path: Path) -> None:
        run = _run(tmp_path, without_document=False, requirements=["SR-4"])
        assert run.summary == "trace: no requirement in scope"
        assert run.document["requirements"] == []
        assert all(path.is_file() for path in (run.result_path, run.proposals_path, run.report_path))

    def test_the_entries_name_no_model_path(self, tmp_path: Path) -> None:
        files = _files(_run(tmp_path))
        for name in ("result.json", "proposals.json"):
            assert "model/" not in files[name] and _LINK.search(files[name]) is None
            assert str(tmp_path) not in files[name]


class TestTheReportsWording:
    def test_labelling_all_counts_the_no_document_group_apart(self, tmp_path: Path) -> None:
        summary = _run(tmp_path, labelling="all", labelling_share=1.0).summary
        assert summary.endswith("chosen in 1 document and among the requirements without a document")

    def test_only_outside_pairs_is_said_so(self, tmp_path: Path) -> None:
        model = TraceModel()
        model.requirement("R1", "query", document="Spec")
        model.component("Alpha", satisfies=["R1@1"])
        model.component("Beta")
        report = _run(tmp_path, model, exclude=["Alpha"]).report_path.read_text(encoding="utf-8")
        assert "No linked requirement in scope has a link the search could recover." in report
        assert "- 1 link points at targets outside the search's scope (1 requirement with no other link)" in report
        assert "nothing to recover" not in report

    def test_a_document_with_a_line_break_keeps_its_table_row_and_heading(self, tmp_path: Path) -> None:
        model = _model()
        model.requirement("SR-9", "Brake temperature twice", document="Two\nlines")
        report = _run(tmp_path, model).report_path.read_text(encoding="utf-8")
        assert "| Two lines | 1 |" in report and "### Two lines" in report

    def test_one_neighbour_is_singular(self, tmp_path: Path) -> None:
        model = TraceModel()
        model.component("Alpha")
        model.requirement("N-1", "pad wear", document="Spec")
        model.link("swc/app/Alpha.yaml", "N-1@1")
        model.requirement("R1", "pad wear", document="Spec")
        report = _run(tmp_path, model).report_path.read_text(encoding="utf-8")
        assert "1 of the 1 linked requirement of this document link here" in report
        assert "similar to 1 linked requirement (e.g. N-1)" in report


class TestWhatTheFilesCanCarry:
    def test_a_stale_version_of_any_width_is_written_exactly(self, tmp_path: Path) -> None:
        """``trace.parse_link`` reads a version wider than ``int()``'s default limit; the files carry it exactly."""
        wide = "9" * 4301
        model = _model()
        model.component("WideUnit", description="brake temperature", satisfies=[f"SR-2@{wide}"])
        run = _run(tmp_path, model)
        entry = next(entry for entry in run.document["requirements"] if entry["id"] == "SR-2")
        assert [link["version"] for link in entry["linked"]] == [parse_link(f"SR-2@{wide}").version]
        for path in (run.result_path, run.proposals_path):
            assert f'"version": {wide}' in path.read_text(encoding="utf-8")
        assert f"(@{wide}, awaiting confirmation)" in run.report_path.read_text(encoding="utf-8")

    def test_exact_decimal_and_dump_json(self) -> None:
        value = parse_link("x@" + "12345678901234567890" * 300).version
        assert value is not None
        assert trace_search.exact_decimal(value) == "12345678901234567890" * 300
        assert trace_search.exact_decimal(-(10**7000)) == "-1" + "0" * 7000
        assert (
            trace_search.dump_json({"a": [value, True, 3]}) == '{"a": [' + "12345678901234567890" * 300 + ", true, 3]}"
        )

    def test_a_requirement_id_with_link_delimiters_links_whole(self, tmp_path: Path) -> None:
        model = _model()
        model.requirement("SR]9", "Brake temperature again", "The brake temperature", document="Spec")
        model.requirement("SR[8", "Brake temperature still", "The brake temperature", document="Spec")
        report = _run(tmp_path, model).report_path.read_text(encoding="utf-8")
        assert "**[SR\\]9](../../../model/requirements/" in report
        assert "**[SR\\[8](../../../model/requirements/" in report
        assert trace_search._label("a\\b") == "a\\\\b"


# ---------------------------------------------------------------------------
# The decisions honoured
# ---------------------------------------------------------------------------


def _write_decisions(project_dir: Path, *records: tuple[str, int, dict[str, Any], str]) -> Path:
    """``trace-decisions.yaml`` in *project_dir*, through the records module's own text."""
    decisions = trace_records.merge_decisions(
        trace_records.no_decisions(),
        [
            trace_records.DecisionRecord.model_validate(
                {"requirement": requirement, "version": version, "target": target, "decision": decision}
            )
            for requirement, version, target, decision in records
        ],
    )
    path = project_dir / trace_records.DECISIONS_FILE
    path.write_text(trace_records.decisions_text(decisions), encoding="utf-8", newline="\n")
    return path


def _snapshot(project_dir: Path) -> dict[str, bytes]:
    return {
        path.relative_to(project_dir).as_posix(): path.read_bytes()
        for path in sorted(project_dir.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


def _rerun(project_dir: Path, **options: Any):
    return run_trace(require_project(str(project_dir / "swcraft.yaml")), None, **options)


def _decide(run, edit) -> None:
    """Apply *edit* to the run's ``proposals.json`` document and write it back in the search's form."""
    document = json.loads(run.proposals_path.read_text(encoding="utf-8"))
    edit(document)
    run.proposals_path.write_text(trace_search.proposals_json(document), encoding="utf-8", newline="\n")


class TestTheDecisionsAreHonoured:
    def test_a_rejected_pair_is_not_proposed_and_is_counted(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "project"
        first = _run(tmp_path)
        entry = next(entry for entry in first.document["requirements"] if entry["id"] == "SR-2")
        target = entry["candidates"][0]["target"]
        _write_decisions(project_dir, ("SR-2", 1, target, "reject"))
        second = _rerun(project_dir)
        entry = next(entry for entry in second.document["requirements"] if entry["id"] == "SR-2")
        assert target not in [candidate["target"] for candidate in entry["candidates"]]
        assert entry["rejectedEarlier"] == 1
        spec = next(row for row in second.document["documents"] if row["document"] == "Spec")
        assert spec["rejectedEarlier"] == 1
        report = second.report_path.read_text(encoding="utf-8")
        assert (
            "| Document | In scope | Linked | Awaiting confirmation only | Without a link | Rejected earlier |"
            in report
        )
        assert "  - 1 candidate rejected earlier, not proposed\n" in report
        proposals = json.loads(second.proposals_path.read_text(encoding="utf-8"))
        assert all("rejectedEarlier" not in entry for entry in proposals["requirements"])

    def test_a_reject_at_an_earlier_version_reopens_the_pair(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "project"
        first = _run(tmp_path)
        entry = next(entry for entry in first.document["requirements"] if entry["id"] == "SR-2")
        target = entry["candidates"][0]["target"]
        _write_decisions(project_dir, ("SR-2", 2, target, "reject"))
        entry = next(entry for entry in _rerun(project_dir).document["requirements"] if entry["id"] == "SR-2")
        assert entry["candidates"][0]["target"] == target and entry["rejectedEarlier"] == 0

    def test_a_malformed_decisions_file_refuses_the_search_naming_its_place(self, tmp_path: Path) -> None:
        manifest, _config = write_project(_model(), tmp_path / "project")
        (tmp_path / "project" / trace_records.DECISIONS_FILE).write_text(
            "formatVersion: 1\ndecisions:\n- requirement: SR-2\n  version: 1\n  decision: maybe\n",
            encoding="utf-8",
            newline="\n",
        )
        with pytest.raises(trace_records.TraceRecordsError) as caught:
            run_trace(require_project(str(manifest)), None)
        assert caught.value.file == trace_records.DECISIONS_FILE
        assert caught.value.pointer.startswith("/decisions/0")
        assert not (tmp_path / "project" / "dist").exists()

    def test_the_linked_entries_carry_the_member_and_the_decision_slot(self, tmp_path: Path) -> None:
        """A bare, a stale and a malformed member each await confirmation; a confirmed one has no slot."""
        model = TraceModel()
        model.requirement("SR-1", "Brake temperature", "The brake temperature", version=2, document="Spec")
        model.component("Alpha", description="brake", satisfies=["SR-1", "SR-1@1", "SR-1@x"])
        model.component("Beta", description="brake", satisfies=["SR-1@2"])
        run = _run(tmp_path, model)
        proposals = json.loads(run.proposals_path.read_text(encoding="utf-8"))
        entry = next(entry for entry in proposals["requirements"] if entry["id"] == "SR-1")
        alpha = {"kind": "swc", "owner": None, "name": "Alpha"}
        beta = {"kind": "swc", "owner": None, "name": "Beta"}
        assert entry["linked"] == [
            {"target": alpha, "member": "SR-1@1", "version": 1, "confirmed": False, "decision": None},
            {"target": alpha, "member": "SR-1", "version": None, "confirmed": False, "decision": None},
            {"target": alpha, "member": "SR-1@x", "version": None, "confirmed": False, "decision": None},
            {"target": beta, "member": "SR-1@2", "version": 2, "confirmed": True},
        ]
        assert trace_records.read_proposals(run.proposals_path).model.requirements


class TestThePendingGuard:
    def test_a_fresh_file_holds_no_pending_work_and_a_second_search_rewrites_it(self, tmp_path: Path) -> None:
        first = _run(tmp_path)
        assert trace_records.pending(trace_records.read_proposals(first.proposals_path).model) == 0
        second = _rerun(tmp_path / "project")
        assert second.proposals_path.read_bytes() == first.proposals_path.read_bytes()

    @pytest.mark.parametrize(("decided", "noun"), [(1, "decision"), (2, "decisions")])
    def test_pending_decisions_refuse_with_the_count_and_nothing_is_written(
        self, tmp_path: Path, decided: int, noun: str
    ) -> None:
        run = _run(tmp_path)

        def accept(document: dict[str, Any]) -> None:
            rows = [candidate for entry in document["requirements"] for candidate in entry["candidates"]]
            for candidate in rows[:decided]:
                candidate["decision"] = "accept"

        _decide(run, accept)
        before = _snapshot(tmp_path / "project")
        with pytest.raises(trace_search.PendingDecisionsError) as caught:
            _rerun(tmp_path / "project")
        assert caught.value.count == decided
        assert str(caught.value) == (
            f"`dist/experiments/trace/proposals.json` holds {decided} {noun} not applied. "
            "Apply them with `--apply`, or search again with `--discard-pending`."
        )
        assert _snapshot(tmp_path / "project") == before

    def test_discard_pending_overwrites(self, tmp_path: Path) -> None:
        run = _run(tmp_path)
        pristine = run.proposals_path.read_bytes()

        def reject(document: dict[str, Any]) -> None:
            next(entry for entry in document["requirements"] if entry["candidates"])["candidates"][0]["decision"] = (
                "reject"
            )

        _decide(run, reject)
        _rerun(tmp_path / "project", discard_pending=True)
        assert run.proposals_path.read_bytes() == pristine

    def test_a_chosen_entry_and_a_confirm_are_pending_work(self, tmp_path: Path) -> None:
        run = _run(tmp_path)

        def decide(document: dict[str, Any]) -> None:
            entry = next(entry for entry in document["requirements"] if entry["id"] == "SR-4")
            rows = [candidate["target"] for candidate in entry["candidates"]]
            outside = next(
                {"kind": "swc", "owner": None, "name": name}
                for name in ("BrakeUnit", "DoorUnit")
                if {"kind": "swc", "owner": None, "name": name} not in rows
            )
            entry["chosen"] = [{"target": outside}]
            awaiting = next(
                link for entry in document["requirements"] for link in entry["linked"] if not link["confirmed"]
            )
            awaiting["decision"] = "confirm"

        _decide(run, decide)
        with pytest.raises(trace_search.PendingDecisionsError) as caught:
            _rerun(tmp_path / "project")
        assert caught.value.count == 2

    def test_a_file_of_applied_and_stale_items_is_no_pending_work(self, tmp_path: Path) -> None:
        run = _run(tmp_path)

        def carried_out(document: dict[str, Any]) -> None:
            rows = [candidate for entry in document["requirements"] for candidate in entry["candidates"]]
            rows[0].update(decision="accept", applied=True)
            rows[1].update(decision="reject", stale="the requirement changed since the search")

        _decide(run, carried_out)
        _rerun(tmp_path / "project")
        proposals = json.loads(run.proposals_path.read_text(encoding="utf-8"))
        assert all(c["decision"] is None for entry in proposals["requirements"] for c in entry["candidates"])

    @pytest.mark.parametrize("broken", [b"{", b'{"formatVersion": 2}', b"\xff"])
    def test_an_unreadable_previous_file_refuses_the_same_way(self, tmp_path: Path, broken: bytes) -> None:
        run = _run(tmp_path)
        run.proposals_path.write_bytes(broken)
        before = _snapshot(tmp_path / "project")
        with pytest.raises(trace_search.PendingDecisionsError) as caught:
            _rerun(tmp_path / "project")
        assert caught.value.count is None
        message = str(caught.value)
        assert message.startswith("`dist/experiments/trace/proposals.json` cannot be read as a proposals file (")
        assert message.endswith(
            "), so it may hold decisions not applied. "
            "Apply it with `--apply`, or search again with `--discard-pending`."
        )
        assert str(tmp_path) not in message
        assert _snapshot(tmp_path / "project") == before
        _rerun(tmp_path / "project", discard_pending=True)
        trace_records.read_proposals(run.proposals_path)

    @pytest.mark.parametrize("during", ["search", "replace"])
    def test_a_decision_saved_while_the_search_runs_refuses_and_nothing_is_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, during: str
    ) -> None:
        run = _run(tmp_path)

        def accept(document: dict[str, Any]) -> None:
            next(c for entry in document["requirements"] for c in entry["candidates"])["decision"] = "accept"

        if during == "search":
            searched = trace_search.search

            def search_and_edit(*arguments: Any, **keys: Any):
                outcome = searched(*arguments, **keys)
                _decide(run, accept)
                return outcome

            monkeypatch.setattr(trace_search, "search", search_and_edit)
        else:
            looked = trace_search.refuse_changed_proposals

            def edit_then_look(*arguments: Any) -> None:
                _decide(run, accept)
                looked(*arguments)

            monkeypatch.setattr(trace_search, "refuse_changed_proposals", edit_then_look)
        before = _snapshot(tmp_path / "project")
        with pytest.raises(trace_search.ProposalsChangedError) as caught:
            _rerun(tmp_path / "project")
        assert str(caught.value) == (
            "`dist/experiments/trace/proposals.json` changed while the search ran, so it may hold decisions not "
            "applied; nothing was written. Apply it with `--apply`, or search again (with `--discard-pending` to "
            "overwrite it)."
        )
        edited = _snapshot(tmp_path / "project")
        proposals = "dist/experiments/trace/proposals.json"
        assert {name: data for name, data in edited.items() if name != proposals} == {
            name: data for name, data in before.items() if name != proposals
        }
        assert trace_records.pending(trace_records.read_proposals(run.proposals_path).model) == 1

    def test_a_proposals_file_created_while_the_search_runs_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = _run(tmp_path)
        kept = run.proposals_path.read_bytes()
        run.proposals_path.unlink()
        searched = trace_search.search

        def search_and_create(*arguments: Any, **keys: Any):
            outcome = searched(*arguments, **keys)
            run.proposals_path.write_bytes(kept)
            return outcome

        monkeypatch.setattr(trace_search, "search", search_and_create)
        with pytest.raises(trace_search.ProposalsChangedError):
            _rerun(tmp_path / "project")
        assert run.proposals_path.read_bytes() == kept
