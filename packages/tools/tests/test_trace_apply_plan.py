"""Apply's plan and its report: the re-check, the planned texts, the records, the marks, the digest.

Nothing here writes but one applied run of marks alone:
:func:`~swcraft_tools.trace_apply.plan_apply` decides, and the staged write is
``test_trace_apply_write.py``'s. Every model is synthetic
(``trace_models``), written in the writer's own form first unless a test is
about a file that is not, and every proposals file is built by
``proposals_for`` and changed in the one slot a test is about.
"""

from __future__ import annotations

import dataclasses
import difflib
import json
from pathlib import Path
from typing import Any

import pytest
from swcraft_tools import experiment_frame, trace_records
from swcraft_tools.trace_apply import (
    ALREADY_DONE,
    APPLIED_EARLIER,
    NOT_WRITTEN,
    PLANNED,
    STALE,
    ApplyPlan,
    TraceApplyError,
    plan_apply,
    plan_digest,
    render_report,
    report_document,
    run_apply,
)
from swcraft_tools.trace_records import DECISIONS_FILE, TraceRecordsError, parse_decisions, parse_proposals
from swcraft_tools.trace_search import TargetAddress
from swcraft_tools.yaml_load import load_yaml_document
from trace_models import (
    CTL,
    OUT,
    STEP,
    TOP,
    ApplyProject,
    TraceModel,
    apply_model,
    port,
    runnable,
    tree,
    write_proposals,
    writer_form,
)


def _one(plan: ApplyPlan) -> Any:
    assert len(plan.items) == 1, plan.items
    return plan.items[0]


def _text(plan: ApplyPlan, target: str) -> str:
    return next(planned.text for planned in plan.model_files if planned.target == target)


@pytest.fixture
def project(tmp_path: Path) -> ApplyProject:
    return ApplyProject(tmp_path, apply_model())


# ---------------------------------------------------------------------------
# The re-check, row by row, with its reasons and its order
# ---------------------------------------------------------------------------


class TestTheRecheck:
    def test_a_requirement_no_longer_in_the_model_is_stale(self, project: ApplyProject) -> None:
        document = project.propose({"SR-2": [(TOP, "accept")]})
        (project.root / "requirements" / "SR-2.yaml").unlink()
        item = _one(project.plan(document))
        assert (item.outcome, item.reason) == (STALE, "SR-2 is no longer in the model")

    def test_a_requirement_declared_twice_is_stale(self, project: ApplyProject) -> None:
        document = project.propose({"SR-2": [(TOP, "accept")]})
        original = project.root / "requirements" / "SR-2.yaml"
        (project.root / "requirements" / "Copy.yaml").write_bytes(original.read_bytes())
        item = _one(project.plan(document))
        assert (item.outcome, item.reason) == (STALE, "SR-2 is declared twice")

    def test_a_requirement_at_a_new_version_is_stale_naming_it(self, project: ApplyProject) -> None:
        document = project.propose({"SR-2": [(TOP, "accept")]})
        project.edit("requirements/SR-2.yaml", lambda d: d.update(version=2, description="Doors shall lock fast"))
        item = _one(project.plan(document))
        assert (item.outcome, item.reason) == (STALE, "SR-2 changed since the search (now version 2)")

    def test_a_requirement_edited_at_the_same_version_is_stale(self, project: ApplyProject) -> None:
        document = project.propose({"SR-2": [(TOP, "accept")]})
        project.edit("requirements/SR-2.yaml", lambda d: d.update(title="Door locking"))
        item = _one(project.plan(document))
        assert (item.outcome, item.reason) == (STALE, "SR-2 changed since the search (same version)")

    def test_a_target_no_longer_in_the_model_is_stale(self, project: ApplyProject) -> None:
        document = project.propose({"SR-2": [(OUT, "accept")]})
        project.edit("swc/app/Ctl.yaml", lambda d: d.update(ports=[]))
        item = _one(project.plan(document))
        assert (item.outcome, item.reason) == (STALE, "port 'pOut' of SWC 'Ctl' is no longer in the model")

    def test_a_target_declared_twice_is_stale(self, project: ApplyProject) -> None:
        document = project.propose({"SR-2": [(CTL, "accept")]})
        copy = project.root / "swc" / "other" / "Ctl.yaml"
        copy.parent.mkdir()
        copy.write_bytes((project.root / "swc" / "app" / "Ctl.yaml").read_bytes())
        item = _one(project.plan(document))
        assert (item.outcome, item.reason) == (STALE, "SWC 'Ctl' is declared twice")

    def test_a_row_whose_target_changed_is_stale(self, project: ApplyProject) -> None:
        document = project.propose({"SR-2": [(OUT, "accept")]})
        project.edit("swc/app/Ctl.yaml", lambda d: d["ports"][0].update(description="a new text"))
        item = _one(project.plan(document))
        assert (item.outcome, item.reason) == (STALE, "port 'pOut' of SWC 'Ctl' changed since the search")

    def test_a_link_on_the_target_moves_no_fingerprint(self, project: ApplyProject) -> None:
        document = project.propose({"SR-2": [(OUT, "accept")]})
        project.edit("swc/app/Ctl.yaml", lambda d: d["ports"][0].update(satisfies=["SR-3@1"]))
        assert _one(project.plan(document)).outcome == PLANNED

    def test_the_requirement_is_reported_before_its_target(self, project: ApplyProject) -> None:
        document = project.propose({"SR-2": [(OUT, "accept")]})
        project.edit("requirements/SR-2.yaml", lambda d: d.update(version=2, title="Locking"))
        project.edit("swc/app/Ctl.yaml", lambda d: d.update(ports=[]))
        item = _one(project.plan(document))
        assert item.reason == "SR-2 changed since the search (now version 2)"

    @pytest.mark.parametrize("member", ["SR-2", "SR-2@1", "SR-2@x", "SR-2@7"])
    def test_an_accept_already_linked_in_any_form_is_already_done(self, project: ApplyProject, member: str) -> None:
        document = project.propose({"SR-2": [(OUT, "accept")]})
        project.edit("swc/app/Ctl.yaml", lambda d: d["ports"][0].update(satisfies=[member]))
        item = _one(project.plan(document))
        assert (item.outcome, item.reason) == (ALREADY_DONE, f"already linked as {member}")

    def test_an_element_form_member_is_no_link_of_the_requirement(self, project: ApplyProject) -> None:
        document = project.propose({"SR-2": [(TOP, "accept")]})
        project.edit("compositions/Top.yaml", lambda d: d.update(satisfies=["swc:SR-2"]))
        assert _one(project.plan(document)).outcome == PLANNED

    @pytest.mark.parametrize("member", ["SR-2", "SR-2@1"])
    def test_a_reject_of_a_pair_linked_since_the_search_is_already_done(
        self, project: ApplyProject, member: str
    ) -> None:
        document = project.propose({"SR-2": [(OUT, "reject")]})
        project.edit("swc/app/Ctl.yaml", lambda d: d["ports"][0].update(satisfies=[member]))
        plan = project.plan(document)
        item = _one(plan)
        assert (item.outcome, item.reason) == (ALREADY_DONE, f"already linked as {member}")
        assert plan.records == () and plan.decisions is None and plan.model_files == ()
        assert plan.proposals is not None
        assert json.loads(plan.proposals.text)["requirements"][0]["candidates"][0]["applied"] is True

    def test_a_confirm_found_in_place_is_already_confirmed(self, project: ApplyProject) -> None:
        document = project.propose({"SR-1": []})
        document["requirements"][0]["linked"][0]["decision"] = "confirm"
        project.edit("swc/app/Ctl.yaml", lambda d: d.update(satisfies=["SR-1@2"]))
        item = _one(project.plan(document))
        assert (item.outcome, item.reason) == (ALREADY_DONE, "already confirmed")

    def test_a_confirm_whose_link_moved_otherwise_is_stale(self, project: ApplyProject) -> None:
        document = project.propose({"SR-1": []})
        document["requirements"][0]["linked"][0]["decision"] = "confirm"
        project.edit("swc/app/Ctl.yaml", lambda d: d.update(satisfies=["SR-1"]))
        item = _one(project.plan(document))
        assert (item.outcome, item.reason) == (STALE, "the link changed since the search")


class TestHardwareKinds:
    """All four hardware kinds share one layer: a target of the wrong kind is no element of the model."""

    @pytest.fixture
    def every_kind(self, tmp_path: Path) -> ApplyProject:
        return ApplyProject(tmp_path, _every_kind())

    def test_a_chosen_entry_naming_a_core_as_a_processor_is_stale(self, every_kind: ApplyProject) -> None:
        processor = TargetAddress("processor", None, "Core0")
        item = _one(every_kind.plan(every_kind.propose({"R-1": []}, chosen={"R-1": [processor]})))
        assert (item.outcome, item.reason) == (STALE, "Processor 'Core0' is no longer in the model")

    def test_a_row_naming_a_core_as_a_processor_is_stale(self, every_kind: ApplyProject) -> None:
        document = every_kind.propose({"R-1": [(TargetAddress("core", None, "Core0"), "accept")]})
        document["requirements"][0]["candidates"][0]["target"]["kind"] = "processor"
        item = _one(every_kind.plan(document))
        assert (item.outcome, item.reason) == (STALE, "Processor 'Core0' is no longer in the model")

    def test_the_right_kind_is_planned(self, every_kind: ApplyProject) -> None:
        core = TargetAddress("core", None, "Core0")
        assert _one(every_kind.plan(every_kind.propose({"R-1": []}, chosen={"R-1": [core]}))).outcome == PLANNED


class TestStubs:
    @pytest.fixture
    def stubbed(self, tmp_path: Path) -> ApplyProject:
        model = apply_model()
        model.component("Outside", external=True, ports=[port("pBus", interface="IExt")], satisfies=["SR-3@1"])
        model.external_interface("IExt", data_elements=["frame"])
        return ApplyProject(tmp_path, model)

    @pytest.mark.parametrize(
        "target",
        [
            TargetAddress("swc", None, "Outside"),
            TargetAddress("port", "Outside", "pBus"),
            TargetAddress("interface", None, "IExt"),
        ],
        ids=["component", "port", "interface"],
    )
    def test_a_chosen_entry_on_an_external_stub_is_stale(self, stubbed: ApplyProject, target: TargetAddress) -> None:
        document = stubbed.propose({"SR-2": []}, chosen={"SR-2": [target]})
        item = _one(stubbed.plan(document))
        assert (item.outcome, item.reason) == (STALE, "an external stub is never linked")

    def test_a_confirm_of_a_link_a_stub_holds_is_planned(self, stubbed: ApplyProject) -> None:
        stubbed.edit("requirements/SR-3.yaml", lambda d: d.update(version=2))
        document = stubbed.propose({"SR-3": []})
        document["requirements"][0]["linked"][0]["decision"] = "confirm"
        plan = stubbed.plan(document)
        item = _one(plan)
        assert (item.outcome, item.member, item.replaced) == (PLANNED, "SR-3@2", "SR-3@1")
        assert "- SR-3@2\n" in _text(plan, "model/swc/app/Outside.yaml")

    def test_a_chosen_entry_on_a_valid_target_is_planned_with_no_fingerprint(self, project: ApplyProject) -> None:
        document = project.propose({"SR-2": []}, chosen={"SR-2": [TOP]})
        project.edit("compositions/Top.yaml", lambda d: d.update(description="changed after the search"))
        item = _one(project.plan(document))
        assert (item.outcome, item.slot, item.member) == (PLANNED, "chosen", "SR-2@1")


# ---------------------------------------------------------------------------
# The planned texts
# ---------------------------------------------------------------------------


def _every_kind() -> TraceModel:
    model = TraceModel()
    model.sr_interface("IData", [("value", "a value")])
    model.component("Ctl", ports=[port("pOut", interface="IData")], runnables=[runnable("Step")])
    model.composition("Top")
    model.task("Cyclic", partition="AppPartition")
    model.hardware("Mcu", "processor")
    model.hardware("Core0", "core")
    model.hardware("Ram", "memory")
    model.hardware("AppPartition", "partition")
    for number in range(1, 13):
        model.requirement(f"R-{number}", f"Requirement {number}", "text")
    return model


_KINDS = [
    ("R-1", TargetAddress("swc", None, "Ctl"), "swc/app/Ctl.yaml"),
    ("R-2", TargetAddress("port", "Ctl", "pOut"), "swc/app/Ctl.yaml"),
    ("R-3", TargetAddress("runnable", "Ctl", "Step"), "swc/app/Ctl.yaml"),
    ("R-4", TargetAddress("interface", None, "IData"), "interfaces/IData.yaml"),
    ("R-5", TargetAddress("composition", None, "Top"), "compositions/Top.yaml"),
    ("R-6", TargetAddress("task", None, "Cyclic"), "tasks/Cyclic.yaml"),
    ("R-7", TargetAddress("processor", None, "Mcu"), "hardware/Mcu.yaml"),
    ("R-8", TargetAddress("core", None, "Core0"), "hardware/Core0.yaml"),
    ("R-9", TargetAddress("memory", None, "Ram"), "hardware/Ram.yaml"),
    ("R-10", TargetAddress("partition", None, "AppPartition"), "hardware/AppPartition.yaml"),
]


class TestPlannedTexts:
    @pytest.fixture
    def every_kind(self, tmp_path: Path) -> ApplyProject:
        return ApplyProject(tmp_path, _every_kind())

    def test_an_accept_on_each_of_the_ten_kinds_is_appended_last(self, every_kind: ApplyProject) -> None:
        for _, target, relative in _KINDS:
            key = None if target.owner is None else f"{target.kind}s"
            every_kind.edit(relative, lambda d, key=key: (d if key is None else d[key][0]).update(satisfies=["OLD-1"]))
        writer_form(every_kind.root)
        plan = every_kind.plan(every_kind.propose({rid: [(target, "accept")] for rid, target, _ in _KINDS}))
        assert [item.outcome for item in plan.items] == [PLANNED] * 10
        state = {planned.target: load_yaml_document(planned.text) for planned in plan.model_files}
        for rid, target, relative in _KINDS:
            element = state[f"model/{relative}"]
            holder = (
                element
                if target.owner is None
                else next(member for member in element[f"{target.kind}s"] if member["name"] == target.name)
            )
            assert holder["satisfies"][-1] == f"{rid}@1"
            assert holder["satisfies"][0] == "OLD-1"
        assert len(plan.model_files) == 8  # the component's three targets share its one file

    def test_several_accepts_on_one_target_are_appended_in_the_id_order(self, every_kind: ApplyProject) -> None:
        plan = every_kind.plan(every_kind.propose({rid: [(TOP, "accept")] for rid in ("R-12", "R-2", "R-11")}))
        assert load_yaml_document(_text(plan, "model/compositions/Top.yaml"))["satisfies"] == [
            "R-11@1",
            "R-12@1",
            "R-2@1",
        ]

    def test_the_files_are_in_the_address_order(self, every_kind: ApplyProject) -> None:
        plan = every_kind.plan(every_kind.propose({rid: [(target, "accept")] for rid, target, _ in reversed(_KINDS)}))
        assert [planned.target for planned in plan.model_files] == [
            "model/swc/app/Ctl.yaml",
            "model/compositions/Top.yaml",
            "model/interfaces/IData.yaml",
            "model/tasks/Cyclic.yaml",
            "model/hardware/AppPartition.yaml",
            "model/hardware/Core0.yaml",
            "model/hardware/Mcu.yaml",
            "model/hardware/Ram.yaml",
        ]

    def test_each_planned_text_only_adds_lines(self, every_kind: ApplyProject) -> None:
        """Every line the file held stays, in its place; only satisfies lines are added."""
        plan = every_kind.plan(every_kind.propose({rid: [(target, "accept")] for rid, target, _ in _KINDS}))
        for planned in plan.model_files:
            assert planned.prior is not None
            before, after = planned.prior.decode("utf-8").splitlines(), planned.text.splitlines()
            matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
            changes = [code for code in matcher.get_opcodes() if code[0] != "equal"]
            assert changes and all(tag == "insert" for tag, *_ in changes), planned.target
            added = [line for tag, _a, _b, start, end in changes for line in after[start:end]]
            assert all(line.strip().startswith(("satisfies:", "- ")) for line in added), added

    def test_a_confirm_replaces_its_member_in_place(self, project: ApplyProject) -> None:
        project.edit("swc/app/Ctl.yaml", lambda d: d.update(satisfies=["SR-3@1", "SR-1@1", "SR-2@1"]))
        writer_form(project.root)
        document = project.propose({"SR-1": []})
        document["requirements"][0]["linked"][0]["decision"] = "confirm"
        plan = project.plan(document)
        assert load_yaml_document(_text(plan, "model/swc/app/Ctl.yaml"))["satisfies"] == ["SR-3@1", "SR-1@2", "SR-2@1"]

    def test_a_confirm_removes_its_member_when_the_current_one_is_present(self, project: ApplyProject) -> None:
        project.edit("swc/app/Ctl.yaml", lambda d: d.update(satisfies=["SR-1@1", "SR-3@1", "SR-1@2"]))
        writer_form(project.root)
        document = project.propose({"SR-1": []})
        linked = document["requirements"][0]["linked"]
        assert [link["member"] for link in linked] == ["SR-1@1", "SR-1@2"]
        linked[0]["decision"] = "confirm"
        plan = project.plan(document)
        assert load_yaml_document(_text(plan, "model/swc/app/Ctl.yaml"))["satisfies"] == ["SR-3@1", "SR-1@2"]

    def test_a_confirm_of_a_member_listed_twice_is_not_written(self, project: ApplyProject) -> None:
        """Two copies of the member: a form Apply does not edit, reported and left for a hand edit."""
        project.edit("swc/app/Ctl.yaml", lambda d: d.update(satisfies=["SR-3@1", "SR-1@1", "SR-2@1", "SR-1@1"]))
        writer_form(project.root)
        document = project.propose({"SR-1": []})
        assert [link["member"] for link in document["requirements"][0]["linked"]] == ["SR-1@1"]
        document["requirements"][0]["linked"][0]["decision"] = "confirm"
        plan = project.plan(document)
        assert plan.model_files == () and plan.records == () and plan.marks == 0
        assert [item.outcome for item in plan.items] == [NOT_WRITTEN]
        assert plan.items[0].reason == (
            "the satisfies list of SWC 'Ctl' is in a form Apply does not edit ('SR-1@1' is listed more than once); "
            "replace 'SR-1@1' with 'SR-1@2' by hand"
        )
        report = render_report(plan, dry_run=True)
        assert "0 stale, 1 not written, 0 undecided" in report and "Links not written (edit by hand):\n" in report

    def test_two_confirms_of_one_target_write_one_member_and_one_record(self, project: ApplyProject) -> None:
        project.edit("swc/app/Ctl.yaml", lambda d: d.update(satisfies=["SR-1", "SR-3@1", "SR-1@1"]))
        writer_form(project.root)
        document = project.propose({"SR-1": []})
        linked = document["requirements"][0]["linked"]
        assert sorted(link["member"] for link in linked) == ["SR-1", "SR-1@1"]
        for link in linked:
            link["decision"] = "confirm"
        plan = project.plan(document)
        satisfies = load_yaml_document(_text(plan, "model/swc/app/Ctl.yaml"))["satisfies"]
        assert sorted(satisfies) == ["SR-1@2", "SR-3@1"]  # the second confirm finds the first one's member
        assert len(plan.confirms) == 2
        assert "Summary: 0 written, 2 confirmed, 0 rejected" in render_report(plan, dry_run=True)
        assert [(record.requirement, record.version, record.decision) for record in plan.records] == [
            ("SR-1", 2, "confirm")
        ]
        assert plan.decisions is not None
        assert len(parse_decisions(plan.decisions.text).decisions) == 1

    def test_one_link_in_two_linked_entries_is_refused_with_its_place(self, project: ApplyProject) -> None:
        document = project.propose({"SR-1": []})
        linked = document["requirements"][0]["linked"]
        linked[0]["decision"] = "confirm"
        linked.append(dict(linked[0]))
        project.write(document)
        with pytest.raises(TraceRecordsError) as caught:
            plan_apply(project.project, project.proposals, file="proposals.json")
        assert str(caught.value) == (
            "proposals.json: /requirements/0/linked/1/member: the link 'SR-1@1' on this target is in linked/0 "
            "already: one linked entry per target and member"
        )

    def test_a_confirm_whose_member_an_earlier_one_replaced_changes_nothing_more(
        self, project: ApplyProject, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Past the reader (its rule switched off), two confirms of one member still plan one replacement."""
        monkeypatch.setattr(trace_records, "_refuse_reader_only", lambda file, proposals: None)
        document = project.propose({"SR-1": []})
        linked = document["requirements"][0]["linked"]
        linked[0]["decision"] = "confirm"
        linked.append(dict(linked[0]))
        plan = project.plan(document)
        assert load_yaml_document(_text(plan, "model/swc/app/Ctl.yaml"))["satisfies"] == ["SR-1@2"]
        assert [(record.requirement, record.version) for record in plan.records] == [("SR-1", 2)]

    def test_the_file_is_the_one_that_declares_the_element(self, tmp_path: Path) -> None:
        model = apply_model()
        model.composition("Side")
        project = ApplyProject(tmp_path, model)
        (project.root / "swc" / "app" / "Ctl.yaml").rename(project.root / "swc" / "app" / "Controller.yaml")
        (project.root / "compositions" / "Side.yaml").rename(project.root / "compositions" / "Side~1.yaml")
        plan = project.plan(
            project.propose(
                {
                    "SR-2": [(OUT, "accept"), (STEP, "accept")],
                    "SR-3": [(TargetAddress("composition", None, "Side"), "accept")],
                }
            )
        )
        assert [planned.target for planned in plan.model_files] == [
            "model/swc/app/Controller.yaml",
            "model/compositions/Side~1.yaml",
        ]
        component = load_yaml_document(_text(plan, "model/swc/app/Controller.yaml"))
        assert component["ports"][0]["satisfies"] == ["SR-2@1"]
        assert component["runnables"][0]["satisfies"] == ["SR-2@1"]
        assert plan.not_written == []

    def test_a_hand_formatted_file_keeps_every_byte_but_the_inserted_lines(self, tmp_path: Path) -> None:
        """Only the satisfies lines are added: every other byte of a hand-formatted file stays."""
        project = ApplyProject(tmp_path, apply_model())
        path = project.root / "compositions" / "Top.yaml"
        prior = "# the top composition\ntitle: Top\nname: Top\ndescription: the top composition\n"
        path.write_text(prior, encoding="utf-8", newline="\n")
        plan = project.plan(project.propose({"SR-2": [(TOP, "accept")]}))
        assert _text(plan, "model/compositions/Top.yaml") == prior + "satisfies:\n- SR-2@1\n"
        assert "writer's form" not in render_report(plan, dry_run=True)

    def test_a_new_key_left_of_a_first_key_behind_a_mark_refuses_the_run_and_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        """The one marked form that edits to no document: the read-back stands in front of the file."""
        project = ApplyProject(tmp_path, apply_model())
        path = project.root / "compositions" / "Top.yaml"
        path.write_text(
            "\n\ufefftitle: Top\n name: Top\n description: the top composition\n", encoding="utf-8", newline="\n"
        )
        project.write(project.propose({"SR-2": [(TOP, "accept")]}))
        before = tree(project.dir)
        with pytest.raises(TraceApplyError, match="would not read back as planned"):
            run_apply(project.project, project.proposals, dry_run=False)
        assert tree(project.dir) == before


# ---------------------------------------------------------------------------
# The decisions records and the marks
# ---------------------------------------------------------------------------


class TestRecordsAndMarks:
    def test_each_planned_decision_is_recorded_with_how_it_was_made(self, project: ApplyProject) -> None:
        document = project.propose({"SR-1": [], "SR-2": [(OUT, "accept"), (TOP, "reject")]})
        document["requirements"][0]["linked"][0]["decision"] = "confirm"
        document["requirements"][1]["candidates"][0]["preselected"] = True
        plan = project.plan(document)
        assert plan.decisions is not None and plan.decisions.target == DECISIONS_FILE
        records = [record.to_document() for record in parse_decisions(plan.decisions.text).decisions]
        assert records == [
            {"requirement": "SR-1", "version": 2, "target": CTL.to_json(), "decision": "confirm", "from": "SR-1@1"},
            {"requirement": "SR-2", "version": 1, "target": OUT.to_json(), "decision": "accept", "preselected": True},
            {"requirement": "SR-2", "version": 1, "target": TOP.to_json(), "decision": "reject"},
        ]

    def test_a_reject_on_a_changed_requirement_is_not_recorded(self, project: ApplyProject) -> None:
        document = project.propose({"SR-2": [(TOP, "reject")]})
        project.edit("requirements/SR-2.yaml", lambda d: d.update(version=2, title="Locking"))
        plan = project.plan(document)
        assert _one(plan).outcome == STALE
        assert plan.records == () and plan.decisions is None

    def test_an_accept_found_present_is_marked_applied_and_recorded_nowhere(self, project: ApplyProject) -> None:
        document = project.propose({"SR-2": [(TOP, "accept")]})
        project.edit("compositions/Top.yaml", lambda d: d.update(satisfies=["SR-2@1"]))
        plan = project.plan(document)
        assert _one(plan).outcome == ALREADY_DONE
        assert plan.records == () and plan.decisions is None and plan.model_files == ()
        assert plan.proposals is not None
        marked = json.loads(plan.proposals.text)["requirements"][0]["candidates"][0]
        assert marked["applied"] is True

    def test_a_record_already_held_is_not_written_again(self, project: ApplyProject) -> None:
        document = project.propose({"SR-2": [(TOP, "reject")]})
        plan = project.plan(document)
        assert plan.decisions is not None
        (project.dir / DECISIONS_FILE).write_text(plan.decisions.text, encoding="utf-8", newline="\n")
        again = project.plan(document)
        assert again.records == () and again.decisions is None
        assert again.proposals is not None  # the mark is still this run's

    def test_stale_items_carry_their_reason_and_others_are_untouched(self, project: ApplyProject) -> None:
        document = project.propose({"SR-2": [(OUT, "accept")], "SR-3": [(TOP, None)]})
        project.edit("swc/app/Ctl.yaml", lambda d: d.update(ports=[]))
        plan = project.plan(document)
        assert plan.proposals is not None
        marked = json.loads(plan.proposals.text)
        assert (
            marked["requirements"][0]["candidates"][0]["stale"] == "port 'pOut' of SWC 'Ctl' is no longer in the model"
        )
        assert "stale" not in marked["requirements"][1]["candidates"][0]
        assert "applied" not in marked["requirements"][1]["candidates"][0]
        assert plan.undecided == 1
        parse_proposals(plan.proposals.text.encode("utf-8"), "proposals.json")  # still the format

    def test_a_confirmed_link_is_not_undecided(self, project: ApplyProject) -> None:
        project.edit("compositions/Top.yaml", lambda d: d.update(satisfies=["SR-2@1", "SR-3@1"]))
        document = project.propose({"SR-1": [], "SR-2": [(OUT, None)], "SR-3": []})
        confirmed = [link for entry in document["requirements"] for link in entry["linked"] if link["confirmed"]]
        assert len(confirmed) == 2
        plan = project.plan(document)
        assert plan.undecided == 2  # SR-1's link awaiting confirmation and SR-2's row; never a confirmed link
        assert "0 stale, 0 not written, 2 undecided; 0 files" in render_report(plan, dry_run=True)

    def test_a_stale_item_is_skipped_and_not_counted(self, project: ApplyProject) -> None:
        document = project.propose({"SR-2": [(TOP, "accept")]})
        document["requirements"][0]["candidates"][0]["stale"] = "an earlier reason"
        plan = project.plan(document)
        assert plan.items == () and plan.changes_nothing


# ---------------------------------------------------------------------------
# A rejection the decisions file already holds
# ---------------------------------------------------------------------------


def _hold(project: ApplyProject, *records: dict[str, Any]) -> bytes:
    """Write the decisions file holding *records*; return its bytes, which a plan must leave alone."""
    text = trace_records.decisions_text(
        trace_records.Decisions.model_validate({"formatVersion": 1, "decisions": list(records)})
    )
    (project.dir / DECISIONS_FILE).write_text(text, encoding="utf-8", newline="\n")
    return (project.dir / DECISIONS_FILE).read_bytes()


def _record(version: int, target: TargetAddress, decision: str) -> dict[str, Any]:
    return {"requirement": "SR-2", "version": version, "target": target.to_json(), "decision": decision}


class TestARecordedRejection:
    def test_a_reject_held_at_the_same_version_is_already_rejected(self, project: ApplyProject) -> None:
        held = _hold(project, _record(1, TOP, "reject"))
        plan = project.plan(project.propose({"SR-2": [(TOP, "reject")]}))
        item = _one(plan)
        assert (item.outcome, item.reason) == (ALREADY_DONE, "already rejected")
        assert plan.records == () and plan.decisions is None and plan.model_files == ()
        assert plan.proposals is not None
        assert json.loads(plan.proposals.text)["requirements"][0]["candidates"][0]["applied"] is True
        assert (project.dir / DECISIONS_FILE).read_bytes() == held
        assert "0 rejected, 1 already done" in render_report(plan, dry_run=True)
        assert "  SR-2 on Composition 'Top': already rejected\n" in render_report(plan, dry_run=True)
        # The digest is the one a planned reject gives: the same mark, no record, no file.
        as_planned = [dataclasses.replace(item, outcome=PLANNED, reason=None)]
        assert plan.digest == plan_digest(plan.model_files, plan.records, as_planned)

    def test_a_reject_held_at_another_version_is_rejected_and_recorded(self, project: ApplyProject) -> None:
        _hold(project, _record(2, TOP, "reject"))
        plan = project.plan(project.propose({"SR-2": [(TOP, "reject")]}))
        assert (_one(plan).outcome, _one(plan).reason) == (PLANNED, None)
        assert [record.to_document() for record in plan.records] == [_record(1, TOP, "reject")]

    def test_the_models_version_keys_the_lookup_never_the_entrys(self, project: ApplyProject) -> None:
        document = project.propose({"SR-2": [(TOP, "reject")]})
        document["requirements"][0]["version"] = 2  # the fingerprint still matches the model's SR-2@1
        _hold(project, _record(2, TOP, "reject"))
        plan = project.plan(document)
        assert (_one(plan).outcome, _one(plan).reason) == (PLANNED, None)
        assert [record.to_document() for record in plan.records] == [_record(1, TOP, "reject")]

    def test_a_held_accept_is_no_rejection(self, project: ApplyProject) -> None:
        _hold(project, _record(1, TOP, "accept"))
        plan = project.plan(project.propose({"SR-2": [(TOP, "reject")]}))
        assert (_one(plan).outcome, _one(plan).reason) == (PLANNED, None)

    def test_a_reject_of_a_pair_linked_since_the_search_stays_already_linked(self, project: ApplyProject) -> None:
        _hold(project, _record(1, OUT, "reject"))
        document = project.propose({"SR-2": [(OUT, "reject")]})
        project.edit("swc/app/Ctl.yaml", lambda d: d["ports"][0].update(satisfies=["SR-2@1"]))
        item = _one(project.plan(document))
        assert (item.outcome, item.reason) == (ALREADY_DONE, "already linked as SR-2@1")

    def test_a_second_proposals_file_finds_the_first_ones_rejection_recorded(self, project: ApplyProject) -> None:
        """The showcase's shape in small: the second file carries a reject the first already recorded."""
        project.write(project.propose({"SR-2": [(TOP, "reject")]}))
        run_apply(project.project, project.proposals, dry_run=False)
        second = write_proposals(
            project.dir / "dist" / "experiments" / "trace2" / "proposals.json",
            project.propose({"SR-2": [(TOP, "reject"), (OUT, "accept")]}),
        )
        held = (project.dir / DECISIONS_FILE).read_bytes()
        plan = plan_apply(project.project, second)
        assert [(item.outcome, item.reason) for item in plan.items] == [
            (ALREADY_DONE, "already rejected"),
            (PLANNED, None),
        ]
        assert "Summary: 1 written, 0 confirmed, 0 rejected, 1 already done" in render_report(plan, dry_run=True)
        assert [record.to_document() for record in plan.records] == [_record(1, OUT, "accept")]
        as_planned = [dataclasses.replace(item, outcome=PLANNED, reason=None) for item in plan.items]
        assert plan.digest == plan_digest(plan.model_files, plan.records, as_planned)
        assert (project.dir / DECISIONS_FILE).read_bytes() == held


# ---------------------------------------------------------------------------
# Allocated levels raised
# ---------------------------------------------------------------------------


def test_a_port_link_raising_a_components_allocated_level_is_reported(tmp_path: Path) -> None:
    model = apply_model()
    model.get("requirements/SR-1.yaml")["asil"] = "B"
    model.get("requirements/SR-2.yaml")["asil"] = "D"
    model.get("swc/app/Ctl.yaml")["asil"] = "C"
    project = ApplyProject(tmp_path, model)
    plan = project.plan(project.propose({"SR-2": [(OUT, "accept")], "SR-3": [(TOP, "accept")]}))
    assert [(raised.component, raised.before, raised.after, raised.declared) for raised in plan.raised_levels] == [
        ("Ctl", "B", "D", "C")
    ]
    assert "Allocated levels raised:\n  SWC 'Ctl': allocated level B → D (it declares C)\n" in render_report(
        plan, dry_run=True
    )


def test_a_link_that_raises_no_level_is_not_reported(tmp_path: Path) -> None:
    model = apply_model()
    model.get("requirements/SR-1.yaml")["asil"] = "D"
    model.get("requirements/SR-2.yaml")["asil"] = "B"
    project = ApplyProject(tmp_path, model)
    plan = project.plan(project.propose({"SR-2": [(OUT, "accept")]}))
    assert plan.raised_levels == ()


# ---------------------------------------------------------------------------
# The report, the digest and the JSON form
# ---------------------------------------------------------------------------


class TestReport:
    def test_the_report_names_each_section_in_its_order(self, project: ApplyProject) -> None:
        document = project.propose(
            {"SR-1": [(TOP, "accept")], "SR-2": [(OUT, "reject"), (STEP, "accept")], "SR-3": [(CTL, "accept")]}
        )
        document["requirements"][0]["linked"][0]["decision"] = "confirm"
        project.edit("swc/app/Ctl.yaml", lambda d: d["runnables"][0].update(satisfies=["SR-2"]))
        project.edit("requirements/SR-3.yaml", lambda d: d.update(title="Lamp states"))
        report = render_report(project.plan(document), dry_run=True)
        assert report == (
            "Trace apply: proposals of the search 'trace' (the model has changed since the search)\n"
            "Dry run: nothing was written.\n"
            "Summary: 1 written, 1 confirmed, 1 rejected, 1 already done, 1 stale, 0 not written, 0 undecided; "
            "2 files\n"
            f"Plan: {project.plan(document).digest}\n"
            "\n"
            "Links to write:\n"
            "  Composition 'Top' satisfies 'SR-1@2'\n"
            "\n"
            "Links to confirm:\n"
            "  SWC 'Ctl' satisfies 'SR-1@1' → 'SR-1@2'\n"
            "\n"
            "Rejections to record:\n"
            "  SR-2@1 rejected for SWC 'Ctl' (port 'pOut')\n"
            "\n"
            "Already done:\n"
            "  SR-2 on SWC 'Ctl' (runnable 'Step'): already linked as SR-2\n"
            "\n"
            "Stale:\n"
            "  SR-3 on SWC 'Ctl': SR-3 changed since the search (same version); search again\n"
        )

    def test_the_header_says_the_model_changed_exactly_when_the_digest_differs(self, project: ApplyProject) -> None:
        document = project.propose({"SR-2": [(TOP, None)]})
        assert project.plan(document).model_changed
        document["model"]["digest"] = experiment_frame.model_digest(project.root)[0]
        plan = project.plan(document)
        assert not plan.model_changed
        assert render_report(plan, dry_run=False).splitlines()[0] == "Trace apply: proposals of the search 'trace'"

    def test_an_applied_run_prints_no_dry_run_line(self, project: ApplyProject) -> None:
        report = render_report(project.plan(project.propose({"SR-2": [(TOP, "accept")]})), dry_run=False)
        assert "Dry run" not in report

    def test_nothing_decided_prints_every_count_zero_and_no_change(self, project: ApplyProject) -> None:
        plan = project.plan(project.propose({"SR-2": []}))
        lines = render_report(plan, dry_run=True).splitlines()
        assert lines[2:4] == [
            "Summary: 0 written, 0 confirmed, 0 rejected, 0 already done, 0 stale, 0 not written, 0 undecided; 0 files",
            "No change.",
        ]
        assert plan.files() == ()

    def test_r6_marks_alone_are_a_change_and_once_written_there_is_none(self, project: ApplyProject) -> None:
        """A file whose only decided items are links already present: the marks are the change."""
        document = project.propose({"SR-2": [(TOP, "accept")], "SR-3": [(OUT, "accept")]})
        project.edit("compositions/Top.yaml", lambda d: d.update(satisfies=["SR-2@1"]))
        project.edit("swc/app/Ctl.yaml", lambda d: d["ports"][0].update(satisfies=["SR-3@1"]))
        plan = project.plan(document)
        report = render_report(plan, dry_run=True)
        assert (
            "Summary: 0 written, 0 confirmed, 0 rejected, 2 already done, 0 stale, 0 not written, 0 undecided; 0 files"
            in report
        )
        assert "No change." not in report
        assert plan.model_files == () and plan.decisions is None and plan.proposals is not None
        applied = run_apply(project.project, project.proposals, dry_run=False)
        assert applied.written == ("dist/experiments/trace/proposals.json",)  # the marks, and nothing else
        after = project.plan()
        report = render_report(after, dry_run=True)
        assert "No change." in report
        assert [item.reason for item in after.items] == [APPLIED_EARLIER, APPLIED_EARLIER]
        assert after.files() == ()

    def test_the_digest_is_stable_and_moves_with_one_decision(self, project: ApplyProject) -> None:
        document = project.propose({"SR-2": [(TOP, "accept"), (OUT, "reject")]})
        first, second = project.plan(document).digest, project.plan(document).digest
        assert first == second and first.startswith("sha256:")
        document["requirements"][0]["candidates"][1]["decision"] = "accept"
        assert project.plan(document).digest != first

    def test_the_json_form_carries_every_section_and_no_path(self, project: ApplyProject) -> None:
        document = project.propose({"SR-2": [(OUT, "accept")]})
        plan = project.plan(document)
        report = report_document(plan, dry_run=True)
        assert report["schemaVersion"] == 1 and report["kind"] == "trace-apply" and report["dryRun"] is True
        assert report["plan"] == plan.digest
        assert report["summary"] == {
            "written": 1,
            "confirmed": 0,
            "rejected": 0,
            "alreadyDone": 0,
            "stale": 0,
            "notWritten": 0,
            "undecided": 0,
            "files": 1,
            "noChange": False,
        }
        assert report["write"] == [{"requirement": "SR-2", "version": 1, "target": OUT.to_json(), "member": "SR-2@1"}]
        assert set(report) == {
            "schemaVersion", "kind", "dryRun", "plan", "summary", "write", "confirm", "reject", "alreadyDone",
            "stale", "notWritten", "raisedLevels",
        }  # fmt: skip

    def test_the_json_form_carries_each_sections_items(self, tmp_path: Path) -> None:
        model = apply_model()
        model.get("requirements/SR-2.yaml")["asil"] = "B"
        project = ApplyProject(tmp_path, model)
        document = project.propose(
            {"SR-1": [], "SR-2": [(STEP, "accept"), (OUT, "reject"), (TOP, "accept")], "SR-3": [(CTL, "accept")]}
        )
        document["requirements"][0]["linked"][0]["decision"] = "confirm"
        document["requirements"][1]["candidates"][0]["preselected"] = True
        project.edit("compositions/Top.yaml", lambda d: d.update(satisfies=["SR-2"]))
        project.edit("requirements/SR-3.yaml", lambda d: d.update(title="Lamp states"))
        report = report_document(project.plan(document), dry_run=False)
        assert report["dryRun"] is False
        assert report["write"] == [
            {"requirement": "SR-2", "version": 1, "target": STEP.to_json(), "member": "SR-2@1", "preselected": True}
        ]
        assert report["confirm"] == [
            {"requirement": "SR-1", "version": 2, "target": CTL.to_json(), "from": "SR-1@1", "member": "SR-1@2"}
        ]
        assert report["reject"] == [{"requirement": "SR-2", "version": 1, "target": OUT.to_json()}]
        assert report["alreadyDone"] == [
            {"requirement": "SR-2", "version": 1, "target": TOP.to_json(), "reason": "already linked as SR-2"}
        ]
        assert report["stale"] == [
            {
                "requirement": "SR-3",
                "version": 1,
                "target": CTL.to_json(),
                "reason": "SR-3 changed since the search (same version)",
            }
        ]
        assert report["raisedLevels"] == [{"component": "Ctl", "before": "QM", "after": "B", "declared": None}]
        assert report["notWritten"] == []

    def test_a_raised_level_of_a_component_declaring_none_names_no_declaration(self, tmp_path: Path) -> None:
        model = apply_model()
        model.get("requirements/SR-2.yaml")["asil"] = "C"
        project = ApplyProject(tmp_path, model)
        plan = project.plan(project.propose({"SR-2": [(OUT, "accept")]}))
        assert [(raised.before, raised.after, raised.declared) for raised in plan.raised_levels] == [("QM", "C", None)]
        assert "Allocated levels raised:\n  SWC 'Ctl': allocated level QM → C\n" in render_report(plan, dry_run=True)

    def test_the_json_form_lists_a_link_not_written(self, tmp_path: Path) -> None:
        project = ApplyProject(tmp_path, apply_model())
        (project.root / "compositions" / "Top.yaml").write_text(
            "title: Top\nname: Top\nsatisfies: [\n  SR-9@1]\n", encoding="utf-8", newline="\n"
        )
        report = report_document(project.plan(project.propose({"SR-2": [(TOP, "accept")]})), dry_run=True)
        assert report["summary"]["notWritten"] == 1 and report["write"] == []
        assert report["notWritten"] == [
            {
                "requirement": "SR-2",
                "version": 1,
                "target": TOP.to_json(),
                "member": "SR-2@1",
                "reason": "the satisfies list of Composition 'Top' is in a form Apply does not edit "
                "(a flow list over several lines); add 'SR-2@1' by hand",
            }
        ]

    def test_no_storage_path_is_printed(self, tmp_path: Path) -> None:
        project = ApplyProject(tmp_path, apply_model(), in_writer_form=False)
        document = project.propose({"SR-2": [(OUT, "accept"), (TOP, "reject")], "SR-3": [(STEP, "accept")]})
        project.edit("swc/app/Ctl.yaml", lambda d: d["runnables"][0].update(description="moved"))
        plan = project.plan(document)
        texts = [render_report(plan, dry_run=True), json.dumps(report_document(plan, dry_run=True))]
        for text in texts:
            assert str(tmp_path) not in text
            for storage in ("model/", "swc/app", ".yaml", "compositions/", DECISIONS_FILE):
                assert storage not in text

    def test_a_control_character_is_printed_escaped(self, project: ApplyProject) -> None:
        document = project.propose({"SR-2": []}, chosen={"SR-2": [TargetAddress("composition", None, "Gh\x1bost")]})
        document["name"] = "trace\nSummary: 9 written"
        report = render_report(project.plan(document), dry_run=True)
        assert "proposals of the search 'trace\\nSummary: 9 written'" in report
        assert "Composition 'Gh\\x1bost' is no longer in the model" in report
        assert report.count("\nSummary:") == 1


def test_a_file_outside_the_project_is_refused_before_it_is_read(
    project: ApplyProject, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "elsewhere" / "proposals.json"
    outside.parent.mkdir()
    outside.write_bytes(project.proposals.read_bytes() if project.proposals.exists() else b"{}")
    real_read_bytes = Path.read_bytes
    read: list[Path] = []

    def read_bytes(self: Path) -> bytes:
        read.append(self)
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    with pytest.raises(TraceApplyError, match="lies outside the project"):
        plan_apply(project.project, outside, file="elsewhere/proposals.json")
    assert outside not in read and outside.resolve() not in read
