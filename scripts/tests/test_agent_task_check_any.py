"""``scripts/agent_task_check.py``'s rendered-task form: a task file, a baseline, coded verdicts.

Held here: the rendered form passes and fails as the committed form does and never lays a
committed ``expected/`` overlay, although it keeps the committed ids; ``may_change`` admits the
fields it names and no other; every failure carries a code of ``CODES``; ``--verdict`` writes
closed keys only; the model root is the manifest's; and the forms cannot be mixed (exit 2).

Not reached: the bindings that render the tasks, and whether a rendered task's assertions are
the right ones for its model (the right runs decide that).
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import AGENT_TASK_BENCH, REPO_ROOT, agent_task_check_module, copy_agent_task_bench

CHECK = agent_task_check_module()
SCRIPT = REPO_ROOT / "scripts" / "agent_task_check.py"
SET_DIR = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "agent-bench"
EXPECTED = SET_DIR / "expected"

#: Committed change tasks whose assertions alone admit every move their expected state makes, so
#: the rendered form (no overlay) passes on that state as the committed form does.
SELF_ALLOWING = (
    "01-connect-two-components",
    "03-new-interface-and-type",
    "04-move-runnable",
    "05-remove-component",
    "07-add-requirement",
    "23-link-a-port-to-a-requirement",
)


def _committed(task_id: str) -> dict[str, Any]:
    return CHECK.load_task(task_id)


def _rendered(task_id: str, **changes: Any) -> dict[str, Any]:
    """The committed task as the binder renders it: its format plus ``set: any`` and an oracle."""
    return {**_committed(task_id), "set": "any", "oracle": "structural", **changes}


def lay_over(bench: Path, task_id: str) -> None:
    """Lay the committed task's expected state over *bench* (the run a right agent leaves)."""
    overlay = EXPECTED / task_id
    for source in sorted(overlay.rglob("*.yaml"), key=lambda path: path.as_posix()):
        target = bench / source.relative_to(overlay)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    removed = overlay / "removed.txt"
    if removed.is_file():
        for line in removed.read_text(encoding="utf-8").splitlines():
            if line.strip():
                (bench / line.strip()).unlink()


def _write_task(tmp_path: Path, task: dict[str, Any]) -> str:
    path = tmp_path / "task.json"
    path.write_text(json.dumps(task, indent=2), encoding="utf-8", newline="\n")
    return path.as_posix()


def _reply(tmp_path: Path, text: str) -> str:
    path = tmp_path / "reply.txt"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path.as_posix()


def _main(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, SCRIPT.as_posix(), *arguments], capture_output=True, text=True, encoding="utf-8", check=False
    )


# ---------------------------------------------------------------------------
# The rendered form passes and fails as the committed one does.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("task_id", SELF_ALLOWING)
def test_a_rendered_change_task_passes_its_right_run_and_fails_the_untouched_copy(task_id: str, tmp_path: Path) -> None:
    bench = copy_agent_task_bench(tmp_path)
    lay_over(bench, task_id)
    assert CHECK.check_task(bench, _committed(task_id)) == []
    assert CHECK.check_task(bench, _rendered(task_id), baseline=AGENT_TASK_BENCH) == []
    assert CHECK.check_task(AGENT_TASK_BENCH, _rendered(task_id), baseline=AGENT_TASK_BENCH)


def test_a_new_element_no_assertion_names_is_outside_a_rendered_task(tmp_path: Path) -> None:
    """An assertion naming an element admits its creation; an element nothing names is still refused."""
    bench = copy_agent_task_bench(tmp_path)
    lay_over(bench, "07-add-requirement")
    stray = bench / "model" / "requirements" / "SWR-BRK-014.yaml"
    stray.write_text(
        "id: SWR-BRK-014\ntitle: Stray\ndescription: Not asked for.\nversion: 1\nsource: architecture\n",
        encoding="utf-8",
        newline="\n",
    )
    failures = CHECK.check_task_coded(bench, _rendered("07-add-requirement"), baseline=AGENT_TASK_BENCH)
    assert [failure.code for failure in failures] == ["outside-the-task"]
    assert "requirements SWR-BRK-014" in failures[0].text and "SWR-BRK-013" not in failures[0].text


def test_the_command_line_checks_a_rendered_task_against_its_baseline(tmp_path: Path) -> None:
    task = _write_task(tmp_path, _rendered("16-tasks-on-a-core"))
    base = (AGENT_TASK_BENCH.as_posix(), "--task-file", task, "--baseline", AGENT_TASK_BENCH.as_posix())
    passing = _main(*base, "--answer", _reply(tmp_path, "Task_100ms and Task_100ms_Low."))
    assert (passing.returncode, passing.stdout) == (0, "PASS 16-tasks-on-a-core\n")
    failing = _main(*base, "--answer", _reply(tmp_path, "Task_50ms."))
    assert failing.returncode == 1
    assert "  - the answer does not name Task_100ms, Task_100ms_Low" in failing.stdout.splitlines()


def test_a_rendered_task_with_a_committed_id_reads_no_expected_overlay(tmp_path: Path) -> None:
    """The showcase's ``expected/05-…/removed.txt`` would admit the removal; a rendered 05 never reads it."""
    bench = copy_agent_task_bench(tmp_path)
    lay_over(bench, "05-remove-component")
    bare = {"assertions": []}
    assert CHECK.check_task(bench, {**_committed("05-remove-component"), "expect": bare}) == []
    failures = CHECK.check_task_coded(bench, _rendered("05-remove-component", expect=bare), baseline=AGENT_TASK_BENCH)
    assert [failure.code for failure in failures] == ["outside-the-task"]
    assert "swc InstrumentCluster: *" in failures[0].text


# ---------------------------------------------------------------------------
# may_change.
# ---------------------------------------------------------------------------


def _rename_with(allowance: list[str]) -> dict[str, Any]:
    task = _rendered("06-rename-data-element")
    allow = {"layer": "swc", "name": "BrakeThermalProtection", "may_change": allowance}
    return {**task, "expect": {**task["expect"], "assertions": [*task["expect"]["assertions"], allow]}}


def test_may_change_admits_the_fields_it_names(tmp_path: Path) -> None:
    bench = copy_agent_task_bench(tmp_path)
    lay_over(bench, "06-rename-data-element")
    assert CHECK.check_task(bench, _rendered("06-rename-data-element"), baseline=AGENT_TASK_BENCH) == [
        "changed outside the task: swc BrakeThermalProtection: ports"
    ]
    assert CHECK.check_task(bench, _rename_with(["ports"]), baseline=AGENT_TASK_BENCH) == []


def test_may_change_admits_no_field_it_does_not_name(tmp_path: Path) -> None:
    bench = copy_agent_task_bench(tmp_path)
    lay_over(bench, "06-rename-data-element")
    assert CHECK.check_task(bench, _rename_with(["description"]), baseline=AGENT_TASK_BENCH) == [
        "changed outside the task: swc BrakeThermalProtection: ports"
    ]


def test_may_change_holds_as_an_assertion_whatever_the_element() -> None:
    model = CHECK.read_model(AGENT_TASK_BENCH)
    assert CHECK.check_assertion(model, {"layer": "swc", "name": "NoSuchComponent", "may_change": ["ports"]}) is None


def test_may_change_never_admits_a_new_element_of_its_name(tmp_path: Path) -> None:
    """An allowance moves fields of an element the baseline holds; an element it names is not created by it."""
    bench = copy_agent_task_bench(tmp_path)
    lay_over(bench, "07-add-requirement")
    (bench / "model" / "requirements" / "SWR-BRK-014.yaml").write_text(
        "id: SWR-BRK-014\ntitle: Stray\ndescription: Not asked for.\nversion: 1\nsource: architecture\n",
        encoding="utf-8",
        newline="\n",
    )
    task = _rendered("07-add-requirement")
    allow = {"layer": "requirements", "name": "SWR-BRK-014", "may_change": ["title"]}
    task = {**task, "expect": {**task["expect"], "assertions": [*task["expect"]["assertions"], allow]}}
    failures = CHECK.check_task_coded(bench, task, baseline=AGENT_TASK_BENCH)
    assert [failure.code for failure in failures] == ["outside-the-task"]
    assert "requirements SWR-BRK-014: *" in failures[0].text


@pytest.mark.parametrize(
    ("allowance", "message"),
    [
        pytest.param({"may_change": []}, "names no field", id="empty"),
        pytest.param({"may_change": ["ports"], "has": {"ports": []}}, "carries a content check", id="with-has"),
        pytest.param({"may_change": ["ports"], "absent": True}, "carries a content check", id="with-absent"),
        pytest.param(
            {"may_change": ["ports"], "refers": {"ports": {"layer": "interfaces"}}},
            "carries a content check",
            id="with-refers",
        ),
    ],
)
def test_a_may_change_that_would_weaken_the_task_is_refused(
    allowance: dict[str, Any], message: str, tmp_path: Path
) -> None:
    task = _rendered("06-rename-data-element")
    nested = {"any_of": [{"layer": "swc", "name": "BrakeThermalProtection", **allowance}]}
    task = {**task, "expect": {**task["expect"], "assertions": [*task["expect"]["assertions"], nested]}}
    verdict = tmp_path / "verdict.json"
    completed = _main(
        AGENT_TASK_BENCH.as_posix(), "--task-file", _write_task(tmp_path, task),
        "--baseline", AGENT_TASK_BENCH.as_posix(), "--verdict", verdict.as_posix(),
    )  # fmt: skip
    assert completed.returncode == 2
    assert message in completed.stderr
    assert not verdict.exists()


# ---------------------------------------------------------------------------
# refers: a property stated by reference.
# ---------------------------------------------------------------------------

#: A type that states its range by reference, the constraint it names, and one that states nothing.
REFERRING = {
    "application_types": {
        "Ref": {"name": "Ref", "kind": "application_type", "data_constr_ref": "Limits"},
        "Bare": {"name": "Bare", "kind": "application_type"},
        "Dangling": {"name": "Dangling", "kind": "application_type", "data_constr_ref": "Nowhere"},
    },
    "data_constraints": {"Limits": {"name": "Limits", "min": 0, "max": 100}},
}


@pytest.mark.parametrize(
    ("name", "target", "failure"),
    [
        pytest.param("Ref", {"layer": "data_constraints", "has": {"min": 0, "max": 100}}, None, id="holds"),
        pytest.param("Ref", {"layer": "data_constraints"}, None, id="any-element"),
        pytest.param("Ref", {"layer": "data_constraints", "has": {"max": 255}}, "which does not contain", id="limits"),
        pytest.param("Dangling", {"layer": "data_constraints"}, "does not hold", id="dangling"),
        pytest.param("Bare", {"layer": "data_constraints"}, "states no data_constr_ref", id="absent"),
        pytest.param("Bare", {"layer": "data_constraints", "optional": True}, None, id="optional"),
    ],
)
def test_refers_holds_where_the_field_names_an_element_of_the_layer_that_contains_has(
    name: str, target: dict[str, Any], failure: str | None
) -> None:
    assertion = {"layer": "application_types", "name": name, "refers": {"data_constr_ref": target}}
    result = CHECK.check_assertion(REFERRING, assertion)
    assert result is None if failure is None else failure in str(result)


def test_refers_admits_the_new_element_its_field_names_and_no_other() -> None:
    baseline = {"application_types": {}, "data_constraints": {}}
    assertion = {
        "layer": "application_types",
        "name": "Ref",
        "refers": {"data_constr_ref": {"layer": "data_constraints"}},
    }
    run = {**REFERRING, "data_constraints": {**REFERRING["data_constraints"], "Stray": {"name": "Stray"}}}
    moved = CHECK.unrequested_changes(run, baseline, None, [{"any_of": [assertion]}])
    assert moved == [
        "application_types Bare: *",
        "application_types Dangling: *",
        "data_constraints Stray: *",
    ]


#: A type that names its conversion, the method it names, and the unit the method names.
CONVERTING = {
    "application_types": {"Conv": {"name": "Conv", "kind": "application_type", "compu_method_ref": "Lin"}},
    "compu_methods": {"Lin": {"name": "Lin", "kind": "linear", "unit": "Pct"}},
    "units": {"Pct": {"name": "Pct"}},
}
NESTED = {
    "layer": "application_types",
    "name": "Conv",
    "refers": {"compu_method_ref": {"layer": "compu_methods", "refers": {"unit": {"layer": "units"}}}},
}


def test_a_targets_own_refers_is_checked_on_the_element_its_field_names() -> None:
    assert CHECK.check_assertion(CONVERTING, NESTED) is None
    dangling = {**CONVERTING, "units": {}}
    assert "Lin's unit names Pct, which units does not hold" in str(CHECK.check_assertion(dangling, NESTED))


def test_a_targets_own_refers_admits_the_new_element_it_names_and_no_other() -> None:
    baseline = {"application_types": {}, "compu_methods": {}, "units": {}}
    run = {
        **CONVERTING,
        "compu_methods": {**CONVERTING["compu_methods"], "Stray": {"name": "Stray", "kind": "linear", "unit": "Lone"}},
        "units": {**CONVERTING["units"], "Lone": {"name": "Lone"}},
    }
    moved = CHECK.unrequested_changes(run, baseline, None, [NESTED])
    assert moved == ["compu_methods Stray: *", "units Lone: *"]


# ---------------------------------------------------------------------------
# Coded failures and the verdict file.
# ---------------------------------------------------------------------------


def _literal_codes() -> set[str]:
    """Every code a ``Failure(...)`` call in the checker spells as a literal."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    return {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Failure"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }


def test_every_code_the_checker_spells_is_one_of_codes_and_each_is_spelt() -> None:
    assert _literal_codes() == set(CHECK.CODES)
    assert len(CHECK.CODES) == len(set(CHECK.CODES)) == 18


def test_the_failures_of_a_broken_run_carry_their_codes(tmp_path: Path) -> None:
    bench = copy_agent_task_bench(tmp_path)
    lay_over(bench, "06-rename-data-element")
    controller = bench / "model" / "swc" / "application" / "BrakeThermalProtection.yaml"
    text = controller.read_text(encoding="utf-8")
    controller.write_text(text.replace("element: brakeRequest", "element: request"), encoding="utf-8", newline="\n")
    codes = {failure.code for failure in CHECK.check_task_coded(bench, _rendered("06-rename-data-element"))}
    assert codes == {"assertion", "outside-the-task", "check-model"}
    question = _rendered("16-tasks-on-a-core")
    failures = CHECK.check_task_coded(bench, question, answer="Task_50ms.", baseline=AGENT_TASK_BENCH)
    assert [failure.code for failure in failures] == ["answer.mentions_all", "answer.mentions_none", "model-changed"]


def test_every_answer_check_has_its_own_code() -> None:
    expected = {
        "mentions_all": ["Alpha"],
        "mentions_none": ["Beta"],
        "number": 7,
        "first_of": {"candidates": ["Gamma", "Delta"], "expected": "Gamma"},
        "with_layer": [{"name": "Epsilon", "layer": "swc"}],
        "with_owner": [{"owner": "Eta", "name": "Theta"}],
        "candidates": {"top": ["Zeta"], "terms": {"Zeta": ["brake"]}},
    }
    codes = [failure.code for failure in CHECK.check_answer_coded("Beta, then Delta and Gamma.", expected)]
    assert codes == [
        "answer.mentions_all",
        "answer.mentions_none",
        "answer.number",
        "answer.with_layer",
        "answer.with_owner",
        "answer.candidates",
        "answer.first_of",
    ]
    names = [f"Name{index:02d}" for index in range(25)]
    long_list = CHECK.check_answer_coded("Name00 only.", {"mentions_all": names, "long_list": True})
    assert [failure.code for failure in long_list] == ["answer.long_list"]


def test_the_verdict_file_has_closed_keys_only(tmp_path: Path) -> None:
    task = _write_task(tmp_path, _rendered("16-tasks-on-a-core"))
    verdict = tmp_path / "verdict.json"
    base = (AGENT_TASK_BENCH.as_posix(), "--task-file", task, "--baseline", AGENT_TASK_BENCH.as_posix())
    assert _main(*base, "--answer", _reply(tmp_path, "Task_50ms."), "--verdict", verdict.as_posix()).returncode == 1
    assert json.loads(verdict.read_text(encoding="utf-8")) == {
        "result": "FAIL",
        "codes": ["answer.mentions_all", "answer.mentions_none"],
    }
    right = _reply(tmp_path, "Task_100ms and Task_100ms_Low.")
    assert _main(*base, "--answer", right, "--verdict", verdict.as_posix()).returncode == 0
    assert json.loads(verdict.read_text(encoding="utf-8")) == {"result": "PASS", "codes": []}


def test_the_committed_form_writes_a_verdict_too(tmp_path: Path) -> None:
    verdict = tmp_path / "verdict.json"
    completed = _main(
        AGENT_TASK_BENCH.as_posix(), "16-tasks-on-a-core",
        "--answer", _reply(tmp_path, "Task_100ms, Task_100ms_Low."), "--verdict", verdict.as_posix(),
    )  # fmt: skip
    assert completed.returncode == 0
    assert json.loads(verdict.read_text(encoding="utf-8")) == {"result": "PASS", "codes": []}


# ---------------------------------------------------------------------------
# The model root is the manifest's.
# ---------------------------------------------------------------------------


def _elsewhere(tmp_path: Path, name: str) -> Path:
    """A copy of the bench whose model lives under ``architecture/``, as its manifest says."""
    bench = copy_agent_task_bench(tmp_path / name)
    (bench / "model").rename(bench / "architecture")
    manifest = bench / "swcraft.yaml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text.replace("model_root: model", "model_root: architecture"), encoding="utf-8", newline="\n")
    return bench


def test_a_project_whose_model_root_is_not_model_is_read(tmp_path: Path) -> None:
    bench = _elsewhere(tmp_path, "run")
    baseline = _elsewhere(tmp_path, "baseline")
    assert CHECK.read_model(bench) == CHECK.read_model(AGENT_TASK_BENCH)
    question = _rendered("16-tasks-on-a-core")
    assert CHECK.check_task(bench, question, answer="Task_100ms and Task_100ms_Low.", baseline=baseline) == []
    lay_over_root = bench / "architecture" / "swc" / "application"
    lay_over_root.joinpath("BrakeThermalProtection.yaml").write_text(
        (
            EXPECTED / "06-rename-data-element" / "model" / "swc" / "application" / "BrakeThermalProtection.yaml"
        ).read_text(encoding="utf-8"),
        encoding="utf-8",
        newline="\n",
    )
    assert CHECK.check_task(bench, question, answer="Task_100ms and Task_100ms_Low.", baseline=baseline) == [
        "the model changed: swc BrakeThermalProtection"
    ]


def test_a_committed_overlay_is_laid_over_a_baseline_whose_root_is_the_manifests(tmp_path: Path) -> None:
    """The committed form's overlay copies the baseline's model by its manifest, not by a literal ``model/``."""
    baseline = copy_agent_task_bench(tmp_path / "baseline")
    bench = copy_agent_task_bench(tmp_path / "run")
    lay_over(bench, "06-rename-data-element")
    assert CHECK.check_task(bench, _committed("06-rename-data-element"), baseline=baseline) == []


def test_the_sentences_reach_a_pipe_as_utf8_whatever_the_locale(tmp_path: Path) -> None:
    """The checker writes UTF-8 to a pipe even in a ``cp1252`` locale.

    A name outside that code page would otherwise be an encoding error.
    """
    name = "Task_\u2202"
    task = _write_task(tmp_path, _rendered("16-tasks-on-a-core", expect={"model": "unchanged",
                       "answer": {"mentions_all": [name]}}))  # fmt: skip
    completed = subprocess.run(
        [sys.executable, SCRIPT.as_posix(), AGENT_TASK_BENCH.as_posix(), "--task-file", task,
         "--baseline", AGENT_TASK_BENCH.as_posix(), "--answer", _reply(tmp_path, "None.")],
        capture_output=True, text=True, encoding="utf-8", check=False,
        env={**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"},
    )  # fmt: skip
    assert completed.returncode == 1, completed.stderr
    assert f"the answer does not name {name}" in completed.stdout


# ---------------------------------------------------------------------------
# What cannot be checked (exit 2).
# ---------------------------------------------------------------------------


def test_a_task_file_with_a_task_id_exits_2(tmp_path: Path) -> None:
    task = _write_task(tmp_path, _rendered("16-tasks-on-a-core"))
    completed = _main(
        AGENT_TASK_BENCH.as_posix(),
        "16-tasks-on-a-core",
        "--task-file",
        task,
        "--baseline",
        AGENT_TASK_BENCH.as_posix(),
    )
    assert completed.returncode == 2
    assert "give no TASK_ID and no --set" in completed.stderr


def test_a_task_file_with_a_set_exits_2(tmp_path: Path) -> None:
    task = _write_task(tmp_path, _rendered("16-tasks-on-a-core"))
    completed = _main(
        AGENT_TASK_BENCH.as_posix(), "--set", "large", "--task-file", task, "--baseline", AGENT_TASK_BENCH.as_posix()
    )
    assert completed.returncode == 2
    assert "give no TASK_ID and no --set" in completed.stderr


def test_an_unreadable_answer_exits_2_naming_its_class_alone(tmp_path: Path) -> None:
    task = _write_task(tmp_path, _rendered("16-tasks-on-a-core"))
    answer = tmp_path / "zqreply.txt"
    answer.write_bytes(b"\xff\xfe\xfa not utf-8")
    verdict = tmp_path / "verdict.json"
    completed = _main(
        AGENT_TASK_BENCH.as_posix(), "--task-file", task, "--baseline", AGENT_TASK_BENCH.as_posix(),
        "--answer", answer.as_posix(), "--verdict", verdict.as_posix(),
    )  # fmt: skip
    assert completed.returncode == 2
    assert "the answer file cannot be read: UnicodeDecodeError" in completed.stderr
    assert "zqreply" not in completed.stderr
    assert not verdict.exists()


def test_a_rendered_question_without_an_answer_exits_2_and_writes_no_verdict(tmp_path: Path) -> None:
    task = _write_task(tmp_path, _rendered("16-tasks-on-a-core"))
    verdict = tmp_path / "verdict.json"
    completed = _main(
        AGENT_TASK_BENCH.as_posix(), "--task-file", task, "--baseline", AGENT_TASK_BENCH.as_posix(),
        "--verdict", verdict.as_posix(),
    )  # fmt: skip
    assert completed.returncode == 2
    assert "give the agent's reply with --answer FILE" in completed.stderr
    assert not verdict.exists()


@pytest.mark.parametrize("broken", ["element", "manifest", "manifest-yaml"])
def test_a_run_that_leaves_a_model_the_loader_refuses_fails_with_a_verdict(broken: str, tmp_path: Path) -> None:
    """The run's own copy unreadable is the run's FAIL (``unreadable-model``), never a run the runner cannot check."""
    bench = copy_agent_task_bench(tmp_path)
    if broken == "element":
        victim = sorted((bench / "model" / "swc").rglob("*.yaml"), key=lambda path: path.as_posix())[0]
        victim.write_text("name: ZqBroken\nports: zqsentinel\n", encoding="utf-8", newline="\n")
    elif broken == "manifest":
        (bench / "swcraft.yaml").unlink()
    else:
        # A manifest libyaml refuses: ``yaml.YAMLError``, neither a ``ValueError`` nor an ``OSError``.
        (bench / "swcraft.yaml").write_text("model_root: [zqsentinel\n", encoding="utf-8", newline="\n")
    verdict = tmp_path / "verdict.json"
    completed = _main(
        bench.as_posix(), "--task-file", _write_task(tmp_path, _rendered("04-move-runnable")),
        "--baseline", AGENT_TASK_BENCH.as_posix(), "--verdict", verdict.as_posix(),
    )  # fmt: skip
    assert completed.returncode == 1, completed.stderr
    assert json.loads(verdict.read_text(encoding="utf-8")) == {"result": "FAIL", "codes": ["unreadable-model"]}
    assert "zqsentinel" not in completed.stdout + completed.stderr
    assert "the run left a model the loader refuses" in completed.stdout


@pytest.mark.parametrize("baseline", ["missing", "empty"])
def test_a_missing_baseline_exits_2(tmp_path: Path, baseline: str) -> None:
    task = _write_task(tmp_path, _rendered("16-tasks-on-a-core"))
    directory = tmp_path / baseline
    if baseline == "empty":
        directory.mkdir()
    verdict = tmp_path / "verdict.json"
    completed = _main(
        AGENT_TASK_BENCH.as_posix(), "--task-file", task, "--baseline", directory.as_posix(),
        "--answer", _reply(tmp_path, "Task_100ms."), "--verdict", verdict.as_posix(),
    )  # fmt: skip
    assert completed.returncode == 2
    assert "no baseline to compare with" in completed.stderr
    assert not verdict.exists()


def test_a_task_file_without_a_baseline_exits_2(tmp_path: Path) -> None:
    task = _write_task(tmp_path, _rendered("16-tasks-on-a-core"))
    completed = _main(AGENT_TASK_BENCH.as_posix(), "--task-file", task)
    assert completed.returncode == 2
    assert "go together" in completed.stderr


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "the task file cannot be read: FileNotFoundError"),
        ("{not json", "the task file cannot be read: JSONDecodeError"),
        ("[1, 2]", "the task file is not a task"),
        (json.dumps({"id": "16-tasks-on-a-core", "kind": "question", "expect": {}}), "is not a rendered task"),
    ],
)
def test_an_unreadable_task_file_exits_2(tmp_path: Path, content: str | None, message: str) -> None:
    path = tmp_path / "task.json"
    if content is not None:
        path.write_text(content, encoding="utf-8", newline="\n")
    completed = _main(
        AGENT_TASK_BENCH.as_posix(), "--task-file", path.as_posix(), "--baseline", AGENT_TASK_BENCH.as_posix()
    )
    assert completed.returncode == 2
    assert message in completed.stderr


def test_neither_form_exits_2() -> None:
    completed = _main(AGENT_TASK_BENCH.as_posix())
    assert completed.returncode == 2
    assert "give a TASK_ID, or --task-file with --baseline" in completed.stderr


# ---------------------------------------------------------------------------
# Names whose underscores the Markdown drop would take (``Swc_``, ``Brake__Ctl``).
# ---------------------------------------------------------------------------


def _answer_names(answer: dict[str, Any]) -> list[str]:
    """Every name an answer check of *answer* looks for."""
    names = [*answer.get("mentions_all", []), *answer.get("mentions_none", [])]
    names += [site.get("owner") or site["name"] for site in answer.get("with_layer", [])]
    names += [part for pair in answer.get("with_owner", []) for part in (pair["owner"], pair["name"])]
    if "candidates" in answer:
        names += [*answer["candidates"]["top"], *(t for terms in answer["candidates"]["terms"].values() for t in terms)]
    if "first_of" in answer:
        names += answer["first_of"]["candidates"]
    return names


def test_every_committed_name_is_its_own_bare_form() -> None:
    """The committed sets' verdicts cannot move: the drop applied to their expected names is the identity."""
    names = [
        name
        for tasks_dir, _ in CHECK.SETS.values()
        for path in sorted(tasks_dir.glob("*.json"), key=lambda path: path.as_posix())
        for name in _answer_names(json.loads(path.read_text(encoding="utf-8"))["expect"].get("answer", {}))
    ]
    assert names, "the committed sets name what their answers look for"
    assert [name for name in names if CHECK._bare(name) != name] == []


@pytest.mark.parametrize(
    "reply",
    [
        "The ports are Brake__Ctl and Swc_.",
        "The ports are `Brake__Ctl` and **Swc_**.",
        "The ports are _Brake__Ctl_ and `Swc_`",
    ],
)
def test_a_name_with_a_trailing_or_doubled_underscore_is_named(reply: str) -> None:
    assert CHECK.check_answer_coded(reply, {"mentions_all": ["Brake__Ctl", "Swc_"]}) == []
    assert (
        CHECK.check_answer_coded(reply, {"first_of": {"candidates": ["Swc_", "Brake__Ctl"], "expected": "Brake__Ctl"}})
        == []
    )
    failures = CHECK.check_answer_coded(reply, {"mentions_none": ["Swc_"]})
    assert [failure.code for failure in failures] == ["answer.mentions_none"]
    sites = [{"name": "Brake__Ctl", "layer": "port", "owner": "Swc_"}]
    assert CHECK.check_answer_coded(f"Component Swc_ has it.\n{reply}", {"with_layer": sites}) == []


def test_the_long_list_short_form_reads_names_with_trailing_and_doubled_underscores() -> None:
    names = [f"p__{index:02d}_" for index in range(25)]
    short = f"There are {len(names)}: " + ", ".join(f"`{name}`" for name in names[:20]) + ", and 5 more."
    assert CHECK.check_answer_coded(short, {"mentions_all": names, "long_list": True}) == []
    joined = f"There are {len(names)}: {names[0]} - {names[24]}"
    assert [f.code for f in CHECK.check_answer_coded(joined, {"mentions_all": names, "long_list": True})] == [
        "answer.long_list"
    ]


@pytest.mark.parametrize(
    "reply",
    [
        "The names are Brake\\_Ctl and pOut\\_001.",
        "The names are **Brake\\_Ctl** and `pOut\\_001`.",
        "The names are Brake_Ctl and pOut_001.",
    ],
)
def test_an_escaped_underscore_reads_as_the_literal_one(reply: str) -> None:
    assert CHECK.check_answer_coded(reply, {"mentions_all": ["Brake_Ctl", "pOut_001"]}) == []
    failures = CHECK.check_answer_coded(reply, {"mentions_none": ["pOut_001"]})
    assert [failure.code for failure in failures] == ["answer.mentions_none"]
    assert CHECK._bare("Brake\\_Ctl") == "Brake_Ctl", "an expected name is read with the same escape"
    assert CHECK.check_answer_coded("Swc\\_ is it.", {"mentions_all": ["Swc_"]}) == []


@pytest.mark.parametrize("written", ["1,001", "1 001", "1 001", "1 001", "1'001", "1001"])
def test_a_count_with_a_thousands_separator_is_stated(written: str) -> None:
    names = [f"pOut{index:04d}" for index in range(1001)]
    short = f"There are {written} unconnected ports; the first twenty: " + ", ".join(names[:20]) + "."
    assert CHECK.check_answer_coded(short, {"mentions_all": names, "long_list": True}) == []
    assert CHECK.check_answer_coded(f"It has {written} ports.", {"number": 1001}) == []


@pytest.mark.parametrize("written", ["11,001", "1,001,5", "1,0001", "21 001", "1.001", "10,01"])
def test_a_grouped_number_inside_another_states_no_count(written: str) -> None:
    failures = CHECK.check_answer_coded(f"It has {written} ports.", {"number": 1001})
    assert [failure.code for failure in failures] == ["answer.number"]


def test_a_large_count_groups_every_three_digits() -> None:
    assert CHECK.states_number("a total of 1,234,567 ports", 1234567)
    assert not CHECK.states_number("a total of 1,234567 ports", 1234567), "one separator throughout"
    assert not CHECK.states_number("1,001", 101)


@pytest.mark.parametrize("word", ["To", "through", "THRU"])
@pytest.mark.parametrize("markup", ["{}", "`{}`"], ids=["plain", "code"])
def test_a_name_that_is_a_joiner_word_is_a_name_never_a_range(word: str, markup: str) -> None:
    names = ["pFirst", word, "pSecond", *(f"pOut{index:03d}" for index in range(27))]
    short = f"There are {len(names)}: " + ", ".join(markup.format(name) for name in names[:20]) + "."
    assert CHECK.check_answer_coded(short, {"mentions_all": names, "long_list": True}) == []
    # The symbol joiners still read a range, the joiner word's own name included.
    joined = short.rstrip(".") + f", then {names[20]} - {names[29]}."
    assert [f.code for f in CHECK.check_answer_coded(joined, {"mentions_all": names, "long_list": True})] == [
        "answer.long_list"
    ]
    other = next(joiner for joiner in ("to", "through") if joiner != word.casefold())
    prose = short.rstrip(".") + f", then {names[20]} {other} {names[29]}."
    assert [f.code for f in CHECK.check_answer_coded(prose, {"mentions_all": names, "long_list": True})] == [
        "answer.long_list"
    ]
