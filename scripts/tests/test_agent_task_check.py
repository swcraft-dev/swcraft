"""``scripts/agent_task_check.py`` decides a benchmark run, and the set's expected results are the bench's.

The checker: a ``change`` task passes on the bench with its expected state (``expected/<id>/``,
plus ``removed.txt``) laid over it and fails on the untouched bench; a ``question`` task passes a
right reply and fails a wrong one or a changed model. The expected answers: each is recomputed
from the bench, so a figure that drifts from the bench fails here and not in a benchmark run.

Not reached: whether the prompts are what an architect types, and anything an agent does (the
overlays are hand-written expected states, not runs).
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    AGENT_TASK_BENCH,
    REPO_ROOT,
    agent_task_check_module,
    copy_agent_task_bench,
)

CHECK = agent_task_check_module()
SCRIPT = REPO_ROOT / "scripts" / "agent_task_check.py"
SET_DIR = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "agent-bench"
BENCH = AGENT_TASK_BENCH
EXPECTED = SET_DIR / "expected"

GROUPS = {
    # 35 and 36 came after the guide questions: a new feature gets a new model task.
    "model-change": (*range(1, 8), 35, 36),
    "question": range(8, 10),
    "mixed": range(10, 11),
    "search-and-statistics": range(11, 15),
    "relations-and-containment": range(15, 18),
    "requirements-and-traceability": range(18, 25),
    "guide-question": (*range(25, 35), 37),
}


def _tasks() -> dict[str, dict[str, Any]]:
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((SET_DIR / "tasks").glob("*.json"), key=lambda path: path.as_posix())
    }


TASKS = _tasks()
READY_CHANGES = sorted(
    (stem for stem, task in TASKS.items() if task["kind"] == "change" and task["status"] == "ready"), key=str
)

#: A right and a wrong reply per question; the right one is phrased as an agent would.
REPLIES = {
    "08-interface-change-impact": (
        "Directly: BrakeThermalProtection and VehicleStateManager (their ports), and the compositions "
        "BrakeSubsystem and Chassis, which pass it through their boundary ports.",
        "VehicleStateManager provides it and BrakeThermalProtection reads it.",
    ),
    "09-ram-for-a-component": (
        "No. RAM has 10240 bytes of headroom under its 75 % threshold; 12 KB does not fit.",
        "Yes, RAM is 32 KB and only 14 KB are used, so there is no problem.",
    ),
    "11-ports-of-a-component": (
        "Six: rBrakeTemperature, rBrakeRequest, rOperatingState, pPermittedBrakeAction, "
        "pThermalProtectionStatus and pThermalProtectionEvent.",
        "5 ports: rBrakeTemperature, rBrakeRequest, pPermittedBrakeAction, pThermalProtectionStatus, "
        "pThermalProtectionEvent.",
    ),
    "12-components-on-an-interface": (
        "BrakeTemperatureSensor provides it; BrakeController and BrakeThermalProtection require it.",
        "BrakeTemperatureSensor, BrakeController, BrakeThermalProtection and DiagnosticManager.",
    ),
    "13-unconnected-ports": (
        "Two: DiagnosticManager.rEcuMode and InstrumentCluster.rOdometer.",
        "DiagnosticManager.rEcuMode.",
    ),
    "14-asil-b-count": (
        "3 components are ASIL B: BrakeThermalProtection, BrakeController and BrakeTemperatureSensor.",
        "One: BrakeThermalProtection.",
    ),
    "15-components-in-a-partition": (
        "BrakeTemperatureSensor (front and rear), BrakeThermalProtection and BrakeController.",
        "BrakeThermalProtection, BrakeController, BrakeTemperatureSensor and DiagnosticManager.",
    ),
    "16-tasks-on-a-core": (
        "Task_100ms and Task_100ms_Low, both in partition P_Vehicle.",
        "Task_100ms and Task_50ms.",
    ),
    "17-largest-ram-consumer": (
        "BrakeThermalProtection, with 4096 bytes; next is BrakeController with 3072.",
        "BrakeController takes 3072 bytes, BrakeThermalProtection 4096.",
    ),
    "18-elements-satisfying-a-requirement": (
        "Three elements satisfy REQ_BTP_DIAGNOSTICS@1:\n\n| Element | Layer |\n|---|---|\n"
        "| DiagnosticManager.ProcessDiagnosticEvent | runnable (swc) |\n| NvM | swc |\n| INvStorage | interface |",
        "DiagnosticManager, NvM and INvStorage.",
    ),
    "19-unallocated-requirements": (
        "31 requirements are satisfied by no element. The first twenty: SWR-BRK-005, SWR-BRK-006, SWR-BRK-007, "
        "SWR-BRK-011, SWR-BRK-012, SWR-DIA-001, SWR-DIA-002, SWR-DIA-003, SWR-DIA-004, SWR-DIA-005, SWR-DIA-006, "
        "SWR-DIA-007, SWR-DIA-008, SWR-DIA-009, SWR-DIA-010, SWR-DRV-001, SWR-DRV-002, SWR-DRV-003, SWR-DRV-004, "
        "SWR-DRV-005, and 11 more by a list request.",
        "SWR-BRK-005, SWR-BRK-006 and REQ_BTP_CORE_LOAD.",
    ),
    "20-links-needing-confirmation": (
        "REQ_BTP_TEMPERATURE_RANGE is at version 2; IBrakeDiagnostics, IBrakeTemperature and "
        "IThermalProtectionStatus still satisfy it at @1.",
        "None: every link names its requirement's current version.",
    ),
    "21-requirements-about-a-topic": (
        "REQ_BTP_DECOMP_MAIN, REQ_BTP_DECOMP_MONITOR, REQ_BTP_TEMPERATURE_RANGE, REQ_SHOWCASE_FEATURE_001, "
        "SWR-BRK-004, SWR-DIA-007, SWR-GEN-001, SWR-THP-001, SWR-THP-003, SWR-THP-006, SWR-THP-007, SWR-THP-009.",
        "REQ_BTP_TEMPERATURE_RANGE, SWR-GEN-001, SWR-THP-006, SWR-THP-007 and REQ_BTP_CORE_LOAD.",
    ),
    "22-candidates-for-a-requirement": (
        "Besides the tasks and cores already linked: P_Brake (shares processor and core), RAM (the load budget) "
        "and P_Vehicle (a core's partition).",
        "P_Brake and P_Vehicle, both partitions of a core.",
    ),
}


def bench_copy(tmp_path: Path) -> Path:
    return copy_agent_task_bench(tmp_path)


def lay_over(bench: Path, task_id: str) -> None:
    """Lay the task's expected state over *bench*: its files written, ``removed.txt``'s deleted."""
    overlay = EXPECTED / task_id
    for source in sorted(overlay.rglob("*.yaml"), key=lambda path: path.as_posix()):
        target = bench / source.relative_to(overlay)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    removed = overlay / "removed.txt"
    if removed.is_file():
        for line in removed.read_text(encoding="utf-8").splitlines():
            (bench / line).unlink()


def run_cli(*arguments: str) -> Any:
    completed = subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", *arguments, "--project", (BENCH / "swcraft.yaml").as_posix()],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return completed.stdout


def run_query(request: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(request), encoding="utf-8", newline="\n")
    return json.loads(run_cli("query", "--request", request_file.as_posix(), "--format", "json"))


# ---------------------------------------------------------------------------
# The set.
# ---------------------------------------------------------------------------


def test_the_set_is_the_twenty_four_tasks_the_guide_questions_and_the_access_form_tasks() -> None:
    # Tasks 25-34 and 37 ask what the guide writes; they alone are kind ``guide``.
    assert sorted(task["task"] for task in TASKS.values()) == list(range(1, 38))
    assert sorted(task["task"] for task in TASKS.values() if task["kind"] == "guide") == [*range(25, 35), 37]
    assert sorted((stem for stem, task in TASKS.items() if task["kind"] == "guide"), key=str) == sorted(
        (stem for stem, task in TASKS.items() if task["group"] == "guide-question"), key=str
    )
    for stem, task in TASKS.items():
        assert task["id"] == stem
        assert stem.startswith(f"{task['task']:02d}-")
        assert task["task"] in GROUPS[task["group"]], stem
        assert task["kind"] in CHECK.KINDS
        assert task["prompt"].strip()


@pytest.mark.parametrize("stem", sorted(TASKS, key=str))
def test_a_task_states_how_its_run_is_counted(stem: str) -> None:
    task = TASKS[stem]
    expect = task["expect"]
    if task["kind"] == "change":
        assert expect["check_model"] == "no-errors"
        assert expect["assertions"]
    elif task["kind"] == "guide":
        assert expect["model"] == "unchanged"
        assert set(expect["guide"]) - {"page_parts"} == {"sections", "must", "must_not"}
        assert set(expect["calls"]) == {"forbidden"}
    else:
        assert expect["model"] == "unchanged"
        assert set(expect["answer"]) <= {
            "mentions_all", "mentions_none", "number", "first_of", "long_list", "with_layer", "with_owner",
            "candidates",
        }  # fmt: skip
        assert expect["answer"]
    assert task["status"] in ("ready", "needs-model-addition")
    assert (task["status"] == "ready") == ("model_addition" not in task)


def test_every_ready_change_task_has_an_expected_state_and_no_other_does() -> None:
    assert sorted((path.name for path in EXPECTED.iterdir()), key=str) == READY_CHANGES


def test_the_brake_data_logger_prompt_is_verbatim() -> None:
    prompt = TASKS["02-brake-data-logger"]["prompt"]
    assert prompt.startswith("Add a brake diagnostics data logger to the model: 1. A new application component")
    assert prompt.endswith("After the changes run check-model.")


def test_the_bench_has_no_error_so_any_error_after_a_run_is_new() -> None:
    assert CHECK.check_model_errors(BENCH) == []


# ---------------------------------------------------------------------------
# Change tasks.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("task_id", READY_CHANGES)
def test_a_change_task_passes_on_its_expected_state(task_id: str, tmp_path: Path) -> None:
    bench = bench_copy(tmp_path)
    lay_over(bench, task_id)
    assert CHECK.check_task(bench, CHECK.load_task(task_id)) == []


@pytest.mark.parametrize("task_id", READY_CHANGES)
def test_a_change_task_fails_on_the_untouched_bench(task_id: str) -> None:
    assert CHECK.check_task(BENCH, CHECK.load_task(task_id))


def test_a_change_that_breaks_the_model_fails_on_check_model(tmp_path: Path) -> None:
    bench = bench_copy(tmp_path)
    lay_over(bench, "06-rename-data-element")
    controller = bench / "model" / "swc" / "application" / "BrakeThermalProtection.yaml"
    text = controller.read_text(encoding="utf-8")
    controller.write_text(text.replace("element: brakeRequest", "element: request"), encoding="utf-8", newline="\n")
    failures = CHECK.check_task(bench, CHECK.load_task("06-rename-data-element"))
    assert any(failure.startswith("check-model: ERROR") for failure in failures), failures


def test_a_change_task_counts_a_partial_run_as_failed(tmp_path: Path) -> None:
    bench = bench_copy(tmp_path)
    lay_over(bench, "05-remove-component")
    (bench / "model" / "sequences" / "thermal_status_display.yaml").write_text(
        (BENCH / "model" / "sequences" / "thermal_status_display.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
        newline="\n",
    )
    failures = CHECK.check_task(bench, CHECK.load_task("05-remove-component"))
    assert "InstrumentCluster is still referenced by: sequences thermal_status_display" in failures


def _lay_over_and_edit(tmp_path: Path, task_id: str, component: str, old: str, new: str) -> Path:
    """A bench copy with *task_id*'s expected state laid over it and one text edit in *component*'s file."""
    bench = bench_copy(tmp_path)
    lay_over(bench, task_id)
    path = bench / "model" / "swc" / "application" / f"{component}.yaml"
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1
    path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
    return bench


@pytest.mark.parametrize(
    ("old", "new", "failure"),
    [
        ("    by: implicit\n", "    by: value\n", "does not contain"),
        ("    by: implicit\n", "", "does not contain"),
        ("    element: permittedAction\n", "    element: permittedAction\n    by: implicit\n", "still contains"),
        ("    element: permittedAction\n", "    element: permittedAction\n    by: value\n", "still contains"),
    ],
    ids=["read-by-value", "read-left-by-argument", "other-read-made-implicit", "other-read-made-by-value"],
)
def test_35_fails_a_read_left_explicit_or_the_other_read_moved(
    old: str, new: str, failure: str, tmp_path: Path
) -> None:
    bench = _lay_over_and_edit(tmp_path, "35-make-a-read-implicit", "BrakeController", old, new)
    failures = CHECK.check_task(bench, CHECK.load_task("35-make-a-read-implicit"))
    assert any(f"swc BrakeController {failure}" in line for line in failures), failures


#: Benchmark task 36's callback runnable as its expected state writes it.
CALLBACK_36 = (
    "- name: OnSnapshotStored\n  events:\n  - type: call_returned\n    port: rNvStorage\n    operation: WriteBlock\n"
)


@pytest.mark.parametrize(
    ("old", "new", "check_model"),
    [
        ("    asynchronous: true\n", "", True),
        (CALLBACK_36, "", False),
        ("    operation: WriteBlock\nbehavior_name", "    operation: ReadBlock\nbehavior_name", True),
        ("    name: Call_StoreSnapshot\n", "", False),
    ],
    ids=["call-left-synchronous", "no-callback", "callback-on-another-operation", "call-name-dropped"],
)
def test_36_fails_a_call_left_waiting_a_missing_callback_or_a_dropped_name(
    old: str, new: str, check_model: bool, tmp_path: Path
) -> None:
    bench = _lay_over_and_edit(tmp_path, "36-call-asynchronously", "DiagnosticManager", old, new)
    failures = CHECK.check_task(bench, CHECK.load_task("36-call-asynchronously"))
    assert any("swc DiagnosticManager does not contain" in line for line in failures), failures
    # At 4.3.0 an asynchronous call no result point references is no error, so a forgotten callback
    # fails the assertion alone; a callback naming a call no runnable makes asynchronous is an error too.
    assert any(line.startswith("check-model: ERROR") for line in failures) == check_model, failures


def test_36_at_4_2_2_a_forgotten_callback_is_also_check_models_error(tmp_path: Path) -> None:
    bench = _lay_over_and_edit(
        tmp_path,
        "36-call-asynchronously",
        "DiagnosticManager",
        CALLBACK_36,
        "",
    )
    manifest = bench / "swcraft.yaml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(
        text.replace('arxml_version: "4.3.0"', 'arxml_version: "4.2.2"'), encoding="utf-8", newline="\n"
    )
    errors = CHECK.check_model_errors(bench)
    assert errors and all("nothing fetches its result" in line for line in errors), errors


def test_the_export_is_committed_although_arxml_is_ignored() -> None:
    export = "packages/tools/tests/fixtures/agent-bench/bench/imports/BrakeLightController.arxml"
    ignored = subprocess.run(["git", "check-ignore", "-q", "--no-index", export], cwd=REPO_ROOT, check=False)
    assert ignored.returncode == 1, f".gitignore swallows {export}; a fresh clone would not have it"


def test_the_import_task_reads_an_export_the_bench_carries(tmp_path: Path) -> None:
    bench = bench_copy(tmp_path)
    completed = subprocess.run(
        [
            sys.executable, "-m", "swcraft_tools.cli", "import", "component",
            "--source", (bench / "imports" / "BrakeLightController.arxml").as_posix(),
            "--project", (bench / "swcraft.yaml").as_posix(), "--dry-run",
        ],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )  # fmt: skip
    assert completed.returncode == 0, completed.stderr
    assert "SWC BrakeLightController: created" in completed.stdout


# ---------------------------------------------------------------------------
# Question tasks.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("task_id", sorted(REPLIES, key=str))
def test_a_question_passes_the_right_reply_and_fails_the_wrong_one(task_id: str) -> None:
    right, wrong = REPLIES[task_id]
    task = CHECK.load_task(task_id)
    assert CHECK.check_task(BENCH, task, answer=right) == []
    assert CHECK.check_task(BENCH, task, answer=wrong)


def test_every_question_has_replies_here() -> None:
    assert sorted(REPLIES, key=str) == sorted((s for s, t in TASKS.items() if t["kind"] == "question"), key=str)


def test_a_question_fails_when_the_run_changed_the_model(tmp_path: Path) -> None:
    bench = bench_copy(tmp_path)
    lay_over(bench, "07-add-requirement")
    right, _ = REPLIES["16-tasks-on-a-core"]
    failures = CHECK.check_task(bench, CHECK.load_task("16-tasks-on-a-core"), answer=right)
    assert failures == ["the model changed: requirements SWR-BRK-013, swc BrakeController"]


# ---------------------------------------------------------------------------
# The command line.
# ---------------------------------------------------------------------------


def _main(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, SCRIPT.as_posix(), *arguments], capture_output=True, text=True, encoding="utf-8", check=False
    )


def test_the_command_line_says_pass_or_fail_with_the_diffs(tmp_path: Path) -> None:
    passing = _main(BENCH.as_posix(), "16-tasks-on-a-core", "--answer", _reply(tmp_path, "Task_100ms, Task_100ms_Low."))
    assert (passing.returncode, passing.stdout) == (0, "PASS 16-tasks-on-a-core\n")
    failing = _main(BENCH.as_posix(), "16-tasks-on-a-core", "--answer", _reply(tmp_path, "Task_50ms."))
    assert failing.returncode == 1
    assert failing.stdout.splitlines()[0] == "FAIL 16-tasks-on-a-core"
    assert "  - the answer does not name Task_100ms, Task_100ms_Low" in failing.stdout.splitlines()


def _reply(tmp_path: Path, text: str) -> str:
    path = tmp_path / "reply.txt"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path.as_posix()


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("16-tasks-on-a-core",), "is a question: give the agent's reply with --answer FILE"),
        (("99-no-such-task",), "no task '99-no-such-task'"),
    ],
)
def test_a_task_that_cannot_be_checked_exits_2(arguments: tuple[str, ...], message: str) -> None:
    completed = _main(BENCH.as_posix(), *arguments)
    assert completed.returncode == 2
    assert message in completed.stderr


def test_a_task_waiting_for_a_model_addition_is_refused(tmp_path: Path) -> None:
    task = {**TASKS["04-move-runnable"], "status": "needs-model-addition", "model_addition": "a second task"}
    (tmp_path / "04-move-runnable.json").write_text(json.dumps(task), encoding="utf-8", newline="\n")
    with pytest.raises(CHECK.TaskError, match="is 'needs-model-addition': a second task"):
        CHECK.load_task("04-move-runnable", tmp_path)


def _refusals() -> list[Exception]:
    generator = CHECK.load_script(CHECK.BENCH_MODULE)._GENERATOR
    return [
        generator.GeneratorNotConfigured("unset", "no generator at /secret/where"),
        generator.SpecimenUnavailable("lock-timeout", "the lock on /secret/where is held"),
        generator.SpecimenDigestMismatch(
            "large-bench", expected="a", actual="b", source_key="ab", source="/secret/where"
        ),
    ]


#: ``(index into _refusals(), the class the error line names)``.
LARGE_BASELINE_REFUSALS = [
    (0, "GeneratorNotConfigured (unset)"),
    (1, "SpecimenUnavailable (lock-timeout)"),
    (2, "SpecimenDigestMismatch"),
]


@pytest.mark.parametrize(("index", "named"), LARGE_BASELINE_REFUSALS)
def test_a_large_baseline_that_cannot_be_taken_exits_2(
    index: int, named: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache or generator refusal is the checker's, never the agent's: exit 2."""
    refusal = _refusals()[index]

    def refuse(target: Path, **_: object) -> Path:
        raise refusal

    monkeypatch.setattr(CHECK.load_script(CHECK.BENCH_MODULE), "build_large", refuse)
    reply = Path(_reply(tmp_path, "P1"))
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
        code = CHECK.main([tmp_path.as_posix(), "13-unconnected-ports", "--set", "large", "--answer", reply.as_posix()])
    assert code == 2
    assert stderr.getvalue() == f"error: the large baseline cannot be taken: {named}\n"
    assert "/secret" not in stderr.getvalue()


# ---------------------------------------------------------------------------
# The expected answers are the bench's.
# ---------------------------------------------------------------------------


def _answer(task_id: str) -> dict[str, Any]:
    return TASKS[task_id]["expect"]["answer"]


def test_08_the_direct_users_are_impacts_first_level() -> None:
    document = json.loads(run_cli("impact", "IVehicleOperatingState", "--depth", "1", "--format", "json"))
    names = sorted(entry["name"] for entries in document["matched"].values() for entry in entries)
    assert names == sorted(_answer("08-interface-change-impact")["mentions_all"])


def test_09_and_17_are_the_resource_reports() -> None:
    report = run_cli("resource-report")
    ram = report.split("RESOURCE RAM", 1)[1]
    headroom = int(re.search(r"headroom (\d+) bytes", ram).group(1))
    assert headroom == TASKS["09-ram-for-a-component"]["reference"]["RAM"]["headroom"]
    assert headroom < 12 * 1000  # 12 KB does not fit, counted either way
    consumers = {name: int(size) for name, size in re.findall(r"- (\w+): (\d+) bytes", ram)}
    assert consumers == TASKS["17-largest-ram-consumer"]["reference"]["consumers"]
    assert (
        max(consumers, key=lambda name: consumers[name]) == _answer("17-largest-ram-consumer")["first_of"]["expected"]
    )
    assert sorted(consumers.values()).count(max(consumers.values())) == 1


def test_11_the_ports_are_the_components(tmp_path: Path) -> None:
    rows = run_query({"address": {"layer": "swc"}, "members": "ports"}, tmp_path)["elements"]
    ports = [row["member"] for row in rows if row["element"] == "BrakeThermalProtection"]
    assert sorted(ports) == sorted(_answer("11-ports-of-a-component")["mentions_all"])
    assert len(ports) == _answer("11-ports-of-a-component")["number"]


def test_12_the_components_on_the_interface(tmp_path: Path) -> None:
    request = {
        "address": {"layer": "swc"},
        "members": "ports",
        "where": [{"field": "interface", "equals": "IBrakeTemperature"}],
    }
    names = {row["element"] for row in run_query(request, tmp_path)["elements"]}
    assert sorted(names) == sorted(_answer("12-components-on-an-interface")["mentions_all"])


def test_13_the_unconnected_ports_are_the_models() -> None:
    model = CHECK.read_model(BENCH)
    ends = set()
    for connector in model["connectors"].values():
        for side in (connector["from"], connector["to"]):
            ends.add((side.get("swc") or side.get("composition"), side["port"]))
    delegated = set()
    for composition in model["compositions"].values():
        types = {member["name"]: member.get("type") or member.get("composition") for member in composition["members"]}
        for delegation in composition.get("delegations", []):
            ends.add((types[delegation["member"]], delegation["member_port"]))
            delegated.add((composition["name"], delegation["port"]))
    unconnected = [
        port["name"]
        for name, component in model["swc"].items()
        for port in component.get("ports", [])
        if (name, port["name"]) not in ends
    ] + [
        port["name"]
        for name, composition in model["compositions"].items()
        for port in composition.get("ports", [])
        if (name, port["name"]) not in ends or (name, port["name"]) not in delegated
    ]
    assert sorted(unconnected) == sorted(_answer("13-unconnected-ports")["mentions_all"])


def test_14_the_asil_b_components_are_the_effective_levels(tmp_path: Path) -> None:
    request = {"address": {"layer": "swc"}, "form": "index", "derived": ["effective_level"]}
    rows = run_query(request, tmp_path)["rows"]
    at_b = [row["element"] for row in rows if row["derived"]["effective_level"]["level"].partition("(")[0] == "B"]
    assert sorted(at_b) == TASKS["14-asil-b-count"]["reference"]["effective"]["B"]
    assert len(at_b) == _answer("14-asil-b-count")["number"]


def test_15_the_components_in_the_partition(tmp_path: Path) -> None:
    request = {"address": {"layer": "swc"}, "form": "index", "derived": ["partitions"]}
    placed: dict[str, list[str]] = {}
    for row in run_query(request, tmp_path)["rows"]:
        partitions = row["derived"]["partitions"]
        if partitions:
            placed.setdefault(partitions["partition"], []).append(row["element"])
    answer = _answer("15-components-in-a-partition")
    assert sorted(placed["P_Brake"]) == sorted(answer["mentions_all"])
    assert sorted(placed["P_Vehicle"]) == sorted(answer["mentions_none"])


def test_16_the_tasks_on_the_core() -> None:
    partitions = json.loads(run_cli("query", "hardware", "--where", "core=Core0", "--format", "json"))["elements"]
    tasks = [
        task
        for partition in partitions
        for task in json.loads(run_cli("query", "tasks", "--where", f"partition={partition}", "--format", "json"))[
            "elements"
        ]
    ]
    assert sorted(tasks) == sorted(_answer("16-tasks-on-a-core")["mentions_all"])


# ---------------------------------------------------------------------------
# Nothing else moved.
# ---------------------------------------------------------------------------


def _rewrite(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, path
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


def test_a_rewritten_description_on_an_element_the_task_changes_fails_naming_the_field(tmp_path: Path) -> None:
    # VehicleControl must lose its member; its description is not the task's.
    bench = bench_copy(tmp_path)
    lay_over(bench, "05-remove-component")
    composition = bench / "model" / "compositions" / "VehicleControl.yaml"
    _rewrite(composition, "description: >-", "description: >-\n  Tidied by the agent.")
    failures = CHECK.check_task(bench, CHECK.load_task("05-remove-component"))
    assert any(
        "changed outside the task" in failure and "compositions VehicleControl: description" in failure
        for failure in failures
    ), failures


def test_an_element_the_task_does_not_touch_fails_when_it_moved(tmp_path: Path) -> None:
    bench = bench_copy(tmp_path)
    lay_over(bench, "05-remove-component")
    _rewrite(bench / "model" / "hardware" / "RAM.yaml", "Illustrative showcase memory budget", "A memory budget")
    failures = CHECK.check_task(bench, CHECK.load_task("05-remove-component"))
    assert any("hardware RAM: description" in failure for failure in failures), failures


def test_a_new_element_no_assertion_allows_fails(tmp_path: Path) -> None:
    bench = bench_copy(tmp_path)
    lay_over(bench, "07-add-requirement")
    (bench / "model" / "data_types" / "units" / "Stray.yaml").write_text(
        "name: Stray\n", encoding="utf-8", newline="\n"
    )
    failures = CHECK.check_task(bench, CHECK.load_task("07-add-requirement"))
    assert any("units Stray: *" in failure for failure in failures), failures


def test_a_layer_named_without_a_name_admits_no_removal(tmp_path: Path) -> None:
    # Benchmark task 01's connector assertion names its layer only: a new connector is the task's, the
    # removal of an unrelated one is not.
    bench = bench_copy(tmp_path)
    lay_over(bench, "01-connect-two-components")
    (bench / "model" / "connectors" / "NvMToDiagnostics.yaml").unlink()
    failures = CHECK.check_task(bench, CHECK.load_task("01-connect-two-components"))
    assert any("connectors NvMToDiagnostics: *" in failure for failure in failures), failures


# ---------------------------------------------------------------------------
# The long-list rule.
# ---------------------------------------------------------------------------

LARGE_TASKS = SET_DIR / "large" / "tasks"


def _large_answer(task_id: str) -> dict[str, Any]:
    return json.loads((LARGE_TASKS / f"{task_id}.json").read_text(encoding="utf-8"))["expect"]["answer"]


PORTS = _large_answer("11-ports-of-a-component")
PORT_NAMES = PORTS["mentions_all"]
#: The large hub task 11 asks about, and the two names a range in a reply joins: the first past the
#: head of twenty, and the last.
HUB = "Swc001abcdefghij"
AFTER_HEAD, LAST = PORT_NAMES[20], PORT_NAMES[-1]


def test_the_long_lists_opt_in_and_nothing_else_does() -> None:
    opted = sorted(
        (directory.parent.name, path.stem)
        for directory in (SET_DIR / "tasks", LARGE_TASKS)
        for path in directory.glob("*.json")
        if json.loads(path.read_text(encoding="utf-8")).get("expect", {}).get("answer", {}).get("long_list")
    )
    assert opted == [
        ("agent-bench", "19-unallocated-requirements"),
        ("large", "11-ports-of-a-component"),
        ("large", "15-components-in-a-partition"),
    ]
    for task_id in ("11-ports-of-a-component", "15-components-in-a-partition"):
        assert len(_large_answer(task_id)["mentions_all"]) > CHECK.LONG_LIST_HEAD
    assert len(_answer("19-unallocated-requirements")["mentions_all"]) > CHECK.LONG_LIST_HEAD


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(f"{HUB} has 550 ports: {', '.join(PORT_NAMES)}.", id="every-name"),
        pytest.param(
            f"{HUB} has 550 ports. The first twenty: {', '.join(PORT_NAMES[:20])}, and 530 more; "
            "ask for the list to get the rest.",
            id="first-twenty-and-n-more",
        ),
        pytest.param(
            "550 ports. The first twenty:\n" + "\n".join(f"- {name}" for name in PORT_NAMES[:20]),
            id="first-twenty-as-a-bulleted-list",
        ),
    ],
)
def test_a_long_list_passes_every_name_or_the_number_and_the_first_twenty(reply: str) -> None:
    assert CHECK.check_answer(reply, PORTS) == []


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        # Short of the head: a head name is missing, refused before any range is looked for.
        pytest.param(
            f"550 ports: {', '.join(PORT_NAMES[:3])}, … and so on.", "is missing", id="pattern-short-of-the-head"
        ),
        pytest.param(f"550 ports: {PORT_NAMES[0]}–{LAST}, in order.", "is missing", id="range-short-of-the-head"),
        pytest.param(f"550 ports: {', '.join(reversed(PORT_NAMES[:20]))}.", "expected order", id="out-of-order"),
        pytest.param(f"Many ports: {', '.join(PORT_NAMES[:20])}, and more.", "number 550", id="no-number"),
    ],
)
def test_a_long_list_fails_a_pattern_a_range_or_a_short_form_missing_a_part(reply: str, reason: str) -> None:
    failures = CHECK.check_answer(reply, PORTS)
    assert any(reason in failure for failure in failures), failures


#: Every alternative of the checker's ``_JOINER``, each between two expected names after a complete
#: head, so the refusal is the range detector's own and no other check's.
JOINERS = {
    "hyphen": "-",
    "spaced-hyphen": " - ",
    "en-dash": "\u2013",
    "spaced-en-dash": " \u2013 ",
    "em-dash": "\u2014",
    "two-dots": "..",
    "three-dots": "...",
    "ellipsis": "\u2026",
    "elision": ", \u2026, ",
    "dotted-elision": ", ..., ",
    "to": " to ",
    "through": " through ",
    "thru": " thru ",
}


@pytest.mark.parametrize("joiner", list(JOINERS.values()), ids=list(JOINERS))
def test_a_long_list_with_a_complete_head_fails_on_every_range_joiner(joiner: str) -> None:
    reply = f"{HUB} has 550 ports: {', '.join(PORT_NAMES[:20])}, then {AFTER_HEAD}{joiner}{LAST}."
    failures = CHECK.check_answer(reply, PORTS)
    assert failures == [f"the answer gives a range or a pattern ('{AFTER_HEAD}{joiner}{LAST}'), not names"], failures


MARKUPS = {"bold": "**{}**", "italic": "*{}*", "code": "`{}`", "underscore": "_{}_", "strike": "~~{}~~"}


@pytest.mark.parametrize("markup", list(MARKUPS.values()), ids=list(MARKUPS))
def test_a_long_list_fails_a_range_hidden_in_markdown(markup: str) -> None:
    left, right = markup.format(AFTER_HEAD), markup.format(LAST)
    reply = f"{HUB} has 550 ports: {', '.join(PORT_NAMES[:20])}, then {left}\u2013{right}."
    failures = CHECK.check_answer(reply, PORTS)
    assert failures == [f"the answer gives a range or a pattern ('{AFTER_HEAD}\u2013{LAST}'), not names"], failures


@pytest.mark.parametrize("markup", list(MARKUPS.values()), ids=list(MARKUPS))
def test_a_long_list_whose_head_names_carry_markdown_passes(markup: str) -> None:
    reply = f"{HUB} has 550 ports, the first twenty: " + ", ".join(markup.format(name) for name in PORT_NAMES[:20])
    assert CHECK.check_answer(reply, PORTS) == []


def test_a_short_list_whose_names_carry_markdown_passes() -> None:
    answer = {"mentions_all": PORT_NAMES[:3]}
    reply = "The ports: " + ", ".join(f"_{name}_" for name in PORT_NAMES[:3])
    assert CHECK.check_answer(reply, answer) == []


def test_a_long_list_in_markdown_without_a_range_passes() -> None:
    reply = "550 ports, the first twenty:\n" + "\n".join(f"* **{name}** - a port" for name in PORT_NAMES[:20])
    assert CHECK.check_answer(reply, PORTS) == []


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(
            "550 ports, the first twenty:\n" + "\n".join(f"{name} - a port of {HUB}" for name in PORT_NAMES[:20]),
            id="name-hyphen-description",
        ),
        pytest.param(
            f"550 ports in {PORT_NAMES[0]}-style names: {', '.join(PORT_NAMES[:20])}, and 530 more.",
            id="hyphenated-word-after-a-name",
        ),
        pytest.param(
            "550 ports, the first twenty:\n" + "\n".join(f"- {name} -" for name in PORT_NAMES[:20]),
            id="dash-at-line-ends",
        ),
    ],
)
def test_a_long_list_passes_a_hyphen_that_joins_no_two_names(reply: str) -> None:
    # A newline between two names is a list, never a range: the joiner is matched on one line.
    assert CHECK.check_answer(reply, PORTS) == []


#: Template 13's list on a model where two components share a port name: the name is kept twice,
#: once per component, so it stands twice in the head, not adjacent.
SHARED_PORT_NAMES = ["pA0", "rShared", "pA2", "rShared", *(f"pB{index}" for index in range(21))]
SHARED_PORTS = {"mentions_all": SHARED_PORT_NAMES, "long_list": True}


def test_a_long_list_whose_head_repeats_a_shared_port_name_passes_the_short_form() -> None:
    reply = f"25 unconnected ports. The first twenty: {', '.join(SHARED_PORT_NAMES[:20])}, and 5 more."
    assert CHECK.long_list_failure(reply, SHARED_PORT_NAMES) is None
    assert CHECK.check_answer(reply, SHARED_PORTS) == []


def test_a_long_list_whose_head_repeats_a_name_fails_a_reply_naming_it_once() -> None:
    head = [name for index, name in enumerate(SHARED_PORT_NAMES[:21]) if index != 3]
    reply = f"25 unconnected ports. The first twenty: {', '.join(head)}, and 5 more."
    failure = CHECK.long_list_failure(reply, SHARED_PORT_NAMES)
    assert failure is not None and "rShared is missing" in failure


def test_a_long_list_s_order_is_not_taken_from_an_introductory_mention() -> None:
    # The component Target has a port Target too: the order is read past each previous name's match.
    names = ["pA0", "pA1", "Target", *(f"pB{index}" for index in range(27))]
    reply = f"Target has 30 ports. The first twenty: {', '.join(names[:20])}, and 10 more."
    assert CHECK.long_list_failure(reply, names) is None
    swapped = [names[0], names[2], names[1], *names[3:20]]
    failure = CHECK.long_list_failure(f"Target has 30 ports: {', '.join(swapped)}, and 10 more.", names)
    assert failure is not None and "not in the expected order" in failure


def test_a_long_list_whose_head_holds_two_names_one_by_the_markup_drop_passes_the_short_form() -> None:
    # ``Port`` and ``Port_`` read alike once the markup is dropped, so ``Port_`` is looked for past
    # ``Port``'s match.
    names = ["Port", "Middle", "Port_", *(f"pB{index}" for index in range(21))]
    reply = f"24 unconnected ports. The first twenty: {', '.join(names[:20])}, and 4 more."
    assert CHECK.long_list_failure(reply, names) is None
    assert CHECK.check_answer(reply, {"mentions_all": names, "long_list": True}) == []
    once = f"24 unconnected ports. The first twenty: {', '.join(names[:2] + names[3:21])}, and 4 more."
    failure = CHECK.long_list_failure(once, names)
    assert failure is not None and "Port_ is missing" in failure


def test_a_reply_naming_every_name_passes_although_it_also_gives_a_range() -> None:
    # The range refusal guards the short form only: a reply that names all 550 hides nothing.
    reply = f"550 ports, {PORT_NAMES[0]} to {LAST}: {', '.join(PORT_NAMES)}."
    assert CHECK.long_list_failure(reply, PORT_NAMES) is not None
    assert CHECK.check_answer(reply, PORTS) == []


# ---------------------------------------------------------------------------
# An explicit range of identifiers.
# ---------------------------------------------------------------------------

#: Benchmark task 19's expected names: the 31 requirements no element satisfies.
UNALLOCATED = TASKS["19-unallocated-requirements"]["expect"]["answer"]


@pytest.mark.parametrize(
    ("written", "spelled"),
    [
        ("SWR-DIA-001 to SWR-DIA-003", "SWR-DIA-001, SWR-DIA-002, SWR-DIA-003"),
        ("SWR-DIA-008 through SWR-DIA-010", "SWR-DIA-008, SWR-DIA-009, SWR-DIA-010"),
        ("SWR-DIA-008 TO SWR-DIA-010", "SWR-DIA-008, SWR-DIA-009, SWR-DIA-010"),
        ("SWR-GEN-001\u2013SWR-GEN-003", "SWR-GEN-001, SWR-GEN-002, SWR-GEN-003"),
        ("SWR-GEN-001 \u2013 SWR-GEN-002", "SWR-GEN-001, SWR-GEN-002"),
        ("SWR-GEN-001 - SWR-GEN-002", "SWR-GEN-001, SWR-GEN-002"),
        ("REQ-7 to REQ-9", "REQ-7, REQ-8, REQ-9"),
        ("A-B-C-0098 to A-B-C-0100.", "A-B-C-0098, A-B-C-0099, A-B-C-0100."),
    ],
)
def test_an_explicit_range_of_identifiers_is_spelled_out(written: str, spelled: str) -> None:
    assert CHECK.expand_id_ranges(written) == spelled


@pytest.mark.parametrize(
    "written",
    [
        pytest.param("SWR-DIA-001 to SWR-DRV-003", id="mixed-prefixes"),
        pytest.param("SWR-DIA-010 to SWR-DIA-001", id="descending"),
        pytest.param("SWR-DIA-003 to SWR-DIA-003", id="one-id"),
        pytest.param("SWR-DIA-01 to SWR-DIA-003", id="other-padding"),
        pytest.param("REQ-9 to REQ-11", id="other-digit-count"),
        pytest.param("SWR-DIA-001 to 010", id="shortened-end"),
        pytest.param("SWR-DIA: 001\u2013010", id="grouped-by-prefix"),
        pytest.param("SWR-DIA-001 to SWR-DIA-201", id="over-the-limit"),
        pytest.param("SWR-DIA-001-SWR-DIA-003", id="unspaced-hyphen"),
        pytest.param("SWR-DIA-001\u2014SWR-DIA-003", id="em-dash"),
        pytest.param("SWR-DIA-001 .. SWR-DIA-003", id="dots"),
        pytest.param("pOut000 to pOut003", id="no-hyphen-before-the-digits"),
        pytest.param("XSWR-DIA-001 to SWR-DIA-003", id="other-prefix-on-the-left"),
    ],
)
def test_a_range_read_more_than_one_way_is_left_as_written(written: str) -> None:
    assert CHECK.expand_id_ranges(written) == written


def test_the_range_limit_is_two_hundred_identifiers() -> None:
    assert CHECK.ID_RANGE_LIMIT == 200
    spelled = CHECK.expand_id_ranges("R-001 to R-200").split(", ")
    assert len(spelled) == 200 and spelled[0] == "R-001" and spelled[-1] == "R-200"


def test_task_19_passes_the_reply_that_gives_the_ids_as_ranges() -> None:
    # Agents, strong models included, write identifier lists as ranges.
    reply = (
        "31 requirements are not satisfied by any element:\n\n"
        "- SWR-BRK-005, SWR-BRK-006, SWR-BRK-007, SWR-BRK-011, SWR-BRK-012\n"
        "- SWR-DIA-001 to SWR-DIA-010 (all ten)\n"
        "- `SWR-DRV-001`\u2013`SWR-DRV-008`\n"
        "- **SWR-GEN-001 through SWR-GEN-004**\n"
        "- SWR-THP-006, SWR-THP-009 - SWR-THP-011"
    )
    assert CHECK.check_answer(reply, UNALLOCATED) == []


def test_task_19_still_fails_a_range_that_spans_a_refused_id() -> None:
    # SWR-BRK-001 is allocated: a range from it names it, and mentions_none refuses the reply.
    reply = (
        "SWR-BRK-001 to SWR-BRK-012, SWR-DIA-001 to SWR-DIA-010, SWR-DRV-001 to SWR-DRV-008, "
        "SWR-GEN-001 to SWR-GEN-004, SWR-THP-006, SWR-THP-009 to SWR-THP-011."
    )
    failures = CHECK.check_answer(reply, UNALLOCATED)
    assert failures == ["the answer names SWR-BRK-001, which is not part of the right answer"]


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param("31 requirements. SWR-DIA: 001\u2013010; SWR-DRV: 001\u2013008.", id="grouped-by-prefix"),
        pytest.param("31 requirements: SWR-BRK-005 to SWR-THP-011.", id="mixed-prefixes"),
        pytest.param("31 requirements: SWR-DIA-001 to 010, SWR-DRV-001 to 008.", id="shortened-ends"),
    ],
)
def test_task_19_still_fails_a_range_the_checker_does_not_expand(reply: str) -> None:
    assert CHECK.check_answer(reply, UNALLOCATED) != []


def test_a_small_set_question_is_unchanged_by_the_rule() -> None:
    for stem, task in TASKS.items():
        answer = task["expect"].get("answer", {})
        if stem == "19-unallocated-requirements":
            continue
        assert "long_list" not in answer, stem
        assert len(answer.get("mentions_all", [])) <= CHECK.LONG_LIST_HEAD, stem
    right, wrong = REPLIES["11-ports-of-a-component"]
    answer = TASKS["11-ports-of-a-component"]["expect"]["answer"]
    assert CHECK.check_answer(right, answer) == []
    assert CHECK.check_answer(wrong, answer)


def test_a_task_without_the_field_still_asks_for_every_name() -> None:
    without = {key: value for key, value in PORTS.items() if key != "long_list"}
    reply = f"550 ports: {', '.join(PORT_NAMES[:20])}, and 530 more."
    assert CHECK.check_answer(reply, PORTS) == []
    failures = CHECK.check_answer(reply, without)
    assert any(failure.startswith("the answer does not name") for failure in failures), failures


# ---------------------------------------------------------------------------
# The requirement questions (tasks 18-22): the expected answers are the bench's.
# ---------------------------------------------------------------------------


def _findings(rule: str) -> list[dict[str, Any]]:
    completed = subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", "check-model", "--format", "json",
         "--project", (BENCH / "swcraft.yaml").as_posix()],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )  # fmt: skip
    return [finding for finding in json.loads(completed.stdout)["findings"] if finding["rule"] == rule]


def test_18_the_elements_satisfying_the_requirement() -> None:
    document = json.loads(run_cli("query", "--dependents", "REQ_BTP_DIAGNOSTICS", "--depth", "1", "--format", "json"))
    sites = sorted(
        f"{entry['layer']} {entry['name']}" + (f" ({entry['detail']})" if entry["detail"] else "")
        for entry in document["matched"]["requirement"]
    )
    assert sites == sorted(TASKS["18-elements-satisfying-a-requirement"]["reference"]["satisfied_by"])
    sites_named = _answer("18-elements-satisfying-a-requirement")["with_layer"]
    named = {site.get("owner") or site["name"] for site in sites_named}
    assert named == {entry["name"] for entry in document["matched"]["requirement"]}


def test_19_and_20_are_check_models_findings() -> None:
    unallocated = [finding["subjects"][0]["element"] for finding in _findings("trace.requirement-unallocated")]
    assert unallocated == _answer("19-unallocated-requirements")["mentions_all"]
    stale = _findings("trace.link-needs-confirmation")
    holders = sorted(finding["subjects"][0]["element"] for finding in stale)
    assert {finding["target"] for finding in stale} == {"REQ_BTP_TEMPERATURE_RANGE"}
    assert ["REQ_BTP_TEMPERATURE_RANGE", *holders] == _answer("20-links-needing-confirmation")["mentions_all"]


def test_21_the_requirements_about_the_phrase() -> None:
    model = CHECK.read_model(BENCH)
    phrase = re.compile(r"(?<![A-Za-z0-9_])brakes?\s+temperatures?(?![A-Za-z0-9_])", re.IGNORECASE)
    words = [
        re.compile(rf"(?<![A-Za-z0-9_]){word}s?(?![A-Za-z0-9_])", re.I) for word in ("brake", "temperature", "thermal")
    ]
    texts = {rid: f"{r.get('title', '')}\n{r.get('description', '')}" for rid, r in model["requirements"].items()}
    assert (
        sorted((rid for rid, text in texts.items() if phrase.search(text)), key=str)
        == _answer("21-requirements-about-a-topic")["mentions_all"]
    )
    unrelated = sorted((rid for rid, text in texts.items() if not any(w.search(text) for w in words)), key=str)
    assert [rid for rid in unrelated if rid != "HAZ_BTP_BRAKE_FADE"] == _answer("21-requirements-about-a-topic")[
        "mentions_none"
    ]


def test_22_the_candidates_are_the_trace_searchs(tmp_path: Path) -> None:
    bench = bench_copy(tmp_path)
    completed = subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", "experiment", "trace", "--format", "json",
         "--project", (bench / "swcraft.yaml").as_posix()],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )  # fmt: skip
    entry = next(r for r in json.loads(completed.stdout)["requirements"] if r["id"] == "REQ_BTP_CORE_LOAD")
    places = {candidate["place"]: candidate for candidate in reversed(entry["candidates"]) if candidate["place"] <= 3}
    rule = _answer("22-candidates-for-a-requirement")["candidates"]
    assert [places[place]["group"]["name"] for place in sorted(places)] == rule["top"]
    for candidate in places.values():
        terms = {term for evidence in candidate["group"]["evidence"] for term in evidence.get("terms", [])}
        assert set(rule["terms"][candidate["group"]["name"]]) <= terms


# ---------------------------------------------------------------------------
# The requirement questions' answer checks.
# ---------------------------------------------------------------------------


WITH_LAYER = {"with_layer": [{"name": "NvM", "layer": "swc"}, {"name": "INvStorage", "layer": "interface"}]}


def test_a_layer_under_a_heading_counts_and_a_name_holding_the_word_does_not() -> None:
    assert CHECK.check_answer("**Components:**\n- NvM\n\nInterfaces:\n- INvStorage", WITH_LAYER) == []
    # A whole bold line is a heading.
    assert CHECK.check_answer("**Components**\n- `NvM`\n\n**Interfaces**\n- _INvStorage_", WITH_LAYER) == []
    assert CHECK.check_answer("- NvM\n- INvStorage", WITH_LAYER) == [
        "the answer names NvM but not its layer (swc)",
        "the answer names INvStorage but not its layer (interface)",
    ]
    assert CHECK.check_answer(
        "WlCsInterface010", {"with_layer": [{"name": "WlCsInterface010", "layer": "interface"}]}
    ) == ["the answer names WlCsInterface010 but not its layer (interface)"]


@pytest.mark.parametrize("heading", ["Partitions", "Memories", "Processors", "CPUs", "RAMs", "Cores"])
def test_a_hardware_plural_states_the_layer_of_every_site_under_it(heading: str) -> None:
    sites = {"with_layer": [{"name": "Part1", "layer": "hardware"}, {"name": "Part2", "layer": "hardware"}]}
    assert CHECK.check_answer(f"{heading}: Part1 and Part2", sites) == []


def test_a_member_site_is_met_by_its_owner_as_a_component() -> None:
    site = {"with_layer": [{"name": "Run", "owner": "WlProviderSwc040", "layer": "runnable"}]}
    assert CHECK.check_answer("- WlProviderSwc040 (component)", site) == []
    assert CHECK.check_answer("- WlProviderSwc040.Run (runnable)", site) == []
    assert CHECK.check_answer("- Run (runnable)", site) == ["the answer does not name WlProviderSwc040"]


def test_candidates_need_their_words() -> None:
    rule = {"candidates": {"top": ["P_Brake", "RAM"], "terms": {"P_Brake": ["core"], "RAM": ["budget"]}}}
    assert CHECK.check_answer("P_Brake (core) and RAM (budget).", rule) == []
    assert CHECK.check_answer("P_Brake (core) and RAM.", rule) == [
        "the answer gives no shared term as the reason for RAM"
    ]


# ---------------------------------------------------------------------------
# A member with its own owner (``with_owner``, the large set's task 13).
# ---------------------------------------------------------------------------

WITH_OWNER = {
    "with_owner": [
        {"owner": "SwcA", "name": "P1"},
        {"owner": "SwcB", "name": "P1"},
        {"owner": "SwcC", "name": "P15"},
    ]
}


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param("SwcA.P1, SwcB.P1, SwcC.P15", id="dotted-on-one-line"),
        pytest.param("- `SwcA`.`P1`\n- **SwcB**: P1\n- SwcC / P15", id="one-per-line-with-markup"),
        pytest.param("| Component | Port |\n|---|---|\n| SwcA | P1 |\n| SwcB | P1 |\n| SwcC | P15 |", id="a-table"),
        pytest.param("P1 (SwcA), P1 (SwcB) and P15 (SwcC)", id="member-before-its-owner"),
        pytest.param("### SwcA\n- P1\n\n**SwcB:**\n- P1\n\nSwcC:\n- P15", id="under-headings"),
    ],
)
def test_with_owner_passes_each_member_with_its_owner(reply: str) -> None:
    assert CHECK.check_answer(reply, WITH_OWNER) == []
    assert CHECK.check_answer(reply, WITH_OWNER, MODEL_OWNERS) == []


#: The model's owners with their members: SwcX holds P1 and P15 too (connected, so not expected).
MODEL_OWNERS = {
    "SwcA": frozenset({"P1"}),
    "SwcB": frozenset({"P1"}),
    "SwcC": frozenset({"P15"}),
    "SwcX": frozenset({"P1", "P15"}),
    "SwcY": frozenset({"Q1"}),
}


def test_with_owner_gives_a_qualified_mention_to_its_owner_never_to_the_heading_above() -> None:
    """Every member under its right heading, each line naming another owner, fails."""
    reply = "### SwcA\n- SwcX.P1\n\n### SwcB\n- SwcX.P1\n\n### SwcC\n- SwcX.P15"
    assert CHECK.check_answer(reply, WITH_OWNER, MODEL_OWNERS) == [
        "the answer names P1 but not as SwcA's",
        "the answer names P1 but not as SwcB's",
        "the answer names P15 but not as SwcC's",
    ]
    # Without the model the checker knows the expected owners alone: SwcX is no owner to it.
    assert CHECK.check_answer(reply, WITH_OWNER) == []
    table = "| Component | Port |\n|---|---|\n| SwcX | P1 |\n| SwcX | P1 |\n| SwcX | P15 |"
    assert CHECK.check_answer("### SwcA, SwcB, SwcC\n" + table, WITH_OWNER, MODEL_OWNERS) == [
        "the answer names P1 but not as SwcA's",
        "the answer names P1 but not as SwcB's",
        "the answer names P15 but not as SwcC's",
    ]


def test_with_owner_fails_a_member_given_to_an_owner_that_does_not_hold_it() -> None:
    reply = "SwcA.P1, SwcB.P1, SwcC.P15, SwcY.P1"
    assert CHECK.check_answer(reply, WITH_OWNER, MODEL_OWNERS) == [
        "the answer gives P1 to SwcY, which has no such member",
    ]
    # An owner that does hold the member is ``mentions_none``'s to refuse.
    assert CHECK.check_answer("SwcA.P1, SwcB.P1, SwcC.P15, SwcX.P1", WITH_OWNER, MODEL_OWNERS) == []


def test_with_owner_fails_a_member_moved_to_another_owner_though_every_name_is_there() -> None:
    assert CHECK.check_answer("SwcA.P15, SwcB.P1, SwcC.P1", WITH_OWNER) == [
        "the answer names P1 but not as SwcA's",
        "the answer names P15 but not as SwcC's",
    ]
    # The same names pass ``mentions_all``: what with_owner adds.
    assert (
        CHECK.check_answer("SwcA.P15, SwcB.P1, SwcC.P1", {"mentions_all": ["SwcA", "SwcB", "SwcC", "P1", "P15"]}) == []
    )


def test_with_owner_names_what_is_missing_and_gives_a_tie_to_neither() -> None:
    assert CHECK.check_answer("SwcA.P1, SwcC.P15", WITH_OWNER) == ["the answer does not name SwcB"]
    assert CHECK.check_answer("SwcA, SwcB, SwcC: P1, P15", WITH_OWNER) == [
        "the answer names P1 but not as SwcA's",
        "the answer names P1 but not as SwcB's",
    ]
    tie = {"with_owner": [{"owner": "SwcA", "name": "P1"}, {"owner": "SwcB", "name": "P2"}]}
    assert CHECK.check_answer("SwcA P1 SwcB, SwcB.P2", tie) == ["the answer names P1 but not as SwcA's"]
    # A line naming no owner under no heading gives its member to none.
    assert CHECK.check_answer("SwcA and SwcB.\n- P1\n- P2", tie) == [
        "the answer names P1 but not as SwcA's",
        "the answer names P2 but not as SwcB's",
    ]


#: A long ``with_owner`` list (the any-model template 13 above twenty pairs): 24 owners, every
#: member named ``P1``, so no member name tells one pair from another.
LONG_PAIRS = [{"owner": f"Swc{index:02d}", "name": "P1"} for index in range(24)]
LONG_OWNED = {"with_owner": LONG_PAIRS, "long_list": True}
#: Those owners in a model that also holds SwcY, which has no ``P1``.
LONG_OWNERS = {**{pair["owner"]: frozenset({"P1"}) for pair in LONG_PAIRS}, "SwcY": frozenset({"Q1"})}


def _dotted(pairs: list[dict[str, str]]) -> str:
    return ", ".join(f"{pair['owner']}.{pair['name']}" for pair in pairs)


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(f"24 unconnected ports: {_dotted(LONG_PAIRS)}.", id="every-pair"),
        pytest.param(
            f"There are 24 unconnected ports. The first twenty: {_dotted(LONG_PAIRS[:20])}, and 4 more.",
            id="first-twenty-and-n-more",
        ),
        pytest.param(
            "24 unconnected ports; the first twenty:\n\n"
            + "\n\n".join(f"### {pair['owner']}\n- {pair['name']}" for pair in LONG_PAIRS[:20]),
            id="first-twenty-under-headings",
        ),
    ],
)
def test_a_long_with_owner_list_passes_every_pair_or_the_number_and_the_first_twenty(reply: str) -> None:
    assert CHECK.check_answer_coded(reply, LONG_OWNED, LONG_OWNERS) == []


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        pytest.param(
            f"24 ports: {_dotted(LONG_PAIRS[:5] + LONG_PAIRS[6:21])}.",
            "Swc05's P1 is missing",
            id="a-head-pair-missing",
        ),
        pytest.param(f"24 ports: {_dotted(list(reversed(LONG_PAIRS[:20])))}.", "expected order", id="out-of-order"),
        pytest.param(f"Many ports: {_dotted(LONG_PAIRS[:20])}, and more.", "number 24", id="no-number"),
        pytest.param(f"24 ports: {_dotted(LONG_PAIRS[:20])}, Swc20.P1–Swc23.P1.", "a range", id="a-range"),
        # Owners and members in two runs: each P1 falls in the last owner's group, so no pair is given.
        pytest.param(
            "24 ports, of " + ", ".join(pair["owner"] for pair in LONG_PAIRS[:20]) + ": " + ", ".join(["P1"] * 20),
            "Swc00's P1 is missing",
            id="owners-and-members-apart",
        ),
    ],
)
def test_a_long_with_owner_list_fails_a_short_form_missing_a_part(reply: str, reason: str) -> None:
    failures = CHECK.check_answer_coded(reply, LONG_OWNED, LONG_OWNERS)
    assert [failure.code for failure in failures] == ["answer.long_list"]
    assert reason in failures[0].text


def test_a_long_with_owner_list_still_fails_a_member_given_to_an_owner_without_it() -> None:
    reply = f"24 ports. The first twenty: {_dotted(LONG_PAIRS[:20])}, and SwcY.P1."
    assert CHECK.check_answer(reply, LONG_OWNED, LONG_OWNERS) == [
        "the answer gives P1 to SwcY, which has no such member"
    ]


#: Two owners that differ only by a trailing underscore, which the markup drop takes.
TWIN_OWNERS = {"Foo": frozenset({"P1"}), "Foo_": frozenset({"P1"}), "Bar": frozenset({"Q1"})}


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param("Foo.P1", id="dotted"),
        pytest.param("- `Foo`.`P1`", id="code-spans"),
        pytest.param("- **Foo**: P1", id="bold"),
        pytest.param("- _Foo_.P1", id="emphasis"),
        pytest.param("| Component | Port |\n|---|---|\n| Foo | P1 |", id="a-table"),
        pytest.param("P1 (Foo)", id="member-before-its-owner"),
        pytest.param("### Foo\n- P1", id="under-a-heading"),
    ],
)
def test_with_owner_gives_a_mention_to_the_owner_it_spells_exactly(reply: str) -> None:
    """``Foo`` and ``Foo_`` read alike once the markup is dropped; the one a mention spells is its owner."""
    assert CHECK.check_answer(reply, {"with_owner": [{"owner": "Foo", "name": "P1"}]}, TWIN_OWNERS) == []


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param("Foo_.P1", id="dotted"),
        pytest.param("- `Foo_`.`P1`", id="code-spans"),
        pytest.param("- **Foo_**: P1", id="bold"),
        pytest.param("- Foo\\_.P1", id="escaped"),
        pytest.param("| Component | Port |\n|---|---|\n| Foo_ | P1 |", id="a-table"),
        pytest.param("P1 (Foo_)", id="member-before-its-owner"),
        pytest.param("### Foo_\n- P1", id="under-a-heading"),
    ],
)
def test_with_owner_gives_the_mirror_mention_to_the_underscored_owner(reply: str) -> None:
    assert CHECK.check_answer(reply, {"with_owner": [{"owner": "Foo_", "name": "P1"}]}, TWIN_OWNERS) == []


def test_with_owner_gives_a_mention_two_owners_read_alike_to_neither() -> None:
    """A mention neither owner spells exactly (``foo``, ``FOO_``) credits nobody, as a tie does."""
    expected = {"with_owner": [{"owner": "Foo", "name": "P1"}]}
    for reply in ("foo.P1", "FOO_.P1", "### foo\n- P1"):
        assert CHECK.check_answer(reply, expected, TWIN_OWNERS) == ["the answer names P1 but not as Foo's"]
    # With one owner of that reading, case and markup are ignored.
    lone = {"Foo": frozenset({"P1"}), "Bar": frozenset({"Q1"})}
    assert CHECK.check_answer("FOO_.P1", expected, lone) == []


def test_with_owner_reads_a_member_by_the_name_it_spells() -> None:
    """A mention is the member it spells exactly; a spelling both members read alike names neither."""
    expected = {"with_owner": [{"owner": "SwcA", "name": "Port"}, {"owner": "SwcB", "name": "Port_"}]}
    owners = {"SwcA": frozenset({"Port"}), "SwcB": frozenset({"Port_"})}
    assert CHECK.check_answer("SwcA.Port, SwcB.Port_", expected, owners) == []
    assert CHECK.check_answer("- `SwcA`.`Port`\n- **SwcB**: `Port_`", expected, owners) == []
    assert CHECK.check_answer("SwcA.Port_, SwcB.Port", expected, owners) == [
        "the answer names Port but not as SwcA's",
        "the answer gives Port to SwcB, which has no such member",
        "the answer names Port_ but not as SwcB's",
        "the answer gives Port_ to SwcA, which has no such member",
    ]
    assert CHECK.check_answer("SwcA.port, SwcB.PORT_", expected, owners) == [
        "the answer names Port but not as SwcA's",
        "the answer names Port_ but not as SwcB's",
    ]
    # A member no other one reads like is still named in any case.
    assert CHECK.check_answer("SwcA.PORT", {"with_owner": [{"owner": "SwcA", "name": "Port"}]}) == []


#: An owner that is also a member: A holds B, B holds P1; C holds Q.
CHAINED = {"with_owner": [{"owner": "A", "name": "B"}, {"owner": "B", "name": "P1"}]}
CHAINED_OWNERS = {"A": frozenset({"B"}), "B": frozenset({"P1"}), "C": frozenset({"Q"})}


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param("A.B, B.P1", id="dotted-on-one-line"),
        pytest.param("- A.B\n- B.P1", id="dotted-one-per-line"),
        pytest.param("B.P1\nA.B", id="the-owner-first-on-its-own-line"),
        pytest.param("- `A`.`B`\n- `B`.`P1`", id="code-spans"),
        pytest.param("| Component | Port |\n|---|---|\n| A | B |\n| B | P1 |", id="a-table"),
        pytest.param(
            "### C\n- Q\n\n| Component | Port |\n|---|---|\n| A | B |\n| B | P1 |", id="a-table-under-a-heading"
        ),
        pytest.param("### C\n- Q\n\n### B\n- P1\n\n### A\n- B", id="under-headings"),
        pytest.param("### C\n- Q\n\n**A:** B\n**B:** P1", id="bold-owner-lines"),
        pytest.param("### C\n- Q\n\nB (A), P1 (B)", id="member-before-its-owner"),
    ],
)
def test_with_owner_reads_an_owner_that_is_also_a_member_by_its_place(reply: str) -> None:
    assert CHECK.check_answer(reply, CHAINED, CHAINED_OWNERS) == []


def test_with_owner_still_fails_an_owner_that_is_a_member_given_to_the_wrong_owner() -> None:
    assert CHECK.check_answer("A;\nC.B, B.P1", CHAINED, CHAINED_OWNERS) == [
        "the answer names B but not as A's",
        "the answer gives B to C, which has no such member",
    ]


#: The large set's task 13 as committed: P1 on five components, P15 on a sixth.
LARGE_13 = json.loads((SET_DIR / "large" / "tasks" / "13-unconnected-ports.json").read_text(encoding="utf-8"))[
    "expect"
]["answer"]
#: Owners beside them that hold the same port names (the large bench's components all do).
LARGE_13_OWNERS = {f"Swc{index:03d}abcdefghij": frozenset({"P1", "P2", "P15"}) for index in range(1, 12)}


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(
            "Six ports are unconnected: P1 on Swc005abcdefghij, Swc006abcdefghij, Swc007abcdefghij, "
            "Swc008abcdefghij and Swc009abcdefghij; P15 on Swc010abcdefghij.",
            id="one-member-on-a-list-of-owners",
        ),
        pytest.param(
            "Port P1 of Swc005abcdefghij, Swc006abcdefghij, Swc007abcdefghij, Swc008abcdefghij, "
            "Swc009abcdefghij, and port P15 of Swc010abcdefghij.",
            id="port-of-a-list-and-port-of-one",
        ),
    ],
)
def test_with_owner_reads_a_member_shared_by_a_list_of_owners_on_the_large_task_13(reply: str) -> None:
    """A correct grouped reply passes; reading owners by raw distance scored it a failure."""
    assert CHECK.check_answer(reply, LARGE_13, LARGE_13_OWNERS) == []


#: Three pairs on two owners, each member held by its own owner alone.
THREE_PAIRS = {
    "with_owner": [
        {"owner": "SwcAlpha", "name": "pIn1"},
        {"owner": "SwcAlpha", "name": "pIn2"},
        {"owner": "SwcBeta", "name": "pOut3"},
    ]
}
THREE_PAIR_OWNERS = {"SwcAlpha": frozenset({"pIn1", "pIn2"}), "SwcBeta": frozenset({"pOut3"})}


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param("SwcAlpha: pIn1, pIn2; SwcBeta: pOut3", id="owner-first-by-semicolon"),
        pytest.param("SwcAlpha: pIn1, pIn2. SwcBeta: pOut3.", id="owner-first-by-sentence"),
        pytest.param("Unconnected: SwcAlpha (pIn1, pIn2), SwcBeta (pOut3)", id="owner-then-parentheses"),
        pytest.param("Three ports: pIn1 and pIn2 on SwcAlpha, pOut3 on SwcBeta.", id="members-before-their-owner"),
    ],
)
def test_with_owner_reads_grouped_members_by_clause(reply: str) -> None:
    assert CHECK.check_answer(reply, THREE_PAIRS, THREE_PAIR_OWNERS) == []


def test_with_owner_fails_a_member_credited_to_a_list_holding_an_owner_without_it() -> None:
    reply = "pIn1 on SwcAlpha and SwcBeta, pIn2 on SwcAlpha, pOut3 on SwcBeta"
    assert CHECK.check_answer(reply, THREE_PAIRS, THREE_PAIR_OWNERS) == [
        "the answer gives pIn1 to SwcBeta, which has no such member"
    ]
    assert CHECK.check_answer("P1 on SwcA, SwcB and SwcY; P15 on SwcC.", WITH_OWNER, MODEL_OWNERS) == [
        "the answer gives P1 to SwcY, which has no such member"
    ]


def test_a_long_with_owner_list_reads_one_member_on_a_list_of_owners_in_its_order() -> None:
    owners = ", ".join(pair["owner"] for pair in LONG_PAIRS[:20])
    reply = f"24 unconnected ports; the first twenty: P1 on {owners}."
    assert CHECK.check_answer_coded(reply, LONG_OWNED, LONG_OWNERS) == []
    backwards = ", ".join(pair["owner"] for pair in reversed(LONG_PAIRS[:20]))
    failures = CHECK.check_answer_coded(f"24 unconnected ports: P1 on {backwards}.", LONG_OWNED, LONG_OWNERS)
    assert [failure.code for failure in failures] == ["answer.long_list"]
    assert "expected order" in failures[0].text


def test_a_with_owner_list_without_the_flag_has_no_short_form() -> None:
    reply = f"There are 24 unconnected ports. The first twenty: {_dotted(LONG_PAIRS[:20])}, and 4 more."
    failures = CHECK.check_answer_coded(reply, {"with_owner": LONG_PAIRS}, LONG_OWNERS)
    assert [failure.code for failure in failures] == ["answer.with_owner"] * 4
