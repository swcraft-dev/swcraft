"""``rule:swc.undeclared-termination-and-restart``, and every reader of it.

``SwcInternalBehavior.handleTerminationAndRestart`` has multiplicity 1 in the template at R4.2.2 (SWCT ``:22932``)
and R4.3.0 (SWCT ``:25469``), while both schemas declare the element ``minOccurs="0"``. SWCraft makes the omission a
refusal:

* **the predicate**: ``validator.components_without_termination_handling``, every component that is not an external
  stub and sets no value, in read order; **the message**: one sentence, printed by every reader;
* **``check-model``**: ``check_termination_and_restart``, the last member of the composed tuple;
* **the writer**: ``_check_termination_and_restart``, last in ``_build_document`` (after emission and the name-space
  guard, before anything is written), so a broken connector is still reported first;
* **``--component`` and ``--all-components``**: a ``ClosureValidationError``; the batch lists it as failed and goes
  on; **``validate``** stops at ``check-model``;
* **the editing protocol**: pass D refuses a ``create`` without the field and every change set on a baseline holding
  an offending component, naming every one; under a narrow approved scope the repair is refused, while an unscoped
  session repairs every component in one change set;
* **the imports** do not refuse a source without the element; ``check-model`` then reports the component;
* **no written file**, through any of the four writing paths, holds a behaviour without the element.

The MCP ``import_component`` draft is ``packages/mcp/tests/test_import_component_termination.py``'s, and the
editor-schema limitation row ``test_editor_schema_limits.py``'s.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from behaviour_losses_support import MINIMAL_SWC
from call_order_support import record_calls
from conftest import REPO_ROOT
from lxml import etree
from model_contract_fixtures import load_binding, load_contract
from project_support import files_of, init_project
from swcraft_tools.arxml_import.pipeline import run_import
from swcraft_tools.arxml_version import ArxmlVersion
from swcraft_tools.cli import main
from swcraft_tools.component_exchange import (
    ClosureValidationError,
    export_all_components,
    export_component,
    import_component,
)
from swcraft_tools.diagnostics import Subject, field_step
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.diagnosis import RULE_CODES
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.schema import SWComponent
from swcraft_tools.validator import (
    CONSISTENCY_RULES,
    check_cross_layer_consistency,
    check_termination_and_restart,
    components_without_termination_handling,
    termination_handling_message,
)
from swcraft_tools.writer import arxml_generation
from swcraft_tools.writer.arxml_generation import _emit_handle_termination_and_restart, generate_arxml
from swcraft_tools.writer.arxml_xml import ar_qname
from test_validator_storage import _composed_members

RULE = "swc.undeclared-termination-and-restart"
RULE_ID = f"rule:{RULE}"
FIELD = "handle_termination_and_restart"
TAG = "HANDLE-TERMINATION-AND-RESTART"
BASIC = REPO_ROOT / "examples" / "basic"
PROVIDER = "ExampleSWC"
CONSUMER = "ExampleConsumerSWC"
VERSIONS = [str(version) for version in ArxmlVersion]
VALUES = ["no_support", "can_be_terminated", "can_be_terminated_and_restarted"]


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _basic(tmp_path: Path) -> Path:
    """A copy of ``examples/basic``: both its components declare ``no_support``."""
    project = tmp_path / "project"
    shutil.copytree(BASIC, project, ignore=shutil.ignore_patterns("dist", "views"))
    return project


def _edit(path: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _swc_file(model: Path, name: str) -> Path:
    (path,) = list(model.glob(f"swc/**/{name}.yaml"))
    return path


def _undeclare(model: Path, *names: str) -> None:
    """Remove *names*' ``handle_termination_and_restart``."""
    for name in names:
        _edit(_swc_file(model, name), lambda document: document.pop(FIELD))


def _without(tmp_path: Path, *names: str) -> Path:
    """``examples/basic`` with *names* declaring no value (the consumer by default)."""
    project = _basic(tmp_path)
    _undeclare(project / "model", *(names or (CONSUMER,)))
    return project


def _break_connector(model: Path) -> None:
    _edit(model / "connectors" / "Example_to_Consumer.yaml", lambda connector: connector["from"].update(port="absent"))


def _case_only_port(model: Path) -> None:
    _edit(
        _swc_file(model, PROVIDER),
        lambda swc: swc["ports"].append({"name": "POUT", "direction": "provide", "interface": "IExample"}),
    )


def _rules(model: Path) -> list[str]:
    return [record.rule for record in check_cross_layer_consistency(str(model)).records]


def _run_cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    """Run ``swcraft *argv``; the exit code (0 when it returned)."""
    monkeypatch.setattr("sys.argv", ["swcraft", *argv])
    try:
        main()
    except SystemExit as exited:
        return int(exited.code or 0)
    return 0


def _behaviours_without_the_element(directory: Path) -> list[str]:
    """Every ``SWC-INTERNAL-BEHAVIOR`` of every ``.arxml`` under *directory* that carries no element.

    Read with ``lxml`` from the written files, sharing no code with the writer.
    """
    missing: list[str] = []
    for arxml in sorted(directory.rglob("*.arxml"), key=lambda path: path.as_posix()):
        root = etree.parse(str(arxml)).getroot()
        for behaviour in root.iter(ar_qname("SWC-INTERNAL-BEHAVIOR")):
            if behaviour.find(ar_qname(TAG)) is None:
                missing.append(f"{arxml.name}:{behaviour.findtext(ar_qname('SHORT-NAME'))}")
    return missing


def _approve(proposal: Any) -> ApprovalDecision:
    return ApprovalDecision(
        change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=True
    )


def _change_set(project: Path, operations: list[dict[str, Any]], scope: list[dict[str, Any]] | None = None) -> dict:
    token = handle_request({"operation": "discover"}, project=str(project))["baseline"]
    return handle_request(
        {"operation": "change_set", "baseline": token, "intent": "termination handling", "operations": operations},
        project=str(project),
        review=_approve,
        approved_scope=scope,
    )


def _field_address(name: str) -> dict[str, Any]:
    return {"layer": "swc", "element": name, "path": [{"field": FIELD}]}


def _set_value(name: str, value: str = "no_support") -> dict[str, Any]:
    return {"operation": "update", "address": _field_address(name), "value": value}


# ---------------------------------------------------------------------------
# The rule, the predicate and the message
# ---------------------------------------------------------------------------


class TestTheRule:
    def test_declared_bound_and_answered_with_consistency_violation(self) -> None:
        (rule,) = [rule for rule in load_contract()["rules"] if rule["id"] == RULE_ID]
        assert rule["family"] == "cross-element"
        assert rule["appliesTo"] == ["field:swc.swc.external", f"field:swc.swc.{FIELD}"]
        assert rule["summary"] == CONSISTENCY_RULES[RULE]
        assert rule["summary"] == (
            "A component whose behaviour is emitted and that sets no `handle_termination_and_restart`, which "
            "AUTOSAR 4.2.2 and 4.3.0 require on every behaviour."
        )
        assert load_binding()["rules"][RULE_ID] == {
            "module": "swcraft_tools.validator.behaviour",
            "callable": "check_termination_and_restart",
        }
        assert RULE_CODES[RULE].code == "consistency-violation"

    def test_the_field_description_says_it_is_required(self) -> None:
        (layer,) = [layer for layer in load_contract()["layers"] if layer["id"] == "layer:swc"]
        (kind,) = [kind for kind in layer["kinds"] if kind["id"] == "kind:swc.swc"]
        (field,) = [field for field in kind["fields"] if field["name"] == FIELD]
        assert field["required"] is False
        assert (
            "Required by `check-model` and by the ARXML writer on every component that is not an external stub."
            in (field["description"])
        )
        assert "then nothing is emitted" not in field["description"]


def test_termination_rule_covers_every_written_version() -> None:
    """The rule reads no version because every version SWCraft writes requires the value.

    ``handleTerminationAndRestart`` is ``1 attr`` in SWCT R4.2.2 ``:22932`` and
    R4.3.0 ``:25469``. A third ``ArxmlVersion`` member fails here until someone
    reads that release's template and re-decides the rule (R22-11 gives ``0..1``
    beside ``[constr_1934]``).
    """
    assert {str(version) for version in ArxmlVersion} == {"4.2.2", "4.3.0"}


class TestThePredicate:
    def test_an_external_stub_is_skipped(self) -> None:
        stub = SWComponent.model_validate(
            {
                "name": "Stub",
                "external": True,
                "external_ref": "/Foreign/Stub",
                "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
            }
        )
        assert components_without_termination_handling([stub]) == []

    def test_every_component_setting_none_is_found_in_read_order(self) -> None:
        swcs = [SWComponent.model_validate({"name": name}) for name in ("Zeta", "Alpha")]
        assert [swc.name for swc in components_without_termination_handling(swcs)] == ["Zeta", "Alpha"]

    @pytest.mark.parametrize("value", VALUES)
    def test_each_value_is_accepted(self, value: str) -> None:
        swc = SWComponent.model_validate({"name": "C", FIELD: value})
        assert components_without_termination_handling([swc]) == []


def test_the_message_is_the_one_sentence() -> None:
    assert termination_handling_message("Comp") == (
        "SWC 'Comp' sets no handle_termination_and_restart: every behaviour SWCraft writes carries "
        "HANDLE-TERMINATION-AND-RESTART, which AUTOSAR 4.2.2 and 4.3.0 require (multiplicity 1). Set it to "
        "no_support, can_be_terminated or can_be_terminated_and_restarted."
    )


# ---------------------------------------------------------------------------
# check-model
# ---------------------------------------------------------------------------


class TestCheckModel:
    def test_the_check_is_the_composed_tuples_last_member(self) -> None:
        # Every check after it was appended later; it keeps its place after the name-space check.
        assert _composed_members()[-10:] == [
            "check_name_space_collisions",
            "check_termination_and_restart",
            "check_runnable_costs",
            "check_requirement_traceability",
            "check_inter_runnable_variables",
            "check_exclusive_areas",
            "check_data_events",
            "check_unknown_keys",
            "check_implicit_reads",
            "check_async_calls",
        ]

    def test_the_example_has_no_finding(self, tmp_path: Path) -> None:
        assert list(check_termination_and_restart(str(_basic(tmp_path) / "model")).records) == []

    def test_one_finding_per_component_on_its_field(self, tmp_path: Path) -> None:
        model = _without(tmp_path, PROVIDER, CONSUMER) / "model"

        records = check_termination_and_restart(str(model)).records

        assert [(record.rule, record.message) for record in records] == [
            (RULE, termination_handling_message(CONSUMER)),
            (RULE, termination_handling_message(PROVIDER)),
        ]
        assert records[0].subjects == (Subject("swc", CONSUMER, (field_step(FIELD),)),)

    def test_reported_last(self, tmp_path: Path) -> None:
        model = _without(tmp_path) / "model"
        _case_only_port(model)

        rules = _rules(model)

        assert rules[-2:] == ["name.name-space-collision", RULE]

    def test_the_cli_exits_1_and_prints_the_sentence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        project = _without(tmp_path)

        code = _run_cli(monkeypatch, "check-model", "--project", str(project / "swcraft.yaml"))

        assert code == 1
        captured = capsys.readouterr()
        assert termination_handling_message(CONSUMER) in captured.out + captured.err
        assert not (project / "dist").exists()


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


class TestTheWritersGuard:
    @pytest.mark.parametrize("version", VERSIONS)
    def test_refused_at_each_version_with_nothing_written(self, version: str, tmp_path: Path) -> None:
        model = _without(tmp_path) / "model"
        output = tmp_path / "out" / "model.arxml"

        with pytest.raises(ValueError) as caught:
            generate_arxml(str(model), str(output), version=version)

        assert str(caught.value) == f"{RULE_ID}: {termination_handling_message(CONSUMER)}"
        assert not output.parent.exists()

    def test_every_offending_component_is_named_in_read_order(self, tmp_path: Path) -> None:
        model = _without(tmp_path, PROVIDER, CONSUMER) / "model"

        with pytest.raises(ValueError) as caught:
            generate_arxml(str(model), str(tmp_path / "model.arxml"))

        assert str(caught.value) == (
            f"{RULE_ID}: {termination_handling_message(CONSUMER)}; {termination_handling_message(PROVIDER)}"
        )

    def test_the_guard_runs_last_after_emission_and_the_name_space_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The writer's check order: emission, connectors, containment, name spaces, then this."""
        model = _without(tmp_path) / "model"
        names = (
            "_emit_compositions",
            "_check_connectors",
            "_check_package_containment",
            "_check_name_space_collisions",
            "_check_termination_and_restart",
        )
        calls = record_calls(monkeypatch, arxml_generation, names)

        with pytest.raises(ValueError, match=RULE_ID):
            generate_arxml(str(model), str(tmp_path / "model.arxml"))

        assert calls == list(names)

    def test_a_broken_connector_is_reported_first(self, tmp_path: Path) -> None:
        """The connector's error alone from ``generate arxml``; both findings from ``check-model``."""
        model = _without(tmp_path) / "model"
        _break_connector(model)
        output = tmp_path / "out" / "model.arxml"

        with pytest.raises(ValueError) as caught:
            generate_arxml(str(model), str(output))

        assert RULE_ID not in str(caught.value)
        assert "Port not found: absent" in str(caught.value)
        assert not output.parent.exists()
        rules = _rules(model)
        assert "connector.endpoint-unknown-port" in rules
        assert rules[-1] == RULE
        assert rules.index("connector.endpoint-unknown-port") < rules.index(RULE)

    def test_a_name_space_group_is_reported_before_it(self, tmp_path: Path) -> None:
        model = _without(tmp_path) / "model"
        _case_only_port(model)

        with pytest.raises(ValueError) as caught:
            generate_arxml(str(model), str(tmp_path / "model.arxml"))

        assert str(caught.value).startswith("rule:name.name-space-collision: ")
        assert RULE_ID not in str(caught.value)

    def test_an_unsupported_version_is_refused_first(self, tmp_path: Path) -> None:
        model = _without(tmp_path) / "model"

        with pytest.raises(ValueError) as caught:
            generate_arxml(str(model), str(tmp_path / "model.arxml"), version="4.4.0")

        assert RULE_ID not in str(caught.value)
        assert "4.4.0" in str(caught.value)

    def test_the_emitter_still_writes_nothing_for_none(self) -> None:
        """The emitter writes nothing for ``None`` and raises nothing: the guard is the one refusal."""
        behavior = etree.Element(ar_qname("SWC-INTERNAL-BEHAVIOR"))

        _emit_handle_termination_and_restart(behavior, SWComponent.model_validate({"name": "C"}))

        assert len(behavior) == 0

    def test_the_cli_exits_1_with_nothing_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        project = _without(tmp_path)

        code = _run_cli(monkeypatch, "generate", "arxml", "--project", str(project / "swcraft.yaml"))

        assert code == 1
        assert termination_handling_message(CONSUMER) in capsys.readouterr().err
        assert not (project / "dist" / "model.arxml").exists()


# ---------------------------------------------------------------------------
# The component paths and validate
# ---------------------------------------------------------------------------


class TestTheComponentPaths:
    def test_the_component_export_is_a_closure_validation_error(self, tmp_path: Path) -> None:
        model = _without(tmp_path) / "model"
        output = tmp_path / "out" / f"{CONSUMER}.arxml"

        with pytest.raises(ClosureValidationError) as caught:
            export_component(str(model), CONSUMER, str(output))

        assert caught.value.issues == [termination_handling_message(CONSUMER)]
        assert not output.parent.exists()

    def test_a_declaring_component_still_exports(self, tmp_path: Path) -> None:
        model = _without(tmp_path) / "model"
        output = tmp_path / f"{PROVIDER}.arxml"

        assert export_component(str(model), PROVIDER, str(output)).ok
        assert _behaviours_without_the_element(tmp_path) == []

    def test_all_components_lists_it_as_failed_and_goes_on(self, tmp_path: Path) -> None:
        model = _without(tmp_path) / "model"
        output_dir = tmp_path / "components"

        reports = {report.swc: report for report in export_all_components(str(model), str(output_dir))}

        assert reports[PROVIDER].ok
        assert not reports[CONSUMER].ok
        assert termination_handling_message(CONSUMER) in reports[CONSUMER].render()
        assert sorted(path.name for path in output_dir.glob("*.arxml")) == [f"{PROVIDER}.arxml"]


class TestValidate:
    def test_validate_stops_at_check_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        project = _without(tmp_path)

        code = _run_cli(monkeypatch, "validate", "--project", str(project / "swcraft.yaml"))

        assert code == 1
        assert f"ERROR: {termination_handling_message(CONSUMER)}" in capsys.readouterr().err
        assert not (project / "dist").exists()


#: path -> the ``swcraft`` arguments after ``--project``, and the directory it writes into.
WRITING_PATHS: dict[str, tuple[tuple[str, ...], str]] = {
    "generate arxml 4.2.2": (("generate", "arxml", "--arxml-version", "4.2.2"), "dist"),
    "generate arxml 4.3.0": (("generate", "arxml", "--arxml-version", "4.3.0"), "dist"),
    "generate arxml --component": (("generate", "arxml", "--component", CONSUMER), "dist"),
    "generate arxml --all-components": (("generate", "arxml", "--all-components", "--output-dir", "{out}"), "out"),
}


@pytest.mark.parametrize("path", sorted(WRITING_PATHS))
def test_no_written_file_holds_a_behaviour_without_the_element(
    path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing value never reaches a written file, through any path.

    The output directory is scanned after each run -- every ``.arxml`` in it, every
    behaviour in each -- rather than the exit status alone. ``--all-components``
    writes the declaring component's file, which is the scan's non-vacuity.
    """
    project = _without(tmp_path)
    arguments, directory = WRITING_PATHS[path]
    out = tmp_path / directory if directory == "out" else project / directory
    arguments = tuple(argument.replace("{out}", str(out)) for argument in arguments)

    code = _run_cli(monkeypatch, *arguments[:2], "--project", str(project / "swcraft.yaml"), *arguments[2:])

    assert code == 1
    written = sorted(out.rglob("*.arxml"), key=lambda path: path.as_posix()) if out.exists() else []
    if out.exists():
        assert _behaviours_without_the_element(out) == []
    if path.endswith("--all-components"):
        assert [file.name for file in written] == [f"{PROVIDER}.arxml"]
    else:
        assert written == []


# ---------------------------------------------------------------------------
# The editing protocol
# ---------------------------------------------------------------------------


class TestPassD:
    def test_a_create_without_the_field_is_refused(self, tmp_path: Path) -> None:
        project = _basic(tmp_path)
        before = files_of(project / "model")

        response = _change_set(
            project,
            [
                {
                    "operation": "create",
                    "address": {"layer": "swc", "element": "ProbeSWC"},
                    "value": {"name": "ProbeSWC", "group": "example", "type": "application", "memory": {"RAM0": 512}},
                },
                {
                    # The requirement link lives in the component.
                    "operation": "create",
                    "address": {
                        "layer": "swc",
                        "element": "ProbeSWC",
                        "path": [{"field": "satisfies"}, {"member": "REQ_EXAMPLE_001@1"}],
                    },
                    "value": "REQ_EXAMPLE_001@1",
                },
            ],
        )

        assert response["response"] == "error", response
        assert response["error"] == "consistency-violation", response
        assert response["message"].startswith(termination_handling_message("ProbeSWC")), response
        assert response["addresses"] == [_field_address("ProbeSWC")]
        assert "additional" not in response
        assert files_of(project / "model") == before

    def test_the_same_create_with_the_field_commits(self, tmp_path: Path) -> None:
        project = _basic(tmp_path)

        response = _change_set(
            project,
            [
                {
                    "operation": "create",
                    "address": {"layer": "swc", "element": "ProbeSWC"},
                    "value": {
                        "name": "ProbeSWC",
                        "group": "example",
                        "type": "application",
                        "memory": {"RAM0": 512},
                        FIELD: "no_support",
                    },
                },
                {
                    # The requirement link lives in the component.
                    "operation": "create",
                    "address": {
                        "layer": "swc",
                        "element": "ProbeSWC",
                        "path": [{"field": "satisfies"}, {"member": "REQ_EXAMPLE_001@1"}],
                    },
                    "value": "REQ_EXAMPLE_001@1",
                },
            ],
        )

        assert response["response"] == "change_set", response

    def test_a_change_set_on_a_baseline_holding_offenders_names_every_one(self, tmp_path: Path) -> None:
        """A change set correct in itself is refused for a violation it did not introduce, naming all of them."""
        project = _without(tmp_path, PROVIDER, CONSUMER)
        before = files_of(project / "model")

        response = _change_set(
            project,
            [
                {
                    "operation": "update",
                    "address": {"layer": "requirements", "element": "REQ_EXAMPLE_001", "path": [{"field": "title"}]},
                    "value": "Retitled",
                }
            ],
        )

        assert response["response"] == "error", response
        assert response["error"] == "consistency-violation", response
        named = [response["addresses"], *(entry["addresses"] for entry in response.get("additional", []))]
        assert named == [[_field_address(CONSUMER)], [_field_address(PROVIDER)]], response
        assert files_of(project / "model") == before

    def test_one_change_set_repairs_every_component(self, tmp_path: Path) -> None:
        project = _without(tmp_path, PROVIDER, CONSUMER)

        response = _change_set(project, [_set_value(PROVIDER), _set_value(CONSUMER)])

        assert response["response"] == "change_set", response
        assert _rules(project / "model") == []


class TestUnderANarrowApprovedScope:
    """A scope covering some offending components only cannot repair the rest."""

    SCOPE = [{"operation": "update", "address": _field_address(PROVIDER)}]

    def test_the_covered_repair_is_refused_by_pass_d(self, tmp_path: Path) -> None:
        project = _without(tmp_path, PROVIDER, CONSUMER)
        before = files_of(project / "model")

        response = _change_set(project, [_set_value(PROVIDER)], scope=self.SCOPE)

        assert response["response"] == "error", response
        assert response["error"] == "consistency-violation", response
        assert response["addresses"] == [_field_address(CONSUMER)], response
        assert files_of(project / "model") == before

    def test_the_repair_of_both_is_refused_by_gate_1(self, tmp_path: Path) -> None:
        project = _without(tmp_path, PROVIDER, CONSUMER)
        before = files_of(project / "model")

        response = _change_set(project, [_set_value(PROVIDER), _set_value(CONSUMER)], scope=self.SCOPE)

        assert response["response"] == "error", response
        assert response["failure_class"] == "unauthorized", response
        assert response["addresses"] == [_field_address(CONSUMER)], response
        assert files_of(project / "model") == before

    def test_an_unscoped_session_repairs_both_in_one_change_set(self, tmp_path: Path) -> None:
        project = _without(tmp_path, PROVIDER, CONSUMER)

        response = _change_set(project, [_set_value(PROVIDER), _set_value(CONSUMER)], scope=None)

        assert response["response"] == "change_set", response
        assert response["status"] == "committed", response


# ---------------------------------------------------------------------------
# The imports do not refuse
# ---------------------------------------------------------------------------


class TestTheImportsDoNotRefuse:
    def test_import_model_of_a_source_without_the_element(self, tmp_path: Path) -> None:
        assert TAG not in MINIMAL_SWC.read_text(encoding="utf-8")
        project = init_project(tmp_path / "proj")

        run_import(str(MINIMAL_SWC), str(project / "swcraft.yaml"), force=True)

        written = yaml.safe_load(_swc_file(project / "model", "WheelSpeedSensorSwc").read_text(encoding="utf-8"))
        assert FIELD not in written
        assert [record.message for record in check_termination_and_restart(str(project / "model")).records] == [
            termination_handling_message("WheelSpeedSensorSwc")
        ]

    def test_import_component_of_an_export_without_the_element(self, tmp_path: Path) -> None:
        exported = tmp_path / "export.arxml"
        export_component(str(_basic(tmp_path) / "model"), CONSUMER, str(exported))
        tree = etree.parse(str(exported))
        for element in list(tree.getroot().iter(ar_qname(TAG))):
            element.getparent().remove(element)
        foreign = tmp_path / "foreign.arxml"
        foreign.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
        project = init_project(tmp_path / "proj")

        report = import_component(str(foreign), str(project / "model"))

        assert report.swc_action == "created"
        written = yaml.safe_load(_swc_file(project / "model", CONSUMER).read_text(encoding="utf-8"))
        assert FIELD not in written
        assert [record.message for record in check_termination_and_restart(str(project / "model")).records] == [
            termination_handling_message(CONSUMER)
        ]
