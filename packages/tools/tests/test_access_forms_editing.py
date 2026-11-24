"""Implicit access and asynchronous calls through both write paths (contract ``12.7.0``).

Over a copy of ``fixtures/access_forms/model``: the write path's name guard for a result point (a
stated ``result`` judged as a name and a derived ``Result_{port}_{operation}`` against the pair's joint
budget of 120, as ``check-model``'s ``identifier.*`` rules do, and a stated one equal to a call point's
name refused as a collision; a refusal leaves the file as it was); ``add_port`` and
``add_runnable`` keeping every form; the editing protocol's acts: a ``call_returned`` event created and
removed at its whole-value address (a lone remove refused at 4.2.2 with nothing written, committed at
4.3.0), a member or field ``update`` of it refused as ``immutable-field``, a field ``update`` of a
read's or a write's ``by`` and of a call's ``asynchronous``, ``poll``, ``result`` and ``timeout``
committed, and an event left dangling by removing its call refused as ``broken-reference``; an empty
safety verdict for each act while the ordinary scope review applies; and the trace link search's
content fingerprint moving with each new field.

What this module does **not** reach: the ARXML the writer emits for the forms, the importer, component
exchange; pass D's codes per rule (``test_async_call_validator.py``,
``test_implicit_access_validator.py``); the safety verdict is shown on one rated shape, not derived for
every model.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT, _write_manifest
from external_stubs_support import change, put
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.reader import read_swc_model
from swcraft_tools.schema import Runnable, SWComponent
from swcraft_tools.trace_search import content_fingerprint
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import mutations
from swcraft_tools.writer.mutations import ReservedNameError, check_runnable_access_names

FIXTURE = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "access_forms" / "model"
CONTROLLER = "BrakeController"
CONTROLLER_FILE = "swc/chassis/BrakeController.yaml"
PEDAL = "BrakePedal"
PEDAL_FILE = "swc/chassis/BrakePedal.yaml"
COMPUTE = "ComputeBrakeTorque"
ON_WRITTEN = "OnBlockWritten"
DIAGNOSE = "Diagnose"

#: ``OnBlockWritten``'s event, as the fixture states it.
RETURNED: dict[str, Any] = {"type": "call_returned", "port": "rNvStorage", "operation": "WriteBlock"}
#: The same event stating its result point's and its own name.
RETURNED_NAMED: dict[str, Any] = {**RETURNED, "result": "Result_Stored", "name": "ASRT_Stored"}


def _load(model_root: Path, relative: str = CONTROLLER_FILE) -> dict[str, Any]:
    return yaml.safe_load((model_root / relative).read_text(encoding="utf-8"))


def _runnables(model_root: Path, relative: str = CONTROLLER_FILE) -> dict[str, dict[str, Any]]:
    return {runnable["name"]: runnable for runnable in _load(model_root, relative)["runnables"]}


def _entry(entries: list[dict[str, Any]], **identity: str) -> dict[str, Any]:
    (entry,) = [entry for entry in entries if all(entry[key] == value for key, value in identity.items())]
    return entry


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


def _findings(model_root: Path, version: str | None = None) -> list[Any]:
    result = check_cross_layer_consistency(str(model_root), version)
    return [*result.records, *result.warnings]


@pytest.fixture
def model(tmp_path: Path) -> Path:
    root = tmp_path / "model"
    shutil.copytree(FIXTURE, root)
    return root


# The write path's name guard for a result point


def _polled(**fields: Any) -> dict[str, Any]:
    return {"port": "rCalibration", "operation": "ReadBlock", "asynchronous": True, "poll": True, **fields}


#: A port and an operation whose `Call_` name fits (128) and whose `Result_` name does not (130).
LONG_PORT, LONG_OPERATION = "r" + "P" * 59, "O" * 62


class TestTheGuardJudgesAResultPoint:
    def test_a_stated_poll_result_that_is_no_identifier_is_refused_and_nothing_is_written(self, model: Path) -> None:
        before = _tree(model)

        with pytest.raises(ValueError, match=r"result point name.*'1Calib'"):
            mutations.add_runnable(str(model), CONTROLLER, "Poller", calls=[_polled(result="1Calib")])

        assert _tree(model) == before

    def test_an_over_budget_derived_poll_result_is_refused_naming_both_names(self, model: Path) -> None:
        before = _tree(model)

        with pytest.raises(ValueError) as refused:
            mutations.add_runnable(
                str(model),
                CONTROLLER,
                "Poller",
                calls=[{"port": LONG_PORT, "operation": LONG_OPERATION, "asynchronous": True, "poll": True}],
            )

        message = str(refused.value)
        assert f"Result_{LONG_PORT}_{LONG_OPERATION}" in message and "(130 characters)" in message
        assert "120 characters between them" in message and "state the call's result" in message
        assert _tree(model) == before

    def test_a_stated_result_takes_the_over_budget_form_out(self, model: Path) -> None:
        mutations.add_runnable(
            str(model),
            CONTROLLER,
            "Poller",
            calls=[
                {"port": LONG_PORT, "operation": LONG_OPERATION, "asynchronous": True, "poll": True, "result": "Done"}
            ],
        )

        (call,) = _runnables(model)["Poller"]["calls"]
        assert call["result"] == "Done"

    def test_a_stated_result_equal_to_a_call_point_name_is_a_collision(self, model: Path) -> None:
        before = _tree(model)

        with pytest.raises(ReservedNameError, match=r"'Call_rCalibration_ReadBlock'"):
            mutations.add_runnable(
                str(model), CONTROLLER, "Poller", calls=[_polled(result="Call_rCalibration_ReadBlock")]
            )

        assert _tree(model) == before

    def test_a_stated_event_result_that_is_no_identifier_is_refused(self) -> None:
        runnable = Runnable.model_validate({"name": "R", "events": [{**RETURNED, "result": "9Done"}]})

        with pytest.raises(ValueError, match=r"call-returned event.*result point name.*'9Done'"):
            check_runnable_access_names(CONTROLLER, runnable)

    def test_an_over_budget_derived_event_result_is_refused(self) -> None:
        event = {"type": "call_returned", "port": LONG_PORT, "operation": LONG_OPERATION}
        runnable = Runnable.model_validate({"name": "R", "events": [event]})

        with pytest.raises(ValueError, match=r"state the event's result"):
            check_runnable_access_names(CONTROLLER, runnable)

    def test_the_fixture_s_runnables_pass(self, model: Path) -> None:
        (swc,) = (swc for swc in read_swc_model(str(model)) if swc.name == CONTROLLER)

        for runnable in swc.runnables:
            check_runnable_access_names(CONTROLLER, runnable)


# add_port and add_runnable on a component using the new forms


class TestTheMutationsKeepTheFixture:
    @pytest.mark.parametrize(
        "call",
        [
            lambda root: mutations.add_port(root, CONTROLLER, "rSecondPedal", "require", "PedalIf"),
            lambda root: mutations.add_runnable(
                root, CONTROLLER, "Other", calls=[_polled(port="rCalibration", operation="GetVersion", poll=False)]
            ),
        ],
        ids=["add_port", "add_runnable"],
    )
    def test_every_form_is_kept(self, model: Path, call: Any) -> None:
        before = _runnables(model)

        call(str(model))

        after = _runnables(model)
        for name, runnable in before.items():
            assert after[name] == runnable

    def test_add_runnable_writes_the_new_call_fields_in_contract_order(self, model: Path) -> None:
        mutations.add_runnable(
            str(model),
            CONTROLLER,
            "Other",
            reads=[{"port": "rPedal", "element": "position", "by": "implicit"}],
            writes=[{"port": "pBrakeTorque", "element": "torque", "by": "implicit"}],
            calls=[_polled(operation="GetVersion", result="Version", timeout=0.25)],
        )

        written = _runnables(model)["Other"]
        assert written["reads"] == [{"port": "rPedal", "element": "position", "by": "implicit"}]
        assert written["writes"] == [{"port": "pBrakeTorque", "element": "torque", "by": "implicit"}]
        (call,) = written["calls"]
        assert list(call) == ["port", "operation", "asynchronous", "poll", "result", "timeout"]


# The editing protocol


def _project(tmp_path: Path, version: str | None = None) -> Path:
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project / "model")
    _write_manifest(project)
    if version is not None:
        manifest = project / "swcraft.yaml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + f'arxml_version: "{version}"\n', encoding="utf-8", newline="\n"
        )
    return project


class _Review:
    """A host that approves the delta, DECLINES any safety question, and records what it was shown."""

    def __init__(self) -> None:
        self.proposals: list[Any] = []

    def __call__(self, proposal: Any) -> ApprovalDecision:
        self.proposals.append(proposal)
        return ApprovalDecision(
            change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=False
        )


def _change_set(
    project: Path,
    operations: list[dict[str, Any]],
    review: _Review | None = None,
    approved_scope: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    token = handle_request({"operation": "discover"}, project=str(project))["baseline"]
    return handle_request(
        {"operation": "change_set", "baseline": token, "intent": "access forms edit", "operations": operations},
        project=str(project),
        review=review or _Review(),
        approved_scope=approved_scope,
        scope_id=None if approved_scope is None else "turn-1",
    )


def _address(element: str, runnable: str, *steps: dict[str, Any]) -> dict[str, Any]:
    return {
        "layer": "swc",
        "element": element,
        "path": [{"field": "runnables"}, {"member": runnable}, *steps],
    }


def _event_address(runnable: str, event: dict[str, Any]) -> dict[str, Any]:
    return _address(CONTROLLER, runnable, {"field": "events"}, {"member": event})


def _create_event(runnable: str, event: dict[str, Any]) -> dict[str, Any]:
    return {"operation": "create", "address": _event_address(runnable, event), "value": event}


def _remove_event(runnable: str, event: dict[str, Any]) -> dict[str, Any]:
    return {"operation": "remove", "address": _event_address(runnable, event)}


def _update(element: str, runnable: str, list_name: str, member: dict[str, str], field: str, value: Any) -> dict:
    return {
        "operation": "update",
        "address": _address(element, runnable, {"field": list_name}, {"member": member}, {"field": field}),
        "value": value,
    }


def _remove_the_event(project: Path) -> None:
    """``OnBlockWritten`` started by a timing event instead of its ``call_returned`` event (a 4.3.0 baseline)."""
    change(
        project / "model",
        CONTROLLER_FILE,
        lambda document: _entry(document["runnables"], name=ON_WRITTEN).update(
            events=[{"type": "timing", "period": 0.1}]
        ),
    )


TIMING_100MS: dict[str, Any] = {"type": "timing", "period": 0.1}


class TestTheProtocolCreatesAndRemovesTheEvent:
    @pytest.mark.parametrize("event", [RETURNED, RETURNED_NAMED], ids=["derived-names", "stated-names"])
    def test_a_create_is_committed_and_written(self, tmp_path: Path, event: dict[str, Any]) -> None:
        project = _project(tmp_path, "4.3.0")
        _remove_the_event(project)

        response = _change_set(project, [_create_event(ON_WRITTEN, event)])

        assert response["status"] == "committed", response
        assert _runnables(project / "model")[ON_WRITTEN]["events"] == [TIMING_100MS, event]
        assert _findings(project / "model", "4.3.0") == []

    def test_a_lone_remove_is_committed_at_4_3_0(self, tmp_path: Path) -> None:
        project = _project(tmp_path, "4.3.0")

        response = _change_set(project, [_remove_event(ON_WRITTEN, RETURNED)])

        assert response["status"] == "committed", response
        assert _runnables(project / "model")[ON_WRITTEN].get("events", []) == []

    def test_a_lone_remove_is_refused_at_4_2_2_with_nothing_written(self, tmp_path: Path) -> None:
        project = _project(tmp_path)
        before = _tree(project / "model")

        response = _change_set(project, [_remove_event(ON_WRITTEN, RETURNED)])

        assert response.get("error") == "consistency-violation", response
        assert "AUTOSAR 4.2.2" in response["message"]
        assert _tree(project / "model") == before

    def test_a_replacement_stating_the_names_is_one_remove_and_one_create(self, tmp_path: Path) -> None:
        project = _project(tmp_path)
        review = _Review()

        response = _change_set(
            project, [_remove_event(ON_WRITTEN, RETURNED), _create_event(ON_WRITTEN, RETURNED_NAMED)], review
        )

        assert response["status"] == "committed", response
        assert _runnables(project / "model")[ON_WRITTEN]["events"] == [RETURNED_NAMED]
        entries = [
            (entry.operation, entry.before if entry.operation == "remove" else entry.after)
            for entry in review.proposals[0].delta
        ]
        # A delta's halves are full values: `result` and `name` at `None`.
        assert entries == [
            ("remove", {**RETURNED, "result": None, "name": None}),
            ("create", RETURNED_NAMED),
        ]

    @pytest.mark.parametrize(
        ("address_tail", "value"),
        [((), {**RETURNED, "operation": "ReadBlock"}), (({"field": "result"},), "Result_Stored")],
        ids=["member", "field"],
    )
    def test_an_update_of_the_event_is_immutable(
        self, tmp_path: Path, address_tail: tuple[dict[str, Any], ...], value: Any
    ) -> None:
        project = _project(tmp_path)
        before = _tree(project / "model")
        address = _event_address(ON_WRITTEN, RETURNED)
        address["path"].extend(address_tail)

        response = _change_set(project, [{"operation": "update", "address": address, "value": value}])

        assert response.get("error") == "immutable-field", response
        assert _tree(project / "model") == before

    def test_an_event_left_dangling_by_removing_its_call_is_a_broken_reference(self, tmp_path: Path) -> None:
        project = _project(tmp_path, "4.3.0")
        before = _tree(project / "model")
        call = {"port": "rNvStorage", "operation": "WriteBlock"}

        response = _change_set(
            project,
            [{"operation": "remove", "address": _address(CONTROLLER, COMPUTE, {"field": "calls"}, {"member": call})}],
        )

        assert response.get("error") == "broken-reference", response
        assert response["addresses"][0]["path"][-3:] == [
            {"field": "events"},
            {"member": RETURNED},
            {"field": "operation"},
        ]
        assert _tree(project / "model") == before


#: The field updates, each ``(operation, check)``: ``check`` reads the committed file.
READ_BY = _update(CONTROLLER, DIAGNOSE, "reads", {"port": "rPedal", "element": "position"}, "by", "implicit")
WRITE_BY = _update(PEDAL, "SamplePedal", "writes", {"port": "pPedal", "element": "position"}, "by", "implicit")
WRITE_BY_CLEARED = _update(CONTROLLER, COMPUTE, "writes", {"port": "pBrakeTorque", "element": "torque"}, "by", None)
GET_VERSION = {"port": "rCalibration", "operation": "GetVersion"}
READ_BLOCK = {"port": "rCalibration", "operation": "ReadBlock"}
ASYNCHRONOUS = _update(CONTROLLER, DIAGNOSE, "calls", GET_VERSION, "asynchronous", True)
POLL = _update(CONTROLLER, COMPUTE, "calls", READ_BLOCK, "poll", False)
RESULT = _update(CONTROLLER, COMPUTE, "calls", READ_BLOCK, "result", "Result_Calib")
TIMEOUT = _update(CONTROLLER, COMPUTE, "calls", READ_BLOCK, "timeout", 1.0)


def _read_by(model_root: Path) -> Any:
    return _entry(_runnables(model_root)[DIAGNOSE]["reads"], port="rPedal").get("by")


def _write_by(model_root: Path) -> Any:
    return _entry(_runnables(model_root, PEDAL_FILE)["SamplePedal"]["writes"], port="pPedal").get("by")


def _cleared_write_by(model_root: Path) -> Any:
    return _entry(_runnables(model_root)[COMPUTE]["writes"], port="pBrakeTorque").get("by", "absent")


def _get_version(model_root: Path) -> dict[str, Any]:
    return _entry(_runnables(model_root)[DIAGNOSE]["calls"], operation="GetVersion")


def _read_block(model_root: Path) -> dict[str, Any]:
    return _entry(_runnables(model_root)[COMPUTE]["calls"], operation="ReadBlock")


FIELD_UPDATES: dict[str, tuple[dict[str, Any], Any, Any]] = {
    "read by": (READ_BY, _read_by, "implicit"),
    "write by": (WRITE_BY, _write_by, "implicit"),
    "write by cleared": (WRITE_BY_CLEARED, _cleared_write_by, "absent"),
    "asynchronous": (ASYNCHRONOUS, _get_version, {**GET_VERSION, "asynchronous": True}),
    "poll": (POLL, _read_block, {**READ_BLOCK, "asynchronous": True, "timeout": 0.5}),
    "result": (
        RESULT,
        _read_block,
        {**READ_BLOCK, "asynchronous": True, "poll": True, "result": "Result_Calib", "timeout": 0.5},
    ),
    "timeout": (TIMEOUT, _read_block, {**READ_BLOCK, "asynchronous": True, "poll": True, "timeout": 1.0}),
}


class TestTheProtocolUpdatesTheFields:
    @pytest.mark.parametrize("case", sorted(FIELD_UPDATES))
    def test_the_update_is_committed_and_written(self, tmp_path: Path, case: str) -> None:
        operation, written, expected = FIELD_UPDATES[case]
        # 4.3.0: an asynchronous `GetVersion` and an unpolled `ReadBlock` need no result point there.
        project = _project(tmp_path, "4.3.0")

        response = _change_set(project, [operation])

        assert response["status"] == "committed", response
        assert written(project / "model") == expected
        assert _findings(project / "model", "4.3.0") == []

    def test_the_delta_reports_a_field_update(self, tmp_path: Path) -> None:
        review = _Review()

        response = _change_set(_project(tmp_path, "4.3.0"), [TIMEOUT], review)

        assert response["status"] == "committed", response
        ((before, after),) = [(entry.before, entry.after) for entry in review.proposals[0].delta]
        assert (before, after) == (0.5, 1.0)


# The authorization boundary: an empty safety verdict, the ordinary review


REQUIREMENT: dict[str, Any] = {
    "id": "REQ_BRAKE_001",
    "title": "Compute the brake torque",
    "description": "The controller shall compute the brake torque.",
    "req_type": "req",
    "version": 1,
    "source": "architecture",
    "asil": "B",
}


def _rated(project: Path) -> None:
    """Link a rated requirement to the controller and rate every component at one level.

    One level everywhere, so no task schedules two (``safety.task-mixed-asil``).
    """
    put(project / "model", "requirements/REQ_BRAKE_001.yaml", REQUIREMENT)
    for component in sorted((project / "model" / "swc").rglob("*.yaml"), key=lambda path: path.as_posix()):
        change(project / "model", component.relative_to(project / "model").as_posix(), lambda d: d.update(asil="B"))
    change(project / "model", CONTROLLER_FILE, lambda document: document.update(satisfies=["REQ_BRAKE_001@1"]))


#: The acts, each as ``(remove the event first, operations)``, in a 4.3.0 project.
SAFETY_CASES: dict[str, tuple[bool, list[dict[str, Any]]]] = {
    "create an event": (True, [_create_event(ON_WRITTEN, RETURNED_NAMED)]),
    "remove an event": (False, [_remove_event(ON_WRITTEN, RETURNED)]),
    "update a read's by": (False, [READ_BY]),
    "update a write's by": (False, [WRITE_BY]),
    "update a call's asynchronous": (False, [ASYNCHRONOUS]),
    "update a call's poll": (False, [POLL]),
    "update a call's result": (False, [RESULT]),
    "update a call's timeout": (False, [TIMEOUT]),
}


class TestNoSafetyQuestionAndTheOrdinaryReview:
    @pytest.mark.parametrize("case", sorted(SAFETY_CASES))
    def test_the_safety_verdict_is_empty_and_a_declined_answer_still_commits(self, tmp_path: Path, case: str) -> None:
        without_event, operations = SAFETY_CASES[case]
        project = _project(tmp_path, "4.3.0")
        _rated(project)
        if without_event:
            _remove_the_event(project)
        assert _findings(project / "model", "4.3.0") == []
        review = _Review()

        response = _change_set(project, operations, review)

        assert response["status"] == "committed", response
        (proposal,) = review.proposals
        assert proposal.safety.changes == ()
        assert len(proposal.delta) == len(operations)

    @pytest.mark.parametrize("case", sorted(SAFETY_CASES))
    def test_an_empty_scope_refuses_it(self, tmp_path: Path, case: str) -> None:
        without_event, operations = SAFETY_CASES[case]
        project = _project(tmp_path, "4.3.0")
        if without_event:
            _remove_the_event(project)
        before = _tree(project / "model")

        response = _change_set(project, operations, approved_scope=[])

        assert response.get("error") == "unauthorized-delta", response
        assert _tree(project / "model") == before


# The trace link search's content fingerprint


def _controller_with(model_root: Path, mutate: Any) -> SWComponent:
    document = _load(model_root)
    mutate(document)
    return SWComponent.model_validate(document)


def _runnable_of(document: dict[str, Any], name: str) -> dict[str, Any]:
    return _entry(document["runnables"], name=name)


#: Each change of one new field, or of the event, on the fixture's controller.
FINGERPRINT_CHANGES: dict[str, Any] = {
    "a read's by": lambda d: _entry(_runnable_of(d, DIAGNOSE)["reads"], port="rPedal").update(by="implicit"),
    "a write's by": lambda d: _entry(_runnable_of(d, COMPUTE)["writes"], port="pBrakeTorque").pop("by"),
    "a call's asynchronous": lambda d: _entry(_runnable_of(d, DIAGNOSE)["calls"], operation="GetVersion").update(
        asynchronous=True
    ),
    "a call's poll": lambda d: _entry(_runnable_of(d, COMPUTE)["calls"], operation="ReadBlock").pop("poll"),
    "a call's result": lambda d: _entry(_runnable_of(d, COMPUTE)["calls"], operation="ReadBlock").update(
        result="Result_Calib"
    ),
    "a call's timeout": lambda d: _entry(_runnable_of(d, COMPUTE)["calls"], operation="ReadBlock").update(timeout=1.0),
    "the call_returned event removed": lambda d: _runnable_of(d, ON_WRITTEN).update(events=[]),
    "the call_returned event's result stated": lambda d: _runnable_of(d, ON_WRITTEN).update(
        events=[{**RETURNED, "result": "Result_Stored"}]
    ),
}


class TestTheContentFingerprintMoves:
    @pytest.mark.parametrize("case", sorted(FINGERPRINT_CHANGES))
    def test_changing_the_field_changes_the_component_s_fingerprint(self, model: Path, case: str) -> None:
        unchanged = _controller_with(model, lambda _document: None)
        changed = _controller_with(model, FINGERPRINT_CHANGES[case])

        assert changed != unchanged
        assert content_fingerprint(changed, {}) != content_fingerprint(unchanged, {})

    def test_an_unchanged_component_keeps_its_fingerprint(self, model: Path) -> None:
        assert content_fingerprint(_controller_with(model, lambda _d: None), {}) == content_fingerprint(
            _controller_with(model, lambda _d: None), {}
        )
