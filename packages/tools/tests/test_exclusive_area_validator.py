"""``check-model`` over the exclusive areas, on copies of ``exclusive_area_support.area_model``.

* **the positive fixture** (an area two runnables of different priority run inside, an
  area one runnable can enter, an unused area) is consistent, with no finding or warning;
* **one negative case per refusal**, each exactly one finding over
  ``check_cross_layer_consistency``: ``swc.exclusive-area.unknown-area`` (one per entry
  naming a missing area, in either role; another component's area is unknown) and the
  kept rules widened: ``name.name-space-collision`` for an area named like a runnable,
  event or variable, exactly and by case; ``identifier.declared``;
  ``swc.external-with-ports`` for areas on a stub; ``text.xml-unrepresentable-character``;
* **the silence cases**: an unused area, an area one runnable uses, one area in both
  roles on one runnable, all of an area's runnables in one task;
* **pass D**: ``unknown-area`` answers ``broken-reference`` with the nearest area of its
  own component, and an illegal area name ``invalid-identifier`` rather than an
  exception, through the protocol and the pre-materialization walk.

What this module does **not** reach: the ARXML writer, the importer and component
exchange; whether an area protects any datum, the order areas nest in and which
runnables can block which, none of which any rule judges.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
from exclusive_area_support import (
    CALIBRATE,
    COMPONENT,
    COMPONENT_FILE,
    FILTER,
    MONITOR,
    OFFSET_TABLE,
    SPARE_AREA,
    SPEED_STATE,
    area_model,
)
from external_stubs_support import change, put
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.address import Address, FieldStep, MemberStep
from swcraft_tools.editing.apply import StagedModel
from swcraft_tools.editing.diagnosis import BROKEN_REFERENCE, RULE_CODES, diagnose, emitted_identifier_violations
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.staging import baseline_token
from swcraft_tools.reader import read_swc_model, read_tasks
from swcraft_tools.validator import (
    WARNING_RULES,
    ConsistencyResult,
    check_cross_layer_consistency,
    check_exclusive_areas,
    exclusive_area_entries,
    exclusive_area_records,
)

OWNER = Subject("swc", COMPONENT)
UNKNOWN_AREA = "swc.exclusive-area.unknown-area"


@pytest.fixture
def model(tmp_path: Path) -> Path:
    return area_model(tmp_path / "model")


def _component(model_root: Path, mutate: Any) -> None:
    change(model_root, COMPONENT_FILE, mutate)


def _area(document: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [area for area in document["exclusive_areas"] if area["name"] == name]
    return found


def _runnable(document: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [runnable for runnable in document["runnables"] if runnable["name"] == name]
    return found


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


def _at(*steps: Any) -> Subject:
    return OWNER.at(*steps)


def _area_at(name: str, *steps: Any) -> Subject:
    return _at(field_step("exclusive_areas"), member_step(name), *steps)


def _entry_at(runnable: str, list_name: str, area: str, *steps: Any) -> Subject:
    return _at(field_step("runnables"), member_step(runnable), field_step(list_name), member_step(area), *steps)


# The positive fixture


class TestThePositiveFixture:
    def test_the_fixture_is_consistent_with_no_finding_and_no_warning(self, model: Path) -> None:
        result = _all(model)

        assert result.consistent
        assert result.records == ()
        assert result.warnings == ()

    def test_the_fixture_holds_every_shape_its_readme_names(self, model: Path) -> None:
        (swc,) = read_swc_model(str(model))
        areas = {area.name: area for area in swc.exclusive_areas}
        assert list(areas) == [SPEED_STATE, OFFSET_TABLE, SPARE_AREA]
        assert areas[SPEED_STATE].description != ""
        assert areas[OFFSET_TABLE].description == ""
        runnables = {runnable.name: runnable for runnable in swc.runnables}
        assert exclusive_area_entries(runnables[FILTER]) == [("runs_inside", SPEED_STATE)]
        assert exclusive_area_entries(runnables[MONITOR]) == [("runs_inside", SPEED_STATE)]
        assert exclusive_area_entries(runnables[CALIBRATE]) == [("can_enter", OFFSET_TABLE)]
        used = {area for runnable in swc.runnables for _list, area in exclusive_area_entries(runnable)}
        assert SPARE_AREA not in used
        # The two runnables sharing SpeedState run in tasks of different priority on one partition.
        tasks = {ref.runnable: task for task in read_tasks(str(model)) for ref in task.runnables}
        assert tasks[FILTER].priority != tasks[MONITOR].priority
        assert {task.partition for task in tasks.values()} == {"PARTITION0"}

    def test_the_records_function_the_writer_calls_answers_the_same(self, model: Path) -> None:
        assert exclusive_area_records(read_swc_model(str(model))) == []
        assert check_exclusive_areas(str(model)).consistent

    def test_the_entries_are_read_can_enter_first_each_in_its_own_order(self, model: Path) -> None:
        _component(
            model,
            lambda d: _runnable(d, FILTER).update(
                runs_inside=[{"area": SPEED_STATE}, {"area": SPARE_AREA}],
                can_enter=[{"area": OFFSET_TABLE}],
            ),
        )
        (swc,) = read_swc_model(str(model))
        (filter_runnable,) = [runnable for runnable in swc.runnables if runnable.name == FILTER]

        assert exclusive_area_entries(filter_runnable) == [
            ("can_enter", OFFSET_TABLE),
            ("runs_inside", SPEED_STATE),
            ("runs_inside", SPARE_AREA),
        ]


# swc.exclusive-area.unknown-area


class TestUnknownArea:
    def test_one_entry_naming_no_area_is_one_finding_at_its_field(self, model: Path) -> None:
        _component(model, lambda d: _runnable(d, CALIBRATE)["can_enter"].append({"area": "Gone"}))

        record = _only(model, UNKNOWN_AREA)

        assert record.subjects == (_entry_at(CALIBRATE, "can_enter", "Gone", field_step("area")),)
        assert record.target == f"{COMPONENT}.Gone"
        assert f"SWC '{COMPONENT}' declares no exclusive area 'Gone'" in record.message
        assert f"runnable '{CALIBRATE}' names" in record.message
        assert "(1 entry)" in record.message

    def test_every_entry_naming_one_missing_area_in_either_role_is_one_finding(self, model: Path) -> None:
        def mutate(document: dict[str, Any]) -> None:
            _runnable(document, FILTER)["runs_inside"].append({"area": "Gone"})
            _runnable(document, MONITOR).setdefault("can_enter", []).append({"area": "Gone"})

        _component(model, mutate)

        record = _only(model, UNKNOWN_AREA)

        assert record.subjects == (
            _entry_at(FILTER, "runs_inside", "Gone", field_step("area")),
            _entry_at(MONITOR, "can_enter", "Gone", field_step("area")),
        )
        assert f"runnables '{FILTER}' and '{MONITOR}' name" in record.message
        assert "(2 entries)" in record.message

    def test_two_missing_areas_are_two_findings(self, model: Path) -> None:
        _component(model, lambda d: _runnable(d, FILTER)["runs_inside"].extend([{"area": "Gone"}, {"area": "Lost"}]))

        assert [record.target for record in _findings(model)] == [f"{COMPONENT}.Gone", f"{COMPONENT}.Lost"]

    def test_an_area_of_another_component_is_unknown(self, model: Path) -> None:
        put(
            model,
            "swc/chassis/Other.yaml",
            {
                "name": "Other",
                "handle_termination_and_restart": "no_support",
                "exclusive_areas": [{"name": "Elsewhere"}],
            },
        )
        change(model, "compositions/Root.yaml", lambda d: d["members"].append({"name": "Other", "type": "Other"}))
        _component(model, lambda d: _runnable(d, FILTER)["runs_inside"].append({"area": "Elsewhere"}))

        record = _only(model, UNKNOWN_AREA)

        assert record.target == f"{COMPONENT}.Elsewhere"

    def test_the_match_is_exact_so_a_case_only_difference_is_unknown(self, model: Path) -> None:
        _component(model, lambda d: _runnable(d, CALIBRATE).update(can_enter=[{"area": OFFSET_TABLE.lower()}]))

        record = _only(model, UNKNOWN_AREA)

        assert record.target == f"{COMPONENT}.{OFFSET_TABLE.lower()}"

    def test_an_external_stub_is_not_judged(self, model: Path) -> None:
        """A stub's runnables are ``swc.external-with-ports``' finding, never this rule's."""
        put(
            model,
            "swc/chassis/Foreign.yaml",
            {
                "name": "Foreign",
                "external": True,
                "external_ref": "/Foreign/Foreign",
                "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
                "runnables": [{"name": "Hidden", "runs_inside": [{"area": "Nowhere"}]}],
            },
        )

        record = _only(model, "swc.external-with-ports")

        assert record.subjects == (Subject("swc", "Foreign", (field_step("runnables"),)),)

    def test_the_rule_is_an_error(self) -> None:
        assert UNKNOWN_AREA not in WARNING_RULES

    def test_the_protocol_answers_broken_reference(self, model: Path) -> None:
        _component(model, lambda d: _runnable(d, CALIBRATE).update(can_enter=[{"area": "OffsetTabel"}]))
        (violation,) = diagnose(_all(model))

        assert violation.error == BROKEN_REFERENCE
        assert violation.addresses[0].element == COMPONENT


# The kept rules widened by addition: exactly one finding each


class TestTheWidenedRules:
    @pytest.mark.parametrize("name", [FILTER, FILTER.lower()], ids=["exactly", "by-case"])
    def test_an_area_named_like_a_runnable(self, model: Path, name: str) -> None:
        _component(model, lambda d: _area(d, SPARE_AREA).update(name=name))

        record = _only(model, "name.name-space-collision")

        assert f"behaviour '{COMPONENT}_InternalBehavior'" in record.message
        assert f"'{name}' (exclusive_areas)" in record.message
        assert _area_at(name) in record.subjects
        assert _at(field_step("runnables"), member_step(FILTER)) in record.subjects

    @pytest.mark.parametrize("name", [f"TMT_{FILTER}", f"tmt_{FILTER}"], ids=["exactly", "by-case"])
    def test_an_area_named_like_an_event(self, model: Path, name: str) -> None:
        _component(model, lambda d: _area(d, SPARE_AREA).update(name=name))

        record = _only(model, "name.name-space-collision")

        assert f"'{name}' (exclusive_areas)" in record.message
        assert _area_at(name) in record.subjects

    @pytest.mark.parametrize("name", [SPARE_AREA, SPARE_AREA.upper()], ids=["exactly", "by-case"])
    def test_an_area_named_like_a_variable(self, model: Path, name: str) -> None:
        put(
            model,
            "data_types/base/uint8_base.yaml",
            {"name": "uint8_base", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"},
        )
        put(
            model,
            "data_types/implementation/uint8.yaml",
            {"name": "uint8", "kind": "value_type", "base": "uint8_base"},
        )
        _component(
            model, lambda d: d.update(variables=[{"name": name, "access": "explicit", "implementation_type": "uint8"}])
        )

        record = _only(model, "name.name-space-collision")

        assert f"'{name}' (variables)" in record.message
        assert f"'{SPARE_AREA}' (exclusive_areas)" in record.message

    def test_two_areas_differing_in_case_are_one_name_space_finding(self, model: Path) -> None:
        """An exact repeat is refused at load; a case-only one loads and is this rule's."""
        _component(model, lambda d: _area(d, SPARE_AREA).update(name=SPEED_STATE.lower()))

        record = _only(model, "name.name-space-collision")

        assert _area_at(SPEED_STATE) in record.subjects
        assert _area_at(SPEED_STATE.lower()) in record.subjects

    def test_an_illegal_area_name(self, model: Path) -> None:
        _component(model, lambda d: _area(d, SPARE_AREA).update(name="spare area"))

        record = _only(model, "identifier.declared")

        assert record.subjects == (_area_at("spare area", field_step("name")),)
        assert f"SWC '{COMPONENT}' exclusive area 'spare area'" in record.message

    def test_an_area_name_over_the_identifier_limit(self, model: Path) -> None:
        long_name = "A" * 129
        _component(model, lambda d: _area(d, SPARE_AREA).update(name=long_name))

        record = _only(model, "identifier.declared")

        assert record.subjects == (_area_at(long_name, field_step("name")),)

    def test_areas_on_an_external_stub(self, model: Path) -> None:
        put(
            model,
            "swc/chassis/Foreign.yaml",
            {
                "name": "Foreign",
                "external": True,
                "external_ref": "/Foreign/Foreign",
                "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
                "exclusive_areas": [{"name": "Hidden"}],
            },
        )

        record = _only(model, "swc.external-with-ports")

        assert record.subjects == (Subject("swc", "Foreign", (field_step("exclusive_areas"),)),)
        assert "declares 'external: true' and also exclusive_areas" in record.message

    def test_an_area_description_xml_cannot_carry(self, model: Path) -> None:
        _component(model, lambda d: _area(d, SPEED_STATE).update(description="Guards\x01 the speed."))

        record = _only(model, "text.xml-unrepresentable-character")

        assert record.subjects == (_area_at(SPEED_STATE, field_step("description")),)
        assert f"SWC '{COMPONENT}' exclusive area '{SPEED_STATE}'" in record.message


# Silence


class TestSilence:
    def test_an_unused_area_is_silent(self, model: Path) -> None:
        """The fixture's ``SpareArea``; the positive case proves it, this names it."""
        (swc,) = read_swc_model(str(model))
        assert SPARE_AREA in {area.name for area in swc.exclusive_areas}
        assert _findings(model) == []

    def test_an_area_one_runnable_uses_is_silent(self, model: Path) -> None:
        _component(model, lambda d: _runnable(d, MONITOR).update(concurrent=True, runs_inside=[{"area": SPARE_AREA}]))

        assert _findings(model) == []

    def test_one_area_in_both_roles_on_one_runnable_is_silent(self, model: Path) -> None:
        _component(model, lambda d: _runnable(d, FILTER).update(can_enter=[{"area": SPEED_STATE}]))

        assert _findings(model) == []

    def test_all_of_an_areas_runnables_in_one_task_is_silent(self, model: Path) -> None:
        def move(document: dict[str, Any]) -> None:
            document["runnables"].append({"swc": COMPONENT, "runnable": MONITOR})

        change(model, "tasks/Task_10ms.yaml", move)
        (model / "tasks" / "Task_100ms.yaml").unlink()

        assert _findings(model) == []


# Pass D, end to end through the editing protocol

CONSUMER = "ExampleConsumerSWC"
CONSUMER_RUNNABLE = "ExampleConsumerRun"


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


def _area_create(area: dict[str, Any]) -> dict[str, Any]:
    """One exclusive area of ``ExampleConsumerSWC``, created at its own member address."""
    return {
        "operation": "create",
        "address": {
            "layer": "swc",
            "element": CONSUMER,
            "path": [{"field": "exclusive_areas"}, {"member": area["name"]}],
        },
        "value": area,
    }


def _entry_create(list_name: str, area: str) -> dict[str, Any]:
    """One area entry of ``ExampleConsumerRun``, created at its own member address."""
    return {
        "operation": "create",
        "address": {
            "layer": "swc",
            "element": CONSUMER,
            "path": [
                {"field": "runnables"},
                {"member": CONSUMER_RUNNABLE},
                {"field": list_name},
                {"member": area},
            ],
        },
        "value": {"area": area},
    }


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return fixtures.build_fixture(tmp_path / "project", "read-swc-element")


class TestPassD:
    def test_the_error_is_mapped_to_broken_reference(self) -> None:
        assert RULE_CODES[UNKNOWN_AREA].code == BROKEN_REFERENCE

    @pytest.mark.parametrize("list_name", ["can_enter", "runs_inside"])
    def test_an_unknown_area_is_refused_as_a_broken_reference(self, project: Path, list_name: str) -> None:
        response = _dispatch(project, _entry_create(list_name, "Nope"))

        assert response["error"] == "broken-reference", response
        assert response["addresses"][0]["element"] == CONSUMER
        assert response["addresses"][0]["path"][-3:] == [
            {"field": list_name},
            {"member": "Nope"},
            {"field": "area"},
        ]

    def test_an_unknown_area_is_answered_with_the_nearest_area_of_its_own_component(self, project: Path) -> None:
        """The repair role ``access-area`` searches the owner's ``exclusive_areas`` alone.

        The provider's exactly-spelled area, nearer than anything the consumer holds, is never offered.
        """
        model_root = project / "model"
        change(
            model_root,
            f"swc/example/{CONSUMER}.yaml",
            lambda d: d.setdefault("exclusive_areas", []).append({"name": "SpeedState"}),
        )
        change(
            model_root,
            "swc/example/ExampleSWC.yaml",
            lambda d: d.setdefault("exclusive_areas", []).append({"name": "SpeedStat"}),
        )

        response = _dispatch(project, _entry_create("runs_inside", "SpeedStat"))

        assert response["error"] == "broken-reference", response
        assert response["message"].endswith(f"{CONSUMER}.exclusive_areas holds no 'SpeedStat'; nearest: SpeedState."), (
            response
        )

    def test_an_area_and_its_entry_created_together_commit(self, project: Path) -> None:
        response = _dispatch(project, _area_create({"name": "SpeedState"}), _entry_create("runs_inside", "SpeedState"))

        assert "error" not in response, response

    def test_an_illegal_area_name_is_refused_with_its_code(self, project: Path) -> None:
        response = _dispatch(project, _area_create({"name": "speed state"}))

        assert response["error"] == "invalid-identifier", response
        assert "speed state" in response["message"]


class TestTheMaterializationWalk:
    """``emitted_identifier_violations``, the pre-materialization path ``check-model``'s identifier rule mirrors."""

    @staticmethod
    def _staged(mutate: Any) -> StagedModel:
        """One staged component holding one runnable, changed by *mutate*, built in place.

        The walk reads the staged dicts alone, so this module stays off the legacy adapters
        ``test_generic_core_gate.py`` counts.
        """
        swc: dict[str, Any] = {"name": CONSUMER, "runnables": [{"name": CONSUMER_RUNNABLE}]}
        mutate(swc)
        return StagedModel(registry=reg.REGISTRY, elements={"swc": [swc]})

    def test_an_illegal_area_name(self) -> None:
        staged = self._staged(lambda swc: swc.update(exclusive_areas=[{"name": "speed state"}]))

        (violation,) = emitted_identifier_violations(staged)

        assert violation.error == "invalid-identifier"
        assert violation.addresses == (
            Address("swc", CONSUMER, (FieldStep("exclusive_areas"), MemberStep("speed state"), FieldStep("name"))),
        )
        assert f"SWC '{CONSUMER}' exclusive area" in violation.message

    def test_a_legal_area_and_its_entries_mint_no_violation(self) -> None:
        def mutate(swc: dict[str, Any]) -> None:
            swc["exclusive_areas"] = [{"name": "SpeedState"}]
            swc["runnables"][0].update(can_enter=[{"area": "SpeedState"}], runs_inside=[{"area": "SpeedState"}])

        assert emitted_identifier_violations(self._staged(mutate)) == ()
