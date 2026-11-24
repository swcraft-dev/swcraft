"""Each of the agent task benchmark's search and containment questions is ONE ``read`` the server answers.

What is deterministic the server computes, so the agent never compares two lists in its head.
Each question of ``packages/tools/tests/fixtures/agent-bench/tasks/`` 11 to 22 is asked as the one
request that answers it (``benchmark_question_support.question_requests``) on the small bench,
held to the task's own expected answer, to its recorded wire size and to having no ``page``
block -- one read, not the first page of one. The large-bench twin is
``test_benchmark_questions_large.py`` (``slow``).

The recorded sizes are a record, not a bound: a change that moves one is re-measured here with the
reason in its commit. What this does not reach: whether the agent asks these reads, and the
choosing a reply still does over the rows (17 takes the largest).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from benchmark_question_support import (
    ask,
    candidates_for,
    counted,
    found_by_text,
    question_requests,
    requirement_requests,
    with_layer,
)
from conftest import AGENT_BENCH
from swcraft_tools.editing.engine import handle_request

SET_DIR = AGENT_BENCH

REQUESTS = question_requests(
    component="BrakeThermalProtection", interface="IBrakeTemperature", partition="P_Brake", core="Core0"
) | requirement_requests(requirement="REQ_BTP_DIAGNOSTICS")

#: Each answer's wire size on the small bench (``read.measure``); a request's shape or the bench moves them.
RECORDED_WIRE_BYTES = {
    "11": 332,
    "12": 429,
    "13": 461,
    "14": 180,
    "15": 803,
    "16": 609,
    "17": 531,
    # The requirements layer's computed values.
    "18": 937,
    "19": 3_578,
    "20": 766,
    # One `find` by `text`.
    "21": 2_322,
    # One `find` for the requirement.
    "22": 2_614,
}


def _task(task_id: str) -> dict[str, Any]:
    return json.loads((SET_DIR / "tasks" / f"{task_id}.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def bench(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("questions") / "bench"
    shutil.copytree(SET_DIR / "bench", target)
    return target / "swcraft.yaml"


def _rows(number: str, bench: Path) -> Any:
    value, size = ask(REQUESTS[number], bench)
    assert size == RECORDED_WIRE_BYTES[number], f"question {number} answers {size} wire bytes"
    return value


def test_11_the_ports_of_a_component(bench: Path) -> None:
    value = _rows("11", bench)
    expected = _task("11-ports-of-a-component")["expect"]["answer"]
    assert sorted(value["identities"]) == sorted(expected["mentions_all"])
    assert value["count"] == expected["number"]
    assert counted(REQUESTS["11"], bench) == expected["number"]


def test_12_the_components_with_a_port_of_an_interface(bench: Path) -> None:
    value = _rows("12", bench)
    components = sorted({row["element"] for row in value["identities"]})
    assert components == sorted(_task("12-components-on-an-interface")["expect"]["answer"]["mentions_all"])


def test_13_the_unconnected_ports(bench: Path) -> None:
    value = _rows("13", bench)
    assert value["identities"] == [
        {
            "element": "DiagnosticManager",
            "member": "rEcuMode",
            "derived": {"unconnected": {"direction": "require", "interface": "IEcuMode"}},
        },
        {
            "element": "InstrumentCluster",
            "member": "rOdometer",
            "derived": {"unconnected": {"direction": "require", "interface": "IOdometer"}},
        },
    ]
    ports = [row["member"] for row in value["identities"]]
    assert sorted(ports) == sorted(_task("13-unconnected-ports")["expect"]["answer"]["mentions_all"])
    assert counted(REQUESTS["13"], bench) == len(ports)


def test_13_a_delegated_port_is_connected(bench: Path) -> None:
    """BrakeThermalProtection's five delegated ports reach the boundary of BrakeSubsystem: no row."""
    value = _rows("13", bench)
    assert "BrakeThermalProtection" not in {row["element"] for row in value["identities"]}


def test_14_the_components_at_asil_b(bench: Path) -> None:
    """One ``count``: ``rating`` counts ``B`` with its decompositions -- the bench has ``B(D)``, ``B(D)`` and ``B``."""
    task = _task("14-asil-b-count")
    assert _rows("14", bench) == task["expect"]["answer"]["number"] == 3
    rows, _ = ask({**REQUESTS["14"], "form": "index"}, bench)
    levels = {row["element"]: row["derived"]["effective_level"] for row in rows["identities"]}
    assert sorted(levels) == sorted(task["reference"]["effective"]["B"])
    assert [level["level"] for level in levels.values()].count("B") == 1, "a `level` clause would count one"


def test_16_the_tasks_on_a_core_as_a_where_over_the_computed_core(bench: Path) -> None:
    """The join of question 16 and a clause on ``derived.core.core`` select the same tasks."""
    request = {
        "address": {"layer": "tasks"},
        "form": "index",
        "derived": ["core"],
        "where": [{"field": "derived.core.core", "equals": "Core0"}],
    }
    value, _ = ask(request, bench)
    tasks = sorted(row["element"] for row in value["identities"])
    assert tasks == sorted(_task("16-tasks-on-a-core")["expect"]["answer"]["mentions_all"])


def test_15_the_components_inside_a_partition(bench: Path) -> None:
    value = _rows("15", bench)
    components = sorted(row["element"] for row in value["identities"])
    assert components == sorted(_task("15-components-in-a-partition")["expect"]["answer"]["mentions_all"])


def test_16_the_tasks_on_a_core(bench: Path) -> None:
    value = _rows("16", bench)
    tasks = sorted(row["element"] for row in value["identities"])
    assert tasks == sorted(_task("16-tasks-on-a-core")["expect"]["answer"]["mentions_all"])
    assert all(row["derived"]["core"] == {"partition": "P_Vehicle", "core": "Core0"} for row in value["identities"])


def test_17_the_largest_consumer_of_a_memory(bench: Path) -> None:
    value = _rows("17", bench)
    of_ram = {row["element"]: row["member"] for row in value if row["key"] == "RAM"}
    assert of_ram == _task("17-largest-ram-consumer")["reference"]["consumers"]
    largest = max(of_ram, key=lambda name: of_ram[name])
    assert largest == _task("17-largest-ram-consumer")["expect"]["answer"]["first_of"]["expected"]


def test_core_abstains_where_the_partition_names_no_core(bench: Path, tmp_path: Path) -> None:
    project = tmp_path / "bench"
    shutil.copytree(bench.parent, project)
    partition = project / "model" / "hardware" / "P_Vehicle.yaml"
    partition.write_text(
        partition.read_text(encoding="utf-8").replace("core: Core0", "core: NoSuchCore"), encoding="utf-8", newline="\n"
    )
    answered = handle_request(
        {"operation": "read", "address": {"layer": "tasks"}, "form": "index", "derived": ["core"]},
        project=project / "swcraft.yaml",
    )
    cores = {row["element"]: row["derived"]["core"] for row in answered["value"]["identities"]}
    assert cores["Task_100ms"] == {"partition": "P_Vehicle", "core": None}
    assert cores["Task_50ms"] == {"partition": "P_Brake", "core": "Core1"}


def _entry_key(entry: dict[str, str]) -> list[tuple[str, str]]:
    return sorted(entry.items())


def test_18_the_elements_satisfying_a_requirement(bench: Path) -> None:
    value = _rows("18", bench)
    expected = _task("18-elements-satisfying-a-requirement")["expect"]["answer"]
    entries = [with_layer(row) for row in value["identities"]]
    assert sorted(entries, key=_entry_key) == sorted(expected["with_layer"], key=_entry_key)
    named = {entry["name"] for entry in entries} | {entry.get("owner") for entry in entries}
    assert not named & set(expected["mentions_none"])


def test_19_the_unallocated_requirements(bench: Path) -> None:
    value = _rows("19", bench)
    expected = _task("19-unallocated-requirements")["expect"]["answer"]
    unallocated = [row["element"] for row in value["identities"]]
    assert unallocated == sorted(expected["mentions_all"])
    assert not set(unallocated) & set(expected["mentions_none"])
    assert counted(REQUESTS["19"], bench) == 31


def test_20_the_links_needing_confirmation(bench: Path) -> None:
    value = _rows("20", bench)
    expected = _task("20-links-needing-confirmation")["expect"]["answer"]
    named = {row["element"] for row in value["identities"]}
    named |= {row["derived"]["stale_links"]["element"] for row in value["identities"]}
    assert named == set(expected["mentions_all"])
    assert all(row["derived"]["stale_links"]["current"] == 2 for row in value["identities"])


def test_21_the_requirements_about_a_topic(bench: Path) -> None:
    """One ``find`` by ``text``: the server's word rule answers the bench's adjacent-phrase twelve."""
    found, size = found_by_text("brake temperature", bench)
    assert size == RECORDED_WIRE_BYTES["21"], f"question 21 answers {size} wire bytes"
    expected = _task("21-requirements-about-a-topic")["expect"]["answer"]
    assert found == sorted(expected["mentions_all"])
    assert not set(found) & set(expected["mentions_none"])


def test_22_the_candidates_for_a_requirement(bench: Path) -> None:
    """One ``find`` for the requirement: the trace search's three places, each with a term it shares."""
    found, size = candidates_for("REQ_BTP_CORE_LOAD", bench)
    assert size == RECORDED_WIRE_BYTES["22"], f"question 22 answers {size} wire bytes"
    expected = _task("22-candidates-for-a-requirement")["expect"]["answer"]["candidates"]
    assert list(found) == expected["top"]
    for group, terms in expected["terms"].items():
        assert set(terms) <= set(found[group]), group
