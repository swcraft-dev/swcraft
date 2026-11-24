"""The agent task benchmark's task set, checked on the large bench (marked ``slow``).

As ``test_agent_task_check.py`` holds the showcase set: the bench's per-layer counts and error-free
state, each change task passes on its expected state and fails on the untouched bench, each question
passes a right reply and fails a wrong one, and each expected answer is recomputed from the bench.

The any-model set on the same bench: every template binds or is not applicable by its code; a
question's answer equals the committed large task's where the binding picks the committed target,
else the CLI's recomputation (``CLI_ORACLES``); each change template's right run passes and the
untouched copy fails. The ``check-model`` runs take minutes, hence ``slow``.

Not reached: whether the bench resembles a real project beyond its counts (its wiring is round
robin by construction), and anything an agent does.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT, agent_task_bind_module, agent_task_check_module
from test_agent_task_bind import CLI_ORACLES, memory_blocks

pytestmark = pytest.mark.slow

CHECK = agent_task_check_module()
BIND = agent_task_bind_module()
LARGE = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "agent-bench" / "large"
TASKS_DIR = LARGE / "tasks"
EXPECTED = LARGE / "expected"
SHOWCASE_TASKS = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "agent-bench" / "tasks"

#: Elements per layer of the large bench, as `load_model_state` reads it.
LARGE_COUNTS = {
    "swc": 150,
    "interfaces": 1200,
    "connectors": 1453,
    "compositions": 1,
    "tasks": 32,
    "sequences": 30,
    "requirements": 20,
    "hardware": 13,
    "application_types": 426,
    "implementation_types": 284,
    "base_types": 142,
    "mapping_sets": 1,
}


def _tasks() -> dict[str, dict[str, Any]]:
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(TASKS_DIR.glob("*.json"), key=lambda path: path.as_posix())
    }


TASKS = _tasks()
CHANGES = sorted((stem for stem, task in TASKS.items() if task["kind"] == "change"), key=str)
QUESTIONS = sorted((stem for stem, task in TASKS.items() if task["kind"] == "question"), key=str)


@pytest.fixture(scope="module")
def large(large_bench: Path) -> Path:
    """``conftest.py``'s session bench, shared with ``test_benchmark_questions_large.py``."""
    return large_bench


def _copy(bench: Path, tmp_path: Path) -> Path:
    target = tmp_path / "run"
    shutil.copytree(bench, target)
    return target


def _lay_over(bench: Path, task_id: str) -> None:
    overlay = EXPECTED / task_id
    for source in sorted(overlay.rglob("*.yaml"), key=lambda path: path.as_posix()):
        target = bench / source.relative_to(overlay)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    removed = overlay / "removed.txt"
    if removed.is_file():
        for line in removed.read_text(encoding="utf-8").splitlines():
            (bench / line).unlink()


def _cli(bench: Path, *arguments: str) -> str:
    return subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", *arguments, "--project", (bench / "swcraft.yaml").as_posix()],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout  # fmt: skip


def _query(bench: Path, request: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(request), encoding="utf-8", newline="\n")
    return json.loads(_cli(bench, "query", "--request", request_file.as_posix(), "--format", "json"))


def _answer(task_id: str) -> dict[str, Any]:
    return TASKS[task_id]["expect"]["answer"]


# ---------------------------------------------------------------------------
# The bench.
# ---------------------------------------------------------------------------


def test_the_large_bench_has_a_real_projects_size(large: Path) -> None:
    state = CHECK.read_model(large)
    assert {layer: len(state[layer]) for layer in LARGE_COUNTS} == LARGE_COUNTS
    ports = {name: len(element.get("ports", [])) for name, element in state["swc"].items()}
    assert max(ports, key=lambda name: ports[name]) == "Swc001abcdefghij"
    assert ports["Swc001abcdefghij"] == 550


def test_the_large_bench_has_no_error(large: Path) -> None:
    assert CHECK.check_model_errors(large) == []


def test_the_import_task_reads_an_export_the_large_bench_carries(large: Path) -> None:
    out = _cli(
        large, "import", "component", "--source", (large / "imports" / "WlLampController.arxml").as_posix(), "--dry-run"
    )
    assert "SWC WlLampController: created" in out


# ---------------------------------------------------------------------------
# The set: the showcase's tasks, bound to the large bench.
# ---------------------------------------------------------------------------


#: The showcase's access-form tasks the large set does not carry: no runnable of the large bench
#: calls an operation, so 36 has no target there; the any-model set reaches both on any model.
SHOWCASE_ONLY = frozenset({"35-make-a-read-implicit", "36-call-asynchronously"})


def test_the_large_set_is_the_showcase_sets_tasks() -> None:
    # The guide questions are the same on any bench, so the showcase set alone carries them.
    showcase = {
        path.stem: task
        for path in SHOWCASE_TASKS.glob("*.json")
        if (task := json.loads(path.read_text(encoding="utf-8")))["kind"] != "guide" and path.stem not in SHOWCASE_ONLY
    }
    assert sorted(TASKS, key=str) == sorted(showcase, key=str)
    for stem, task in TASKS.items():
        assert (task["task"], task["group"], task["kind"], task["status"]) == (
            showcase[stem]["task"], showcase[stem]["group"], showcase[stem]["kind"], "ready",
        )  # fmt: skip
        assert set(task["expect"]) == set(showcase[stem]["expect"])
    assert sorted((path.name for path in EXPECTED.iterdir()), key=str) == CHANGES


@pytest.mark.parametrize("task_id", CHANGES)
def test_a_large_change_task_passes_on_its_expected_state_and_fails_untouched(
    task_id: str, large: Path, tmp_path: Path
) -> None:
    task = CHECK.load_task(task_id, TASKS_DIR)
    run = _copy(large, tmp_path)
    _lay_over(run, task_id)
    assert CHECK.check_task(run, task, baseline=large, expected_dir=EXPECTED) == []
    assert CHECK.check_task(large, task, baseline=large, expected_dir=EXPECTED)


def _replies(task_id: str) -> tuple[str, str]:
    """A right reply built from the expected answer, and a wrong one missing what it requires."""
    answer = _answer(task_id)
    if "first_of" in answer:
        rule = answer["first_of"]
        wrong = next(candidate for candidate in rule["candidates"] if candidate != rule["expected"])
        return f"{rule['expected']}, then {wrong}.", f"{wrong}, then {rule['expected']}."
    if "with_layer" in answer:
        sites = answer["with_layer"]
        lines = [f"- {site.get('owner') or site['name']} ({site['layer']})" for site in sites]
        return "\n".join(lines), "\n".join([f"- {sites[0].get('owner') or sites[0]['name']}", *lines[1:]])
    if "candidates" in answer:
        rule = answer["candidates"]
        lines = [f"- {name}: shares the term {rule['terms'][name][0]}" for name in rule["top"]]
        return "\n".join(lines), "\n".join(lines[1:])
    if "with_owner" in answer:
        lines = [f"- {pair['owner']}.{pair['name']}" for pair in answer["with_owner"]]
        return "\n".join(lines), "\n".join(lines[1:])
    named = ", ".join(answer.get("mentions_all", []))
    number = f"{answer['number']}: " if "number" in answer else ""
    right = f"{number}{named}."
    if "number" in answer:
        return right, f"{answer['number'] + 1}: {named}."
    return right, f"{', '.join(answer['mentions_all'][1:])}."


@pytest.mark.parametrize("task_id", QUESTIONS)
def test_a_large_question_passes_the_right_reply_and_fails_the_wrong_one(task_id: str, large: Path) -> None:
    right, wrong = _replies(task_id)
    task = CHECK.load_task(task_id, TASKS_DIR)
    assert CHECK.check_task(large, task, answer=right, baseline=large, expected_dir=EXPECTED) == []
    assert CHECK.check_task(large, task, answer=wrong, baseline=large, expected_dir=EXPECTED)


def test_the_command_line_checks_the_large_set(large: Path, tmp_path: Path) -> None:
    reply = tmp_path / "reply.txt"
    reply.write_text(_replies("16-tasks-on-a-core")[0], encoding="utf-8", newline="\n")
    completed = subprocess.run(
        [sys.executable, (REPO_ROOT / "scripts" / "agent_task_check.py").as_posix(), large.as_posix(),
         "16-tasks-on-a-core", "--answer", reply.as_posix(), "--set", "large"],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )  # fmt: skip
    assert (completed.returncode, completed.stdout) == (0, "PASS 16-tasks-on-a-core\n"), completed.stderr


# ---------------------------------------------------------------------------
# The expected answers are the large bench's.
# ---------------------------------------------------------------------------


def test_08_the_direct_users(large: Path) -> None:
    document = json.loads(_cli(large, "impact", "If0168abcdefghij", "--depth", "1", "--format", "json"))
    names = sorted(entry["name"] for entries in document["matched"].values() for entry in entries)
    assert names == _answer("08-interface-change-impact")["mentions_all"]
    assert "Swc001abcdefghij" in names


def test_09_and_17_are_the_resource_reports(large: Path) -> None:
    report = _cli(large, "resource-report")
    me1 = report.split("RESOURCE Me1abc", 1)[1].split("RESOURCE ", 1)[0]
    headroom = float(re.search(r"headroom ([\d.]+) bytes", me1).group(1))
    reference = TASKS["09-ram-for-a-component"]["reference"]["Me1abc"]
    assert headroom == reference["headroom"]
    assert headroom < 40 * 1000 < 40 * 1024 < reference["size"] - reference["consumed"]  # the threshold decides
    consumers = {name: int(size) for name, size in re.findall(r"- (\w+): (\d+) bytes", me1) if int(size) > 0}
    rule = _answer("17-largest-ram-consumer")["first_of"]
    assert sorted(consumers) == rule["candidates"]
    assert max(consumers, key=lambda name: consumers[name]) == rule["expected"]
    assert sorted(consumers.values()).count(max(consumers.values())) == 1


def test_11_the_ports_are_the_hubs(large: Path, tmp_path: Path) -> None:
    rows = _query(large, {"address": {"layer": "swc"}, "members": "ports"}, tmp_path)["elements"]
    ports = [row["member"] for row in rows if row["element"] == "Swc001abcdefghij"]
    assert ports == _answer("11-ports-of-a-component")["mentions_all"]
    assert len(ports) == _answer("11-ports-of-a-component")["number"]


def test_12_the_components_on_the_interface(large: Path, tmp_path: Path) -> None:
    request = {
        "address": {"layer": "swc"},
        "members": "ports",
        "where": [{"field": "interface", "equals": "If0169abcdefghij"}],
    }
    names = sorted({row["element"] for row in _query(large, request, tmp_path)["elements"]})
    assert names == _answer("12-components-on-an-interface")["mentions_all"]


def test_13_the_unconnected_ports(large: Path, tmp_path: Path) -> None:
    model = CHECK.read_model(large)
    ends = {
        (side["swc"], side["port"])
        for connector in model["connectors"].values()
        for side in (connector["from"], connector["to"])
    }
    unconnected = sorted(
        f"{name}.{port['name']}"
        for name, component in model["swc"].items()
        for port in component.get("ports", [])
        if (name, port["name"]) not in ends
    )
    assert unconnected == TASKS["13-unconnected-ports"]["reference"]["ports"]
    # Port names repeat across components, so the answer holds each port with its own component.
    request = {
        "address": {"layer": "swc"},
        "members": "ports",
        "form": "index",
        "derived": ["unconnected"],
        "where": [{"field": "derived.unconnected.direction", "exists": True}],
    }
    rows = _query(large, request, tmp_path)["rows"]
    pairs = [{"owner": row["element"], "name": row["member"]} for row in rows]
    assert pairs == _answer("13-unconnected-ports")["with_owner"]
    assert [f"{pair['owner']}.{pair['name']}" for pair in pairs] == unconnected
    assert not any(composition.get("ports") for composition in model["compositions"].values())


def test_13_a_reply_that_swaps_two_components_ports_fails(large: Path) -> None:
    """Swc005abcdefghij's and Swc010abcdefghij's ports swapped, every name kept, fails."""
    task = CHECK.load_task("13-unconnected-ports", TASKS_DIR)
    pairs = {pair["owner"]: pair["name"] for pair in _answer("13-unconnected-ports")["with_owner"]}
    right = "\n".join(f"- {owner}.{name}" for owner, name in pairs.items())
    assert CHECK.check_task(large, task, answer=right, baseline=large, expected_dir=EXPECTED) == []
    pairs["Swc005abcdefghij"], pairs["Swc010abcdefghij"] = pairs["Swc010abcdefghij"], pairs["Swc005abcdefghij"]
    swapped = "\n".join(f"- {owner}.{name}" for owner, name in pairs.items())
    failures = CHECK.check_task(large, task, answer=swapped, baseline=large, expected_dir=EXPECTED)
    assert failures == [
        "the answer names P1 but not as Swc005abcdefghij's",
        "the answer names P15 but not as Swc010abcdefghij's",
    ]


def test_13_a_reply_naming_another_component_under_the_right_headings_fails(large: Path) -> None:
    """Each port under its own component's heading, each line naming an unexpected component, fails."""
    task = CHECK.load_task("13-unconnected-ports", TASKS_DIR)
    pairs = _answer("13-unconnected-ports")["with_owner"]
    assert "Swc001abcdefghij" in CHECK.read_model(large)["swc"]
    headed = "\n\n".join(f"### {pair['owner']}\n- {pair['owner']}.{pair['name']}" for pair in pairs)
    assert CHECK.check_task(large, task, answer=headed, baseline=large, expected_dir=EXPECTED) == []
    moved = "\n\n".join(f"### {pair['owner']}\n- Swc001abcdefghij.{pair['name']}" for pair in pairs)
    failures = CHECK.check_task(large, task, answer=moved, baseline=large, expected_dir=EXPECTED)
    assert [failure for failure in failures if "but not as" in failure] == [
        f"the answer names {pair['name']} but not as {pair['owner']}'s" for pair in pairs
    ]


def test_14_the_asil_b_components(large: Path, tmp_path: Path) -> None:
    request = {"address": {"layer": "swc"}, "form": "index", "derived": ["effective_level"]}
    rows = _query(large, request, tmp_path)["rows"]
    at_b = sorted(row["element"] for row in rows if row["derived"]["effective_level"]["level"].partition("(")[0] == "B")
    assert at_b == TASKS["14-asil-b-count"]["reference"]["B"]
    assert len(at_b) == _answer("14-asil-b-count")["number"]


def test_15_the_components_in_the_partition(large: Path, tmp_path: Path) -> None:
    request = {"address": {"layer": "swc"}, "form": "index", "derived": ["partitions"]}
    placed: dict[str, list[str]] = {}
    for row in _query(large, request, tmp_path)["rows"]:
        if row["derived"]["partitions"]:
            placed.setdefault(row["derived"]["partitions"]["partition"], []).append(row["element"])
    answer = _answer("15-components-in-a-partition")
    assert sorted(placed["Pa3ab"]) == answer["mentions_all"]
    assert not set(placed["Pa3ab"]) & set(answer["mentions_none"])


def test_16_the_tasks_on_the_core(large: Path) -> None:
    partitions = json.loads(_cli(large, "query", "hardware", "--where", "core=Co1abc", "--format", "json"))["elements"]
    tasks = sorted(
        task
        for partition in partitions
        for task in json.loads(_cli(large, "query", "tasks", "--where", f"partition={partition}", "--format", "json"))[
            "elements"
        ]
    )
    assert tasks == _answer("16-tasks-on-a-core")["mentions_all"]
    assert "Ts31abcde" in tasks


# ---------------------------------------------------------------------------
# The requirement questions (tasks 18-22) are the large bench's.
# ---------------------------------------------------------------------------


def _findings(bench: Path, rule: str) -> list[dict[str, Any]]:
    completed = subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", "check-model", "--format", "json",
         "--project", (bench / "swcraft.yaml").as_posix()],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )  # fmt: skip
    return [finding for finding in json.loads(completed.stdout)["findings"] if finding["rule"] == rule]


def test_18_the_elements_satisfying_the_requirement(large: Path) -> None:
    document = json.loads(_cli(large, "query", "--dependents", "Rq03abcdef", "--depth", "1", "--format", "json"))
    sites = sorted(
        f"{entry['layer']} {entry['name']}" + (f" ({entry['detail']})" if entry["detail"] else "")
        for entry in document["matched"]["requirement"]
    )
    assert sites == sorted(TASKS["18-elements-satisfying-a-requirement"]["reference"]["satisfied_by"])
    sites_named = _answer("18-elements-satisfying-a-requirement")["with_layer"]
    named = {site.get("owner") or site["name"] for site in sites_named}
    assert named == {entry["name"] for entry in document["matched"]["requirement"]}


def test_19_and_20_are_check_models_findings(large: Path) -> None:
    findings = _findings(large, "trace.requirement-unallocated")
    unallocated = sorted(finding["subjects"][0]["element"] for finding in findings)
    assert unallocated == _answer("19-unallocated-requirements")["mentions_all"]
    stale = _findings(large, "trace.link-needs-confirmation")
    holders = sorted({finding["subjects"][0]["element"] for finding in stale})
    assert ["Rq01abcdefghijk", *holders] == _answer("20-links-needing-confirmation")["mentions_all"]
    assert all("Rq01abcdefghijk@1" in finding["message"] for finding in stale)


def test_21_the_requirements_about_the_phrase(large: Path) -> None:
    matches, unrelated = _about(CHECK.read_model(large), ("supply", "voltage"))
    assert matches == _answer("21-requirements-about-a-topic")["mentions_all"]
    assert unrelated == _answer("21-requirements-about-a-topic")["mentions_none"]


def _about(model: dict[str, Any], words: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """Benchmark task 21's rule: the requirements holding the phrase, and those holding none of its words."""
    phrase = re.compile(r"(?<![A-Za-z0-9_])" + r"\s+".join(f"{word}s?" for word in words) + r"(?![A-Za-z0-9_])", re.I)
    each = [re.compile(rf"(?<![A-Za-z0-9_]){word}s?(?![A-Za-z0-9_])", re.I) for word in words]
    texts = {rid: f"{r.get('title', '')}\n{r.get('description', '')}" for rid, r in model["requirements"].items()}
    matches = sorted((rid for rid, text in texts.items() if phrase.search(text)), key=str)
    unrelated = sorted((rid for rid, text in texts.items() if not any(w.search(text) for w in each)), key=str)
    return matches, unrelated


def test_22_the_candidates_are_the_trace_searchs(large: Path, tmp_path: Path) -> None:
    run = _copy(large, tmp_path)
    document = json.loads(_cli(run, "experiment", "trace", "--format", "json"))
    entry = next(r for r in document["requirements"] if r["id"] == "Rq17abcdef")
    places = {candidate["place"]: candidate for candidate in reversed(entry["candidates"]) if candidate["place"] <= 3}
    rule = _answer("22-candidates-for-a-requirement")["candidates"]
    assert [places[place]["group"]["name"] for place in sorted(places)] == rule["top"]
    for candidate in places.values():
        terms = {term for evidence in candidate["group"]["evidence"] for term in evidence.get("terms", [])}
        assert set(rule["terms"][candidate["group"]["name"]]) <= terms


# ---------------------------------------------------------------------------
# The any-model set, bound to the large bench.
# ---------------------------------------------------------------------------

TEMPLATES = {template["id"]: template for template in BIND.load_templates()}
TEMPLATE_QUESTIONS = sorted((tid for tid, template in TEMPLATES.items() if template["kind"] == "question"), key=str)
TEMPLATE_CHANGES = sorted((tid for tid, template in TEMPLATES.items() if template["kind"] == "change"), key=str)

#: What the large bench leaves not applicable, measured by one ``bind_all``: 17 is a ``tie`` (the
#: busiest memory's two largest consumers declare one figure); 18 is a ``long-site-list`` (the
#: requirement with the most link sites has more than twenty); 36 is ``no-synchronous-call`` (no
#: runnable of the bench calls an operation).
LARGE_NOT_APPLICABLE = {
    "17-largest-ram-consumer": "tie",
    "18-elements-satisfying-a-requirement": "long-site-list",
    "36-call-asynchronously": "no-synchronous-call",
}

#: The question templates whose binding picks the committed large task's target (the rendered prompt
#: is the committed one); their answers are held to the committed ones.
COINCIDING = {
    "11-ports-of-a-component",
    "19-unallocated-requirements",
    "20-links-needing-confirmation",
    "21-requirements-about-a-topic",
}


@pytest.fixture(scope="module")
def large_model(large: Path) -> Any:
    return BIND.BoundModel.load(large / "swcraft.yaml")


@pytest.fixture(scope="module")
def large_bound(large_model: Any) -> dict[str, Any]:
    """The bind result on the large bench, computed once per module."""
    return BIND.bind_all(large_model)


def _coincides(template_id: str, rendered: Any) -> bool:
    return BIND.render_task(TEMPLATES[template_id], rendered)["prompt"] == TASKS[template_id]["prompt"]


def test_every_template_binds_on_the_large_bench_or_is_not_applicable_by_its_code(
    large_bound: dict[str, Any],
) -> None:
    assert set(large_bound) == set(TEMPLATES)
    not_applicable = {tid: result.code for tid, result in large_bound.items() if isinstance(result, BIND.NotApplicable)}
    assert not_applicable == LARGE_NOT_APPLICABLE
    assert set(not_applicable.values()) <= set(BIND.NOT_APPLICABLE)


def test_17_is_a_tie_where_the_busiest_memorys_two_largest_consumers_share_a_figure(large_model: Any) -> None:
    blocks = memory_blocks(large_model)
    counts = {memory: sum(1 for amount in block["consumers"].values() if amount) for memory, block in blocks.items()}
    busiest = min(counts, key=lambda memory: (-counts[memory], memory))
    amounts = sorted((amount for amount in blocks[busiest]["consumers"].values() if amount), reverse=True)
    assert amounts[0] == amounts[1]


def test_the_questions_that_pick_the_committed_target_are_the_measured_ones(large_bound: dict[str, Any]) -> None:
    rendered = {tid: large_bound[tid] for tid in TEMPLATE_QUESTIONS if isinstance(large_bound[tid], BIND.Rendered)}
    assert {tid for tid, result in rendered.items() if _coincides(tid, result)} == COINCIDING


@pytest.mark.parametrize("template_id", TEMPLATE_QUESTIONS)
def test_a_large_question_binding_is_the_committed_answer_where_the_target_coincides_else_the_clis(
    template_id: str, large_model: Any, large_bound: dict[str, Any], tmp_path: Path
) -> None:
    rendered = large_bound[template_id]
    if isinstance(rendered, BIND.NotApplicable):
        assert LARGE_NOT_APPLICABLE[template_id] == rendered.code
        return
    answer = rendered.expect["answer"]
    if _coincides(template_id, rendered):
        # The binder chooses its own wrong names, so they are held only to be none of the right ones.
        committed = _answer(template_id)
        assert {key: value for key, value in answer.items() if key != "mentions_none"} == {
            key: value for key, value in committed.items() if key != "mentions_none"
        }
        assert set(answer.get("mentions_none", [])).isdisjoint(committed.get("mentions_all", []))
        return
    assert template_id in CLI_ORACLES, f"{template_id} has no CLI oracle and does not pick the committed target"
    CLI_ORACLES[template_id](large_model, rendered, tmp_path)


@pytest.mark.parametrize("template_id", TEMPLATE_CHANGES)
def test_a_large_change_bindings_right_run_passes_and_the_untouched_copy_fails(
    template_id: str, large_model: Any, large_bound: dict[str, Any], tmp_path: Path
) -> None:
    rendered = large_bound[template_id]
    if isinstance(rendered, BIND.NotApplicable):
        assert LARGE_NOT_APPLICABLE[template_id] == rendered.code
        return
    task = BIND.render_task(TEMPLATES[template_id], rendered)

    def codes(project: Path) -> list[str]:
        return [failure.code for failure in CHECK.check_task_coded(project, task, baseline=large_model.project)]

    assert codes(BIND.apply_right_run(large_model, rendered, tmp_path / "run")) == []
    assert "assertion" in codes(BIND.scratch_project(large_model, tmp_path / "untouched"))
