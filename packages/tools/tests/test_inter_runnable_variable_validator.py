"""``check-model`` over the inter-runnable variables, over a copy of the shared fixture.

The positive fixture (``inter_runnable_variable_support.variable_model``) is consistent, with no finding
and no warning; each negative case gives one finding, for the three variable errors and for the kept
rules widened by addition; the read-without-init warning is present on a read without an ``init``,
absent with one and on a write-only variable, and leaves the model consistent. Pass D answers each
error with its protocol code and an illegal or overlong name with ``invalid-identifier`` rather than an
exception; the warning has no protocol code and does not prevent staging a change set.

**Not reached:** the ARXML writer's twin, the importer and component exchange; a description holding a
character XML cannot carry (``test_xml_unrepresentable_character.py``); the order of execution, which
no rule here judges.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
from external_stubs_support import change, put
from inter_runnable_variable_support import (
    COMPONENT,
    COMPONENT_FILE,
    FILTERED,
    LOGGER,
    MONITOR,
    PEAK,
    SPARE,
    WRITER,
    variable_model,
)
from swcraft_tools.data_description import read_data_description
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.address import Address, FieldStep, MemberStep
from swcraft_tools.editing.apply import StagedModel
from swcraft_tools.editing.diagnosis import (
    BROKEN_REFERENCE,
    CONSISTENCY_VIOLATION,
    RULE_CODES,
    diagnose,
    emitted_identifier_violations,
)
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.staging import baseline_token
from swcraft_tools.reader import read_mapping_sets, read_swc_model
from swcraft_tools.validator import (
    WARNING_RULES,
    ConsistencyResult,
    check_cross_layer_consistency,
    inter_runnable_variable_records,
)

OWNER = Subject("swc", COMPONENT)
IMPLEMENTATION = "data_types/implementation"


@pytest.fixture
def model(tmp_path: Path) -> Path:
    return variable_model(tmp_path / "model")


def _component(model_root: Path, mutate: Any) -> None:
    change(model_root, COMPONENT_FILE, mutate)


def _variable(document: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [variable for variable in document["variables"] if variable["name"] == name]
    return found


def _runnable(document: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [runnable for runnable in document["runnables"] if runnable["name"] == name]
    return found


def _retype(model_root: Path, name: str, **fields: Any) -> None:
    """Give variable *name* the type fields in *fields* (and nothing else of the two)."""

    def mutate(document: dict[str, Any]) -> None:
        variable = _variable(document, name)
        variable.pop("application_type", None)
        variable.pop("implementation_type", None)
        variable.update(fields)

    _component(model_root, mutate)


def _type(model_root: Path, document: dict[str, Any]) -> None:
    put(model_root, f"{IMPLEMENTATION}/{document['name']}.yaml", document)


def _all(model_root: Path) -> ConsistencyResult:
    return check_cross_layer_consistency(str(model_root))


def _of(model_root: Path, rule: str) -> list[Any]:
    result = _all(model_root)
    return [record for record in (*result.records, *result.warnings) if record.rule == rule]


def _rules(model_root: Path) -> list[str]:
    result = _all(model_root)
    return [record.rule for record in (*result.records, *result.warnings)]


def _at(*steps: Any) -> Subject:
    return OWNER.at(*steps)


def _variable_at(name: str, *steps: Any) -> Subject:
    return _at(field_step("variables"), member_step(name), *steps)


def _entry_at(runnable: str, list_name: str, variable: str, *steps: Any) -> Subject:
    return _at(field_step("runnables"), member_step(runnable), field_step(list_name), member_step(variable), *steps)


# The positive fixture


class TestThePositiveFixture:
    def test_the_fixture_is_consistent_with_no_finding_and_no_warning(self, model: Path) -> None:
        result = _all(model)

        assert result.consistent
        assert result.records == ()
        assert result.warnings == ()

    def test_the_fixture_holds_every_shape_its_readme_names(self, model: Path) -> None:
        (swc,) = read_swc_model(str(model))
        variables = {variable.name: variable for variable in swc.variables}
        assert (variables[FILTERED].access, variables[FILTERED].application_type) == ("implicit", "VehicleSpeed")
        assert (variables[PEAK].access, variables[PEAK].implementation_type) == ("explicit", "uint16")
        assert variables[FILTERED].init is not None and variables[PEAK].init is not None
        assert variables[SPARE].init is None
        runnables = {runnable.name: runnable for runnable in swc.runnables}
        assert {access.variable for access in runnables[MONITOR].reads_variables} >= {PEAK}
        assert {access.variable for access in runnables[MONITOR].writes_variables} == {PEAK}
        assert runnables[LOGGER].reads_variables[0].name == "RV_Logged"
        touched = {
            access.variable
            for runnable in swc.runnables
            for access in (*runnable.reads_variables, *runnable.writes_variables)
        }
        assert SPARE not in touched
        periods = {runnable.name: runnable.events[0].period for runnable in swc.runnables}
        assert len({periods[WRITER], periods[LOGGER], periods[MONITOR]}) == 3

    def test_the_records_function_the_writer_calls_answers_the_same(self, model: Path) -> None:
        records = inter_runnable_variable_records(
            read_swc_model(str(model)), read_data_description(str(model)), read_mapping_sets(str(model))
        )
        assert records == []


# swc.variable.undeclared-type


class TestUndeclaredType:
    @pytest.mark.parametrize(
        ("field", "kind"), [("application_type", "application"), ("implementation_type", "implementation")]
    )
    def test_a_type_naming_nothing_is_one_finding_at_its_field(self, model: Path, field: str, kind: str) -> None:
        _retype(model, PEAK, **{field: "Nope"})

        (record,) = _of(model, "swc.variable.undeclared-type")

        assert record.subjects == (_variable_at(PEAK, field_step(field)),)
        assert record.target == "Nope"
        assert record.message == f"SWC '{COMPONENT}' variable '{PEAK}' references undeclared {kind} type 'Nope'."

    def test_an_implementation_type_on_the_application_side_is_undeclared(self, model: Path) -> None:
        """The side is the field's: ``uint16`` is no application type."""
        _retype(model, PEAK, application_type="uint16")

        assert len(_of(model, "swc.variable.undeclared-type")) == 1

    def test_an_external_data_type_stub_counts_as_declared(self, model: Path) -> None:
        _type(
            model,
            {
                "name": "Foreign_T",
                "kind": "external_type",
                "external_ref": "/Foreign/Foreign_T",
                "external_dest": "IMPLEMENTATION-DATA-TYPE",
            },
        )
        _retype(model, SPARE, implementation_type="Foreign_T")

        assert _of(model, "swc.variable.undeclared-type") == []
        assert _of(model, "swc.variable.pointer-type") == []

    def test_the_protocol_answers_broken_reference(self, model: Path) -> None:
        _retype(model, PEAK, implementation_type="Nope")
        (violation,) = [
            violation
            for violation in diagnose(_all(model))
            if any(address.element == COMPONENT for address in violation.addresses)
        ]
        assert violation.error == BROKEN_REFERENCE


# swc.variable.pointer-type


class TestPointerType:
    def _pointer(self, model_root: Path) -> None:
        _type(model_root, {"name": "Ptr_T", "kind": "pointer", "target": "uint8"})

    def test_a_variable_typed_by_a_pointer(self, model: Path) -> None:
        self._pointer(model)
        _retype(model, SPARE, implementation_type="Ptr_T")

        (record,) = _of(model, "swc.variable.pointer-type")

        assert record.subjects == (
            _variable_at(SPARE, field_step("implementation_type")),
            Subject("implementation_types", "Ptr_T"),
        )
        assert "is typed by 'Ptr_T', which is a pointer" in record.message
        assert "[constr_1296]" in record.message

    def test_a_reference_chain_ending_at_a_pointer(self, model: Path) -> None:
        self._pointer(model)
        _type(model, {"name": "Ptr_Alias", "kind": "reference_type", "base": "Ptr_T"})
        _type(model, {"name": "Ptr_Alias2", "kind": "reference_type", "base": "Ptr_Alias"})
        _retype(model, SPARE, implementation_type="Ptr_Alias2")

        (record,) = _of(model, "swc.variable.pointer-type")

        assert record.subjects[-1] == Subject("implementation_types", "Ptr_T")
        assert "'Ptr_Alias2' -> 'Ptr_Alias' -> 'Ptr_T'" in record.message

    def test_a_reference_chain_ending_at_a_value_type_is_silent(self, model: Path) -> None:
        _type(model, {"name": "Count_Alias", "kind": "reference_type", "base": "uint8"})
        _retype(model, SPARE, implementation_type="Count_Alias")

        assert _rules(model) == []

    def test_a_reference_cycle_is_not_judged_and_does_not_hang(self, model: Path) -> None:
        _type(model, {"name": "Loop_A", "kind": "reference_type", "base": "Loop_B"})
        _type(model, {"name": "Loop_B", "kind": "reference_type", "base": "Loop_A"})
        _retype(model, SPARE, implementation_type="Loop_A")

        rules = _rules(model)

        assert "swc.variable.pointer-type" not in rules
        assert "data-type.cycle" in rules

    def test_an_application_type_mapped_to_a_pointer(self, model: Path) -> None:
        self._pointer(model)
        change(
            model, "data_types/mapping_sets/SpeedMapping.yaml", lambda d: d["maps"][0].update(implementation="Ptr_T")
        )

        (record,) = _of(model, "swc.variable.pointer-type")

        assert record.subjects == (
            _variable_at(FILTERED, field_step("application_type")),
            Subject("implementation_types", "Ptr_T"),
        )
        assert "application type 'VehicleSpeed', which the component's mapping sets pair with 'Ptr_T'" in (
            record.message
        )

    def test_an_application_type_mapped_to_a_reference_chain_ending_at_a_pointer(self, model: Path) -> None:
        self._pointer(model)
        _type(model, {"name": "Ptr_Alias", "kind": "reference_type", "base": "Ptr_T"})
        change(
            model,
            "data_types/mapping_sets/SpeedMapping.yaml",
            lambda d: d["maps"][0].update(implementation="Ptr_Alias"),
        )

        (record,) = _of(model, "swc.variable.pointer-type")

        assert record.subjects[-1] == Subject("implementation_types", "Ptr_T")
        assert (
            "the component's mapping sets pair with 'Ptr_Alias', resolving through 'Ptr_Alias' -> 'Ptr_T', a pointer"
            in record.message
        )

    def test_a_broken_reference_chain_is_not_judged(self, model: Path) -> None:
        """A chain that breaks is the data type rule's, never the variable's."""
        _type(model, {"name": "Broken_Alias", "kind": "reference_type", "base": "Missing_T"})
        _retype(model, SPARE, implementation_type="Broken_Alias")

        rules = _rules(model)

        assert "swc.variable.pointer-type" not in rules
        assert "data-type.reference-type-undeclared-base" in rules

    def test_a_component_naming_an_undeclared_mapping_set_is_not_judged(self, model: Path) -> None:
        """The application side is judged only when every set the component names is declared once."""
        self._pointer(model)
        change(
            model, "data_types/mapping_sets/SpeedMapping.yaml", lambda d: d["maps"][0].update(implementation="Ptr_T")
        )
        _component(model, lambda d: d["data_type_mappings"].append("NoSuchMapping"))

        rules = _rules(model)

        assert "swc.variable.pointer-type" not in rules
        assert "swc.unknown-mapping-set" in rules

    def test_an_application_type_paired_with_two_implementation_types_is_not_judged(self, model: Path) -> None:
        self._pointer(model)
        put(
            model,
            "data_types/mapping_sets/PointerMapping.yaml",
            {"name": "PointerMapping", "maps": [{"application": "VehicleSpeed", "implementation": "Ptr_T"}]},
        )
        _component(model, lambda d: d["data_type_mappings"].append("PointerMapping"))

        rules = _rules(model)

        assert "swc.variable.pointer-type" not in rules
        assert "data-type.application-type-double-mapped" in rules

    def test_the_protocol_answers_consistency_violation(self, model: Path) -> None:
        self._pointer(model)
        _retype(model, SPARE, implementation_type="Ptr_T")
        (violation,) = diagnose(_all(model))
        assert violation.error == CONSISTENCY_VIOLATION
        assert "[constr_1296]" in violation.message


# swc.variable-access.unknown-variable


class TestUnknownVariable:
    def test_every_entry_naming_one_missing_variable_is_one_finding(self, model: Path) -> None:
        def mutate(document: dict[str, Any]) -> None:
            _runnable(document, WRITER)["writes_variables"].append({"variable": "Gone"})
            _runnable(document, MONITOR)["reads_variables"].append({"variable": "Gone"})

        _component(model, mutate)

        (record,) = _of(model, "swc.variable-access.unknown-variable")

        assert record.subjects == (
            _entry_at(WRITER, "writes_variables", "Gone", field_step("variable")),
            _entry_at(MONITOR, "reads_variables", "Gone", field_step("variable")),
        )
        assert record.target == f"{COMPONENT}.Gone"
        assert f"SWC '{COMPONENT}' declares no variable 'Gone'" in record.message
        assert "(2 entries)" in record.message
        assert "[constr_2026]" in record.message

    def test_a_variable_of_another_component_is_unknown(self, model: Path) -> None:
        put(
            model,
            "swc/chassis/Other.yaml",
            {
                "name": "Other",
                "handle_termination_and_restart": "no_support",
                "variables": [{"name": "Elsewhere", "access": "explicit", "implementation_type": "uint8"}],
            },
        )
        change(model, "compositions/Root.yaml", lambda d: d["members"].append({"name": "Other", "type": "Other"}))
        _component(model, lambda d: _runnable(d, WRITER)["writes_variables"].append({"variable": "Elsewhere"}))

        (record,) = _of(model, "swc.variable-access.unknown-variable")

        assert record.target == f"{COMPONENT}.Elsewhere"

    def test_the_protocol_answers_broken_reference(self, model: Path) -> None:
        """The diagnosis; the suggestion is the change set's, ``TestPassD``'s nearest-variable case."""
        _component(model, lambda d: _runnable(d, WRITER)["writes_variables"].append({"variable": "PeakSped"}))
        (violation,) = diagnose(_all(model))

        assert violation.error == BROKEN_REFERENCE
        assert violation.addresses[0].element == COMPONENT


# swc.variable.read-without-init (the warning)


class TestReadWithoutInit:
    def test_a_read_variable_with_no_init_is_one_warning_naming_every_reader(self, model: Path) -> None:
        _component(model, lambda d: _variable(d, FILTERED).pop("init"))

        result = _all(model)
        (warning,) = result.warnings

        assert result.consistent and result.records == ()
        assert warning.rule == "swc.variable.read-without-init"
        assert warning.subjects == (
            _variable_at(FILTERED, field_step("init")),
            _entry_at(LOGGER, "reads_variables", FILTERED),
            _entry_at(MONITOR, "reads_variables", FILTERED),
        )
        assert f"runnables '{LOGGER}' and '{MONITOR}' read it" in warning.message
        assert "[TPS_SWCT_01522]" in warning.message

    def test_a_read_variable_with_an_init_is_silent(self, model: Path) -> None:
        assert _of(model, "swc.variable.read-without-init") == []

    def test_a_write_only_variable_with_no_init_is_silent(self, model: Path) -> None:
        _component(model, lambda d: _runnable(d, WRITER)["writes_variables"].append({"variable": SPARE}))

        assert _rules(model) == []

    def test_the_rule_is_a_warning_and_has_no_protocol_code(self) -> None:
        assert "swc.variable.read-without-init" in WARNING_RULES
        assert "swc.variable.read-without-init" not in RULE_CODES

    def test_the_warning_does_not_refuse_a_commit(self, tmp_path: Path) -> None:
        project = fixtures.build_fixture(tmp_path / "project", "read-swc-element")
        variable = {"name": "Shared", "access": "implicit", "implementation_type": "float32"}
        response = _dispatch(project, _variable_create(variable), _read_create({"variable": "Shared"}))
        assert response["response"] == "change_set", response
        assert response["status"] == "staged", response

        # The staged model does draw the warning: the same two changes, made to a copy on disk.
        def mutate(document: dict[str, Any]) -> None:
            document.setdefault("variables", []).append(variable)
            (runnable,) = [r for r in document["runnables"] if r["name"] == CONSUMER_RUNNABLE]
            runnable.setdefault("reads_variables", []).append({"variable": "Shared"})

        change(project / "model", f"swc/example/{CONSUMER}.yaml", mutate)
        result = _all(project / "model")
        assert [warning.rule for warning in result.warnings] == ["swc.variable.read-without-init"]
        assert result.consistent


# The kept rules widened by addition


class TestTheWidenedRules:
    def test_an_init_the_type_cannot_hold_is_the_init_rule_at_the_variable(self, model: Path) -> None:
        _component(model, lambda d: _variable(d, PEAK).update(init={"a": 1}))

        (record,) = _of(model, "interface.init-shape-mismatch")

        assert record.subjects[0] == _variable_at(PEAK, field_step("init"))
        assert record.subjects[1].layer == "implementation_types"
        assert record.message.startswith(f"SWC '{COMPONENT}' variable '{PEAK}' init value at 'init'")

    def test_an_init_out_of_range(self, model: Path) -> None:
        _type(model, {"name": "Small_T", "kind": "value_type", "base": "uint8_base", "value_max": 3})
        _retype(model, PEAK, implementation_type="Small_T")
        _component(model, lambda d: _variable(d, PEAK).update(init=9))

        (record,) = _of(model, "interface.init-out-of-range")

        assert record.subjects[0] == _variable_at(PEAK, field_step("init"))

    def test_an_init_on_an_external_type_stub(self, model: Path) -> None:
        _type(
            model,
            {
                "name": "Foreign_T",
                "kind": "external_type",
                "external_ref": "/Foreign/Foreign_T",
                "external_dest": "IMPLEMENTATION-DATA-TYPE",
            },
        )
        _retype(model, PEAK, implementation_type="Foreign_T")

        (record,) = _of(model, "interface.init-on-external-type")

        assert record.subjects == (
            _variable_at(PEAK, field_step("init")),
            Subject("implementation_types", "Foreign_T"),
        )

    def test_an_application_type_no_set_pairs(self, model: Path) -> None:
        put(model, "data_types/application/Torque.yaml", {"name": "Torque", "kind": "application_type"})
        _retype(model, SPARE, application_type="Torque")

        (record,) = _of(model, "data-type.application-type-unmapped")

        assert "speaks type 'Torque' in its variables" in record.message
        assert record.subjects == (_at(field_step("data_type_mappings")),)

    def test_a_variable_and_a_runnable_differing_in_case_are_one_name_space_finding(self, model: Path) -> None:
        _component(model, lambda d: _variable(d, SPARE).update(name="logspeed"))

        (record,) = _of(model, "name.name-space-collision")

        assert f"behaviour '{COMPONENT}_InternalBehavior'" in record.message
        assert "'logspeed' (variables)" in record.message
        assert _variable_at("logspeed") in record.subjects

    def test_a_variable_named_like_an_event(self, model: Path) -> None:
        _component(model, lambda d: _variable(d, SPARE).update(name=f"TMT_{WRITER}"))

        (record,) = _of(model, "name.name-space-collision")

        assert f"'TMT_{WRITER}' (variables)" in record.message

    def test_a_stated_access_name_equal_to_a_derived_one(self, model: Path) -> None:
        _component(
            model,
            lambda d: _runnable(d, MONITOR)["writes_variables"][0].update(name=f"RV_{FILTERED}"),
        )

        (record,) = _of(model, "swc.access.name-collision")

        assert record.subjects == (_at(field_step("runnables"), member_step(MONITOR)),)
        assert f"reads_variables variable '{FILTERED}' (derived)" in record.message
        assert f"writes_variables variable '{PEAK}' (stated)" in record.message

    def test_a_stated_access_name_differing_in_case_is_a_name_space_finding(self, model: Path) -> None:
        _component(model, lambda d: _runnable(d, MONITOR)["writes_variables"][0].update(name=f"rv_{FILTERED}"))

        (record,) = _of(model, "name.name-space-collision")

        assert f"runnable '{COMPONENT}/{MONITOR}'" in record.message
        assert _entry_at(MONITOR, "writes_variables", PEAK, field_step("name")) in record.subjects
        assert _entry_at(MONITOR, "reads_variables", FILTERED) in record.subjects

    def test_an_illegal_variable_name(self, model: Path) -> None:
        _component(model, lambda d: _variable(d, SPARE).update(name="spare counter"))

        (record,) = _of(model, "identifier.declared")

        assert record.subjects == (_variable_at("spare counter", field_step("name")),)
        assert f"SWC '{COMPONENT}' variable 'spare counter'" in record.message

    def test_an_illegal_stated_access_name(self, model: Path) -> None:
        _component(model, lambda d: _runnable(d, LOGGER)["reads_variables"][0].update(name="logged speed"))

        (record,) = _of(model, "identifier.declared")

        assert record.subjects == (_entry_at(LOGGER, "reads_variables", FILTERED, field_step("name")),)

    @pytest.mark.parametrize(("list_name", "prefix"), [("reads_variables", "RV_"), ("writes_variables", "WV_")])
    def test_a_derived_access_name_over_its_budget(self, model: Path, list_name: str, prefix: str) -> None:
        long_name = "V" * 126

        def mutate(document: dict[str, Any]) -> None:
            _variable(document, SPARE).update(name=long_name, init=0)
            document["runnables"][0].setdefault(list_name, []).append({"variable": long_name})

        _component(model, mutate)

        (record,) = _of(model, "identifier.generated")

        assert record.subjects == (_entry_at(WRITER, list_name, long_name),)
        assert f"'{prefix}{long_name}' (129 characters)" in record.message
        assert "at most 125 characters" in record.message

    def test_a_derived_access_name_on_its_budget_is_legal(self, model: Path) -> None:
        on_budget = "V" * 125

        def mutate(document: dict[str, Any]) -> None:
            _variable(document, SPARE).update(name=on_budget, init=0)
            _runnable(document, MONITOR)["reads_variables"].append({"variable": on_budget})

        _component(model, mutate)

        assert _rules(model) == []

    def test_variables_on_an_external_stub(self, model: Path) -> None:
        put(
            model,
            "swc/chassis/Foreign.yaml",
            {
                "name": "Foreign",
                "external": True,
                "external_ref": "/Foreign/Foreign",
                "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
                "variables": [{"name": "Hidden", "access": "explicit", "implementation_type": "NoSuchType"}],
            },
        )

        (record,) = _of(model, "swc.external-with-ports")

        assert record.subjects == (Subject("swc", "Foreign", (field_step("variables"),)),)
        assert "declares 'external: true' and also variables" in record.message
        # The stub's variable is judged by no variable rule of its own: its undeclared type draws nothing.
        assert [record for record in _of(model, "swc.variable.undeclared-type") if "Foreign" in record.message] == []


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


def _variable_create(variable: dict[str, Any]) -> dict[str, Any]:
    """One variable of ``ExampleConsumerSWC``, created at its own member address."""
    return {
        "operation": "create",
        "address": {
            "layer": "swc",
            "element": CONSUMER,
            "path": [{"field": "variables"}, {"member": variable["name"]}],
        },
        "value": variable,
    }


def _access_create(list_name: str, entry: dict[str, Any]) -> dict[str, Any]:
    """One variable access of ``ExampleConsumerRun``, created at its own member address."""
    return {
        "operation": "create",
        "address": {
            "layer": "swc",
            "element": CONSUMER,
            "path": [
                {"field": "runnables"},
                {"member": CONSUMER_RUNNABLE},
                {"field": list_name},
                {"member": entry["variable"]},
            ],
        },
        "value": entry,
    }


def _read_create(entry: dict[str, Any]) -> dict[str, Any]:
    return _access_create("reads_variables", entry)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return fixtures.build_fixture(tmp_path / "project", "read-swc-element")


class TestPassD:
    def test_the_three_errors_are_mapped_and_the_warning_is_not(self) -> None:
        assert RULE_CODES["swc.variable.undeclared-type"].code == BROKEN_REFERENCE
        assert RULE_CODES["swc.variable-access.unknown-variable"].code == BROKEN_REFERENCE
        assert RULE_CODES["swc.variable.pointer-type"].code == CONSISTENCY_VIOLATION

    def test_an_undeclared_type_is_refused_as_a_broken_reference(self, project: Path) -> None:
        response = _dispatch(
            project, _variable_create({"name": "Shared", "access": "explicit", "implementation_type": "Nope"})
        )

        assert response["error"] == "broken-reference", response
        assert response["addresses"][0]["element"] == CONSUMER

    def test_an_unknown_variable_is_answered_with_the_nearest_variable_of_its_own_component(
        self, project: Path
    ) -> None:
        """The repair role ``access-variable`` searches the owner's ``variables`` alone (``[constr_2026]``).

        The provider's variable spelled exactly as the dangling name is never offered.
        """
        model_root = project / "model"
        change(
            model_root,
            f"swc/example/{CONSUMER}.yaml",
            lambda d: d.setdefault("variables", []).append(
                {"name": "PeakSpeed", "access": "explicit", "implementation_type": "float32"}
            ),
        )
        change(
            model_root,
            "swc/example/ExampleSWC.yaml",
            lambda d: d.setdefault("variables", []).append(
                {"name": "PeakSped", "access": "explicit", "implementation_type": "float32"}
            ),
        )

        response = _dispatch(project, _read_create({"variable": "PeakSped"}))

        assert response["error"] == "broken-reference", response
        assert response["message"].endswith(f"{CONSUMER}.variables holds no 'PeakSped'; nearest: PeakSpeed."), response

    def test_an_unknown_variable_is_refused_as_a_broken_reference(self, project: Path) -> None:
        response = _dispatch(project, _read_create({"variable": "Nope"}))

        assert response["error"] == "broken-reference", response
        assert response["addresses"][0]["path"][-2:] == [{"member": "Nope"}, {"field": "variable"}]

    def test_a_pointer_typed_variable_is_refused_as_a_consistency_violation(self, project: Path) -> None:
        response = _dispatch(
            project,
            {
                "operation": "create",
                "address": {"layer": "implementation_types", "element": "Ptr_T"},
                "value": {"name": "Ptr_T", "kind": "pointer", "target": "float32"},
            },
            _variable_create({"name": "Shared", "access": "explicit", "implementation_type": "Ptr_T"}),
        )

        assert response["error"] == "consistency-violation", response
        assert "[constr_1296]" in response["message"], response

    def test_an_illegal_variable_name_is_refused_with_its_code(self, project: Path) -> None:
        response = _dispatch(
            project, _variable_create({"name": "my shared", "access": "explicit", "implementation_type": "float32"})
        )

        assert response["error"] == "invalid-identifier", response
        assert "my shared" in response["message"]

    @pytest.mark.parametrize(("list_name", "prefix"), [("reads_variables", "RV_"), ("writes_variables", "WV_")])
    def test_an_overlong_derived_access_name_is_refused_with_its_code(
        self, project: Path, list_name: str, prefix: str
    ) -> None:
        long_name = "V" * 126
        response = _dispatch(
            project,
            _variable_create({"name": long_name, "access": "explicit", "implementation_type": "float32", "init": 0}),
            _access_create(list_name, {"variable": long_name}),
        )

        assert response["error"] == "invalid-identifier", response
        assert f"{prefix}{long_name}" in response["message"], response


class TestTheMaterializationWalk:
    """``emitted_identifier_violations``, the pre-materialization path ``check-model``'s two identifier rules mirror."""

    def _staged(self, project: Path, mutate: Any) -> StagedModel:
        """One staged component holding one runnable, changed by *mutate*.

        Built in place: the walk reads the staged dicts alone, and this module stays off the legacy
        adapters ``test_generic_core_gate.py`` counts. *project* keeps every case's fixture shape.
        """
        del project
        swc: dict[str, Any] = {"name": CONSUMER, "runnables": [{"name": CONSUMER_RUNNABLE}]}
        mutate(swc)
        return StagedModel(registry=reg.REGISTRY, elements={"swc": [swc]})

    def test_an_illegal_variable_name(self, project: Path) -> None:
        staged = self._staged(
            project,
            lambda swc: swc.update(variables=[{"name": "my shared", "access": "explicit", "implementation_type": "x"}]),
        )

        (violation,) = emitted_identifier_violations(staged)

        assert violation.error == "invalid-identifier"
        assert violation.addresses == (
            Address("swc", CONSUMER, (FieldStep("variables"), MemberStep("my shared"), FieldStep("name"))),
        )

    @pytest.mark.parametrize(("list_name", "prefix"), [("reads_variables", "RV_"), ("writes_variables", "WV_")])
    def test_an_overlong_derived_access_name(self, project: Path, list_name: str, prefix: str) -> None:
        long_name = "V" * 126
        staged = self._staged(project, lambda swc: swc["runnables"][0].update({list_name: [{"variable": long_name}]}))

        (violation,) = emitted_identifier_violations(staged)

        assert f"{prefix}{long_name}" in violation.message
        assert violation.addresses == (
            Address(
                "swc",
                CONSUMER,
                (FieldStep("runnables"), MemberStep(CONSUMER_RUNNABLE), FieldStep(list_name), MemberStep(long_name)),
            ),
        )

    def test_a_stated_access_name_is_judged_and_replaces_the_derived_form(self, project: Path) -> None:
        long_name = "V" * 126
        staged = self._staged(
            project,
            lambda swc: swc["runnables"][0].update(reads_variables=[{"variable": long_name, "name": "read long"}]),
        )

        (violation,) = emitted_identifier_violations(staged)

        assert violation.addresses[0].path[-1] == FieldStep("name")
        assert f"RV_{long_name}" not in violation.message
