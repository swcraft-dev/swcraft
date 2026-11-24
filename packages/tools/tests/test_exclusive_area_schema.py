"""The exclusive areas at the declaration, the loader and the derived views.

A component declares ``exclusive_areas`` (a ``name`` and an optional ``description``),
and a runnable names areas of its own component it ``can_enter``
(``canEnterExclusiveArea``) and ``runs_inside`` (``runsInsideExclusiveArea``), each entry
``{area: <name>}``. Five questions:

* **what the contract declares**: the three member kinds, their fields and lists, the
  rule identifiers with their families and bindings, and the kept rules widened;
* **what is refused at load**: a blank area name or entry, an area named twice, an area
  listed twice in one list;
* **what is written out**: the lists are omitted at their default;
* **how the entry list is addressed** when its identity field is a member reference: the
  protocol address, the ``diff`` key, the editor-schema path and the ``references.json`` edge;
* **one name shared by an area and a runnable is one finding**
  (``rule:name.name-space-collision``; its validator half is ``test_exclusive_area_validator.py``'s).

What this module does **not** reach: no cross-element rule is exercised
(``swc.exclusive-area.unknown-area`` is the validator's tests'); nothing here emits
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
from swcraft_tools.editing.diagnosis import BROKEN_REFERENCE, RULE_CODES
from swcraft_tools.model_registry import production_registry
from swcraft_tools.schema import CanEnterArea, ExclusiveArea, Runnable, RunsInsideArea, SWComponent
from swcraft_tools.writer.elements import element_to_dict

BINDING = Path(__file__).resolve().parents[1] / "src" / "swcraft_tools" / "model_contract" / "binding.json"

#: The one cross-element identifier the areas add. An area's name shared with a
#: runnable, an event or a variable is ``name.name-space-collision``'s, widened.
UNKNOWN_AREA = "swc.exclusive-area.unknown-area"

#: The six element-local identifiers and the method each is bound to. The entry kinds'
#: rule is spelled ``reference-not-blank``, as ``swc.variable_read.reference-not-blank`` is.
ELEMENT_LOCAL = {
    "rule:swc.swc.exclusive_areas.no-duplicate-entry": ("kind:swc.swc", "exclusive_areas_must_not_have_duplicates"),
    "rule:swc.runnable.can_enter.no-duplicate-entry": ("kind:swc.runnable", "can_enter_must_not_have_duplicates"),
    "rule:swc.runnable.runs_inside.no-duplicate-entry": (
        "kind:swc.runnable",
        "runs_inside_must_not_have_duplicates",
    ),
    "rule:swc.exclusive_area.name.not-blank": ("kind:swc.exclusive_area", "name_must_not_be_blank"),
    "rule:swc.can_enter_area.reference-not-blank": ("kind:swc.can_enter_area", "area_must_not_be_blank"),
    "rule:swc.runs_inside_area.reference-not-blank": ("kind:swc.runs_inside_area", "area_must_not_be_blank"),
}

#: Each kept rule this contract widens, and the identifiers it gains.
WIDENED = {
    "rule:name.name-space-collision": {"field:swc.swc.exclusive_areas", "field:swc.exclusive_area.name"},
    "rule:identifier.declared": {"field:swc.exclusive_area.name"},
    "rule:swc.external-with-ports": {"field:swc.swc.exclusive_areas"},
    "rule:text.xml-unrepresentable-character": {"field:swc.exclusive_area.description"},
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
    """A component with areas and both reference lists, as a file's mapping."""
    return {
        "name": "SpeedEstimator",
        "runnables": [
            {"name": "FilterSpeed", "runs_inside": [{"area": "SpeedState"}]},
            {"name": "MonitorSpeed", "runs_inside": [{"area": "SpeedState"}]},
            {"name": "CalibrateOffsets", "can_enter": [{"area": "OffsetTable"}]},
        ],
        "exclusive_areas": [
            {"name": "SpeedState", "description": "Guards the filtered speed."},
            {"name": "OffsetTable"},
        ],
        **fields,
    }


# The declaration


class TestTheContractDeclaresTheAreas:
    def test_the_contract_version_is_the_minor_that_admits_them(self) -> None:
        """The shipped contract version; kinds, fields and rules added without removal are a minor."""
        # The contract version in force; every contract bump moves it.
        assert load_contract()["contractVersion"] == "12.7.1"

    def test_the_area_is_a_member_of_the_components_exclusive_areas(self) -> None:
        kind = _kinds()["kind:swc.exclusive_area"]
        fields = _fields("kind:swc.exclusive_area")

        assert (kind["kind"], kind["memberOnly"]) == ("exclusive_area", True)
        assert kind["memberOf"] == [{"kind": "swc", "field": "exclusive_areas"}]
        assert list(fields) == ["name", "description"]
        assert (fields["name"]["required"], fields["name"]["immutable"]) == (True, True)
        assert (fields["description"]["required"], fields["description"]["default"]) == (False, "")

    @pytest.mark.parametrize(
        ("kind_id", "list_name"),
        [("kind:swc.can_enter_area", "can_enter"), ("kind:swc.runs_inside_area", "runs_inside")],
    )
    def test_each_entry_kind_is_a_member_of_its_own_list(self, kind_id: str, list_name: str) -> None:
        kind = _kinds()[kind_id]
        fields = _fields(kind_id)

        assert kind["memberOf"] == [{"kind": "runnable", "field": list_name}]
        assert list(fields) == ["area"]
        area = fields["area"]
        assert (area["type"], area["required"], area["immutable"]) == ("string", True, True)
        assert area["memberReference"]["collections"] == ["collection:swc.swc.exclusive_areas"]
        assert "via" not in area["memberReference"]

    def test_the_three_lists_sit_where_decision_1_puts_them(self) -> None:
        """The three lists sit in the XSD's order.

        ``exclusive_areas`` after ``variables``; ``can_enter`` then ``runs_inside`` after ``writes_variables``.
        """
        component = list(_fields("kind:swc.swc"))
        runnable = list(_fields("kind:swc.runnable"))

        assert component[component.index("variables") + 1] == "exclusive_areas"
        at = runnable.index("writes_variables")
        assert runnable[at + 1 : at + 3] == ["can_enter", "runs_inside"]

    @pytest.mark.parametrize(
        ("kind_id", "field", "identity", "member"),
        [
            ("kind:swc.swc", "exclusive_areas", "name", "exclusive_area"),
            ("kind:swc.runnable", "can_enter", "area", "can_enter_area"),
            ("kind:swc.runnable", "runs_inside", "area", "runs_inside_area"),
        ],
    )
    def test_each_list_is_field_identified_and_omitted_at_its_default(
        self, kind_id: str, field: str, identity: str, member: str
    ) -> None:
        declared = _fields(kind_id)[field]

        assert (declared["required"], declared["default"], declared["omitAtDefault"]) == (False, [], True)
        assert declared["collection"]["memberIdentity"] == "field"
        assert declared["collection"]["identityField"] == identity
        assert declared["collection"]["memberKinds"] == [member]

    def test_the_one_cross_element_rule_is_declared_summarised_bound_and_coded(self) -> None:
        declared = _rules()[f"rule:{UNKNOWN_AREA}"]

        assert declared["family"] == "cross-element"
        assert "severity" not in declared
        assert declared["summary"] == validator.CONSISTENCY_RULES[UNKNOWN_AREA]
        assert _binding()["rules"][f"rule:{UNKNOWN_AREA}"]["module"].rpartition(".")[0] == "swcraft_tools.validator"
        assert (RULE_CODES[UNKNOWN_AREA].code, RULE_CODES[UNKNOWN_AREA].group) == (BROKEN_REFERENCE, "access-area")
        assert UNKNOWN_AREA not in validator.WARNING_RULES

    def test_no_new_warning_and_no_area_collision_rule_exist(self) -> None:
        """No warning is added; an area named like a runnable, event or variable is ``name.name-space-collision``'s."""
        added = {rule_id for rule_id in _rules() if "exclusive" in rule_id or "area" in rule_id.split(".")[-1]}
        assert all(_rules()[rule_id].get("severity") is None for rule_id in added)
        assert not [rule_id for rule_id in _rules() if rule_id.startswith("rule:name.") and "area" in rule_id]

    @pytest.mark.parametrize("rule_id", sorted(ELEMENT_LOCAL))
    def test_each_element_local_rule_is_bound_to_its_validator(self, rule_id: str) -> None:
        assert _rules()[rule_id]["family"] == "element-local"
        kind, method = ELEMENT_LOCAL[rule_id]
        assert _binding()["rules"][rule_id] == {"kind": kind, "method": method}

    @pytest.mark.parametrize("rule_id", sorted(WIDENED))
    def test_each_widened_rule_claims_the_new_fields(self, rule_id: str) -> None:
        declared = _rules()[rule_id]

        assert WIDENED[rule_id] <= set(declared["appliesTo"])
        assert declared["summary"] == validator.CONSISTENCY_RULES[rule_id.removeprefix("rule:")]

    def test_the_widened_summaries_name_the_areas_where_they_enumerate(self) -> None:
        assert "variables and exclusive areas" in _rules()["rule:name.name-space-collision"]["summary"]
        assert "exclusive areas" in _rules()["rule:swc.external-with-ports"]["summary"]


# What loads, and what is refused at load


class TestTheLoader:
    def test_the_three_kinds_and_three_fields_load(self) -> None:
        component = SWComponent.model_validate(_component())

        assert [area.name for area in component.exclusive_areas] == ["SpeedState", "OffsetTable"]
        assert component.exclusive_areas[0].description == "Guards the filtered speed."
        assert component.exclusive_areas[1].description == ""
        assert component.runnables[0].runs_inside == [RunsInsideArea(area="SpeedState")]
        assert component.runnables[2].can_enter == [CanEnterArea(area="OffsetTable")]
        assert component.runnables[2].runs_inside == []

    def test_one_runnable_may_name_one_area_in_both_roles(self) -> None:
        """No TPS constraint forbids naming one area in both roles, so it loads."""
        runnable = Runnable.model_validate({"name": "R", "can_enter": [{"area": "A"}], "runs_inside": [{"area": "A"}]})
        assert runnable.can_enter[0].area == runnable.runs_inside[0].area == "A"

    def test_an_unused_area_loads(self) -> None:
        component = SWComponent.model_validate(_component(exclusive_areas=[{"name": "Nobody"}], runnables=[]))
        assert component.exclusive_areas == [ExclusiveArea(name="Nobody")]

    def test_a_blank_area_name_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="exclusive area name must not be blank"):
            ExclusiveArea.model_validate({"name": "  "})

    @pytest.mark.parametrize(("model", "list_name"), [(CanEnterArea, "can_enter"), (RunsInsideArea, "runs_inside")])
    def test_a_blank_entry_is_refused(self, model: type, list_name: str) -> None:
        with pytest.raises(ValidationError, match=f"{list_name} entry must name a non-blank exclusive area"):
            model.model_validate({"area": " "})

    def test_an_entry_without_an_area_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="area"):
            RunsInsideArea.model_validate({})

    def test_an_area_named_twice_is_refused(self) -> None:
        twice = [{"name": "A"}, {"name": "A", "description": "again"}]
        with pytest.raises(ValidationError, match="exclusive area named twice in exclusive_areas: A"):
            SWComponent.model_validate(_component(exclusive_areas=twice, runnables=[]))

    @pytest.mark.parametrize("list_name", ["can_enter", "runs_inside"])
    def test_an_area_listed_twice_in_one_list_is_refused(self, list_name: str) -> None:
        with pytest.raises(ValidationError, match=f"duplicate {list_name} entry in runnable: A"):
            Runnable.model_validate({"name": "R", list_name: [{"area": "A"}, {"area": "A"}]})

    def test_a_case_only_repeat_is_not_refused_at_load(self) -> None:
        """Exact comparison: ``a`` names another area, and whether it exists is ``check-model``'s."""
        runnable = Runnable.model_validate({"name": "R", "runs_inside": [{"area": "A"}, {"area": "a"}]})
        assert [entry.area for entry in runnable.runs_inside] == ["A", "a"]


# What is written out


class TestTheGenericSerializer:
    def test_a_component_without_the_lists_dumps_exactly_as_before(self) -> None:
        component = SWComponent(
            name="A",
            handle_termination_and_restart="no_support",
            runnables=[Runnable(name="R", min_start_interval=0.01)],
        )

        # No key for the three lists at their default.
        assert element_to_dict(component) == {
            "name": "A",
            "type": "application",
            "ports": [],
            "runnables": [{"name": "R", "min_start_interval": 0.01, "events": []}],
            "handle_termination_and_restart": "no_support",
        }

    def test_the_lists_are_written_when_they_hold_an_entry(self) -> None:
        dumped = element_to_dict(SWComponent.model_validate(_component()))

        assert dumped["exclusive_areas"] == [
            {"name": "SpeedState", "description": "Guards the filtered speed."},
            {"name": "OffsetTable"},
        ]
        assert dumped["runnables"][0]["runs_inside"] == [{"area": "SpeedState"}]
        assert "can_enter" not in dumped["runnables"][0]
        assert dumped["runnables"][2]["can_enter"] == [{"area": "OffsetTable"}]
        assert list(dumped).index("exclusive_areas") == list(dumped).index("runnables") + 1


# A field-identified entry list whose identity field is a member reference


class TestTheFieldIdentifiedEntryList:
    @staticmethod
    def _snapshot(component: SWComponent) -> Any:
        """A stand-in answering ``state`` the way ``ModelSnapshot`` does, with one layer populated."""
        registry = production_registry()

        class _Snapshot:
            state = {layer.name: [] for layer in registry.layers} | {"swc": [component]}

        return _Snapshot()

    def test_the_identity_field_is_itself_a_member_reference(self) -> None:
        """A ``field``-identity collection whose identity field carries a ``memberReference``."""
        for kind_id, list_field in (
            ("kind:swc.can_enter_area", "can_enter"),
            ("kind:swc.runs_inside_area", "runs_inside"),
        ):
            identity = _fields("kind:swc.runnable")[list_field]["collection"]["identityField"]
            assert "memberReference" in _fields(kind_id)[identity], kind_id

    @pytest.mark.parametrize("list_name", ["runs_inside", "can_enter"])
    def test_an_entry_is_addressed_by_the_area_it_names(self, list_name: str) -> None:
        component = SWComponent.model_validate(_component())
        runnable = "FilterSpeed" if list_name == "runs_inside" else "CalibrateOffsets"
        area = "SpeedState" if list_name == "runs_inside" else "OffsetTable"
        address = addr.Address.from_json(
            {
                "layer": "swc",
                "element": "SpeedEstimator",
                "path": [{"field": "runnables"}, {"member": runnable}, {"field": list_name}, {"member": area}],
            }
        )

        result = addr.resolve(address, self._snapshot(component), placement={"group": "chassis"})

        assert result.found, result
        owner = next(r for r in component.runnables if r.name == runnable)
        assert result.value == getattr(owner, list_name)[0]
        assert result.kind is not None and result.kind.kind == f"{list_name}_area"

    def test_an_area_is_addressed_by_its_name(self) -> None:
        component = SWComponent.model_validate(_component())
        address = addr.Address.from_json(
            {
                "layer": "swc",
                "element": "SpeedEstimator",
                "path": [{"field": "exclusive_areas"}, {"member": "OffsetTable"}],
            }
        )

        result = addr.resolve(address, self._snapshot(component), placement={"group": "chassis"})

        assert result.found and result.value is component.exclusive_areas[1]

    def test_an_address_naming_no_entry_is_not_found(self) -> None:
        component = SWComponent.model_validate(_component())
        address = addr.Address.from_json(
            {
                "layer": "swc",
                "element": "SpeedEstimator",
                "path": [
                    {"field": "runnables"},
                    {"member": "FilterSpeed"},
                    {"field": "runs_inside"},
                    {"member": "OffsetTable"},
                ],
            }
        )

        assert not addr.resolve(address, self._snapshot(component), placement={"group": "chassis"}).found

    @pytest.mark.parametrize(
        ("owner", "field", "item", "key"),
        [
            (SWComponent, "exclusive_areas", {"name": "A", "description": "x"}, "A"),
            (Runnable, "can_enter", {"area": "A"}, "A"),
            (Runnable, "runs_inside", {"area": "A"}, "A"),
        ],
    )
    def test_the_diff_key_is_the_identity_field(self, owner: type, field: str, item: dict[str, Any], key: str) -> None:
        """A changed area description changes the area, never a removal and an addition: it is no identity."""
        rule = collection_rule(owner, field)

        assert rule is not None
        assert rule.key(item) == key
        assert rule.member_identity(item) == key
        if "description" in item:
            assert rule.key({**item, "description": "other"}) == key

    def test_the_editor_schema_reaches_the_entry_through_the_runnable(self) -> None:
        document = editor_schema.layer_schema(production_registry().layer("swc"))
        definitions = document["definitions"]

        for list_name in ("can_enter", "runs_inside"):
            declared = definitions["runnable"]["properties"][list_name]
            assert declared["type"] == "array"
            assert declared["items"] == {"$ref": f"#/definitions/{list_name}_area"}
            assert definitions[f"{list_name}_area"]["required"] == ["area"]
        assert document["properties"]["exclusive_areas"]["items"] == {"$ref": "#/definitions/exclusive_area"}
        assert definitions["exclusive_area"]["required"] == ["name"]

    @pytest.mark.parametrize("list_name", ["can_enter", "runs_inside"])
    def test_navigation_resolves_the_area_field_into_the_components_areas(self, list_name: str) -> None:
        references = json.loads(editor_schema.references_artifact())
        (entry,) = [ref for ref in references["memberReferences"] if ref["field"] == f"field:swc.{list_name}_area.area"]

        assert entry["paths"] == [{"topKind": "swc", "path": ["runnables", "*", list_name, "*", "area"]}]
        assert [collection["path"] for collection in entry["collections"]] == [["exclusive_areas"]]


# One collision, one finding


def test_an_area_and_a_runnable_of_one_name_are_one_finding(tmp_path: Path) -> None:
    """``rule:name.name-space-collision`` reports a shared name once, and no second rule does.

    The behaviour's name space holds runnables, events, variables and areas.
    """
    model_root = tmp_path / "model"
    (model_root / "swc" / "chassis").mkdir(parents=True)
    (model_root / "swc" / "chassis" / "SpeedEstimator.yaml").write_text(
        "name: SpeedEstimator\n"
        "handle_termination_and_restart: no_support\n"
        "runnables:\n"
        "  - name: FilterSpeed\n"
        "exclusive_areas:\n"
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
