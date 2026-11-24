"""The inter-runnable variables, at the declaration, the loader and the derived views.

A component declares ``variables`` -- each ``explicit`` or ``implicit``, typed by exactly one of
``application_type`` and ``implementation_type``, with an optional ``init`` -- and a runnable names
the variables of its own component it reads and writes in ``reads_variables`` and
``writes_variables``, one member kind per list.

This module asks five questions, and only these:

* **what the contract declares** -- the three member kinds, their fields and the three lists, the
  rule identifiers with their families, bindings and the one warning;
* **what is refused at load** -- a missing ``access``, both or neither type field, a blank
  reference, a variable named twice, a variable listed twice in one access list;
* **what is written out** -- the lists are omitted at their default, so a component without them
  dumps with no key for them;
* **how a field-identified access list is addressed** -- the protocol address, the ``diff`` key and
  the editor-schema path of ``reads_variables`` by its ``variable``;
* **that one name shared by a variable and a runnable is one finding** --
  ``rule:name.name-space-collision``'s alone.

**Not reached:** no other cross-element rule is exercised (the validator's tests); nothing here emits
ARXML, imports an export, compares a component export or writes through
``writer.yaml_documents.swc_to_dict``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from model_contract_fixtures import load_contract
from pydantic import ValidationError
from swcraft_tools import editor_schema, validator
from swcraft_tools.differ import collection_rule
from swcraft_tools.editing import address as addr
from swcraft_tools.editing.diagnosis import RULE_CODES
from swcraft_tools.model_registry import production_registry
from swcraft_tools.schema import Runnable, SWComponent, Variable, VariableReadRef, VariableWriteRef
from swcraft_tools.writer.elements import element_to_dict

BINDING = Path(__file__).resolve().parents[1] / "src" / "swcraft_tools" / "model_contract" / "binding.json"

#: The four cross-element identifiers the contract adds. A variable's name shared with a runnable or
#: an event is no rule of its own: it is ``name.name-space-collision``'s, widened.
CROSS_ELEMENT = (
    "swc.variable.undeclared-type",
    "swc.variable-access.unknown-variable",
    "swc.variable.pointer-type",
    "swc.variable.read-without-init",
)

#: The eight element-local identifiers and the method each is bound to.
ELEMENT_LOCAL = {
    "rule:swc.swc.variables.no-duplicate-entry": ("kind:swc.swc", "variables_must_not_have_duplicates"),
    "rule:swc.runnable.reads_variables.no-duplicate-entry": (
        "kind:swc.runnable",
        "reads_variables_must_not_have_duplicates",
    ),
    "rule:swc.runnable.writes_variables.no-duplicate-entry": (
        "kind:swc.runnable",
        "writes_variables_must_not_have_duplicates",
    ),
    "rule:swc.variable.type-form": ("kind:swc.variable", "exactly_one_type_field"),
    "rule:swc.variable.application_type.not-blank": ("kind:swc.variable", "application_type_must_not_be_blank"),
    "rule:swc.variable.implementation_type.not-blank": (
        "kind:swc.variable",
        "implementation_type_must_not_be_blank",
    ),
    "rule:swc.variable_read.reference-not-blank": ("kind:swc.variable_read", "variable_must_not_be_blank"),
    "rule:swc.variable_write.reference-not-blank": ("kind:swc.variable_write", "variable_must_not_be_blank"),
}


def _kinds() -> dict[str, dict[str, Any]]:
    return {kind["id"]: kind for layer in load_contract()["layers"] for kind in layer["kinds"]}


def _fields(kind_id: str) -> dict[str, dict[str, Any]]:
    return {field["name"]: field for field in _kinds()[kind_id]["fields"]}


def _rules() -> dict[str, dict[str, Any]]:
    return {rule["id"]: rule for rule in load_contract()["rules"]}


def _binding() -> dict[str, Any]:
    with BINDING.open(encoding="utf-8") as handle:
        return json.load(handle)


def _component(**fields: Any) -> dict[str, Any]:
    """A component file's mapping, with one implicit variable and one runnable reading it."""
    return {
        "name": "SpeedEstimator",
        "variables": [{"name": "FilteredSpeed", "access": "implicit", "implementation_type": "uint16"}],
        "runnables": [{"name": "MonitorSpeed", "reads_variables": [{"variable": "FilteredSpeed"}]}],
        **fields,
    }


# The declaration


class TestTheContractDeclaresTheVariables:
    def test_the_contract_version_is_the_minor_that_admits_them(self) -> None:
        """The contract version this module was checked against; any bump moves it."""
        assert load_contract()["contractVersion"] == "12.7.1"

    def test_the_variable_is_a_member_of_the_components_variables(self) -> None:
        kind = _kinds()["kind:swc.variable"]

        assert (kind["kind"], kind["memberOnly"]) == ("variable", True)
        assert kind["memberOf"] == [{"kind": "swc", "field": "variables"}]
        assert list(_fields("kind:swc.variable")) == [
            "name",
            "access",
            "application_type",
            "implementation_type",
            "init",
            "description",
            "sw_calibration_access",
        ]

    def test_access_is_required_with_no_default_and_names_the_two_roles(self) -> None:
        access = _fields("kind:swc.variable")["access"]

        assert (access["type"], access["required"], access["immutable"]) == ("string", True, False)
        assert "default" not in access
        assert access["enum"] == ["implicit", "explicit"]

    def test_the_type_pair_and_init_are_the_data_elements_shape(self) -> None:
        variable = _fields("kind:swc.variable")
        element = _fields("kind:interfaces.data_element")

        for name in ("application_type", "implementation_type"):
            assert variable[name]["reference"] == element[name]["reference"], name
            assert (variable[name]["required"], variable[name]["default"]) == (False, None), name
        assert variable["init"]["type"] == element["init"]["type"] == "value"
        assert variable["sw_calibration_access"]["enum"] == element["sw_calibration_access"]["enum"]
        assert variable["name"]["immutable"] is True

    @pytest.mark.parametrize(
        ("kind_id", "list_name"),
        [("kind:swc.variable_read", "reads_variables"), ("kind:swc.variable_write", "writes_variables")],
    )
    def test_each_access_kind_is_a_member_of_its_own_list(self, kind_id: str, list_name: str) -> None:
        kind = _kinds()[kind_id]
        fields = _fields(kind_id)

        assert kind["memberOf"] == [{"kind": "runnable", "field": list_name}]
        assert list(fields) == ["variable", "name"]
        variable = fields["variable"]
        assert (variable["required"], variable["immutable"]) == (True, True)
        assert variable["memberReference"]["collections"] == ["collection:swc.swc.variables"]
        assert (fields["name"]["required"], fields["name"]["default"]) == (False, None)

    def test_the_three_lists_sit_where_decision_1_puts_them(self) -> None:
        component = list(_fields("kind:swc.swc"))
        runnable = list(_fields("kind:swc.runnable"))

        assert component[component.index("runnables") + 1] == "variables"
        assert runnable[runnable.index("calls") + 1 : runnable.index("calls") + 3] == [
            "reads_variables",
            "writes_variables",
        ]

    @pytest.mark.parametrize(
        ("kind_id", "field", "identity"),
        [
            ("kind:swc.swc", "variables", "name"),
            ("kind:swc.runnable", "reads_variables", "variable"),
            ("kind:swc.runnable", "writes_variables", "variable"),
        ],
    )
    def test_each_list_is_field_identified_and_omitted_at_its_default(
        self, kind_id: str, field: str, identity: str
    ) -> None:
        declared = _fields(kind_id)[field]

        assert (declared["required"], declared["default"], declared["omitAtDefault"]) == (False, [], True)
        assert declared["collection"]["memberIdentity"] == "field"
        assert declared["collection"]["identityField"] == identity

    @pytest.mark.parametrize("rule", CROSS_ELEMENT)
    def test_each_cross_element_rule_is_declared_summarised_and_bound(self, rule: str) -> None:
        declared = _rules()[f"rule:{rule}"]

        assert declared["family"] == "cross-element"
        assert declared["summary"] == validator.CONSISTENCY_RULES[rule]
        assert _binding()["rules"][f"rule:{rule}"]["module"].rpartition(".")[0] == "swcraft_tools.validator"

    def test_the_read_without_init_rule_is_the_one_warning_and_has_no_code(self) -> None:
        """A read of a variable that states no ``init`` is warned, never refused, so it has no protocol code."""
        assert _rules()["rule:swc.variable.read-without-init"]["severity"] == "warning"
        assert "swc.variable.read-without-init" in validator.WARNING_RULES
        assert "swc.variable.read-without-init" not in RULE_CODES
        for rule in CROSS_ELEMENT[:3]:
            assert "severity" not in _rules()[f"rule:{rule}"], rule
            assert rule in RULE_CODES, rule

    def test_no_separate_variable_collision_rule_exists(self) -> None:
        """A name shared across the behaviour's containers is ``name.name-space-collision``'s, widened."""
        assert "rule:name.variable-collision" not in _rules()
        applies = _rules()["rule:name.name-space-collision"]["appliesTo"]
        assert {"field:swc.swc.variables", "field:swc.variable.name"} <= set(applies)

    @pytest.mark.parametrize("rule_id", sorted(ELEMENT_LOCAL))
    def test_each_element_local_rule_is_bound_to_its_validator(self, rule_id: str) -> None:
        assert _rules()[rule_id]["family"] == "element-local"
        kind, method = ELEMENT_LOCAL[rule_id]
        assert _binding()["rules"][rule_id] == {"kind": kind, "method": method}

    def test_the_access_prefixes_are_restated_in_the_validator(self) -> None:
        assert (validator.GENERATED_READ_VARIABLE_PREFIX, validator.GENERATED_WRITE_VARIABLE_PREFIX) == ("RV_", "WV_")


# What loads, and what is refused at load


class TestTheLoader:
    def test_the_three_kinds_and_three_fields_load(self) -> None:
        component = SWComponent.model_validate(
            _component(
                variables=[
                    {
                        "name": "FilteredSpeed",
                        "access": "implicit",
                        "application_type": "VehicleSpeed",
                        "init": 0,
                        "description": "Speed after the 10 ms filter.",
                        "sw_calibration_access": "read_only",
                    },
                    {"name": "PeakResetRequest", "access": "explicit", "implementation_type": "Boolean"},
                ],
                runnables=[
                    {"name": "FilterSpeed", "writes_variables": [{"variable": "FilteredSpeed"}]},
                    {
                        "name": "MonitorSpeed",
                        "reads_variables": [{"variable": "FilteredSpeed"}],
                        "writes_variables": [{"variable": "PeakResetRequest", "name": "WV_PeakReset"}],
                    },
                ],
            )
        )

        assert [variable.access for variable in component.variables] == ["implicit", "explicit"]
        assert component.variables[0].init == 0
        assert component.runnables[0].writes_variables == [VariableWriteRef(variable="FilteredSpeed")]
        assert component.runnables[1].writes_variables[0].name == "WV_PeakReset"

    def test_one_runnable_may_read_and_write_the_same_variable(self) -> None:
        runnable = Runnable.model_validate(
            {"name": "R", "reads_variables": [{"variable": "V"}], "writes_variables": [{"variable": "V"}]}
        )
        assert runnable.reads_variables[0].variable == runnable.writes_variables[0].variable == "V"

    def test_a_missing_access_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="access"):
            Variable.model_validate({"name": "V", "implementation_type": "uint8"})

    def test_an_access_outside_the_two_roles_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="access"):
            Variable.model_validate({"name": "V", "access": "shared", "implementation_type": "uint8"})

    @pytest.mark.parametrize(
        "types",
        [{}, {"application_type": "VehicleSpeed", "implementation_type": "uint16"}],
        ids=["neither", "both"],
    )
    def test_both_or_neither_type_field_is_refused(self, types: dict[str, str]) -> None:
        with pytest.raises(ValidationError, match="exactly one of 'application_type' and 'implementation_type'"):
            Variable.model_validate({"name": "V", "access": "implicit", **types})

    @pytest.mark.parametrize("field", ["application_type", "implementation_type"])
    def test_a_blank_type_reference_is_refused(self, field: str) -> None:
        with pytest.raises(ValidationError, match=f"variable {field} must not be blank"):
            Variable.model_validate({"name": "V", "access": "implicit", field: "  "})

    @pytest.mark.parametrize("model", [VariableReadRef, VariableWriteRef])
    def test_a_blank_access_reference_is_refused(self, model: type) -> None:
        with pytest.raises(ValidationError, match="must name a non-blank variable"):
            model.model_validate({"variable": " "})

    def test_a_variable_named_twice_is_refused(self) -> None:
        twice = [
            {"name": "V", "access": "implicit", "implementation_type": "uint8"},
            {"name": "V", "access": "explicit", "implementation_type": "uint8"},
        ]
        with pytest.raises(ValidationError, match="variable named twice in variables: V"):
            SWComponent.model_validate(_component(variables=twice, runnables=[]))

    @pytest.mark.parametrize("list_name", ["reads_variables", "writes_variables"])
    def test_a_variable_listed_twice_in_one_access_list_is_refused(self, list_name: str) -> None:
        with pytest.raises(ValidationError, match=f"duplicate {list_name} entry in runnable: V"):
            Runnable.model_validate({"name": "R", list_name: [{"variable": "V"}, {"variable": "V", "name": "X"}]})

    def test_a_case_only_repeat_is_not_refused_at_load(self) -> None:
        """Exact comparison, as the port access lists' guard is: ``v`` names another variable."""
        runnable = Runnable.model_validate({"name": "R", "reads_variables": [{"variable": "V"}, {"variable": "v"}]})
        assert [entry.variable for entry in runnable.reads_variables] == ["V", "v"]


# What is written out


class TestTheGenericSerializer:
    def test_a_component_without_the_lists_dumps_exactly_as_before(self) -> None:
        component = SWComponent(
            name="A",
            handle_termination_and_restart="no_support",
            runnables=[Runnable(name="R", min_start_interval=0.01)],
        )

        # No key for the three lists, as a component without them always wrote.
        assert element_to_dict(component) == {
            "name": "A",
            "type": "application",
            "ports": [],
            "runnables": [{"name": "R", "min_start_interval": 0.01, "events": []}],
            "handle_termination_and_restart": "no_support",
        }

    def test_the_lists_are_written_when_they_hold_an_entry(self) -> None:
        dumped = element_to_dict(SWComponent.model_validate(_component()))

        assert dumped["variables"] == [{"name": "FilteredSpeed", "access": "implicit", "implementation_type": "uint16"}]
        assert dumped["runnables"][0]["reads_variables"] == [{"variable": "FilteredSpeed"}]
        assert "writes_variables" not in dumped["runnables"][0]
        assert list(dumped).index("variables") == list(dumped).index("runnables") + 1


# A field-identified access list: address, diff key, editor-schema path


class TestTheFieldIdentifiedAccessList:
    @staticmethod
    def _snapshot(component: SWComponent) -> Any:
        """A stand-in answering ``state`` the way ``ModelSnapshot`` does, with one layer populated."""
        registry = production_registry()

        class _Snapshot:
            state = {layer.name: [] for layer in registry.layers} | {"swc": [component]}

        return _Snapshot()

    def test_an_access_entry_is_addressed_by_the_variable_it_names(self) -> None:
        component = SWComponent.model_validate(_component())
        address = addr.Address.from_json(
            {
                "layer": "swc",
                "element": "SpeedEstimator",
                "path": [
                    {"field": "runnables"},
                    {"member": "MonitorSpeed"},
                    {"field": "reads_variables"},
                    {"member": "FilteredSpeed"},
                ],
            }
        )

        result = addr.resolve(address, self._snapshot(component), placement={"group": "chassis"})

        assert result.found, result
        assert result.value == component.runnables[0].reads_variables[0]
        assert result.kind is not None and result.kind.kind == "variable_read"

    def test_a_variable_is_addressed_by_its_name(self) -> None:
        component = SWComponent.model_validate(_component())
        address = addr.Address.from_json(
            {"layer": "swc", "element": "SpeedEstimator", "path": [{"field": "variables"}, {"member": "FilteredSpeed"}]}
        )

        result = addr.resolve(address, self._snapshot(component), placement={"group": "chassis"})

        assert result.found and result.value is component.variables[0]

    @pytest.mark.parametrize(
        ("owner", "field", "item", "key"),
        [
            (SWComponent, "variables", {"name": "V", "access": "implicit", "implementation_type": "u8"}, "V"),
            (Runnable, "reads_variables", {"variable": "V", "name": "RV_Stated"}, "V"),
            (Runnable, "writes_variables", {"variable": "V", "name": None}, "V"),
        ],
    )
    def test_the_diff_key_is_the_identity_field_and_nothing_else(
        self, owner: type, field: str, item: dict[str, Any], key: str
    ) -> None:
        """A stated access name is no identity: renaming it changes the entry, never removes and adds one."""
        rule = collection_rule(owner, field)

        assert rule is not None
        assert rule.key(item) == key
        assert rule.member_identity(item) == key
        renamed = {**item, "name": "Other"} if "variable" in item else item
        assert rule.key(renamed) == key

    def test_the_editor_schema_reaches_the_entry_through_the_runnable(self) -> None:
        document = editor_schema.layer_schema(production_registry().layer("swc"))
        definitions = document["definitions"]

        reads = definitions["runnable"]["properties"]["reads_variables"]
        assert reads["type"] == "array"
        assert reads["items"] == {"$ref": "#/definitions/variable_read"}
        assert definitions["variable_read"]["required"] == ["variable"]
        assert definitions["variable"]["properties"]["access"]["enum"] == ["implicit", "explicit"]
        assert document["properties"]["variables"]["items"] == {"$ref": "#/definitions/variable"}

    def test_navigation_resolves_the_variable_field_into_the_components_variables(self) -> None:
        references = json.loads(editor_schema.references_artifact())
        (entry,) = [ref for ref in references["memberReferences"] if ref["field"] == "field:swc.variable_read.variable"]

        assert entry["paths"] == [{"topKind": "swc", "path": ["runnables", "*", "reads_variables", "*", "variable"]}]
        assert [collection["path"] for collection in entry["collections"]] == [["variables"]]


# One collision, one finding


def test_a_variable_and_a_runnable_of_one_name_are_one_finding(tmp_path: Path) -> None:
    """``rule:name.name-space-collision`` reports a shared name once, and no second rule reports it again.

    The name is shared, exact or case-only, across two containers of the behaviour's name space.
    """
    model_root = tmp_path / "model"
    (model_root / "swc" / "chassis").mkdir(parents=True)
    (model_root / "swc" / "chassis" / "SpeedEstimator.yaml").write_text(
        "name: SpeedEstimator\n"
        "handle_termination_and_restart: no_support\n"
        "variables:\n"
        "  - name: FilterSpeed\n"
        "    access: implicit\n"
        "    implementation_type: uint16\n"
        "runnables:\n"
        "  - name: FilterSpeed\n",
        encoding="utf-8",
        newline="\n",
    )

    records = [
        *validator.check_name_uniqueness(str(model_root)).records,
        *validator.check_name_space_collisions(str(model_root)).records,
    ]
    about = [record for record in records if "FilterSpeed" in record.message]

    assert [record.rule for record in about] == ["name.name-space-collision"]
