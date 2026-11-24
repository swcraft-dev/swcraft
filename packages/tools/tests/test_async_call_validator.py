"""``check-model`` over asynchronous calls and ``call_returned`` events (contract ``12.7.0``).

Over a copy of ``fixtures/access_forms/model``: the consistent fixture, one negative case per rule (each
exactly one finding), the version-aware rule through ``check-model``, pass D and the writer, the widened
kept identifier rules, and pass D's codes.

What this module does **not** reach: the ARXML the writer emits for the forms
(``test_access_forms_arxml.py``), the importer, component exchange, and a wait point, which SWCraft does
not model.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT, _write_manifest
from external_stubs_support import change
from swcraft_tools.cli import main
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.address import Address, FieldStep, MemberStep
from swcraft_tools.editing.apply import StagedModel
from swcraft_tools.editing.diagnosis import (
    BROKEN_REFERENCE,
    CONSISTENCY_VIOLATION,
    RULE_CODES,
    emitted_identifier_violations,
)
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.resolve import REFERENCE_POOLS
from swcraft_tools.editing.staging import baseline_token
from swcraft_tools.reader import read_swc_model
from swcraft_tools.validator import (
    GENERATED_RESULT_POINT_PREFIX,
    ConsistencyIssue,
    async_call_records,
    check_async_calls,
    check_cross_layer_consistency,
    emitted_result_point_names,
    result_point_entries,
)
from swcraft_tools.writer.arxml_generation import generate_arxml

FIXTURE = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "access_forms" / "model"
CONTROLLER = "BrakeController"
CONTROLLER_FILE = "swc/chassis/BrakeController.yaml"
COMPUTE = "ComputeBrakeTorque"
ON_WRITTEN = "OnBlockWritten"
DIAGNOSE = "Diagnose"

MIXED = "swc.call.mixed-synchrony"
TIMEOUT = "swc.call.timeout-mismatch"
WITHOUT = "swc.call.async-without-result"
SEVERAL = "swc.call.several-results"
UNKNOWN = "swc.call-returned-event.unknown-call"
AMBIGUOUS = "swc.call-returned-event.ambiguous-call"
INIT = "swc.call.init-runnable-result"


@pytest.fixture
def model(tmp_path: Path) -> Path:
    root = tmp_path / "model"
    shutil.copytree(FIXTURE, root)
    return root


def _runnable(document: dict[str, Any], name: str) -> dict[str, Any]:
    (runnable,) = [entry for entry in document["runnables"] if entry["name"] == name]
    return runnable


def _call(document: dict[str, Any], runnable: str, port: str, operation: str) -> dict[str, Any]:
    (call,) = [
        entry
        for entry in _runnable(document, runnable)["calls"]
        if (entry["port"], entry["operation"]) == (port, operation)
    ]
    return call


def _controller(model_root: Path, mutate: Any) -> None:
    change(model_root, CONTROLLER_FILE, mutate)


def _add_call(model_root: Path, runnable: str, call: dict[str, Any]) -> None:
    _controller(model_root, lambda d: _runnable(d, runnable).setdefault("calls", []).append(call))


def _add_runnable(
    model_root: Path, name: str, *events: dict[str, Any], calls: list[dict[str, Any]] | None = None
) -> None:
    runnable: dict[str, Any] = {"name": name, "events": list(events)}
    if calls:
        runnable["calls"] = calls
    _controller(model_root, lambda d: d["runnables"].append(runnable))


def _drop_the_event(model_root: Path) -> None:
    """``OnBlockWritten`` started by a timing event instead of its ``call_returned`` event; a task still maps it."""
    _controller(model_root, lambda d: _runnable(d, ON_WRITTEN).update(events=[{"type": "timing", "period": 0.1}]))


def _findings(model_root: Path, version: str | None = None) -> list[ConsistencyIssue]:
    result = check_cross_layer_consistency(str(model_root), version)
    return [*result.records, *result.warnings]


def _only(model_root: Path, rule: str, version: str | None = None) -> ConsistencyIssue:
    findings = _findings(model_root, version)
    assert [finding.rule for finding in findings] == [rule], [(f.rule, f.message) for f in findings]
    return findings[0]


def _owner() -> Subject:
    return Subject("swc", CONTROLLER)


def _call_at(runnable: str, port: str, operation: str, *steps: Any) -> Subject:
    return _owner().at(
        field_step("runnables"),
        member_step(runnable),
        field_step("calls"),
        member_step({"port": port, "operation": operation}),
        *steps,
    )


def _event_at(runnable: str, value: dict[str, Any], *steps: Any) -> Subject:
    return _owner().at(field_step("runnables"), member_step(runnable), field_step("events"), member_step(value), *steps)


def _returned(port: str, operation: str, **fields: Any) -> dict[str, Any]:
    return {"type": "call_returned", "port": port, "operation": operation, **fields}


class TestThePositiveFixture:
    @pytest.mark.parametrize("version", [None, "4.2.2", "4.3.0"])
    def test_the_fixture_is_consistent_at_both_versions(self, model: Path, version: str | None) -> None:
        assert _findings(model, version) == []

    def test_the_record_function_answers_nothing_over_the_fixture(self, model: Path) -> None:
        assert async_call_records(read_swc_model(str(model)), "4.2.2") == []

    def test_the_disk_entry_point_is_the_record_function(self, model: Path) -> None:
        _controller(model, lambda d: _call(d, COMPUTE, "rCalibration", "ReadBlock").pop("poll"))

        assert [issue.rule for issue in check_async_calls(str(model)).records] == [WITHOUT]
        assert not check_async_calls(str(model), "4.3.0").records


class TestMixedSynchrony:
    def test_one_pair_called_both_ways_is_one_finding_naming_every_entry(self, model: Path) -> None:
        _add_call(model, DIAGNOSE, {"port": "rNvStorage", "operation": "WriteBlock"})

        finding = _only(model, MIXED)

        assert finding.subjects == (
            _call_at(COMPUTE, "rNvStorage", "WriteBlock"),
            _call_at(DIAGNOSE, "rNvStorage", "WriteBlock"),
        )
        assert "asynchronously from runnable 'ComputeBrakeTorque'" in finding.message
        assert "synchronously from runnable 'Diagnose'" in finding.message


class TestTimeoutMismatch:
    def test_two_stated_timeouts_differ(self, model: Path) -> None:
        _add_call(
            model,
            DIAGNOSE,
            {"port": "rCalibration", "operation": "ReadBlock", "asynchronous": True, "poll": True, "timeout": 0.2},
        )

        finding = _only(model, TIMEOUT)

        assert finding.subjects == (
            _call_at(COMPUTE, "rCalibration", "ReadBlock", field_step("timeout")),
            _call_at(DIAGNOSE, "rCalibration", "ReadBlock", field_step("timeout")),
        )
        assert "'ComputeBrakeTorque' 0.5, 'Diagnose' 0.2" in finding.message

    def test_an_absent_timeout_is_a_value_of_its_own(self, model: Path) -> None:
        _add_call(
            model, DIAGNOSE, {"port": "rCalibration", "operation": "ReadBlock", "asynchronous": True, "poll": True}
        )

        assert "'Diagnose' no timeout" in _only(model, TIMEOUT).message

    def test_one_timeout_on_every_call_is_silent(self, model: Path) -> None:
        _add_call(
            model,
            DIAGNOSE,
            {"port": "rCalibration", "operation": "ReadBlock", "asynchronous": True, "poll": True, "timeout": 0.5},
        )

        assert _findings(model) == []


class TestSeveralResults:
    def test_a_poll_and_an_event_naming_its_pair(self, model: Path) -> None:
        _add_runnable(model, "OnCalibrationRead", _returned("rCalibration", "ReadBlock"))

        finding = _only(model, SEVERAL)

        assert finding.subjects == (
            _call_at(COMPUTE, "rCalibration", "ReadBlock"),
            _event_at("OnCalibrationRead", _returned("rCalibration", "ReadBlock")),
        )
        assert "its own poll in runnable 'ComputeBrakeTorque'" in finding.message

    def test_two_events_naming_one_pair(self, model: Path) -> None:
        _add_runnable(model, "OnBlockWrittenToo", _returned("rNvStorage", "WriteBlock"))

        finding = _only(model, SEVERAL)

        assert finding.subjects[0] == _call_at(COMPUTE, "rNvStorage", "WriteBlock")
        assert len(finding.subjects) == 3

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_at_either_version(self, model: Path, version: str) -> None:
        _add_runnable(model, "OnBlockWrittenToo", _returned("rNvStorage", "WriteBlock"))

        _only(model, SEVERAL, version)


class TestAsyncWithoutResult:
    """``[constr_2006]`` judged per edition: R4.2.2 exactly one result point, R4.3.0 at most one."""

    def _without_result(self, model: Path) -> None:
        _controller(model, lambda d: _call(d, COMPUTE, "rCalibration", "ReadBlock").pop("poll"))

    @pytest.mark.parametrize("version", [None, "4.2.2"])
    def test_refused_in_a_4_2_2_project(self, model: Path, version: str | None) -> None:
        self._without_result(model)

        finding = _only(model, WITHOUT, version)

        assert finding.subjects == (_call_at(COMPUTE, "rCalibration", "ReadBlock"),)
        assert "AUTOSAR 4.2.2" in finding.message

    def test_accepted_in_a_4_3_0_project(self, model: Path) -> None:
        self._without_result(model)

        assert _findings(model, "4.3.0") == []

    def test_an_event_removed_leaves_its_call_without_a_result(self, model: Path) -> None:
        _drop_the_event(model)

        assert _only(model, WITHOUT).subjects == (_call_at(COMPUTE, "rNvStorage", "WriteBlock"),)


class TestUnknownCall:
    def test_an_event_naming_a_synchronous_pair(self, model: Path) -> None:
        event = _returned("rCalibration", "GetVersion")
        _add_runnable(model, "OnVersion", event)

        finding = _only(model, UNKNOWN)

        assert finding.subjects == (_event_at("OnVersion", event, field_step("operation")),)
        assert finding.target == f"{CONTROLLER}.rCalibration.GetVersion"

    def test_an_event_naming_an_undeclared_port_is_judged_by_its_call_alone(self, model: Path) -> None:
        _add_runnable(model, "OnNothing", _returned("rNope", "Nothing"))

        _only(model, UNKNOWN)

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_at_either_version(self, model: Path, version: str) -> None:
        _add_runnable(model, "OnVersion", _returned("rCalibration", "GetVersion"))

        _only(model, UNKNOWN, version)


class TestAmbiguousCall:
    def test_two_asynchronous_callers_of_an_event_s_pair(self, model: Path) -> None:
        _add_call(model, DIAGNOSE, {"port": "rNvStorage", "operation": "WriteBlock", "asynchronous": True})

        finding = _only(model, AMBIGUOUS)

        assert finding.subjects == (_event_at(ON_WRITTEN, _returned("rNvStorage", "WriteBlock")),)
        assert "runnables 'ComputeBrakeTorque' and 'Diagnose'" in finding.message

    def test_two_asynchronous_callers_with_no_event_need_a_result_each(self, model: Path) -> None:
        _drop_the_event(model)
        _add_call(model, DIAGNOSE, {"port": "rNvStorage", "operation": "WriteBlock", "asynchronous": True})

        assert [finding.rule for finding in _findings(model)] == [WITHOUT, WITHOUT]
        assert _findings(model, "4.3.0") == []


class TestInitRunnableResult:
    def test_an_event_on_a_runnable_an_init_event_starts(self, model: Path) -> None:
        _controller(model, lambda d: _runnable(d, ON_WRITTEN)["events"].append({"type": "init"}))

        finding = _only(model, INIT)

        assert finding.subjects == (_event_at(ON_WRITTEN, _returned("rNvStorage", "WriteBlock")),)

    def test_a_poll_on_a_runnable_an_init_event_starts(self, model: Path) -> None:
        _add_runnable(
            model,
            "InitCalibration",
            {"type": "init"},
            calls=[
                {"port": "rCalibration", "operation": "ReadBlock", "asynchronous": True, "poll": True, "timeout": 0.5}
            ],
        )

        finding = _only(model, INIT)

        assert finding.subjects == (_call_at("InitCalibration", "rCalibration", "ReadBlock", field_step("poll")),)

    def test_an_asynchronous_call_without_a_result_point_is_allowed(self, model: Path) -> None:
        _add_runnable(
            model,
            "InitStore",
            {"type": "init"},
            calls=[{"port": "rNvStorage", "operation": "WriteBlock", "asynchronous": True}],
        )
        _drop_the_event(model)

        # Two asynchronous callers and no event: no init finding, only `[constr_2006]` at 4.2.2, once per call.
        assert [finding.rule for finding in _findings(model)] == [WITHOUT, WITHOUT]
        assert _findings(model, "4.3.0") == []


# The version through each of the three: check-model, pass D, the writer


def _project(tmp_path: Path, version: str | None) -> Path:
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project / "model")
    _write_manifest(project)
    if version is not None:
        manifest = project / "swcraft.yaml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + f'arxml_version: "{version}"\n', encoding="utf-8", newline="\n"
        )
    _controller(project / "model", lambda d: _call(d, COMPUTE, "rCalibration", "ReadBlock").pop("poll"))
    return project


class TestCheckModelReadsTheManifest:
    @pytest.mark.parametrize(("version", "code"), [(None, 1), ("4.2.2", 1), ("4.3.0", 0)])
    def test_the_manifest_s_version_decides(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        version: str | None,
        code: int,
    ) -> None:
        project = _project(tmp_path, version)
        monkeypatch.setattr("sys.argv", ["swcraft", "check-model", "--project", str(project / "swcraft.yaml")])

        try:
            main()
            exited = 0
        except SystemExit as exit_:
            exited = exit_.code or 0

        captured = capsys.readouterr()
        assert exited == code
        assert ("nothing fetches its result" in captured.out + captured.err) == (code == 1)


class TestTheWriterReadsTheVersionItGenerates:
    def test_4_2_2_refuses_by_the_rule_and_writes_nothing(self, tmp_path: Path) -> None:
        project = _project(tmp_path, None)
        output = tmp_path / "out" / "model.arxml"

        with pytest.raises(ValueError, match=rf"rule:{WITHOUT}:"):
            generate_arxml(str(project / "model"), str(output), version="4.2.2")

        assert not output.exists()

    def test_4_3_0_writes_the_call_without_a_result_point(self, tmp_path: Path) -> None:
        # The call point's content is `test_access_forms_arxml.py`'s.
        project = _project(tmp_path, None)
        output = tmp_path / "out" / "model.arxml"

        generate_arxml(str(project / "model"), str(output), version="4.3.0")

        text = output.read_text(encoding="utf-8")
        assert "<SHORT-NAME>Call_rCalibration_ReadBlock</SHORT-NAME>" in text
        assert "Result_rCalibration_ReadBlock" not in text


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


def _call_address(runnable: str, port: str, operation: str, *steps: dict[str, Any]) -> dict[str, Any]:
    return {
        "layer": "swc",
        "element": CONTROLLER,
        "path": [
            {"field": "runnables"},
            {"member": runnable},
            {"field": "calls"},
            {"member": {"port": port, "operation": operation}},
            *steps,
        ],
    }


def _event_create(runnable: str, event: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation": "create",
        "address": {
            "layer": "swc",
            "element": CONTROLLER,
            "path": [{"field": "runnables"}, {"member": runnable}, {"field": "events"}, {"member": event}],
        },
        "value": event,
    }


def _set_poll(value: bool) -> dict[str, Any]:
    return {
        "operation": "update",
        "address": _call_address(COMPUTE, "rNvStorage", "WriteBlock", {"field": "poll"}),
        "value": value,
    }


def _protocol_project(tmp_path: Path, version: str | None) -> Path:
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project / "model")
    _write_manifest(project)
    if version is not None:
        manifest = project / "swcraft.yaml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + f'arxml_version: "{version}"\n', encoding="utf-8", newline="\n"
        )
    return project


def _get_version_asynchronous() -> dict[str, Any]:
    """``Diagnose``'s call of ``rCalibration.GetVersion`` made asynchronous: its one caller, with no result point."""
    return {
        "operation": "update",
        "address": _call_address(DIAGNOSE, "rCalibration", "GetVersion", {"field": "asynchronous"}),
        "value": True,
    }


class TestPassD:
    @pytest.mark.parametrize("rule", [MIXED, TIMEOUT, WITHOUT, SEVERAL, AMBIGUOUS, INIT])
    def test_each_error_is_a_consistency_violation(self, rule: str) -> None:
        assert RULE_CODES[rule].code == CONSISTENCY_VIOLATION

    def test_unknown_call_is_an_unsuggested_broken_reference(self) -> None:
        assert RULE_CODES[UNKNOWN].code == BROKEN_REFERENCE
        assert RULE_CODES[UNKNOWN].group not in REFERENCE_POOLS

    @pytest.mark.parametrize(("version", "refused"), [(None, True), ("4.2.2", True), ("4.3.0", False)])
    def test_the_manifest_s_version_decides(self, tmp_path: Path, version: str | None, refused: bool) -> None:
        project = _protocol_project(tmp_path, version)

        response = _dispatch(project, _get_version_asynchronous())

        if refused:
            assert response["error"] == "consistency-violation", response
            assert WITHOUT not in response["message"] and "AUTOSAR 4.2.2" in response["message"]
            assert response["addresses"][0]["path"][-1] == {
                "member": {"port": "rCalibration", "operation": "GetVersion"}
            }
        else:
            assert "error" not in response, response

    def test_a_poll_beside_an_event_is_refused(self, tmp_path: Path) -> None:
        response = _dispatch(_protocol_project(tmp_path, None), _set_poll(True))

        assert response["error"] == "consistency-violation", response
        assert "result points" in response["message"]

    def test_an_event_naming_no_asynchronous_call_carries_no_candidate(self, tmp_path: Path) -> None:
        response = _dispatch(
            _protocol_project(tmp_path, None), _event_create(DIAGNOSE, _returned("rCalibration", "GetVersion"))
        )

        assert response["error"] == "broken-reference", response
        assert response["addresses"][0]["path"][-1] == {"field": "operation"}
        assert "nearest" not in response["message"].lower()

    def test_an_init_runnable_s_event_is_refused(self, tmp_path: Path) -> None:
        response = _dispatch(_protocol_project(tmp_path, None), _event_create(ON_WRITTEN, {"type": "init"}))

        assert response["error"] == "consistency-violation", response
        assert "init event" in response["message"]

    def test_an_illegal_stated_result_is_an_invalid_identifier(self, tmp_path: Path) -> None:
        response = _dispatch(
            _protocol_project(tmp_path, None),
            {
                "operation": "update",
                "address": _call_address(COMPUTE, "rCalibration", "ReadBlock", {"field": "result"}),
                "value": "not a name",
            },
        )

        assert response["error"] == "invalid-identifier", response

    def test_an_over_budget_derived_result_point_is_an_invalid_identifier(self, tmp_path: Path) -> None:
        project = _protocol_project(tmp_path, None)
        port = _long_port_for_result()

        response = _dispatch(
            project,
            {
                "operation": "create",
                "address": {"layer": "swc", "element": CONTROLLER, "path": [{"field": "ports"}, {"member": port}]},
                "value": {"name": port, "direction": "require", "interface": "CalibrationIf"},
            },
            {
                "operation": "create",
                "address": _call_address(DIAGNOSE, port, "ReadBlock"),
                "value": {"port": port, "operation": "ReadBlock", "asynchronous": True, "poll": True},
            },
        )

        assert response["error"] == "invalid-identifier", response
        assert f"'{port}'.'ReadBlock'" in response["message"]


# The widened kept rules


def _long_port_for_result() -> str:
    """A port name whose ``Result_{port}_ReadBlock`` is 129 characters and whose ``Call_`` form is 127."""
    length = 129 - len(GENERATED_RESULT_POINT_PREFIX) - 1 - len("ReadBlock")
    return "r" + "x" * (length - 1)


class TestTheResultPointNames:
    def test_the_entries_and_their_names(self, model: Path) -> None:
        (controller,) = [swc for swc in read_swc_model(str(model)) if swc.name == CONTROLLER]
        compute = next(runnable for runnable in controller.runnables if runnable.name == COMPUTE)
        on_written = next(runnable for runnable in controller.runnables if runnable.name == ON_WRITTEN)

        assert result_point_entries(compute) == [("poll", "rCalibration", "ReadBlock", None)]
        assert emitted_result_point_names(compute) == ["Result_rCalibration_ReadBlock"]
        assert emitted_result_point_names(on_written) == ["Result_rNvStorage_WriteBlock"]

    def test_a_stated_result_is_written_as_it_is(self, model: Path) -> None:
        _controller(model, lambda d: _call(d, COMPUTE, "rCalibration", "ReadBlock").update(result="CalibResult"))
        (controller,) = [swc for swc in read_swc_model(str(model)) if swc.name == CONTROLLER]
        compute = next(runnable for runnable in controller.runnables if runnable.name == COMPUTE)

        assert emitted_result_point_names(compute) == ["CalibResult"]
        assert _findings(model) == []

    def test_an_illegal_stated_poll_result(self, model: Path) -> None:
        _controller(model, lambda d: _call(d, COMPUTE, "rCalibration", "ReadBlock").update(result="not a name"))

        finding = _only(model, "identifier.declared")

        assert finding.subjects == (_call_at(COMPUTE, "rCalibration", "ReadBlock", field_step("result")),)

    def test_an_illegal_stated_event_result(self, model: Path) -> None:
        _controller(model, lambda d: _runnable(d, ON_WRITTEN)["events"][0].update(result="not a name"))

        finding = _only(model, "identifier.declared")

        assert finding.subjects == (_event_at(ON_WRITTEN, _returned("rNvStorage", "WriteBlock", result="not a name")),)

    def test_an_over_budget_poll_result_point(self, model: Path) -> None:
        port = _long_port_for_result()
        _controller(
            model, lambda d: d["ports"].append({"name": port, "direction": "require", "interface": "CalibrationIf"})
        )
        _add_call(model, DIAGNOSE, {"port": port, "operation": "ReadBlock", "asynchronous": True, "poll": True})

        finding = _only(model, "identifier.generated")

        assert finding.subjects == (_call_at(DIAGNOSE, port, "ReadBlock"),)
        assert f"{GENERATED_RESULT_POINT_PREFIX}{port}_ReadBlock" in finding.message

    def test_a_stated_result_takes_the_form_out(self, model: Path) -> None:
        port = _long_port_for_result()
        _controller(
            model, lambda d: d["ports"].append({"name": port, "direction": "require", "interface": "CalibrationIf"})
        )
        _add_call(
            model,
            DIAGNOSE,
            {"port": port, "operation": "ReadBlock", "asynchronous": True, "poll": True, "result": "LongResult"},
        )

        assert _findings(model) == []

    def test_a_stated_result_equal_to_a_call_point(self, model: Path) -> None:
        _controller(
            model, lambda d: _call(d, COMPUTE, "rCalibration", "ReadBlock").update(result="Call_rNvStorage_WriteBlock")
        )

        finding = _only(model, "swc.access.name-collision")

        assert "the polled result point of calls port 'rCalibration' operation 'ReadBlock'" in finding.message

    def test_a_stated_result_equal_to_a_call_point_ignoring_case(self, model: Path) -> None:
        _controller(
            model, lambda d: _call(d, COMPUTE, "rCalibration", "ReadBlock").update(result="call_rNvStorage_WriteBlock")
        )

        finding = _only(model, "name.name-space-collision")

        assert _call_at(COMPUTE, "rCalibration", "ReadBlock", field_step("result")) in finding.subjects

    def test_an_event_result_equal_to_an_access_point_of_its_runnable(self, model: Path) -> None:
        _controller(
            model,
            lambda d: (
                _runnable(d, ON_WRITTEN)["events"][0].update(result="R_rWheelSpeed_speed"),
                _runnable(d, ON_WRITTEN).update(reads=[{"port": "rWheelSpeed", "element": "speed"}]),
            ),
        )

        finding = _only(model, "swc.access.name-collision")

        assert (
            "the result point of the call_returned event on port 'rNvStorage' operation 'WriteBlock'" in finding.message
        )

    def test_a_stated_event_name_equal_to_a_runnable(self, model: Path) -> None:
        _controller(model, lambda d: _runnable(d, ON_WRITTEN)["events"][0].update(name=DIAGNOSE))

        assert _only(model, "name.runnable-event-collision")


class TestTheMaterializationWalk:
    """``emitted_identifier_violations``: the pre-materialization path ``check-model``'s identifier rules mirror."""

    @staticmethod
    def _staged(runnable: dict[str, Any]) -> StagedModel:
        swc: dict[str, Any] = {"name": CONTROLLER, "runnables": [runnable]}
        return StagedModel(registry=reg.REGISTRY, elements={"swc": [swc]})

    def test_an_over_budget_poll(self) -> None:
        port = _long_port_for_result()
        call = {"port": port, "operation": "ReadBlock", "asynchronous": True, "poll": True}

        (violation,) = emitted_identifier_violations(self._staged({"name": DIAGNOSE, "calls": [call]}))

        assert violation.error == "invalid-identifier"
        assert violation.addresses == (
            Address(
                "swc",
                CONTROLLER,
                (
                    FieldStep("runnables"),
                    MemberStep(DIAGNOSE),
                    FieldStep("calls"),
                    MemberStep({"port": port, "operation": "ReadBlock"}),
                ),
            ),
        )

    def test_an_over_budget_event_result_point(self) -> None:
        port = _long_port_for_result()
        event = _returned(port, "ReadBlock", name="OnRead")

        (violation,) = emitted_identifier_violations(self._staged({"name": ON_WRITTEN, "events": [event]}))

        assert violation.error == "invalid-identifier"
        assert "result point" in violation.message

    def test_an_illegal_stated_poll_result(self) -> None:
        call = {"port": "rCalibration", "operation": "ReadBlock", "asynchronous": True, "poll": True, "result": "a b"}

        (violation,) = emitted_identifier_violations(self._staged({"name": COMPUTE, "calls": [call]}))

        assert violation.error == "invalid-identifier"
        assert violation.addresses[0].path[-1] == FieldStep("result")

    def test_an_illegal_stated_event_result(self) -> None:
        event = _returned("rNvStorage", "WriteBlock", result="a b")

        (violation,) = emitted_identifier_violations(self._staged({"name": ON_WRITTEN, "events": [event]}))

        assert violation.error == "invalid-identifier"

    def test_a_legal_poll_and_event_mint_nothing(self) -> None:
        call = {"port": "rCalibration", "operation": "ReadBlock", "asynchronous": True, "poll": True}
        runnable = {"name": COMPUTE, "calls": [call], "events": [_returned("rNvStorage", "WriteBlock")]}

        assert emitted_identifier_violations(self._staged(runnable)) == ()


# Every caller that reads the manifest's version passes it

# `_project`'s polled call lost its `poll`, so a caller that dropped the manifest's version would
# refuse a valid 4.3.0 model with every other test green. A cheap outcome is asserted; otherwise the
# composer is observed in its own module's namespace.

#: ``(manifest version, the version the caller must hand the composer, whether the rule fires)``: a
#: manifest stating none resolves to 4.2.2, so a caller that handed ``None`` would fail the equality too.
_CALLER_VERSIONS = [("4.3.0", "4.3.0", False), (None, "4.2.2", True)]


class _Stop(Exception):
    """Raised by an observer once the call it watches has been recorded."""


def _observe(
    monkeypatch: pytest.MonkeyPatch, module: Any, name: str, *, stop: bool = True
) -> list[tuple[Any, list[str]]]:
    """Wrap *module*'s *name* (a composer or `all_findings`); each call's version and the rules it answered."""
    real = getattr(module, name)
    seen: list[tuple[Any, list[str]]] = []

    def observing(root: str, arxml_version: Any = None) -> Any:
        answer = real(root, arxml_version)
        findings = answer[0] if isinstance(answer, tuple) else [*answer.records, *answer.warnings]
        seen.append((arxml_version, [finding.rule for finding in findings]))
        if stop:
            raise _Stop
        return answer

    monkeypatch.setattr(module, name, observing)
    return seen


def _manifest_project(tmp_path: Path, version: str | None) -> Any:
    from swcraft_tools.project import require_project

    return require_project(str(_project(tmp_path, version) / "swcraft.yaml"))


def _assert_judged(seen: list[tuple[Any, list[str]]], expected: str, fires: bool) -> None:
    assert seen, "the caller never reached the composer"
    for version, rules in seen:
        assert version == expected
        assert (WITHOUT in rules) is fires


class TestEveryVersionReadingCallerPassesTheManifestsVersion:
    @pytest.mark.parametrize(("version", "code"), [("4.3.0", 0), (None, 1)])
    def test_cli_validate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], version, code
    ) -> None:
        project = _project(tmp_path, version)
        monkeypatch.setattr("sys.argv", ["swcraft", "validate", "--project", str(project / "swcraft.yaml")])

        try:
            main()
            exited = 0
        except SystemExit as exit_:
            exited = exit_.code or 0

        captured = capsys.readouterr()
        assert exited == code, captured.out + captured.err
        assert ("nothing fetches its result" in captured.out + captured.err) == (code == 1)

    @pytest.mark.parametrize(("version", "expected", "fires"), _CALLER_VERSIONS)
    def test_the_allocation_s_model_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version, expected, fires
    ) -> None:
        from swcraft_tools import allocation

        seen = _observe(monkeypatch, allocation, "check_cross_layer_consistency")

        with pytest.raises(_Stop):
            allocation.run_allocation(_manifest_project(tmp_path, version), None)

        _assert_judged(seen, expected, fires)

    def test_the_allocation_s_move_sets_are_judged_at_the_manifest_s_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``allocation.py``'s ``_Checker`` is handed ``run_allocation``'s version through ``solve_curve``."""
        from swcraft_tools import allocation

        handed: list[Any] = []

        class Recording(allocation._Checker):
            def __init__(self, model_root: Any, projection: Any, arxml_version: Any = None) -> None:
                handed.append(arxml_version)
                raise _Stop

        monkeypatch.setattr(allocation, "_Checker", Recording)

        with pytest.raises(_Stop):
            allocation.run_allocation(_manifest_project(tmp_path, "4.3.0"), None)

        assert handed == ["4.3.0"]

    def test_the_allocation_refuses_the_model_at_the_default(self, tmp_path: Path) -> None:
        from swcraft_tools import allocation

        with pytest.raises(allocation.ModelRefusedError):
            allocation.run_allocation(_manifest_project(tmp_path, None), None)

    @pytest.mark.parametrize(("version", "expected", "fires"), _CALLER_VERSIONS)
    def test_the_trace_search(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version, expected, fires) -> None:
        from swcraft_tools import trace_search

        seen = _observe(monkeypatch, trace_search, "check_cross_layer_consistency")

        with pytest.raises(_Stop):
            trace_search.run_trace(_manifest_project(tmp_path, version), None)

        _assert_judged(seen, expected, fires)

    @pytest.mark.parametrize(("version", "expected", "fires"), _CALLER_VERSIONS)
    def test_the_trace_candidates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version, expected, fires
    ) -> None:
        from swcraft_tools.editing import trace_candidates

        project = _project(tmp_path, version)
        requirement = project / "model" / "requirements" / "REQ_BRAKE.yaml"
        requirement.parent.mkdir(parents=True, exist_ok=True)
        requirement.write_text(
            "id: REQ_BRAKE\ntitle: Brake\ndescription: The brake.\nversion: 1\nsource: architecture\n",
            encoding="utf-8",
            newline="\n",
        )
        seen = _observe(monkeypatch, trace_candidates, "check_cross_layer_consistency", stop=False)

        handle_request({"operation": "find", "for_requirement": "REQ_BRAKE"}, project=project)

        _assert_judged(seen, expected, fires)

    @pytest.mark.parametrize(("version", "expected", "fires"), _CALLER_VERSIONS)
    def test_the_change_report(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version, expected, fires) -> None:
        from scratch_git import git as _git
        from swcraft_tools import change_report

        project = _project(tmp_path, version)
        # The suite's helper states a committer: a CI runner has no git identity of its own.
        for argv in (("init", "-q"), ("add", "-A"), ("commit", "-q", "-m", "base")):
            _git(*argv, cwd=project)
        seen = _observe(monkeypatch, change_report, "all_findings", stop=False)

        change_report.compute_change_report(_manifest_project_at(project), "HEAD", diagrams=False)

        assert len(seen) == 2
        _assert_judged(seen, expected, fires)

    @pytest.mark.parametrize(("version", "expected", "fires"), _CALLER_VERSIONS)
    def test_the_metrics(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version, expected, fires) -> None:
        from swcraft_tools import metrics

        seen = _observe(monkeypatch, metrics, "all_findings", stop=False)

        metrics.compute_metrics(_manifest_project(tmp_path, version), [])

        _assert_judged(seen, expected, fires)


def _manifest_project_at(project: Path) -> Any:
    from swcraft_tools.project import require_project

    return require_project(str(project / "swcraft.yaml"))


# `import component`'s identifier guard judges the result points


def _controller_of(model_root: Path) -> Any:
    (controller,) = [swc for swc in read_swc_model(str(model_root)) if swc.name == CONTROLLER]
    return controller


class TestTheComponentImportGuardJudgesTheResultPoints:
    """``illegal_identifiers`` asks every result point what ``check_identifier_conformance`` asks it.

    Guards a withdrawn result name being written and ``check-model`` then refusing the component.
    """

    def _long_poll(self, model: Path, **call: Any) -> str:
        port = _long_port_for_result()
        _controller(
            model, lambda d: d["ports"].append({"name": port, "direction": "require", "interface": "CalibrationIf"})
        )
        _add_call(model, DIAGNOSE, {"port": port, "operation": "ReadBlock", "asynchronous": True, "poll": True, **call})
        return port

    def test_an_over_budget_derived_result_point_is_a_violation(self, model: Path) -> None:
        from swcraft_tools.component_exchange.name_policies import illegal_identifiers

        port = self._long_poll(model)

        (violation,) = illegal_identifiers(_controller_of(model), [], [])

        assert f"'{GENERATED_RESULT_POINT_PREFIX}{port}_ReadBlock' (129 characters" in violation
        assert "result point" in violation
        assert "get 120 characters between them" in violation

    def test_a_stated_result_takes_the_form_out(self, model: Path) -> None:
        from swcraft_tools.component_exchange.name_policies import illegal_identifiers

        self._long_poll(model, result="LongResult")

        assert illegal_identifiers(_controller_of(model), [], []) == []

    def test_an_illegal_stated_event_result_is_a_violation(self, model: Path) -> None:
        from swcraft_tools.component_exchange.name_policies import illegal_identifiers

        _controller(model, lambda d: _runnable(d, ON_WRITTEN)["events"][0].update(result="not a name"))

        (violation,) = illegal_identifiers(_controller_of(model), [], [])

        assert "call-returned event 'rNvStorage'.'WriteBlock' result point stated name 'not a name'" in violation

    def test_the_fixture_s_result_points_are_legal(self, model: Path) -> None:
        from swcraft_tools.component_exchange.name_policies import illegal_identifiers

        assert illegal_identifiers(_controller_of(model), [], []) == []
