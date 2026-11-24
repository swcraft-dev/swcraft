"""The change report's facts and its two documents, against hand-computed expectations.

The fixture is a temporary git repository holding a copy of ``examples/basic``
with three additions (an ``init`` on ``IExample.value``, a second requirement
satisfied by ``ExampleSWC``'s port ``pOut`` and a third requirement nothing
satisfies), committed; then the working tree is edited:

- ``MonitorSWC`` added (a port on ``IExample``, a runnable, 1024 bytes of
  ``RAM0``, a bare link to ``REQ_EXAMPLE_002``), a member of ``Root``, fed by the
  new connector ``Example_to_Monitor``;
- ``Example_to_Consumer`` renamed ``Example_feeds_Consumer`` (same endpoints);
- ``IExample.value``'s ``init`` 0 → 5;
- ``Task_10ms``'s ``wcet`` 1200us → 1500us, and it runs ``MonitorSWC.MonitorRun``;
- ``REQ_EXAMPLE_001`` bumped to version 2, re-confirmed by
  ``ExampleConsumerSWC`` (``@1`` → ``@2``) and left at ``@1`` by ``ExampleSWC``;
- ``REQ_EXAMPLE_002``'s link moved from ``ExampleSWC``'s port ``pOut`` to its
  runnable ``ExampleRun``.

Every expectation below is computed by hand from that edit: the rows, the
counts (13 changes in 10 elements), the two introduced warnings (the
unallocated ``REQ_EXAMPLE_003`` is a warning at both revisions and stays out),
the core's 12 % → 15 % (1.2 ms then 1.5 ms of a 10 ms period), the memory's
3584 → 4608 bytes, the ARXML elements the writer names for an added component
and a changed constant and composition.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from node_support import requires_node
from scratch_git import git as _git
from swcraft_tools import change_report as change_report_module
from swcraft_tools.change_report import (
    ArxmlElementChange,
    ChangedElementRow,
    ChangeReport,
    ConsumerGroup,
    Dependent,
    RequirementRow,
    ResourceRow,
    RevisionParseError,
    _key_text,
    compute_change_report,
    introduced_findings,
    revisions_from_arguments,
)
from swcraft_tools.change_report_render import (
    is_report_file,
    render_json,
    render_markdown,
    report_files,
)
from swcraft_tools.differ import (
    ChangedItem,
    LayerDiff,
    ModelDiff,
    ModelStateDiff,
    _render_key,
    collection_rule,
    format_diff_json,
)
from swcraft_tools.model_state import RevisionReadError, git_environment
from swcraft_tools.project import require_project
from swcraft_tools.schema import SWComponent, Task
from swcraft_tools.snapshot import SnapshotReadError
from swcraft_tools.validator import ConsistencyIssue

REPO_ROOT = Path(__file__).resolve().parents[3]

WARN_NEEDS_CONFIRMATION = (
    "SWC 'ExampleSWC' satisfies 'REQ_EXAMPLE_001@1', but requirement 'REQ_EXAMPLE_001' is at version 2: confirm "
    "the link against version 2 and write 'REQ_EXAMPLE_001@2'."
)
WARN_NOT_STATED = (
    "SWC 'MonitorSWC' satisfies 'REQ_EXAMPLE_002' without a version: requirement 'REQ_EXAMPLE_002' is at version "
    "1; write 'REQ_EXAMPLE_002@1' once the link is confirmed against it."
)


# The fixture


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path.name}"
    _write(path, text.replace(old, new))


def _copy_basic(directory: Path) -> Path:
    project = directory / "project"
    shutil.copytree(REPO_ROOT / "examples" / "basic", project, ignore=shutil.ignore_patterns("dist", "views"))
    return project


def _commit(project: Path) -> None:
    _git("init", "-q", cwd=project)
    _git("add", "-A", cwd=project)
    _git("commit", "-q", "-m", "before", cwd=project)


IEXAMPLE = "name: IExample\nkind: sender_receiver\ndata_elements:\n  - name: value\n    implementation_type: float32\n"
PORT_DESCRIPTION = "    description: Example output port providing data\n"
RUN_EVENTS = "      - type: timing\n        period: 0.01\n"


def _requirement(identifier: str, description: str) -> str:
    return (
        f"id: {identifier}\ntitle: {identifier}\ndescription: {description}\nreq_type: req\nversion: 1\n"
        "source: architecture\n"
    )


def build_project(directory: Path) -> Path:
    """The committed fixture with the working-tree edit of the module docstring; returns the manifest."""
    project = _copy_basic(directory)
    model = project / "model"
    _write(model / "interfaces" / "IExample.yaml", IEXAMPLE + "    init: 0\n")
    _write(model / "requirements" / "REQ_EXAMPLE_002.yaml", _requirement("REQ_EXAMPLE_002", "The second."))
    _write(model / "requirements" / "REQ_EXAMPLE_003.yaml", _requirement("REQ_EXAMPLE_003", "Nothing satisfies it."))
    example = model / "swc" / "example" / "ExampleSWC.yaml"
    _replace(example, PORT_DESCRIPTION, PORT_DESCRIPTION + "    satisfies:\n    - REQ_EXAMPLE_002@1\n")
    _commit(project)

    _write(model / "interfaces" / "IExample.yaml", IEXAMPLE + "    init: 5\n")
    _replace(example, "    satisfies:\n    - REQ_EXAMPLE_002@1\n", "")
    _replace(example, RUN_EVENTS, RUN_EVENTS + "    satisfies:\n    - REQ_EXAMPLE_002@1\n")
    _replace(model / "requirements" / "REQ_EXAMPLE_001.yaml", "version: 1", "version: 2")
    _replace(model / "swc" / "example" / "ExampleConsumerSWC.yaml", "REQ_EXAMPLE_001@1", "REQ_EXAMPLE_001@2")
    _write(
        model / "swc" / "example" / "MonitorSWC.yaml",
        "name: MonitorSWC\ntype: application\nports:\n  - name: rMon\n    direction: require\n"
        "    interface: IExample\nrunnables:\n  - name: MonitorRun\n    events:\n" + RUN_EVENTS + "memory:\n"
        "  RAM0: 1024\nhandle_termination_and_restart: no_support\nsatisfies:\n- REQ_EXAMPLE_002\n",
    )
    root = model / "compositions" / "Root.yaml"
    _write(root, root.read_text(encoding="utf-8") + "  - name: MonitorSWC\n    type: MonitorSWC\n")
    _write(
        model / "connectors" / "Example_to_Monitor.yaml",
        "name: Example_to_Monitor\nfrom:\n  swc: ExampleSWC\n  port: pOut\nto:\n  swc: MonitorSWC\n  port: rMon\n",
    )
    old_connector = model / "connectors" / "Example_to_Consumer.yaml"
    _write(
        model / "connectors" / "Example_feeds_Consumer.yaml",
        old_connector.read_text(encoding="utf-8").replace("name: Example_to_Consumer", "name: Example_feeds_Consumer"),
    )
    old_connector.unlink()
    task = model / "tasks" / "Task_10ms.yaml"
    _replace(task, "wcet: 1200us", "wcet: 1500us")
    _write(task, task.read_text(encoding="utf-8") + "- swc: MonitorSWC\n  runnable: MonitorRun\n")
    return project / "swcraft.yaml"


def _short_sha(project: Path, revision: str = "HEAD") -> str:
    return _git("rev-parse", "--short", revision, cwd=project).stdout.strip()


@pytest.fixture(scope="module")
def fixture_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_project(tmp_path_factory.mktemp("change-report"))


@pytest.fixture(scope="module")
def report(fixture_project: Path) -> ChangeReport:
    return compute_change_report(require_project(str(fixture_project)), "HEAD", None, diagrams=False)


@pytest.fixture(scope="module")
def head_label(fixture_project: Path) -> str:
    return f"HEAD ({_short_sha(fixture_project.parent)})"


# The revisions, What changed, the verdict, the requirements


def test_the_arguments_follow_swcraft_diff() -> None:
    assert revisions_from_arguments(None, None) == ("HEAD", None)
    assert revisions_from_arguments("HEAD~1", None) == ("HEAD~1", None)
    assert revisions_from_arguments("v1", "v2") == ("v1", "v2")


def test_the_labels_name_the_revision_with_its_short_sha_and_the_working_tree(
    report: ChangeReport, head_label: str
) -> None:
    assert (report.project_name, report.old_label, report.new_label) == (
        "swcraft-basic-example",
        head_label,
        "working tree",
    )


def test_what_changed_lists_every_element_in_display_order(report: ChangeReport) -> None:
    assert report.changed_elements == (
        ChangedElementRow("requirements", "Requirements", "REQ_EXAMPLE_001", "changed", ("version: 1 → 2",)),
        ChangedElementRow("compositions", "Compositions", "Root", "changed", ("members: + MonitorSWC",)),
        ChangedElementRow("swc", "SWCs", "MonitorSWC", "added"),
        ChangedElementRow(
            "swc",
            "SWCs",
            "ExampleConsumerSWC",
            "changed",
            ("satisfies: + REQ_EXAMPLE_001@2", "satisfies: − REQ_EXAMPLE_001@1"),
        ),
        ChangedElementRow(
            "swc",
            "SWCs",
            "ExampleSWC",
            "changed",
            ("ports.pOut.satisfies: − REQ_EXAMPLE_002@1", "runnables.ExampleRun.satisfies: + REQ_EXAMPLE_002@1"),
        ),
        ChangedElementRow("interfaces", "Interfaces", "IExample", "changed", ("data_elements.value.init: 0 → 5",)),
        ChangedElementRow(
            "connectors", "Connectors", "Example_feeds_Consumer", "added", rename_of="Example_to_Consumer"
        ),
        ChangedElementRow("connectors", "Connectors", "Example_to_Monitor", "added"),
        ChangedElementRow("connectors", "Connectors", "Example_to_Consumer", "removed"),
        ChangedElementRow(
            "tasks",
            "Tasks",
            "Task_10ms",
            "changed",
            ("wcet: '1200us' → '1500us'", "runnables: + MonitorSWC.MonitorRun"),
        ),
    )


def test_the_verdict_counts(report: ChangeReport) -> None:
    """K = 13; E = 10 rows; C = Root, the one composition holding a change.

    K = 1 (version) + 1 (member) + 1 (MonitorSWC) + 2 + 2 (the satisfies items) + 1 (init) + 3 (connectors)
    + 2 (wcet, runnable entry).
    """
    assert (report.introduced_errors, report.introduced_warnings) == (0, 2)
    assert (report.change_count, report.element_count, report.compositions) == (13, 10, ("Root",))


def test_the_requirement_rows(report: ChangeReport) -> None:
    assert report.requirement_rows == (
        RequirementRow("REQ_EXAMPLE_001", "SWC 'ExampleConsumerSWC'", "re-confirmed", "1", "2"),
        RequirementRow("REQ_EXAMPLE_001", None, "needs-confirmation", messages=(WARN_NEEDS_CONFIRMATION,)),
        RequirementRow("REQ_EXAMPLE_002", "SWC 'ExampleSWC' (runnable 'ExampleRun')", "gained", None, "1"),
        RequirementRow("REQ_EXAMPLE_002", "SWC 'MonitorSWC'", "gained", None, None),
        RequirementRow("REQ_EXAMPLE_002", "SWC 'ExampleSWC' (port 'pOut')", "lost", "1", None),
        RequirementRow("REQ_EXAMPLE_002", None, "needs-confirmation", messages=(WARN_NOT_STATED,)),
    )


def test_a_derived_from_finding_stays_out_of_the_requirement_rows(tmp_path: Path) -> None:
    """The two trace rules also report a requirement's ``derived_from``; that finding is a finding only."""
    project = _copy_basic(tmp_path)
    requirements = project / "model" / "requirements"
    _write(
        requirements / "REQ_CHILD.yaml", _requirement("REQ_CHILD", "Derived.") + "derived_from:\n- REQ_EXAMPLE_001@1\n"
    )
    _commit(project)
    _replace(requirements / "REQ_EXAMPLE_001.yaml", "version: 1", "version: 2")
    _replace(project / "model" / "swc" / "example" / "ExampleSWC.yaml", "REQ_EXAMPLE_001@1", "REQ_EXAMPLE_001@2")
    _replace(
        project / "model" / "swc" / "example" / "ExampleConsumerSWC.yaml", "REQ_EXAMPLE_001@1", "REQ_EXAMPLE_001@2"
    )
    result = compute_change_report(require_project(str(project / "swcraft.yaml")), "HEAD", diagrams=False)
    assert [finding.message for finding in result.findings] == [
        "Requirement 'REQ_CHILD' derives from 'REQ_EXAMPLE_001@1', but requirement 'REQ_EXAMPLE_001' is at version 2:"
        " confirm the link against version 2 and write 'REQ_EXAMPLE_001@2'."
    ]
    assert [row.change for row in result.requirement_rows] == ["re-confirmed", "re-confirmed"]


@pytest.fixture(scope="module")
def unchanged_report(tmp_path_factory: pytest.TempPathFactory) -> ChangeReport:
    project = _copy_basic(tmp_path_factory.mktemp("unchanged"))
    _commit(project)
    return compute_change_report(require_project(str(project / "swcraft.yaml")), "HEAD", diagrams=False)


def test_no_change_is_an_empty_report(unchanged_report: ChangeReport) -> None:
    result = unchanged_report
    assert result.is_empty and result.diff.is_empty
    assert (result.change_count, result.findings, result.requirement_rows, result.consumers) == (0, (), (), ())
    assert result.arxml.compared and result.arxml.elements == ()
    assert result.compositions == ()


def test_two_revisions_are_both_materialised(fixture_project: Path, tmp_path: Path) -> None:
    project = _copy_basic(tmp_path)
    _commit(project)
    _replace(project / "model" / "tasks" / "Task_10ms.yaml", "priority: 5", "priority: 6")
    _git("commit", "-q", "-am", "after", cwd=project)
    result = compute_change_report(require_project(str(project / "swcraft.yaml")), "HEAD~1", "HEAD", diagrams=False)
    assert (result.old_label, result.new_label) == (
        f"HEAD~1 ({_short_sha(project, 'HEAD~1')})",
        f"HEAD ({_short_sha(project)})",
    )
    assert [row.leaves for row in result.changed_elements] == [("priority: 5 → 6",)]


@pytest.mark.parametrize("revision", ["no-such-revision", "--output=x"])
def test_a_revision_that_cannot_be_resolved_is_refused(fixture_project: Path, revision: str) -> None:
    with pytest.raises(ValueError):
        compute_change_report(require_project(str(fixture_project)), revision, diagrams=False)


def test_a_revision_that_names_no_commit_keeps_git_text_for_the_terminal_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A tree is archived as a commit is, so the short id's `^{commit}` is the first git call to refuse it.
    project = _copy_basic(tmp_path)
    _commit(project)
    environments: list[dict[str, str] | None] = []
    run = change_report_module.subprocess.run

    def recording(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        environments.append(kwargs.get("env"))  # type: ignore[arg-type]
        return run(*args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(change_report_module.subprocess, "run", recording)
    with pytest.raises(RevisionReadError) as caught:
        compute_change_report(require_project(str(project / "swcraft.yaml")), "HEAD^{tree}", diagrams=False)
    assert str(caught.value) == "revision 'HEAD^{tree}' names no commit: git found no commit"
    assert caught.value.render(full_path=True).startswith("revision 'HEAD^{tree}' names no commit: ")
    # Every git call the side makes speaks in the C locale, the short id's included.
    assert environments and all(env is not None and env["LC_ALL"] == "C" for env in environments)
    assert git_environment()["LC_ALL"] == "C"


def test_a_revision_without_the_model_root_is_refused(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "notes").mkdir(parents=True)
    _write(project / "notes" / "README.md", "before the model\n")
    _commit(project)
    _copy_basic(tmp_path / "copy")
    shutil.copytree(tmp_path / "copy" / "project", project, dirs_exist_ok=True)
    with pytest.raises(SnapshotReadError) as caught:
        compute_change_report(require_project(str(project / "swcraft.yaml")), "HEAD", diagrams=False)
    assert tempfile.gettempdir() not in str(caught.value)


def test_a_duplicate_identity_at_either_revision_is_refused(tmp_path: Path) -> None:
    project = _copy_basic(tmp_path)
    _commit(project)
    swc = project / "model" / "swc" / "example"
    _write(swc / "Copy.yaml", (swc / "ExampleSWC.yaml").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="duplicate key 'ExampleSWC'"):
        compute_change_report(require_project(str(project / "swcraft.yaml")), "HEAD", diagrams=False)


def test_a_file_that_does_not_parse_at_a_revision_names_no_temporary_tree(tmp_path: Path) -> None:
    project = _copy_basic(tmp_path)
    task = project / "model" / "tasks" / "Task_10ms.yaml"
    _write(task, "name: [unclosed\n")
    _commit(project)
    _write(task, "name: Task_10ms\nperiod: 0.01\npriority: 5\npartition: PARTITION0\n")
    with pytest.raises(RevisionParseError) as caught:
        compute_change_report(require_project(str(project / "swcraft.yaml")), "HEAD", diagrams=False)
    full = caught.value.render(full_path=True)
    assert full.startswith("revision 'HEAD': Invalid task YAML in tasks/Task_10ms.yaml")
    assert tempfile.gettempdir() not in full and os.path.realpath(tempfile.gettempdir()) not in full


def test_a_revisions_refusal_of_two_fields_stays_one_line_under_its_prefix(tmp_path: Path) -> None:
    """A wrapper is never split into the CLI's lines; the details join by ``; ``."""
    project = _copy_basic(tmp_path)
    task = project / "model" / "tasks" / "Task_10ms.yaml"
    _write(task, "name: Task_10ms\nperiod: .nan\npriority: x\npartition: PARTITION0\n")
    _commit(project)
    _write(task, "name: Task_10ms\nperiod: 0.01\npriority: 5\npartition: PARTITION0\n")
    with pytest.raises(RevisionParseError) as caught:
        compute_change_report(require_project(str(project / "swcraft.yaml")), "HEAD", diagrams=False)
    expected = (
        "revision 'HEAD': Invalid task YAML in tasks/Task_10ms.yaml: period: Input should be a finite number; "
        "priority: Input should be a valid integer, unable to parse string as an integer"
    )
    assert caught.value.render(full_path=True) == expected
    assert str(caught.value) == expected


def test_the_temporary_trees_are_removed_on_success_and_on_a_refusal(
    fixture_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[str] = []
    materialize = change_report_module.materialize_revision

    def recording(model_root: str, revision: str):
        tmpdir, extracted = materialize(model_root, revision)
        made.append(tmpdir.name)
        return tmpdir, extracted

    monkeypatch.setattr(change_report_module, "materialize_revision", recording)
    compute_change_report(require_project(str(fixture_project)), "HEAD", diagrams=False)
    project = _copy_basic(tmp_path)
    _commit(project)
    swc = project / "model" / "swc" / "example"
    _write(swc / "Copy.yaml", (swc / "ExampleSWC.yaml").read_text(encoding="utf-8"))
    with pytest.raises(ValueError):
        compute_change_report(require_project(str(project / "swcraft.yaml")), "HEAD", diagrams=False)
    assert len(made) == 2 and not any(Path(name).exists() for name in made)


def test_a_key_is_spelled_as_swcraft_diff_spells_it() -> None:
    """The restated rule agrees with ``differ._render_key`` on a composite key, a value key and a name."""
    composite = collection_rule(Task, "runnables")
    events = collection_rule(SWComponent, "runnables")
    cases = [
        ((None, "Chassis/Brake/Monitor", "DetectFade"), composite),
        (("BrakeController", None, "Process"), composite),
        ((0.01, "timing"), None),
        ("pOut", events),
    ]
    for key, rule in cases:
        assert _key_text(key, rule) == _render_key(key, rule)
    assert _key_text((None, "Chassis/Brake/Monitor", "DetectFade"), composite) == "Chassis/Brake/Monitor.DetectFade"


# Findings, resources, consumers


def test_the_introduced_findings_and_no_finding_the_model_already_had(report: ChangeReport) -> None:
    assert [(finding.rule, finding.severity, finding.message) for finding in report.findings] == [
        ("trace.link-needs-confirmation", "warning", WARN_NEEDS_CONFIRMATION),
        ("trace.link-version-not-stated", "warning", WARN_NOT_STATED),
    ]
    assert not any("REQ_EXAMPLE_003" in finding.message for finding in report.findings)


def test_findings_are_a_multiset_errors_first() -> None:
    def issue(rule: str, message: str) -> ConsistencyIssue:
        return ConsistencyIssue(rule=rule, message=message)

    warning = issue("trace.requirement-stub", "stub")
    error = issue("name.duplicate-swc", "twice")
    before = [warning, error]
    after = [error, error, warning, warning, issue("connector.unknown-port", "new")]
    assert introduced_findings(before, after) == (error, after[4], warning)


def test_fail_on_counts_the_introduced_findings(report: ChangeReport) -> None:
    assert report.fails_on("warning") and not report.fails_on("error") and not report.fails_on("never")
    with pytest.raises(ValueError):
        report.fails_on("info")


def test_the_resource_rows(report: ChangeReport) -> None:
    assert report.resources == (
        ResourceRow("CORE0", "core", ("70.00%", "70.00%"), "12.00%", "15.00%", "+3.00%", "within budget"),
        ResourceRow(
            "RAM0",
            "memory",
            ("209715.2 bytes", "209715 bytes"),
            "3584 bytes",
            "4608 bytes",
            "+1024 bytes",
            "within budget",
        ),
    )


def test_a_fractional_budget_reads_whole_in_the_markdown_and_exact_in_the_json(report: ChangeReport) -> None:
    assert "| `RAM0` | 209715 bytes |" in render_markdown(report)
    (memory,) = (row for row in json.loads(render_json(report))["resources"] if row["name"] == "RAM0")
    assert memory["budget"] == "209715.2 bytes"


def test_the_consumers_of_a_changed_interface_at_the_after_model(report: ChangeReport) -> None:
    """``IExample``'s users after the change.

    The three components on it, the task running two of them, the two connectors between them, the composition
    holding them and the sequence of the task.
    """
    assert report.consumers == (
        ConsumerGroup(
            "IExample",
            "changed",
            (
                Dependent("swc", "SWCs", "ExampleConsumerSWC"),
                Dependent("swc", "SWCs", "ExampleSWC"),
                Dependent("swc", "SWCs", "MonitorSWC"),
                Dependent("tasks", "Tasks", "Task_10ms"),
                Dependent("connectors", "Connectors", "Example_to_Monitor"),
                Dependent("compositions", "Compositions", "Root"),
                Dependent("sequences", "Sequences", "task10ms_consumer_cycle"),
                Dependent("connectors", "Connectors", "Example_feeds_Consumer"),
            ),
        ),
    )


@pytest.fixture(scope="module")
def removal_report(tmp_path_factory: pytest.TempPathFactory) -> ChangeReport:
    """``RAM1`` and ``IDiagnosticService`` (used by ``ExampleSWC``'s port ``rDiag``) removed."""
    project = _copy_basic(tmp_path_factory.mktemp("removal"))
    model = project / "model"
    _write(
        model / "hardware" / "RAM1.yaml",
        "name: RAM1\nkind: memory\ntype: RAM\nsize: 1024\naccessible:\n- CORE0\nthreshold: 0.5\n"
        "enforcement: advisory\n",
    )
    example = model / "swc" / "example" / "ExampleSWC.yaml"
    diag_port = "  - name: rDiag\n    direction: require\n    interface: IDiagnosticService\n"
    _replace(example, PORT_DESCRIPTION, PORT_DESCRIPTION + diag_port)
    _commit(project)
    (model / "hardware" / "RAM1.yaml").unlink()
    (model / "interfaces" / "IDiagnosticService.yaml").unlink()
    _replace(example, diag_port, "")
    return compute_change_report(require_project(str(project / "swcraft.yaml")), "HEAD", diagrams=False)


def test_a_removed_resource_keeps_its_row(removal_report: ChangeReport) -> None:
    assert removal_report.resources[-1] == ResourceRow(
        "RAM1", "memory", ("512 bytes", "512 bytes"), "0 bytes", None, None, None
    )
    assert [row.change for row in removal_report.resources] == ["0", "0", None]


def test_a_removed_interface_is_read_at_the_before_model(removal_report: ChangeReport) -> None:
    assert removal_report.consumers == (
        ConsumerGroup(
            "IDiagnosticService",
            "removed",
            (
                Dependent("swc", "SWCs", "ExampleSWC"),
                Dependent("connectors", "Connectors", "Example_to_Consumer"),
                Dependent("compositions", "Compositions", "Root"),
            ),
        ),
    )


# ARXML and the diagrams


def test_the_arxml_rows(report: ChangeReport) -> None:
    assert report.arxml.compared and report.arxml.reason is None
    assert report.arxml.elements == (
        ArxmlElementChange("PortInterfaces/IExample_value_Init", "CONSTANT-SPECIFICATION", "changed"),
        ArxmlElementChange("ComponentTypes/MonitorSWC", "APPLICATION-SW-COMPONENT-TYPE", "added"),
        ArxmlElementChange("ComponentTypes/MonitorSWC_Implementation", "SWC-IMPLEMENTATION", "added"),
        ArxmlElementChange("ComponentTypes/Root", "COMPOSITION-SW-COMPONENT-TYPE", "changed"),
    )


def test_a_removed_arxml_element(removal_report: ChangeReport) -> None:
    assert removal_report.arxml.elements == (
        ArxmlElementChange("ComponentTypes/ExampleSWC", "APPLICATION-SW-COMPONENT-TYPE", "changed"),
        ArxmlElementChange("PortInterfaces/IDiagnosticService", "CLIENT-SERVER-INTERFACE", "removed"),
    )


def test_a_side_with_an_error_is_not_compared(tmp_path: Path) -> None:
    project = _copy_basic(tmp_path)
    _commit(project)
    _replace(
        project / "model" / "swc" / "example" / "ExampleConsumerSWC.yaml", "interface: IExample", "interface: INope"
    )
    result = compute_change_report(require_project(str(project / "swcraft.yaml")), "HEAD", diagrams=False)
    assert result.introduced_errors >= 1
    assert not result.arxml.compared and result.arxml.elements == ()
    assert result.arxml.reason == (
        f"Not compared: the model in the working tree has {result.introduced_errors} error(s); "
        "`swcraft validate` refuses it."
    )
    assert result.fails_on("error") and result.fails_on("warning") and not result.fails_on("never")


def test_a_writer_refusal_names_the_side_and_not_the_writer_text(
    fixture_project: Path, head_label: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    reached: list[str] = []

    def refuse(model_root: str, version: object) -> None:
        reached.append(model_root)
        raise ValueError(f"refused in {model_root}")

    monkeypatch.setattr("swcraft_tools.writer.arxml_generation._build_document", refuse)
    result = compute_change_report(require_project(str(fixture_project)), "HEAD", diagrams=False)
    assert (result.arxml.compared, result.arxml.reason) == (
        False,
        f"Not compared: the writer refused the model at {head_label}.",
    )
    assert result.arxml.elements == ()
    assert len(reached) == 1


def test_no_diagrams_keeps_the_shown_set_and_says_why(report: ChangeReport) -> None:
    assert report.diagrams is None
    assert report.diagram_note == "Not drawn: `--no-diagrams` was given."
    assert report.layout_refusal is None
    assert report.compositions == ("Root",)


def test_a_layout_failure_is_the_note_and_names_no_path(fixture_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from swcraft_tools.writer.diagrams import change
    from swcraft_tools.writer.diagrams.layout import DiagramLayoutError

    reached: list[int] = []

    def fail(*args: object) -> None:
        reached.append(len(args))
        raise DiagramLayoutError("Node.js was not found on PATH.", located="Node.js was not found at /opt/node/bin.")

    monkeypatch.setattr(change, "change_overviews", fail)
    result = compute_change_report(require_project(str(fixture_project)), "HEAD", diagrams=True)
    assert result.diagrams is None
    assert result.diagram_note == "Not drawn: Node.js was not found on PATH."
    assert result.layout_refusal == "Node.js was not found on PATH."
    assert result.compositions == ("Root",)
    assert reached == [3]


@pytest.fixture(scope="module")
def drawn_report(fixture_project: Path) -> ChangeReport:
    requires_node()
    return compute_change_report(require_project(str(fixture_project)), "HEAD", diagrams=True)


def test_the_overviews_are_drawn(drawn_report: ChangeReport) -> None:
    assert drawn_report.diagram_note is None and drawn_report.diagrams is not None
    assert [(overview.composition, overview.stem) for overview in drawn_report.diagrams] == [("Root", "Root")]
    assert all(overview.before_svg is not None for overview in drawn_report.diagrams)


# The Markdown and the JSON

EXPECTED_MARKDOWN = """\
# Change Report

swcraft-basic-example · working tree against {head}

## Verdict

**0 error(s) · 2 warning(s) introduced (1 already in the model) · 13 change(s) in 10 element(s) · 1 composition(s).**

## What changed

| Element | Kind | Change |
| --- | --- | --- |
| `REQ_EXAMPLE_001` | Requirements | changed — version: 1 → 2 |
| `Root` | Compositions | changed — members: + MonitorSWC |
| `MonitorSWC` | SWCs | added |
| `ExampleConsumerSWC` | SWCs | changed — satisfies: + REQ_EXAMPLE_001@2; satisfies: − REQ_EXAMPLE_001@1 |
| `ExampleSWC` | SWCs | changed — ports.pOut.satisfies: − REQ_EXAMPLE_002@1; runnables.ExampleRun.satisfies: + \
REQ_EXAMPLE_002@1 |
| `IExample` | Interfaces | changed — data_elements.value.init: 0 → 5 |
| `Example_feeds_Consumer` | Connectors | added — possibly a rename of `Example_to_Consumer` |
| `Example_to_Monitor` | Connectors | added |
| `Example_to_Consumer` | Connectors | removed |
| `Task_10ms` | Tasks | changed — wcet: '1200us' → '1500us'; runnables: + MonitorSWC.MonitorRun |

## Requirements

| Requirement | Holder | Change |
| --- | --- | --- |
| `REQ_EXAMPLE_001` | SWC 'ExampleConsumerSWC' | re-confirmed, @1 → @2 |
| `REQ_EXAMPLE_001` | — | needs confirmation: {needs} |
| `REQ_EXAMPLE_002` | SWC 'ExampleSWC' (runnable 'ExampleRun') | gained, @1 |
| `REQ_EXAMPLE_002` | SWC 'MonitorSWC' | gained, no version stated |
| `REQ_EXAMPLE_002` | SWC 'ExampleSWC' (port 'pOut') | lost, @1 |
| `REQ_EXAMPLE_002` | — | needs confirmation: {stated} |

## Findings

Findings the model already had are not listed.

| Finding | Severity |
| --- | --- |
| {needs} | warning |
| {stated} | warning |

## Resources

| Resource | Budget | Before | After | Change | Status |
| --- | --- | --- | --- | --- | --- |
| `CORE0` | 70.00% | 12.00% | 15.00% | +3.00% | within budget |
| `RAM0` | 209715 bytes | 3584 bytes | 4608 bytes | +1024 bytes | within budget |

## Interface consumers

Every element that depends on an interface the change changed (read at the new revision) or removed (read at the \
old revision).

### `IExample` (changed)

| Layer | Dependents |
| --- | --- |
| SWCs | `ExampleConsumerSWC`, `ExampleSWC`, `MonitorSWC` |
| Tasks | `Task_10ms` |
| Connectors | `Example_to_Monitor`, `Example_feeds_Consumer` |
| Compositions | `Root` |
| Sequences | `task10ms_consumer_cycle` |

## ARXML

| Element | Type | Change |
| --- | --- | --- |
| `PortInterfaces/IExample_value_Init` | CONSTANT-SPECIFICATION | changed |
| `ComponentTypes/MonitorSWC` | APPLICATION-SW-COMPONENT-TYPE | added |
| `ComponentTypes/MonitorSWC_Implementation` | SWC-IMPLEMENTATION | added |
| `ComponentTypes/Root` | COMPOSITION-SW-COMPONENT-TYPE | changed |

## Diagrams

Not drawn: `--no-diagrams` was given.

---

Generated by `swcraft change-report` from the model at two revisions; every line is computed from the model, \
nothing is written by AI. The manifest at the working tree is used for both revisions.
"""


def _sections(markdown: str) -> dict[str, str]:
    """``## <title>`` → its body, and ``""`` → the title and meta above the first section."""
    parts = markdown.split("\n## ")
    sections = {"": parts[0]}
    for part in parts[1:]:
        title, _, body = part.partition("\n")
        sections[title] = body
    return sections


def test_the_markdown_matches_the_hand_written_text_section_by_section(report: ChangeReport, head_label: str) -> None:
    expected = EXPECTED_MARKDOWN.format(head=head_label, needs=WARN_NEEDS_CONFIRMATION, stated=WARN_NOT_STATED)
    actual = render_markdown(report)
    assert list(_sections(actual)) == list(_sections(expected))
    for title, body in _sections(expected).items():
        assert _sections(actual)[title] == body, title
    assert actual == expected


def test_the_json_document(report: ChangeReport, head_label: str) -> None:
    document = json.loads(render_json(report))
    assert list(document) == [
        "schemaVersion",
        "kind",
        "revisions",
        "verdict",
        "changes",
        "requirements",
        "findings",
        "resources",
        "consumers",
        "arxml",
        "diagrams",
    ]
    assert (document["schemaVersion"], document["kind"]) == (1, "change-report")
    assert document["revisions"] == {"old": head_label, "new": "working tree"}
    assert document["verdict"] == {
        "errors": 0,
        "warnings": 2,
        "changes": 13,
        "elements": 10,
        "compositions": 1,
        "existingErrors": 0,
        "existingWarnings": 1,
    }
    assert document["changes"] == json.loads(format_diff_json(report.diff))["layers"]
    assert document["requirements"][0] == {
        "requirement": "REQ_EXAMPLE_001",
        "holder": "SWC 'ExampleConsumerSWC'",
        "change": "re-confirmed",
        "oldVersion": "1",
        "newVersion": "2",
        "messages": [],
    }
    assert document["requirements"][-1]["messages"] == [WARN_NOT_STATED]
    assert document["findings"][0] == {
        "rule": "trace.link-needs-confirmation",
        "severity": "warning",
        "message": WARN_NEEDS_CONFIRMATION,
    }
    assert document["resources"][1] == {
        "name": "RAM0",
        "kind": "memory",
        "budget": "209715.2 bytes",
        "before": "3584 bytes",
        "after": "4608 bytes",
        "change": "+1024 bytes",
        "status": "within budget",
    }
    assert document["consumers"][0]["dependents"][0] == {"layer": "swc", "name": "ExampleConsumerSWC"}
    assert document["arxml"]["compared"] is True and document["arxml"]["reason"] is None
    assert document["arxml"]["elements"][1] == {
        "path": "ComponentTypes/MonitorSWC",
        "type": "APPLICATION-SW-COMPONENT-TYPE",
        "change": "added",
    }
    assert document["diagrams"] is None
    assert render_json(report).endswith("}\n")


def test_a_model_text_is_escaped_in_a_cell(report: ChangeReport) -> None:
    odd = dataclasses.replace(
        report,
        changed_elements=(
            ChangedElementRow("swc", "SWCs", "a`b", "changed", ("description: 'x | y' → 'two\nlines <b>'",)),
        ),
        findings=(ConsistencyIssue(rule="name.duplicate-swc", message="one | two\nthree <i>&"),),
    )
    markdown = render_markdown(odd)
    assert "| `` a`b `` | SWCs | changed — description: 'x \\| y' → 'two lines &lt;b>' |" in markdown
    assert "| one \\| two three &lt;i>&amp; | error |" in markdown
    assert "<b>" not in markdown and "<i>" not in markdown


def test_a_non_finite_float_is_spelled_as_the_diff_spells_it(report: ChangeReport) -> None:
    changed = ChangedItem(key="ExampleSWC", changed_fields=[("description", float("nan"), float("inf"))])
    diff = ModelDiff.from_state_diff(ModelStateDiff(layers={"swc": LayerDiff(changed=[changed])}))
    document = json.loads(render_json(dataclasses.replace(report, diff=diff)))
    assert document["changes"]["swcs"]["changed"][0]["fields"] == [
        {"field": "description", "old": "NaN", "new": "Infinity"}
    ]


def test_the_report_names_no_temporary_tree_and_no_project_location(
    report: ChangeReport, fixture_project: Path
) -> None:
    forbidden = {
        tempfile.gettempdir(),
        os.path.realpath(tempfile.gettempdir()),
        str(fixture_project.parent),
        fixture_project.parent.as_posix(),
    }
    for name, text in report_files(report):
        for location in forbidden:
            assert location not in text, (name, location)


def test_the_same_change_gives_the_same_bytes(report: ChangeReport, fixture_project: Path) -> None:
    again = compute_change_report(require_project(str(fixture_project)), "HEAD", None, diagrams=False)
    assert report_files(again) == report_files(report)


def test_the_report_files_an_earlier_run_may_have_left() -> None:
    for name in (
        "change-report.md",
        "Change-Report-PR.md",
        "Change-Report.JSON",
        "overview-Root-before.svg",
        "overview-a~1-after.svg",
    ):
        assert is_report_file(name), name
    for name in ("notes.md", "overview-Root.svg", "overview-Root-before.png", "report.md"):
        assert not is_report_file(name), name


def test_no_change_says_so_in_every_section(unchanged_report: ChangeReport) -> None:
    markdown = render_markdown(unchanged_report)
    for sentence in (
        "**No change to the model.**",
        "\n\nNo change to the model.\n",
        "No requirement link was gained, lost or re-confirmed, and none needs confirmation.",
        "The change introduces no finding; findings the model already had are not listed.",
        "No interface was changed or removed.",
        "No ARXML element changed.",
    ):
        assert sentence in markdown


def test_the_drawn_overviews_are_listed_as_files(drawn_report: ChangeReport) -> None:
    files = dict(report_files(drawn_report))
    assert list(files) == [
        "change-report.md",
        "change-report-pr.md",
        "change-report.json",
        "overview-Root-before.svg",
        "overview-Root-after.svg",
    ]
    assert "<svg" in files["overview-Root-before.svg"] and "<svg" in files["overview-Root-after.svg"]
    assert "![Root after](overview-Root-after.svg)" in files["change-report.md"]
    assert json.loads(files["change-report.json"])["diagrams"] == [
        {"composition": "Root", "before": "overview-Root-before.svg", "after": "overview-Root-after.svg"}
    ]


def test_the_pull_request_file_is_the_report_without_its_diagrams_section(drawn_report: ChangeReport) -> None:
    """The pictures are build artifacts no repository holds, so the PR text names none of them."""
    files = dict(report_files(drawn_report))
    full, pr = files["change-report.md"], files["change-report-pr.md"]
    head, diagrams_and_footer = full.split("\n## Diagrams\n", 1)
    footer = diagrams_and_footer[diagrams_and_footer.index("\n---\n") :]
    assert pr == head + footer
    assert "## Diagrams" not in pr
    for absent in (".svg", "Legend:", "Not drawn", "overview-"):
        assert absent not in pr, absent


def test_the_pull_request_file_is_written_without_pictures_too(report: ChangeReport) -> None:
    files = dict(report_files(report))
    assert "Not drawn: `--no-diagrams` was given." in files["change-report.md"]
    assert files["change-report-pr.md"] == render_markdown(report, diagrams=False)
    assert "## Diagrams" not in files["change-report-pr.md"]
    assert "Not drawn" not in files["change-report-pr.md"]


@pytest.mark.parametrize(
    ("errors", "warnings", "expected"),
    [
        (0, 0, "**0 error(s) · 2 warning(s) introduced · 13 change(s)"),
        (0, 32, "**0 error(s) · 2 warning(s) introduced (32 already in the model) · 13 change(s)"),
        (1, 32, "**0 error(s) · 2 warning(s) introduced (1 error, 32 warnings already in the model) · 13 change(s)"),
        (2, 1, "**0 error(s) · 2 warning(s) introduced (2 errors, 1 warning already in the model) · 13 change(s)"),
        (3, 0, "**0 error(s) · 2 warning(s) introduced (3 errors already in the model) · 13 change(s)"),
    ],
    ids=["none", "warnings", "both", "singular", "errors"],
)
def test_the_verdict_names_the_findings_already_in_the_model(
    report: ChangeReport, errors: int, warnings: int, expected: str
) -> None:
    """The verdict names existing findings: "0 warning(s) introduced" is not "the model has no warning"."""
    already = dataclasses.replace(report, existing_errors=errors, existing_warnings=warnings)
    for markdown in (render_markdown(already), render_markdown(already, diagrams=False)):
        assert expected in markdown
    verdict = json.loads(render_json(already))["verdict"]
    assert (verdict["existingErrors"], verdict["existingWarnings"]) == (errors, warnings)


def test_a_finding_the_model_already_had_is_counted_once(report: ChangeReport) -> None:
    """The fixture's model holds one warning at both revisions: already there, never introduced."""
    assert (report.existing_errors, report.existing_warnings) == (0, 1)
    assert (report.introduced_errors, report.introduced_warnings) == (0, 2)


def test_the_json_consumers_say_whether_the_interface_changed_or_went(report: ChangeReport) -> None:
    """A program can tell a removed interface (read at the old model) from a changed one."""
    document = json.loads(render_json(report))

    assert {group["change"] for group in document["consumers"]} <= {"changed", "removed"}
    assert all(list(group) == ["interface", "change", "dependents"] for group in document["consumers"])


def test_reordering_the_members_of_one_satisfies_moves_no_link(monkeypatch) -> None:
    """`[REQ@1, REQ@2]` written as `[REQ@2, REQ@1]` is no re-confirmation."""
    before = {("REQ", "SWC 'A'"): ("1", "2")}
    after = {("REQ", "SWC 'A'"): ("2", "1")}
    answers = iter((before, after))
    monkeypatch.setattr(change_report_module, "_link_versions", lambda _state: next(answers))

    assert change_report_module._requirement_rows(None, None, ()) == ()


def test_a_picture_link_is_percent_encoded() -> None:
    """A name the schema accepts with a space or a `#` cannot end the destination or start a fragment."""
    from swcraft_tools.change_report_render import _link

    assert _link("overview-A B-after.svg") == "overview-A%20B-after.svg"
    assert _link("overview-A#B-before.svg") == "overview-A%23B-before.svg"


def test_a_text_no_xml_document_holds_is_a_note_and_no_picture(tmp_path: Path) -> None:
    """A composition title holding U+0001 is a note, never written into an SVG."""
    requires_node()
    manifest = build_project(tmp_path)
    root = manifest.parent / "model" / "compositions" / "Root.yaml"
    _write(root, root.read_text(encoding="utf-8").replace("title: Root composition", 'title: "bad\\x01text"', 1))

    report = compute_change_report(require_project(str(manifest)), "HEAD", diagrams=True)

    assert report.diagrams is None
    assert report.diagram_note.startswith("Not drawn: no diagram was written: the model's text holds U+0001")
    assert report.layout_refusal is not None
