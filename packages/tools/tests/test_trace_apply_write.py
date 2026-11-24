"""Apply's staged write and the run around it (``--expect-plan``).

Every case compares the bytes of the **whole project directory** before and
after: an applied run moves exactly the model files, the decisions file and the
proposals file it planned; a dry run, a refusal and a rolled-back run move none.
Each failure is injected at one place -- a rename, a restore, a file changed
between the plan and the staging -- and the requirements import's own staged
write is driven through the same failing rename for the parity of the two
copies of the pattern. The models are synthetic (``trace_models``).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import make_empty_model_root, need, needs_document
from swcraft_tools import trace_apply
from swcraft_tools.requirements_import import apply_requirements_import, plan_requirements_import
from swcraft_tools.requirements_interchange import parse_needs_text
from swcraft_tools.trace_apply import ApplyRun, TraceApplyError, apply_plan, plan_apply, run_apply
from swcraft_tools.trace_records import DECISIONS_FILE, TraceRecordsError, parse_decisions
from swcraft_tools.validator import check_requirement_traceability
from swcraft_tools.yaml_load import load_yaml_document
from trace_models import OUT, STEP, TOP, ApplyProject, apply_model, confirming, failing_replace, tree

CTL_FILE = "model/swc/app/Ctl.yaml"
PROPOSALS = "dist/experiments/trace/proposals.json"


def no_staging(directory: Path) -> bool:
    return not [path for path in directory.rglob("*") if path.name.endswith(".tmp")]


def apply(project: ApplyProject, *, dry_run: bool = False, expect_plan: str | None = None) -> ApplyRun:
    return run_apply(project.project, project.proposals, dry_run=dry_run, expect_plan=expect_plan)


@pytest.fixture
def project(tmp_path: Path) -> ApplyProject:
    return ApplyProject(tmp_path, apply_model())


@pytest.fixture
def three_files(project: ApplyProject) -> ApplyProject:
    """A plan that writes all three kinds of file: one model file, the decisions file (created), the proposals file."""
    project.write(confirming(project, {"SR-2": [(OUT, "accept"), (TOP, "reject")]}))
    return project


# ---------------------------------------------------------------------------
# An applied run and a dry run
# ---------------------------------------------------------------------------


class TestTheRun:
    def test_an_applied_run_writes_the_links_the_decisions_and_the_marks(self, three_files: ApplyProject) -> None:
        before = tree(three_files.dir)
        run = apply(three_files)
        assert run.written == (CTL_FILE, DECISIONS_FILE, PROPOSALS)
        after = tree(three_files.dir)
        assert {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)} == {
            CTL_FILE,
            DECISIONS_FILE,
            PROPOSALS,
        }
        assert after[CTL_FILE] == run.plan.model_files[0].text.encode("utf-8")
        component = load_yaml_document(after[CTL_FILE].decode("utf-8"))
        assert component["satisfies"] == ["SR-1@2"]
        assert component["ports"][0]["satisfies"] == ["SR-2@1"]
        records = parse_decisions(after[DECISIONS_FILE].decode("utf-8")).decisions
        assert [(record.requirement, record.decision) for record in records] == [
            ("SR-1", "confirm"),
            ("SR-2", "accept"),
            ("SR-2", "reject"),
        ]
        marked = json.loads(after[PROPOSALS])["requirements"]
        assert marked[0]["linked"][0]["applied"] is True
        assert [row["applied"] for row in marked[1]["candidates"]] == [True, True]
        assert no_staging(three_files.dir)

    def test_the_report_is_the_plans_and_rendered_before_the_write(self, three_files: ApplyProject) -> None:
        run = apply(three_files)
        assert "Dry run" not in run.report
        assert (
            "Summary: 1 written, 1 confirmed, 1 rejected, 0 already done, 0 stale, 0 not written, 0 undecided; 1 files"
            in (run.report)
        )
        assert run.document["dryRun"] is False and run.document["plan"] == run.plan.digest

    def test_a_dry_run_writes_nothing(self, three_files: ApplyProject) -> None:
        before = tree(three_files.dir)
        run = apply(three_files, dry_run=True)
        assert run.written == ()
        assert run.report.splitlines()[1] == "Dry run: nothing was written."
        assert tree(three_files.dir) == before

    def test_a_second_apply_writes_nothing_and_reports_already_done(self, three_files: ApplyProject) -> None:
        apply(three_files)
        before = tree(three_files.dir)
        run = apply(three_files)
        assert run.written == ()
        assert tree(three_files.dir) == before
        assert (
            "Summary: 0 written, 0 confirmed, 0 rejected, 3 already done, 0 stale, 0 not written, 0 undecided; 0 files"
            in (run.report)
        )
        assert "No change." in run.report
        assert run.report.count("applied by an earlier Apply") == 3

    def test_a_link_removed_by_hand_after_an_apply_is_not_written_back(self, three_files: ApplyProject) -> None:
        apply(three_files)
        three_files.edit("swc/app/Ctl.yaml", lambda d: d["ports"][0].update(satisfies=[]))
        before = tree(three_files.dir)
        assert apply(three_files).written == ()
        assert tree(three_files.dir) == before
        component = load_yaml_document((three_files.root / "swc/app/Ctl.yaml").read_text(encoding="utf-8"))
        assert component["ports"][0]["satisfies"] == []

    def test_every_stale_item_carries_its_reason_after_an_applied_run_and_none_after_a_dry_run(
        self, project: ApplyProject
    ) -> None:
        project.write(project.propose({"SR-2": [(OUT, "accept"), (STEP, "reject")], "SR-3": [(TOP, None)]}))
        project.edit("swc/app/Ctl.yaml", lambda d: d.update(ports=[], runnables=[]))
        apply(project, dry_run=True)
        rows = json.loads(project.proposals.read_bytes())["requirements"][0]["candidates"]
        assert ["stale" in row for row in rows] == [False, False]
        run = apply(project)
        assert run.written == (PROPOSALS,)  # new stale marks alone: the proposals file and nothing else
        marked = json.loads(project.proposals.read_bytes())["requirements"]
        assert [row["stale"] for row in marked[0]["candidates"]] == [
            "port 'pOut' of SWC 'Ctl' is no longer in the model",
            "runnable 'Step' of SWC 'Ctl' is no longer in the model",
        ]
        assert "stale" not in marked[1]["candidates"][0] and "applied" not in marked[1]["candidates"][0]

    def test_the_decisions_file_is_created_by_the_first_record(self, project: ApplyProject) -> None:
        project.write(project.propose({"SR-2": [(TOP, "reject")]}))
        assert not (project.dir / DECISIONS_FILE).exists()
        assert DECISIONS_FILE in apply(project).written
        assert (project.dir / DECISIONS_FILE).is_file()

    def test_an_apply_that_records_nothing_creates_no_decisions_file(self, project: ApplyProject) -> None:
        project.write(project.propose({"SR-2": [(TOP, "accept")]}))
        project.edit("compositions/Top.yaml", lambda d: d.update(satisfies=["SR-2@1"]))
        assert apply(project).written == (PROPOSALS,)
        assert not (project.dir / DECISIONS_FILE).exists()


# ---------------------------------------------------------------------------
# --expect-plan
# ---------------------------------------------------------------------------


class TestExpectPlan:
    def test_the_dry_runs_digest_lets_the_applied_run_write(self, three_files: ApplyProject) -> None:
        digest = apply(three_files, dry_run=True).plan.digest
        assert apply(three_files, expect_plan=digest).written == (CTL_FILE, DECISIONS_FILE, PROPOSALS)

    def test_a_different_digest_refuses_with_nothing_written(self, three_files: ApplyProject) -> None:
        digest = apply(three_files, dry_run=True).plan.digest
        three_files.edit("compositions/Top.yaml", lambda d: d.update(satisfies=["SR-3@1"]))
        document = json.loads(three_files.proposals.read_bytes())
        document["requirements"][1]["candidates"][1]["decision"] = "accept"
        three_files.write(document)
        before = tree(three_files.dir)
        with pytest.raises(TraceApplyError, match=r"^the plan changed since the dry run; run the dry run again$"):
            apply(three_files, expect_plan=digest)
        assert tree(three_files.dir) == before

    def test_the_digest_moves_with_the_planned_text_alone(self, project: ApplyProject) -> None:
        """A confirm carries no fingerprint: a hand edit of its file between the two runs moves only its text."""
        project.write(confirming(project, {}))
        digest = apply(project, dry_run=True).plan.digest
        project.edit("swc/app/Ctl.yaml", lambda d: d.update(description="edited after the dry run"))
        before = tree(project.dir)
        with pytest.raises(TraceApplyError, match=r"^the plan changed since the dry run; run the dry run again$"):
            apply(project, expect_plan=digest)
        assert tree(project.dir) == before


# ---------------------------------------------------------------------------
# A planned text that does not read back
# ---------------------------------------------------------------------------


class TestReadBack:
    def test_a_text_that_is_no_yaml_refuses_with_nothing_written(
        self, three_files: ApplyProject, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(trace_apply, "edit_satisfies", lambda text, changes: ("name: [unclosed\n", {}))
        before = tree(three_files.dir)
        with pytest.raises(TraceApplyError, match=r"^SWC 'Ctl' would not read back as planned: ParserError$"):
            apply(three_files)
        assert tree(three_files.dir) == before

    def test_a_text_that_reads_back_as_another_element_refuses_with_nothing_written(
        self, three_files: ApplyProject, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_edit = trace_apply.edit_satisfies

        def edit(text: str, changes: Any) -> tuple[str, dict[int, str]]:
            edited, refused = real_edit(text, changes)
            return edited.replace("brake control", "other"), refused

        monkeypatch.setattr(trace_apply, "edit_satisfies", edit)
        before = tree(three_files.dir)
        with pytest.raises(
            TraceApplyError,
            match=r"^SWC 'Ctl': its planned file would not read back as the element it was built from$",
        ):
            apply(three_files)
        assert tree(three_files.dir) == before


# ---------------------------------------------------------------------------
# Nothing changed underneath
# ---------------------------------------------------------------------------


def _change_model(project: ApplyProject) -> str:
    project.edit("swc/app/Ctl.yaml", lambda d: d.update(description="edited while Apply ran"))
    return CTL_FILE


def _create_decisions(project: ApplyProject) -> str:
    (project.dir / DECISIONS_FILE).write_text("formatVersion: 1\ndecisions: []\n", encoding="utf-8", newline="\n")
    return DECISIONS_FILE


def _tick_saved(project: ApplyProject) -> str:
    document = json.loads(project.proposals.read_bytes())
    document["requirements"][1]["candidates"][0]["preselected"] = True  # the panel saved a tick
    project.write(document)
    return PROPOSALS


@pytest.mark.parametrize(
    "change", [_change_model, _create_decisions, _tick_saved], ids=["a model file", "the decisions file", "proposals"]
)
def test_a_file_changed_between_the_plan_and_the_staging_refuses_with_nothing_written(
    project: ApplyProject, change: Callable[[ApplyProject], str]
) -> None:
    project.write(confirming(project, {"SR-2": [(OUT, "accept")], "SR-3": []}))
    plan = plan_apply(project.project, project.proposals)
    target = change(project)
    before = tree(project.dir)
    with pytest.raises(TraceApplyError) as caught:
        apply_plan(plan)
    assert str(caught.value) == f"the file {target} changed while Apply ran; run it again"
    assert tree(project.dir) == before
    assert no_staging(project.dir)


def test_a_decisions_file_changed_but_not_rewritten_still_refuses(project: ApplyProject) -> None:
    """The comparison reaches every file the plan read, the decisions file included when the run records nothing."""
    project.write(project.propose({"SR-2": [(TOP, "accept")]}))
    project.edit("compositions/Top.yaml", lambda d: d.update(satisfies=["SR-2@1"]))
    plan = plan_apply(project.project, project.proposals)
    assert plan.decisions is None and plan.proposals is not None
    _create_decisions(project)
    before = tree(project.dir)
    with pytest.raises(TraceApplyError, match=f"the file {DECISIONS_FILE} changed"):
        apply_plan(plan)
    assert tree(project.dir) == before


def test_a_reject_only_plan_refuses_when_its_target_changed(project: ApplyProject) -> None:
    """A reject's target is not rewritten, yet its content decided the record: it is compared too."""
    project.write(project.propose({"SR-2": [(TOP, "reject")]}))
    plan = plan_apply(project.project, project.proposals)
    assert [item.requirement for item in plan.rejects] == ["SR-2"] and plan.model_files == ()
    project.edit("compositions/Top.yaml", lambda d: d.update(satisfies=["SR-2@1"]))  # linked by hand meanwhile
    before = tree(project.dir)
    with pytest.raises(TraceApplyError) as caught:
        apply_plan(plan)
    assert str(caught.value) == "the file model/compositions/Top.yaml changed while Apply ran; run it again"
    assert tree(project.dir) == before
    assert no_staging(project.dir)


def test_an_accept_already_done_is_not_marked_when_its_link_went_meanwhile(project: ApplyProject) -> None:
    """An accept found already linked is marked applied only if the link still stands when the marks are written."""
    project.write(project.propose({"SR-2": [(TOP, "accept")]}))
    project.edit("compositions/Top.yaml", lambda d: d.update(satisfies=["SR-2@1"]))
    plan = plan_apply(project.project, project.proposals)
    assert [item.reason for item in plan.already_done] == ["already linked as SR-2@1"]
    project.edit("compositions/Top.yaml", lambda d: d.update(satisfies=[]))  # the link removed meanwhile
    before = tree(project.dir)
    with pytest.raises(TraceApplyError, match="the file model/compositions/Top.yaml changed"):
        apply_plan(plan)
    assert tree(project.dir) == before
    assert [item.requirement for item in apply(project).plan.writes] == ["SR-2"]


def test_an_applied_run_never_leaves_a_list_naming_a_requirement_twice(project: ApplyProject) -> None:
    """``trace.duplicate-link`` (contract ``12.5.0``): Apply creates no duplicate, and its confirm removes one.

    The accept's holder names SR-2 already, at another version: it is *already
    done* and nothing is appended. The confirm's holder holds SR-1 at both the
    old and the current version (a duplicate written by hand after the search):
    the old member goes and the current one is not written twice. The model is
    refused by the rule before the run and passes it after.
    """
    project.write(confirming(project, {"SR-2": [(OUT, "accept")]}))
    project.edit("swc/app/Ctl.yaml", lambda d: d.update(satisfies=["SR-1@1", "SR-1@2"]))
    project.edit("swc/app/Ctl.yaml", lambda d: d["ports"][0].update(satisfies=["SR-2@7"]))
    assert [record.target for record in _duplicates(project)] == ["SR-1"]

    run = apply(project)

    assert [item.reason for item in run.plan.already_done] == ["already linked as SR-2@7"]
    component = load_yaml_document((project.root / "swc" / "app" / "Ctl.yaml").read_text(encoding="utf-8"))
    assert component["satisfies"] == ["SR-1@2"]
    assert component["ports"][0]["satisfies"] == ["SR-2@7"]
    assert _duplicates(project) == []


def _duplicates(project: ApplyProject) -> list[Any]:
    """The ``trace.duplicate-link`` findings of *project*'s model, as ``check-model`` mints them."""
    records = check_requirement_traceability(str(project.root)).records
    return [record for record in records if record.rule == "trace.duplicate-link"]


# ---------------------------------------------------------------------------
# The rollback
# ---------------------------------------------------------------------------


class TestTheRollback:
    @pytest.mark.parametrize("failing", [1, 2, 3], ids=["the model file", "the decisions file", "the proposals file"])
    def test_a_failing_rename_leaves_every_file_at_its_prior_bytes(
        self, three_files: ApplyProject, monkeypatch: pytest.MonkeyPatch, failing: int
    ) -> None:
        before = tree(three_files.dir)
        calls = failing_replace(monkeypatch, failing)
        with pytest.raises(OSError, match="injected rename failure"):
            apply(three_files)
        assert [Path(call).name for call in calls][-1] == ["Ctl.yaml", DECISIONS_FILE, "proposals.json"][failing - 1]
        assert tree(three_files.dir) == before
        assert no_staging(three_files.dir)

    def test_a_failing_restore_is_named_project_relative_and_the_rest_restored(
        self, three_files: ApplyProject, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = tree(three_files.dir)
        failing_replace(monkeypatch, 3)
        real_write_bytes = Path.write_bytes

        def write_bytes(self: Path, data: bytes) -> int:
            if self.name == "Ctl.yaml":
                raise OSError("injected restore failure")
            return real_write_bytes(self, data)

        monkeypatch.setattr(Path, "write_bytes", write_bytes)
        with pytest.raises(OSError, match="injected rename failure") as caught:
            apply(three_files)
        assert caught.value.__notes__ == [f"the rollback could not restore: {CTL_FILE}"]
        after = tree(three_files.dir)
        assert after[CTL_FILE] != before[CTL_FILE]  # the one restore that failed
        del after[CTL_FILE], before[CTL_FILE]
        assert after == before
        assert str(three_files.dir) not in caught.value.__notes__[0]

    def test_the_parity_with_the_requirements_import(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both copies of the staged pattern, through the same failing second rename, restore the same state."""
        project = ApplyProject(tmp_path / "trace", apply_model())
        project.write(project.propose({"SR-2": [(OUT, "accept")]}))
        root = make_empty_model_root(tmp_path / "import")
        (root / "requirements").mkdir(exist_ok=True)
        (root / "requirements/SWR-1.yaml").write_text(
            "id: SWR-1\ntitle: Old\ndescription: Text.\nreq_type: req\nversion: 1\nsource: external\nbaseline: SRS-1\n",
            encoding="utf-8",
            newline="\n",
        )
        selected = parse_needs_text(json.dumps(needs_document(need("SWR-1"), need("SWR-2"))))
        import_plan = plan_requirements_import(root, selected)
        assert len(import_plan.writes) == 2

        outcomes = []
        for directory, run in (
            (project.dir, lambda: apply(project)),
            (root, lambda: apply_requirements_import(import_plan)),
        ):
            before = tree(directory)
            with monkeypatch.context() as patch:
                failing_replace(patch, 2)
                with pytest.raises(OSError) as caught:
                    run()
            outcomes.append((tree(directory) == before, no_staging(directory), str(caught.value)))
        assert outcomes == [(True, True, "injected rename failure")] * 2


# ---------------------------------------------------------------------------
# Recovery from a run killed after its model renames
# ---------------------------------------------------------------------------


def test_a_run_killed_after_its_model_renames_converges_on_the_next_apply(three_files: ApplyProject) -> None:
    plan = plan_apply(three_files.project, three_files.proposals)
    for planned in plan.model_files:  # the renames a killed run got through
        planned.path.write_text(planned.text, encoding="utf-8", newline="\n")
    model_before = three_files.root.joinpath("swc/app/Ctl.yaml").read_bytes()
    run = apply(three_files)
    assert run.plan.model_files == ()
    assert run.written == (DECISIONS_FILE, PROPOSALS)
    assert three_files.root.joinpath("swc/app/Ctl.yaml").read_bytes() == model_before
    assert "SR-1 on SWC 'Ctl': already confirmed" in run.report
    assert "SR-2 on SWC 'Ctl' (port 'pOut'): already linked as SR-2@1" in run.report
    records = parse_decisions((three_files.dir / DECISIONS_FILE).read_text(encoding="utf-8")).decisions
    assert [(record.requirement, record.decision) for record in records] == [("SR-2", "reject")]
    marked = json.loads(three_files.proposals.read_bytes())["requirements"]
    assert marked[0]["linked"][0]["applied"] is True
    assert [row["applied"] for row in marked[1]["candidates"]] == [True, True]


# ---------------------------------------------------------------------------
# The confirm-member refusals, through the run
# ---------------------------------------------------------------------------


def _member_edit(**slots: Any) -> Callable[[dict[str, Any]], None]:
    def edit(link: dict[str, Any]) -> None:
        link.update(decision="confirm", **slots)

    return edit


CONFIRM_MEMBER_REFUSALS = [
    ("a member naming another requirement", _member_edit(member="SR-3@1"), "/requirements/0/linked/0/member"),
    ("a member in the element form", _member_edit(member="compositions:Top"), "/requirements/0/linked/0/member"),
    ("a member disagreeing with version", _member_edit(version=3), "/requirements/0/linked/0/version"),
    ("a bare member with a version", _member_edit(member="SR-1"), "/requirements/0/linked/0/version"),
    (
        "a confirm whose member already names the recorded version",
        _member_edit(member="SR-1@2", version=2),
        "/requirements/0/linked/0/confirmed",
    ),
]


@pytest.mark.parametrize(
    ("name", "edit", "pointer"), CONFIRM_MEMBER_REFUSALS, ids=[case[0] for case in CONFIRM_MEMBER_REFUSALS]
)
def test_a_confirm_member_refusal_through_the_run_leaves_every_byte_unchanged(
    tmp_path: Path, name: str, edit: Callable[[dict[str, Any]], None], pointer: str
) -> None:
    model = apply_model()
    model.get("swc/app/Ctl.yaml")["satisfies"] = ["SR-1@1", "SR-3@1"]
    project = ApplyProject(tmp_path, model)
    document = project.propose({"SR-1": [], "SR-2": [(TOP, "accept")]})
    edit(document["requirements"][0]["linked"][0])
    project.write(document)
    before = tree(project.dir)
    for dry_run in (True, False):
        with pytest.raises(TraceRecordsError) as caught:
            apply(project, dry_run=dry_run)
        assert caught.value.pointer == pointer, name
    assert tree(project.dir) == before
