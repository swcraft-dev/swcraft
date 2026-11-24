"""The ``resource_report`` MCP tool.

The tool is the only channel for the ``info`` findings an ``advisory`` resource produces, and it is
read-only: an over-threshold resource is reported here, never failed, and no audit entry is written.
The model is seeded as YAML under ``hardware/``, which is what the tool reads.

The tool answers structured and one question at a time: the overview (no argument), one core or
memory, or whether an amount fits. The text stays ``swcraft resource-report``'s; the projections are
held to it in ``packages/tools/tests/test_resource_report.py``, and ``fits`` here to the hand-recorded
reference of the agent task benchmark's task 09.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

# `conftest` here is `packages/tools/tests/conftest.py`; this suite's own is `tests.conftest`.
from conftest import AGENT_TASK_BENCH, REPO_ROOT, SAFETY_MODEL_ROOT, measure_mcp_scale_module
from protocol_client import call
from swcraft_mcp.server import resource_report
from swcraft_tools import resource_report as resource_report_mod
from swcraft_tools.editing import read as read_mod
from swcraft_tools.validator import check_hardware_capability


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _hardware(tmp_path: Path) -> Path:
    return tmp_path / "model" / "hardware"


def _memory(tmp_path: Path, *, size: int, threshold: float, enforcement: str = "strict") -> None:
    _write(
        _hardware(tmp_path) / "RAM.yaml",
        f"name: RAM\nkind: memory\ntype: RAM\nsize: {size}\naccessible:\n  - CORE0\n"
        f"threshold: {threshold}\nenforcement: {enforcement}\n",
    )


def _budgeting_swc(tmp_path: Path, *, size_bytes: int) -> None:
    _write(
        tmp_path / "model" / "swc" / "powertrain" / "EngineSWC.yaml",
        f"name: EngineSWC\ntype: application\nmemory:\n  RAM: {size_bytes}\n",
    )


def test_resource_report_no_budget_declared() -> None:
    """A model with no core and no memory renders empty lists, not a "no resources" sentence."""
    answer = resource_report()

    assert (answer["response"], answer["cores"], answer["memories"], answer["findings"]) == ("resources", [], [], [])


def test_resource_report_renders_breakdown(tmp_path: Path) -> None:
    _memory(tmp_path, size=10000, threshold=0.8)
    _budgeting_swc(tmp_path, size_bytes=2048)

    overview = resource_report()
    memory = resource_report(memory="RAM")

    assert overview["memories"] == [
        {
            "name": "RAM",
            "kind": "memory",
            "enforcement": "strict",
            "size": 10000,
            "used": "2048",
            "limit": "8000",
            "headroom": "5952",
            "physical_headroom": "7952",
            "status": "within budget",
        }
    ]
    assert "consumers" not in overview["memories"][0]
    assert memory["resource"] == overview["memories"][0]
    assert memory["consumers"] == [{"name": "EngineSWC", "bytes": 2048}]


def test_resource_report_marks_a_breach(tmp_path: Path) -> None:
    _memory(tmp_path, size=1000, threshold=0.5)
    _budgeting_swc(tmp_path, size_bytes=900)

    answer = resource_report()

    assert answer["memories"][0]["status"] == "over budget"
    assert [(f["resource"], f["severity"], f["code"]) for f in answer["findings"]] == [
        ("RAM", "error", "threshold_breach")
    ]


def test_resource_report_renders_advisory_info_findings(tmp_path: Path) -> None:
    """An advisory breach is invisible to validate() — this tool is its channel."""
    _memory(tmp_path, size=1000, threshold=0.5, enforcement="advisory")
    _budgeting_swc(tmp_path, size_bytes=900)

    answer = resource_report()

    assert [(f["severity"], f["code"]) for f in answer["findings"]] == [("info", "threshold_breach")]


def test_resource_report_does_not_audit(tmp_path: Path) -> None:
    from swcraft_tools.auditor import AuditLog

    _memory(tmp_path, size=10000, threshold=0.8)
    # The server holds no auditor of its own, so a recording tool would have written this file.
    auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))
    before = len(auditor.get_entries())

    resource_report()

    assert len(auditor.get_entries()) == before


def test_resource_report_malformed_model_returns_error_string(tmp_path: Path) -> None:
    _memory(tmp_path, size=10000, threshold=0.8)
    _write(
        _hardware(tmp_path) / "Broken.yaml",
        "name: Broken\nkind: memory\ntype: RAM\nsize: -5\naccessible:\n  - CORE0\nthreshold: 0.8\n",
    )

    assert resource_report().startswith("ERROR:")


def test_resource_report_absurd_pool_size_renders_exactly(tmp_path: Path) -> None:
    """A `size` past ~1e308 renders exactly: `ResourceUsage` carries exact `Fraction`s, never a float."""
    _write(
        _hardware(tmp_path) / "Huge.yaml",
        f"name: Huge\nkind: memory\ntype: RAM\nsize: {10**400}\naccessible:\n  - CORE0\nthreshold: 0.8\n",
    )

    answer = resource_report()

    assert answer["memories"][0]["limit"] == str(8 * 10**399)
    assert answer["memories"][0]["size"] == 10**400


# ---------------------------------------------------------------------------
# The `TOPOLOGY` section
# ---------------------------------------------------------------------------


def _clean_topology(tmp_path: Path) -> None:
    """A processor, one core and its partition: what every topology check reads, and ``_memory`` does not supply."""
    _write(_hardware(tmp_path) / "CPU.yaml", "name: CPU\nkind: processor\n")
    _write(_hardware(tmp_path) / "CORE0.yaml", "name: CORE0\nkind: core\nthreshold: 0.7\n")
    _write(_hardware(tmp_path) / "PARTITION0.yaml", "name: PARTITION0\nkind: partition\ncore: CORE0\n")


def test_resource_report_names_a_task_in_an_undeclared_partition_in_a_topology_section(tmp_path: Path) -> None:
    """A task in an undeclared partition is named in the topology section, as the CLI's report names it.

    Such a task consumes nothing, so it reaches no budget finding; without ``check_hardware_topology``
    the report showed zero findings over a core whose load was unaccounted. The tool and the CLI
    must not answer differently.
    """
    _clean_topology(tmp_path)
    _memory(tmp_path, size=10000, threshold=0.8)
    _budgeting_swc(tmp_path, size_bytes=2048)
    _write(
        tmp_path / "model" / "tasks" / "Task_10ms.yaml",
        "name: Task_10ms\nperiod: 0.01\npriority: 5\npartition: PARTITION9\nwcet: 1200us\n",
    )

    answer = resource_report()

    assert len(answer["topology"]) == 1
    assert answer["topology"][0]["rule"] == "hardware.undeclared-partition"
    assert answer["topology"][0]["message"].startswith("Task 'Task_10ms' runs in partition 'PARTITION9'")
    # Not a gate and not part of the arithmetic: the budget findings and the `RAM` row are untouched.
    assert answer["findings"] == []
    assert [row["name"] for row in answer["memories"]] == ["RAM"]


def test_resource_report_names_neither_placement_rule_over_the_acceptance_fixture(tmp_path: Path) -> None:
    """The tool renders budgets and `check_hardware_topology`'s records, and nothing of the level.

    A level is not a budget, the report's always-exits-0 promise is about budgets, and a placement
    finding belongs to `check-model`. Non-vacuous: the same fixture mints the safety findings there.
    """
    shutil.copytree(SAFETY_MODEL_ROOT, tmp_path / "model", dirs_exist_ok=True)
    assert {record.rule for record in check_hardware_capability(str(tmp_path / "model")).records} == {
        "safety.partition-on-unqualified-core",
        "safety.memory-below-component",
        "safety.component-below-partition",
        "safety.partition-memory-below-level",
    }

    answer = resource_report()

    assert "RAM0" in [row["name"] for row in answer["memories"]]
    assert "CORE0" in [row["name"] for row in answer["cores"]]
    assert "safety." not in json.dumps(answer)
    assert answer["topology"] == []


def test_resource_report_omits_the_topology_section_when_nothing_is_wrong(tmp_path: Path) -> None:
    """The section appears only when there is something to say."""
    _clean_topology(tmp_path)
    _memory(tmp_path, size=10000, threshold=0.8)
    _budgeting_swc(tmp_path, size_bytes=2048)

    assert resource_report()["topology"] == []


def test_resource_report_malformed_element_returns_error_string(tmp_path: Path) -> None:
    """An element file that does not parse is an ``ERROR:`` string naming its storage name, never a raise.

    The tool catches ``ValueError`` alone, so a raw ``yaml.YAMLError`` the layer reader did not wrap
    would fail this case.
    """
    _write(tmp_path / "model" / "swc" / "powertrain" / "EngineSWC.yaml", "name: [unclosed\n")

    output = resource_report()

    assert output.startswith("ERROR: ")
    assert "swc/powertrain/EngineSWC.yaml" in output
    assert str(tmp_path) not in output


# ---------------------------------------------------------------------------
# One resource, and a fit -- the small bench and examples/basic
# ---------------------------------------------------------------------------


def _bench(tmp_path: Path) -> None:
    """The agent task benchmark's bench model, under the autouse project's model root."""
    shutil.copytree(AGENT_TASK_BENCH / "model", tmp_path / "model", dirs_exist_ok=True)


def _task(number: str) -> dict:
    tasks = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "agent-bench" / "tasks"
    (path,) = tasks.glob(f"{number}-*.json")
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_fit_is_decided_by_the_threshold_against_the_reference(tmp_path: Path) -> None:
    """The bench's task 09: 12 288 bytes against 10 240 of headroom is a no, 8 192 a yes.

    The raw free space says yes to both.
    """
    _bench(tmp_path)
    reference = _task("09")["reference"]["RAM"]

    refused = resource_report(fits={"memory": "RAM", "bytes": 12288})
    accepted = resource_report(fits={"memory": "RAM", "bytes": 8192})

    assert refused == {
        "response": "fits",
        "resource": "RAM",
        "kind": "memory",
        "size": reference["size"],
        "asked": "12288",
        "used": str(reference["consumed"]),
        "status": "within budget",
        "fits": False,
        "within_threshold": {"fits": False, "limit": str(reference["budget"]), "headroom": str(reference["headroom"])},
        "within_size": {
            "fits": True,
            "limit": str(reference["size"]),
            "headroom": str(reference["size"] - reference["consumed"]),
        },
        "answer": "fits physically, breaks the threshold by 2048 bytes",
    }
    # The two limits and headrooms are the verdicts' own; none is spelled a second time at the top.
    assert "limit" not in refused and "headroom" not in refused
    assert accepted["fits"] is True
    assert reference["size"] - reference["consumed"] > 12288


def test_a_core_fit_takes_a_fraction_read_as_its_decimal_text(tmp_path: Path) -> None:
    _bench(tmp_path)

    answer = resource_report(fits={"core": "Core0", "utilization": "0.05"})
    as_number = resource_report(fits={"core": "Core0", "utilization": 0.05})
    too_much = resource_report(fits={"core": "Core0", "utilization": "0.64"})

    assert (answer["asked"], answer["used"], answer["fits"]) == ("5.00%", "7.00%", True)
    assert answer["within_threshold"] == {"fits": True, "limit": "70.00%", "headroom": "63.00%"}
    assert answer["within_size"] == {"fits": True, "limit": "100.00%", "headroom": "93.00%"}
    assert answer["answer"] == "fits within the threshold and the size"
    assert "limit" not in answer and "headroom" not in answer
    assert as_number == answer
    assert too_much["fits"] is False
    assert too_much["within_size"]["fits"] is True
    assert too_much["answer"] == "fits physically, breaks the threshold by 1.00%"
    assert read_mod.measure(too_much) < 1_000


def test_a_fit_sent_over_the_wire_is_the_answer_the_function_gives(tmp_path: Path) -> None:
    """``fits`` through FastMCP's binding arrives as the function reads it, numbers included."""
    _bench(tmp_path)

    by_bytes = call("resource_report", fits={"memory": "RAM", "bytes": 12288})
    by_share = call("resource_report", fits={"core": "Core0", "utilization": 0.05})

    assert by_bytes.structured == resource_report(fits={"memory": "RAM", "bytes": 12288})
    assert by_share.structured == resource_report(fits={"core": "Core0", "utilization": "0.05"})
    assert by_bytes.structured["fits"] is False and by_share.structured["fits"] is True


def test_a_fit_on_a_strict_budget_with_an_undeclared_consumer_is_null_only_for_a_yes(tmp_path: Path) -> None:
    _memory(tmp_path, size=10000, threshold=0.8)
    _budgeting_swc(tmp_path, size_bytes=2048)
    _write(tmp_path / "model" / "swc" / "powertrain" / "BodySWC.yaml", "name: BodySWC\ntype: application\n")

    answer = resource_report(fits={"memory": "RAM", "bytes": 1})

    assert answer["fits"] is None
    assert answer["within_size"]["fits"] is None
    assert answer["answer"] == "unknown: 1 consumer(s) declare no budget for it, so its use leaves their load out"
    assert answer["status"] == "unknown (not every consumer declared a budget)"
    assert resource_report(memory="RAM")["undeclared_first"] == ["BodySWC"]
    # A "no" is always given, the threshold's as the size's: the missing load only adds to the use.
    breaks = resource_report(fits={"memory": "RAM", "bytes": 6000})
    assert (breaks["fits"], breaks["within_size"]["fits"]) == (False, None)
    assert breaks["answer"] == (
        "breaks the threshold by 48 bytes; within the size is unknown: 1 consumer(s) declare no budget for it"
    )
    over = resource_report(fits={"memory": "RAM", "bytes": 8000})
    assert (over["fits"], over["within_size"]["fits"]) == (False, False)
    assert over["answer"] == "does not fit: over the size by 48 bytes"


def test_one_memory_answers_its_largest_consumer_first(tmp_path: Path) -> None:
    """The bench's task 17 in one bounded call: the consumers largest first, the expected component the first row."""
    _bench(tmp_path)
    task = _task("17")

    answer = resource_report(memory="RAM")

    assert {row["name"]: row["bytes"] for row in answer["consumers"]} == task["reference"]["consumers"]
    assert answer["consumers"][0]["name"] == task["expect"]["answer"]["first_of"]["expected"]
    amounts = [row["bytes"] for row in answer["consumers"]]
    assert amounts == sorted(amounts, reverse=True)
    assert read_mod.measure(answer) < 1_000


def test_one_core_answers_its_tasks_and_the_memories_it_reaches(tmp_path: Path) -> None:
    shutil.copytree(REPO_ROOT / "examples" / "basic" / "model", tmp_path / "model", dirs_exist_ok=True)

    answer = resource_report(core="CORE0")
    overview = resource_report()

    assert answer["resource"] == overview["cores"][0]
    assert [row["name"] for row in answer["tasks"]] and all(
        set(row) == {"name", "utilization"} for row in answer["tasks"]
    )
    assert answer["memories"] == [row for row in overview["memories"] if row["name"] == "RAM0"]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"core": "CORE9"}, "ERROR: no core named 'CORE9'; the declared cores are CORE0."),
        ({"memory": "CORE0"}, "ERROR: 'CORE0' is a core, not a memory; ask for it as core='CORE0'."),
        ({"fits": {"memory": "RAM9", "bytes": 1}}, "ERROR: no memory named 'RAM9'; the declared memories are RAM0."),
    ],
)
def test_an_unknown_name_is_refused_naming_the_declared_ones(tmp_path: Path, arguments: dict, expected: str) -> None:
    shutil.copytree(REPO_ROOT / "examples" / "basic" / "model", tmp_path / "model", dirs_exist_ok=True)

    assert resource_report(**arguments) == expected


def test_an_unknown_name_among_many_declared_ones_is_refused_within_the_budget(tmp_path: Path) -> None:
    """750 memories: the refusal names the first ten and counts the rest, never all of them."""
    for index in range(750):
        _write(
            _hardware(tmp_path) / f"MemoryWithALongDeclaredName{index:03d}.yaml",
            f"name: MemoryWithALongDeclaredName{index:03d}\nkind: memory\ntype: RAM\nsize: 1024\n"
            "accessible:\n  - CORE0\nthreshold: 0.8\n",
        )

    answer = resource_report(memory="RAM9")

    named = ", ".join(f"MemoryWithALongDeclaredName{index:03d}" for index in range(10))
    assert answer == f"ERROR: no memory named 'RAM9'; the declared memories are {named} and 740 more."


@pytest.mark.parametrize(
    "arguments",
    [{"memory": "RAM"}, {"fits": {"memory": "RAM", "bytes": 200}}],
)
def test_a_name_two_hardware_elements_carry_is_refused(tmp_path: Path, arguments: dict) -> None:
    """Two RAMs sized 1000 and 100 hold opposite verdicts for 200 bytes; reader order must not pick one."""
    _memory(tmp_path, size=1000, threshold=0.8)
    _write(
        _hardware(tmp_path) / "RAM_copy.yaml",
        "name: RAM\nkind: memory\ntype: RAM\nsize: 100\naccessible:\n  - CORE0\nthreshold: 0.8\n",
    )

    assert resource_report(**arguments) == (
        "ERROR: 2 hardware elements are named 'RAM' (hardware.duplicate-name), so no one answer is its; "
        "give each its own name, and swcraft validate reports the duplicate."
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"core": "CORE0", "memory": "RAM0"},
        {"core": "CORE0", "fits": {"memory": "RAM0", "bytes": 1}},
        {"memory": "RAM0", "fits": {"memory": "RAM0", "bytes": 1}},
    ],
)
def test_two_questions_in_one_call_are_refused(arguments: dict) -> None:
    answer = resource_report(**arguments)

    assert isinstance(answer, str) and answer.startswith("ERROR: resource_report answers one question per call")


@pytest.mark.parametrize(
    "fits",
    [
        {"memory": "RAM0"},
        {"memory": "RAM0", "bytes": -1},
        {"memory": "RAM0", "bytes": True},
        {"memory": "RAM0", "bytes": 1.5},
        {"core": "CORE0", "utilization": "5%"},
        {"core": "CORE0", "utilization": "-0.1"},
        # Refused before `Fraction` expands the exponent, which would stall the server.
        {"core": "CORE0", "utilization": "1e999999999"},
        {"core": "CORE0", "utilization": "5E-999999999"},
        # `Fraction` reads digit grouping, which the exponent bound would not see.
        {"core": "CORE0", "utilization": "1e9_999_999_999"},
        {"core": "CORE0", "utilization": "0.0_5"},
        {"core": "CORE0", "utilization": "0." + "0" * 100 + "5"},
        {"core": "CORE0", "bytes": 1},
        {"memory": "", "bytes": 1},
    ],
)
def test_a_malformed_fit_is_refused(fits: dict) -> None:
    answer = resource_report(fits=fits)

    assert isinstance(answer, str) and answer.startswith("ERROR: fits")


@pytest.mark.parametrize(
    ("utilization", "expected"),
    [
        ("5%", "ERROR: fits.utilization is a fraction of the core, such as \"0.05\" for 5 %, not '5%'."),
        (
            "1e999999999",
            "ERROR: fits.utilization is a fraction of the core, such as \"0.05\" for 5 %, not '1e999999999'.",
        ),
        ("1/0", "ERROR: fits.utilization is a fraction of the core, such as \"0.05\" for 5 %, not '1/0'."),
        (True, 'ERROR: fits.utilization is a fraction of the core, such as "0.05" for 5 %, not True.'),
        ("-0.1", "ERROR: fits.utilization is 0 or more, not '-0.1'."),
        (-0.1, "ERROR: fits.utilization is 0 or more, not -0.1."),
    ],
)
def test_a_utilization_refusal_keeps_its_sentence(utilization: object, expected: str) -> None:
    """The reading moved into ``resource_report.parse_utilization``; the tool's sentences stayed the tool's."""
    assert resource_report(fits={"core": "CORE0", "utilization": utilization}) == expected


def test_only_the_overview_points_a_folded_missing_budget_at_the_one_resource_call(tmp_path: Path) -> None:
    """The library states the count; the server adds how to ask for the names, and only where that is a new call."""
    _memory(tmp_path, size=10000, threshold=0.8)
    _write(
        _hardware(tmp_path) / "RAM1.yaml",
        "name: RAM1\nkind: memory\ntype: RAM\nsize: 10000\naccessible:\n  - CORE0\nthreshold: 0.8\n",
    )
    _budgeting_swc(tmp_path, size_bytes=2048)

    overview = resource_report()
    memory = resource_report(memory="RAM1")

    (folded,) = [f for f in overview["findings"] if f["code"] == "missing_budget"]
    assert folded["message"] == (
        "1 consumer(s) of memory 'RAM1' declare no budget for it. resource_report memory='RAM1' names the first 10."
    )
    (alone,) = [f for f in memory["findings"] if f["code"] == "missing_budget"]
    assert alone["message"] == "1 consumer(s) of memory 'RAM1' declare no budget for it."
    assert memory["undeclared_first"] == ["EngineSWC"]


def test_an_answer_over_the_budget_is_refused_naming_the_cli(tmp_path: Path, monkeypatch) -> None:
    _memory(tmp_path, size=10000, threshold=0.8)
    _budgeting_swc(tmp_path, size_bytes=2048)
    monkeypatch.setattr(read_mod, "RESPONSE_BUDGET_WIRE_BYTES", 1)

    overview = resource_report()
    memory = resource_report(memory="RAM")

    assert overview.startswith("ERROR: this answer measures ")
    assert 'Ask for it with memory="RAM".' in overview
    # The overview's narrower request is the shared clause the measurement
    # script parses, so a refused overview reads as a budget refusal there.
    assert measure_mcp_scale_module().text_hint(overview) == {"memory": "RAM"}
    assert "`swcraft resource-report --project PATH`" in overview
    assert memory.startswith("ERROR: this answer measures ")
    assert "Ask for it with" not in memory
    assert "`swcraft resource-report --project PATH`" in memory


def test_the_overview_names_where_a_data_received_runnables_data_comes_from(tmp_path: Path) -> None:
    """``arrivals``, only when the model has a data-received event, with the closing sentence once."""
    _clean_topology(tmp_path)
    assert "arrivals" not in resource_report()
    model = tmp_path / "model"
    _write(
        model / "interfaces" / "ISpeed.yaml",
        "name: ISpeed\nkind: sender_receiver\ndata_elements:\n  - name: Speed\n    implementation_type: u8\n",
    )
    _write(
        model / "swc" / "chassis" / "WheelSpeed.yaml",
        "name: WheelSpeed\ntype: application\nports:\n  - name: pSpeed\n    direction: provide\n    interface: ISpeed\n"
        "runnables:\n  - name: Sample\n    writes:\n      - port: pSpeed\n        element: Speed\n",
    )
    _write(
        model / "swc" / "chassis" / "WheelMonitor.yaml",
        "name: WheelMonitor\ntype: application\nports:\n  - name: rSpeed\n    direction: require\n"
        "    interface: ISpeed\nrunnables:\n  - name: OnSpeed\n    events:\n      - type: data\n"
        "        port: rSpeed\n        element: Speed\n",
    )
    _write(
        model / "compositions" / "Vehicle.yaml",
        "name: Vehicle\ntitle: Vehicle\nroot: true\nmembers:\n  - name: Sensor\n    type: WheelSpeed\n"
        "  - name: Monitor\n    type: WheelMonitor\n",
    )
    _write(
        model / "connectors" / "C1.yaml",
        "name: C1\nfrom:\n  swc: WheelSpeed\n  port: pSpeed\nto:\n  swc: WheelMonitor\n  port: rSpeed\n",
    )
    _write(
        model / "tasks" / "Task_20ms.yaml",
        "name: Task_20ms\nperiod: 0.02\npriority: 5\npartition: PARTITION0\nwcet: 1200us\nrunnables:\n"
        "  - swc: WheelSpeed\n    runnable: Sample\n",
    )

    answer = resource_report()

    assert answer["arrivals"] == {
        "first": ["Monitor.OnSpeed (rSpeed.Speed): data arrives at most every 20 ms (from Sensor.Sample in Task_20ms)"],
        "more": 0,
        "note": (
            "These are bounds taken from the senders' task periods, derived from the model; they are not measured "
            "rates. Where the RTE activates the receiving task on arrival, a bound limits that task's activations "
            "from the sender's side only."
        ),
    }
    assert answer["findings"] == []


def test_an_overview_whose_every_arrival_line_would_pass_the_budget_is_still_answered(tmp_path: Path) -> None:
    """The arrival lines grow with the model; the overview carries the first ones and counts the rest.

    1 200 lines alone measure past the response budget; unbounded, the whole overview was refused.
    """
    _clean_topology(tmp_path)
    _memory(tmp_path, size=4096, threshold=0.8)
    model = tmp_path / "model"
    elements = [f"WheelSpeedSignal{index:02d}" for index in range(30)]
    _write(
        model / "interfaces" / "ISpeed.yaml",
        "name: ISpeed\nkind: sender_receiver\ndata_elements:\n"
        + "".join(f"  - name: {element}\n    implementation_type: u8\n" for element in elements),
    )
    _write(
        model / "swc" / "chassis" / "WheelSpeed.yaml",
        "name: WheelSpeed\ntype: application\nports:\n  - name: pSpeed\n    direction: provide\n    interface: ISpeed\n"
        "runnables:\n  - name: Sample\n    writes:\n"
        + "".join(f"      - port: pSpeed\n        element: {element}\n" for element in elements),
    )
    _write(
        model / "swc" / "chassis" / "WheelMonitor.yaml",
        "name: WheelMonitor\ntype: application\nports:\n  - name: rSpeed\n    direction: require\n"
        "    interface: ISpeed\nrunnables:\n"
        + "".join(
            f"  - name: OnSpeed{index:02d}\n    events:\n      - type: data\n        port: rSpeed\n"
            f"        element: {element}\n"
            for index, element in enumerate(elements)
        ),
    )
    _write(
        model / "compositions" / "Vehicle.yaml",
        "name: Vehicle\ntitle: Vehicle\nroot: true\nmembers:\n  - name: Sensor\n    type: WheelSpeed\n"
        + "".join(f"  - name: WheelMonitorOccurrence{index:02d}\n    type: WheelMonitor\n" for index in range(40)),
    )
    _write(
        model / "connectors" / "C1.yaml",
        "name: C1\nfrom:\n  swc: WheelSpeed\n  port: pSpeed\nto:\n  swc: WheelMonitor\n  port: rSpeed\n",
    )
    _write(
        model / "tasks" / "Task_20ms.yaml",
        "name: Task_20ms\nperiod: 0.02\npriority: 5\npartition: PARTITION0\nwcet: 1200us\nrunnables:\n"
        "  - swc: WheelSpeed\n    runnable: Sample\n",
    )
    report = resource_report_mod.resource_report(str(model))
    every_line = [resource_report_mod.arrival_line(arrival) for arrival in report.arrivals]
    assert len(every_line) == 1200
    assert read_mod.measure(every_line) > read_mod.RESPONSE_BUDGET_WIRE_BYTES

    answer = resource_report()

    assert isinstance(answer, dict), answer
    assert [row["name"] for row in answer["cores"]] == ["CORE0"]
    assert [row["name"] for row in answer["memories"]] == ["RAM"]
    named = resource_report_mod.ARRIVALS_NAMED
    assert answer["arrivals"]["first"] == every_line[:named]
    assert answer["arrivals"]["more"] == 1200 - named
    assert answer["arrivals"]["rest"].startswith("swcraft resource-report prints every line")
    assert read_mod.measure(answer) <= read_mod.RESPONSE_BUDGET_WIRE_BYTES


def test_an_overview_whose_first_arrival_lines_name_a_deep_instance_path_is_still_answered(tmp_path: Path) -> None:
    """Twenty lines are no byte bound: each names its occurrence's instance path.

    Forty compositions deep with long member names, twenty lines alone measure past the budget, so a
    count cap alone kept the overview refused whole.
    """
    _clean_topology(tmp_path)
    _memory(tmp_path, size=4096, threshold=0.8)
    model = tmp_path / "model"
    elements = [f"Signal{index:02d}" for index in range(20)]
    _write(
        model / "interfaces" / "ISpeed.yaml",
        "name: ISpeed\nkind: sender_receiver\ndata_elements:\n"
        + "".join(f"  - name: {element}\n    implementation_type: u8\n" for element in elements),
    )
    _write(
        model / "swc" / "chassis" / "WheelMonitor.yaml",
        "name: WheelMonitor\ntype: application\nports:\n  - name: rSpeed\n    direction: require\n"
        "    interface: ISpeed\nrunnables:\n"
        + "".join(
            f"  - name: On{element}\n    events:\n      - type: data\n        port: rSpeed\n"
            f"        element: {element}\n"
            for element in elements
        ),
    )
    depth = 40
    for level in range(depth):
        member = f"Level{level:02d}" + "x" * 113
        inner = f"    composition: Nest{level + 1:02d}\n" if level + 1 < depth else "    type: WheelMonitor\n"
        _write(
            model / "compositions" / f"Nest{level:02d}.yaml",
            f"name: Nest{level:02d}\ntitle: Nest{level:02d}\nroot: {'true' if level == 0 else 'false'}\n"
            f"members:\n  - name: {member}\n{inner}",
        )
    report = resource_report_mod.resource_report(str(model))
    every_line = [resource_report_mod.arrival_line(arrival) for arrival in report.arrivals]
    assert len(every_line) == 20
    assert read_mod.measure(every_line) > read_mod.RESPONSE_BUDGET_WIRE_BYTES

    answer = resource_report()

    assert isinstance(answer, dict), answer
    assert [row["name"] for row in answer["cores"]] == ["CORE0"]
    assert [row["name"] for row in answer["memories"]] == ["RAM"]
    kept = len(answer["arrivals"]["first"])
    assert 0 < kept < resource_report_mod.ARRIVALS_NAMED
    assert answer["arrivals"]["first"] == every_line[:kept]
    assert answer["arrivals"]["more"] == 20 - kept
    assert answer["arrivals"]["rest"].startswith("swcraft resource-report prints every line")
    assert read_mod.measure(answer) <= read_mod.RESPONSE_BUDGET_WIRE_BYTES


def test_a_malformed_connector_in_a_model_with_a_data_received_event_is_an_error_string(tmp_path: Path) -> None:
    """The arrivals read ``connectors/`` once an event exists: the reply names the file by its storage name only."""
    _clean_topology(tmp_path)
    model = tmp_path / "model"
    _write(
        model / "interfaces" / "ISpeed.yaml",
        "name: ISpeed\nkind: sender_receiver\ndata_elements:\n  - name: Speed\n    implementation_type: u8\n",
    )
    _write(
        model / "swc" / "chassis" / "WheelMonitor.yaml",
        "name: WheelMonitor\ntype: application\nports:\n  - name: rSpeed\n    direction: require\n"
        "    interface: ISpeed\nrunnables:\n  - name: OnSpeed\n    events:\n      - type: data\n"
        "        port: rSpeed\n        element: Speed\n",
    )
    _write(model / "connectors" / "Broken.yaml", "name: [unclosed\n")

    output = resource_report()

    assert isinstance(output, str) and output.startswith("ERROR: ")
    assert "connectors/Broken.yaml" in output
    assert str(tmp_path) not in output


# ---------------------------------------------------------------------------
# The IMPLICIT ACCESS section
# ---------------------------------------------------------------------------

_ACCESS_FORMS = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "access_forms" / "model"


def test_the_overview_counts_the_implicit_accesses_per_task(tmp_path: Path) -> None:
    """``implicit``, only when the model has an implicit access, bounded as ``arrivals`` is."""
    _clean_topology(tmp_path)
    assert "implicit" not in resource_report()
    shutil.copytree(_ACCESS_FORMS, tmp_path / "model", dirs_exist_ok=True)

    answer = resource_report()

    assert answer["implicit"] == {
        "tasks": [
            {"task": "Task_10ms", "core": "CORE0", "reads": 1, "writes": 2, "runnables": 2},
            {"task": "Task_100ms", "core": "CORE0", "reads": 1, "writes": 0, "runnables": 1},
        ],
        "noTask": [
            {"occurrence": "BrakeController", "runnable": "Diagnose", "reads": 1, "writes": 0, "case": "in no task"},
            {
                "occurrence": "RearAxle/Sensor",
                "runnable": "SampleSpeed",
                "reads": 0,
                "writes": 1,
                "case": "in no task",
            },
        ],
        "notWalked": [],
        "more": 0,
        "note": (
            "Implicit reads and writes the task's runnables declare; when the RTE copies them, and what that costs, "
            "is not in the model."
        ),
    }
    assert answer["findings"] == []


def test_an_overview_whose_every_implicit_row_would_pass_the_budget_is_still_answered(tmp_path: Path) -> None:
    """1 800 unmapped occurrence rows measure past the budget; the overview carries the first and counts the rest."""
    _clean_topology(tmp_path)
    _memory(tmp_path, size=4096, threshold=0.8)
    model = tmp_path / "model"
    _write(
        model / "interfaces" / "ISpeed.yaml",
        "name: ISpeed\nkind: sender_receiver\ndata_elements:\n  - name: Speed\n    implementation_type: u8\n",
    )
    types = [f"Reader{index:02d}" for index in range(30)]
    for name in types:
        _write(
            model / "swc" / "chassis" / f"{name}.yaml",
            f"name: {name}\ntype: application\nports:\n  - name: rSpeed\n    direction: require\n"
            "    interface: ISpeed\nrunnables:\n  - name: Run\n    reads:\n      - port: rSpeed\n"
            "        element: Speed\n        by: implicit\n",
        )
    _write(
        model / "compositions" / "Box.yaml",
        "name: Box\ntitle: Box\nmembers:\n" + "".join(f"  - name: {name}\n    type: {name}\n" for name in types),
    )
    _write(
        model / "compositions" / "Vehicle.yaml",
        "name: Vehicle\ntitle: Vehicle\nroot: true\nmembers:\n"
        + "".join(f"  - name: Occurrence{index:02d}{'x' * 108}\n    composition: Box\n" for index in range(60)),
    )
    report = resource_report_mod.resource_report(str(model))
    every_row = resource_report_mod.implicit_rows(report.implicit)["noTask"]
    assert len(every_row) == 1800
    assert read_mod.measure(every_row) > read_mod.RESPONSE_BUDGET_WIRE_BYTES

    answer = resource_report()

    assert isinstance(answer, dict), answer
    assert [row["name"] for row in answer["memories"]] == ["RAM"]
    kept = len(answer["implicit"]["noTask"])
    assert 0 < kept <= resource_report_mod.IMPLICIT_NAMED
    assert answer["implicit"]["tasks"] == []
    assert answer["implicit"]["noTask"] == every_row[:kept]
    assert answer["implicit"]["more"] == 1800 - kept
    assert answer["implicit"]["rest"].startswith("swcraft resource-report prints every row")
    assert read_mod.measure(answer) <= read_mod.RESPONSE_BUDGET_WIRE_BYTES


def test_a_malformed_task_in_a_model_with_an_implicit_access_is_an_error_string(tmp_path: Path) -> None:
    """The section reads ``tasks/`` once an implicit access exists: the reply names the file by its storage name."""
    _clean_topology(tmp_path)
    shutil.copytree(_ACCESS_FORMS, tmp_path / "model", dirs_exist_ok=True)
    _write(tmp_path / "model" / "tasks" / "Broken.yaml", "name: [unclosed\n")

    output = resource_report()

    assert isinstance(output, str) and output.startswith("ERROR: ")
    assert "tasks/Broken.yaml" in output
    assert str(tmp_path) not in output
