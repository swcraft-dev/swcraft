"""The resource report's ``IMPLICIT ACCESS`` section (contract ``12.7.0``).

The shared fixture ``fixtures/access_forms/model`` gives the section its task
lines and its occurrences in no task; hand-built models under ``tmp_path`` give
every case of the counting rule (one occurrence per entry, a runnable
occurrence counted once per task and in each task, an ``instance-ambiguous``
entry mapping nothing, the walk undefined, the cap), the read discipline, the
four surfaces and the leaf's import set.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT, _write, _write_manifest, element_path
from swcraft_tools import implicit_access
from swcraft_tools import resource_report as resource_report_module
from swcraft_tools.cli import main
from swcraft_tools.implicit_access import (
    IN_NO_TASK,
    OCCURRENCE,
    PAST_THE_CAP,
    PAST_THE_CAP_LINE,
    PLACED_NOWHERE,
    TASK,
    ImplicitLine,
    implicit_accesses,
)
from swcraft_tools.resource_report import resource_report
from swcraft_tools.scheduling_identity import PATH_CAP

FIXTURE = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "access_forms" / "model"

#: The section the fixture's report ends with: two tables.
FIXTURE_SECTION = [
    "IMPLICIT ACCESS (implicit reads and writes the task's runnables declare; when the RTE copies them, and what "
    "that costs, is not in the model)",
    "  Task        Core   Implicit reads  Implicit writes  Runnables",
    "  Task_10ms   CORE0  1               2                2",
    "  Task_100ms  CORE0  1               0                1",
    "  RUNNABLES IN NO TASK",
    "    Runnable                     Implicit reads  Implicit writes",
    "    BrakeController.Diagnose     1               0",
    "    RearAxle/Sensor.SampleSpeed  0               1",
]

#: The Markdown section the fixture's report file ends with.
FIXTURE_MARKDOWN = (
    "## Implicit access\n\n"
    "Implicit reads and writes the task's runnables declare; when the RTE copies them, and what that costs, "
    "is not in the model.\n\n"
    "| Task | Core | Implicit reads | Implicit writes | Runnables |\n"
    "| --- | --- | --- | --- | --- |\n"
    "| `Task_10ms` | `CORE0` | 1 | 2 | 2 |\n"
    "| `Task_100ms` | `CORE0` | 1 | 0 | 1 |\n\n"
    "### Runnables in no task\n\n"
    "| Runnable | Implicit reads | Implicit writes |\n"
    "| --- | --- | --- |\n"
    "| `BrakeController.Diagnose` | 1 | 0 |\n"
    "| `RearAxle/Sensor.SampleSpeed` | 0 | 1 |\n"
)

#: The fixture's rows, as the JSON document and the MCP overview carry them.
FIXTURE_ROWS = {
    "tasks": [
        {"task": "Task_10ms", "core": "CORE0", "reads": 1, "writes": 2, "runnables": 2},
        {"task": "Task_100ms", "core": "CORE0", "reads": 1, "writes": 0, "runnables": 1},
    ],
    "noTask": [
        {"occurrence": "BrakeController", "runnable": "Diagnose", "reads": 1, "writes": 0, "case": "in no task"},
        {"occurrence": "RearAxle/Sensor", "runnable": "SampleSpeed", "reads": 0, "writes": 1, "case": "in no task"},
    ],
    "notWalked": [],
}


def _fixture(tmp_path: Path) -> Path:
    model = tmp_path / "model"
    shutil.copytree(FIXTURE, model)
    return model


class _Model:
    """A hand-built model: one sender/receiver interface, one partition on ``CORE0``, and what each test adds."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.put(
            "interfaces",
            {
                "name": "ISpeed",
                "kind": "sender_receiver",
                "data_elements": [{"name": "Speed", "implementation_type": "u8"}],
            },
        )
        self.put("hardware", {"name": "CPU", "kind": "processor"})
        self.put("hardware", {"name": "CORE0", "kind": "core", "threshold": 0.7})
        self.put("hardware", {"name": "PARTITION0", "kind": "partition", "core": "CORE0"})

    def put(self, layer: str, document: dict[str, Any], **placement: str) -> None:
        _write(element_path(self.root, layer, document["name"], **placement), document)

    def reader(self, name: str = "Reader", *, reads: int = 1, writes: int = 0, runnable: str = "Run") -> None:
        """A component whose *runnable* reads ``rSpeed.Speed`` implicitly *reads* times (one port per read)."""
        ports = [{"name": f"r{index}", "direction": "require", "interface": "ISpeed"} for index in range(reads)]
        ports += [{"name": f"p{index}", "direction": "provide", "interface": "ISpeed"} for index in range(writes)]
        self.put(
            "swc",
            {
                "name": name,
                "type": "application",
                "ports": ports,
                "runnables": [
                    {
                        "name": runnable,
                        "reads": [
                            {"port": f"r{index}", "element": "Speed", "by": "implicit"} for index in range(reads)
                        ],
                        "writes": [
                            {"port": f"p{index}", "element": "Speed", "by": "implicit"} for index in range(writes)
                        ],
                    }
                ],
            },
            group="g",
        )

    def composition(self, name: str, members: dict[str, str], *, root: bool = False) -> None:
        """*members* maps a member name to a component type, or ``"@Name"`` to a composition."""
        self.put(
            "compositions",
            {
                "name": name,
                "title": name,
                "root": root,
                "members": [
                    {"name": member, "composition": kind[1:]}
                    if kind.startswith("@")
                    else {"name": member, "type": kind}
                    for member, kind in members.items()
                ],
            },
        )

    def task(self, name: str, period: float, *entries: dict[str, str], partition: str = "PARTITION0") -> None:
        self.put(
            "tasks",
            {"name": name, "period": period, "priority": 1, "partition": partition, "runnables": list(entries)},
        )

    def lines(self) -> tuple[ImplicitLine, ...]:
        return implicit_accesses(str(self.root))


def _swc(component: str, runnable: str = "Run") -> dict[str, str]:
    return {"swc": component, "runnable": runnable}


def _instance(path: str, runnable: str = "Run") -> dict[str, str]:
    return {"instance": path, "runnable": runnable}


def _line(
    task: str, *, reads: int = 0, writes: int = 0, runnables: int = 1, core: str | None = "CORE0"
) -> ImplicitLine:
    return ImplicitLine(TASK, reads, writes, task=task, core=core, runnables=runnables)


def _unmapped(occurrence: str, *, reads: int = 0, writes: int = 0, case: str = IN_NO_TASK) -> ImplicitLine:
    return ImplicitLine(OCCURRENCE, reads, writes, occurrence=occurrence, runnable="Run", case=case)


# The fixture


def test_the_fixture_gives_task_lines_and_the_occurrences_no_task_maps(tmp_path: Path) -> None:
    """A type placed twice and mapped once: the mapped occurrence in its task, the other in no task."""
    assert implicit_accesses(str(_fixture(tmp_path))) == (
        _line("Task_10ms", reads=1, writes=2, runnables=2),
        _line("Task_100ms", reads=1),
        ImplicitLine(OCCURRENCE, 1, 0, occurrence="BrakeController", runnable="Diagnose", case=IN_NO_TASK),
        ImplicitLine(OCCURRENCE, 0, 1, occurrence="RearAxle/Sensor", runnable="SampleSpeed", case=IN_NO_TASK),
    )


def test_the_text_ends_with_the_section_in_the_plans_form(tmp_path: Path) -> None:
    text = resource_report(str(_fixture(tmp_path))).render()

    assert text.splitlines()[-len(FIXTURE_SECTION) :] == FIXTURE_SECTION
    assert text.splitlines()[-len(FIXTURE_SECTION) - 1] == ""
    assert text.count("IMPLICIT ACCESS") == 1


def test_the_section_is_additive_the_rest_of_every_surface_is_the_report_without_the_forms(tmp_path: Path) -> None:
    """The fixture with every ``by: implicit`` removed reports exactly what the fixture reports outside the section."""
    with_forms = resource_report(str(_fixture(tmp_path)))
    plain_root = tmp_path / "plain"
    shutil.copytree(FIXTURE, plain_root)
    for path in (plain_root / "swc").rglob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("    by: implicit\n", ""), encoding="utf-8", newline="\n")
    without = resource_report(str(plain_root))

    assert without.implicit == ()
    assert dataclasses.replace(with_forms, implicit=(), model_root=without.model_root).render() == without.render()
    assert with_forms.render().startswith(without.render().replace(str(plain_root), with_forms.model_root))
    for surface in (
        lambda report: resource_report_module.report_markdown(report, "Demo"),
        resource_report_module.format_report_json,
        lambda report: json.dumps(resource_report_module.overview(report)),
    ):
        assert surface(dataclasses.replace(with_forms, implicit=())) == surface(without)
    assert "IMPLICIT" not in without.render()
    assert "Implicit access" not in resource_report_module.report_markdown(without, "Demo")
    assert "implicit" not in json.loads(resource_report_module.format_report_json(without))
    assert "implicit" not in resource_report_module.overview(without)


# The counting rule


def test_a_runnable_occurrence_mapped_by_two_entries_of_one_task_counts_once(tmp_path: Path) -> None:
    model = _Model(tmp_path)
    model.reader(reads=2, writes=1)
    model.composition("Vehicle", {"R": "Reader"}, root=True)
    model.task("Task_10ms", 0.01, _swc("Reader"), _instance("R"))

    assert model.lines() == (_line("Task_10ms", reads=2, writes=1),)


def test_a_runnable_occurrence_mapped_to_two_tasks_counts_in_each(tmp_path: Path) -> None:
    model = _Model(tmp_path)
    model.reader()
    model.composition("Vehicle", {"R": "Reader"}, root=True)
    model.task("Task_A", 0.02, _swc("Reader"))
    model.task("Task_B", 0.01, _instance("R"))

    # Shortest period first.
    assert model.lines() == (_line("Task_B", reads=1), _line("Task_A", reads=1))


def test_two_occurrences_of_one_type_in_one_task_are_two_runnables(tmp_path: Path) -> None:
    model = _Model(tmp_path)
    model.reader()
    model.composition("Box", {"R": "Reader"})
    model.composition("Vehicle", {"Front": "@Box", "R": "Reader"}, root=True)
    model.task("Task_10ms", 0.01, _instance("R"), _instance("Front/R"))

    assert model.lines() == (_line("Task_10ms", reads=2, runnables=2),)


def test_an_instance_ambiguous_entry_maps_nothing(tmp_path: Path) -> None:
    model = _Model(tmp_path)
    model.reader()
    model.composition("Box", {"R": "Reader"})
    model.composition("Vehicle", {"Front": "@Box", "R": "Reader"}, root=True)
    model.task("Task_10ms", 0.01, _swc("Reader"))

    assert model.lines() == (_unmapped("Front/R", reads=1), _unmapped("R", reads=1))


def test_a_task_mapping_no_implicit_access_has_no_line(tmp_path: Path) -> None:
    model = _Model(tmp_path)
    model.reader()
    model.put("swc", {"name": "Quiet", "type": "application", "runnables": [{"name": "Run"}]}, group="g")
    model.composition("Vehicle", {"R": "Reader", "Q": "Quiet"}, root=True)
    model.task("Task_10ms", 0.01, _swc("Reader"))
    model.task("Task_5ms", 0.005, _swc("Quiet"))

    assert model.lines() == (_line("Task_10ms", reads=1),)


def test_a_task_whose_partition_does_not_resolve_names_no_core(tmp_path: Path) -> None:
    model = _Model(tmp_path)
    model.reader()
    model.composition("Vehicle", {"R": "Reader"}, root=True)
    model.task("Task_10ms", 0.01, _swc("Reader"), partition="Nowhere")

    assert model.lines() == (_line("Task_10ms", reads=1, core=None),)
    assert resource_report_module.implicit_rows(model.lines())["tasks"] == [
        {"task": "Task_10ms", "core": None, "reads": 1, "writes": 0, "runnables": 1}
    ]
    assert resource_report_module.implicit_text(model.lines())[2:4] == [
        "  Task       Core          Implicit reads  Implicit writes  Runnables",
        "  Task_10ms  core unknown  1               0                1",
    ]
    assert "| `Task_10ms` | core unknown | 1 | 0 | 1 |" in resource_report_module.implicit_markdown(model.lines())


def test_a_type_placed_in_no_composition_is_one_line_named_by_its_type(tmp_path: Path) -> None:
    model = _Model(tmp_path)
    model.reader()
    model.put("swc", {"name": "Quiet", "type": "application", "runnables": [{"name": "Run"}]}, group="g")
    model.composition("Vehicle", {"Q": "Quiet"}, root=True)
    model.task("Task_10ms", 0.01, _swc("Reader"))

    assert model.lines() == (_unmapped("Reader", reads=1, case=PLACED_NOWHERE),)
    # No task maps an implicit access: a sentence stands for the empty task table, and the runnable's cell
    # carries why it has no occurrence.
    assert resource_report_module.implicit_text(model.lines())[2:] == [
        "  No task maps an implicit access.",
        "  RUNNABLES IN NO TASK",
        "    Runnable                               Implicit reads  Implicit writes",
        "    Reader.Run (placed in no composition)  1               0",
    ]
    assert resource_report_module.implicit_markdown(model.lines())[4:] == [
        "No task maps an implicit access.",
        "",
        "### Runnables in no task",
        "",
        "| Runnable | Implicit reads | Implicit writes |",
        "| --- | --- | --- |",
        "| `Reader.Run` (placed in no composition) | 1 | 0 |",
        "",
    ]
    assert resource_report_module.implicit_rows(model.lines())["noTask"] == [
        {"occurrence": "Reader", "runnable": "Run", "reads": 1, "writes": 0, "case": PLACED_NOWHERE}
    ]


def test_an_external_component_is_passed_over(tmp_path: Path) -> None:
    model = _Model(tmp_path)
    model.put(
        "swc",
        {
            "name": "Outside",
            "type": "application",
            "external": True,
            "external_ref": "/Foreign/Outside",
            "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
        },
        group="g",
    )

    assert model.lines() == ()


# The walk undefined, and the cap


@pytest.mark.parametrize("shape", ["no root", "two roots", "a cycle the root reaches"])
def test_an_undefined_walk_maps_by_the_syntactic_identity(tmp_path: Path, shape: str) -> None:
    """A ``swc`` entry maps its type's runnable; an ``instance`` entry maps nothing; a line names the type."""
    model = _Model(tmp_path)
    model.reader("Reader")
    model.reader("Other")
    if shape == "no root":
        model.composition("Vehicle", {"R": "Reader", "O": "Other"})
    elif shape == "two roots":
        model.composition("Vehicle", {"R": "Reader"}, root=True)
        model.composition("Spare", {"O": "Other"}, root=True)
    else:
        model.composition("Vehicle", {"R": "Reader", "O": "Other", "Loop": "@Inner"}, root=True)
        model.composition("Inner", {"Back": "@Vehicle"})
    model.task("Task_10ms", 0.01, _swc("Reader"), _instance("O"))

    assert model.lines() == (_line("Task_10ms", reads=1), _unmapped("Other", reads=1))


def test_occurrences_past_the_cap_are_counted_and_one_mapped_beyond_it_counts_in_its_task(tmp_path: Path) -> None:
    model = _Model(tmp_path)
    model.reader()
    model.composition("Wrap", {"R": "Reader"})
    placed = PATH_CAP + 3
    model.composition("Vehicle", {f"W{index:03d}": "@Wrap" for index in range(placed)}, root=True)
    beyond = f"W{placed - 1:03d}/R"
    model.task("Task_10ms", 0.01, _instance("W000/R"), _instance(beyond))

    lines = model.lines()

    assert lines[0] == _line("Task_10ms", reads=2, runnables=2)
    unmapped = [line.occurrence for line in lines if line.shape == OCCURRENCE]
    assert unmapped == [f"W{index:03d}/R" for index in range(1, PATH_CAP)]
    assert beyond not in unmapped
    assert lines[-1] == ImplicitLine(PAST_THE_CAP_LINE, occurrence="Reader", case=PAST_THE_CAP, more=3)
    # The unwalked occurrences are no row of the table: a sentence under it counts them.
    sentence = f"Reader: the occurrences past the first {PATH_CAP} are not walked (3 more)"
    text = resource_report_module.implicit_text(lines)
    assert text[-1] == f"    {sentence}"
    assert text[-2].startswith(f"    W{PATH_CAP - 1:03d}/R.Run ")
    assert resource_report_module.implicit_markdown(lines)[-2:] == [f"- `Reader`: {sentence[len('Reader: ') :]}", ""]
    assert resource_report_module.implicit_rows(lines)["notWalked"] == [
        {"type": "Reader", "case": PAST_THE_CAP, "more": 3}
    ]


# The read discipline


def _broken(model_root: Path, layer: str) -> None:
    (model_root / layer).mkdir(parents=True, exist_ok=True)
    (model_root / layer / "Broken.yaml").write_text("name: [unclosed\n", encoding="utf-8", newline="\n")


def test_a_model_with_no_implicit_access_reads_no_other_layer(tmp_path: Path) -> None:
    """The ``ARRIVALS`` test's twin: ``swc/`` alone is parsed."""
    model = _Model(tmp_path)
    model.put(
        "swc",
        {
            "name": "Plain",
            "type": "application",
            "runnables": [{"name": "Run", "reads": [{"port": "r", "element": "x"}]}],
        },
        group="g",
    )
    for layer in ("tasks", "compositions", "connectors", "hardware"):
        _broken(tmp_path, layer)

    assert implicit_accesses(str(tmp_path)) == ()


def test_a_missing_model_root_answers_nothing(tmp_path: Path) -> None:
    assert implicit_accesses(str(tmp_path / "absent")) == ()


def test_with_an_implicit_access_the_tasks_are_read_and_the_connectors_never(tmp_path: Path) -> None:
    model = _Model(tmp_path)
    model.reader()
    model.composition("Vehicle", {"R": "Reader"}, root=True)
    _broken(tmp_path, "connectors")
    assert model.lines() == (_unmapped("R", reads=1),)

    _broken(tmp_path, "tasks")
    with pytest.raises(ValueError, match="Broken.yaml"):
        model.lines()


def test_the_command_reads_the_tasks_once_an_implicit_access_exists_even_with_no_hardware(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``test_cli_resource_report_layer_absent_reads_swc_for_the_arrivals``' twin: exit 1, the file by storage name."""
    manifest = _write_manifest(tmp_path)
    model_root = tmp_path / "model"
    model = _Model(model_root)
    shutil.rmtree(model_root / "hardware")
    model.reader()
    _broken(model_root, "tasks")
    monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest)])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Broken.yaml" in captured.err


# The surfaces


def test_the_markdown_carries_the_section_as_two_tables_with_its_sentence_once(tmp_path: Path) -> None:
    text = resource_report_module.report_markdown(resource_report(str(_fixture(tmp_path))), "Demo")

    assert text.endswith(FIXTURE_MARKDOWN)
    assert text.count(resource_report_module.IMPLICIT_NOTE) == 1


def test_the_json_document_adds_the_two_keys_as_rows_and_keeps_its_version(tmp_path: Path) -> None:
    document = json.loads(resource_report_module.format_report_json(resource_report(str(_fixture(tmp_path)))))

    assert list(document)[-2:] == ["implicit", "implicitNote"]
    assert document["schemaVersion"] == resource_report_module.RESOURCE_REPORT_SCHEMA_VERSION == 1
    assert document["implicit"] == FIXTURE_ROWS
    assert document["implicitNote"] == resource_report_module.IMPLICIT_NOTE


def test_the_mcp_overview_carries_the_rows_bounded_as_the_arrivals_are(tmp_path: Path) -> None:
    report = resource_report(str(_fixture(tmp_path)))

    answer = resource_report_module.overview(report)

    assert list(answer) == ["response", "cores", "memories", "findings", "topology", "implicit"]
    assert answer["implicit"] == FIXTURE_ROWS | {"more": 0, "note": resource_report_module.IMPLICIT_NOTE}


def test_the_mcp_overview_carries_the_first_rows_and_counts_the_rest(tmp_path: Path) -> None:
    """The cap runs over the tables in their order: the task rows, then the runnables in no task, then the unwalked."""
    report = resource_report(str(_fixture(tmp_path)))
    named = resource_report_module.IMPLICIT_NAMED
    capped = ImplicitLine(PAST_THE_CAP_LINE, occurrence="Reader", case=PAST_THE_CAP, more=3)
    many = dataclasses.replace(report, implicit=report.implicit[:1] * (named - 1) + report.implicit[2:] + (capped,))

    implicit = resource_report_module.overview(many)["implicit"]

    assert implicit == {
        "tasks": [FIXTURE_ROWS["tasks"][0]] * (named - 1),
        "noTask": FIXTURE_ROWS["noTask"][:1],
        "notWalked": [],
        "more": 2,
        "note": resource_report_module.IMPLICIT_NOTE,
        "rest": resource_report_module.IMPLICIT_REST,
    }
    assert sum(line.startswith("  Task_10ms ") for line in many.render().splitlines()) == named - 1


def test_the_mcp_overview_bounds_the_first_rows_in_bytes_too(tmp_path: Path) -> None:
    report = resource_report(str(_fixture(tmp_path)))
    deep = "/".join(f"Level{index:02d}" + "x" * 120 for index in range(40))
    long_line = dataclasses.replace(report.implicit[2], occurrence=deep)
    long = dataclasses.replace(report, implicit=(long_line,) * 20)

    implicit = resource_report_module.overview(long)["implicit"]

    kept = len(implicit["noTask"])
    assert 0 < kept < resource_report_module.IMPLICIT_NAMED
    assert implicit["tasks"] == [] and implicit["notWalked"] == []
    assert implicit["more"] == 20 - kept
    assert implicit["rest"] == resource_report_module.IMPLICIT_REST
    wire = len(json.dumps(json.dumps(implicit["noTask"], ensure_ascii=False), ensure_ascii=False).encode("utf-8"))
    assert wire <= resource_report_module.IMPLICIT_FIRST_BYTES
    assert len(json.loads(resource_report_module.format_report_json(long))["implicit"]["noTask"]) == 20


def test_the_findings_line_does_not_count_the_section(tmp_path: Path) -> None:
    report = resource_report(str(_fixture(tmp_path)))
    without = dataclasses.replace(report, implicit=())

    assert report.implicit
    assert (report.error_count, report.info_count) == (without.error_count, without.info_count)


def test_the_command_prints_the_section_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _write_manifest(tmp_path)
    shutil.copytree(FIXTURE, tmp_path / "model")
    monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest)])

    main()  # exits 0: it must not raise

    assert capsys.readouterr().out.splitlines()[-len(FIXTURE_SECTION) :] == FIXTURE_SECTION


# The leaf


def test_the_leaf_imports_the_schema_the_reader_and_the_scheduling_identity_and_nothing_else() -> None:
    """Neither the validator nor the writer is reached."""
    tree = ast.parse(Path(implicit_access.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("swcraft_tools"):
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names if alias.name.startswith("swcraft_tools")}
    assert imported == {"swcraft_tools.schema", "swcraft_tools.reader", "swcraft_tools.scheduling_identity"}
