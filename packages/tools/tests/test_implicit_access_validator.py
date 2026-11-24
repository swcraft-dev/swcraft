"""``check-model`` over implicit reads and writes (contract ``12.7.0``).

What is asserted, over a copy of the shared fixture ``fixtures/access_forms/model``:

* **the positive fixture** -- implicit reads and writes of standard elements, on
  ports with no com-spec -- is consistent;
* **one negative case per rule**, each giving **exactly** its findings over the
  whole of ``check_cross_layer_consistency``: an implicit read of a queued
  element (``[constr_2020]``) and an implicit read of an element its port's
  com-spec states ``enable_update: true`` for (``[constr_1103]``);
* **the silence cases**: an implicit write of a queued element (no rule of
  either edition forbids it), an explicit read of an element whose com-spec
  states ``enable_update``, a read the ``swc.access.*`` rules already report;
* **pass D**: each finding a ``consistency-violation`` through the protocol.

What this module does **not** reach: the ARXML the writer emits for the forms
and the importer. The direction and interface-kind checks of
an implicit access are the ``swc.access.*`` rules', unchanged and tested there.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT, _write_manifest
from external_stubs_support import change
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.editing.diagnosis import CONSISTENCY_VIOLATION, RULE_CODES
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.staging import baseline_token
from swcraft_tools.reader import read_interfaces, read_swc_model
from swcraft_tools.validator import (
    ConsistencyIssue,
    check_cross_layer_consistency,
    check_implicit_reads,
    implicit_read_records,
)
from swcraft_tools.writer.arxml_generation import generate_arxml

FIXTURE = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "access_forms" / "model"
CONTROLLER = "BrakeController"
CONTROLLER_FILE = "swc/chassis/BrakeController.yaml"
COMPUTE = "ComputeBrakeTorque"
DIAGNOSE = "Diagnose"
QUEUED = "swc.access.queued-implicit-read"
ENABLE_UPDATE = "swc.access.implicit-read-enable-update"


@pytest.fixture
def model(tmp_path: Path) -> Path:
    root = tmp_path / "model"
    shutil.copytree(FIXTURE, root)
    return root


def _findings(model_root: Path) -> list[ConsistencyIssue]:
    result = check_cross_layer_consistency(str(model_root))
    return [*result.records, *result.warnings]


def _rules(model_root: Path) -> list[str]:
    return [finding.rule for finding in _findings(model_root)]


def _queue_the_wheel_speed(model_root: Path) -> None:
    """``WheelSpeedIf.speed`` queued: ``ComputeBrakeTorque`` and ``Diagnose`` both read it implicitly."""
    change(model_root, "interfaces/WheelSpeedIf.yaml", lambda d: d["data_elements"][0].update(sw_impl_policy="queued"))


def _enable_update_on_the_wheel_speed(model_root: Path) -> None:
    def mutate(document: dict[str, Any]) -> None:
        (port,) = [port for port in document["ports"] if port["name"] == "rWheelSpeed"]
        port["com_specs"] = [{"data_element": "speed", "enable_update": True}]

    change(model_root, CONTROLLER_FILE, mutate)


def _read_at(runnable: str, port: str, element: str, *steps: Any) -> Subject:
    return Subject("swc", CONTROLLER).at(
        field_step("runnables"),
        member_step(runnable),
        field_step("reads"),
        member_step({"port": port, "element": element}),
        *steps,
    )


class TestThePositiveFixture:
    def test_the_fixture_is_consistent(self, model: Path) -> None:
        assert _findings(model) == []

    def test_the_record_function_answers_nothing(self, model: Path) -> None:
        assert implicit_read_records(read_swc_model(str(model)), read_interfaces(str(model))) == []


class TestQueuedImplicitRead:
    def test_each_implicit_read_of_a_queued_element_is_one_finding(self, model: Path) -> None:
        _queue_the_wheel_speed(model)

        findings = [finding for finding in _findings(model) if finding.rule == QUEUED]

        assert [finding.subjects[0] for finding in findings] == [
            _read_at(COMPUTE, "rWheelSpeed", "speed", field_step("by")),
            _read_at(DIAGNOSE, "rWheelSpeed", "speed", field_step("by")),
        ]
        assert findings[0].subjects[1] == Subject("interfaces", "WheelSpeedIf").at(
            field_step("data_elements"), member_step("speed"), field_step("sw_impl_policy")
        )
        assert "[constr_2020]" in findings[0].message

    def test_beside_the_queued_receiver_s_own_rule_nothing_else(self, model: Path) -> None:
        _queue_the_wheel_speed(model)

        # The receiver's missing queue length is the com-spec rule's, unchanged by the form.
        assert _rules(model) == ["swc.port.queued-receiver-without-queue-length", QUEUED, QUEUED]

    def test_an_explicit_read_of_a_queued_element_is_not_this_rule(self, model: Path) -> None:
        _queue_the_wheel_speed(model)
        change(
            model,
            CONTROLLER_FILE,
            lambda d: [read.pop("by", None) for runnable in d["runnables"] for read in runnable.get("reads", [])],
        )

        assert QUEUED not in _rules(model)

    def test_an_implicit_write_of_a_queued_element_is_silent(self, model: Path) -> None:
        # `WheelSensor.SampleSpeed` writes `speed` implicitly; with every read made explicit, nothing judges it.
        _queue_the_wheel_speed(model)
        change(
            model,
            CONTROLLER_FILE,
            lambda d: [read.pop("by", None) for runnable in d["runnables"] for read in runnable.get("reads", [])],
        )

        assert not [rule for rule in _rules(model) if rule.startswith("swc.access.")]

    def test_a_read_through_an_undeclared_port_is_the_access_rules(self, model: Path) -> None:
        change(
            model,
            CONTROLLER_FILE,
            lambda d: d["runnables"][0]["reads"].append({"port": "rNope", "element": "speed", "by": "implicit"}),
        )

        assert QUEUED not in _rules(model) and ENABLE_UPDATE not in _rules(model)


class TestEnableUpdate:
    def test_each_implicit_read_of_such_an_element_is_one_finding(self, model: Path) -> None:
        _enable_update_on_the_wheel_speed(model)

        findings = _findings(model)

        assert [finding.rule for finding in findings] == [ENABLE_UPDATE, ENABLE_UPDATE]
        assert findings[0].subjects == (
            _read_at(COMPUTE, "rWheelSpeed", "speed", field_step("by")),
            Subject("swc", CONTROLLER).at(
                field_step("ports"),
                member_step("rWheelSpeed"),
                field_step("com_specs"),
                member_step("speed"),
                field_step("enable_update"),
            ),
        )
        assert "[constr_1103]" in findings[0].message

    def test_an_explicit_read_is_silent(self, model: Path) -> None:
        _enable_update_on_the_wheel_speed(model)
        change(
            model,
            CONTROLLER_FILE,
            lambda d: [read.pop("by", None) for runnable in d["runnables"] for read in runnable.get("reads", [])],
        )

        assert _findings(model) == []

    def test_enable_update_false_is_silent(self, model: Path) -> None:
        def mutate(document: dict[str, Any]) -> None:
            (port,) = [port for port in document["ports"] if port["name"] == "rWheelSpeed"]
            port["com_specs"] = [{"data_element": "speed", "enable_update": False}]

        change(model, CONTROLLER_FILE, mutate)

        assert _findings(model) == []

    def test_the_disk_entry_point_is_the_record_function(self, model: Path) -> None:
        _enable_update_on_the_wheel_speed(model)

        assert [issue.rule for issue in check_implicit_reads(str(model)).records] == [ENABLE_UPDATE, ENABLE_UPDATE]


class TestTheWriterRefusesWhatCheckModelRefuses:
    def test_an_implicit_read_with_enable_update_writes_nothing(self, model: Path, tmp_path: Path) -> None:
        _enable_update_on_the_wheel_speed(model)
        output = tmp_path / "out" / "model.arxml"

        with pytest.raises(ValueError, match=rf"rule:{ENABLE_UPDATE}:"):
            generate_arxml(str(model), str(output))

        assert not output.exists()


def _dispatch(project_root: Path, *operations: dict[str, Any]) -> dict[str, Any]:
    return handle_request(
        {
            "operation": "change_set",
            "baseline": baseline_token(project_root / "model"),
            "intent": "a test change set",
            "operations": list(operations),
        },
        project=project_root,
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    shutil.copytree(FIXTURE, root / "model")
    _write_manifest(root)
    return root


class TestPassD:
    @pytest.mark.parametrize("rule", [QUEUED, ENABLE_UPDATE])
    def test_each_error_is_a_consistency_violation(self, rule: str) -> None:
        assert RULE_CODES[rule].code == CONSISTENCY_VIOLATION

    def test_a_queued_element_read_implicitly_is_refused(self, project: Path) -> None:
        response = _dispatch(
            project,
            {
                "operation": "update",
                "address": {
                    "layer": "interfaces",
                    "element": "WheelSpeedIf",
                    "path": [{"field": "data_elements"}, {"member": "speed"}, {"field": "sw_impl_policy"}],
                },
                "value": "queued",
            },
        )

        assert response["error"] == "consistency-violation", response
        assert "implicitly" in response["message"]

    def test_an_enable_update_under_an_implicit_read_is_refused(self, project: Path) -> None:
        response = _dispatch(
            project,
            {
                "operation": "create",
                "address": {
                    "layer": "swc",
                    "element": CONTROLLER,
                    "path": [
                        {"field": "ports"},
                        {"member": "rWheelSpeed"},
                        {"field": "com_specs"},
                        {"member": "speed"},
                    ],
                },
                "value": {"data_element": "speed", "enable_update": True},
            },
        )

        assert response["error"] == "consistency-violation", response
        assert "enable_update" in response["message"]
        assert {"field": "by"} in [address["path"][-1] for address in response["addresses"]]

    def test_making_the_read_implicit_is_refused_by_the_same_rule(self, project: Path) -> None:
        change(
            project / "model",
            CONTROLLER_FILE,
            lambda d: (
                [read.pop("by", None) for runnable in d["runnables"] for read in runnable.get("reads", [])] and None
            ),
        )
        _enable_update_on_the_wheel_speed(project / "model")

        response = _dispatch(
            project,
            {
                "operation": "update",
                "address": {
                    "layer": "swc",
                    "element": CONTROLLER,
                    "path": [
                        {"field": "runnables"},
                        {"member": COMPUTE},
                        {"field": "reads"},
                        {"member": {"port": "rWheelSpeed", "element": "speed"}},
                        {"field": "by"},
                    ],
                },
                "value": "implicit",
            },
        )

        assert response["error"] == "consistency-violation", response
