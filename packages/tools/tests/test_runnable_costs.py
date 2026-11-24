"""A runnable's declared budget (contract ``9.3.0``, unit-bearing since ``10.0.0``): its fields and rules.

``field:swc.runnable.wcet`` is the runnable's execution-time budget as written,
a whole number and one of ``ns``, ``us``, ``ms``, ``s``; ``field:swc.runnable.wcet_type``
is ``gross`` or ``net`` and travels with it. Four element-local rules refuse at
load: the format (``rule:swc.runnable.wcet.format``), a zero budget
(``rule:swc.runnable.wcet.positive``), the retired ``wcet_us`` spelling
(``rule:swc.runnable.wcet.retired-spelling``) and a budget without its type or
a type without its budget (``rule:swc.runnable.wcet_type.paired``). The
cross-element ``rule:task.runnable-cost-exceeds-task`` fires when the declared
budgets of a task's runnables sum to **more** than the task's own ``wcet``; it
is judged only when the task declares a budget and at least one of its runnables
one, equality is silent, every budget is compared as an exact duration, and a
``net`` budget is summed like a ``gross`` one with a sentence saying so.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from conftest import _core, _root_composition, _swc, _task
from pydantic import ValidationError
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.editing.diagnosis import CONSISTENCY_VIOLATION, RULE_CODES
from swcraft_tools.model_state import ElementParseError
from swcraft_tools.reader import read_swc_model
from swcraft_tools.schema import Runnable
from swcraft_tools.validator import CONSISTENCY_RULES, check_cross_layer_consistency, check_runnable_costs

RULE = "task.runnable-cost-exceeds-task"


def _model(
    root: Path,
    *,
    budget: str | None = "500us",
    costs: tuple[str | None, str | None] = ("200us", "250us"),
    types: tuple[str, str] = ("gross", "gross"),
    root_members: tuple = ("Engine",),
) -> None:
    """One core and partition, a component ``Engine`` with two runnables, and ``Task_10ms`` running both."""
    _core(root, "CORE0")
    runnables = []
    for name, cost, wcet_type in zip(("RunA", "RunB"), costs, types, strict=True):
        runnable: dict = {"name": name, "events": [{"type": "timing", "period": 0.01}]}
        if cost is not None:
            runnable["wcet"] = cost
            runnable["wcet_type"] = wcet_type
        runnables.append(runnable)
    _swc(root, "Engine", runnables=runnables)
    extra = {} if budget is None else {"wcet": budget}
    _task(
        root,
        "Task_10ms",
        runnables=[{"swc": "Engine", "runnable": "RunA"}, {"swc": "Engine", "runnable": "RunB"}],
        **extra,
    )
    _root_composition(root, root_members)


def _findings(root: Path) -> list:
    return [record for record in check_runnable_costs(str(root)).records if record.rule == RULE]


class TestTheField:
    def test_it_is_optional_and_carried_as_written(self) -> None:
        assert (Runnable(name="Run").wcet, Runnable(name="Run").wcet_type) == (None, None)
        runnable = Runnable(name="Run", wcet="850ns", wcet_type="net")
        assert (runnable.wcet, runnable.wcet_type) == ("850ns", "net")

    @pytest.mark.parametrize("value", ["0us", "0ns", "0s"])
    def test_zero_is_refused_at_load_by_the_positive_rule_s_validator(self, value: str) -> None:
        with pytest.raises(ValidationError) as caught:
            Runnable(name="Run", wcet=value, wcet_type="gross")
        (error,) = caught.value.errors()
        assert error["loc"] == ("wcet",)
        # `rule:swc.runnable.wcet.positive`'s binding names this validator.
        assert "wcet must be > 0" in error["msg"]

    @pytest.mark.parametrize(
        ("value", "sentence"),
        [
            (400, "wcet 400 names no unit: write it with one of ns, us, ms, s, e.g. '400us'"),
            (
                "1.5ms",
                "wcet '1.5ms' is not a whole number of ms (an ARXML cseCodeFactor is an Integer): write '1500us'",
            ),
            ("850 ns", "wcet '850 ns' has a space: write '850ns'"),
            ("0850ns", "wcet '0850ns' has a leading zero: write '850ns'"),
            ("850µs", "wcet '850µs' uses 'µs': write '850us'"),
            ("-5us", "wcet '-5us' is not a whole number followed by one of ns, us, ms, s"),
        ],
    )
    def test_the_format_rule_refuses_with_the_spelling_to_write(self, value: Any, sentence: str) -> None:
        with pytest.raises(ValidationError) as caught:
            Runnable(name="Run", wcet=value, wcet_type="gross")
        (error,) = caught.value.errors()
        assert error["loc"] == ("wcet",)
        assert error["msg"] == f"Value error, {sentence}"

    def test_a_budget_without_its_type_is_refused(self) -> None:
        with pytest.raises(ValidationError) as caught:
            Runnable(name="Run", wcet="400us")
        assert caught.value.errors()[0]["msg"] == (
            "Value error, runnable 'Run' declares wcet without a wcet_type: add 'wcet_type: gross' "
            "(time including external calls) or 'wcet_type: net' (without them)"
        )

    def test_a_type_without_its_budget_is_refused(self) -> None:
        with pytest.raises(ValidationError) as caught:
            Runnable(name="Run", wcet_type="net")
        assert caught.value.errors()[0]["msg"] == (
            "Value error, runnable 'Run' declares a wcet_type and no wcet: a type without a budget states "
            "nothing; remove it or add the budget"
        )

    def test_the_retired_spelling_is_refused_with_the_file_s_number(self) -> None:
        for document in ({"name": "Run", "wcet_us": 400}, {"name": "Run", "wcet_us": 400, "wcet": "400us"}):
            with pytest.raises(ValidationError) as caught:
                Runnable.model_validate(document)
            assert caught.value.errors()[0]["msg"] == (
                "Value error, 'wcet_us' was renamed 'wcet' by contract 10.0.0 and now carries its unit: write "
                "'wcet: 400us' for 'wcet_us: 400', and give the budget a 'wcet_type' (gross or net)"
            )

    def test_a_retired_value_that_is_no_integer_gets_the_generic_spelling(self) -> None:
        with pytest.raises(ValidationError, match=re.escape("write 'wcet: <value>us' for 'wcet_us: <value>'")):
            Runnable.model_validate({"name": "Run", "wcet_us": "fast"})

    def test_a_model_instance_passes_the_before_validator_untouched(self) -> None:
        runnable = Runnable(name="Run", wcet="2ms", wcet_type="gross")
        assert Runnable.model_validate(runnable) is runnable


def _swc_file(root: Path, runnable: dict) -> None:
    _swc(root, "Engine", runnables=[{"name": "RunA", **runnable}])


class TestTheLoadRulesThroughAFile:
    """Each rule as a user meets it: a written YAML file, read by the one parser."""

    @pytest.mark.parametrize(
        ("runnable", "sentence"),
        [
            (
                {"wcet_us": 400},
                "'wcet_us' was renamed 'wcet' by contract 10.0.0 and now carries its unit: write 'wcet: 400us' for "
                "'wcet_us: 400', and give the budget a 'wcet_type' (gross or net)",
            ),
            ({"wcet": 400, "wcet_type": "gross"}, "wcet 400 names no unit: write it with one of ns, us, ms, s"),
            ({"wcet": "0us", "wcet_type": "gross"}, "wcet must be > 0"),
            ({"wcet": "400us"}, "runnable 'RunA' declares wcet without a wcet_type"),
            ({"wcet_type": "gross"}, "runnable 'RunA' declares a wcet_type and no wcet"),
        ],
        ids=["retired-spelling", "format", "positive", "paired-budget", "paired-type"],
    )
    def test_each_rule_refuses_the_file(self, tmp_path: Path, runnable: dict, sentence: str) -> None:
        _swc_file(tmp_path, runnable)
        with pytest.raises(ElementParseError) as caught:
            read_swc_model(str(tmp_path))
        assert sentence in str(caught.value)
        assert str(tmp_path) not in str(caught.value)

    def test_the_budget_is_carried_as_written(self, tmp_path: Path) -> None:
        _swc_file(tmp_path, {"wcet": "850ns", "wcet_type": "net"})
        (swc,) = read_swc_model(str(tmp_path))
        assert (swc.runnables[0].wcet, swc.runnables[0].wcet_type) == ("850ns", "net")


class TestTheRule:
    def test_it_is_declared_and_mapped_to_consistency_violation(self) -> None:
        assert RULE in CONSISTENCY_RULES
        assert RULE_CODES[RULE].code == CONSISTENCY_VIOLATION

    def test_it_fires_over_budget_and_names_the_task_the_sum_the_budget_and_the_runnables(self, tmp_path: Path) -> None:
        _model(tmp_path, budget="400us", costs=("200us", "250us"))
        (finding,) = _findings(tmp_path)
        assert finding.message == (
            "Task 'Task_10ms' declares a budget of 400us, but the declared budgets of its runnables sum to "
            "450us: 'Engine.RunA' (200us), 'Engine.RunB' (250us)."
        )
        entry_a = member_step({"swc": "Engine", "instance": None, "runnable": "RunA"})
        entry_b = member_step({"swc": "Engine", "instance": None, "runnable": "RunB"})
        assert finding.subjects == (
            Subject("tasks", "Task_10ms", (field_step("wcet"),)),
            Subject("tasks", "Task_10ms", (field_step("runnables"), entry_a)),
            Subject("tasks", "Task_10ms", (field_step("runnables"), entry_b)),
            Subject("swc", "Engine", (field_step("runnables"), member_step("RunA"), field_step("wcet"))),
            Subject("swc", "Engine", (field_step("runnables"), member_step("RunB"), field_step("wcet"))),
        )

    def test_budgets_in_different_units_are_compared_exactly_and_summed_in_the_finest(self, tmp_path: Path) -> None:
        _model(tmp_path, budget="1ms", costs=("600us", "500000ns"))
        (finding,) = _findings(tmp_path)
        assert finding.message == (
            "Task 'Task_10ms' declares a budget of 1ms, but the declared budgets of its runnables sum to "
            "1100000ns: 'Engine.RunA' (600us), 'Engine.RunB' (500000ns)."
        )
        _model(tmp_path, budget="1ms", costs=("600us", "400000ns"))
        assert _findings(tmp_path) == []

    def test_one_net_budget_is_summed_and_named(self, tmp_path: Path) -> None:
        _model(tmp_path, budget="400us", types=("gross", "net"))
        (finding,) = _findings(tmp_path)
        assert finding.message.endswith(
            "(250us). 1 of them is declared net, so its external calls are not counted in the sum."
        )

    def test_several_net_budgets_are_counted(self, tmp_path: Path) -> None:
        _model(tmp_path, budget="400us", types=("net", "net"))
        (finding,) = _findings(tmp_path)
        assert finding.message.endswith(
            "(250us). 2 of them are declared net, so their external calls are not counted in the sum."
        )

    def test_gross_budgets_say_nothing_of_net(self, tmp_path: Path) -> None:
        _model(tmp_path, budget="400us")
        (finding,) = _findings(tmp_path)
        assert "net" not in finding.message

    def test_check_model_refuses_it(self, tmp_path: Path) -> None:
        _model(tmp_path, budget="400us")
        result = check_cross_layer_consistency(str(tmp_path))
        assert RULE in {record.rule for record in result.records}

    def test_equality_is_silent(self, tmp_path: Path) -> None:
        _model(tmp_path, budget="450us", costs=("200us", "250us"))
        assert _findings(tmp_path) == []

    def test_silent_without_a_budget(self, tmp_path: Path) -> None:
        _model(tmp_path, budget=None, costs=("200us", "250us"))
        assert _findings(tmp_path) == []

    def test_silent_without_a_cost(self, tmp_path: Path) -> None:
        _model(tmp_path, budget="1us", costs=(None, None))
        assert _findings(tmp_path) == []

    def test_a_runnable_without_a_cost_adds_nothing(self, tmp_path: Path) -> None:
        _model(tmp_path, budget="300us", costs=(None, "250us"))
        assert _findings(tmp_path) == []
        _model(tmp_path, budget="200us", costs=(None, "250us"))
        (finding,) = _findings(tmp_path)
        assert "sum to 250us: 'Engine.RunB' (250us)." in finding.message

    def test_an_instance_entry_is_costed_on_the_type_it_resolves_to(self, tmp_path: Path) -> None:
        _model(tmp_path, budget="400us", root_members=({"name": "engine", "type": "Engine"},))
        _task(
            tmp_path,
            "Task_10ms",
            wcet="400us",
            runnables=[{"instance": "engine", "runnable": "RunA"}, {"instance": "engine", "runnable": "RunB"}],
        )
        (finding,) = _findings(tmp_path)
        assert "'engine.RunA' (200us), 'engine.RunB' (250us)." in finding.message
        assert Subject("swc", "Engine", (field_step("runnables"), member_step("RunA"), field_step("wcet"))) in (
            finding.subjects
        )

    def test_one_runnable_reached_by_two_spellings_is_counted_once(self, tmp_path: Path) -> None:
        _model(tmp_path, budget="300us", root_members=({"name": "engine", "type": "Engine"},))
        _task(
            tmp_path,
            "Task_10ms",
            wcet="300us",
            runnables=[{"swc": "Engine", "runnable": "RunB"}, {"instance": "engine", "runnable": "RunB"}],
        )
        assert _findings(tmp_path) == []

    def test_two_occurrences_of_one_type_are_counted_twice(self, tmp_path: Path) -> None:
        """The task runs ``RunA`` once for each placed ``Engine``: 200us twice is over a 300us budget."""
        members = ({"name": "engine1", "type": "Engine"}, {"name": "engine2", "type": "Engine"})
        _model(tmp_path, budget="300us", root_members=members)
        _task(
            tmp_path,
            "Task_10ms",
            wcet="300us",
            runnables=[{"instance": "engine1", "runnable": "RunA"}, {"instance": "engine2", "runnable": "RunA"}],
        )
        (finding,) = _findings(tmp_path)
        assert "sum to 400us: 'engine1.RunA' (200us), 'engine2.RunA' (200us)." in finding.message

    def test_an_unresolved_entry_adds_nothing(self, tmp_path: Path) -> None:
        _model(tmp_path, budget="100us")
        _task(
            tmp_path,
            "Task_10ms",
            wcet="100us",
            runnables=[{"swc": "Missing", "runnable": "RunA"}, {"swc": "Engine", "runnable": "Missing"}],
        )
        assert _findings(tmp_path) == []

    def test_the_message_carries_no_path(self, tmp_path: Path) -> None:
        """`TestNoValidatorMessageCarriesAStoragePath`'s rule, on this check's message."""
        _model(tmp_path, budget="400us")
        (finding,) = _findings(tmp_path)
        assert str(tmp_path) not in finding.message
        assert tmp_path.name not in finding.message
        assert "/" not in finding.message and "\\" not in finding.message
        assert ".yaml" not in finding.message
