"""Tests for the resource-budget verdicts and their validator wiring.

CPU utilization and memory consumption against their thresholds, rate-monotonic
priority conformance, the exact-rational boundary behaviour, and
``check_resource_budgets`` / ``check_cross_layer_consistency`` wiring.

All threshold comparisons use ``fractions.Fraction``: a plain float comparison
produces false breaches at the boundary (``100 * 0.29`` is ``28.999999999999996``
in Python), and ``Task.period`` is never quantized to integer microseconds.
"""

from __future__ import annotations

import pathlib
from fractions import Fraction

import pytest
from conftest import _codes, _core, _memory, _processor, _root_composition, _swc, _task
from swcraft_tools.resource_analysis import (
    FINDING_MISSING_BUDGET,
    FINDING_RATE_MONOTONIC_VIOLATION,
    FINDING_THRESHOLD_BREACH,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    analyze_resource_budgets,
    format_bytes,
    format_percent,
)
from swcraft_tools.validator import check_cross_layer_consistency, check_resource_budgets, model_view

#: The multi-core, multi-memory acceptance fixture model.
_FIXTURE_MODEL = str(pathlib.Path(__file__).parent / "fixtures" / "hardware" / "model")


def _usage(model_root, name: str):
    """The single :class:`ResourceUsage` for the named core or memory."""
    analysis = analyze_resource_budgets(str(model_root))
    return next(u for u in analysis.resources if u.name == name)


# ---------------------------------------------------------------------------
# CPU utilization
# ---------------------------------------------------------------------------


class TestCpuUtilization:
    @staticmethod
    def _roadmap_example(model_root, first_wcet: str, threshold: float = 0.7) -> None:
        """400us/1ms + 900us/5ms + 1500us/10ms = 73% against a 70% threshold."""
        _core(model_root, "CORE0", threshold=threshold)
        _task(model_root, "Task_1ms", period=0.001, priority=30, partition="PARTITION0", wcet=first_wcet)
        _task(model_root, "Task_5ms", period=0.005, priority=20, partition="PARTITION0", wcet="900us")
        _task(model_root, "Task_10ms", period=0.01, priority=10, partition="PARTITION0", wcet="1500us")

    def test_worked_roadmap_example_breaches_the_threshold(self, tmp_path):
        self._roadmap_example(tmp_path, first_wcet="400us")

        usage = _usage(tmp_path, "CORE0")

        assert usage.consumption == pytest.approx(0.73)
        assert _codes(usage.findings) == [FINDING_THRESHOLD_BREACH]
        assert usage.findings[0].severity == SEVERITY_ERROR
        assert "CPU core 'CORE0'" in usage.findings[0].message
        assert "73.00%" in usage.findings[0].message
        assert "70.00%" in usage.findings[0].message

        result = check_resource_budgets(str(tmp_path))
        assert result.consistent is False
        assert result.issues == [usage.findings[0].message]

    def test_same_example_passes_once_the_first_task_drops_to_250us(self, tmp_path):
        self._roadmap_example(tmp_path, first_wcet="250us")

        usage = _usage(tmp_path, "CORE0")

        assert usage.consumption == pytest.approx(0.58)
        assert usage.findings == []
        assert check_resource_budgets(str(tmp_path)).consistent is True

    def test_utilization_is_summed_per_core_independently(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.7)
        _core(tmp_path, "CORE1", threshold=0.7)
        _task(tmp_path, "Task_a", period=0.01, priority=10, partition="PARTITION0", wcet="6000us")
        _task(tmp_path, "Task_b", period=0.01, priority=10, partition="PARTITION1", wcet="8000us")

        core0, core1 = analyze_resource_budgets(str(tmp_path)).resources

        assert core0.findings == []
        assert _codes(core1.findings) == [FINDING_THRESHOLD_BREACH]

    def test_a_core_with_no_tasks_never_breaches(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.7)

        usage = _usage(tmp_path, "CORE0")

        assert usage.consumption == 0.0
        assert usage.findings == []


# ---------------------------------------------------------------------------
# Memory consumption
# ---------------------------------------------------------------------------


class TestMemoryConsumption:
    def test_consumption_over_size_times_threshold_is_a_breach(self, tmp_path):
        _memory(tmp_path, "RAM", size=1000, threshold=0.8)
        _swc(tmp_path, "Alpha", memory={"RAM": 500})
        _swc(tmp_path, "Beta", memory={"RAM": 301})

        usage = _usage(tmp_path, "RAM")

        assert usage.consumption == pytest.approx(801.0)
        assert _codes(usage.findings) == [FINDING_THRESHOLD_BREACH]
        assert usage.findings[0].severity == SEVERITY_ERROR
        assert "801 bytes" in usage.findings[0].message
        assert "800 bytes" in usage.findings[0].message

        assert check_resource_budgets(str(tmp_path)).issues == [usage.findings[0].message]

    def test_consumption_under_the_budget_passes(self, tmp_path):
        _memory(tmp_path, "RAM", size=1000, threshold=0.8)
        _swc(tmp_path, "Alpha", memory={"RAM": 500})
        _swc(tmp_path, "Beta", memory={"RAM": 299})

        assert _usage(tmp_path, "RAM").findings == []
        assert check_resource_budgets(str(tmp_path)).consistent is True

    def test_pool_with_no_consumers_at_all_never_breaches(self, tmp_path):
        _memory(tmp_path, "RAM", size=1000, threshold=0.8)

        usage = _usage(tmp_path, "RAM")

        assert usage.consumers == []
        assert usage.consumption == 0.0
        assert usage.findings == []
        assert check_resource_budgets(str(tmp_path)).consistent is True

    def test_threshold_one_allows_the_full_size(self, tmp_path):
        _memory(tmp_path, "RAM", size=1000, threshold=1.0)
        _swc(tmp_path, "Alpha", memory={"RAM": 1000})

        assert _usage(tmp_path, "RAM").findings == []


# ---------------------------------------------------------------------------
# Exact-rational boundary behaviour
# ---------------------------------------------------------------------------


class TestExactRationalBoundaries:
    def test_memory_exactly_at_a_threshold_the_float_product_would_misjudge(self, tmp_path):
        # `100 * 0.29` is 28.999999999999996 in binary floats, so a naive
        # comparison would report this 29-byte consumption as over budget.
        assert 100 * 0.29 < 29  # guards the premise of this test
        _memory(tmp_path, "RAM", size=100, threshold=0.29)
        _swc(tmp_path, "Alpha", memory={"RAM": 29})

        assert _usage(tmp_path, "RAM").findings == []
        assert check_resource_budgets(str(tmp_path)).consistent is True

    def test_memory_one_byte_over_that_same_boundary_is_a_breach(self, tmp_path):
        _memory(tmp_path, "RAM", size=100, threshold=0.29)
        _swc(tmp_path, "Alpha", memory={"RAM": 30})

        assert _codes(_usage(tmp_path, "RAM").findings) == [FINDING_THRESHOLD_BREACH]

    def test_a_fractional_megabyte_budget_is_never_rendered_rounded_up(self, tmp_path):
        """A 1 MiB pool at 0.8 has an exact budget of 838860.8 bytes.

        Six significant digits would render it `838861`, so a consumption of exactly
        838861 bytes would "exceed its budget of 838861 bytes".
        """
        _memory(tmp_path, "RAM", size=1024 * 1024, threshold=0.8)
        _swc(tmp_path, "Alpha", memory={"RAM": 838861})

        message = _usage(tmp_path, "RAM").findings[0].message

        assert "consumption 838861 bytes exceeds its budget of 838860.8 bytes" in message
        assert "838861 bytes exceeds its budget of 838861" not in message

    def test_a_multi_megabyte_budget_is_never_rendered_in_exponential_notation(self, tmp_path):
        _memory(tmp_path, "RAM", size=2 * 1024 * 1024, threshold=0.8)
        _swc(tmp_path, "Alpha", memory={"RAM": 2 * 1024 * 1024})

        message = _usage(tmp_path, "RAM").findings[0].message

        assert "1677721.6 bytes" in message
        assert "e+" not in message

    def test_cpu_exactly_at_a_threshold_the_float_quotient_would_misjudge(self, tmp_path):
        # 2350us / 5ms is 0.47000000000000003 in binary floats, a false breach
        # of a 0.47 threshold.
        assert 2350 * 1e-6 / 0.005 > 0.47  # guards the premise of this test
        _core(tmp_path, "CORE0", threshold=0.47)
        _task(tmp_path, "Task_5ms", period=0.005, priority=20, partition="PARTITION0", wcet="2350us")

        assert _usage(tmp_path, "CORE0").findings == []
        assert check_resource_budgets(str(tmp_path)).consistent is True

    def test_cpu_one_microsecond_over_that_same_boundary_is_a_breach(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.47)
        _task(tmp_path, "Task_5ms", period=0.005, priority=20, partition="PARTITION0", wcet="2351us")

        assert _codes(_usage(tmp_path, "CORE0").findings) == [FINDING_THRESHOLD_BREACH]

    def test_usage_figures_are_exact_fractions_all_the_way_to_the_renderer(self, tmp_path):
        """`ResourceUsage` carries exact `Fraction`s, never floats.

        The printed figures must be the numbers the exact verdict used; a float on the
        way out reopens the gap the comparison closes.
        """
        _memory(tmp_path, "RAM", size=2**54, threshold=1.0)
        _swc(tmp_path, "Alpha", memory={"RAM": 2**53 + 1})

        usage = _usage(tmp_path, "RAM")

        assert isinstance(usage.consumption, Fraction)
        assert isinstance(usage.limit, Fraction)
        assert isinstance(usage.headroom, Fraction)
        assert isinstance(usage.consumers[0].amount, Fraction)
        # 2**53 + 1 is the smallest integer a float cannot hold: float() rounds
        # it down to 2**53 and the byte count silently reads one lower.
        assert format_bytes(usage.consumption) == str(2**53 + 1)
        assert format_bytes(usage.consumers[0].amount) == str(2**53 + 1)

    def test_the_headline_figure_and_the_breach_message_spell_one_number(self, tmp_path):
        """A total marginally over the threshold whose float form is exactly 0.7.

        The breach message renders the exact total (`~70.00%`) and the headline renders
        `usage.consumption`; a float field would claim a flat `70.00%`, the very threshold
        it is reported as exceeding.
        """
        _core(tmp_path, "CORE0", threshold=0.7)
        _task(tmp_path, "Task_1ms", period=0.001, priority=30, partition="PARTITION0", wcet="700us")
        _task(tmp_path, "Task_slow", period=1e11, priority=10, partition="PARTITION0", wcet="1us")

        usage = _usage(tmp_path, "CORE0")

        assert float(usage.consumption) == 0.7  # guards the premise of this test
        assert _codes(usage.findings) == [FINDING_THRESHOLD_BREACH]
        assert format_percent(usage.consumption) == "~70.00%"
        assert f"utilization {format_percent(usage.consumption)} exceeds" in usage.findings[0].message

    def test_cpu_threshold_one_allows_a_fully_loaded_core(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=1.0)
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, partition="PARTITION0", wcet="10000us")

        assert _usage(tmp_path, "CORE0").findings == []


class TestSubMicrosecondPeriods:
    def test_fractional_microsecond_period_is_not_rounded(self, tmp_path):
        # 1us / 1.5us is exactly 2/3. Quantizing the period to 2us would yield
        # 0.5 and hide this breach of a 0.6 threshold.
        _core(tmp_path, "CORE0", threshold=0.6)
        _task(tmp_path, "Task_fast", period=1.5e-06, priority=40, partition="PARTITION0", wcet="1us")

        usage = _usage(tmp_path, "CORE0")

        assert usage.consumers[0].amount == pytest.approx(2 / 3)
        assert usage.consumption == pytest.approx(2 / 3)
        assert _codes(usage.findings) == [FINDING_THRESHOLD_BREACH]

    def test_sub_microsecond_period_does_not_divide_by_zero(self, tmp_path):
        # Quantizing a 0.5us period to integer microseconds would give 0.
        _core(tmp_path, "CORE0", threshold=1.0)
        _task(tmp_path, "Task_fast", period=5e-07, priority=40, partition="PARTITION0", wcet="1us")

        usage = _usage(tmp_path, "CORE0")

        assert usage.consumption == pytest.approx(2.0)
        assert _codes(usage.findings) == [FINDING_THRESHOLD_BREACH]
        assert "200.00%" in usage.findings[0].message


# ---------------------------------------------------------------------------
# Rate-monotonic priority conformance
# ---------------------------------------------------------------------------


class TestRateMonotonicConformance:
    def test_shorter_period_with_lower_priority_is_a_violation(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.9)
        _task(tmp_path, "Task_10ms", period=0.01, priority=5, partition="PARTITION0", wcet="100us")
        _task(tmp_path, "Task_100ms", period=0.1, priority=20, partition="PARTITION0", wcet="1000us")

        usage = _usage(tmp_path, "CORE0")

        assert _codes(usage.findings) == [FINDING_RATE_MONOTONIC_VIOLATION]
        finding = usage.findings[0]
        assert finding.severity == SEVERITY_ERROR
        # The core is named, not numbered: `Task.core` references the core's identity.
        assert "on core CORE0" in finding.message
        assert "Task_10ms" in finding.message
        assert "Task_100ms" in finding.message

        assert check_resource_budgets(str(tmp_path)).issues == [finding.message]

    def test_rate_monotonic_assignment_produces_no_finding(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.9)
        _task(tmp_path, "Task_10ms", period=0.01, priority=20, partition="PARTITION0", wcet="100us")
        _task(tmp_path, "Task_100ms", period=0.1, priority=5, partition="PARTITION0", wcet="1000us")

        assert _usage(tmp_path, "CORE0").findings == []

    def test_equal_priorities_on_different_periods_violate_the_strict_ordering(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.9)
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, partition="PARTITION0", wcet="100us")
        _task(tmp_path, "Task_100ms", period=0.1, priority=10, partition="PARTITION0", wcet="1000us")

        assert _codes(_usage(tmp_path, "CORE0").findings) == [FINDING_RATE_MONOTONIC_VIOLATION]

    def test_equal_periods_impose_no_ordering(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.9)
        _task(tmp_path, "Task_a", period=0.01, priority=10, partition="PARTITION0", wcet="100us")
        _task(tmp_path, "Task_b", period=0.01, priority=5, partition="PARTITION0", wcet="100us")

        assert _usage(tmp_path, "CORE0").findings == []

    def test_tasks_on_different_cores_are_compared_independently(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.9)
        _core(tmp_path, "CORE1", threshold=0.9)
        _task(tmp_path, "Task_10ms", period=0.01, priority=5, partition="PARTITION0", wcet="100us")
        _task(tmp_path, "Task_100ms", period=0.1, priority=20, partition="PARTITION1", wcet="1000us")

        core0, core1 = analyze_resource_budgets(str(tmp_path)).resources

        assert core0.findings == []
        assert core1.findings == []

    def test_a_single_task_core_is_vacuously_conformant(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.9)
        _task(tmp_path, "Task_10ms", period=0.01, priority=5, partition="PARTITION0", wcet="100us")

        assert _usage(tmp_path, "CORE0").findings == []

    def test_a_fully_inverted_core_reports_one_finding_per_adjacent_inversion(self, tmp_path):
        """Aggregation policy: adjacent pairs of the (period, priority-desc) ordering, not all pairs.

        A fully inverted core is one mistake; all pairs (28 for 8 tasks) would bury every
        other issue. Adjacent pairs are complete — if every boundary holds, the ordering
        is transitively rate-monotonic — and report each inversion once.
        """
        _core(tmp_path, "CORE0", threshold=1.0)
        _task(tmp_path, "Task_1ms", period=0.001, priority=1, partition="PARTITION0", wcet="1us")
        _task(tmp_path, "Task_2ms", period=0.002, priority=2, partition="PARTITION0", wcet="1us")
        _task(tmp_path, "Task_4ms", period=0.004, priority=3, partition="PARTITION0", wcet="1us")
        _task(tmp_path, "Task_8ms", period=0.008, priority=4, partition="PARTITION0", wcet="1us")

        findings = _usage(tmp_path, "CORE0").findings

        assert _codes(findings) == [FINDING_RATE_MONOTONIC_VIOLATION] * 3
        assert [(f.message.split("'")[1], f.message.split("'")[3]) for f in findings] == [
            ("Task_1ms", "Task_2ms"),
            ("Task_2ms", "Task_4ms"),
            ("Task_4ms", "Task_8ms"),
        ]

    def test_one_misplaced_task_among_conformant_ones_is_reported_once(self, tmp_path):
        # Task_2ms is the only task out of order; the 1ms/4ms/8ms tasks are
        # rate-monotonic among themselves.
        _core(tmp_path, "CORE0", threshold=1.0)
        _task(tmp_path, "Task_1ms", period=0.001, priority=40, partition="PARTITION0", wcet="1us")
        _task(tmp_path, "Task_2ms", period=0.002, priority=5, partition="PARTITION0", wcet="1us")
        _task(tmp_path, "Task_4ms", period=0.004, priority=20, partition="PARTITION0", wcet="1us")
        _task(tmp_path, "Task_8ms", period=0.008, priority=10, partition="PARTITION0", wcet="1us")

        findings = _usage(tmp_path, "CORE0").findings

        assert len(findings) == 1
        assert "'Task_2ms'" in findings[0].message
        assert "'Task_4ms'" in findings[0].message

    def test_equal_period_groups_are_compared_at_their_boundaries_only(self, tmp_path):
        """Within a period group priority is free; across groups the extremes decide."""
        _core(tmp_path, "CORE0", threshold=1.0)
        _task(tmp_path, "Task_fast_a", period=0.001, priority=30, partition="PARTITION0", wcet="1us")
        _task(tmp_path, "Task_fast_b", period=0.001, priority=10, partition="PARTITION0", wcet="1us")
        _task(tmp_path, "Task_slow_a", period=0.01, priority=20, partition="PARTITION0", wcet="1us")
        _task(tmp_path, "Task_slow_b", period=0.01, priority=5, partition="PARTITION0", wcet="1us")

        findings = _usage(tmp_path, "CORE0").findings

        # The group's lowest priority (Task_fast_b, 10) is not higher than the
        # slower group's highest (Task_slow_a, 20) — reported exactly once.
        assert len(findings) == 1
        assert "'Task_fast_b'" in findings[0].message
        assert "'Task_slow_a'" in findings[0].message

    def test_a_conformant_multi_task_core_reports_nothing(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=1.0)
        for index, period in enumerate([0.001, 0.002, 0.004, 0.008, 0.016]):
            _task(
                tmp_path, f"Task_{index}", period=period, priority=50 - index * 10, partition="PARTITION0", wcet="1us"
            )

        assert _usage(tmp_path, "CORE0").findings == []

    def test_a_task_without_wcet_still_takes_part_in_the_ordering_check(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.9, enforcement="advisory")
        _task(tmp_path, "Task_10ms", period=0.01, priority=5, partition="PARTITION0")
        _task(tmp_path, "Task_100ms", period=0.1, priority=20, partition="PARTITION0", wcet="1000us")

        assert _codes(_usage(tmp_path, "CORE0").findings) == [
            FINDING_MISSING_BUDGET,
            FINDING_RATE_MONOTONIC_VIOLATION,
        ]


# ---------------------------------------------------------------------------
# advisory downgrades every per-resource verdict to `info`
# ---------------------------------------------------------------------------


class TestAdvisoryEnforcement:
    def test_advisory_cpu_breach_is_info_and_never_an_issue(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.5, enforcement="advisory")
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, partition="PARTITION0", wcet="8000us")

        usage = _usage(tmp_path, "CORE0")

        assert _codes(usage.findings) == [FINDING_THRESHOLD_BREACH]
        assert usage.findings[0].severity == SEVERITY_INFO
        assert check_resource_budgets(str(tmp_path)).consistent is True
        assert check_resource_budgets(str(tmp_path)).issues == []

    def test_advisory_memory_breach_is_info_and_never_an_issue(self, tmp_path):
        _memory(tmp_path, "RAM", size=100, threshold=0.5, enforcement="advisory")
        _swc(tmp_path, "Alpha", memory={"RAM": 90})

        usage = _usage(tmp_path, "RAM")

        assert [f.severity for f in usage.findings] == [SEVERITY_INFO]
        assert check_resource_budgets(str(tmp_path)).consistent is True

    def test_advisory_rate_monotonic_violation_is_info_and_never_an_issue(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.9, enforcement="advisory")
        _task(tmp_path, "Task_10ms", period=0.01, priority=5, partition="PARTITION0", wcet="100us")
        _task(tmp_path, "Task_100ms", period=0.1, priority=20, partition="PARTITION0", wcet="1000us")

        usage = _usage(tmp_path, "CORE0")

        assert _codes(usage.findings) == [FINDING_RATE_MONOTONIC_VIOLATION]
        assert usage.findings[0].severity == SEVERITY_INFO
        assert check_resource_budgets(str(tmp_path)).consistent is True

    def test_advisory_missing_budget_is_info_and_never_an_issue(self, tmp_path):
        _core(tmp_path, "CORE0", enforcement="advisory")
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, partition="PARTITION0")

        assert check_resource_budgets(str(tmp_path)).consistent is True


# ---------------------------------------------------------------------------
# check_resource_budgets drains both finding levels
# ---------------------------------------------------------------------------


class TestCheckResourceBudgets:
    def test_hardware_with_no_budget_is_always_consistent(self, tmp_path):
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, partition="PARTITION7", wcet="10us")
        _swc(tmp_path, "Alpha", memory={"NoSuchMemory": 128})

        result = check_resource_budgets(str(tmp_path))

        assert result.consistent is True
        assert result.issues == []

    def test_an_unresolved_reference_alone_is_no_longer_this_check_s(self, tmp_path):
        """A task in an undeclared partition is the topology check's finding, not this check's.

        ``check_hardware_topology`` reports it as ``hardware.undeclared-partition`` within
        ``check_cross_layer_consistency``, so the model still fails ``check-model`` while
        ``check_resource_budgets`` is silent.
        """
        _core(tmp_path, "CORE0", threshold=1.0)
        _task(tmp_path, "Task_declared", period=0.01, priority=10, partition="PARTITION0", wcet="10us")
        _task(tmp_path, "Task_orphan", period=0.01, priority=10, partition="PARTITION9", wcet="10us")

        analysis = analyze_resource_budgets(str(tmp_path))
        assert analysis.all_findings() == []

        assert check_resource_budgets(str(tmp_path)).consistent is True
        assert not check_cross_layer_consistency(str(tmp_path)).consistent

    def test_every_resources_findings_are_drained_in_order(self, tmp_path):
        """The drain walks the resources in reader order, concatenating each one's findings."""
        _core(tmp_path, "CORE0", threshold=0.5)
        _memory(tmp_path, "RAM", size=16, threshold=0.5)
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, partition="PARTITION0", wcet="8000us")
        _swc(tmp_path, "Alpha", memory={"RAM": 4096})

        result = check_resource_budgets(str(tmp_path))

        assert len(result.issues) == 2
        assert "CPU core 'CORE0' utilization" in result.issues[0]
        assert "Memory 'RAM' consumption" in result.issues[1]


# ---------------------------------------------------------------------------
# check_cross_layer_consistency wiring
# ---------------------------------------------------------------------------


class TestCrossLayerWiring:
    def test_a_budget_breach_surfaces_in_the_aggregate_check(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.5)
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, partition="PARTITION0", wcet="8000us")

        result = check_cross_layer_consistency(str(tmp_path))

        assert result.consistent is False
        assert any("exceeds its threshold" in issue for issue in result.issues)

    def test_an_advisory_breach_does_not_surface_in_the_aggregate_check(self, tmp_path):
        # The processor and the root composition are written because
        # `hardware.no-processor` and `composition.no-root` are both part of the
        # aggregate: without them the model fails for a reason that has nothing
        # to do with the claim under test.
        _processor(tmp_path)
        _root_composition(tmp_path)
        _core(tmp_path, "CORE0", threshold=0.5, enforcement="advisory")
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, partition="PARTITION0", wcet="8000us")

        result = check_cross_layer_consistency(str(tmp_path))

        assert result.consistent is True
        assert result.issues == []

    def test_a_model_whose_hardware_declares_no_budget_is_unaffected_by_this_check(self, tmp_path):
        # `check_cross_layer_consistency` still refuses the model — `hardware/`
        # is mandatory and this one has no processor — so the claim asserted
        # here is the budget check's own verdict, not the aggregate's.
        _task(tmp_path, "Task_10ms", period=0.01, priority=10, partition="PARTITION0")

        assert check_resource_budgets(str(tmp_path)).consistent is True


# ---------------------------------------------------------------------------
# Acceptance fixture — multi-core, multi-pool, every outcome at once
# ---------------------------------------------------------------------------


class TestAcceptanceFixture:
    def test_every_declared_core_and_memory_is_analyzed(self):
        analysis = analyze_resource_budgets(_FIXTURE_MODEL)

        # Reader order, and the processor is not among them: `Resource` does not admit it.
        assert [u.name for u in analysis.resources] == ["CORE0", "CORE1", "CORE2", "CORE3", "RAM", "ROM"]

    def test_the_passing_core_and_pool_have_no_findings(self):
        analysis = analyze_resource_budgets(_FIXTURE_MODEL)
        usages = {u.name: u for u in analysis.resources}

        assert usages["CORE0"].consumption == pytest.approx(0.15)
        assert usages["CORE0"].findings == []
        assert usages["RAM"].consumption == pytest.approx(1536.0)
        assert usages["RAM"].findings == []

    def test_the_cpu_breach_memory_breach_and_rm_violation_are_errors(self):
        usages = {u.name: u for u in analyze_resource_budgets(_FIXTURE_MODEL).resources}

        assert usages["CORE1"].consumption == pytest.approx(0.73)
        assert _codes(usages["CORE1"].findings) == [FINDING_THRESHOLD_BREACH]
        assert usages["CORE1"].findings[0].severity == SEVERITY_ERROR

        assert _codes(usages["CORE2"].findings) == [FINDING_RATE_MONOTONIC_VIOLATION]
        assert usages["CORE2"].findings[0].severity == SEVERITY_ERROR

        assert usages["ROM"].consumption == pytest.approx(1000.0)
        assert _codes(usages["ROM"].findings) == [FINDING_THRESHOLD_BREACH]
        assert usages["ROM"].findings[0].severity == SEVERITY_ERROR

    def test_the_advisory_core_breaches_as_info_only(self):
        usages = {u.name: u for u in analyze_resource_budgets(_FIXTURE_MODEL).resources}

        assert usages["CORE3"].consumption == pytest.approx(0.8)
        assert _codes(usages["CORE3"].findings) == [FINDING_THRESHOLD_BREACH]
        assert usages["CORE3"].findings[0].severity == SEVERITY_INFO

    def test_check_resource_budgets_reports_exactly_the_three_strict_breaches(self):
        result = check_resource_budgets(_FIXTURE_MODEL)

        assert result.consistent is False
        assert len(result.issues) == 3
        assert any("'CORE1'" in issue and "exceeds its threshold" in issue for issue in result.issues)
        assert any("Rate-monotonic violation on core CORE2" in issue for issue in result.issues)
        assert any("'ROM'" in issue and "exceeds its budget" in issue for issue in result.issues)
        assert all("CORE3" not in issue for issue in result.issues)


# ---------------------------------------------------------------------------
# What the check reads
# ---------------------------------------------------------------------------


class TestTheLayersTheCheckReads:
    @staticmethod
    def _count(monkeypatch, name: str, *, refuse: bool = False) -> list[str]:
        """Route the view's reader *name* through a counter; with *refuse*, any call fails the test."""
        calls: list[str] = []
        real = getattr(model_view, name)

        def counted(root):
            if refuse:
                raise AssertionError(f"{name} must not be read when no budget is declared")
            calls.append(root)
            return real(root)

        monkeypatch.setattr(model_view, name, counted)
        return calls

    def test_no_budget_means_the_task_and_swc_layers_are_never_read(self, tmp_path, monkeypatch):
        self._count(monkeypatch, "read_tasks", refuse=True)
        self._count(monkeypatch, "read_swc_model", refuse=True)
        _processor(tmp_path)

        assert check_resource_budgets(str(tmp_path)).consistent is True

    def test_a_budgeted_resource_reads_each_layer_once(self, tmp_path, monkeypatch):
        tasks = self._count(monkeypatch, "read_tasks")
        swcs = self._count(monkeypatch, "read_swc_model")
        _core(tmp_path, "CORE0")

        check_resource_budgets(str(tmp_path))

        assert (len(tasks), len(swcs)) == (1, 1)
