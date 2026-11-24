"""``check-model`` over the data-received events, over a copy of the shared fixture.

What is asserted (``data_event_support.data_event_model``):

* **the positive fixture** is consistent, with no finding and no warning;
* **one negative case per refusal**, each giving **exactly one** finding over the whole of
  ``check_cross_layer_consistency``: an unknown port, a provide port, a client/server port, a
  mode-switch stub port, an unknown element, an element a sender/receiver stub does not list, an
  over-budget ``DRT_`` pair and a bad disabled mode; and the kept rules the kind reaches
  (``identifier.declared``, ``name.name-space-collision``);
* the ``DRT_`` pair budget at its boundary, and two events of one pair on two runnables;
* **the silence cases**;
* the identity and name functions answer the new kind, and ``event_identity`` raises on an unknown one;
* **pass D**: each of the four errors through the protocol, an over-budget pair refused as
  ``invalid-identifier``, and the pre-materialization walk.

**Not reached:** the ARXML writer, the importer, component exchange and the differ, the hand-written
serializer, and the queued split of ``[SWS_Rte_07403]``, which no rule judges.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
from data_event_support import (
    COMPONENT,
    COMPONENT_FILE,
    FILTER,
    FRONT_LEFT,
    FRONT_RIGHT,
    MODE_PORT,
    MONITOR,
    REAR_LEFT,
    REAR_PORT,
    SLEEP_MODE,
    TRACK_REAR,
    WHEEL_PORT,
    data_event,
    data_event_model,
    tasks_of_runnables_started_by,
)
from external_stubs_support import change, put
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.editing import diagnosis
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
from swcraft_tools.editing.staging import baseline_token
from swcraft_tools.reader import read_swc_model
from swcraft_tools.schema import DataEvent, DisabledMode, InitEvent, Runnable, SWComponent, TimingEvent
from swcraft_tools.validator import (
    GENERATED_DATA_EVENT_PREFIX,
    ConsistencyResult,
    check_cross_layer_consistency,
    check_data_events,
    data_event_records,
    derived_event_names,
    emitted_event_names,
    event_identity,
)
from swcraft_tools.validator.behaviour import _disabled_event_phrase

OWNER = Subject("swc", COMPONENT)
UNKNOWN_PORT = "swc.data-event.unknown-port"
DIRECTION = "swc.data-event.direction-mismatch"
KIND = "swc.data-event.interface-kind-mismatch"
UNKNOWN_ELEMENT = "swc.data-event.unknown-element"
DISABLED_MODE = "swc.timing_event.disabled-mode-port"


@pytest.fixture
def model(tmp_path: Path) -> Path:
    return data_event_model(tmp_path / "model")


def _component(model_root: Path, mutate: Any) -> None:
    change(model_root, COMPONENT_FILE, mutate)


def _runnable(document: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [runnable for runnable in document["runnables"] if runnable["name"] == name]
    return found


def _add_runnable(model_root: Path, name: str, *events: dict[str, Any]) -> None:
    """A runnable *name* started by *events*, appended to the component."""
    _component(model_root, lambda d: d["runnables"].append({"name": name, "events": list(events)}))


#: The ``com_specs`` a require port on ``WheelSpeedIf`` states, so its queued element is not a finding.
QUEUE_SPEC = [{"data_element": FRONT_RIGHT, "queue_length": 4}]


def _add_port(model_root: Path, name: str, direction: str, interface: str) -> None:
    port: dict[str, Any] = {"name": name, "direction": direction, "interface": interface}
    if direction == "require" and interface == "WheelSpeedIf":
        port["com_specs"] = QUEUE_SPEC
    _component(model_root, lambda d: d["ports"].append(port))


def _all(model_root: Path) -> ConsistencyResult:
    return check_cross_layer_consistency(str(model_root))


def _findings(model_root: Path) -> list[Any]:
    result = _all(model_root)
    return [*result.records, *result.warnings]


def _only(model_root: Path, rule: str) -> Any:
    """The one finding of the whole check, which must be of *rule*."""
    findings = _findings(model_root)
    assert [record.rule for record in findings] == [rule], [record.message for record in findings]
    return findings[0]


def _event_at(runnable: str, value: dict[str, Any], *steps: Any) -> Subject:
    return OWNER.at(field_step("runnables"), member_step(runnable), field_step("events"), member_step(value), *steps)


# The positive fixture


class TestThePositiveFixture:
    def test_the_fixture_is_consistent_with_no_finding_and_no_warning(self, model: Path) -> None:
        result = _all(model)

        assert result.consistent
        assert result.records == ()
        assert result.warnings == ()

    def test_the_fixture_holds_every_shape_its_readme_names(self, model: Path) -> None:
        (swc,) = read_swc_model(str(model))
        runnables = {runnable.name: runnable for runnable in swc.runnables}
        assert list(runnables) == [FILTER, MONITOR, TRACK_REAR]
        (filter_event,) = runnables[FILTER].events
        assert isinstance(filter_event, DataEvent)
        assert (filter_event.port, filter_event.element) == (WHEEL_PORT, FRONT_LEFT)
        assert [(read.port, read.element) for read in runnables[FILTER].reads] == [(WHEEL_PORT, FRONT_LEFT)]
        timing, monitor_event = runnables[MONITOR].events
        assert isinstance(timing, TimingEvent)
        assert isinstance(monitor_event, DataEvent)
        assert (monitor_event.port, monitor_event.element) == (WHEEL_PORT, FRONT_RIGHT)
        assert [mode.model_dump() for mode in monitor_event.disabled_modes] == [SLEEP_MODE]
        assert runnables[MONITOR].reads == []
        (rear_event,) = runnables[TRACK_REAR].events
        assert isinstance(rear_event, DataEvent)
        assert (rear_event.port, rear_event.element) == (REAR_PORT, REAR_LEFT)

    def test_the_derived_names_are_the_pair_form(self, model: Path) -> None:
        (swc,) = read_swc_model(str(model))
        assert derived_event_names(swc) == [
            f"{GENERATED_DATA_EVENT_PREFIX}{WHEEL_PORT}_{FRONT_LEFT}",
            f"TMT_{MONITOR}",
            f"{GENERATED_DATA_EVENT_PREFIX}{WHEEL_PORT}_{FRONT_RIGHT}",
            f"{GENERATED_DATA_EVENT_PREFIX}{REAR_PORT}_{REAR_LEFT}",
        ]

    def test_a_stated_name_is_emitted_in_its_slot(self, model: Path) -> None:
        _component(model, lambda d: _runnable(d, FILTER)["events"][0].update(name="OnFrontLeft"))
        (swc,) = read_swc_model(str(model))
        assert emitted_event_names(swc)[0] == "OnFrontLeft"
        assert _findings(model) == []

    def test_the_disk_check_answers_the_in_memory_core(self, model: Path) -> None:
        assert check_data_events(str(model)).records == ()


# One finding per refusal


class TestTheResolutionFamily:
    def test_an_unknown_port(self, model: Path) -> None:
        _add_runnable(model, "Extra", data_event("R_Nope", FRONT_LEFT))

        finding = _only(model, UNKNOWN_PORT)

        value = {"type": "data", "port": "R_Nope", "element": FRONT_LEFT}
        assert finding.subjects == (_event_at("Extra", value, field_step("port")),)
        assert finding.target == f"{COMPONENT}.R_Nope"
        assert "'R_Nope'" in finding.message
        assert "does not declare" in finding.message

    def test_a_port_of_another_component_is_unknown(self, model: Path) -> None:
        """``[constr_2530]``: the port is a feature of the event's own component; there is no cross-component form."""
        put(
            model,
            "swc/chassis/Other.yaml",
            {
                "name": "Other",
                "type": "application",
                "ports": [
                    {
                        "name": "R_Elsewhere",
                        "direction": "require",
                        "interface": "WheelSpeedIf",
                        "com_specs": QUEUE_SPEC,
                    }
                ],
                "handle_termination_and_restart": "no_support",
            },
        )
        change(model, "compositions/Root.yaml", lambda d: d["members"].append({"name": "Other", "type": "Other"}))
        _add_runnable(model, "Extra", data_event("R_Elsewhere", FRONT_LEFT))

        _only(model, UNKNOWN_PORT)

    def test_a_provide_port(self, model: Path) -> None:
        _add_port(model, "P_WheelSpeed", "provide", "WheelSpeedIf")
        _add_runnable(model, "Extra", data_event("P_WheelSpeed", FRONT_LEFT))

        finding = _only(model, DIRECTION)

        value = {"type": "data", "port": "P_WheelSpeed", "element": FRONT_LEFT}
        assert finding.subjects == (
            _event_at("Extra", value),
            OWNER.at(field_step("ports"), member_step("P_WheelSpeed"), field_step("direction")),
        )
        assert "provide port" in finding.message
        assert "require port" in finding.message

    def test_a_provide_port_naming_no_element_is_one_finding(self, model: Path) -> None:
        """The order is decided: the direction stops the event, so its element is not looked up."""
        _add_port(model, "P_WheelSpeed", "provide", "WheelSpeedIf")
        _add_runnable(model, "Extra", data_event("P_WheelSpeed", "Nope"))

        _only(model, DIRECTION)

    def test_a_client_server_port(self, model: Path) -> None:
        put(
            model,
            "interfaces/SpeedServiceIf.yaml",
            {"name": "SpeedServiceIf", "kind": "client_server", "operations": [{"name": "Get"}]},
        )
        _add_port(model, "R_SpeedService", "require", "SpeedServiceIf")
        _add_runnable(model, "Extra", data_event("R_SpeedService", FRONT_LEFT))

        finding = _only(model, KIND)

        value = {"type": "data", "port": "R_SpeedService", "element": FRONT_LEFT}
        assert finding.subjects == (
            _event_at("Extra", value),
            OWNER.at(field_step("ports"), member_step("R_SpeedService"), field_step("interface")),
        )
        assert "client/server" in finding.message
        assert "sender/receiver" in finding.message

    def test_a_mode_switch_stub_port(self, model: Path) -> None:
        """A stub's kind is its ``external_dest``, decided before any member lookup: one finding, the kind's."""
        _add_runnable(model, "Extra", data_event(MODE_PORT, "Sleep"))

        finding = _only(model, KIND)

        assert "external interface stub" in finding.message
        assert "MODE-SWITCH-INTERFACE" in finding.message

    def test_an_unknown_element(self, model: Path) -> None:
        _add_runnable(model, "Extra", data_event(WHEEL_PORT, "Nope"))

        finding = _only(model, UNKNOWN_ELEMENT)

        value = {"type": "data", "port": WHEEL_PORT, "element": "Nope"}
        assert finding.subjects == (_event_at("Extra", value, field_step("element")),)
        assert finding.target == "WheelSpeedIf.Nope"
        assert "does not declare" in finding.message

    def test_an_element_a_stub_does_not_list(self, model: Path) -> None:
        _add_runnable(model, "Extra", data_event(REAR_PORT, "RearRight"))

        finding = _only(model, UNKNOWN_ELEMENT)

        assert "external interface stub 'RearSpeedIf' does not list data element 'RearRight'" in finding.message
        assert finding.target == "RearSpeedIf.RearRight"

    def test_a_port_naming_no_declared_interface_is_left_to_its_own_rule(self, model: Path) -> None:
        _add_port(model, "R_Lost", "require", "NoSuchIf")
        _add_runnable(model, "Extra", data_event("R_Lost", FRONT_LEFT))

        assert [record.rule for record in _findings(model)] == ["port.undeclared-interface"]

    def test_the_in_memory_core_mints_only_its_family(self, model: Path) -> None:
        _add_runnable(model, "Extra", data_event("R_Nope", FRONT_LEFT), data_event(WHEEL_PORT, "Nope"))
        swcs = read_swc_model(str(model))
        from swcraft_tools.reader import read_interfaces

        records = data_event_records(swcs, read_interfaces(str(model)))

        assert [record.rule for record in records] == [UNKNOWN_PORT, UNKNOWN_ELEMENT]


class TestTheKeptRulesTheKindReaches:
    def test_a_bad_disabled_mode_says_event(self, model: Path) -> None:
        bad = {**SLEEP_MODE, "port": WHEEL_PORT}
        _add_runnable(model, "Extra", data_event(WHEEL_PORT, FRONT_LEFT, disabled_modes=[bad]))

        finding = _only(model, DISABLED_MODE)

        assert f"data-received event (port '{WHEEL_PORT}' element '{FRONT_LEFT}')" in finding.message
        assert "timing" not in finding.message
        value = {"type": "data", "port": WHEEL_PORT, "element": FRONT_LEFT, "disabled_modes": [bad]}
        assert finding.subjects == (_event_at("Extra", value),)

    def test_a_good_disabled_mode_is_silent(self, model: Path) -> None:
        _add_runnable(model, "Extra", data_event(WHEEL_PORT, FRONT_LEFT, disabled_modes=[SLEEP_MODE]))

        assert _findings(model) == []

    def test_a_stated_name_that_is_no_identifier(self, model: Path) -> None:
        _component(model, lambda d: _runnable(d, FILTER)["events"][0].update(name="on front left"))

        finding = _only(model, "identifier.declared")

        assert f"runnable '{FILTER}' data-received event name" in finding.message

    def test_a_variable_named_like_a_derived_name(self, model: Path) -> None:
        derived = f"{GENERATED_DATA_EVENT_PREFIX}{WHEEL_PORT}_{FRONT_LEFT}"
        _component(
            model,
            lambda d: d.update(
                variables=[{"name": derived.lower(), "access": "explicit", "implementation_type": "uint16"}]
            ),
        )

        finding = _only(model, "name.name-space-collision")

        assert derived in finding.message

    def test_an_area_named_like_a_derived_name(self, model: Path) -> None:
        derived = f"{GENERATED_DATA_EVENT_PREFIX}{REAR_PORT}_{REAR_LEFT}"
        _component(model, lambda d: d.update(exclusive_areas=[{"name": derived.upper()}]))

        finding = _only(model, "name.name-space-collision")

        assert derived in finding.message

    def test_a_runnable_named_like_a_derived_name(self, model: Path) -> None:
        derived = f"{GENERATED_DATA_EVENT_PREFIX}{WHEEL_PORT}_{FRONT_LEFT}"
        _component(model, lambda d: d["runnables"].append({"name": derived}))

        finding = _only(model, "name.runnable-event-collision")

        assert f"DATA-RECEIVED-EVENT '{derived}' the writer generates for port '{WHEEL_PORT}'" in finding.message


# The DRT_ pair budget (rule:identifier.generated)


def _long_port(model_root: Path, length: int) -> str:
    """A require port on ``WheelSpeedIf`` whose name is *length* characters long."""
    name = "R" + "x" * (length - 1)
    _add_port(model_root, name, "require", "WheelSpeedIf")
    return name


class TestTheGeneratedPairBudget:
    def test_a_pair_at_the_boundary_is_silent(self, model: Path) -> None:
        port = _long_port(model, 128 - len(GENERATED_DATA_EVENT_PREFIX) - 1 - len(FRONT_LEFT))
        _add_runnable(model, "Extra", data_event(port, FRONT_LEFT))

        assert _findings(model) == []

    def test_a_pair_one_over_is_one_finding_at_the_event(self, model: Path) -> None:
        port = _long_port(model, 129 - len(GENERATED_DATA_EVENT_PREFIX) - 1 - len(FRONT_LEFT))
        _add_runnable(model, "Extra", data_event(port, FRONT_LEFT))

        finding = _only(model, "identifier.generated")

        assert finding.subjects == (_event_at("Extra", {"type": "data", "port": port, "element": FRONT_LEFT}),)
        assert f"data-received event '{port}'.'{FRONT_LEFT}'" in finding.message
        assert "123" in finding.message

    def test_a_stated_name_takes_the_derived_form_out_of_the_budget(self, model: Path) -> None:
        port = _long_port(model, 129 - len(GENERATED_DATA_EVENT_PREFIX) - 1 - len(FRONT_LEFT))
        _add_runnable(model, "Extra", data_event(port, FRONT_LEFT, name="OnLongPort"))

        assert _findings(model) == []

    def test_two_events_of_one_pair_on_two_runnables_are_charged_their_suffixed_names(self, model: Path) -> None:
        """The base fits; the retroactive counter's ``_0`` / ``_1`` push both over, each its own finding."""
        port = _long_port(model, 128 - len(GENERATED_DATA_EVENT_PREFIX) - 1 - len(FRONT_LEFT))
        _add_runnable(model, "First", data_event(port, FRONT_LEFT))
        _add_runnable(model, "Second", data_event(port, FRONT_LEFT))

        findings = _findings(model)

        assert [record.rule for record in findings] == ["identifier.generated", "identifier.generated"]
        base = f"{GENERATED_DATA_EVENT_PREFIX}{port}_{FRONT_LEFT}"
        assert f"{base}_0" in findings[0].message
        assert f"{base}_1" in findings[1].message

    def test_an_illegal_port_name_is_not_charged_twice(self, model: Path) -> None:
        """One root cause, one issue: the declared port name is the finding, never the pair."""
        _component(
            model,
            lambda d: d["ports"].append(
                {"name": "R wheel", "direction": "require", "interface": "WheelSpeedIf", "com_specs": QUEUE_SPEC}
            ),
        )
        _add_runnable(model, "Extra", data_event("R wheel", FRONT_LEFT))

        assert [record.rule for record in _findings(model)] == ["identifier.declared"]


# Silence


class TestTheSilenceCases:
    def test_an_event_beside_a_timing_event(self, model: Path) -> None:
        """The fixture's monitor: the standard's own example (§ 7.2.5)."""
        assert _findings(model) == []

    def test_an_event_beside_an_init_event(self, model: Path) -> None:
        _component(model, lambda d: _runnable(d, FILTER)["events"].append({"type": "init"}))
        assert _findings(model) == []

    def test_one_event_repeated_on_one_runnable(self, model: Path) -> None:
        _component(model, lambda d: _runnable(d, FILTER)["events"].append(data_event(WHEEL_PORT, FRONT_LEFT)))

        assert _findings(model) == []
        (swc,) = read_swc_model(str(model))
        base = f"{GENERATED_DATA_EVENT_PREFIX}{WHEEL_PORT}_{FRONT_LEFT}"
        assert derived_event_names(swc)[:2] == [f"{base}_0", f"{base}_1"]

    def test_an_event_on_an_element_the_runnable_does_not_read(self, model: Path) -> None:
        """The fixture's monitor and rear tracker read nothing they are started by."""
        (swc,) = read_swc_model(str(model))
        runnables = {runnable.name: runnable for runnable in swc.runnables}
        assert runnables[MONITOR].reads == [] and runnables[TRACK_REAR].reads == []
        assert _findings(model) == []

    def test_two_runnables_started_by_one_element(self, model: Path) -> None:
        _add_runnable(model, "Extra", data_event(WHEEL_PORT, FRONT_LEFT))
        assert _findings(model) == []

    def test_two_runnables_started_by_one_queued_element_in_two_tasks_are_not_judged(self, model: Path) -> None:
        """``[SWS_Rte_07403]``'s queued split is a stated limit, not a rule."""
        _component(model, lambda d: _runnable(d, FILTER)["events"].append(data_event(WHEEL_PORT, FRONT_RIGHT)))
        assert tasks_of_runnables_started_by(model, FRONT_RIGHT) == {FILTER: ["Task_10ms"], MONITOR: ["Task_100ms"]}
        assert _findings(model) == []


# The identity and name functions


class TestTheIdentityAndNameFunctions:
    def test_event_identity_answers_the_kind(self) -> None:
        event = DataEvent(type="data", port="P", element="E")
        assert event_identity(event) == {"type": "data", "port": "P", "element": "E"}

    def test_event_identity_carries_the_disabled_modes_and_a_stated_name(self) -> None:
        mode = DisabledMode(**SLEEP_MODE)
        event = DataEvent(type="data", port="P", element="E", disabled_modes=[mode], name="N")
        assert event_identity(event) == {
            "type": "data",
            "port": "P",
            "element": "E",
            "disabled_modes": [SLEEP_MODE],
            "name": "N",
        }

    def test_event_identity_still_answers_an_init_event(self) -> None:
        assert event_identity(InitEvent(type="init")) == {"type": "init"}

    def test_event_identity_raises_on_a_kind_it_does_not_know(self) -> None:
        class Unknown:
            type = "unknown"
            name = None

        with pytest.raises(TypeError, match="Unknown"):
            event_identity(Unknown())  # type: ignore[arg-type]

    def test_the_disabled_mode_phrase_names_each_kind_and_raises_on_any_other(self) -> None:
        # One explicit arm per kind that declares `disabled_modes`: an untaught kind is a programming error.
        assert _disabled_event_phrase(TimingEvent(type="timing", period=0.01)) == "timing event (period 0.01s)"
        assert (
            _disabled_event_phrase(DataEvent(type="data", port="P", element="E"))
            == "data-received event (port 'P' element 'E')"
        )
        with pytest.raises(TypeError, match="InitEvent"):
            _disabled_event_phrase(InitEvent(type="init"))  # type: ignore[arg-type]

    def test_the_name_derivation_answers_the_kind(self) -> None:
        swc = SWComponent(
            name="C",
            type="application",
            runnables=[Runnable(name="R", events=[DataEvent(type="data", port="P", element="E")])],
        )
        assert derived_event_names(swc) == ["DRT_P_E"]

    def test_diagnosis_event_base_answers_the_kind(self) -> None:
        assert diagnosis._event_base({"name": "R"}, {"type": "data", "port": "P", "element": "E"}) == "DRT_P_E"

    def test_diagnosis_event_identity_answers_the_kind(self) -> None:
        staged = {"type": "data", "port": "P", "element": "E", "disabled_modes": [SLEEP_MODE], "name": "N"}
        assert diagnosis._event_identity(staged) == event_identity(
            DataEvent(type="data", port="P", element="E", disabled_modes=[DisabledMode(**SLEEP_MODE)], name="N")
        )

    def test_diagnosis_leaves_out_empty_disabled_modes(self) -> None:
        assert diagnosis._event_identity({"type": "data", "port": "P", "element": "E", "disabled_modes": []}) == {
            "type": "data",
            "port": "P",
            "element": "E",
        }


# Pass D, end to end through the editing protocol

CONSUMER = "ExampleConsumerSWC"
CONSUMER_RUNNABLE = "ExampleConsumerRun"
PROVIDER = "ExampleSWC"
PROVIDER_RUNNABLE = "ExampleRun"


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


def _event_create(
    event: dict[str, Any], *, element: str = CONSUMER, runnable: str = CONSUMER_RUNNABLE
) -> dict[str, Any]:
    """One data-received event of *runnable*, created at its own whole-value address."""
    return {
        "operation": "create",
        "address": {
            "layer": "swc",
            "element": element,
            "path": [{"field": "runnables"}, {"member": runnable}, {"field": "events"}, {"member": event}],
        },
        "value": event,
    }


def _port_create(port: dict[str, Any], *, element: str = CONSUMER) -> dict[str, Any]:
    return {
        "operation": "create",
        "address": {"layer": "swc", "element": element, "path": [{"field": "ports"}, {"member": port["name"]}]},
        "value": port,
    }


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return fixtures.build_fixture(tmp_path / "project", "read-swc-element")


class TestPassD:
    @pytest.mark.parametrize(
        ("rule", "code"),
        [
            (UNKNOWN_PORT, BROKEN_REFERENCE),
            (UNKNOWN_ELEMENT, BROKEN_REFERENCE),
            (DIRECTION, CONSISTENCY_VIOLATION),
            (KIND, CONSISTENCY_VIOLATION),
        ],
    )
    def test_each_error_is_mapped(self, rule: str, code: str) -> None:
        assert RULE_CODES[rule].code == code

    def test_a_valid_event_commits(self, project: Path) -> None:
        response = _dispatch(project, _event_create(data_event("rIn", "value")))

        assert "error" not in response, response

    def test_an_unknown_port_is_refused_as_a_broken_reference(self, project: Path) -> None:
        response = _dispatch(project, _event_create(data_event("rNope", "value")))

        assert response["error"] == "broken-reference", response
        assert response["addresses"][0]["element"] == CONSUMER
        assert response["addresses"][0]["path"][-1] == {"field": "port"}

    def test_an_unknown_element_is_refused_as_a_broken_reference(self, project: Path) -> None:
        response = _dispatch(project, _event_create(data_event("rIn", "nope")))

        assert response["error"] == "broken-reference", response
        assert response["addresses"][0]["path"][-1] == {"field": "element"}

    def test_a_provide_port_is_refused_as_a_consistency_violation(self, project: Path) -> None:
        response = _dispatch(
            project, _event_create(data_event("pOut", "value"), element=PROVIDER, runnable=PROVIDER_RUNNABLE)
        )

        assert response["error"] == "consistency-violation", response
        assert "provide port" in response["message"]

    def test_a_client_server_port_is_refused_as_a_consistency_violation(self, project: Path) -> None:
        response = _dispatch(
            project,
            _port_create({"name": "rDiag", "direction": "require", "interface": "IDiagnosticService"}),
            _event_create(data_event("rDiag", "value")),
        )

        assert response["error"] == "consistency-violation", response
        assert "client/server" in response["message"]

    def test_an_over_budget_pair_is_refused_as_an_invalid_identifier(self, project: Path) -> None:
        port = "r" + "x" * (129 - len(GENERATED_DATA_EVENT_PREFIX) - 1 - len("value") - 1)

        response = _dispatch(
            project,
            _port_create({"name": port, "direction": "require", "interface": "IExample"}),
            _event_create(data_event(port, "value")),
        )

        assert response["error"] == "invalid-identifier", response
        assert f"'{port}'.'value'" in response["message"]


class TestTheMaterializationWalk:
    """``emitted_identifier_violations``, the pre-materialization path ``check-model``'s identifier rule mirrors."""

    @staticmethod
    def _staged(*events: dict[str, Any]) -> StagedModel:
        swc: dict[str, Any] = {"name": CONSUMER, "runnables": [{"name": CONSUMER_RUNNABLE, "events": list(events)}]}
        return StagedModel(registry=reg.REGISTRY, elements={"swc": [swc]})

    def test_an_over_budget_pair(self) -> None:
        port = "r" + "x" * 120
        event = data_event(port, "value")

        (violation,) = emitted_identifier_violations(self._staged(event))

        assert violation.error == "invalid-identifier"
        assert violation.addresses == (
            Address(
                "swc",
                CONSUMER,
                (FieldStep("runnables"), MemberStep(CONSUMER_RUNNABLE), FieldStep("events"), MemberStep(event)),
            ),
        )
        assert f"data-received event '{port}'.'value'" in violation.message

    def test_an_illegal_stated_name(self) -> None:
        (violation,) = emitted_identifier_violations(self._staged(data_event("rIn", "value", name="on value")))

        assert violation.error == "invalid-identifier"
        assert "data-received event" in violation.message

    def test_a_legal_event_mints_no_violation(self) -> None:
        assert emitted_identifier_violations(self._staged(data_event("rIn", "value"))) == ()
