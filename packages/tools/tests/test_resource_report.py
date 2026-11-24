"""Tests for the resource utilization report.

The report **recomputes nothing**: it renders :func:`analyze_resource_budgets`'s
result. Three properties are load-bearing:

- ``advisory`` resources produce ``info`` findings, which ``swcraft validate``
  never prints (it prints *issues* only); this report is their **only** channel.
- there is **one** finding level, rendered inside its owning resource's block:
  no ``Model-wide findings:`` section, field or reader.
- the model-wide facts a reader does want are **composed by the caller**:
  ``cli.cmd_resource_report`` runs ``check_hardware_topology`` and hands the
  records to :func:`resource_report`, which renders them in a ``TOPOLOGY``
  section and imports no gate of its own. The rendering is asserted here, the
  composition in ``test_cli.py``.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import pathlib
import shutil
from fractions import Fraction
from types import SimpleNamespace

import pytest
from conftest import (
    AGENT_BENCH,
    AGENT_TASK_BENCH,
    REPO_ROOT,
    SAFETY_MODEL_ROOT,
    _core,
    _memory,
    _partition,
    _processor,
    _swc,
    _task,
    _write,
    _write_manifest,
    element_path,
)
from swcraft_tools import resource_report as resource_report_module
from swcraft_tools.cli import main
from swcraft_tools.resource_analysis import (
    FINDING_MISSING_BUDGET,
    SEVERITY_INFO,
    STATUS_BREACH,
    STATUS_UNKNOWN,
    ResourceAnalysis,
    analyze_resource_budgets,
    budget_status,
    fits,
    fits_size,
    format_whole_bytes,
    partition_shares,
)
from swcraft_tools.resource_report import ResourceReport, resource_report
from swcraft_tools.validator import check_hardware_capability, check_hardware_topology, check_resource_budgets
from swcraft_tools.writer import needs_export

#: The multi-core, multi-memory acceptance fixture model.
_FIXTURE_MODEL = str(pathlib.Path(__file__).parent / "fixtures" / "hardware" / "model")

#: The one demo model the repository ships that declares a processor, a core and
#: a memory — the model the golden below was captured from.
_DEMO_MODEL = REPO_ROOT / "examples" / "basic" / "model"

#: `examples/basic`'s report **from line 2 on**. The fixture's README says why
#: line 1 is not in it and how it is regenerated: only after an accepted
#: rendering change, never to make a diff go away.
_GOLDEN = pathlib.Path(__file__).parent / "fixtures" / "resource_report_golden" / "basic-before.txt"


def _lines(text: str) -> list[str]:
    return text.splitlines()


# ---------------------------------------------------------------------------
# Per-resource rendering, both kinds
# ---------------------------------------------------------------------------


class TestCpuRendering:
    def test_cpu_resource_renders_consumption_threshold_headroom_and_breakdown(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.7)
        _task(tmp_path, "Task_1ms", period=0.001, priority=30, partition="PARTITION0", wcet="400us")
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, partition="PARTITION0", wcet="1500us")

        rendered = resource_report(str(tmp_path)).render()

        assert "RESOURCE CORE0 (core, strict)" in rendered
        assert "  STATUS within budget" in rendered
        assert "  UTILIZATION 55.00% of a 70.00% threshold (headroom 15.00%)" in rendered
        assert "  CONSUMERS 2 task(s), 0 without a declared budget" in rendered
        assert "    - Task_1ms: 40.00% (wcet 400us / period 0.001s)" in rendered
        assert "    - Task_10ms: 15.00% (wcet 1500us / period 0.01s)" in rendered

    def test_a_task_without_a_declared_budget_is_counted_and_marked(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.7)
        _task(tmp_path, "Task_budgeted", period=0.01, priority=10, partition="PARTITION0", wcet="1500us")
        _task(tmp_path, "Task_untracked", period=0.02, priority=5, partition="PARTITION0")

        rendered = resource_report(str(tmp_path)).render()

        assert "  CONSUMERS 2 task(s), 1 without a declared budget" in rendered
        assert "    - Task_untracked: not declared (period 0.02s, no wcet declared)" in rendered

    def test_a_core_with_no_tasks_renders_an_empty_breakdown(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.7)

        rendered = resource_report(str(tmp_path)).render()

        assert "  CONSUMERS 0 task(s), 0 without a declared budget" in rendered
        assert "  UTILIZATION 0.00% of a 70.00% threshold (headroom 70.00%)" in rendered


class TestMemoryRendering:
    def test_memory_resource_renders_consumption_budget_headroom_and_breakdown(self, tmp_path):
        _memory(tmp_path, "RAM", size=4096, threshold=0.8)
        _swc(tmp_path, "Alpha", memory={"RAM": 1024})
        _swc(tmp_path, "Beta", memory={"RAM": 512})

        rendered = resource_report(str(tmp_path)).render()

        assert "RESOURCE RAM (memory, size 4096 bytes, strict)" in rendered
        assert "  STATUS within budget" in rendered
        assert "  CONSUMPTION 1536 bytes of a 3276.8 byte budget (headroom 1740.8 bytes)" in rendered
        assert "  CONSUMERS 2 SWC(s), 0 without a declared budget" in rendered
        assert "    - Alpha: 1024 bytes" in rendered
        assert "    - Beta: 512 bytes" in rendered

    def test_an_swc_without_a_declared_budget_is_counted_and_marked(self, tmp_path):
        _memory(tmp_path, "RAM", size=4096, threshold=0.8)
        _swc(tmp_path, "Alpha", memory={"RAM": 1024})
        _swc(tmp_path, "Beta")

        rendered = resource_report(str(tmp_path)).render()

        assert "  CONSUMERS 2 SWC(s), 1 without a declared budget" in rendered
        assert "    - Beta: not declared" in rendered


# ---------------------------------------------------------------------------
# Breach marking
# ---------------------------------------------------------------------------


class TestBreachMarking:
    def test_a_cpu_breach_is_marked_over_budget_and_lists_the_finding(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.5)
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, partition="PARTITION0", wcet="8000us")

        rendered = resource_report(str(tmp_path)).render()

        assert "  STATUS over budget" in rendered
        assert "within budget" not in rendered
        assert "    - [error] threshold_breach:" in rendered
        assert "  UTILIZATION 80.00% of a 50.00% threshold (headroom -30.00%)" in rendered

    def test_a_memory_breach_is_marked_over_budget(self, tmp_path):
        _memory(tmp_path, "RAM", size=1000, threshold=0.8)
        _swc(tmp_path, "Alpha", memory={"RAM": 801})

        rendered = resource_report(str(tmp_path)).render()

        assert "  STATUS over budget" in rendered
        assert "  CONSUMPTION 801 bytes of a 800 byte budget (headroom -1 bytes)" in rendered

    def test_a_non_threshold_finding_does_not_mark_the_resource_over_budget(self, tmp_path):
        # A rate-monotonic violation is a finding, but the core is not over its
        # load threshold — the status line must say so.
        _core(tmp_path, "CORE0", threshold=0.9)
        _task(tmp_path, "Task_10ms", period=0.01, priority=5, partition="PARTITION0", wcet="100us")
        _task(tmp_path, "Task_100ms", period=0.1, priority=20, partition="PARTITION0", wcet="1000us")

        rendered = resource_report(str(tmp_path)).render()

        assert "  STATUS within budget" in rendered
        assert "    - [error] rate_monotonic_violation:" in rendered


# ---------------------------------------------------------------------------
# advisory `info` findings — this report is their one channel
# ---------------------------------------------------------------------------


class TestAdvisoryFindingsAreRenderedHere:
    def test_an_advisory_breach_is_rendered_although_validate_never_prints_it(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.5, enforcement="advisory")
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, partition="PARTITION0", wcet="8000us")

        analysis = analyze_resource_budgets(str(tmp_path))
        message = analysis.resources[0].findings[0].message
        assert analysis.resources[0].findings[0].severity == SEVERITY_INFO

        # `swcraft validate` prints issues only, and an advisory finding is
        # never an issue — this report is its one channel.
        assert check_resource_budgets(str(tmp_path)).issues == []

        rendered = resource_report(str(tmp_path)).render()
        assert f"    - [info] threshold_breach: {message}" in rendered

    def test_an_advisory_missing_budget_is_rendered_as_info(self, tmp_path):
        _memory(tmp_path, "RAM", size=1024, threshold=0.8, enforcement="advisory")
        _swc(tmp_path, "Alpha")

        rendered = resource_report(str(tmp_path)).render()

        assert check_resource_budgets(str(tmp_path)).issues == []
        assert "    - [info] missing_budget:" in rendered

    def test_the_report_counts_both_severities_separately(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.5)
        _core(tmp_path, "CORE1", threshold=0.5, enforcement="advisory")
        _task(tmp_path, "Task_a", period=0.01, priority=10, partition="PARTITION0", wcet="8000us")
        _task(tmp_path, "Task_b", period=0.01, priority=10, partition="PARTITION1", wcet="8000us")

        report = resource_report(str(tmp_path))

        assert report.error_count == 1
        assert report.info_count == 1
        assert "FINDINGS 1 error(s), 1 informational" in report.render()


# ---------------------------------------------------------------------------
# No model-wide finding level
# ---------------------------------------------------------------------------


class TestTheDeletedModelWideChannel:
    """``ResourceAnalysis.findings`` and the ``Model-wide findings:`` section are gone.

    Reachability is ``rule:hardware.unreachable-memory`` of ``check_hardware_topology``:
    a call from ``resource_analysis`` into the validator would invert the boundary
    that module holds.

    What this class does **not** reach: where the finding a reader wants *is*
    reported. That is ``test_hardware_topology.py``'s; the composition into this
    command's output is ``cmd_resource_report``'s, asserted in ``test_cli.py`` and,
    on the renderer's side, in :class:`TestTheComposedTopologySection` below.
    """

    def test_the_analysis_has_no_model_wide_finding_level_at_all(self, tmp_path):
        """The field is deleted, not merely empty: asserted on the dataclass, not an instance."""
        _core(tmp_path, "CORE0", threshold=1.0)
        _task(tmp_path, "Task_declared", period=0.01, priority=10, partition="PARTITION0", wcet="10us")

        analysis = analyze_resource_budgets(str(tmp_path))

        assert not hasattr(analysis, "findings")
        assert "findings" not in {f.name for f in dataclasses.fields(ResourceAnalysis)}

    def test_no_model_reaches_a_model_wide_section_because_there_is_none(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=1.0)
        _task(tmp_path, "Task_declared", period=0.01, priority=10, partition="PARTITION0", wcet="10us")

        assert "Model-wide findings:" not in resource_report(str(tmp_path)).render()

    @pytest.mark.parametrize("module", [resource_report_module, needs_export])
    def test_no_renderer_can_still_emit_the_section(self, module):
        """No string literal of a renderer spells the heading, so the section's *code* is gone too.

        A report with no model-wide finding would pass the test above with the branch
        still in the module. ``needs_export`` is included so a section brought into the
        requirements output cannot bring the heading with it.

        What this does **not** reach: prose. Docstrings are excluded on purpose, so a docstring
        may name the heading.
        """
        tree = ast.parse(pathlib.Path(inspect.getsourcefile(module)).read_text(encoding="utf-8"))
        literals = [
            node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        docstrings = {
            ast.get_docstring(node, clean=False)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef)
        }
        assert not [text for text in literals if "Model-wide" in text and text not in docstrings]

    def test_a_budget_against_an_undeclared_memory_is_reported_by_check_model_not_here(self, tmp_path):
        """Both halves: ``check-model`` reports it and this report does not.

        The "not here" half alone would not say the finding is reported anywhere.
        """
        _processor(tmp_path, "CPU")
        _core(tmp_path, "CORE0", threshold=1.0)
        _memory(tmp_path, "RAM", size=1024, threshold=0.8)
        _swc(tmp_path, "Alpha", memory={"RAM": 128, "NoSuchMemory": 64})

        report = resource_report(str(tmp_path))

        assert report.analysis.all_findings() == []
        assert "Model-wide findings:" not in report.render()

        topology = check_hardware_topology(str(tmp_path))
        assert not topology.consistent
        assert {record.rule for record in topology.records} == {"hardware.undeclared-memory"}

    def test_a_task_on_an_undeclared_core_is_invisible_to_this_renderer_on_its_own(self, tmp_path):
        """The renderer's documented narrowness: called with no records, it renders no orphan task.

        A task on an undeclared core matches no `ResourceUsage` -- no block, no consumer
        row, no finding -- and the summary still reads `FINDINGS 0 error(s), 0 informational`.
        The *command* reports it in a `TOPOLOGY` section, asserted at the CLI where the
        composition happens:
        `test_cli.py::TestCliResourceReport::test_cli_resource_report_reports_a_task_in_an_undeclared_partition`.
        """
        _processor(tmp_path, "CPU")
        _core(tmp_path, "CORE0", threshold=0.7)
        _task(tmp_path, "Task_orphan", period=0.01, priority=10, partition="PARTITION9", wcet="9000us")

        rendered = resource_report(str(tmp_path)).render()

        assert "FINDINGS 0 error(s), 0 informational" in rendered
        assert "TOPOLOGY" not in rendered
        assert "Task_orphan" not in rendered
        assert "PARTITION9" not in rendered
        assert "RESOURCE CORE0 (core, strict)" in rendered
        assert "  STATUS within budget" in rendered
        assert "  CONSUMERS 0 task(s), 0 without a declared budget" in rendered

        topology = check_hardware_topology(str(tmp_path))
        assert not topology.consistent
        assert {record.rule for record in topology.records} == {"hardware.undeclared-partition"}


# ---------------------------------------------------------------------------
# The composed topology section
# ---------------------------------------------------------------------------


class TestTheComposedTopologySection:
    """What the renderer does with the records a caller hands it, and only that.

    Given records it renders them, given none nothing; either way it computes no
    topology and counts none of it into `FINDINGS`. The composition is `test_cli.py`'s.
    """

    def test_the_section_renders_the_records_it_is_handed(self, tmp_path):
        """One line per record: the rule id, then the rule's own words.

        The records come from a real `check_hardware_topology` run, so the line is one a
        caller can actually produce.
        """
        _processor(tmp_path, "CPU")
        _core(tmp_path, "CORE0", threshold=0.7)
        _task(tmp_path, "Task_orphan", period=0.01, priority=10, partition="PARTITION9", wcet="9000us")

        records = check_hardware_topology(str(tmp_path)).records
        rendered = resource_report(str(tmp_path), records).render()

        assert "TOPOLOGY 1 finding(s)" in rendered
        assert f"  - hardware.undeclared-partition: {records[0].message}" in rendered

    def test_the_section_stands_between_the_summary_and_the_first_resource(self, tmp_path):
        """A topology finding is about the model, so it stands after the summary, before the blocks.

        Separated by the one blank line every section of this report is.
        """
        _processor(tmp_path, "CPU")
        _core(tmp_path, "CORE0", threshold=0.7)
        _task(tmp_path, "Task_orphan", period=0.01, priority=10, partition="PARTITION9", wcet="9000us")

        records = check_hardware_topology(str(tmp_path)).records
        lines = _lines(resource_report(str(tmp_path), records).render())
        summary = lines.index("FINDINGS 0 error(s), 0 informational")

        assert lines[summary + 1] == ""
        assert lines[summary + 2] == "TOPOLOGY 1 finding(s)"
        assert lines[summary + 3].startswith("  - hardware.undeclared-partition: ")
        assert lines[summary + 4] == ""
        assert lines[summary + 5] == "RESOURCE CORE0 (core, strict)"

    def test_every_record_gets_a_line_and_the_count_is_the_records(self, tmp_path):
        """Two findings, two lines, and a header that says two.

        A count taken from anything else would still read plausibly on a one-finding model.
        """
        _core(tmp_path, "CORE0", threshold=0.7)
        _task(tmp_path, "Task_orphan", period=0.01, priority=10, partition="PARTITION9", wcet="9000us")

        records = check_hardware_topology(str(tmp_path)).records
        rendered = resource_report(str(tmp_path), records).render()
        rules = {record.rule for record in records}

        assert rules == {"hardware.no-processor", "hardware.undeclared-partition"}
        assert f"TOPOLOGY {len(records)} finding(s)" in rendered
        assert len([line for line in _lines(rendered) if line.startswith("  - hardware.")]) == len(records)

    def test_a_caller_that_passes_nothing_renders_no_section(self, tmp_path):
        """The default: a model *with* topology findings renders no section unless handed them."""
        _core(tmp_path, "CORE0", threshold=0.7)
        _task(tmp_path, "Task_orphan", period=0.01, priority=10, partition="PARTITION9", wcet="9000us")

        assert not check_hardware_topology(str(tmp_path)).consistent
        assert "TOPOLOGY" not in resource_report(str(tmp_path)).render()

    def test_the_section_does_not_move_the_finding_counts(self, tmp_path):
        """`FINDINGS` counts budget findings, and a topology record has no severity.

        Folding them in would make `FINDINGS 0 error(s)` depend on the caller.
        """
        _processor(tmp_path, "CPU")
        _core(tmp_path, "CORE0", threshold=0.7)
        _task(tmp_path, "Task_orphan", period=0.01, priority=10, partition="PARTITION9", wcet="9000us")

        records = check_hardware_topology(str(tmp_path)).records
        report = resource_report(str(tmp_path), records)

        assert report.error_count == 0
        assert report.info_count == 0
        assert "FINDINGS 0 error(s), 0 informational" in report.render()

    def test_the_renderer_imports_no_gate(self):
        """`resource_report` imports no validator; the seam is the structural `TopologyRecord`.

        What this does **not** reach: a transitive import. It reads this module's own
        `import` statements, so a validator reached through a third module would pass.
        """
        source = pathlib.Path(inspect.getsourcefile(resource_report_module)).read_text(encoding="utf-8")
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        assert imported, "resource_report.py imports nothing -- has the module moved?"
        assert not [module for module in imported if "validator" in module]


# ---------------------------------------------------------------------------
# The report is byte-identical where the semantics did not change
# ---------------------------------------------------------------------------


class TestTheByteIdentityOfTheDemoReport:
    """The demo model's report against its golden file.

    The golden is `packages/tools/tests/fixtures/resource_report_golden/basic-before.txt`.

    What it does **not** reach, and the fixture's README says the same: one model
    (`examples/basic`), one host, one renderer; the requirements output; and a model
    that produces a topology finding, whose `TOPOLOGY` section a clean model lacks.
    """

    def test_the_demo_models_report_is_unchanged_from_line_2_on(self):
        rendered = resource_report(str(_DEMO_MODEL)).render()

        body = "\n".join(rendered.split("\n")[1:]) + "\n"

        assert body == _GOLDEN.read_text(encoding="utf-8")

    def test_the_header_line_is_asserted_by_shape_because_it_holds_an_absolute_path(self):
        """The one line a byte-for-byte golden could never match twice.

        Line 1 is `Resource utilization report: {model_root}`, and `model_root`
        is an absolute path that differs per checkout.
        """
        rendered = resource_report(str(_DEMO_MODEL)).render()

        assert _lines(rendered)[0] == f"Resource utilization report: {_DEMO_MODEL}"

    def test_the_demo_model_produces_no_topology_finding(self):
        """No `TOPOLOGY` section is due, so the byte-identity is not a caller passing nothing."""
        assert check_hardware_topology(str(_DEMO_MODEL)).consistent


# ---------------------------------------------------------------------------
# The command carries neither placement finding
# ---------------------------------------------------------------------------


def test_the_command_names_neither_placement_rule_over_the_acceptance_fixture(tmp_path, monkeypatch, capsys):
    """`swcraft resource-report` composes `check_hardware_topology` and nothing else.

    The report has no safety section: a level is not a budget, the always-exits-0
    promise is about budgets, and a placement finding belongs to `check-model`.
    Asserted at the COMMAND, where the records are composed, over the acceptance
    fixture, which mints the placement findings in the check (`CORE0` rated below
    its `D` partitions, `RAM0` below `TorqueLimiter`, `P_Mixed`'s two lower
    components, every partition's `RAM0` below its `D`), so the absence is not vacuous.
    """
    shutil.copytree(SAFETY_MODEL_ROOT, tmp_path / "model")
    manifest = _write_manifest(tmp_path)
    assert {record.rule for record in check_hardware_capability(str(tmp_path / "model")).records} == {
        "safety.partition-on-unqualified-core",
        "safety.memory-below-component",
        "safety.component-below-partition",
        "safety.partition-memory-below-level",
    }
    monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest)])

    main()  # exits 0: it must not raise

    out = capsys.readouterr().out
    assert "RESOURCE RAM0 (memory" in out
    assert "safety." not in out
    assert "TOPOLOGY" not in out


# ---------------------------------------------------------------------------
# The opt-in: no resources declared at all
# ---------------------------------------------------------------------------


class TestNoBudgetDeclared:
    """The empty-analysis branch is **deleted**, and the header still tells the truth.

    ``hardware/`` is mandatory, so "No resources declared" names no state. A hand-built
    ``hardware/`` with no core and no memory renders three true lines: the header,
    ``DECLARED 0``, and a finding count of zero.
    """

    def test_a_model_with_no_budget_renders_the_header_and_a_zero_count(self, tmp_path):
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, partition="PARTITION0")
        _swc(tmp_path, "Alpha")

        report = resource_report(str(tmp_path))
        rendered = report.render()

        assert report.analysis.resources == []
        assert report.error_count == 0
        assert report.info_count == 0
        assert _lines(rendered) == [
            f"Resource utilization report: {tmp_path}",
            "",
            "DECLARED 0 resource(s)",
            "FINDINGS 0 error(s), 0 informational",
        ]
        assert "RESOURCE " not in rendered
        assert "No resources declared" not in rendered

    def test_an_empty_hardware_directory_is_treated_the_same(self, tmp_path):
        (tmp_path / "hardware").mkdir()

        assert "DECLARED 0 resource(s)" in resource_report(str(tmp_path)).render()


# ---------------------------------------------------------------------------
# Determinism and the acceptance fixture
# ---------------------------------------------------------------------------


class TestDeterminismAndAcceptanceFixture:
    def test_rendering_is_deterministic_across_runs(self):
        assert resource_report(_FIXTURE_MODEL).render() == resource_report(_FIXTURE_MODEL).render()

    def test_the_report_recomputes_nothing_and_renders_every_finding(self):
        report = resource_report(_FIXTURE_MODEL)
        rendered = report.render()

        analysis = analyze_resource_budgets(_FIXTURE_MODEL)
        for finding in analysis.all_findings():
            assert finding.message in rendered

        assert report.error_count == 3
        assert report.info_count == 1

    def test_the_fixture_renders_every_declared_resource_in_order(self):
        rendered = resource_report(_FIXTURE_MODEL).render()
        headers = [line for line in _lines(rendered) if line.startswith("RESOURCE ")]

        assert [h.split()[1] for h in headers] == ["CORE0", "CORE1", "CORE2", "CORE3", "RAM", "ROM"]

    def test_the_report_header_names_the_model_root_and_resource_count(self):
        rendered = resource_report(_FIXTURE_MODEL).render()

        assert rendered.startswith(f"Resource utilization report: {_FIXTURE_MODEL}")
        assert "DECLARED 6 resource(s)" in rendered

    def test_resource_report_returns_a_ResourceReport(self):
        report = resource_report(_FIXTURE_MODEL)

        assert isinstance(report, ResourceReport)
        assert report.model_root == _FIXTURE_MODEL


# ---------------------------------------------------------------------------
# The structured answers of the MCP tool: projections of the same report, held
# to the text `render()` prints for the same analysis
# ---------------------------------------------------------------------------

_BENCH_MODEL = AGENT_TASK_BENCH / "model"


def _blocks(rendered: str) -> dict[str, list[str]]:
    """The text report's ``RESOURCE`` blocks by resource name, each its lines in order."""
    blocks: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in _lines(rendered):
        if line.startswith("RESOURCE "):
            current = blocks.setdefault(line.split()[1], [])
        elif not line:
            current = None
        if current is not None:
            current.append(line)
    return blocks


def _aggregate(row: dict) -> str:
    """The aggregate line ``_render_usage`` prints, rebuilt from a structured row's strings."""
    if row["kind"] == "core":
        return f"  UTILIZATION {row['utilization']} of a {row['limit']} threshold (headroom {row['headroom']})"
    return f"  CONSUMPTION {row['used']} bytes of a {row['limit']} byte budget (headroom {row['headroom']} bytes)"


class TestStructuredAnswers:
    @pytest.mark.parametrize("model", [_FIXTURE_MODEL, str(_DEMO_MODEL), str(_BENCH_MODEL)])
    def test_the_overview_rows_and_findings_are_the_text_reports(self, model):
        report = resource_report(model)
        blocks = _blocks(report.render())
        answer = resource_report_module.overview(report)

        rows = answer["cores"] + answer["memories"]
        assert answer["response"] == "resources"
        assert sorted(row["name"] for row in rows) == sorted(blocks)
        for row in rows:
            block = blocks[row["name"]]
            header = f"RESOURCE {row['name']} ({row['kind']}"
            if row["kind"] == "memory":
                header += f", size {row['size']} bytes"
            assert block[0] == f"{header}, {row['enforcement']})"
            assert block[1] == f"  STATUS {row['status']}"
            assert block[2] == _aggregate(row)
        assert all("consumers" not in row and "tasks" not in row for row in rows)
        findings = analyze_resource_budgets(model).all_findings()
        kept = [f["message"] for f in answer["findings"] if f["code"] != FINDING_MISSING_BUDGET]
        assert kept == [f.message for f in findings if f.code != FINDING_MISSING_BUDGET]
        folded = sum(f["count"] for f in answer["findings"] if f["code"] == FINDING_MISSING_BUDGET)
        assert folded == sum(1 for f in findings if f.code == FINDING_MISSING_BUDGET)
        assert answer["topology"] == []

    def test_the_overview_carries_the_findings_of_an_advisory_resource(self):
        answer = resource_report_module.overview(resource_report(_FIXTURE_MODEL))

        severities = [finding["severity"] for finding in answer["findings"]]
        assert severities.count("error") == 3
        assert severities.count("info") == 1
        assert all(
            finding["resource"] in {"CORE0", "CORE1", "CORE2", "CORE3", "RAM", "ROM"} for finding in answer["findings"]
        )

    def test_the_overview_carries_the_topology_records_it_is_handed(self, tmp_path):
        _processor(tmp_path)
        _core(tmp_path, "CORE0")
        _task(tmp_path, "Task_10ms", partition="PARTITION9", wcet="1000us")
        records = check_hardware_topology(str(tmp_path)).records

        answer = resource_report_module.overview(resource_report(str(tmp_path), records))

        assert answer["topology"] == [{"rule": record.rule, "message": record.message} for record in records]
        assert any(entry["rule"] == "hardware.undeclared-partition" for entry in answer["topology"])

    def test_one_memory_lists_its_consumers_largest_first(self, tmp_path):
        _memory(tmp_path, "RAM", size=8192, threshold=0.8)
        _swc(tmp_path, "Alpha", memory={"RAM": 512})
        _swc(tmp_path, "Beta", memory={"RAM": 2048})
        _swc(tmp_path, "Gamma", memory={"RAM": 512})
        _swc(tmp_path, "Delta", memory={"RAM": 0})
        _swc(tmp_path, "Epsilon")

        answer = resource_report_module.resource_answer(resource_report(str(tmp_path)), "memory", "RAM")

        # Largest first; the two 512-byte budgets keep the report's (reader) order.
        assert answer["consumers"] == [
            {"name": "Beta", "bytes": 2048},
            {"name": "Alpha", "bytes": 512},
            {"name": "Gamma", "bytes": 512},
        ]
        assert (answer["undeclared"], answer["undeclared_first"]) == (1, ["Epsilon"])
        assert answer["declared_zero"] == 1
        assert answer["resource"]["used"] == "3072"
        assert [(f["code"], f["count"]) for f in answer["findings"]] == [(FINDING_MISSING_BUDGET, 1)]
        assert "memories" not in answer

    def test_a_missing_budget_is_counted_not_named_per_component(self, tmp_path):
        """Components that budget only the memory they use: the answers grow with the resources, not the model.

        Each component declares one of two memories and leaves the other out, so each memory has
        one ``missing_budget`` finding per component of the other -- the shape a model takes when
        nothing writes a ``0`` for a memory a component does not use.
        """
        _memory(tmp_path, "RAM0", size=65536, threshold=0.8)
        _memory(tmp_path, "RAM1", size=65536, threshold=0.8, enforcement="advisory")
        for index in range(30):
            _swc(tmp_path, f"Swc{index:02d}", memory={f"RAM{index % 2}": 128})
        report = resource_report(str(tmp_path))

        overview = resource_report_module.overview(report)
        answer = resource_report_module.resource_answer(report, "memory", "RAM0")

        assert [(f["resource"], f["code"], f["severity"], f["count"]) for f in overview["findings"]] == [
            ("RAM0", FINDING_MISSING_BUDGET, "error", 15),
            ("RAM1", FINDING_MISSING_BUDGET, "info", 15),
        ]
        assert (answer["undeclared"], len(answer["undeclared_first"])) == (15, resource_report_module.UNDECLARED_NAMED)
        assert answer["undeclared_first"] == [f"Swc{index:02d}" for index in range(1, 30, 2)][:10]
        assert [f["count"] for f in answer["findings"]] == [15]
        assert len(answer["consumers"]) == 15
        # The library states the count only; how to ask for the names is the caller's syntax.
        assert answer["findings"][0]["message"] == "15 consumer(s) of memory 'RAM0' declare no budget for it."
        pointed = resource_report_module.overview(report, ask_for=lambda kind, name: f" Ask {kind} {name}.")
        assert pointed["findings"][0]["message"].endswith("declare no budget for it. Ask memory RAM0.")

    def test_a_kind_the_model_declares_none_of_is_refused_naming_that(self, tmp_path):
        _core(tmp_path, "CORE0")

        with pytest.raises(resource_report_module.UnknownResource) as refused:
            resource_report_module.resource_answer(resource_report(str(tmp_path)), "memory", "RAM")

        assert str(refused.value) == "no memory named 'RAM': the model declares no memory."

    def test_the_bench_answers_its_largest_ram_consumer_first(self):
        """The small bench's task 17: the reference figures, the expected answer the first row."""
        task = json.loads((AGENT_BENCH / "tasks" / "17-largest-ram-consumer.json").read_text(encoding="utf-8"))
        answer = resource_report_module.resource_answer(resource_report(str(_BENCH_MODEL)), "memory", "RAM")

        assert {row["name"]: row["bytes"] for row in answer["consumers"]} == task["reference"]["consumers"]
        assert answer["consumers"][0]["name"] == task["expect"]["answer"]["first_of"]["expected"]

    def test_one_core_lists_its_tasks_and_the_memories_it_reaches(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.7)
        _core(tmp_path, "CORE1", threshold=0.7)
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, wcet="1500us")
        _task(tmp_path, "Task_1ms", period=0.001, priority=30, wcet="400us")
        _task(tmp_path, "Task_idle", period=0.1, priority=1)
        _memory(tmp_path, "RAM0", accessible=["CORE0"])
        _memory(tmp_path, "RAM1", accessible=["CORE1"])
        report = resource_report(str(tmp_path))

        answer = resource_report_module.resource_answer(report, "core", "CORE0")

        assert answer["resource"]["utilization"] == "55.00%"
        assert answer["tasks"] == [
            {"name": "Task_1ms", "utilization": "40.00%"},
            {"name": "Task_10ms", "utilization": "15.00%"},
        ]
        assert (answer["undeclared"], answer["undeclared_first"]) == (1, ["Task_idle"])
        overview_rows = {row["name"]: row for row in resource_report_module.overview(report)["memories"]}
        assert answer["memories"] == [overview_rows["RAM0"]]

    def test_a_fit_reports_the_figures_it_is_decided_on(self):
        """The small bench's task 09, against the hand-recorded reference, not against the report."""
        task = json.loads((AGENT_BENCH / "tasks" / "09-ram-for-a-component.json").read_text(encoding="utf-8"))
        reference = task["reference"]["RAM"]
        report = resource_report(str(_BENCH_MODEL))

        refused = resource_report_module.fits_answer(report, "memory", "RAM", Fraction(12288))
        accepted = resource_report_module.fits_answer(report, "memory", "RAM", Fraction(8192))

        assert refused["fits"] is False
        assert accepted["fits"] is True
        assert refused["size"] == reference["size"]
        assert refused["used"] == str(reference["consumed"])
        assert refused["within_threshold"] == {
            "fits": False,
            "limit": str(reference["budget"]),
            "headroom": str(reference["headroom"]),
        }
        assert refused["within_size"] == {
            "fits": True,
            "limit": str(reference["size"]),
            "headroom": str(reference["size"] - reference["consumed"]),
        }
        assert refused["answer"] == "fits physically, breaks the threshold by 2048 bytes"
        assert "limit" not in refused and "headroom" not in refused
        assert refused["asked"] == "12288"
        # The raw free space would fit the request: the threshold is what refuses it.
        assert reference["size"] - reference["consumed"] >= 12288

    def test_a_fit_on_an_unknown_status_is_null(self, tmp_path):
        _memory(tmp_path, "RAM", size=4096, threshold=0.8)
        _swc(tmp_path, "Alpha", memory={"RAM": 512})
        _swc(tmp_path, "Beta")

        answer = resource_report_module.fits_answer(resource_report(str(tmp_path)), "memory", "RAM", Fraction(1))

        assert answer["fits"] is None
        assert answer["status"].startswith("unknown")

    def test_a_core_fit_is_spelled_in_percent(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.7)
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, wcet="1500us")

        answer = resource_report_module.fits_answer(resource_report(str(tmp_path)), "core", "CORE0", Fraction("0.05"))

        assert (answer["asked"], answer["used"], answer["fits"]) == ("5.00%", "15.00%", True)
        assert answer["within_threshold"] == {"fits": True, "limit": "70.00%", "headroom": "55.00%"}
        assert answer["within_size"] == {"fits": True, "limit": "100.00%", "headroom": "85.00%"}
        assert answer["answer"] == resource_report_module.ANSWER_FITS
        assert "size" not in answer

    def test_an_unknown_name_lists_at_most_ten_declared_names(self, tmp_path):
        for index in range(12):
            _memory(tmp_path, f"RAM{index:02d}")

        with pytest.raises(resource_report_module.UnknownResource) as refused:
            resource_report_module.resource_answer(resource_report(str(tmp_path)), "memory", "RAMX")

        named = ", ".join(f"RAM{index:02d}" for index in range(resource_report_module.DECLARED_NAMED))
        assert str(refused.value) == f"no memory named 'RAMX'; the declared memories are {named} and 2 more."

    def test_a_name_two_resources_carry_is_refused_whatever_the_reader_order(self, tmp_path):
        """Sized 1000 and 100 at 0.8, a 200-byte fit is yes for one and no for the other: neither is the answer."""
        _memory(tmp_path, "RAM", size=1000)
        (tmp_path / "hardware" / "RAM_copy.yaml").write_text(
            "name: RAM\nkind: memory\ntype: RAM\nsize: 100\naccessible:\n  - CORE0\nthreshold: 0.8\n",
            encoding="utf-8",
            newline="\n",
        )
        _core(tmp_path, "CORE1")
        (tmp_path / "hardware" / "CORE1_mem.yaml").write_text(
            "name: CORE1\nkind: memory\ntype: RAM\nsize: 100\naccessible:\n  - CORE0\nthreshold: 0.8\n",
            encoding="utf-8",
            newline="\n",
        )
        report = resource_report(str(tmp_path))
        assert any(
            record.rule == "hardware.duplicate-name" for record in check_hardware_topology(str(tmp_path)).records
        )

        with pytest.raises(resource_report_module.AmbiguousResource, match="2 hardware elements are named 'RAM'"):
            resource_report_module.fits_answer(report, "memory", "RAM", Fraction(200))
        with pytest.raises(resource_report_module.AmbiguousResource):
            resource_report_module.resource_answer(report, "memory", "RAM")
        # One namespace across the kinds: a core and a memory sharing a name are refused as well.
        with pytest.raises(resource_report_module.AmbiguousResource, match="named 'CORE1'"):
            resource_report_module.resource_answer(report, "core", "CORE1")

    def test_an_unknown_name_is_refused_naming_the_declared_ones(self):
        report = resource_report(_FIXTURE_MODEL)

        with pytest.raises(
            resource_report_module.UnknownResource, match="no memory named 'RAM9'; the declared memories are RAM, ROM."
        ):
            resource_report_module.resource_answer(report, "memory", "RAM9")
        with pytest.raises(resource_report_module.UnknownResource, match="'RAM' is a memory, not a core"):
            resource_report_module.fits_answer(report, "core", "RAM", Fraction(0))


# ---------------------------------------------------------------------------
# The two fit verdicts: `fit_verdicts` and the physical headroom
# ---------------------------------------------------------------------------


def _verdicts(model_root, kind: str, name: str, amount: Fraction) -> dict:
    report = resource_report(str(model_root))
    return resource_report_module.fit_verdicts(resource_report_module._usage(report, kind, name), amount)


def _strict_with_an_undeclared_consumer(tmp_path, *, budget: int) -> None:
    """A strict 4096-byte RAM at 0.8 (a 3276.8 byte limit), Alpha budgeting *budget*, Beta declaring nothing."""
    _memory(tmp_path, "RAM", size=4096, threshold=0.8)
    _swc(tmp_path, "Alpha", memory={"RAM": budget})
    _swc(tmp_path, "Beta")


def _strict_core_with_an_undeclared_task(tmp_path, *, wcet: str) -> None:
    """A strict CORE0 at 0.7, Task_A declaring *wcet* every 10 ms, Task_B declaring no wcet."""
    _core(tmp_path, "CORE0", threshold=0.7)
    _task(tmp_path, "Task_A", period=0.01, priority=10, wcet=wcet)
    _task(tmp_path, "Task_B", period=0.01, priority=11)


def _task_09_memory(tmp_path) -> None:
    """The bench's task 09 case: a strict 32768-byte RAM at 0.75 (a 24576 byte limit) with 14336 bytes used."""
    _memory(tmp_path, "RAM", size=32768, threshold=0.75)
    _swc(tmp_path, "Alpha", memory={"RAM": 8192})
    _swc(tmp_path, "Beta", memory={"RAM": 6144})


class TestFitVerdicts:
    """Every pair the two verdicts can take, each held to its one sentence, in `fit_verdicts`' order."""

    @pytest.mark.parametrize(
        ("build", "amount", "pair", "sentence"),
        [
            (
                lambda root: _strict_with_an_undeclared_consumer(root, budget=512),
                1,
                (None, None),
                resource_report_module.ANSWER_UNKNOWN.format(count=1),
            ),
            (
                lambda root: _strict_with_an_undeclared_consumer(root, budget=512),
                3000,
                (False, None),
                resource_report_module.ANSWER_OVER_THRESHOLD_SIZE_UNKNOWN.format(excess="235.2 bytes", count=1),
            ),
            (
                lambda root: _strict_with_an_undeclared_consumer(root, budget=512),
                4000,
                (False, False),
                resource_report_module.ANSWER_OVER_SIZE.format(excess="416 bytes"),
            ),
            (
                lambda root: _strict_with_an_undeclared_consumer(root, budget=4000),
                1,
                (False, None),
                resource_report_module.ANSWER_OVER_THRESHOLD_SIZE_UNKNOWN.format(excess="724.2 bytes", count=1),
            ),
            (
                _task_09_memory,
                20000,
                (False, False),
                resource_report_module.ANSWER_OVER_SIZE.format(excess="1568 bytes"),
            ),
            (
                _task_09_memory,
                12288,
                (False, True),
                resource_report_module.ANSWER_WITHIN_SIZE_ONLY.format(excess="2048 bytes"),
            ),
            (_task_09_memory, 8192, (True, True), resource_report_module.ANSWER_FITS),
        ],
    )
    def test_each_pair_states_its_sentence(self, tmp_path, build, amount, pair, sentence):
        build(tmp_path)

        verdicts = _verdicts(tmp_path, "memory", "RAM", Fraction(amount))

        assert (verdicts["within_threshold"]["fits"], verdicts["within_size"]["fits"]) == pair
        assert verdicts["fits"] is verdicts["within_threshold"]["fits"]
        assert verdicts["answer"] == sentence

    @pytest.mark.parametrize(
        ("wcet", "amount", "pair", "sentence"),
        [
            ("1500us", Fraction(1, 100), (None, None), resource_report_module.ANSWER_UNKNOWN.format(count=1)),
            (
                "1500us",
                Fraction(6, 10),
                (False, None),
                resource_report_module.ANSWER_OVER_THRESHOLD_SIZE_UNKNOWN.format(excess="5.00%", count=1),
            ),
            ("1500us", Fraction(9, 10), (False, False), resource_report_module.ANSWER_OVER_SIZE.format(excess="5.00%")),
            (
                "7500us",
                Fraction(1, 100),
                (False, None),
                resource_report_module.ANSWER_OVER_THRESHOLD_SIZE_UNKNOWN.format(excess="6.00%", count=1),
            ),
        ],
    )
    def test_a_core_with_an_undeclared_task_states_the_unknown_pairs(self, tmp_path, wcet, amount, pair, sentence):
        """The two unknown answers on a core: a task with no ``wcet`` leaves the core's load unknown."""
        _strict_core_with_an_undeclared_task(tmp_path, wcet=wcet)

        verdicts = _verdicts(tmp_path, "core", "CORE0", amount)

        assert (verdicts["within_threshold"]["fits"], verdicts["within_size"]["fits"]) == pair
        assert verdicts["answer"] == sentence

    def test_the_sentences_are_the_examples_the_plan_names(self):
        """The templates as a reader meets them, so a rewording is a decision rather than a drift."""
        assert resource_report_module.ANSWER_WITHIN_SIZE_ONLY.format(excess="2048 bytes") == (
            "fits physically, breaks the threshold by 2048 bytes"
        )
        assert resource_report_module.ANSWER_UNKNOWN.format(count=2) == (
            "unknown: 2 consumer(s) declare no budget for it, so its use leaves their load out"
        )

    def test_task_09_carries_both_limits_and_both_headrooms(self, tmp_path):
        _task_09_memory(tmp_path)

        assert _verdicts(tmp_path, "memory", "RAM", Fraction(12288)) == {
            "fits": False,
            "within_threshold": {"fits": False, "limit": "24576", "headroom": "10240"},
            "within_size": {"fits": True, "limit": "32768", "headroom": "18432"},
            "answer": "fits physically, breaks the threshold by 2048 bytes",
        }

    def test_a_null_size_verdict_is_keyed_on_the_declarations_not_on_the_status(self, tmp_path):
        """Strict, over its threshold and with an undeclared consumer: the status says over budget, the size null."""
        _strict_with_an_undeclared_consumer(tmp_path, budget=4000)
        report = resource_report(str(tmp_path))
        usage = resource_report_module._usage(report, "memory", "RAM")

        verdicts = resource_report_module.fit_verdicts(usage, Fraction(1))

        assert budget_status(usage) == STATUS_BREACH
        assert (verdicts["fits"], verdicts["within_size"]["fits"]) == (False, None)

    def test_a_threshold_no_is_given_under_an_undeclared_consumer(self, tmp_path):
        """Strict, within its threshold today, an undeclared consumer: over the threshold within the size is a "no".

        The missing load only adds to the use, so the threshold's "no" stands as the size's does;
        only a "yes" that would rest on the partial figure stays unknown.
        """
        _strict_with_an_undeclared_consumer(tmp_path, budget=512)
        report = resource_report(str(tmp_path))
        usage = resource_report_module._usage(report, "memory", "RAM")

        over = resource_report_module.fits_answer(report, "memory", "RAM", Fraction(3000))
        under = resource_report_module.fits_answer(report, "memory", "RAM", Fraction(2764))

        assert budget_status(usage) == STATUS_UNKNOWN
        assert over["status"] == STATUS_UNKNOWN
        assert (over["fits"], over["within_threshold"]["fits"], over["within_size"]["fits"]) == (False, False, None)
        assert over["answer"] == (
            "breaks the threshold by 235.2 bytes; within the size is unknown: 1 consumer(s) declare no budget for it"
        )
        assert (under["fits"], under["within_size"]["fits"]) == (None, None)
        assert under["answer"] == resource_report_module.ANSWER_UNKNOWN.format(count=1)

    def test_one_unit_over_is_one_byte(self, tmp_path):
        _memory(tmp_path, "RAM", size=100, threshold=0.5)

        over_the_threshold = _verdicts(tmp_path, "memory", "RAM", Fraction(51))
        over_the_size = _verdicts(tmp_path, "memory", "RAM", Fraction(101))

        assert over_the_threshold["answer"] == "fits physically, breaks the threshold by 1 byte"
        assert over_the_size["answer"] == "does not fit: over the size by 1 byte"

    def test_an_inexact_excess_carries_the_approximation_marker(self, tmp_path):
        """1003 bytes at 0.333 is a 333.999 byte limit: 400 bytes break it by 66.001, which two decimals miss."""
        _memory(tmp_path, "RAM", size=1003, threshold=0.333)

        verdicts = _verdicts(tmp_path, "memory", "RAM", Fraction(400))

        assert verdicts["within_threshold"]["limit"] == "~333.99"
        assert verdicts["answer"] == "fits physically, breaks the threshold by ~66 bytes"

    def test_a_core_is_spelled_in_percent_against_its_100_percent(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.7)
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, wcet="1500us")

        breaks = _verdicts(tmp_path, "core", "CORE0", Fraction("0.6"))
        over = _verdicts(tmp_path, "core", "CORE0", Fraction("0.9"))

        assert resource_report_module.fits_answer(resource_report(str(tmp_path)), "core", "CORE0", Fraction("0.6"))[
            "asked"
        ] == ("60.00%")
        assert breaks["within_threshold"] == {"fits": False, "limit": "70.00%", "headroom": "55.00%"}
        assert breaks["within_size"] == {"fits": True, "limit": "100.00%", "headroom": "85.00%"}
        assert breaks["answer"] == "fits physically, breaks the threshold by 5.00%"
        assert over["answer"] == "does not fit: over the size by 5.00%"

    def test_an_amount_of_zero(self, tmp_path):
        """Zero fits a resource within its threshold, and is still a "no" for one already over it."""
        _task_09_memory(tmp_path)
        # Advisory, so the two RAM consumers that budget nothing for it count as zero.
        _memory(tmp_path, "ROM", size=1000, threshold=0.5, enforcement="advisory")
        _swc(tmp_path, "Gamma", memory={"RAM": 0, "ROM": 600})

        within = _verdicts(tmp_path, "memory", "RAM", Fraction(0))
        over = _verdicts(tmp_path, "memory", "ROM", Fraction(0))

        assert (within["fits"], within["within_size"]["fits"], within["answer"]) == (
            True,
            True,
            resource_report_module.ANSWER_FITS,
        )
        assert (over["fits"], over["within_size"]["fits"]) == (False, True)
        assert over["answer"] == "fits physically, breaks the threshold by 100 bytes"

    def test_every_row_carries_the_physical_headroom(self, tmp_path):
        _task_09_memory(tmp_path)
        _core(tmp_path, "CORE0", threshold=0.7)
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, wcet="1500us")
        report = resource_report(str(tmp_path))

        overview = resource_report_module.overview(report)

        assert [row["physical_headroom"] for row in overview["memories"]] == ["18432"]
        assert [row["physical_headroom"] for row in overview["cores"]] == ["85.00%"]
        assert (
            list(overview["memories"][0]).index("physical_headroom")
            == list(overview["memories"][0]).index("headroom") + 1
        )
        assert resource_report_module.resource_answer(report, "memory", "RAM")["resource"] == overview["memories"][0]
        assert resource_report_module.resource_answer(report, "core", "CORE0")["memories"] == overview["memories"]


class TestParseUtilization:
    @pytest.mark.parametrize(
        ("text", "value"), [("0.05", Fraction(1, 20)), (" 1/8 ", Fraction(1, 8)), ("5e-2", Fraction(1, 20))]
    )
    def test_a_fraction_of_a_core_is_read_exactly(self, text, value):
        assert resource_report_module.parse_utilization(text) == value

    def test_the_sign_is_left_to_the_caller(self):
        assert resource_report_module.parse_utilization("-0.1") == Fraction(-1, 10)

    @pytest.mark.parametrize(
        "text", ["5%", "", "1/0", "1e999999999", "1e9_999_999_999", "0.0_5", "0." + "0" * 100 + "5"]
    )
    def test_a_malformed_or_unbounded_text_is_a_value_error(self, text):
        with pytest.raises(ValueError):
            resource_report_module.parse_utilization(text)


# ---------------------------------------------------------------------------
# The CLI's forms: the text's `PHYSICAL HEADROOM` and `FIT` lines, and the JSON document
# ---------------------------------------------------------------------------


def _memory_fit(amount: int) -> resource_report_module.FitQuestion:
    return resource_report_module.FitQuestion(resource_report_module.KIND_MEMORY, Fraction(amount))


def _core_and_task_09_memory(tmp_path) -> None:
    _task_09_memory(tmp_path)
    _core(tmp_path, "CORE0", threshold=0.7)
    _task(tmp_path, "Task_10ms", period=0.01, priority=10, wcet="1500us")


class TestThePhysicalHeadroomAndFitLines:
    def test_each_block_gains_the_physical_headroom_after_its_aggregate_line(self, tmp_path):
        _core_and_task_09_memory(tmp_path)

        blocks = _blocks(resource_report(str(tmp_path)).render())

        assert blocks["CORE0"][2:4] == [
            "  UTILIZATION 15.00% of a 70.00% threshold (headroom 55.00%)",
            "  PHYSICAL HEADROOM 85.00% of the core",
        ]
        assert blocks["RAM"][2:4] == [
            "  CONSUMPTION 14336 bytes of a 24576 byte budget (headroom 10240 bytes)",
            "  PHYSICAL HEADROOM 18432 bytes of a 32768 byte size",
        ]
        assert not any(line.startswith("  FIT") for block in blocks.values() for line in block)

    def test_a_fit_question_adds_its_line_to_the_asked_kind_only(self, tmp_path):
        _core_and_task_09_memory(tmp_path)

        blocks = _blocks(resource_report(str(tmp_path)).render(fit=_memory_fit(12288)))

        assert blocks["RAM"][3:6] == [
            "  PHYSICAL HEADROOM 18432 bytes of a 32768 byte size",
            "  FIT 12288 bytes: fits physically, breaks the threshold by 2048 bytes",
            "  CONSUMERS 2 SWC(s), 0 without a declared budget",
        ]
        assert not any(line.startswith("  FIT") for line in blocks["CORE0"])

    def test_a_core_question_is_spelled_in_percent(self, tmp_path):
        _core_and_task_09_memory(tmp_path)
        fit = resource_report_module.FitQuestion(resource_report_module.KIND_CORE, Fraction(1, 2))

        blocks = _blocks(resource_report(str(tmp_path)).render(fit=fit))

        assert "  FIT 50.00%: fits within the threshold and the size" in blocks["CORE0"]
        assert not any(line.startswith("  FIT") for line in blocks["RAM"])

    def test_a_question_only_adds_its_fit_lines(self, tmp_path):
        """With a question the text is the plain report plus its ``FIT`` lines, every other line byte for byte.

        The plain report's own lines are held as literal strings above
        (``test_each_block_gains_the_physical_headroom_after_its_aggregate_line``); this test holds
        that a question changes none of them.
        """
        _core_and_task_09_memory(tmp_path)
        report = resource_report(str(tmp_path))

        plain = report.render().splitlines()
        asked = report.render(fit=_memory_fit(1)).splitlines()

        assert [line for line in asked if not line.startswith("  FIT")] == plain


class TestTheJsonDocument:
    def _document(self, tmp_path, fit=None) -> dict:
        return json.loads(resource_report_module.format_report_json(resource_report(str(tmp_path)), fit))

    def test_the_keys_and_their_order(self, tmp_path):
        _core_and_task_09_memory(tmp_path)

        document = self._document(tmp_path)

        assert list(document) == ["schemaVersion", "findings", "topology", "fit", "cores", "memories"]
        assert document["schemaVersion"] == resource_report_module.RESOURCE_REPORT_SCHEMA_VERSION == 1
        assert document["findings"] == {"errors": 0, "informational": 0}
        assert (document["topology"], document["fit"]) == ([], None)
        assert list(document["memories"][0]) == [
            "name",
            "kind",
            "enforcement",
            "size",
            "used",
            "limit",
            "headroom",
            "physical_headroom",
            "status",
            "consumers",
            "findings",
        ]
        assert list(document["cores"][0]["consumers"][0]) == ["name", "layer", "amount", "declared", "detail"]

    def test_each_resource_is_its_overview_row_and_more_a_memorys_budget_figures_aside(self, tmp_path):
        _core_and_task_09_memory(tmp_path)
        overview = resource_report_module.overview(resource_report(str(tmp_path)))

        document = self._document(tmp_path)

        for kind, aside in (("cores", set()), ("memories", {"limit", "headroom"})):
            assert [
                {key: value for key, value in entry.items() if key not in {"consumers", "findings"} | aside}
                for entry in document[kind]
            ] == [{key: value for key, value in entry.items() if key not in aside} for entry in overview[kind]]

    def test_a_memory_row_reads_in_whole_bytes_and_the_rest_of_the_document_stays_exact(self, tmp_path):
        _processor(tmp_path)
        _memory(tmp_path, "RAM", size=4096, threshold=0.8)
        _swc(tmp_path, "Alpha", memory={"RAM": 4000})
        report = resource_report(str(tmp_path))

        document = json.loads(resource_report_module.format_report_json(report, _memory_fit(1)))

        row = document["memories"][0]
        assert (row["limit"], row["headroom"]) == ("3276", "-724")
        assert row["fit"]["within_threshold"] == {"fits": False, "limit": "3276.8", "headroom": "-723.2"}
        assert "3276.8 bytes" in row["findings"][0]["message"]
        answer = resource_report_module.overview(report)["memories"][0]
        assert (answer["limit"], answer["headroom"]) == ("3276.8", "-723.2")

    def test_the_consumers_are_ordered_as_the_mcp_answer_orders_them(self, tmp_path):
        _memory(tmp_path, "RAM", size=8192, threshold=0.8)
        _swc(tmp_path, "Alpha", memory={"RAM": 512})
        _swc(tmp_path, "Delta", memory={"RAM": 0})
        _swc(tmp_path, "Beta", memory={"RAM": 2048})
        _swc(tmp_path, "Epsilon")
        _swc(tmp_path, "Gamma", memory={"RAM": 512})
        answer = resource_report_module.resource_answer(resource_report(str(tmp_path)), "memory", "RAM")

        consumers = self._document(tmp_path)["memories"][0]["consumers"]

        listed = [consumer["name"] for consumer in consumers]
        assert listed[: len(answer["consumers"])] == [consumer["name"] for consumer in answer["consumers"]]
        assert listed == ["Beta", "Alpha", "Gamma", "Epsilon", "Delta"]
        assert consumers[3] == {
            "name": "Epsilon",
            "layer": "swc",
            "amount": "not declared",
            "declared": False,
            "detail": "",
        }
        assert consumers[0]["amount"] == "2048 bytes"

    def test_a_core_lists_its_tasks_with_their_detail(self, tmp_path):
        _core_and_task_09_memory(tmp_path)

        (consumer,) = self._document(tmp_path)["cores"][0]["consumers"]

        assert consumer == {
            "name": "Task_10ms",
            "layer": "tasks",
            "amount": "15.00%",
            "declared": True,
            "detail": "wcet 1500us / period 0.01s",
        }

    def test_every_finding_is_unfolded_with_its_subjects(self, tmp_path):
        """The CLI is the whole report: one entry per finding, the `missing_budget` ones included."""
        _strict_with_an_undeclared_consumer(tmp_path, budget=512)
        _swc(tmp_path, "Gamma")
        report = resource_report(str(tmp_path))
        (usage,) = report.analysis.resources

        findings = self._document(tmp_path)["memories"][0]["findings"]

        assert [finding["code"] for finding in findings] == [finding.code for finding in usage.findings]
        assert [finding["code"] for finding in findings].count(FINDING_MISSING_BUDGET) == 2
        assert findings[0]["subjects"] == [subject.to_json() for subject in usage.findings[0].subjects]
        assert findings[0]["subjects"][0]["layer"] == "swc"
        assert list(findings[0]) == ["severity", "code", "message", "subjects"]

    def test_the_fit_stands_on_the_asked_kind_only(self, tmp_path):
        _core_and_task_09_memory(tmp_path)
        report = resource_report(str(tmp_path))
        verdicts = resource_report_module.fit_verdicts(
            resource_report_module._usage(report, "memory", "RAM"), Fraction(12288)
        )

        document = self._document(tmp_path, _memory_fit(12288))

        assert document["fit"] == {"kind": "memory", "asked": "12288"}
        assert document["memories"][0]["fit"] == verdicts
        assert document["memories"][0]["fit"]["answer"] == "fits physically, breaks the threshold by 2048 bytes"
        assert "fit" not in document["cores"][0]

    def test_a_question_of_a_kind_the_model_has_none_of_stands_on_no_row(self, tmp_path):
        """``--fit-bytes`` over a model with cores only: the document states the question, and no row a fit."""
        _core(tmp_path, "CORE0", threshold=0.7)
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, wcet="1500us")

        document = self._document(tmp_path, _memory_fit(12288))

        assert document["fit"] == {"kind": "memory", "asked": "12288"}
        assert document["memories"] == []
        assert "fit" not in document["cores"][0]

    def test_a_core_question_is_spelled_in_percent(self, tmp_path):
        _core_and_task_09_memory(tmp_path)
        fit = resource_report_module.FitQuestion(resource_report_module.KIND_CORE, Fraction(125, 1000))

        document = self._document(tmp_path, fit)

        assert document["fit"] == {"kind": "core", "asked": "12.50%"}
        assert document["cores"][0]["fit"]["within_size"] == {"fits": True, "limit": "100.00%", "headroom": "85.00%"}
        assert "fit" not in document["memories"][0]

    def test_the_topology_records_and_the_counts(self, tmp_path):
        _memory(tmp_path, "RAM", size=4096, threshold=0.8, enforcement="advisory")
        _swc(tmp_path, "Alpha", memory={"RAM": 4000})
        _task(tmp_path, "Task_lost", period=0.01, priority=10, partition="NOWHERE", wcet="100us")
        records = check_hardware_topology(str(tmp_path)).records
        report = resource_report(str(tmp_path), records)

        document = json.loads(resource_report_module.format_report_json(report))

        assert document["findings"] == {"errors": report.error_count, "informational": report.info_count}
        assert document["findings"]["informational"] >= 1
        assert document["topology"] == [{"rule": record.rule, "message": record.message} for record in records]
        assert document["topology"]

    def test_the_document_is_deterministic_and_newline_terminated(self, tmp_path):
        _core_and_task_09_memory(tmp_path)
        report = resource_report(str(tmp_path))

        first = resource_report_module.format_report_json(report, _memory_fit(1))

        assert first == resource_report_module.format_report_json(report, _memory_fit(1))
        assert first.endswith("}\n") and not first.endswith("\n\n")


class TestReportMarkdown:
    """The ``--report`` file's text: what ``render()`` carries, in Markdown, with no storage path."""

    def test_it_carries_each_resources_figures_consumers_and_findings(self) -> None:
        report = resource_report(_FIXTURE_MODEL)

        text = resource_report_module.report_markdown(report, "Demo")

        assert text.startswith("# Resource utilization report: Demo\n")
        assert f"{len(report.analysis.resources)} resource(s) declared" in text
        for usage in report.analysis.resources:
            assert f"## {usage.name} (" in text
            assert f"- **Status:** {budget_status(usage)}" in text
            for consumer in usage.consumers:
                assert f"`{consumer.name}`" in text
            for finding in usage.findings:
                assert finding.code in text
        assert _FIXTURE_MODEL not in text

    def test_a_pipe_and_a_line_break_keep_the_table(self) -> None:
        report = resource_report(_FIXTURE_MODEL)
        usage = next(usage for usage in report.analysis.resources if usage.consumers)
        hostile = dataclasses.replace(usage.consumers[0], detail="a|b\nc")
        usage.consumers[0] = hostile

        text = resource_report_module.report_markdown(report, "Demo")

        assert "a\\|b c" in text
        row = next(line for line in text.splitlines() if line.startswith(f"| `{hostile.name}` "))
        assert row.replace("\\|", "").count("|") == 5

    def test_the_topology_section_keeps_each_record_on_one_line(self) -> None:
        record = SimpleNamespace(rule="hardware.undeclared-partition", message="a|b\nc")

        text = resource_report_module.report_markdown(resource_report(_FIXTURE_MODEL, [record]), "Demo")

        assert "\n## Topology\n\n- hardware.undeclared-partition: a\\|b c\n\n" in text
        assert "## Topology" not in resource_report_module.report_markdown(resource_report(_FIXTURE_MODEL), "Demo")

    def test_a_hostile_project_name_stays_on_the_title_line(self) -> None:
        text = resource_report_module.report_markdown(resource_report(_FIXTURE_MODEL), "a\nb")

        assert text.splitlines()[0] == "# Resource utilization report: a b"

    def test_a_resource_with_no_consumer_says_so(self, tmp_path: pathlib.Path) -> None:
        model_root = tmp_path / "model"
        (model_root / "hardware").mkdir(parents=True)
        _core(model_root, "CORE0")

        text = resource_report_module.report_markdown(resource_report(str(model_root)), "Demo")

        assert "No consumer." in text

    def test_the_file_and_the_document_read_a_memory_the_same(self) -> None:
        report = resource_report(str(_DEMO_MODEL))

        text = resource_report_module.report_markdown(report, "Demo")
        row = json.loads(resource_report_module.format_report_json(report))["memories"][0]

        assert (row["limit"], row["headroom"]) == ("209715", "206131")
        assert "- **Consumption:** 3584 bytes of a 209715 byte budget (headroom 206131 bytes)" in text
        assert "- **Physical headroom:** 258560 bytes of a 262144 byte size" in text
        assert "209715.2" not in text


def _two_partitions_on_one_core(model_root: pathlib.Path) -> None:
    _processor(model_root)
    _core(model_root, "CORE0", partition=False)
    _core(model_root, "CORE1", partition=False)
    _partition(model_root, "P_SAFE", core="CORE0")
    _partition(model_root, "P_QM", core="CORE0")
    _partition(model_root, "P_EMPTY", core="CORE0")
    _partition(model_root, "P_OTHER", core="CORE1")
    # 1/3 of a third is a figure two decimals cannot state, so the sum has to be the exact one.
    _task(model_root, "Task_A", period=0.03, priority=30, partition="P_SAFE", wcet="1ms")
    _task(model_root, "Task_B", period=0.03, priority=30, partition="P_SAFE", wcet="1ms")
    _task(model_root, "Task_C", period=0.06, priority=20, partition="P_QM", wcet="3ms")
    _task(model_root, "Task_D", period=0.06, priority=20, partition="P_QM")
    _task(model_root, "Task_E", period=0.01, priority=40, partition="P_OTHER", wcet="1ms")


class TestThePartitionsOfACore:
    """The report file's Partition column and subtotals: a name and a sum, never a verdict."""

    @staticmethod
    def _section(text: str, heading: str) -> list[str]:
        lines = text.splitlines()
        start = next(number for number, line in enumerate(lines) if line.startswith(f"## {heading} "))
        following = (number for number, line in enumerate(lines) if number > start and line.startswith("## "))
        return lines[start : next(following, len(lines))]

    def test_each_task_carries_its_partition_and_a_component_none(self, tmp_path) -> None:
        _two_partitions_on_one_core(tmp_path)
        _memory(tmp_path, "RAM", size=4096, accessible=["CORE0"])
        _swc(tmp_path, "Alpha", memory={"RAM": 10})

        usages = {usage.name: usage for usage in analyze_resource_budgets(str(tmp_path)).resources}

        assert {c.name: c.partition for c in usages["CORE0"].consumers} == {
            "Task_A": "P_SAFE",
            "Task_B": "P_SAFE",
            "Task_C": "P_QM",
            "Task_D": "P_QM",
        }
        assert [c.partition for c in usages["RAM"].consumers] == [""]
        assert partition_shares(usages["RAM"]) == []

    def test_the_shares_are_exact_and_add_up_to_the_core_with_an_undeclared_task_counted_as_zero(
        self, tmp_path
    ) -> None:
        _two_partitions_on_one_core(tmp_path)

        usage = analyze_resource_budgets(str(tmp_path)).resources[0]

        assert partition_shares(usage) == [("P_SAFE", Fraction(1, 15)), ("P_QM", Fraction(1, 20))]
        assert sum(share for _, share in partition_shares(usage)) == usage.consumption

    def test_a_tie_is_ordered_by_name(self, tmp_path) -> None:
        _processor(tmp_path)
        _core(tmp_path, "CORE0", partition=False)
        _partition(tmp_path, "P_B", core="CORE0")
        _partition(tmp_path, "P_A", core="CORE0")
        _task(tmp_path, "Task_B", partition="P_B", wcet="1ms")
        _task(tmp_path, "Task_A", partition="P_A", wcet="1ms")

        usage = analyze_resource_budgets(str(tmp_path)).resources[0]

        assert [name for name, _ in partition_shares(usage)] == ["P_A", "P_B"]

    def test_a_core_section_has_the_column_and_a_subtotal_of_a_name_and_a_share_alone(self, tmp_path) -> None:
        _two_partitions_on_one_core(tmp_path)

        text = resource_report_module.report_markdown(resource_report(str(tmp_path)), "Demo")

        section = self._section(text, "CORE0")
        assert "| Task | Partition | Amount | Detail |" in section
        assert "| `Task_C` | `P_QM` | 5.00% | wcet 3ms / period 0.06s |" in section
        assert "| `Task_D` | `P_QM` | not declared | period 0.06s, no wcet declared |" in section
        subtotal = section.index("| Partition | Share of the core |")
        assert section[subtotal + 1 :][:3] == ["| --- | --- |", "| `P_SAFE` | ~6.66% |", "| `P_QM` | 5.00% |"]

    def test_a_partition_no_task_names_and_one_of_another_core_are_not_listed(self, tmp_path) -> None:
        _two_partitions_on_one_core(tmp_path)

        text = resource_report_module.report_markdown(resource_report(str(tmp_path)), "Demo")

        assert "P_EMPTY" not in text
        assert "P_OTHER" not in "\n".join(self._section(text, "CORE0"))
        assert "| `P_OTHER` | 10.00% |" in self._section(text, "CORE1")

    def test_a_task_outside_a_declared_partition_is_on_no_core_so_no_row_lacks_one(self, tmp_path) -> None:
        _processor(tmp_path)
        _core(tmp_path, "CORE0")
        _task(tmp_path, "Task_in", wcet="1ms")
        _task(tmp_path, "Task_lost", partition="NOWHERE", wcet="1ms")

        usage = analyze_resource_budgets(str(tmp_path)).resources[0]

        assert [(c.name, bool(c.partition)) for c in usage.consumers] == [("Task_in", True)]

    def test_no_task_on_a_core_means_no_column_and_no_subtotal(self, tmp_path) -> None:
        _processor(tmp_path)
        _core(tmp_path, "CORE0")
        _memory(tmp_path, "RAM", size=4096)
        _swc(tmp_path, "Alpha", memory={"RAM": 10})

        text = resource_report_module.report_markdown(resource_report(str(tmp_path)), "Demo")

        assert "No consumer." in self._section(text, "CORE0")
        assert "Partition" not in text
        assert "| Component | Amount | Detail |" in text

    def test_a_subtotal_changes_neither_the_text_nor_the_document(self, tmp_path) -> None:
        _two_partitions_on_one_core(tmp_path)
        report = resource_report(str(tmp_path))

        assert "P_SAFE" not in report.render()
        assert "P_SAFE" not in resource_report_module.format_report_json(report)


class TestWholeBytes:
    """A memory's budget and headroom read whole in the file and the view; no comparison reads that spelling."""

    @pytest.mark.parametrize(
        ("value", "text"),
        [
            (Fraction(1048576, 5), "209715"),
            (Fraction(10), "10"),
            (Fraction(0), "0"),
            (Fraction(-3616, 5), "-724"),
            (Fraction(-1, 5), "-1"),
            (Fraction(33399, 100), "333"),
        ],
    )
    def test_the_figure_rounds_down(self, value: Fraction, text: str) -> None:
        assert format_whole_bytes(value) == text

    @pytest.mark.parametrize(
        ("used", "status_is_breach"),
        [(3276, False), (3277, True)],
    )
    def test_at_the_boundary_the_verdict_is_the_exact_comparison_and_the_whole_figures_agree(
        self, tmp_path, used: int, status_is_breach: bool
    ) -> None:
        """4096 x 0.8 is 3276.8: 3276 is within it and 3277 over, as they are against the whole 3276."""
        _processor(tmp_path)
        _memory(tmp_path, "RAM", size=4096, threshold=0.8)
        _swc(tmp_path, "Alpha", memory={"RAM": used})
        report = resource_report(str(tmp_path))
        usage = report.analysis.resources[0]

        assert usage.limit == Fraction(16384, 5)
        assert (budget_status(usage) == STATUS_BREACH) is status_is_breach
        assert (usage.consumption > usage.limit) is status_is_breach
        whole_limit = int(format_whole_bytes(usage.limit))
        whole_headroom = int(format_whole_bytes(usage.headroom))
        assert whole_limit == 3276
        assert (used > whole_limit) is status_is_breach
        assert (whole_headroom < 0) is status_is_breach
        assert whole_limit - used == whole_headroom
        text = resource_report_module.report_markdown(report, "Demo")
        assert f"of a 3276 byte budget (headroom {whole_headroom} " in text
        assert ".8 byte budget" not in text

    @pytest.mark.parametrize("amount", [0, 1, 2, 3])
    def test_a_fit_of_whole_bytes_answers_as_it_would_against_the_floor(self, tmp_path, amount: int) -> None:
        """The display rule's premise at `fits`: a whole use and amount never tell a budget from its floor."""
        _processor(tmp_path)
        _memory(tmp_path, "RAM", size=4096, threshold=0.8)
        _swc(tmp_path, "Alpha", memory={"RAM": 3275})
        usage = analyze_resource_budgets(str(tmp_path)).resources[0]

        assert fits(usage, Fraction(amount)) is (3275 + amount <= 3276)
        assert fits_size(usage, Fraction(amount)) is True

    def test_a_fractional_component_budget_is_refused_at_load_which_is_the_premise(self, tmp_path) -> None:
        _processor(tmp_path)
        _memory(tmp_path, "RAM", size=4096, threshold=0.8)
        _swc(tmp_path, "Alpha", memory={"RAM": 10.5})

        with pytest.raises(ValueError, match="integer"):
            analyze_resource_budgets(str(tmp_path))

    def test_a_verdict_sentence_stays_exact_in_the_file(self, tmp_path) -> None:
        _processor(tmp_path)
        _memory(tmp_path, "RAM", size=4096, threshold=0.8)
        _swc(tmp_path, "Alpha", memory={"RAM": 4000})
        report = resource_report(str(tmp_path))

        text = resource_report_module.report_markdown(report, "Demo", _memory_fit(1))

        assert "exceeds its budget of 3276.8 bytes" in text
        assert "breaks the threshold by 724.2 bytes" in text

    def test_a_core_figure_is_not_touched(self) -> None:
        document = json.loads(resource_report_module.format_report_json(resource_report(str(_DEMO_MODEL))))

        assert (document["cores"][0]["limit"], document["cores"][0]["headroom"]) == ("70.00%", "58.00%")

    def test_the_text_keeps_the_exact_figures(self) -> None:
        assert (
            "3584 bytes of a 209715.2 byte budget (headroom 206131.2 bytes)"
            in resource_report(str(_DEMO_MODEL)).render()
        )


# ---------------------------------------------------------------------------
# The ARRIVALS section
# ---------------------------------------------------------------------------


def _one_data_received_event(model_root: pathlib.Path) -> None:
    """The bench's task 09 budgets, and ``Sensor.Sample`` in ``Task_10ms`` sending what ``Monitor.OnSpeed`` receives."""
    _core_and_task_09_memory(model_root)
    _write(
        element_path(model_root, "interfaces", "ISpeed"),
        {
            "name": "ISpeed",
            "kind": "sender_receiver",
            "data_elements": [{"name": "Speed", "implementation_type": "u8"}],
        },
    )
    _swc(
        model_root,
        "WheelSpeed",
        ports=[{"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}],
        runnables=[{"name": "Sample", "writes": [{"port": "pSpeed", "element": "Speed"}]}],
    )
    _swc(
        model_root,
        "WheelMonitor",
        ports=[{"name": "rSpeed", "direction": "require", "interface": "ISpeed"}],
        runnables=[{"name": "OnSpeed", "events": [{"type": "data", "port": "rSpeed", "element": "Speed"}]}],
    )
    _write(
        element_path(model_root, "compositions", "Vehicle"),
        {
            "name": "Vehicle",
            "title": "Vehicle",
            "root": True,
            "members": [{"name": "Sensor", "type": "WheelSpeed"}, {"name": "Monitor", "type": "WheelMonitor"}],
        },
    )
    _write(
        element_path(model_root, "connectors", "C1"),
        {
            "name": "C1",
            "from": {"swc": "WheelSpeed", "port": "pSpeed"},
            "to": {"swc": "WheelMonitor", "port": "rSpeed"},
        },
    )
    _task(
        model_root,
        "Task_10ms",
        period=0.01,
        priority=10,
        wcet="1500us",
        runnables=[{"swc": "WheelSpeed", "runnable": "Sample"}],
    )


_ARRIVAL_LINE = "Monitor.OnSpeed (rSpeed.Speed): data arrives at most every 10 ms (from Sensor.Sample in Task_10ms)"


class TestTheArrivalsSection:
    """The section on the three surfaces and the MCP overview; a model without the event keeps every byte."""

    def test_a_model_with_no_data_received_event_has_no_arrivals_and_no_key(self, tmp_path):
        _core_and_task_09_memory(tmp_path)
        report = resource_report(str(tmp_path))

        assert report.arrivals == ()
        assert "ARRIVALS" not in report.render()
        assert "Arrivals" not in resource_report_module.report_markdown(report, "Demo")
        assert "arrivals" not in resource_report_module.overview(report)
        assert "arrivals" not in json.loads(resource_report_module.format_report_json(report))

    def test_the_text_carries_the_section_last_with_the_closing_sentence_once(self, tmp_path):
        _one_data_received_event(tmp_path)

        text = resource_report(str(tmp_path)).render()

        assert text.splitlines()[-3:] == [
            "ARRIVALS 1 line(s)",
            f"  - {_ARRIVAL_LINE}",
            f"  {resource_report_module.ARRIVALS_NOTE}",
        ]
        assert text.count(resource_report_module.ARRIVALS_NOTE) == 1

    def test_the_markdown_carries_the_section_with_the_closing_sentence_once(self, tmp_path):
        _one_data_received_event(tmp_path)

        text = resource_report_module.report_markdown(resource_report(str(tmp_path)), "Demo")

        assert text.endswith(f"## Arrivals\n\n- {_ARRIVAL_LINE}\n\n{resource_report_module.ARRIVALS_NOTE}\n")
        assert text.count(resource_report_module.ARRIVALS_NOTE) == 1

    def test_the_json_document_adds_the_two_keys_and_keeps_its_version(self, tmp_path):
        """An added optional key does not raise the version (``differ.py``'s rule)."""
        _one_data_received_event(tmp_path)

        document = json.loads(resource_report_module.format_report_json(resource_report(str(tmp_path))))

        assert list(document) == [
            "schemaVersion",
            "findings",
            "topology",
            "fit",
            "cores",
            "memories",
            "arrivals",
            "arrivalsNote",
        ]
        assert document["schemaVersion"] == resource_report_module.RESOURCE_REPORT_SCHEMA_VERSION == 1
        assert document["arrivals"] == [
            {
                "occurrence": "Monitor",
                "runnable": "OnSpeed",
                "port": "rSpeed",
                "element": "Speed",
                "verdict": "bounded",
                "periodMs": "10",
                "writer": "Sensor.Sample",
                "task": "Task_10ms",
                "more": 0,
            }
        ]
        assert document["arrivalsNote"] == resource_report_module.ARRIVALS_NOTE

    def test_an_unknown_line_carries_its_case_and_no_bound(self, tmp_path):
        _one_data_received_event(tmp_path)
        (tmp_path / "connectors" / "C1.yaml").unlink()
        report = resource_report(str(tmp_path))

        document = json.loads(resource_report_module.format_report_json(report))

        assert document["arrivals"][0] == {
            "occurrence": "Monitor",
            "runnable": "OnSpeed",
            "port": "rSpeed",
            "element": "Speed",
            "verdict": "unknown",
            "case": "no connector reaches the port",
            "more": 0,
        }
        assert (
            "  - Monitor.OnSpeed (rSpeed.Speed): arrival rate unknown: the source is outside the model "
            "(no connector reaches the port)" in report.render().splitlines()
        )

    def test_a_period_is_printed_exactly_in_milliseconds(self, tmp_path):
        _one_data_received_event(tmp_path)
        _task(
            tmp_path,
            "Task_10ms",
            period=0.0025,
            priority=10,
            wcet="1500us",
            runnables=[{"swc": "WheelSpeed", "runnable": "Sample"}],
        )

        assert "data arrives at most every 2.5 ms" in resource_report(str(tmp_path)).render()

    def test_the_mcp_overview_carries_the_lines_and_the_closing_sentence_once(self, tmp_path):
        _one_data_received_event(tmp_path)

        answer = resource_report_module.overview(resource_report(str(tmp_path)))

        assert list(answer) == ["response", "cores", "memories", "findings", "topology", "arrivals"]
        assert answer["arrivals"] == {"first": [_ARRIVAL_LINE], "more": 0, "note": resource_report_module.ARRIVALS_NOTE}

    def test_the_mcp_overview_carries_the_first_lines_and_counts_the_rest(self, tmp_path):
        """The lines grow with the model: the overview keeps the first ``ARRIVALS_NAMED``, counts the rest."""
        _one_data_received_event(tmp_path)
        report = resource_report(str(tmp_path))
        named = resource_report_module.ARRIVALS_NAMED
        many = dataclasses.replace(report, arrivals=report.arrivals * (named + 3))

        arrivals = resource_report_module.overview(many)["arrivals"]

        assert arrivals == {
            "first": [_ARRIVAL_LINE] * named,
            "more": 3,
            "note": resource_report_module.ARRIVALS_NOTE,
            "rest": resource_report_module.ARRIVALS_REST,
        }
        assert many.render().count(_ARRIVAL_LINE) == named + 3

    def test_the_mcp_overview_at_exactly_the_cap_counts_nothing_and_points_nowhere(self, tmp_path):
        _one_data_received_event(tmp_path)
        report = resource_report(str(tmp_path))
        named = resource_report_module.ARRIVALS_NAMED
        exact = dataclasses.replace(report, arrivals=report.arrivals * named)

        arrivals = resource_report_module.overview(exact)["arrivals"]

        assert len(arrivals["first"]) == named
        assert arrivals["more"] == 0
        assert "rest" not in arrivals

    def test_the_mcp_overview_bounds_the_first_lines_in_bytes_too(self, tmp_path):
        """A line names its instance path, so twenty lines are no byte bound."""
        _one_data_received_event(tmp_path)
        report = resource_report(str(tmp_path))
        deep = "/".join(f"Level{index:02d}" + "x" * 120 for index in range(40))
        (arrival,) = report.arrivals
        long = dataclasses.replace(report, arrivals=(dataclasses.replace(arrival, occurrence=deep),) * 20)
        every_line = [resource_report_module.arrival_line(line) for line in long.arrivals]

        arrivals = resource_report_module.overview(long)["arrivals"]

        kept = len(arrivals["first"])
        assert 0 < kept < resource_report_module.ARRIVALS_NAMED
        assert arrivals["first"] == every_line[:kept]
        assert arrivals["more"] == 20 - kept
        assert arrivals["rest"] == resource_report_module.ARRIVALS_REST
        wire = len(json.dumps(json.dumps(arrivals["first"], ensure_ascii=False), ensure_ascii=False).encode("utf-8"))
        assert wire <= resource_report_module.ARRIVALS_FIRST_BYTES
        # The CLI's surfaces stay complete.
        assert long.render().count(every_line[0]) == 20
        assert len(json.loads(resource_report_module.format_report_json(long))["arrivals"]) == 20

    def test_a_first_line_alone_past_the_byte_cap_is_counted_and_named_by_none(self, tmp_path):
        _one_data_received_event(tmp_path)
        report = resource_report(str(tmp_path))
        (arrival,) = report.arrivals
        huge = "/".join(["N" * 120] * (resource_report_module.ARRIVALS_FIRST_BYTES // 120 + 1))
        alone = dataclasses.replace(report, arrivals=(dataclasses.replace(arrival, occurrence=huge), arrival))

        arrivals = resource_report_module.overview(alone)["arrivals"]

        # A prefix: the short line after the long one is counted, never named out of order.
        assert arrivals["first"] == []
        assert arrivals["more"] == 2
        assert arrivals["rest"] == resource_report_module.ARRIVALS_REST

    def test_the_findings_line_does_not_count_the_section(self, tmp_path):
        _one_data_received_event(tmp_path)
        after = resource_report(str(tmp_path))
        # The same model with the receiving runnable started by nothing: the budgets are the same.
        _swc(
            tmp_path,
            "WheelMonitor",
            ports=[{"name": "rSpeed", "direction": "require", "interface": "ISpeed"}],
            runnables=[{"name": "OnSpeed"}],
        )
        before = resource_report(str(tmp_path))

        assert after.arrivals and not before.arrivals
        assert after.error_count > 0
        assert (after.error_count, after.info_count) == (before.error_count, before.info_count)
        findings = [line for line in after.render().splitlines() if line.startswith("FINDINGS")]
        assert findings == [line for line in before.render().splitlines() if line.startswith("FINDINGS")]

    def test_the_command_prints_the_section_and_exits_0(self, tmp_path, monkeypatch, capsys):
        _one_data_received_event(tmp_path / "model")
        manifest = _write_manifest(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest)])

        main()  # exits 0: it must not raise

        out = capsys.readouterr().out
        assert f"  - {_ARRIVAL_LINE}" in out.splitlines()
        report = resource_report(str(tmp_path / "model"))
        assert f"FINDINGS {report.error_count} error(s), {report.info_count} informational" in out.splitlines()

    def test_a_second_writer_and_an_unplaced_receiver_render_on_every_surface(self, tmp_path):
        """A bound counting one more writer, and a not-derived line, on the text, Markdown, JSON and overview."""
        _one_data_received_event(tmp_path)
        _swc(
            tmp_path,
            "WheelSpeed",
            ports=[{"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}],
            runnables=[
                {"name": "Sample", "writes": [{"port": "pSpeed", "element": "Speed"}]},
                {"name": "Fast", "writes": [{"port": "pSpeed", "element": "Speed"}]},
            ],
        )
        _task(
            tmp_path,
            "Task_20ms",
            period=0.02,
            priority=5,
            wcet="1000us",
            runnables=[{"swc": "WheelSpeed", "runnable": "Fast"}],
        )
        _swc(
            tmp_path,
            "SpareMonitor",
            ports=[{"name": "rSpeed", "direction": "require", "interface": "ISpeed"}],
            runnables=[{"name": "OnSpeed", "events": [{"type": "data", "port": "rSpeed", "element": "Speed"}]}],
        )
        report = resource_report(str(tmp_path))
        unplaced = "SpareMonitor.OnSpeed (rSpeed.Speed): not derived: the component is placed in no composition"
        bound = (
            "Monitor.OnSpeed (rSpeed.Speed): data arrives at most every 10 ms "
            "(from Sensor.Sample in Task_10ms, and 1 more)"
        )

        assert report.render().splitlines()[-4:] == [
            "ARRIVALS 2 line(s)",
            f"  - {unplaced}",
            f"  - {bound}",
            f"  {resource_report_module.ARRIVALS_NOTE}",
        ]
        assert resource_report_module.report_markdown(report, "Demo").endswith(
            f"## Arrivals\n\n- {unplaced}\n- {bound}\n\n{resource_report_module.ARRIVALS_NOTE}\n"
        )
        assert json.loads(resource_report_module.format_report_json(report))["arrivals"] == [
            {
                "occurrence": "SpareMonitor",
                "runnable": "OnSpeed",
                "port": "rSpeed",
                "element": "Speed",
                "verdict": "not-derived",
                "case": "the component is placed in no composition",
                "more": 0,
            },
            {
                "occurrence": "Monitor",
                "runnable": "OnSpeed",
                "port": "rSpeed",
                "element": "Speed",
                "verdict": "bounded",
                "periodMs": "10",
                "writer": "Sensor.Sample",
                "task": "Task_10ms",
                "more": 1,
            },
        ]
        assert resource_report_module.overview(report)["arrivals"]["first"] == [unplaced, bound]

    def test_the_header_counts_lines_and_a_type_placed_twice_gives_two(self, tmp_path):
        """One event on a type placed twice: two lines, and the header says lines, not events."""
        _one_data_received_event(tmp_path)
        _write(
            element_path(tmp_path, "compositions", "Vehicle"),
            {
                "name": "Vehicle",
                "title": "Vehicle",
                "root": True,
                "members": [
                    {"name": "Sensor", "type": "WheelSpeed"},
                    {"name": "Monitor", "type": "WheelMonitor"},
                    {"name": "Spare", "type": "WheelMonitor"},
                ],
            },
        )

        lines = resource_report(str(tmp_path)).render().splitlines()

        assert lines[-4:-1] == [
            "ARRIVALS 2 line(s)",
            f"  - {_ARRIVAL_LINE}",
            "  - Spare.OnSpeed (rSpeed.Speed): data arrives at most every 10 ms (from Sensor.Sample in Task_10ms)",
        ]

    def test_the_cap_line_carries_the_fixed_case_and_its_count(self):
        """The case is one string of the closed list; the occurrences left out travel in ``more``."""
        from swcraft_tools.data_arrival import NOT_DERIVED, PAST_THE_CAP, Arrival

        arrival = Arrival("WheelMonitor", "OnSpeed", "rSpeed", "Speed", NOT_DERIVED, case=PAST_THE_CAP, more=3)

        assert resource_report_module.arrival_line(arrival) == (
            f"WheelMonitor.OnSpeed (rSpeed.Speed): not derived: {PAST_THE_CAP} (3 more)"
        )
        assert resource_report_module.arrival_json(arrival) == {
            "occurrence": "WheelMonitor",
            "runnable": "OnSpeed",
            "port": "rSpeed",
            "element": "Speed",
            "verdict": "not-derived",
            "case": PAST_THE_CAP,
            "more": 3,
        }

    def test_a_malformed_connector_file_in_a_model_with_the_event_exits_1(self, tmp_path, monkeypatch, capsys):
        """The arrivals read ``connectors/`` once an event exists: an unparseable file there fails the read."""
        _one_data_received_event(tmp_path / "model")
        (tmp_path / "model" / "connectors" / "Broken.yaml").write_text(
            "name: [unclosed\n", encoding="utf-8", newline="\n"
        )
        manifest = _write_manifest(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "resource-report", "--project", str(manifest)])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Error:" in captured.err
        # The CLI names a file of the user's own project by its full native path (`render(full_path=...)`),
        # so the separator is the platform's: a backslash on Windows.
        assert str(pathlib.Path("connectors", "Broken.yaml")) in captured.err
