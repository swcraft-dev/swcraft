"""The two rules over a runnable's ``symbol``, and every place that refuses them.

A runnable may state a ``symbol`` (AUTOSAR's ``RunnableEntity.symbol``, the
name of its entry point in C); ``null`` means *the symbol is the name*. Two
cross-element rules judge it, one predicate mints both
(``validator.runnable_symbol_records``), and four places ask that predicate:

* ``rule:swc.runnable.symbol-not-c-identifier`` -- a stated symbol must be a C
  identifier, ``[a-zA-Z_][a-zA-Z0-9_]*`` with no length bound, the ``CIdentifier``
  primitive of GST R4.2.2 Table 4.31 and R4.3.0 Table 4.49 (``identifier.py``);
* ``rule:name.duplicate-runnable-symbol`` -- within one component no two
  runnables may be **written** with one symbol (``symbol`` when stated, else the
  name), compared exactly: two runnables of one component share that
  component's symbol, so one runnable symbol would be one entry point for both,
  which ``[constr_2025]`` forbids (System Template R4.2.2 ``:26114-26118``,
  R4.3.0 ``:30512-30516``).

Each is asserted in ``check-model``, in the ARXML writer's up-front guard (no
file), in the component export's closure (no file) and in the editing protocol's
pass D (``consistency-violation``), and every writer and closure case asserts the
rule's **own** message, not only that something refused.

What this does not reach: uniqueness across two components (it depends on each
component's own symbol, which the model does not carry) and across an ECU; and
the ARXML the writer emits for a legal symbol, which is
``test_arxml_symbol_and_port_init_writer.py``'s.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT
from model_contract_fixtures import load_binding, load_contract
from project_support import files_of
from swcraft_tools.component_exchange import export_component
from swcraft_tools.component_exchange.export import ClosureValidationError
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.diagnosis import RULE_CODES
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.identifier import REASON_C_IDENTIFIER
from swcraft_tools.schema import Runnable, SWComponent
from swcraft_tools.validator import CONSISTENCY_RULES, check_cross_layer_consistency, runnable_symbol_records
from swcraft_tools.writer.arxml_generation import generate_arxml

BASIC = REPO_ROOT / "examples" / "basic"
PROVIDER = "swc/example/ExampleSWC.yaml"
CONSUMER = "swc/example/ExampleConsumerSWC.yaml"

NOT_C_IDENTIFIER = "swc.runnable.symbol-not-c-identifier"
DUPLICATE = "name.duplicate-runnable-symbol"
RULES = (NOT_C_IDENTIFIER, DUPLICATE)


def _not_c_identifier_message(swc: str, runnable: str, symbol: str) -> str:
    return (
        f"SWC '{swc}' runnable '{runnable}' states symbol {symbol!r}, which {REASON_C_IDENTIFIER}: the symbol is "
        "the name of the runnable's entry point in C."
    )


def _duplicate_message(swc: str, runnables: str, symbol: str) -> str:
    return (
        f"SWC '{swc}' runnables {runnables} are written with one symbol '{symbol}' (a runnable that states no "
        "symbol is written with its name): two runnables of one component share the component's symbol, so one "
        "runnable symbol gives both one entry point, which [constr_2025] forbids. Give each runnable a symbol of "
        "its own."
    )


# ---------------------------------------------------------------------------
# Builders over a copy of examples/basic
# ---------------------------------------------------------------------------


def _basic(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(BASIC, project, ignore=shutil.ignore_patterns("dist", "views"))
    return project


def _edit(model: Path, relative: str, mutate: Callable[[dict[str, Any]], object]) -> None:
    path = model / relative
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _symbol(model: Path, relative: str, symbol: str, index: int = 0) -> None:
    _edit(model, relative, lambda d: d["runnables"][index].update(symbol=symbol))


def _second_runnable(model: Path, relative: str, name: str, symbol: str | None) -> None:
    runnable: dict[str, Any] = {"name": name}
    if symbol is not None:
        runnable["symbol"] = symbol
    _edit(model, relative, lambda d: d["runnables"].append(runnable))


def _break_not_c_identifier(model: Path) -> None:
    _symbol(model, PROVIDER, "1bad")


def _break_duplicate(model: Path) -> None:
    # A stated symbol equal to the other runnable's NAME: the name is what that one is written with.
    _second_runnable(model, PROVIDER, "Second", "ExampleRun")


BREAKS: dict[str, tuple[Callable[[Path], None], str]] = {
    NOT_C_IDENTIFIER: (_break_not_c_identifier, _not_c_identifier_message("ExampleSWC", "ExampleRun", "1bad")),
    DUPLICATE: (_break_duplicate, _duplicate_message("ExampleSWC", "'ExampleRun' and 'Second'", "ExampleRun")),
}


def _records(model: Path, rule: str) -> list[Any]:
    return [record for record in check_cross_layer_consistency(str(model)).records if record.rule == rule]


def _messages(model: Path, rule: str) -> list[str]:
    return [record.message for record in _records(model, rule)]


# ---------------------------------------------------------------------------
# The rules: declared, bound, answered
# ---------------------------------------------------------------------------


class TestTheRules:
    @pytest.mark.parametrize("rule", RULES)
    def test_declared_bound_and_answered_with_consistency_violation(self, rule: str) -> None:
        (declared,) = [entry for entry in load_contract()["rules"] if entry["id"] == f"rule:{rule}"]
        assert declared["family"] == "cross-element"
        assert declared["summary"] == CONSISTENCY_RULES[rule]
        assert "field:swc.runnable.symbol" in declared["appliesTo"]
        assert load_binding()["rules"][f"rule:{rule}"] == {
            "module": "swcraft_tools.validator.behaviour",
            "callable": "runnable_symbol_records",
        }
        assert RULE_CODES[rule].code == "consistency-violation"

    def test_the_contract_counts(self) -> None:
        families = [entry["family"] for entry in load_contract()["rules"]]
        # The contract's rules by family; moves whenever the contract adds or retires a rule.
        assert len(families) == 352
        assert (families.count("cross-element"), families.count("element-local"), families.count("schema-config")) == (
            207,
            144,
            1,
        )


# ---------------------------------------------------------------------------
# The predicate
# ---------------------------------------------------------------------------


def _component(*runnables: Runnable, name: str = "Owner", external: bool = False) -> SWComponent:
    return SWComponent(name=name, type="application", external=external, runnables=list(runnables))


def _rule_messages(*components: SWComponent) -> list[tuple[str, str]]:
    return [(record.rule, record.message) for record in runnable_symbol_records(list(components))]


class TestNotACIdentifier:
    @pytest.mark.parametrize("symbol", ["1bad", "bad-symbol", "", " padded", "padded ", "in ner", "line\n", "Läufer"])
    def test_a_stated_symbol_that_is_not_one(self, symbol: str) -> None:
        assert _rule_messages(_component(Runnable(name="Run", symbol=symbol))) == [
            (NOT_C_IDENTIFIER, _not_c_identifier_message("Owner", "Run", symbol))
        ]

    @pytest.mark.parametrize("symbol", ["_leading", "Owner_Run", "Run", "x" * 300])
    def test_a_c_identifier_is_silent(self, symbol: str) -> None:
        assert _rule_messages(_component(Runnable(name="Run", symbol=symbol))) == []

    def test_no_stated_symbol_is_silent(self) -> None:
        assert _rule_messages(_component(Runnable(name="Run"))) == []

    def test_the_subject_is_the_runnable_s_symbol(self) -> None:
        (record,) = runnable_symbol_records([_component(Runnable(name="Run", symbol="1bad"))])
        (subject,) = record.subjects
        assert subject.layer == "swc"
        assert subject.element == "Owner"
        assert [step.value for step in subject.path] == ["runnables", "Run", "symbol"]


class TestDuplicateWrittenSymbol:
    def test_two_runnables_stating_one_symbol(self) -> None:
        component = _component(Runnable(name="A", symbol="Shared"), Runnable(name="B", symbol="Shared"))
        assert _rule_messages(component) == [(DUPLICATE, _duplicate_message("Owner", "'A' and 'B'", "Shared"))]

    def test_a_symbol_equal_to_another_runnable_s_name(self) -> None:
        component = _component(Runnable(name="A"), Runnable(name="B", symbol="A"))
        assert _rule_messages(component) == [(DUPLICATE, _duplicate_message("Owner", "'A' and 'B'", "A"))]

    def test_three_in_one_group_are_one_finding(self) -> None:
        component = _component(
            Runnable(name="A", symbol="S"), Runnable(name="B", symbol="S"), Runnable(name="C", symbol="S")
        )
        assert _rule_messages(component) == [(DUPLICATE, _duplicate_message("Owner", "'A', 'B' and 'C'", "S"))]

    def test_a_symbol_equal_to_its_own_runnable_s_name_is_accepted(self) -> None:
        assert _rule_messages(_component(Runnable(name="A", symbol="A"), Runnable(name="B"))) == []

    def test_compared_exactly_since_c_is_case_sensitive(self) -> None:
        assert _rule_messages(_component(Runnable(name="A", symbol="Entry"), Runnable(name="B", symbol="entry"))) == []

    def test_two_components_sharing_a_symbol_are_accepted(self) -> None:
        first = _component(Runnable(name="A", symbol="Shared"), name="First")
        second = _component(Runnable(name="B", symbol="Shared"), name="Second")
        assert _rule_messages(first, second) == []

    def test_without_a_stated_symbol_it_cannot_fire(self) -> None:
        """Two runnables of one name are `rule:name.duplicate-runnable`'s alone: no symbol, no second finding."""
        assert _rule_messages(_component(Runnable(name="A"), Runnable(name="A"))) == []

    def test_the_subjects_are_each_runnable_s_written_symbol(self) -> None:
        (record,) = runnable_symbol_records([_component(Runnable(name="A"), Runnable(name="B", symbol="A"))])
        assert [[step.value for step in subject.path] for subject in record.subjects] == [
            ["runnables", "A"],
            ["runnables", "B", "symbol"],
        ]


def test_an_external_stub_is_skipped() -> None:
    """A stub declares no runnable (`rule:swc.external-with-ports`), so neither rule reads one."""
    stub = _component(Runnable(name="A", symbol="1bad"), Runnable(name="B", symbol="1bad"), external=True)
    assert runnable_symbol_records([stub]) == []


# ---------------------------------------------------------------------------
# check-model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rule", RULES)
def test_each_rule_fires_on_its_break_with_its_message(rule: str, tmp_path: Path) -> None:
    model = _basic(tmp_path) / "model"
    assert _records(model, rule) == []

    BREAKS[rule][0](model)

    assert _messages(model, rule) == [BREAKS[rule][1]]


def _check_model(project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", "check-model", "--project", str(project / "swcraft.yaml")],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
    )


@pytest.mark.parametrize("rule", RULES)
def test_check_model_lists_each_rule(rule: str, tmp_path: Path) -> None:
    project = _basic(tmp_path)
    BREAKS[rule][0](project / "model")

    result = _check_model(project)

    assert result.returncode == 1, result.stdout
    assert BREAKS[rule][1] in result.stderr, result.stderr


def test_the_example_with_legal_symbols_passes_check_model(tmp_path: Path) -> None:
    """A symbol equal to the name, a leading underscore, and one symbol on two components: all legal."""
    project = _basic(tmp_path)
    model = project / "model"
    _symbol(model, PROVIDER, "_Shared")
    _symbol(model, CONSUMER, "_Shared")
    _second_runnable(model, PROVIDER, "Second", "Second")

    assert [record for record in check_cross_layer_consistency(str(model)).records if record.rule in RULES] == []


# ---------------------------------------------------------------------------
# The ARXML writer: refused before any write, with the rule's own message
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
@pytest.mark.parametrize("rule", RULES)
def test_generate_arxml_refuses_with_the_rule_s_message_and_writes_nothing(
    rule: str, version: str, tmp_path: Path
) -> None:
    model = _basic(tmp_path) / "model"
    BREAKS[rule][0](model)
    out = tmp_path / "out" / "model.arxml"

    with pytest.raises(ValueError) as caught:
        generate_arxml(str(model), str(out), version=version)

    assert f"rule:{rule}: {BREAKS[rule][1]}" in str(caught.value)
    assert not out.exists()
    assert not out.parent.exists() or list(out.parent.iterdir()) == []


# ---------------------------------------------------------------------------
# The component export: refused at the closure, before any write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rule", RULES)
def test_the_component_export_refuses_at_the_closure_and_writes_nothing(rule: str, tmp_path: Path) -> None:
    model = _basic(tmp_path) / "model"
    BREAKS[rule][0](model)
    out = tmp_path / "ExampleSWC.arxml"

    with pytest.raises(ClosureValidationError) as caught:
        export_component(str(model), "ExampleSWC", str(out))

    assert caught.value.issues == [BREAKS[rule][1]]
    assert not out.exists()
    assert not Path(f"{out}.tmp").exists()


# ---------------------------------------------------------------------------
# Pass D
# ---------------------------------------------------------------------------


def _approve(proposal: Any) -> ApprovalDecision:
    return ApprovalDecision(
        change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=True
    )


def _change_set(project: Path, operations: list[dict[str, Any]]) -> dict[str, Any]:
    token = handle_request({"operation": "discover"}, project=str(project))["baseline"]
    return handle_request(
        {"operation": "change_set", "baseline": token, "intent": "runnable symbols", "operations": operations},
        project=str(project),
        review=_approve,
    )


CHANGE_SETS: dict[str, list[dict[str, Any]]] = {
    NOT_C_IDENTIFIER: [
        {
            "operation": "update",
            "address": {
                "layer": "swc",
                "element": "ExampleSWC",
                "path": [{"field": "runnables"}, {"member": "ExampleRun"}, {"field": "symbol"}],
            },
            "value": "1bad",
        }
    ],
    DUPLICATE: [
        {
            "operation": "create",
            "address": {
                "layer": "swc",
                "element": "ExampleSWC",
                "path": [{"field": "runnables"}, {"member": "Second"}],
            },
            "value": {"name": "Second", "symbol": "ExampleRun"},
        }
    ],
}


@pytest.mark.parametrize("rule", RULES)
def test_pass_d_refuses_a_change_set_that_introduces_each(rule: str, tmp_path: Path) -> None:
    project = _basic(tmp_path)
    before = files_of(project / "model")

    response = _change_set(project, CHANGE_SETS[rule])

    assert response["response"] == "error", response
    assert response["error"] == "consistency-violation", response
    messages = [response["message"], *(entry["message"] for entry in response.get("additional", []))]
    assert any(message.startswith(BREAKS[rule][1]) for message in messages), response
    assert files_of(project / "model") == before


def test_a_shared_symbol_with_a_line_break_is_rendered_not_printed_raw() -> None:
    """The duplicate rule renders the shared symbol through ``render_model_scalar``: no raw line break in a message."""
    swc = SWComponent(
        name="InventedComp",
        runnables=[Runnable(name="InventedA", symbol="a\nb"), Runnable(name="InventedB", symbol="a\nb")],
    )

    (duplicate,) = [record for record in runnable_symbol_records([swc]) if record.rule == DUPLICATE]

    assert "written with one symbol 'a\\nb' (" in duplicate.message
    assert "\n" not in duplicate.message
