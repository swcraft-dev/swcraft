"""A runnable and a generated event share one ``SHORT-NAME`` name space.

``RUNNABLES`` and ``EVENTS`` are the XML containers of two aggregations of one
``SWC-INTERNAL-BEHAVIOR``, not ``Identifiable``s, so by ``[TPS_GST_00018]`` the
behaviour is the nearest ``Identifiable`` of both and ``[constr_2508]`` requires
their ``SHORT-NAME``s to be unique **case-insensitively** within it (R4.2.2
``GST:2606–2616`` / ``:2635–2636``, R4.3.0 ``GST:2682–2692`` / ``:2711–2712``, R22-11
``GST:2959–2969`` / ``:2981–2983``). Guards the defect of a runnable ``TMT_Foo`` beside the
timing event the writer names ``TMT_Foo`` passing ``check-model`` and being emitted.

``rule:name.runnable-event-collision`` (contract ``7.9.0``) is one predicate,
``validator.runnable_event_collisions``, read by five places, each held here on
the same six model shapes:

* ``check-model`` -- one finding per colliding runnable name, its subject the
  runnable;
* pass D -- the change set is refused as ``consistency-violation``;
* ``generate arxml`` -- refused before writing anything;
* ``export component`` -- :class:`ClosureValidationError`, not the writer's bare
  ``ValueError``;
* ``import component`` -- :class:`ReservedNameError`, beside the other names the
  writer refuses, with nothing written.

What this module does **not** reach: every other case-only pair (two runnables,
two ports, two events differing only in case), which the rule deliberately does
not judge -- a known exporter anomaly.
"""

from __future__ import annotations

import copy
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT
from lxml import etree
from project_support import files_of, init_project
from swcraft_tools.arxml_version import NAMESPACE
from swcraft_tools.component_exchange import (
    ClosureValidationError,
    ReservedNameError,
    export_component,
    import_component,
)
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.reader import read_swc_model
from swcraft_tools.validator import (
    CONSISTENCY_RULES,
    check_cross_layer_consistency,
    check_name_uniqueness,
    runnable_event_collisions,
)
from swcraft_tools.writer.arxml_generation import generate_arxml

RULE = "name.runnable-event-collision"
BASIC = REPO_ROOT / "examples" / "basic"
PROVIDER = "swc/example/ExampleSWC.yaml"

_NS = f"{{{NAMESPACE}}}"


# ---------------------------------------------------------------------------
# The six shapes, each a mutation of `examples/basic`'s ExampleSWC
# ---------------------------------------------------------------------------


def _runnable(name: str, *events: dict[str, Any]) -> dict[str, Any]:
    runnable: dict[str, Any] = {"name": name}
    if events:
        runnable["events"] = list(events)
    return runnable


_TIMING = {"type": "timing", "period": 0.01}


def _timing_pair(swc: dict[str, Any]) -> None:
    swc["runnables"].append(_runnable("TMT_ExampleRun"))


def _init_pair(swc: dict[str, Any]) -> None:
    swc["runnables"].append(_runnable("Boot", {"type": "init"}))
    swc["runnables"].append(_runnable("IT_Boot"))


def _operation_pair(swc: dict[str, Any]) -> None:
    swc["ports"].append({"name": "pDiag", "direction": "provide", "interface": "IDiagnosticService"})
    swc["runnables"].append(
        _runnable("OIT_pDiag_ReadDtc", {"type": "operation", "port": "pDiag", "operation": "ReadDtc"})
    )


def _case_only_pair(swc: dict[str, Any]) -> None:
    swc["runnables"].append(_runnable("tmt_examplerun"))


def _counter_pair(swc: dict[str, Any]) -> None:
    # Two timing events on ExampleRun: the retroactive counter emits
    # `TMT_ExampleRun_0` and `TMT_ExampleRun_1`, and `TMT_ExampleRun` is free.
    swc["runnables"][0]["events"].append({"type": "timing", "period": 0.02})
    swc["runnables"].append(_runnable("TMT_ExampleRun_0"))


def _no_event(swc: dict[str, Any]) -> None:
    swc["runnables"].append(_runnable("Other"))
    swc["runnables"].append(_runnable("TMT_Other"))


#: shape -> (mutation, the colliding runnable or None, the event phrase the finding names)
SHAPES: dict[str, tuple[Callable[[dict[str, Any]], None], str | None, str]] = {
    "timing": (
        _timing_pair,
        "TMT_ExampleRun",
        "TIMING-EVENT 'TMT_ExampleRun' the writer generates for runnable 'ExampleRun'",
    ),
    "init": (_init_pair, "IT_Boot", "INIT-EVENT 'IT_Boot' the writer generates for runnable 'Boot'"),
    "operation": (
        _operation_pair,
        "OIT_pDiag_ReadDtc",
        "OPERATION-INVOKED-EVENT 'OIT_pDiag_ReadDtc' the writer generates for port 'pDiag' operation 'ReadDtc' "
        "(on runnable 'OIT_pDiag_ReadDtc')",
    ),
    "case-only": (
        _case_only_pair,
        "tmt_examplerun",
        "TIMING-EVENT 'TMT_ExampleRun' the writer generates for runnable 'ExampleRun'",
    ),
    "counter": (
        _counter_pair,
        "TMT_ExampleRun_0",
        "TIMING-EVENT 'TMT_ExampleRun_0' the writer generates for runnable 'ExampleRun'",
    ),
    "no-event": (_no_event, None, ""),
}
COLLIDING = [name for name, (_, runnable, _) in SHAPES.items() if runnable is not None]


def _project(tmp_path: Path, shape: str | None = None) -> Path:
    project = tmp_path / "project"
    shutil.copytree(BASIC, project, ignore=shutil.ignore_patterns("dist", "views"))
    if shape is not None:
        _mutate(project / "model", SHAPES[shape][0])
    return project


def _mutate(model_root: Path, mutation: Callable[[dict[str, Any]], None]) -> None:
    path = model_root / PROVIDER
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutation(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _findings(model_root: Path) -> list[Any]:
    return [record for record in check_name_uniqueness(str(model_root)).records if record.rule == RULE]


# ---------------------------------------------------------------------------
# check-model
# ---------------------------------------------------------------------------


class TestTheRule:
    def test_the_rule_is_declared(self) -> None:
        assert RULE in CONSISTENCY_RULES

    def test_the_unmodified_example_has_no_finding(self, tmp_path: Path) -> None:
        assert _findings(_project(tmp_path) / "model") == []

    @pytest.mark.parametrize("shape", COLLIDING)
    def test_one_finding_naming_the_runnable_and_the_event(self, shape: str, tmp_path: Path) -> None:
        _, runnable, event = SHAPES[shape]
        (finding,) = _findings(_project(tmp_path, shape) / "model")

        assert f"SWC 'ExampleSWC' runnable '{runnable}'" in finding.message
        assert event in finding.message
        assert f"rename runnable '{runnable}'" in finding.message
        assert finding.subjects == (Subject("swc", "ExampleSWC").at(field_step("runnables"), member_step(runnable)),)

    def test_two_runnables_with_no_event_have_no_finding(self, tmp_path: Path) -> None:
        assert _findings(_project(tmp_path, "no-event") / "model") == []

    def test_the_counter_frees_the_base_name(self, tmp_path: Path) -> None:
        """Two events on one runnable emit ``_0`` and ``_1``: the bare base is emitted by nothing."""
        project = _project(tmp_path)
        _mutate(
            project / "model",
            lambda swc: (
                swc["runnables"][0]["events"].append({"type": "timing", "period": 0.02}),
                swc["runnables"].append(_runnable("TMT_ExampleRun")),
            ),
        )

        assert _findings(project / "model") == []

    def test_a_runnable_colliding_with_two_events_is_one_finding(self, tmp_path: Path) -> None:
        """``TMT_Foo`` and ``TMT_foo`` are two emitted events; ``tmt_foo`` collides with both, one finding."""
        project = _project(tmp_path)
        _mutate(
            project / "model",
            lambda swc: swc["runnables"].extend(
                [_runnable("Foo", _TIMING), _runnable("foo", _TIMING), _runnable("tmt_foo")]
            ),
        )

        (finding,) = _findings(project / "model")
        assert "'TMT_Foo'" in finding.message
        assert "'TMT_foo'" in finding.message

    def test_a_colliding_name_declared_twice_is_one_finding(self, tmp_path: Path) -> None:
        """The duplicate is ``rule:name.duplicate-runnable``'s; this rule prints its sentence once."""
        project = _project(tmp_path)
        _mutate(
            project / "model",
            lambda swc: swc["runnables"].extend([_runnable("TMT_ExampleRun"), _runnable("TMT_ExampleRun")]),
        )

        (finding,) = _findings(project / "model")
        assert "runnable 'TMT_ExampleRun'" in finding.message
        rules = [record.rule for record in check_name_uniqueness(str(project / "model")).records]
        assert "name.duplicate-runnable" in rules

    def test_an_external_stub_is_not_judged(self, tmp_path: Path) -> None:
        """A stub emits no behaviour; its runnables are ``rule:swc.external-with-ports``' finding alone."""
        project = _project(tmp_path)
        stub = {
            "name": "StubSwc",
            "type": "application",
            "external": True,
            "external_ref": "/Foreign/StubSwc",
            "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
            "runnables": [_runnable("Run", _TIMING), _runnable("TMT_Run")],
        }
        path = project / "model" / "swc" / "external" / "StubSwc.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump(stub, sort_keys=False), encoding="utf-8", newline="\n")
        (swc,) = [swc for swc in read_swc_model(str(project / "model")) if swc.name == "StubSwc"]

        assert runnable_event_collisions(swc) == []
        assert _findings(project / "model") == []
        stub_rules = [
            record.rule
            for record in check_cross_layer_consistency(str(project / "model")).records
            if "StubSwc" in record.message
        ]
        assert "swc.external-with-ports" in stub_rules
        assert RULE not in stub_rules

    def test_the_predicate_is_the_findings_source(self, tmp_path: Path) -> None:
        project = _project(tmp_path, "timing")
        (swc,) = [swc for swc in read_swc_model(str(project / "model")) if swc.name == "ExampleSWC"]

        (collision,) = runnable_event_collisions(swc)
        (finding,) = _findings(project / "model")
        assert collision.runnable == "TMT_ExampleRun"
        assert collision.message == finding.message

    def test_the_finding_reaches_check_model(self, tmp_path: Path) -> None:
        project = _project(tmp_path, "timing")

        assert RULE in [record.rule for record in check_cross_layer_consistency(str(project / "model")).records]


# ---------------------------------------------------------------------------
# The four twins
# ---------------------------------------------------------------------------


def _approve(proposal: Any) -> ApprovalDecision:
    return ApprovalDecision(
        change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=True
    )


class TestPassD:
    def test_a_change_set_creating_the_pair_is_refused(self, tmp_path: Path) -> None:
        project = _project(tmp_path)
        before = files_of(project / "model")
        token = handle_request({"operation": "discover"}, project=str(project))["baseline"]

        response = handle_request(
            {
                "operation": "change_set",
                "baseline": token,
                "intent": "a runnable named like a generated event",
                "operations": [
                    {
                        "operation": "create",
                        "address": {
                            "layer": "swc",
                            "element": "ExampleSWC",
                            "path": [{"field": "runnables"}, {"member": "TMT_ExampleRun"}],
                        },
                        "value": {"name": "TMT_ExampleRun"},
                    }
                ],
            },
            project=str(project),
            review=_approve,
        )

        assert response["response"] == "error", response
        assert response["error"] == "consistency-violation", response
        # The protocol names no rule id: the finding's own sentence, and the runnable as the
        # address responsible.
        assert "runnable 'TMT_ExampleRun' shares its SHORT-NAME" in response["message"]
        assert "'member': 'TMT_ExampleRun'" in str(response)
        assert files_of(project / "model") == before


class TestGenerateArxml:
    @pytest.mark.parametrize("shape", COLLIDING)
    def test_refused_before_writing(self, shape: str, tmp_path: Path) -> None:
        project = _project(tmp_path, shape)
        output = tmp_path / "out" / "model.arxml"
        (finding,) = _findings(project / "model")

        with pytest.raises(ValueError) as caught:
            generate_arxml(str(project / "model"), str(output))

        assert finding.message in str(caught.value)
        assert not output.exists()

    def test_the_no_event_shape_generates(self, tmp_path: Path) -> None:
        project = _project(tmp_path, "no-event")
        output = tmp_path / "model.arxml"

        generate_arxml(str(project / "model"), str(output))

        assert output.is_file()


class TestExportComponent:
    @pytest.mark.parametrize("shape", COLLIDING)
    def test_refused_as_a_closure_validation_error(self, shape: str, tmp_path: Path) -> None:
        project = _project(tmp_path, shape)
        output = tmp_path / "ExampleSWC.arxml"
        (finding,) = _findings(project / "model")

        with pytest.raises(ClosureValidationError) as caught:
            export_component(str(project / "model"), "ExampleSWC", str(output))

        assert finding.message in caught.value.issues
        assert not output.exists()


def _foreign_export(tmp_path: Path, runnable: str) -> Path:
    """ExampleSWC's export with one more runnable, *runnable*, beside ExampleRun's timing event.

    The timing event is renamed in the file to *runnable* with its case swapped --
    a foreign tool's own name, which the file holds apart from the runnable's,
    so the reader drops nothing. The importer states an event's differing source
    name, but not this one: it equals the new runnable's ignoring case (``[constr_2508]``), so
    the settling pass declines it with a descriptive line and the event keeps
    the name the writer derives, ``TMT_ExampleRun``. The new runnable is a copy
    of ``ExampleRun``'s, its ``SHORT-NAME`` and ``SYMBOL`` renamed, so nothing
    in it is unread.
    """
    source = _project(tmp_path / "source")
    exported = tmp_path / "exported.arxml"
    export_component(str(source / "model"), "ExampleSWC", str(exported))
    tree = etree.parse(str(exported))
    (event,) = tree.getroot().iter(f"{_NS}TIMING-EVENT")
    event.find(f"{_NS}SHORT-NAME").text = runnable.swapcase()
    (original,) = tree.getroot().iter(f"{_NS}RUNNABLE-ENTITY")
    added = copy.deepcopy(original)
    added.find(f"{_NS}SHORT-NAME").text = runnable
    added.find(f"{_NS}SYMBOL").text = runnable
    original.addnext(added)
    foreign = tmp_path / "foreign.arxml"
    foreign.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    return foreign


class TestImportComponent:
    @pytest.mark.parametrize("runnable", ["TMT_ExampleRun", "tmt_examplerun"])
    def test_refused_beside_the_reserved_names(self, runnable: str, tmp_path: Path) -> None:
        foreign = _foreign_export(tmp_path, runnable)
        target = init_project(tmp_path / "target")
        before = files_of(target)

        with pytest.raises(ReservedNameError) as caught:
            import_component(str(foreign), str(target / "model"))

        assert f"runnable '{runnable}'" in str(caught.value)
        assert "TIMING-EVENT 'TMT_ExampleRun'" in str(caught.value)
        assert files_of(target) == before

    def test_the_same_export_without_the_pair_imports(self, tmp_path: Path) -> None:
        foreign = _foreign_export(tmp_path, "Other")
        target = init_project(tmp_path / "target")

        report = import_component(str(foreign), str(target / "model"))

        assert report.swc_action == "created"
