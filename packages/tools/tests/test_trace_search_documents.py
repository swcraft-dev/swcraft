"""``swcraft experiment trace --documents``: the panel's hint before a search.

The answer is the search's own Documents table over the same index and scope, the
pass the configuration gives each document, and the proposals file a search would
replace -- and nothing is written. Every model is synthetic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from swcraft_tools import trace_records
from swcraft_tools.project import require_project
from swcraft_tools.trace_search import (
    DOCUMENTS_KIND,
    DOCUMENTS_SCHEMA_VERSION,
    AmbiguousIdentityError,
    TraceConfigUnreadableError,
    TraceExperimentError,
    documents_text,
    run_trace,
    run_trace_documents,
)
from trace_models import TraceModel, port, runnable, tree, write_project


def _model() -> TraceModel:
    """Two documents and a no-document group; one link on each document, one awaiting confirmation, one stub."""
    model = TraceModel()
    model.sr_interface("IBrakeTemp", [("temperature", "the brake temperature")])
    model.component(
        "BrakeUnit",
        description="brake temperature supervision",
        ports=[port("pBrakeTemp", interface="IBrakeTemp")],
        runnables=[runnable("Supervise", description="supervise the brake temperature")],
        satisfies=["SR-1@1", "SR-3@1"],
    )
    model.component("DoorUnit", description="door lock control", satisfies=["DR-1@1"])
    model.requirement("SR-1", "Brake temperature", "The brake temperature shall be supervised", document="Brake")
    model.requirement("SR-2", "Brake report", "Report the brake temperature", document="Brake")
    model.requirement("SR-3", "Brake release", "Release the brake", version=2, document="Brake")
    model.requirement("SR-9", "Brake stub", "", document="Brake", stub=True)
    model.requirement("DR-1", "Door lock", "Doors shall lock", document="Door")
    model.requirement("DR-2", "Door unlock", "Doors shall unlock", document="Door")
    model.requirement("N-1", "Lamp", "The lamp shall light")
    return model


def _project(tmp_path: Path, **keys: Any) -> tuple[Path, Path, Path | None]:
    project_dir = tmp_path / "project"
    manifest, config = write_project(_model(), project_dir, **keys)
    return project_dir, manifest, config


def _documents(manifest: Path, config: Path | None) -> dict[str, Any]:
    return run_trace_documents(require_project(str(manifest)), config)


def _passes(answer: dict[str, Any]) -> dict[str | None, str | None]:
    return {row["document"]: row["pass"] for row in answer["documents"]}


class TestTheAnswer:
    def test_it_is_the_search_s_documents_table(self, tmp_path: Path) -> None:
        _dir, manifest, config = _project(tmp_path, labelling=["Door"])
        answer = _documents(manifest, config)
        run = run_trace(require_project(str(manifest)), config)
        assert answer["documents"] == run.document["documents"]
        assert (answer["schemaVersion"], answer["kind"]) == (DOCUMENTS_SCHEMA_VERSION, DOCUMENTS_KIND)

    def test_the_rows_count_the_links(self, tmp_path: Path) -> None:
        _dir, manifest, config = _project(tmp_path)
        rows = {row["document"]: row for row in _documents(manifest, config)["documents"]}
        assert list(rows) == ["Brake", "Door", None]
        brake = rows["Brake"]
        assert (brake["inScope"], brake["linked"], brake["awaitingOnly"], brake["withoutLink"], brake["stubs"]) == (
            3,
            1,
            1,
            1,
            1,
        )
        assert (rows["Door"]["linked"], rows["Door"]["withoutLink"]) == (1, 1)
        assert rows[None]["inScope"] == 1

    @pytest.mark.parametrize(
        ("keys", "expected"),
        [
            ({}, {"Brake": "review", "Door": "review", None: "review"}),
            ({"labelling": ["Door"]}, {"Brake": "review", "Door": "labelling", None: "review"}),
            ({"labelling": "all"}, {"Brake": "labelling", "Door": "labelling", None: "labelling"}),
        ],
        ids=["no-configuration", "a-list", "all"],
    )
    def test_each_pass_is_the_configuration_s(self, tmp_path: Path, keys: dict[str, Any], expected) -> None:
        _dir, manifest, config = _project(tmp_path, **keys)
        assert _passes(_documents(manifest, config)) == expected

    def test_a_document_out_of_scope_has_no_pass(self, tmp_path: Path) -> None:
        _dir, manifest, config = _project(tmp_path, documents=["Brake"], without_document=False)
        assert _passes(_documents(manifest, config)) == {"Brake": "review", "Door": None, None: None}

    def test_the_rejected_pairs_are_counted(self, tmp_path: Path) -> None:
        project_dir, manifest, config = _project(tmp_path)
        run = run_trace(require_project(str(manifest)), config)
        entry = next(entry for entry in run.document["requirements"] if entry["id"] == "SR-2")
        record = trace_records.DecisionRecord.model_validate(
            {"requirement": "SR-2", "version": 1, "target": entry["candidates"][0]["target"], "decision": "reject"}
        )
        (project_dir / trace_records.DECISIONS_FILE).write_text(
            trace_records.decisions_text(trace_records.merge_decisions(trace_records.no_decisions(), [record])),
            encoding="utf-8",
            newline="\n",
        )
        rows = {row["document"]: row for row in _documents(manifest, config)["documents"]}
        assert rows["Brake"]["rejectedEarlier"] == 1

    def test_the_proposals_file_before_and_after_a_search(self, tmp_path: Path) -> None:
        _dir, manifest, config = _project(tmp_path, name="mine")
        assert _documents(manifest, config)["proposals"] is None
        run_trace(require_project(str(manifest)), config)
        assert _documents(manifest, config)["proposals"] == "dist/experiments/mine/proposals.json"

    def test_an_output_names_its_own_directory(self, tmp_path: Path) -> None:
        _dir, manifest, config = _project(tmp_path, output="reports/trace")
        run_trace(require_project(str(manifest)), config)
        assert _documents(manifest, config)["proposals"] == "reports/trace/proposals.json"

    def test_pending_work_does_not_refuse_it(self, tmp_path: Path) -> None:
        project_dir, manifest, config = _project(tmp_path)
        run = run_trace(require_project(str(manifest)), config)
        text = run.proposals_path.read_text(encoding="utf-8").replace('"decision": null', '"decision": "accept"', 1)
        run.proposals_path.write_text(text, encoding="utf-8", newline="\n")
        before = tree(project_dir)
        assert _documents(manifest, config)["proposals"] == "dist/experiments/trace/proposals.json"
        assert tree(project_dir) == before

    def test_the_text_form(self, tmp_path: Path) -> None:
        _dir, manifest, config = _project(tmp_path, labelling=["Door"])
        assert documents_text(_documents(manifest, config)) == (
            "Brake: review: 3 in scope, 1 linked, 1 awaiting confirmation, 1 without a link, 0 rejected earlier; "
            "1 stub\n"
            "Door: labelling: 2 in scope, 1 linked, 0 awaiting confirmation, 1 without a link, 0 rejected earlier\n"
            "No document: review: 1 in scope, 0 linked, 0 awaiting confirmation, 1 without a link, 0 rejected "
            "earlier\n"
            "Proposals file: none yet\n"
        )


class TestItWritesNothing:
    def test_every_byte_of_the_project_is_unchanged(self, tmp_path: Path) -> None:
        project_dir, manifest, config = _project(tmp_path, labelling="all")
        run_trace(require_project(str(manifest)), config)
        before = tree(project_dir)
        _documents(manifest, config)
        assert tree(project_dir) == before


class TestTheRefusals:
    """What the search refuses before it searches, each refused here too, with nothing written."""

    def test_an_ambiguous_identity(self, tmp_path: Path) -> None:
        model = _model()
        model.component("BrakeUnit", description="again", group="other")
        manifest, config = write_project(model, tmp_path / "project")
        before = tree(tmp_path / "project")
        with pytest.raises(AmbiguousIdentityError):
            _documents(manifest, config)
        assert tree(tmp_path / "project") == before

    def test_a_configuration_refused(self, tmp_path: Path) -> None:
        project_dir, manifest, _config = _project(tmp_path, candidates=0)
        before = tree(project_dir)
        with pytest.raises(TraceExperimentError, match="candidates"):
            _documents(manifest, project_dir / "experiments" / "trace.yaml")
        assert tree(project_dir) == before

    def test_a_name_the_configuration_uses_that_the_model_lacks(self, tmp_path: Path) -> None:
        project_dir, manifest, config = _project(tmp_path, documents=["Nowhere"])
        with pytest.raises(TraceExperimentError, match="Nowhere"):
            _documents(manifest, config)
        assert not (project_dir / "dist").exists()

    def test_an_output_over_the_model(self, tmp_path: Path) -> None:
        project_dir, manifest, config = _project(tmp_path, output="model/trace")
        with pytest.raises(TraceExperimentError):
            _documents(manifest, config)
        assert not (project_dir / "model" / "trace").exists()

    def test_an_unreadable_configuration_file(self, tmp_path: Path) -> None:
        _dir, manifest, _config = _project(tmp_path)
        with pytest.raises(TraceConfigUnreadableError):
            _documents(manifest, tmp_path / "missing.yaml")

    def test_a_malformed_decisions_file(self, tmp_path: Path) -> None:
        project_dir, manifest, config = _project(tmp_path)
        (project_dir / trace_records.DECISIONS_FILE).write_text("formatVersion: 7\n", encoding="utf-8", newline="\n")
        before = tree(project_dir)
        with pytest.raises(trace_records.TraceRecordsError) as caught:
            _documents(manifest, config)
        assert caught.value.file == trace_records.DECISIONS_FILE
        assert tree(project_dir) == before
