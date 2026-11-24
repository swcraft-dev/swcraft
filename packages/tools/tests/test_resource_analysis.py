"""Tests for the budget analysis over ``hardware/``.

The arithmetic over the declared :class:`~swcraft_tools.schema.Core` and
:class:`~swcraft_tools.schema.Memory` elements: missing consumer budgets, the two
threshold verdicts and rate-monotonic conformance, plus the shape of the result (one
finding level and the per-consumer breakdown). The structural checks are contract rules
``validator.check_hardware_topology`` reports. ``swcraft resource-report``'s golden
fixture (``test_resource_report.py``) holds the rendered figures byte for byte.

The three codes are asserted to be the module's **whole** vocabulary
(:class:`TestTheRetiredStructuralChecks`), not merely the ones that happen to
appear: ``validator._RESOURCE_FINDING_RULES`` is a one-to-one table over this
module's codes, so a retired code this module still emitted would raise
``KeyError`` inside ``check_resource_budgets`` — a crash in ``swcraft
validate``, not a wrong answer.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re
from fractions import Fraction

import pytest
from conftest import REPO_ROOT, _codes, _core, _memory, _partition, _processor, _swc, _task, _write
from swcraft_tools import resource_analysis, resource_report
from swcraft_tools.diagnostics import Subject, SubjectStep
from swcraft_tools.execution_budget import UNITS
from swcraft_tools.reader import read_hardware, read_swc_model, read_tasks
from swcraft_tools.resource_analysis import (
    FINDING_MISSING_BUDGET,
    FINDING_RATE_MONOTONIC_VIOLATION,
    FINDING_THRESHOLD_BREACH,
    MEMORY_BUDGET_EXAMPLE,
    NOT_DECLARED,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    STATUS_BREACH,
    STATUS_OK,
    STATUS_UNKNOWN,
    WCET_BUDGET_EXAMPLE,
    analyze_resource_budgets,
    analyze_resources,
    budget_status,
    format_amount,
    format_bytes,
    format_percent,
    has_resources,
    is_core,
)
from swcraft_tools.schema import Core, Memory, SWComponent, Task
from swcraft_tools.writer import needs_export

_SCHEMA_MODULE = "swcraft_tools.schema"


def _schema_imports(tree: ast.Module, *, package: str) -> set[str]:
    """Every name *tree* imports from ``swcraft_tools.schema``, in any spelling.

    Covers ``import swcraft_tools.schema``, ``from swcraft_tools import schema``,
    ``from swcraft_tools.schema import ...`` and the relative ``from ..schema import Core``
    ``writer/`` uses. *package* is the importing module's own package, against which a
    relative ``level`` is resolved.

    What it does not reach: an import built at runtime (``importlib``,
    ``__import__``), which no module in this repository does, and a name reached
    through another module that imported it.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names if alias.name == _SCHEMA_MODULE}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - node.level + 1])
                module = f"{base}.{node.module}" if node.module else base
            else:
                module = node.module or ""
            if module == _SCHEMA_MODULE:
                found |= {alias.name for alias in node.names}
            elif f"{module}.schema" == _SCHEMA_MODULE:
                found |= {alias.name for alias in node.names if alias.name == "schema"}
    return found


# ---------------------------------------------------------------------------
# Nothing to budget: hardware that declares no core and no memory
# ---------------------------------------------------------------------------


class TestNoBudgetedHardware:
    """``hardware/`` is mandatory, but a *budget* is still something a model can lack.

    A hand-built ``hardware/`` holding only the processor, or one the reader finds empty,
    is an empty analysis rather than a crash.
    """

    def test_hardware_with_only_a_processor_produces_a_clean_result(self, tmp_path):
        # A task on a core nobody declared and a budget against a memory nobody
        # declared: `check_hardware_topology` reports both, not this module.
        _processor(tmp_path)
        _task(tmp_path, "Task_10ms", partition="PARTITION3")
        _swc(tmp_path, "Alpha", memory={"NoSuchMemory": 128})

        analysis = analyze_resource_budgets(str(tmp_path))

        assert analysis.resources == []
        assert analysis.all_findings() == []
        assert analysis.error_findings() == []

    def test_missing_hardware_dir_produces_a_clean_result(self, tmp_path):
        _task(tmp_path, "Task_10ms", partition="PARTITION3")

        analysis = analyze_resource_budgets(str(tmp_path))

        assert analysis.resources == []
        assert analysis.all_findings() == []

    def test_empty_hardware_dir_produces_a_clean_result(self, tmp_path):
        (tmp_path / "hardware").mkdir()
        _task(tmp_path, "Task_10ms", partition="PARTITION3")

        analysis = analyze_resource_budgets(str(tmp_path))

        assert analysis.resources == []
        assert analysis.all_findings() == []

    def test_no_budget_means_the_task_and_swc_layers_are_never_read(self, tmp_path, monkeypatch):
        def _boom(_model_root):
            raise AssertionError("model layers must not be read when no budget is declared")

        monkeypatch.setattr("swcraft_tools.resource_analysis.read_tasks", _boom)
        monkeypatch.setattr("swcraft_tools.resource_analysis.read_swc_model", _boom)
        _processor(tmp_path)

        assert analyze_resource_budgets(str(tmp_path)).resources == []


#: Every committed model that declares a ``hardware/`` layer: the example and the test tree's fixtures.
_COMMITTED_MODELS = sorted(
    (
        hardware.parent
        for hardware in (
            REPO_ROOT / "examples" / "basic" / "model" / "hardware",
            *(REPO_ROOT / "packages" / "tools" / "tests" / "fixtures").glob("*/model/hardware"),
        )
        if hardware.is_dir()
    ),
    key=lambda path: path.as_posix(),
)


def _budgeted_model(root: pathlib.Path) -> None:
    """A model holding both budgeted kinds, a consumer of each and a finding: two cores, a memory, three tasks."""
    _processor(root)
    _core(root, "CORE0")
    _core(root, "CORE1", partition=False)
    _partition(root, "PARTITION1", core="CORE1")
    _memory(root, "RAM0", size=64, accessible=["CORE0"])
    _task(root, "Task_10ms", partition="PARTITION0", wcet="1200us")
    _task(root, "Task_5ms", period=0.005, priority=5, partition="PARTITION0")
    _task(root, "Task_1ms", period=0.001, priority=20, partition="PARTITION1", wcet="900us")
    _swc(root, "Alpha", memory={"RAM0": 60})
    _swc(root, "Beta")


class TestThePureCore:
    """``has_resources`` and ``analyze_resources`` decide over loaded layers; the reading function composes them."""

    def test_has_resources_is_false_without_a_core_or_a_memory(self, tmp_path):
        _processor(tmp_path)
        _partition(tmp_path, "PARTITION0", core="CORE0")

        assert has_resources(read_hardware(str(tmp_path))) is False
        assert has_resources([]) is False

    @pytest.mark.parametrize(
        "declare", [lambda root: _core(root, "CORE0"), lambda root: _memory(root, "RAM0")], ids=["core", "memory"]
    )
    def test_has_resources_is_true_for_a_core_or_a_memory(self, tmp_path, declare):
        _processor(tmp_path)
        declare(tmp_path)

        assert has_resources(read_hardware(str(tmp_path))) is True

    def test_with_no_resources_the_core_needs_no_task_or_component(self, tmp_path):
        _processor(tmp_path)

        assert analyze_resources(read_hardware(str(tmp_path)), [], []) == analyze_resource_budgets(str(tmp_path))

    def test_analyze_resources_equals_the_reading_function_on_a_budgeted_model(self, tmp_path):
        _budgeted_model(tmp_path)
        root = str(tmp_path)

        analysis = analyze_resources(read_hardware(root), read_tasks(root), read_swc_model(root))

        assert analysis == analyze_resource_budgets(root)
        assert [usage.name for usage in analysis.resources] == ["CORE0", "CORE1", "RAM0"]
        assert analysis.all_findings() != []

    @pytest.mark.parametrize("model", _COMMITTED_MODELS, ids=lambda path: path.parent.name)
    def test_analyze_resources_equals_the_reading_function_on_every_committed_model(self, model):
        root = str(model)

        assert analyze_resources(read_hardware(root), read_tasks(root), read_swc_model(root)) == (
            analyze_resource_budgets(root)
        )


# ---------------------------------------------------------------------------
# The structural checks this module does not own
# ---------------------------------------------------------------------------


class TestTheRetiredStructuralChecks:
    """The four retired finding codes are absent from the module's vocabulary and its findings.

    That the rules fire is ``tests/test_hardware_topology.py``'s claim.
    """

    #: Every retired code, and the rule that reports its case.
    RETIRED = {
        "duplicate_resource_name": "hardware.duplicate-name",
        "duplicate_core_id": "hardware.duplicate-name (a core is identified by its name)",
        "undeclared_memory_pool": "hardware.undeclared-memory",
        "undeclared_core": "hardware.undeclared-core",
    }

    def test_the_module_defines_none_of_the_retired_codes(self):
        defined = {
            value
            for name, value in vars(resource_analysis).items()
            if name.startswith("FINDING_") and isinstance(value, str)
        }

        assert defined == {"missing_budget", "threshold_breach", "rate_monotonic_violation"}
        assert defined.isdisjoint(self.RETIRED)

    def test_a_duplicate_name_is_not_the_analysis_business(self, tmp_path):
        """Two files claiming one name. The reader keeps both; the analysis budgets both."""
        _write(
            tmp_path / "hardware" / "RAM.yaml",
            {"name": "RAM", "kind": "memory", "type": "RAM", "size": 1024, "accessible": ["CORE0"], "threshold": 0.8},
        )
        _write(
            tmp_path / "hardware" / "RAM_copy.yaml",
            {"name": "RAM", "kind": "memory", "type": "RAM", "size": 2048, "accessible": ["CORE0"], "threshold": 0.5},
        )

        analysis = analyze_resource_budgets(str(tmp_path))

        assert self.RETIRED.keys().isdisjoint(_codes(analysis.all_findings()))

    def test_a_budget_against_an_undeclared_memory_is_not_the_analysis_business(self, tmp_path):
        _memory(tmp_path, "RAM")
        _swc(tmp_path, "Alpha", memory={"RAM": 128, "ROM": 512})

        analysis = analyze_resource_budgets(str(tmp_path))

        assert _codes(analysis.all_findings()) == []

    def test_a_task_on_an_undeclared_core_is_not_the_analysis_business(self, tmp_path):
        _core(tmp_path, "CORE0")
        _task(tmp_path, "Task_10ms", partition="PARTITION0", wcet="100us")
        _task(tmp_path, "Task_5ms", partition="PARTITION1", wcet="100us")

        analysis = analyze_resource_budgets(str(tmp_path))

        # The task on the undeclared core simply consumes nothing: it is not a
        # consumer of CORE0, and there is no second core to charge it to.
        assert [c.name for c in analysis.resources[0].consumers] == ["Task_10ms"]

    def test_the_all_advisory_downgrade_went_with_the_coverage_check(self, tmp_path):
        """An all-``advisory`` model gets no downgraded coverage finding: the analysis says nothing.

        An unresolved reference is the topology rules' error whatever the budget
        enforcement says, because it is about the model, not a budget.
        """
        _core(tmp_path, "CORE0", enforcement="advisory")
        _task(tmp_path, "Task_10ms", partition="PARTITION0", wcet="100us")
        _task(tmp_path, "Task_5ms", partition="PARTITION1", wcet="100us")

        assert analyze_resource_budgets(str(tmp_path)).all_findings() == []


# ---------------------------------------------------------------------------
# A declared budget with nothing to spend it against
# ---------------------------------------------------------------------------


class TestInertBudgets:
    def test_declared_wcet_is_inert_when_no_core_is_declared(self, tmp_path):
        """A ``wcet`` budget with no core declared produces no CPU usage and no finding."""
        _memory(tmp_path, "RAM")
        _task(tmp_path, "Task_5ms", period=0.005, partition="PARTITION7", wcet="4000us")
        _swc(tmp_path, "Alpha", memory={"RAM": 16})

        analysis = analyze_resource_budgets(str(tmp_path))

        assert analysis.all_findings() == []
        # Only the memory is analyzed; the task contributes to nothing.
        assert [usage.name for usage in analysis.resources] == ["RAM"]
        assert [c.name for c in analysis.resources[0].consumers] == ["Alpha"]

    def test_every_core_declared_produces_no_finding(self, tmp_path):
        _core(tmp_path, "CORE0")
        _core(tmp_path, "CORE1")
        _task(tmp_path, "Task_10ms", partition="PARTITION0", wcet="100us")
        _task(tmp_path, "Task_5ms", partition="PARTITION1", wcet="100us")

        assert analyze_resource_budgets(str(tmp_path)).all_findings() == []

    def test_a_memory_its_task_cannot_reach_is_still_charged_here(self, tmp_path):
        """``accessible`` is a topology fact, and the arithmetic is not about it.

        ``rule:hardware.unreachable-memory`` (``validator.check_hardware_topology``) refuses
        the model; the declared budget is still a real byte count against a real memory, so
        the analysis charges it rather than disagreeing with ``check-model`` quietly.

        What this does **not** reach: whether the model is refused
        (``test_hardware_topology.py``, over this same shape).
        """
        _core(tmp_path, "CORE0")
        _core(tmp_path, "CORE1")
        _memory(tmp_path, "RAM1", size=1024, accessible=["CORE1"])
        _task(tmp_path, "Task_10ms", partition="PARTITION0", wcet="100us")
        _swc(tmp_path, "Alpha", memory={"RAM1": 16})

        analysis = analyze_resource_budgets(str(tmp_path))

        assert analysis.all_findings() == []
        ram = next(usage for usage in analysis.resources if usage.name == "RAM1")
        assert [c.name for c in ram.consumers] == ["Alpha"]
        assert ram.consumption == Fraction(16)


# ---------------------------------------------------------------------------
# Check 4: missing consumer budgets (per-resource, severity follows enforcement)
# ---------------------------------------------------------------------------


class TestUnitBearingBudgets:
    """Contract ``10.0.0``: a budget carries its unit, and every unit is read into one exact duration."""

    @pytest.mark.parametrize("wcet", ["1500us", "1500000ns"])
    def test_one_duration_in_two_spellings_is_one_utilization(self, tmp_path, wcet):
        _core(tmp_path, "CORE0")
        _task(tmp_path, "Task_10ms", period=0.01, partition="PARTITION0", wcet=wcet)

        usage = analyze_resource_budgets(str(tmp_path)).resources[0]

        assert usage.consumption == Fraction(3, 20)
        assert [(c.name, c.amount, c.detail) for c in usage.consumers] == [
            ("Task_10ms", Fraction(3, 20), f"wcet {wcet} / period 0.01s")
        ]

    def test_a_sub_microsecond_budget_is_exact(self, tmp_path):
        _core(tmp_path, "CORE0")
        _task(tmp_path, "Task_1ms", period=0.001, partition="PARTITION0", wcet="850ns")

        assert analyze_resource_budgets(str(tmp_path)).resources[0].consumption == Fraction(17, 20000)

    def test_the_microsecond_detail_reads_as_before(self, tmp_path):
        """The line ``wcet 1200us / period 0.01s`` is unchanged for a budget written in ``us``."""
        _core(tmp_path, "CORE0")
        _task(tmp_path, "Task_10ms", period=0.01, partition="PARTITION0", wcet="1200us")

        (consumer,) = analyze_resource_budgets(str(tmp_path)).resources[0].consumers
        assert (consumer.amount, consumer.detail) == (Fraction(3, 25), "wcet 1200us / period 0.01s")


class TestMissingCpuBudget:
    def test_strict_core_reports_a_task_without_wcet(self, tmp_path):
        _core(tmp_path, "CORE0")
        _task(tmp_path, "Task_10ms", partition="PARTITION0")

        analysis = analyze_resource_budgets(str(tmp_path))

        usage = analysis.resources[0]
        assert _codes(usage.findings) == [FINDING_MISSING_BUDGET]
        finding = usage.findings[0]
        assert finding.severity == SEVERITY_ERROR
        assert finding.subjects == (Subject("tasks", "Task_10ms", (SubjectStep("field", "wcet"),)),)
        assert analysis.error_findings() == [finding]

    def test_the_sentence_names_the_task_the_core_the_field_every_unit_and_an_example(self, tmp_path):
        """The refusal an agent reads says what to write, not only that a budget is missing.

        Every unit is matched as a whole token, so ``s`` is never found inside
        ``ms``; the units are read from ``execution_budget.UNITS``, never copied.
        """
        _core(tmp_path, "CORE0")
        _task(tmp_path, "Task_10ms", partition="PARTITION0")

        (finding,) = analyze_resource_budgets(str(tmp_path)).resources[0].findings

        tokens = set(re.findall(r"[A-Za-z0-9_]+", finding.message))
        assert {"Task_10ms", "CORE0"} <= tokens
        assert set(UNITS) <= tokens
        spans = set(re.findall(r"`([^`]+)`", finding.message))
        assert {"wcet", f"wcet: {WCET_BUDGET_EXAMPLE}"} <= spans

    def test_the_wcet_example_is_a_budget_the_loader_accepts(self):
        """The oracle is the model's own loader: ``Task`` refuses ``0us``, which ``parse_budget`` alone accepts."""
        task = Task(name="T", period=0.01, priority=1, partition="PARTITION0", wcet=WCET_BUDGET_EXAMPLE)

        assert task.wcet == WCET_BUDGET_EXAMPLE

    def test_advisory_core_reports_the_same_gap_as_info_only(self, tmp_path):
        _core(tmp_path, "CORE0", enforcement="advisory")
        _task(tmp_path, "Task_10ms", partition="PARTITION0")

        analysis = analyze_resource_budgets(str(tmp_path))

        usage = analysis.resources[0]
        assert [f.severity for f in usage.findings] == [SEVERITY_INFO]
        assert analysis.error_findings() == []

    def test_missing_budget_counts_as_zero_in_the_breakdown(self, tmp_path):
        _core(tmp_path, "CORE0", enforcement="advisory")
        _task(tmp_path, "Task_10ms", period=0.01, partition="PARTITION0")

        usage = analyze_resource_budgets(str(tmp_path)).resources[0]

        assert [(c.name, c.amount, c.declared) for c in usage.consumers] == [("Task_10ms", 0.0, False)]
        assert usage.consumption == 0.0

    def test_declared_wcet_produces_no_finding(self, tmp_path):
        _core(tmp_path, "CORE0")
        _task(tmp_path, "Task_10ms", period=0.01, partition="PARTITION0", wcet="1500us")

        analysis = analyze_resource_budgets(str(tmp_path))

        assert analysis.all_findings() == []
        usage = analysis.resources[0]
        assert usage.consumers[0].declared is True
        assert usage.consumers[0].amount == pytest.approx(0.15)

    def test_tasks_on_another_core_are_not_consumers(self, tmp_path):
        _core(tmp_path, "CORE0")
        _core(tmp_path, "CORE1")
        _task(tmp_path, "Task_a", partition="PARTITION0", wcet="100us")
        _task(tmp_path, "Task_b", partition="PARTITION1")

        analysis = analyze_resource_budgets(str(tmp_path))

        core0, core1 = analysis.resources
        assert [c.name for c in core0.consumers] == ["Task_a"]
        assert [c.name for c in core1.consumers] == ["Task_b"]
        assert core0.findings == []
        assert _codes(core1.findings) == [FINDING_MISSING_BUDGET]

    def test_core_with_no_tasks_has_no_consumers_and_no_findings(self, tmp_path):
        _core(tmp_path, "CORE0")

        usage = analyze_resource_budgets(str(tmp_path)).resources[0]

        assert usage.consumers == []
        assert usage.findings == []
        assert usage.consumption == 0.0


class TestMissingMemoryBudget:
    def test_strict_pool_reports_an_swc_that_declares_no_budget(self, tmp_path):
        _memory(tmp_path, "RAM")
        _swc(tmp_path, "Alpha", memory={"RAM": 128})
        _swc(tmp_path, "Beta")

        analysis = analyze_resource_budgets(str(tmp_path))

        usage = analysis.resources[0]
        assert _codes(usage.findings) == [FINDING_MISSING_BUDGET]
        assert usage.findings[0].severity == SEVERITY_ERROR
        assert "RAM" in usage.findings[0].message
        assert "Beta" in usage.findings[0].message

    def test_the_sentence_names_the_swc_the_memory_the_field_and_an_example(self, tmp_path):
        """The refusal says which map to add the memory to and offers a value for it."""
        _memory(tmp_path, "RAM")
        _swc(tmp_path, "Beta")

        (finding,) = analyze_resource_budgets(str(tmp_path)).resources[0].findings

        tokens = set(re.findall(r"[A-Za-z0-9_]+", finding.message))
        assert {"Beta", "RAM"} <= tokens
        spans = set(re.findall(r"`([^`]+)`", finding.message))
        assert {"memory", f"RAM: {MEMORY_BUDGET_EXAMPLE}"} <= spans
        assert finding.code == FINDING_MISSING_BUDGET
        assert finding.severity == SEVERITY_ERROR
        assert finding.subjects == (
            Subject("swc", "Beta", (SubjectStep("field", "memory"), SubjectStep("key", "RAM"))),
        )

    def test_the_memory_example_is_a_budget_the_loader_accepts(self):
        """``SWComponent.memory`` holds whole bytes at least ``0``; the example is one of them."""
        swc = SWComponent(name="Beta", memory={"RAM": MEMORY_BUDGET_EXAMPLE})

        assert swc.memory == {"RAM": MEMORY_BUDGET_EXAMPLE}
        assert isinstance(MEMORY_BUDGET_EXAMPLE, int)

    def test_advisory_pool_reports_the_same_gap_as_info_only(self, tmp_path):
        _memory(tmp_path, "RAM", enforcement="advisory")
        _swc(tmp_path, "Beta")

        analysis = analyze_resource_budgets(str(tmp_path))

        assert [f.severity for f in analysis.resources[0].findings] == [SEVERITY_INFO]
        assert analysis.error_findings() == []

    def test_every_swc_declared_produces_no_finding_and_sums_the_breakdown(self, tmp_path):
        _memory(tmp_path, "RAM", size=1024)
        _swc(tmp_path, "Alpha", memory={"RAM": 128})
        _swc(tmp_path, "Beta", memory={"RAM": 256})

        analysis = analyze_resource_budgets(str(tmp_path))

        usage = analysis.resources[0]
        assert usage.findings == []
        assert [(c.name, c.amount) for c in usage.consumers] == [("Alpha", 128.0), ("Beta", 256.0)]
        assert usage.consumption == 384.0

    def test_pool_with_no_swcs_at_all_has_no_consumers(self, tmp_path):
        _memory(tmp_path, "RAM")

        usage = analyze_resource_budgets(str(tmp_path)).resources[0]

        assert usage.consumers == []
        assert usage.findings == []


# ---------------------------------------------------------------------------
# External stubs
# ---------------------------------------------------------------------------


class TestExternalStubsAreNotConsumers:
    """A stub is not charged, not accused and not named.

    An ``external: true`` component is a *reference* the ARXML import created for a
    prototype whose type the export does not carry; budgets and task mappings ignore
    stubs. These rows hold the memory half; the CPU half needs no skip
    (:class:`TestTheCpuArmNeverSeesAnSwc`).
    """

    def test_a_stub_is_not_reported_for_a_missing_memory_budget(self, tmp_path):
        """A stub is not accused of a gap it cannot close.

        ``swcraft import model`` writes stubs and no ``memory``, so a strict memory would
        make the imported project fail its own gate.
        """
        _memory(tmp_path, "RAM")
        _swc(tmp_path, "Alpha", memory={"RAM": 128})
        _swc(tmp_path, "Ghost", external=True, external_ref="/Foreign/ComponentTypes/Ghost")

        usage = analyze_resource_budgets(str(tmp_path)).resources[0]

        assert usage.findings == []
        assert [c.name for c in usage.consumers] == ["Alpha"]

    def test_a_stub_is_skipped_even_when_it_declares_a_budget(self, tmp_path):
        """A stub's declared ``memory`` is dropped: this project does not build that component.

        ``rule:swc.external-with-ports`` checks ``ports`` and ``runnables`` only, so a
        stub *can* declare a budget; it is not counted, not a consumer row, not a finding.
        """
        _memory(tmp_path, "RAM", size=1024)
        _swc(tmp_path, "Alpha", memory={"RAM": 128})
        _swc(tmp_path, "Ghost", external=True, memory={"RAM": 512})

        usage = analyze_resource_budgets(str(tmp_path)).resources[0]

        assert usage.findings == []
        assert [(c.name, c.amount) for c in usage.consumers] == [("Alpha", 128.0)]
        assert usage.consumption == Fraction(128)

    def test_a_threshold_breach_names_no_stub_among_its_subjects(self, tmp_path):
        """The breach's subjects are the entries that add to the total, so never a stub."""
        _memory(tmp_path, "RAM", size=100, threshold=0.5)
        _swc(tmp_path, "Alpha", memory={"RAM": 90})
        _swc(tmp_path, "Ghost", external=True, memory={"RAM": 8})

        usage = analyze_resource_budgets(str(tmp_path)).resources[0]

        assert _codes(usage.findings) == [FINDING_THRESHOLD_BREACH]
        assert [subject.element for subject in usage.findings[0].subjects] == ["RAM", "Alpha"]


class TestTheCpuArmNeverSeesAnSwc:
    def test_the_core_analysis_takes_no_swc_argument_at_all(self, tmp_path):
        """The CPU half needs no stub skip: ``_analyze_cpu_core`` is handed no component.

        It takes the core, the task list and the hardware by name; the ``swc`` layer reaches
        only ``_analyze_memory``. Checked against the signature.

        What this does **not** reach: whether a *task* may name a stub's runnable
        (``TaskRunnableRef.swc`` is resolved by ``validator``'s task-mapping pass).
        """
        parameters = list(inspect.signature(resource_analysis._analyze_cpu_core).parameters)

        assert parameters == ["resource", "tasks", "by_name"]

    def test_a_stub_changes_no_core_usage(self, tmp_path):
        _core(tmp_path, "CORE0")
        _memory(tmp_path, "RAM")
        _task(tmp_path, "Task_10ms", partition="PARTITION0", wcet="100us")
        _swc(tmp_path, "Alpha", memory={"RAM": 16})
        _swc(tmp_path, "Ghost", external=True)

        core_usage = next(u for u in analyze_resource_budgets(str(tmp_path)).resources if u.name == "CORE0")

        assert [c.name for c in core_usage.consumers] == ["Task_10ms"]
        assert core_usage.findings == []


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


class TestAnalysisShape:
    def test_one_usage_per_declared_budget_in_reader_order(self, tmp_path):
        _core(tmp_path, "CORE0")
        _memory(tmp_path, "RAM")
        _memory(tmp_path, "ROM", size=4096, memory_type="ROM")

        analysis = analyze_resource_budgets(str(tmp_path))

        assert [u.name for u in analysis.resources] == ["CORE0", "RAM", "ROM"]
        assert [is_core(u.resource) for u in analysis.resources] == [True, False, False]
        assert [u.resource.enforcement for u in analysis.resources] == ["strict", "strict", "strict"]

    def test_the_processor_produces_no_usage_of_its_own(self, tmp_path):
        """The one hardware kind that yields no usage: ``Resource`` does not admit a ``Processor``."""
        _processor(tmp_path)
        _core(tmp_path, "CORE0")

        analysis = analyze_resource_budgets(str(tmp_path))

        assert [u.name for u in analysis.resources] == ["CORE0"]

    def test_all_findings_concatenates_the_resources_in_reader_order(self, tmp_path):
        """The one drain: every resource's findings, resources in reader order.

        What this does **not** reach: whether a consumer *calls* the drain.
        ``validator.check_resource_budgets``, the report's two counters and
        ``error_findings`` each do, and each is asserted where it lives.
        """
        _core(tmp_path, "CORE0")
        _memory(tmp_path, "RAM")
        _task(tmp_path, "Task_a", partition="PARTITION0")  # per-element: missing wcet
        _swc(tmp_path, "Alpha")  # per-element: missing memory budget

        analysis = analyze_resource_budgets(str(tmp_path))

        assert _codes(analysis.all_findings()) == [FINDING_MISSING_BUDGET, FINDING_MISSING_BUDGET]
        assert analysis.all_findings() == [
            *analysis.resources[0].findings,
            *analysis.resources[1].findings,
        ]
        assert len(analysis.error_findings()) == 2

    def test_the_usage_holds_the_declared_element_itself(self, tmp_path):
        """``ResourceUsage.resource`` is the declared element, by **identity**: an equal copy would be an adapter.

        What this does not reach: the arithmetic, which the rest of this file asserts.
        """
        _processor(tmp_path)
        _core(tmp_path, "CORE0")
        _memory(tmp_path, "RAM")

        declared = {element.name: element for element in read_hardware(str(tmp_path))}
        analysis = analyze_resource_budgets(str(tmp_path))

        assert isinstance(analysis.resources[0].resource, Core)
        assert isinstance(analysis.resources[1].resource, Memory)
        # Same *values*, freshly read: the reader is called once per call, so
        # identity across two calls is not what is being claimed.
        assert analysis.resources[0].resource == declared["CORE0"]
        assert analysis.resources[1].resource == declared["RAM"]

    def test_the_usage_reads_the_elements_own_fields(self, tmp_path):
        """``name``, ``size``, ``threshold`` and ``enforcement`` are read off the usage's element."""
        _core(tmp_path, "CORE0", threshold=0.6, enforcement="advisory", hw_id=3)
        _memory(tmp_path, "RAM", size=2048, threshold=0.9, memory_type="ROM")

        core, ram = (usage.resource for usage in analyze_resource_budgets(str(tmp_path)).resources)

        assert (core.name, core.threshold, core.enforcement) == ("CORE0", 0.6, "advisory")
        assert (ram.name, ram.size, ram.threshold, ram.enforcement) == ("RAM", 2048, 0.9, "strict")
        assert not hasattr(core, "core")
        # The usage holds the whole element, so fields no figure reads are reachable too.
        assert core.hw_id == 3
        assert (ram.type, ram.accessible) == ("ROM", ["CORE0"])

    def test_the_discriminator_is_the_published_seam(self, tmp_path):
        """``is_core`` is public and the union's members are not imported by the renderers.

        ``is_core`` stays published rather than an ``isinstance(resource, Core)`` at each call
        site, which would put ``swcraft_tools.schema`` into ``resource_report.py``'s imports.
        ``writer/needs_export.py`` imports no ``schema`` class at all, which keeps it outside
        the ``schema-import`` scan of ``consumer-inventory.json``, so it must not gain one either.

        What this does not reach: the coverage artifact that counts those import blocks
        (``test_model_contract_coverage.py`` holds that figure). The scan reads every spelling
        of the import (:func:`_schema_imports`).
        """
        _core(tmp_path, "CORE0")
        _memory(tmp_path, "RAM")

        core, ram = (usage.resource for usage in analyze_resource_budgets(str(tmp_path)).resources)

        assert is_core(core) is True
        assert is_core(ram) is False
        assert resource_analysis.Resource == Core | Memory

        for module in (resource_report, needs_export):
            source = pathlib.Path(inspect.getsourcefile(module)).read_text(encoding="utf-8")
            assert _schema_imports(ast.parse(source), package=module.__package__) == set()

    def test_the_import_scan_reads_all_four_spellings(self):
        """The scan above asserts an empty set, so its own reach needs an oracle.

        A scan keyed on the absolute module name would miss `from ..schema import Core`,
        the spelling `writer/` uses for its siblings.
        """
        spellings = (
            ("import swcraft_tools.schema", "swcraft_tools", {"swcraft_tools.schema"}),
            ("from swcraft_tools import schema", "swcraft_tools", {"schema"}),
            ("from swcraft_tools.schema import Core", "swcraft_tools", {"Core"}),
            ("from ..schema import Core", "swcraft_tools.writer", {"Core"}),
            ("from .schema import Memory", "swcraft_tools", {"Memory"}),
        )
        for source, package, expected in spellings:
            assert _schema_imports(ast.parse(source), package=package) == expected, source
        # And a sibling that is not `schema` is not a hit.
        assert _schema_imports(ast.parse("from ..reader import read_hardware"), package="swcraft_tools.writer") == set()

    def test_limit_and_headroom_are_populated_for_both_kinds(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.7)
        _memory(tmp_path, "RAM", size=1000, threshold=0.8)
        _task(tmp_path, "Task_10ms", period=0.01, partition="PARTITION0", wcet="1500us")
        _swc(tmp_path, "Alpha", memory={"RAM": 300})

        core, ram = analyze_resource_budgets(str(tmp_path)).resources

        assert core.limit == pytest.approx(0.7)
        assert core.consumption == pytest.approx(0.15)
        assert core.headroom == pytest.approx(0.55)
        assert ram.limit == pytest.approx(800.0)
        assert ram.consumption == pytest.approx(300.0)
        assert ram.headroom == pytest.approx(500.0)


class TestReaderErrorsPropagate:
    def test_malformed_hardware_file_raises_value_error(self, tmp_path):
        (tmp_path / "hardware").mkdir()
        (tmp_path / "hardware" / "Bad.yaml").write_text(
            "name: Bad\nkind: memory\ntype: RAM\nsize: -1\naccessible: [CORE0]\nthreshold: 0.5\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="Bad.yaml"):
            analyze_resource_budgets(str(tmp_path))


# ---------------------------------------------------------------------------
# The public rendering helpers — the single copy every surface uses
# ---------------------------------------------------------------------------


class TestFormatBytes:
    """`format_bytes` is shared by the findings and the report, so a number is spelled one way.

    The table below is the contract.
    """

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, "0"),
            (512, "512"),
            (512.0, "512"),
            (Fraction(1024), "1024"),
            # 256 KiB x 0.8 -- what examples/basic/ renders on every real build.
            (Fraction(262144) * Fraction(4, 5), "209715.2"),
            (209715.2, "209715.2"),
            # 2 MiB x 0.8 = 1677721.6: six significant digits would render
            # `1.67772e+06` and round 838860.8 UP to `838861`.
            (Fraction(2 * 1024 * 1024) * Fraction(4, 5), "1677721.6"),
            (Fraction(1024 * 1024) * Fraction(4, 5), "838860.8"),
            (-1, "-1"),
            (-1740.8, "-1740.8"),
            (0.5, "0.5"),
            (Fraction(1, 100), "0.01"),
        ],
    )
    def test_value_table(self, value, expected):
        assert format_bytes(value) == expected

    def test_a_value_two_decimals_cannot_express_is_marked_approximate(self):
        # Truncated toward zero, never rounded up, and flagged -- a rendered
        # budget that reads higher than the exact limit produces the
        # self-contradictory "consumption N exceeds its budget of N".
        assert format_bytes(Fraction(1, 3)) == "~0.33"
        assert format_bytes(Fraction(8388608, 10)) == "838860.8"
        assert format_bytes(Fraction(2999, 1000)) == "~2.99"

    def test_a_sub_hundredth_value_never_renders_as_a_bare_zero(self):
        """`f"{0.004:.2f}".rstrip("0").rstrip(".")` would produce a bare "0"."""
        assert format_bytes(0.004) == "~0"

    def test_a_sub_hundredth_negative_value_keeps_its_magnitude(self):
        """Truncation is downward, not toward zero, so a marginal negative headroom is not `~-0`."""
        assert format_bytes(Fraction(-1, 125)) == "~-0.01"  # -0.008
        assert format_bytes(-0.004) == "~-0.01"
        assert format_percent(Fraction(-1, 100_000)) == "~-0.01%"

    def test_a_multi_megabyte_pool_never_renders_in_exponential_notation(self):
        assert "e+" not in format_bytes(Fraction(16 * 1024 * 1024))


class TestFormatPercent:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0.0, "0.00%"),
            (0.55, "55.00%"),
            (Fraction(73, 100), "73.00%"),
            (0.7, "70.00%"),
            (2.0, "200.00%"),
            (-0.3, "-30.00%"),
        ],
    )
    def test_value_table(self, value, expected):
        assert format_percent(value) == expected

    def test_a_marginal_breach_is_not_rendered_as_equal_to_its_threshold(self):
        """Rounding both sides would print "70.00% exceeds its threshold of 70.00%"."""
        over = Fraction(70_000_001, 100_000_000)

        assert format_percent(over) == "~70.00%"
        assert format_percent(Fraction(7, 10)) == "70.00%"
        assert format_percent(over) != format_percent(Fraction(7, 10))


class TestFormatAmountAndStatus:
    def test_amount_follows_the_resource_kind_and_marks_an_undeclared_consumer(self, tmp_path):
        _core(tmp_path, "CORE0")
        _memory(tmp_path, "RAM", size=1024)
        _task(tmp_path, "Task_10ms", period=0.01, partition="PARTITION0", wcet="1500us")
        _swc(tmp_path, "Alpha", memory={"RAM": 128})
        _swc(tmp_path, "Beta")

        core, ram = analyze_resource_budgets(str(tmp_path)).resources

        assert format_amount(core.resource, core.consumers[0]) == "15.00%"
        assert format_amount(ram.resource, ram.consumers[0]) == "128 bytes"
        assert format_amount(ram.resource, ram.consumers[1]) == NOT_DECLARED

    def test_status_reflects_the_threshold_verdict_only(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.9)
        # A rate-monotonic violation, but the core is well under its threshold.
        _task(tmp_path, "Task_10ms", period=0.01, priority=5, partition="PARTITION0", wcet="100us")
        _task(tmp_path, "Task_100ms", period=0.1, priority=20, partition="PARTITION0", wcet="1000us")

        assert budget_status(analyze_resource_budgets(str(tmp_path)).resources[0]) == STATUS_OK

    def test_status_is_the_breach_word_when_the_threshold_is_exceeded(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.5)
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, partition="PARTITION0", wcet="8000us")

        assert budget_status(analyze_resource_budgets(str(tmp_path)).resources[0]) == STATUS_BREACH

    def test_a_strict_resource_nobody_budgeted_reads_unknown_not_within_budget(self, tmp_path):
        # Consumption is 0 only because nothing is known -- a "within budget"
        # headline would read as a pass for an entirely unmeasured resource.
        _memory(tmp_path, "RAM", size=1024)
        _swc(tmp_path, "Alpha")
        _swc(tmp_path, "Beta")

        assert budget_status(analyze_resource_budgets(str(tmp_path)).resources[0]) == STATUS_UNKNOWN

    def test_a_strict_core_nobody_budgeted_reads_unknown(self, tmp_path):
        _core(tmp_path, "CORE0")
        _task(tmp_path, "Task_10ms", period=0.01, partition="PARTITION0")

        assert budget_status(analyze_resource_budgets(str(tmp_path)).resources[0]) == STATUS_UNKNOWN

    def test_a_single_unbudgeted_consumer_is_enough_to_make_the_verdict_unknown(self, tmp_path):
        # Beta declares nothing, so 128 bytes is not the pool's consumption --
        # it is a lower bound.
        _memory(tmp_path, "RAM", size=1024)
        _swc(tmp_path, "Alpha", memory={"RAM": 128})
        _swc(tmp_path, "Beta")

        assert budget_status(analyze_resource_budgets(str(tmp_path)).resources[0]) == STATUS_UNKNOWN

    def test_the_verdict_is_real_once_every_consumer_declared(self, tmp_path):
        _memory(tmp_path, "RAM", size=1024)
        _swc(tmp_path, "Alpha", memory={"RAM": 128})
        _swc(tmp_path, "Beta", memory={"RAM": 64})

        assert budget_status(analyze_resource_budgets(str(tmp_path)).resources[0]) == STATUS_OK

    def test_a_resource_with_no_consumers_at_all_is_within_budget(self, tmp_path):
        # Genuinely unused, not unknown: there is nothing left to declare.
        _memory(tmp_path, "RAM", size=1024)

        assert budget_status(analyze_resource_budgets(str(tmp_path)).resources[0]) == STATUS_OK

    def test_an_advisory_resource_nobody_budgeted_stays_within_budget(self, tmp_path):
        # Under `advisory` a missing budget counts as zero by design, so the
        # headline is the intended answer rather than missing data.
        _memory(tmp_path, "RAM", size=1024, enforcement="advisory")
        _swc(tmp_path, "Alpha")

        assert budget_status(analyze_resource_budgets(str(tmp_path)).resources[0]) == STATUS_OK

    def test_a_breach_still_wins_over_the_unknown_verdict(self, tmp_path):
        # Not reachable with zero declared consumers on a single resource, but
        # the precedence is asserted so a future reordering cannot hide a breach.
        _core(tmp_path, "CORE0", threshold=0.5)
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, partition="PARTITION0", wcet="8000us")
        _task(tmp_path, "Task_20ms", period=0.02, priority=20, partition="PARTITION0")

        assert budget_status(analyze_resource_budgets(str(tmp_path)).resources[0]) == STATUS_BREACH


# ---------------------------------------------------------------------------
# Where a finding is: `Finding.subjects` and `Finding.target`
# ---------------------------------------------------------------------------


def _subject(layer, element, *steps):
    """A :class:`Subject` from a terse ``(kind, value)`` spelling."""
    return Subject(layer, element, tuple(SubjectStep(kind, value) for kind, value in steps))


def _missing_budget(root):
    _core(root, "CORE0")
    _memory(root, "RAM", size=1024)
    _task(root, "T", partition="PARTITION0")
    _swc(root, "Alpha")


def _memory_breach(root):
    _memory(root, "RAM", size=16, threshold=0.5)
    _swc(root, "Alpha", memory={"RAM": 4096})


def _cpu_breach(root):
    _core(root, "CORE0", threshold=0.1)
    _task(root, "T", period=0.01, partition="PARTITION0", wcet="9000us")


def _rate_monotonic(root):
    _core(root, "CORE0", threshold=0.9)
    _task(root, "Fast", period=0.005, priority=1, partition="PARTITION0", wcet="10us")
    _task(root, "Slow", period=0.05, priority=9, partition="PARTITION0", wcet="10us")


#: One row per shape of finding this module produces: the model that produces it,
#: and the **exact** ``(code, subjects, target)`` of every finding it yields.
#: Between them the rows reach every ``Subject(...)`` construction in
#: ``resource_analysis.py``.
#:
#: The structural findings are ``validator.check_hardware_topology``'s, their
#: subjects asserted in ``tests/test_hardware_topology.py``. Both element-side
#: subjects here name the ``hardware`` layer, the one a client addresses to raise
#: a threshold or enlarge a memory.
SUBJECT_CASES = (
    (
        "missing budgets",
        _missing_budget,
        (
            ("missing_budget", (_subject("tasks", "T", ("field", "wcet")),), None),
            ("missing_budget", (_subject("swc", "Alpha", ("field", "memory"), ("key", "RAM")),), None),
        ),
    ),
    (
        "memory threshold breach",
        _memory_breach,
        (
            (
                "threshold_breach",
                # The memory's `size` and every budget drawn on it: both directions
                # of repair are legal, so both are named.
                (
                    _subject("hardware", "RAM", ("field", "size")),
                    _subject("swc", "Alpha", ("field", "memory"), ("key", "RAM")),
                ),
                None,
            ),
        ),
    ),
    (
        "cpu threshold breach",
        _cpu_breach,
        (
            (
                "threshold_breach",
                # A core has no size to enlarge, so the hardware side of the
                # repair is its `threshold`.
                (
                    _subject("hardware", "CORE0", ("field", "threshold")),
                    _subject("tasks", "T", ("field", "wcet")),
                ),
                None,
            ),
        ),
    ),
    (
        "rate-monotonic violation",
        _rate_monotonic,
        (
            (
                "rate_monotonic_violation",
                (
                    _subject("tasks", "Fast", ("field", "priority")),
                    _subject("tasks", "Slow", ("field", "priority")),
                ),
                None,
            ),
        ),
    ),
)


class TestFindingSubjects:
    """Where each finding *is*, in model terms.

    ``Finding.message`` is prose and prose is never contract, so a consumer acting on a
    finding (the editing protocol rejecting a change set) reads ``subjects``; a wrong
    subject would send a client's repair to the wrong place with every test green.

    ``target``, the grouping key a reference finding populates, is ``None`` on every
    finding this module builds; asserting it catches a later finding setting it by accident.
    """

    @pytest.mark.parametrize(("label", "build", "expected"), SUBJECT_CASES, ids=[case[0] for case in SUBJECT_CASES])
    def test_the_finding_names_the_model_places_it_is_about(self, tmp_path, label, build, expected):
        build(tmp_path)
        findings = analyze_resource_budgets(str(tmp_path)).all_findings()
        observed = [(finding.code, finding.subjects, finding.target) for finding in findings]
        assert observed == [tuple(row) for row in expected], label

    def test_every_case_states_a_subject(self):
        # A row expecting `()` would make the case above pass over nothing.
        for label, _build, expected in SUBJECT_CASES:
            for code, subjects, _target in expected:
                assert subjects, f"{label}: {code} states no subject"

    def test_a_subject_is_never_a_storage_path(self, tmp_path):
        # The reason `diagnostics.py` exists as its own leaf module: a subject is
        # a MODEL place, so nothing in one may be a file name or a directory.
        _memory_breach(tmp_path)
        for finding in analyze_resource_budgets(str(tmp_path)).all_findings():
            for subject in finding.subjects:
                rendered = repr(subject)
                assert ".yaml" not in rendered and "/" not in rendered and str(tmp_path) not in rendered


class TestTheCoreIsReachedThroughThePartition:
    """A task's core load is its PARTITION's core's (contract ``9.0.0``), reached in two hops.

    Every other case in this file writes one partition per core (``conftest._core``),
    so a regression charging only one partition per core, or charging a task through
    a partition whose ``core`` resolves to no core, would pass them all. These cases
    give the second hop models of their own.
    """

    def test_two_partitions_on_one_core_are_both_charged_to_it(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.9)
        _partition(tmp_path, "P_Second", core="CORE0")
        _task(tmp_path, "T_First", period=0.01, partition="PARTITION0", wcet="1000us")
        _task(tmp_path, "T_Second", period=0.01, partition="P_Second", wcet="2000us")

        (usage,) = analyze_resource_budgets(str(tmp_path)).resources

        assert usage.name == "CORE0"
        assert sorted(consumer.name for consumer in usage.consumers) == ["T_First", "T_Second"]
        assert usage.consumption == Fraction(3, 10)

    def test_a_rate_monotonic_violation_across_two_partitions_of_one_core_is_found(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.9)
        _partition(tmp_path, "P_Second", core="CORE0")
        _task(tmp_path, "Fast", period=0.005, priority=1, partition="PARTITION0", wcet="10us")
        _task(tmp_path, "Slow", period=0.05, priority=9, partition="P_Second", wcet="10us")

        (usage,) = analyze_resource_budgets(str(tmp_path)).resources

        assert _codes(usage.findings) == [FINDING_RATE_MONOTONIC_VIOLATION]

    @pytest.mark.parametrize("target", ["CORE9", "RAM0"], ids=["undeclared core", "a memory"])
    def test_a_partition_on_no_core_charges_its_task_nowhere(self, tmp_path, target):
        """A partition whose ``core`` is undeclared, or names a memory, gives its task no core.

        The task is in no usage, no total and no finding here: the topology pass
        reports the partition (``hardware.partition-undeclared-core`` /
        ``hardware.partition-core-not-a-core``).
        """
        _core(tmp_path, "CORE0", threshold=0.9)
        _memory(tmp_path, "RAM0")
        _partition(tmp_path, "P_Lost", core=target)
        _task(tmp_path, "T_Lost", period=0.01, partition="P_Lost", wcet="9000us")

        analysis = analyze_resource_budgets(str(tmp_path))

        for usage in analysis.resources:
            assert "T_Lost" not in [consumer.name for consumer in usage.consumers]
            assert usage.findings == []
        core0 = next(usage for usage in analysis.resources if usage.name == "CORE0")
        assert core0.consumption == 0

    def test_a_partition_name_declared_twice_resolves_to_the_first_file(self, tmp_path):
        """First file wins over every hardware element, as the topology pass resolves it."""
        _core(tmp_path, "CORE0", threshold=0.9, partition=False)
        _core(tmp_path, "CORE1", threshold=0.9, partition=False)
        _write(tmp_path / "hardware" / "P_Dup.yaml", {"name": "P_Dup", "kind": "partition", "core": "CORE0"})
        _write(tmp_path / "hardware" / "P_Dup~1.yaml", {"name": "P_Dup", "kind": "partition", "core": "CORE1"})
        _task(tmp_path, "T", period=0.01, partition="P_Dup", wcet="1000us")
        first = next(element for element in read_hardware(str(tmp_path)) if element.name == "P_Dup")
        other = "CORE1" if first.core == "CORE0" else "CORE0"

        usages = {usage.name: usage for usage in analyze_resource_budgets(str(tmp_path)).resources}

        assert [consumer.name for consumer in usages[first.core].consumers] == ["T"]
        assert usages[other].consumers == []


# ---------------------------------------------------------------------------
# A question about one resource: `fits` and `reachable_memories`
# ---------------------------------------------------------------------------


def _usage_of(model_root: pathlib.Path, name: str) -> resource_analysis.ResourceUsage:
    return next(usage for usage in analyze_resource_budgets(str(model_root)).resources if usage.name == name)


class TestFits:
    """``consumption + amount <= limit`` in exact fractions; ``None`` only for a "yes" over an unknown status."""

    def test_the_threshold_decides_never_the_raw_free_space(self, tmp_path):
        # 32768 x 0.75 = 24576 of budget, 14336 used: 10240 of headroom, 18432 raw free.
        _memory(tmp_path, "RAM", size=32768, threshold=0.75)
        _swc(tmp_path, "Alpha", memory={"RAM": 14336})
        usage = _usage_of(tmp_path, "RAM")

        assert resource_analysis.fits(usage, Fraction(12288)) is False
        assert resource_analysis.fits(usage, Fraction(8192)) is True

    def test_the_boundary_is_inclusive_and_exact(self, tmp_path):
        """``size x threshold`` is 3276.8 here: 1740.8 more is exactly the limit and fits; a byte more does not."""
        _memory(tmp_path, "RAM", size=4096, threshold=0.8)
        _swc(tmp_path, "Alpha", memory={"RAM": 1536})
        usage = _usage_of(tmp_path, "RAM")

        assert resource_analysis.fits(usage, Fraction(17408, 10)) is True
        assert resource_analysis.fits(usage, Fraction(1741)) is False

    def test_a_core_fits_a_utilization_fraction(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.7)
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, wcet="5500us")
        usage = _usage_of(tmp_path, "CORE0")

        assert resource_analysis.fits(usage, Fraction("0.15")) is True
        assert resource_analysis.fits(usage, Fraction("0.16")) is False

    def test_an_unknown_status_answers_none_for_a_yes(self, tmp_path):
        _memory(tmp_path, "RAM", size=32768, threshold=0.75, enforcement="strict")
        _swc(tmp_path, "Alpha", memory={"RAM": 1024})
        _swc(tmp_path, "Beta")
        usage = _usage_of(tmp_path, "RAM")

        assert budget_status(usage) == STATUS_UNKNOWN
        assert resource_analysis.fits(usage, Fraction(1)) is None
        assert resource_analysis.fits(usage, Fraction(23552)) is None

    def test_an_unknown_status_still_gives_a_no(self, tmp_path):
        """The missing load only adds to the use, so a sum over the limit is a "no"."""
        _memory(tmp_path, "RAM", size=32768, threshold=0.75, enforcement="strict")
        _swc(tmp_path, "Alpha", memory={"RAM": 1024})
        _swc(tmp_path, "Beta")
        usage = _usage_of(tmp_path, "RAM")

        assert budget_status(usage) == STATUS_UNKNOWN
        assert resource_analysis.fits(usage, Fraction(23553)) is False
        assert resource_analysis.fits_size(usage, Fraction(23553)) is None
        assert resource_analysis.fits(usage, Fraction(31745)) is False
        assert resource_analysis.fits_size(usage, Fraction(31745)) is False

    def test_a_core_with_an_undeclared_task_still_gives_a_no(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.7)
        _task(tmp_path, "Task_A", period=0.01, priority=10, wcet="1500us")
        _task(tmp_path, "Task_B", period=0.01, priority=11)
        usage = _usage_of(tmp_path, "CORE0")

        assert budget_status(usage) == STATUS_UNKNOWN
        assert resource_analysis.fits(usage, Fraction("0.55")) is None
        assert resource_analysis.fits(usage, Fraction("0.56")) is False
        assert resource_analysis.fits_size(usage, Fraction("0.56")) is None

    def test_an_advisory_budget_with_an_undeclared_consumer_still_answers(self, tmp_path):
        """Under ``advisory`` a missing budget counts as zero by design, so the status is known."""
        _memory(tmp_path, "RAM", size=1000, threshold=0.5, enforcement="advisory")
        _swc(tmp_path, "Alpha", memory={"RAM": 400})
        _swc(tmp_path, "Beta")
        usage = _usage_of(tmp_path, "RAM")

        assert resource_analysis.fits(usage, Fraction(100)) is True
        assert resource_analysis.fits(usage, Fraction(101)) is False

    def test_a_resource_already_over_budget_fits_nothing(self, tmp_path):
        _memory(tmp_path, "RAM", size=1000, threshold=0.5)
        _swc(tmp_path, "Alpha", memory={"RAM": 900})
        usage = _usage_of(tmp_path, "RAM")

        assert budget_status(usage) == STATUS_BREACH
        assert resource_analysis.fits(usage, Fraction(0)) is False


class TestReachableMemories:
    def test_the_memories_whose_accessible_names_the_core_in_reader_order(self, tmp_path):
        _core(tmp_path, "CORE0")
        _core(tmp_path, "CORE1")
        _memory(tmp_path, "RAM0", accessible=["CORE0"])
        _memory(tmp_path, "RAM1", accessible=["CORE1"])
        _memory(tmp_path, "SHARED", accessible=["CORE0", "CORE1"])
        analysis = analyze_resource_budgets(str(tmp_path))

        assert [usage.name for usage in resource_analysis.reachable_memories(analysis, "CORE0")] == ["RAM0", "SHARED"]
        assert [usage.name for usage in resource_analysis.reachable_memories(analysis, "CORE1")] == ["RAM1", "SHARED"]
        assert resource_analysis.reachable_memories(analysis, "CORE9") == []


# ---------------------------------------------------------------------------
# The physical arithmetic: `physical_limit` and `fits_size`
# ---------------------------------------------------------------------------


class TestPhysicalLimit:
    def test_a_memory_is_limited_by_its_size(self, tmp_path):
        _memory(tmp_path, "RAM", size=32768, threshold=0.75)
        _swc(tmp_path, "Alpha", memory={"RAM": 14336})

        assert resource_analysis.physical_limit(_usage_of(tmp_path, "RAM")) == Fraction(32768)

    def test_a_core_is_limited_by_the_whole_core(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.7)
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, wcet="5500us")

        assert resource_analysis.physical_limit(_usage_of(tmp_path, "CORE0")) == Fraction(1)


class TestFitsSize:
    """``used + amount <= physical_limit``, exact; ``None`` only for a "yes" a missing load could falsify."""

    def test_a_memory_fits_up_to_its_size_inclusive(self, tmp_path):
        _memory(tmp_path, "RAM", size=32768, threshold=0.75)
        _swc(tmp_path, "Alpha", memory={"RAM": 14336})
        usage = _usage_of(tmp_path, "RAM")

        assert resource_analysis.fits_size(usage, Fraction(18432)) is True
        assert resource_analysis.fits_size(usage, Fraction(18433)) is False

    def test_a_core_fits_up_to_one_hundred_percent_with_a_fraction_amount(self, tmp_path):
        # 5500us / 10ms = 55 %: 45 % more is exactly the whole core and fits, a hair more does not.
        _core(tmp_path, "CORE0", threshold=0.7)
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, wcet="5500us")
        usage = _usage_of(tmp_path, "CORE0")

        assert resource_analysis.fits_size(usage, Fraction(45, 100)) is True
        assert resource_analysis.fits_size(usage, Fraction(4501, 10000)) is False

    def test_a_strict_yes_over_an_undeclared_consumer_is_none(self, tmp_path):
        _memory(tmp_path, "RAM", size=32768, threshold=0.75, enforcement="strict")
        _swc(tmp_path, "Alpha", memory={"RAM": 1024})
        _swc(tmp_path, "Beta")
        usage = _usage_of(tmp_path, "RAM")

        assert budget_status(usage) == STATUS_UNKNOWN
        assert resource_analysis.fits_size(usage, Fraction(1)) is None
        assert resource_analysis.fits_size(usage, Fraction(31744)) is None

    def test_a_strict_no_over_an_undeclared_consumer_is_still_given(self, tmp_path):
        """The missing load only adds to the use, so a "no" stays true."""
        _memory(tmp_path, "RAM", size=32768, threshold=0.75, enforcement="strict")
        _swc(tmp_path, "Alpha", memory={"RAM": 1024})
        _swc(tmp_path, "Beta")
        usage = _usage_of(tmp_path, "RAM")

        assert resource_analysis.fits_size(usage, Fraction(31745)) is False

    def test_the_rule_is_keyed_on_the_declarations_not_on_the_status(self, tmp_path):
        """Over its threshold, the status says *over budget* before *unknown*; the size's "yes" is still ``None``."""
        _memory(tmp_path, "RAM", size=1000, threshold=0.5, enforcement="strict")
        _swc(tmp_path, "Alpha", memory={"RAM": 900})
        _swc(tmp_path, "Beta")
        usage = _usage_of(tmp_path, "RAM")

        assert budget_status(usage) == STATUS_BREACH
        assert resource_analysis.fits(usage, Fraction(50)) is False
        assert resource_analysis.fits_size(usage, Fraction(50)) is None
        assert resource_analysis.fits_size(usage, Fraction(101)) is False

    def test_an_advisory_resource_with_an_undeclared_consumer_answers_yes_and_no(self, tmp_path):
        """Under ``advisory`` a missing budget counts as zero by design."""
        _memory(tmp_path, "RAM", size=1000, threshold=0.5, enforcement="advisory")
        _swc(tmp_path, "Alpha", memory={"RAM": 400})
        _swc(tmp_path, "Beta")
        usage = _usage_of(tmp_path, "RAM")

        assert resource_analysis.fits_size(usage, Fraction(600)) is True
        assert resource_analysis.fits_size(usage, Fraction(601)) is False

    def test_a_resource_over_its_threshold_still_fits_within_its_size(self, tmp_path):
        _memory(tmp_path, "RAM", size=1000, threshold=0.5)
        _swc(tmp_path, "Alpha", memory={"RAM": 900})
        usage = _usage_of(tmp_path, "RAM")

        assert budget_status(usage) == STATUS_BREACH
        assert resource_analysis.fits(usage, Fraction(0)) is False
        assert resource_analysis.fits_size(usage, Fraction(100)) is True
        assert resource_analysis.fits_size(usage, Fraction(101)) is False
