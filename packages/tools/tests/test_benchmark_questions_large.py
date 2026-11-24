"""Questions 11 to 22 as one call each on the agent task benchmark's large bench (marked ``slow``).

The twin of ``test_benchmark_questions_in_one_read.py``: the same requests
(``benchmark_question_support.question_requests``) with the large bench's names, held to the
answers of ``packages/tools/tests/fixtures/agent-bench/large/tasks/<id>.json`` -> ``expect.answer`` and to their
recorded wire sizes. It also holds questions 09 and 17 as the MCP ``resource_report`` tool answers
them, task 05's ``with_dependents`` reply as the server sends it, the requirements layer's computed
values against the trace rules' findings, and task 24 as one ``change_set`` with ``confirm_links``.
The bench is ``conftest.py``'s session ``large_bench``, shared with ``test_agent_task_bench_large.py``
so ``test-slow`` generates it once; generating it is why the module is ``slow``.

What this does not reach: whether the large bench resembles a real project beyond its counts, and
whether an agent asks these reads.
"""

from __future__ import annotations

import json
from fractions import Fraction
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
    trace_agreement,
    with_layer,
)
from conftest import AGENT_BENCH
from swcraft_tools.editing.delta import summarize_response
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.read import measure
from swcraft_tools.resource_report import fits_answer, resource_answer, resource_report

pytestmark = pytest.mark.slow

TASKS_DIR = AGENT_BENCH / "large" / "tasks"

REQUESTS = question_requests(
    component="Swc001abcdefghij", interface="If0169abcdefghij", partition="Pa3ab", core="Co1abc"
) | requirement_requests(requirement="Rq03abcdef")

#: Each answer's wire size on the large bench (``read.measure``); a request's shape or the bench moves them.
RECORDED_WIRE_BYTES = {
    "11": 5_027,
    "12": 422,
    "13": 1_046,
    "14": 181,
    "15": 7_500,
    "16": 1_136,
    "17": 40_634,
    "18": 1_349,
    "19": 657,
    "20": 513,
    "21": 449,
    "22": 1_582,
}


def _task(task_id: str) -> dict[str, Any]:
    return json.loads((TASKS_DIR / f"{task_id}.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def bench(large_bench: Path) -> Path:
    return large_bench / "swcraft.yaml"


def _rows(number: str, bench: Path) -> Any:
    value, size = ask(REQUESTS[number], bench)
    assert size == RECORDED_WIRE_BYTES[number], f"question {number} answers {size} wire bytes"
    return value


def test_11_the_ports_of_the_hub(bench: Path) -> None:
    value = _rows("11", bench)
    expected = _task("11-ports-of-a-component")["expect"]["answer"]
    assert sorted(value["identities"]) == sorted(expected["mentions_all"])
    assert value["count"] == expected["number"]
    assert counted(REQUESTS["11"], bench) == expected["number"]


def test_12_the_components_with_a_port_of_an_interface(bench: Path) -> None:
    value = _rows("12", bench)
    expected = _task("12-components-on-an-interface")["expect"]["answer"]
    components = sorted({row["element"] for row in value["identities"]})
    assert components == sorted(expected["mentions_all"])
    assert not set(components) & set(expected["mentions_none"])


def test_13_the_unconnected_ports(bench: Path) -> None:
    value = _rows("13", bench)
    ports = sorted(f"{row['element']}.{row['member']}" for row in value["identities"])
    assert ports == sorted(_task("13-unconnected-ports")["reference"]["ports"])
    assert counted(REQUESTS["13"], bench) == len(ports)


def test_14_the_components_at_asil_b(bench: Path) -> None:
    """One ``count`` over the computed ``rating``: 24, the number the task expects."""
    task = _task("14-asil-b-count")
    assert _rows("14", bench) == task["expect"]["answer"]["number"] == 24
    rows, _ = ask({**REQUESTS["14"], "form": "index"}, bench)
    assert sorted(row["element"] for row in rows["identities"]) == sorted(task["reference"]["B"])


def test_15_the_components_inside_a_partition(bench: Path) -> None:
    value = _rows("15", bench)
    task = _task("15-components-in-a-partition")
    components = sorted(row["element"] for row in value["identities"])
    assert components == sorted(task["expect"]["answer"]["mentions_all"])
    assert len(components) == task["reference"]["count"]


def test_16_the_tasks_on_a_core(bench: Path) -> None:
    value = _rows("16", bench)
    expected = _task("16-tasks-on-a-core")["expect"]["answer"]
    tasks = sorted(row["element"] for row in value["identities"])
    assert tasks == sorted(expected["mentions_all"])
    assert not set(tasks) & set(expected["mentions_none"])


def test_16_the_tasks_on_a_core_as_a_where_over_the_computed_core(bench: Path) -> None:
    """A clause on ``derived.core.core`` keeps the idle task too: it maps no runnable and runs on the core."""
    request = {
        "address": {"layer": "tasks"},
        "form": "index",
        "derived": ["core"],
        "where": [{"field": "derived.core.core", "equals": "Co1abc"}],
    }
    value, _ = ask(request, bench)
    expected = _task("16-tasks-on-a-core")["expect"]["answer"]
    tasks = sorted(row["element"] for row in value["identities"])
    assert tasks == sorted(expected["mentions_all"])
    assert "Ts31abcde" in tasks
    assert not set(tasks) & set(expected["mentions_none"])


def test_17_the_largest_consumer_of_a_memory(bench: Path) -> None:
    value = _rows("17", bench)
    task = _task("17-largest-ram-consumer")
    of_ram = {row["element"]: row["member"] for row in value if row["key"] == "Me1abc"}
    consumers = sorted(name for name, used in of_ram.items() if used)
    assert len(consumers) == task["reference"]["consumers"], "the resource report counts a component using any"
    assert consumers == sorted(task["expect"]["answer"]["first_of"]["candidates"])
    assert {name: of_ram[name] for name in task["reference"]["top"]} == task["reference"]["top"]
    assert max(of_ram, key=lambda name: of_ram[name]) == task["expect"]["answer"]["first_of"]["expected"]


# Questions 09 and 17 as the resource tool answers them: one bounded call each


def _report(bench: Path) -> Any:
    """The large bench's resource report, as the MCP tool builds it but for the topology records it does not read."""
    return resource_report(str(bench.parent / "model"))


def test_09_a_fit_on_the_large_bench_is_decided_by_the_threshold(bench: Path) -> None:
    """Me1abc for 40 960 bytes: no, against 26 316.8 of headroom, although 51 712 bytes are raw free."""
    reference = _task("09-ram-for-a-component")["reference"]["Me1abc"]
    answer = fits_answer(_report(bench), "memory", "Me1abc", Fraction(40960))

    assert answer["fits"] is False
    assert answer["size"] == reference["size"]
    assert answer["used"] == str(reference["consumed"])
    assert answer["within_threshold"]["limit"] == str(reference["budget"])
    assert answer["within_threshold"]["headroom"] == str(reference["headroom"])
    assert answer["within_size"]["fits"] is True
    assert reference["size"] - reference["consumed"] == 51_712


def test_17_the_largest_consumer_of_a_memory_is_the_first_row_of_one_bounded_answer(bench: Path) -> None:
    """Me1abc's consumers largest first: the expected component leads, in a few kilobytes rather than a 40 KB read."""
    task = _task("17-largest-ram-consumer")
    answer = resource_answer(_report(bench), "memory", "Me1abc")
    consumers = {row["name"]: row["bytes"] for row in answer["consumers"]}

    assert answer["consumers"][0]["name"] == task["expect"]["answer"]["first_of"]["expected"]
    assert len(consumers) == task["reference"]["consumers"]
    assert sorted(consumers) == sorted(task["expect"]["answer"]["first_of"]["candidates"])
    assert {name: consumers[name] for name in task["reference"]["top"]} == task["reference"]["top"]
    assert measure(answer) < 4_000


# The benchmark's task 05: the removal as the change-set reply leaves the server


#: The benchmark's task 05 ``with_dependents`` reply on the large bench (``read.measure``): the engine's answer, every
#: removed element whole, and the same answer as the server sends it. Its seventeen entries' addresses,
#: summaries and ``origin`` flags alone are about 4 KB, so the elision halves the reply and no more.
#: A field added to a removed element's kind, or a change to the bench, moves them.
TASK_05_ENGINE_WIRE_BYTES = 10_746
TASK_05_SENT_WIRE_BYTES = 4_805


def test_05_the_with_dependents_reply_as_sent_carries_each_whole_element_as_its_summary(bench: Path) -> None:
    """``Swc076abcdefghij`` removed with its dependents: every element removed whole goes out summarised.

    Staged, never committed, so the shared session bench is not moved.
    """
    baseline = handle_request({"operation": "discover", "form": "baseline"}, project=bench)["baseline"]
    operation = {
        "operation": "remove",
        "address": {"layer": "swc", "element": "Swc076abcdefghij"},
        "with_dependents": True,
    }
    answer = handle_request({"operation": "change_set", "baseline": baseline, "operations": [operation]}, project=bench)
    sent = summarize_response(answer)

    assert answer["status"] == "staged"
    elements = [entry for entry in sent["delta"] if "path" not in entry["address"]]
    expected = {
        assertion["name"]
        for assertion in _task("05-remove-component")["expect"]["assertions"]
        if assertion.get("absent") and assertion["layer"] in {"swc", "connectors"}
    }
    assert expected <= {entry["address"]["element"] for entry in elements}
    assert all(entry.get("before_form") == "summary" for entry in elements)
    assert all("before_form" not in entry for entry in sent["delta"] if "path" in entry["address"])
    assert elements[0]["before"]["ports"] == 13
    assert (measure(answer), measure(sent)) == (TASK_05_ENGINE_WIRE_BYTES, TASK_05_SENT_WIRE_BYTES)


# Questions 18 to 20: the requirements layer's computed values


def _entry_key(entry: dict[str, str]) -> list[tuple[str, str]]:
    return sorted(entry.items())


def test_18_the_seven_sites_of_a_requirement(bench: Path) -> None:
    """Rq03abcdef at all seven site kinds.

    A component, a runnable, a port, an interface, a composition, a task and a processor.
    """
    value = _rows("18", bench)
    expected = _task("18-elements-satisfying-a-requirement")["expect"]["answer"]
    entries = [with_layer(row) for row in value["identities"]]
    assert sorted(entries, key=_entry_key) == sorted(expected["with_layer"], key=_entry_key)
    named = {entry["name"] for entry in entries} | {entry.get("owner") for entry in entries}
    assert not named & set(expected["mentions_none"])


def test_19_the_four_unallocated_requirements(bench: Path) -> None:
    value = _rows("19", bench)
    expected = _task("19-unallocated-requirements")["expect"]["answer"]
    assert [row["element"] for row in value["identities"]] == expected["mentions_all"]
    assert counted(REQUESTS["19"], bench) == 4


def test_20_the_two_stale_links(bench: Path) -> None:
    value = _rows("20", bench)
    expected = _task("20-links-needing-confirmation")["expect"]["answer"]
    named = {row["element"] for row in value["identities"]}
    named |= {row["derived"]["stale_links"]["element"] for row in value["identities"]}
    assert named == set(expected["mentions_all"])
    assert [row["derived"]["stale_links"]["member"] for row in value["identities"]] == [None, "P1"]


def test_21_the_requirements_about_supply_voltage(bench: Path) -> None:
    """Rq01abcdefghijk and Rq03abcdef hold both words; Rq16abcdef and Rq19abcdef hold one each and are not answered."""
    found, size = found_by_text("supply voltage", bench)
    assert size == RECORDED_WIRE_BYTES["21"], f"question 21 answers {size} wire bytes"
    expected = _task("21-requirements-about-a-topic")["expect"]["answer"]
    assert found == sorted(expected["mentions_all"])
    assert not set(found) & (set(expected["mentions_none"]) | {"Rq16abcdef", "Rq19abcdef"})


def test_the_values_agree_with_the_trace_rules_on_the_large_bench(bench: Path) -> None:
    values, findings = trace_agreement(bench.parent / "model")
    assert values == findings
    unallocated, stale = values
    assert unallocated == ["Rq16abcdef", "Rq17abcdef", "Rq18abcdef", "Rq19abcdef"]
    assert sum(stale.values()) == 2


def test_22_the_candidates_for_the_hub_requirement(bench: Path) -> None:
    """One ``find`` for Rq17abcdef: the search ranks the components whose descriptions share its words."""
    found, size = candidates_for("Rq17abcdef", bench)
    assert size == RECORDED_WIRE_BYTES["22"], f"question 22 answers {size} wire bytes"
    expected = _task("22-candidates-for-a-requirement")["expect"]["answer"]["candidates"]
    assert list(found) == expected["top"]
    for group, terms in expected["terms"].items():
        assert set(terms) <= set(found[group]), group


# The benchmark's task 24: one `change_set` confirming every link at the new version


def _confirm_rq02(project: Path, **review: Any) -> dict[str, Any]:
    baseline = handle_request({"operation": "discover", "form": "baseline"}, project=project)["baseline"]
    operation = {
        "operation": "update",
        "address": {"layer": "requirements", "element": "Rq02abcdef", "path": [{"field": "version"}]},
        "value": 2,
        "confirm_links": True,
    }
    return handle_request(
        {"operation": "change_set", "baseline": baseline, "operations": [operation]}, project=project, **review
    )


def test_24_the_seven_links_are_rewritten_as_a_remove_and_a_create(bench: Path) -> None:
    """Rq02abcdef is linked at ``@1`` at the seven site kinds; raised to version 2 each link is rewritten.

    Each link is one remove and one create of ``Rq02abcdef@2`` in the same list. Staged, never committed: the
    shared session bench is not moved.
    """
    answer = _confirm_rq02(bench)
    assert answer["status"] == "staged", answer
    server = [entry for entry in answer["delta"] if entry.get("origin") == "server"]
    assert [entry["operation"] for entry in server] == ["remove", "create"] * 7
    for removed, created in zip(server[::2], server[1::2], strict=True):
        assert removed["address"]["path"][-1] == {"member": "Rq02abcdef@1"}
        assert created["address"]["path"][-1] == {"member": "Rq02abcdef@2"}
        assert removed["address"]["path"][:-1] == created["address"]["path"][:-1]
    owners = {(entry["address"]["layer"], entry["address"]["path"][0]["field"]) for entry in server}
    assert owners == {
        ("swc", "satisfies"),
        ("swc", "ports"),
        ("swc", "runnables"),
        ("interfaces", "satisfies"),
        ("compositions", "satisfies"),
        ("tasks", "satisfies"),
        ("hardware", "satisfies"),
    }


def test_24_committed_it_passes_the_large_benchs_task_24(bench: Path, tmp_path: Path) -> None:
    """On a copy of the session bench, committed: the checker's nothing-else-moved rule over the large set."""
    import shutil

    from conftest import agent_task_check_module
    from swcraft_tools.editing.authorization import ApprovalDecision

    run = tmp_path / "run"
    shutil.copytree(bench.parent, run)
    answer = _confirm_rq02(
        run,
        review=lambda proposal: ApprovalDecision(
            change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True
        ),
    )
    assert answer["status"] == "committed", answer
    check = agent_task_check_module()
    tasks_dir, expected_dir = check.SETS["large"]
    task = check.load_task("24-confirm-links-at-a-new-version", tasks_dir)
    assert check.check_task(run, task, baseline=bench.parent, expected_dir=expected_dir) == []
