"""A unit, a compu method and a data constraint are elements of three layers, and a type may name one.

Three layers under ``data_types/`` -- ``units``, ``compu_methods`` and ``data_constraints``
(``data_types/constraints/``) -- hold four element kinds and three member kinds (``table_entry``,
``linear_limits``, ``linear_limit_bound``), sized to what the standard's elements state and to
``[constr_1375]``'s category table:

* ``unit``: ``display_name``, ``description``, ``package``;
* ``texttable`` (a compu method): ``entries`` (``table_entry`` members: ``name``, ``value``,
  ``description``), ``description``, ``package``;
* ``linear`` (a compu method): ``unit`` (required reference), ``factor``, ``offset``, ``limits`` (a
  ``linear_limits`` member, its ``lower``/``upper`` ``linear_limit_bound`` members), ``description``,
  ``package`` -- no scale label, which ``[constr_1375]`` makes not applicable for ``LINEAR``;
* ``data_constr``: ``min``, ``max``, ``level``, ``description``, ``package``.

A type names an element through a reference field exclusive with the inline form: an application
type's ``unit_ref``/``unit``, ``compu_method_ref``/``conversion`` and ``data_constr_ref``/``range``
(at most one of each pair), an enumeration's ``compu_method_ref``/``literals`` (exactly one), and a
value or reference type's ``compu_method_ref`` (no inline counterpart).

This module holds what the loader and the declaration decide (layers, kinds, fields, defaults,
form rules, the view's accessors, the dumps, the ``{enum}_CompuMethod`` reservation), and that
``generate arxml`` writes each element and reference and the component export carries the elements
its closure names, and no other.

**Not reached:** the fields of the two ``linear_limits`` member kinds (``NEW_KINDS`` pins the other
five kinds'); the cross-element rules (``test_data_description_validator.py``; each model here
declares what it names); the elements' XML (``test_data_description_writer.py``); the importers
(``test_data_description_import.py``, ``test_component_data_description.py``).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import model_element_path, write_model_element
from pydantic import ValidationError

TESTS = Path(__file__).resolve().parent
EXCHANGE_MODEL_ROOT = TESTS / "fixtures" / "component_exchange" / "model"

#: The three layers: name -> (directory, display order, discriminator of the top-level kinds).
NEW_LAYERS = {
    "units": ("data_types/units", 7, None),
    "compu_methods": ("data_types/compu_methods", 8, "kind"),
    "data_constraints": ("data_types/constraints", 9, None),
}

#: Kind -> (layer, field names in order).
NEW_KINDS = {
    "unit": ("units", ("name", "display_name", "description", "package")),
    "texttable": ("compu_methods", ("name", "kind", "entries", "description", "package")),
    "table_entry": ("compu_methods", ("name", "value", "description")),
    "linear": ("compu_methods", ("name", "kind", "unit", "factor", "offset", "limits", "description", "package")),
    "data_constr": ("data_constraints", ("name", "min", "max", "level", "description", "package")),
}

#: (layer, kind, field) -> the layer the field names an element of.
REFERENCE_FIELDS = {
    ("application_types", "application_type", "unit_ref"): "units",
    ("application_types", "application_type", "compu_method_ref"): "compu_methods",
    ("application_types", "application_type", "data_constr_ref"): "data_constraints",
    ("application_types", "enum", "compu_method_ref"): "compu_methods",
    ("implementation_types", "value_type", "compu_method_ref"): "compu_methods",
    ("implementation_types", "reference_type", "compu_method_ref"): "compu_methods",
}

#: Every reference field, the linear compu method's ``unit`` included, with the payload of an
#: element that states it and nothing else of its kind.
REFERENCING_PAYLOADS: dict[str, tuple[str, dict[str, Any]]] = {
    "application_type.unit_ref": ("unit_ref", {"name": "Speed", "kind": "application_type"}),
    "application_type.compu_method_ref": ("compu_method_ref", {"name": "Speed", "kind": "application_type"}),
    "application_type.data_constr_ref": ("data_constr_ref", {"name": "Speed", "kind": "application_type"}),
    "enum.compu_method_ref": ("compu_method_ref", {"name": "Gear", "kind": "enum"}),
    "value_type.compu_method_ref": ("compu_method_ref", {"name": "Gear", "kind": "value_type", "base": "uint8"}),
    "reference_type.compu_method_ref": (
        "compu_method_ref",
        {"name": "Gear", "kind": "reference_type", "base": "uint8"},
    ),
    "linear.unit": ("unit", {"name": "Kmh_Compu", "kind": "linear"}),
}


def _dump(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _model(tmp_path: Path) -> Path:
    """A copy of the component exchange tree, which ``generate arxml`` accepts as it stands."""
    model_root = tmp_path / "model"
    shutil.copytree(EXCHANGE_MODEL_ROOT, model_root)
    return model_root


def _data_type(payload: dict[str, Any]) -> Any:
    from swcraft_tools.model_registry import production_registry

    return production_registry().kind(payload["kind"]).model.model_validate(payload)


# The declaration


class TestTheThreeLayersAreDeclared:
    def test_the_model_has_fifteen_layers(self) -> None:
        from swcraft_tools.model_registry import production_registry

        assert len(production_registry().layers) == 15

    @pytest.mark.parametrize("layer", sorted(NEW_LAYERS))
    def test_each_layer_is_stored_under_data_types_flat_named_by_name_and_arxml_bound(self, layer: str) -> None:
        from swcraft_tools.model_registry import production_registry

        facts = production_registry().layer(layer)
        directory, display_order, discriminator = NEW_LAYERS[layer]

        assert facts.storage.directory == directory
        assert facts.storage.nesting == "flat"
        assert facts.identity_field == "name"
        assert facts.arxml_bound is True
        assert facts.display_order == display_order
        assert {kind.discriminator_field for kind in facts.top_level_kinds} == {discriminator}

    def test_the_compu_methods_layer_holds_the_two_categories_and_the_table_entry(self) -> None:
        from swcraft_tools.model_registry import production_registry

        facts = production_registry().layer("compu_methods")

        assert sorted(kind.kind for kind in facts.top_level_kinds) == ["linear", "texttable"]
        assert production_registry().kind("table_entry").layer == "compu_methods"

    @pytest.mark.parametrize("kind", sorted(NEW_KINDS))
    def test_each_kind_declares_exactly_its_fields_in_order(self, kind: str) -> None:
        from swcraft_tools.contract import load_contract

        layer, fields = NEW_KINDS[kind]
        (declared,) = [
            kind_decl
            for layer_decl in load_contract()["layers"]
            if layer_decl["name"] == layer
            for kind_decl in layer_decl["kinds"]
            if kind_decl["kind"] == kind
        ]

        assert tuple(field["name"] for field in declared["fields"]) == fields

    @pytest.mark.parametrize(("owner", "target"), sorted(REFERENCE_FIELDS.items()), ids=lambda value: str(value))
    def test_each_reference_field_names_its_layer_is_optional_and_sits_before_the_description(
        self, owner: tuple[str, str, str], target: str
    ) -> None:
        from swcraft_tools.contract import load_contract

        layer, kind, field_name = owner
        (declared,) = [
            kind_decl
            for layer_decl in load_contract()["layers"]
            if layer_decl["name"] == layer
            for kind_decl in layer_decl["kinds"]
            if kind_decl["kind"] == kind
        ]
        names = [field["name"] for field in declared["fields"]]
        (field,) = [field for field in declared["fields"] if field["name"] == field_name]

        assert field["type"] == "string"
        assert field["required"] is False
        assert field["default"] is None
        assert field["reference"] == {"layer": target, "identityField": "name"}
        assert names.index(field_name) < names.index("description")

    def test_an_enumerations_literals_are_optional(self) -> None:
        from swcraft_tools.contract import load_contract

        (literals,) = [
            field
            for layer_decl in load_contract()["layers"]
            if layer_decl["name"] == "application_types"
            for kind_decl in layer_decl["kinds"]
            if kind_decl["kind"] == "enum"
            for field in kind_decl["fields"]
            if field["name"] == "literals"
        ]

        assert literals["required"] is False
        assert literals["default"] is None

    def test_the_linear_compu_methods_unit_names_a_unit_element_and_is_required(self) -> None:
        from swcraft_tools.contract import load_contract

        (unit,) = [
            field
            for layer_decl in load_contract()["layers"]
            if layer_decl["name"] == "compu_methods"
            for kind_decl in layer_decl["kinds"]
            if kind_decl["kind"] == "linear"
            for field in kind_decl["fields"]
            if field["name"] == "unit"
        ]

        assert unit["required"] is True
        assert unit["reference"] == {"layer": "units", "identityField": "name"}

    @pytest.mark.parametrize(
        "rule",
        [
            "rule:application_types.application_type.unit-form",
            "rule:application_types.application_type.conversion-form",
            "rule:application_types.application_type.range-form",
            "rule:application_types.enum.literals-form",
            "rule:compu_methods.texttable.entries.not-empty",
            "rule:application_types.application_type.unit_ref.not-blank",
            "rule:application_types.application_type.compu_method_ref.not-blank",
            "rule:application_types.application_type.data_constr_ref.not-blank",
            "rule:application_types.enum.compu_method_ref.not-blank",
            "rule:implementation_types.value_type.compu_method_ref.not-blank",
            "rule:implementation_types.reference_type.compu_method_ref.not-blank",
            "rule:compu_methods.linear.unit.not-blank",
            "rule:units.unit.name.not-blank",
            "rule:compu_methods.texttable.name.not-blank",
            "rule:compu_methods.table_entry.name.not-blank",
            "rule:compu_methods.linear.name.not-blank",
            "rule:data_constraints.data_constr.name.not-blank",
            "rule:data_constraints.data_constr.bounds-coherent",
        ],
    )
    def test_each_element_local_rule_is_declared_element_local(self, rule: str) -> None:
        from swcraft_tools.contract import load_contract

        (declared,) = [entry for entry in load_contract()["rules"] if entry["id"] == rule]

        assert declared["family"] == "element-local"


# The kinds: load and defaults


class TestEachKindLoadsWithItsDefaults:
    def test_a_unit_needs_a_name_alone(self) -> None:
        from swcraft_tools.schema import Unit

        unit = Unit.model_validate({"name": "Kmh"})

        assert (unit.display_name, unit.description, unit.package) == ("", "", None)

    def test_a_texttable_holds_its_entries_in_order_each_with_an_empty_description(self) -> None:
        from swcraft_tools.schema import TableEntry, TextTable

        table = TextTable.model_validate(
            {
                "name": "Gear_Table",
                "kind": "texttable",
                "entries": [{"name": "PARK", "value": 0}, {"name": "D", "value": 1}],
            }
        )

        assert [(entry.name, entry.value, entry.description) for entry in table.entries] == [
            ("PARK", 0, ""),
            ("D", 1, ""),
        ]
        assert all(isinstance(entry, TableEntry) for entry in table.entries)
        assert (table.description, table.package) == ("", None)

    def test_a_linear_compu_method_defaults_to_the_identity_and_keeps_an_integer_exact(self) -> None:
        from swcraft_tools.schema import Linear

        identity = Linear.model_validate({"name": "Kmh_Compu", "kind": "linear", "unit": "Kmh"})
        exact = Linear.model_validate(
            {"name": "Big_Compu", "kind": "linear", "unit": "Kmh", "factor": 2, "offset": 18446744073709551615}
        )

        assert (identity.factor, identity.offset, identity.description, identity.package) == (1.0, 0.0, "", None)
        assert (exact.factor, exact.offset) == (2, 18446744073709551615)
        assert isinstance(exact.offset, int)

    @pytest.mark.parametrize(
        ("limits", "coherent"),
        [({"min": 5, "max": 1}, False), ({"min": 1, "max": 1}, True), ({"min": 5}, True), ({"max": -1}, True)],
        ids=["lower-above-upper", "equal", "lower-alone", "upper-alone"],
    )
    def test_a_data_constraints_limits_are_judged_as_an_inline_ranges(
        self, limits: dict[str, int], coherent: bool
    ) -> None:
        """A lower limit above the upper one is refused by reference as it is inline: the two forms are judged alike."""
        from pydantic import ValidationError
        from swcraft_tools.schema import DataConstr, ValueRange

        def accepted(model: type) -> bool:
            try:
                model.model_validate({"name": "Probe_Limits", **limits} if model is DataConstr else limits)
            except ValidationError as error:
                assert "must be <= 'max'" in str(error)
                return False
            return True

        assert (accepted(DataConstr), accepted(ValueRange)) == (coherent, coherent)

    def test_a_data_constraint_states_neither_limit_by_default_and_keeps_an_integer_exact(self) -> None:
        from swcraft_tools.schema import DataConstr

        empty = DataConstr.model_validate({"name": "Speed_Limits"})
        bounded = DataConstr.model_validate({"name": "Count_Limits", "min": 0, "max": 18446744073709551615, "level": 0})

        assert (empty.min, empty.max, empty.level, empty.description, empty.package) == (None, None, None, "", None)
        assert (bounded.min, bounded.max, bounded.level) == (0, 18446744073709551615, 0)

    @pytest.mark.parametrize(
        ("cls", "payload"),
        [
            ("Unit", {"name": "Kmh", "factor": 1}),
            ("TextTable", {"name": "T", "kind": "texttable", "entries": [{"name": "A", "value": 0}], "label": "x"}),
            ("Linear", {"name": "L", "kind": "linear", "unit": "Kmh", "label": "x"}),
            ("DataConstr", {"name": "C", "internal_min": 0}),
        ],
        ids=lambda value: value if isinstance(value, str) else "",
    )
    def test_an_unknown_key_is_refused_rather_than_dropped(self, cls: str, payload: dict[str, Any]) -> None:
        from swcraft_tools import schema

        with pytest.raises(ValidationError):
            getattr(schema, cls).model_validate(payload)

    def test_a_linear_compu_method_without_a_unit_is_refused(self) -> None:
        from swcraft_tools.schema import Linear

        with pytest.raises(ValidationError):
            Linear.model_validate({"name": "Kmh_Compu", "kind": "linear"})

    def test_a_texttable_without_entries_is_refused(self) -> None:
        from swcraft_tools.schema import TextTable

        with pytest.raises(ValidationError):
            TextTable.model_validate({"name": "Gear_Table", "kind": "texttable"})

    def test_an_empty_texttable_is_refused(self) -> None:
        from swcraft_tools.schema import TextTable

        with pytest.raises(ValidationError, match="entries must not be empty"):
            TextTable.model_validate({"name": "Gear_Table", "kind": "texttable", "entries": []})

    @pytest.mark.parametrize("cls", ["Unit", "TextTable", "Linear", "DataConstr"])
    @pytest.mark.parametrize("name", ["", "   ", "a/b", "a\\b", ".."])
    def test_a_blank_or_path_shaped_element_name_is_refused(self, cls: str, name: str) -> None:
        from swcraft_tools import schema

        payload: dict[str, Any] = {
            "Unit": {"name": name},
            "TextTable": {"name": name, "kind": "texttable", "entries": [{"name": "A", "value": 0}]},
            "Linear": {"name": name, "kind": "linear", "unit": "Kmh"},
            "DataConstr": {"name": name},
        }[cls]

        with pytest.raises(ValidationError):
            getattr(schema, cls).model_validate(payload)

    @pytest.mark.parametrize("name", ["", "  "])
    def test_a_blank_table_entry_name_is_refused(self, name: str) -> None:
        from swcraft_tools.schema import TextTable

        with pytest.raises(ValidationError):
            TextTable.model_validate({"name": "T", "kind": "texttable", "entries": [{"name": name, "value": 0}]})

    def test_the_reader_loads_each_layer_from_its_directory(self, tmp_path: Path) -> None:
        from swcraft_tools.reader import read_layer
        from swcraft_tools.schema import DataConstr, Linear, TextTable, Unit

        write_model_element(tmp_path, {"name": "Kmh", "display_name": "km/h"}, layer="units")
        write_model_element(tmp_path, {"name": "Speed_Compu", "kind": "linear", "unit": "Kmh", "factor": 0.1})
        write_model_element(
            tmp_path, {"name": "Gear_Table", "kind": "texttable", "entries": [{"name": "P", "value": 0}]}
        )
        write_model_element(tmp_path, {"name": "Speed_Limits", "min": 0, "max": 400}, layer="data_constraints")

        assert (tmp_path / "data_types" / "units" / "Kmh.yaml").is_file()
        assert (tmp_path / "data_types" / "constraints" / "Speed_Limits.yaml").is_file()
        assert [type(element) for element in read_layer(str(tmp_path), "units")] == [Unit]
        assert sorted(type(element).__name__ for element in read_layer(str(tmp_path), "compu_methods")) == [
            Linear.__name__,
            TextTable.__name__,
        ]
        assert [type(element) for element in read_layer(str(tmp_path), "data_constraints")] == [DataConstr]


# The form rules and the reference fields' not-blank rules


class TestTheFormRules:
    @pytest.mark.parametrize(
        ("inline", "reference"),
        [
            ({"unit": "km/h"}, {"unit_ref": "Kmh"}),
            ({"conversion": {"factor": 0.1}}, {"compu_method_ref": "Speed_Compu"}),
            ({"range": {"min": 0}}, {"data_constr_ref": "Speed_Limits"}),
        ],
        ids=["unit", "conversion", "range"],
    )
    def test_an_application_type_states_at_most_one_form_of_each_property(
        self, inline: dict[str, Any], reference: dict[str, Any]
    ) -> None:
        from swcraft_tools.schema import ApplicationDataType

        base = {"name": "Speed", "kind": "application_type"}

        assert ApplicationDataType.model_validate({**base, **inline})
        assert ApplicationDataType.model_validate({**base, **reference})
        assert ApplicationDataType.model_validate(base)
        with pytest.raises(ValidationError, match="not both"):
            ApplicationDataType.model_validate({**base, **inline, **reference})

    def test_an_application_type_may_name_all_three_elements(self) -> None:
        from swcraft_tools.schema import ApplicationDataType

        dt = ApplicationDataType.model_validate(
            {
                "name": "Speed",
                "kind": "application_type",
                "unit_ref": "Kmh",
                "compu_method_ref": "Speed_Compu",
                "data_constr_ref": "Speed_Limits",
            }
        )

        assert (dt.unit, dt.conversion, dt.range) == ("", None, None)
        assert (dt.unit_ref, dt.compu_method_ref, dt.data_constr_ref) == ("Kmh", "Speed_Compu", "Speed_Limits")

    def test_a_blank_inline_unit_beside_a_unit_reference_is_no_second_form(self) -> None:
        """A blank ``unit`` is no unit (the writer emits nothing for it), so it does not collide with ``unit_ref``."""
        from swcraft_tools.schema import ApplicationDataType

        assert ApplicationDataType.model_validate(
            {"name": "Speed", "kind": "application_type", "unit": " ", "unit_ref": "Kmh"}
        )

    def test_an_enumeration_states_exactly_one_of_literals_and_a_compu_method(self) -> None:
        from swcraft_tools.schema import EnumDataType

        literals = {"literals": [{"name": "PARK", "value": 0}]}
        table = {"compu_method_ref": "Gear_Table"}

        assert EnumDataType.model_validate({"name": "Gear", "kind": "enum", **literals}).compu_method_ref is None
        assert EnumDataType.model_validate({"name": "Gear", "kind": "enum", **table}).literals is None
        with pytest.raises(ValidationError, match="exactly one"):
            EnumDataType.model_validate({"name": "Gear", "kind": "enum", **literals, **table})
        with pytest.raises(ValidationError, match="exactly one"):
            EnumDataType.model_validate({"name": "Gear", "kind": "enum"})

    def test_an_enumeration_that_states_literals_states_at_least_one(self) -> None:
        from swcraft_tools.schema import EnumDataType

        with pytest.raises(ValidationError, match="literals must not be empty"):
            EnumDataType.model_validate({"name": "Gear", "kind": "enum", "literals": []})

    @pytest.mark.parametrize("kind", ["value_type", "reference_type"])
    def test_an_implementation_type_may_name_a_compu_method(self, kind: str) -> None:
        dt = _data_type({"name": "Gear", "kind": kind, "base": "uint8", "compu_method_ref": "Gear_Table"})

        assert dt.compu_method_ref == "Gear_Table"

    @pytest.mark.parametrize("kind", ["implementation_record", "implementation_array", "pointer", "record", "array"])
    def test_no_other_type_kind_names_a_compu_method(self, kind: str) -> None:
        from swcraft_tools.model_registry import production_registry

        assert "compu_method_ref" not in production_registry().kind(kind).model.model_fields

    @pytest.mark.parametrize("case", sorted(REFERENCING_PAYLOADS))
    @pytest.mark.parametrize("value", ["", "   "])
    def test_a_blank_reference_is_refused(self, case: str, value: str) -> None:
        from swcraft_tools.model_registry import production_registry

        field, payload = REFERENCING_PAYLOADS[case]
        model = production_registry().kind(payload["kind"]).model

        with pytest.raises(ValidationError, match="must not be blank"):
            model.model_validate({**payload, field: value})

    @pytest.mark.parametrize("case", sorted(REFERENCING_PAYLOADS))
    def test_a_named_reference_is_accepted(self, case: str) -> None:
        from swcraft_tools.model_registry import production_registry

        field, payload = REFERENCING_PAYLOADS[case]
        model = production_registry().kind(payload["kind"]).model

        assert getattr(model.model_validate({**payload, field: "Named"}), field) == "Named"


# The view


class TestTheViewResolvesEachElementInItsLayer:
    def test_each_accessor_answers_the_element_of_its_layer_alone(self, tmp_path: Path) -> None:
        from swcraft_tools.data_description import read_data_description

        write_model_element(tmp_path, {"name": "Speed", "display_name": "km/h"}, layer="units")
        write_model_element(tmp_path, {"name": "Speed", "kind": "linear", "unit": "Speed"})
        write_model_element(tmp_path, {"name": "Speed"}, layer="data_constraints")
        write_model_element(tmp_path, {"name": "Speed", "kind": "application_type"})

        view = read_data_description(str(tmp_path))

        assert type(view.unit("Speed")).__name__ == "Unit"
        assert type(view.compu_method("Speed")).__name__ == "Linear"
        assert type(view.data_constr("Speed")).__name__ == "DataConstr"
        assert type(view.application("Speed")).__name__ == "ApplicationDataType"
        assert [view.unit(name) for name in ("Absent", None)] == [None, None]

    def test_the_data_types_are_the_three_type_layers_and_nothing_of_the_three_new_ones(self, tmp_path: Path) -> None:
        from swcraft_tools.data_description import read_data_description

        write_model_element(tmp_path, {"name": "Kmh"}, layer="units")
        write_model_element(
            tmp_path, {"name": "Gear_Table", "kind": "texttable", "entries": [{"name": "P", "value": 0}]}
        )
        write_model_element(tmp_path, {"name": "Limits"}, layer="data_constraints")
        write_model_element(tmp_path, {"name": "Speed", "kind": "application_type", "unit_ref": "Kmh"})

        view = read_data_description(str(tmp_path))

        assert [dt.name for dt in view.data_types] == ["Speed"]
        assert [element.name for element in view.units] == ["Kmh"]
        assert [element.name for element in view.compu_methods] == ["Gear_Table"]
        assert [element.name for element in view.data_constraints] == ["Limits"]
        assert [element.name for element in view.elements] == ["Kmh", "Gear_Table", "Limits"]

    def test_a_name_repeated_inside_a_new_layer_resolves_to_its_first_declaration(self) -> None:
        from swcraft_tools.data_description import DataDescription
        from swcraft_tools.schema import Unit

        first, second = Unit(name="Kmh", display_name="km/h"), Unit(name="Kmh", display_name="kph")

        assert DataDescription([first, second]).unit("Kmh") is first
        assert DataDescription.last_declared([first, second]).unit("Kmh") is second
        assert DataDescription([first, second]).units == (first, second)

    @pytest.mark.parametrize(
        ("payload", "layer"),
        [
            ({"name": "P"}, "units"),
            ({"name": "P", "kind": "texttable", "entries": [{"name": "A", "value": 0}]}, "compu_methods"),
            ({"name": "P", "kind": "linear", "unit": "U"}, "compu_methods"),
            ({"name": "P"}, "data_constraints"),
        ],
        ids=["unit", "texttable", "linear", "data_constr"],
    )
    def test_the_validator_answers_each_class_with_its_layer_and_its_package(
        self, payload: dict[str, Any], layer: str
    ) -> None:
        from swcraft_tools.model_registry import production_registry
        from swcraft_tools.schema import DataConstr, Unit
        from swcraft_tools.validator import data_description_package_path, data_type_layer

        model = (
            production_registry().kind(payload["kind"]).model
            if "kind" in payload
            else Unit
            if layer == "units"
            else DataConstr
        )
        element = model.model_validate(payload)
        package = {"units": "Units", "compu_methods": "CompuMethods", "data_constraints": "Constraints"}[layer]

        assert data_type_layer(element) == layer
        assert data_description_package_path(element) == f"DataTypes/{package}"

    def test_the_writers_placement_has_a_home_for_each_layer(self) -> None:
        from swcraft_tools.writer.arxml_xml import PackagePlacement

        assert {home.value for home in PackagePlacement.Home} >= {
            "DataTypes/Units",
            "DataTypes/CompuMethods",
            "DataTypes/Constraints",
        }


# {enum}_CompuMethod is reserved where the enumeration states literals


def _enum(form: str, name: str = "Gear") -> dict[str, Any]:
    if form == "literals":
        return {"name": name, "kind": "enum", "literals": [{"name": "PARK", "value": 0}]}
    return {"name": name, "kind": "enum", "compu_method_ref": "Gear_Table"}


class TestTheEnumerationsCompuMethodIsReservedWhereItStatesLiterals:
    @pytest.mark.parametrize(("form", "collides"), [("literals", True), ("table", False)])
    def test_check_model_judges_the_name_by_the_form(self, tmp_path: Path, form: str, collides: bool) -> None:
        from swcraft_tools.validator import check_cross_layer_consistency

        write_model_element(tmp_path, _enum(form))
        write_model_element(tmp_path, {"name": "Gear_CompuMethod", "kind": "application_type"})
        rules = [record.rule for record in check_cross_layer_consistency(str(tmp_path)).records]

        assert ("data-type.compu-method-name-collision" in rules) is collides

    @pytest.mark.parametrize(("form", "collides"), [("literals", True), ("table", False)])
    def test_create_data_type_judges_the_name_by_the_form(self, tmp_path: Path, form: str, collides: bool) -> None:
        from swcraft_tools.writer.mutations import ReservedNameError, create_data_type

        write_model_element(tmp_path, _enum(form))
        if collides:
            with pytest.raises(ReservedNameError, match="Gear_CompuMethod"):
                create_data_type(str(tmp_path), "Gear_CompuMethod", "application_type")
        else:
            create_data_type(str(tmp_path), "Gear_CompuMethod", "application_type")

    @pytest.mark.parametrize(("form", "claims"), [("literals", ("Gear_CompuMethod",)), ("table", ())])
    def test_the_package_claims_carry_the_generated_form_of_an_enumeration_with_literals_alone(
        self, form: str, claims: tuple[str, ...]
    ) -> None:
        from swcraft_tools.validator import root_package_members

        members = root_package_members([], [], [_data_type(_enum(form))], [], [], [])

        (gear,) = [member for member in members["DataTypes/Application"] if member.name == "Gear"]
        assert gear.generated == claims

    @pytest.mark.parametrize(("form", "charged"), [("literals", True), ("table", False)])
    def test_the_editing_protocols_identifier_diagnosis_charges_the_form_by_the_enumerations_form(
        self, form: str, charged: bool
    ) -> None:
        from swcraft_tools.editing.diagnosis import _MEMBERS_AND_GENERATED_FORMS

        long_name = "E" * 120  # legal itself; its `_CompuMethod` is 132 characters
        violations = _MEMBERS_AND_GENERATED_FORMS["application_types"](_enum(form, long_name))

        assert any("_CompuMethod" in violation.message for violation in violations) is charged

    @pytest.mark.parametrize(("form", "reserved"), [("literals", True), ("table", False)])
    def test_the_writers_predicate_answers_the_form(self, form: str, reserved: bool) -> None:
        from swcraft_tools.writer.arxml_generation import (
            COMPU_METHOD_SUFFIX,
            RESERVED_DATA_TYPE_RULES,
            generates_kind_form,
        )

        dt = _data_type(_enum(form))

        assert [suffix for suffix, _what, kinds in RESERVED_DATA_TYPE_RULES if generates_kind_form(dt, kinds)] == (
            [COMPU_METHOD_SUFFIX] if reserved else []
        )


# The dumps (a hand-written dump never erases a field)


class TestTheDumpsWriteTheReferenceFields:
    @pytest.mark.parametrize("case", sorted(case for case in REFERENCING_PAYLOADS if case != "linear.unit"))
    def test_the_hand_written_dump_writes_a_stated_reference_before_the_description(self, case: str) -> None:
        from swcraft_tools.writer.yaml_documents import data_type_to_dict

        field, payload = REFERENCING_PAYLOADS[case]
        dumped = data_type_to_dict(_data_type({**payload, field: "Named", "description": "text"}))

        assert dumped[field] == "Named"
        assert list(dumped).index(field) < list(dumped).index("description")

    @pytest.mark.parametrize("case", sorted(case for case in REFERENCING_PAYLOADS if case != "linear.unit"))
    def test_the_hand_written_dump_equals_the_derived_serializer(self, case: str) -> None:
        from swcraft_tools.writer.elements import element_to_dict
        from swcraft_tools.writer.yaml_documents import data_type_to_dict

        field, payload = REFERENCING_PAYLOADS[case]
        dt = _data_type({**payload, field: "Named", "description": "text", "package": "Pkg"})

        assert data_type_to_dict(dt) == element_to_dict(dt)

    @pytest.mark.parametrize("case", sorted(case for case in REFERENCING_PAYLOADS if case != "linear.unit"))
    def test_an_unset_reference_is_left_out(self, case: str) -> None:
        from swcraft_tools.writer.yaml_documents import data_type_to_dict

        field, payload = REFERENCING_PAYLOADS[case]
        if payload["kind"] == "enum":
            payload = {**payload, "literals": [{"name": "PARK", "value": 0}]}

        assert field not in data_type_to_dict(_data_type(payload))

    def test_an_enumeration_that_names_a_table_writes_no_literals(self) -> None:
        from swcraft_tools.writer.yaml_documents import data_type_to_dict

        assert data_type_to_dict(_data_type(_enum("table"))) == {
            "name": "Gear",
            "kind": "enum",
            "compu_method_ref": "Gear_Table",
        }

    @pytest.mark.parametrize(
        ("kind", "arguments"),
        [
            ("application_type", {"unit_ref": "Kmh", "compu_method_ref": "Speed_Compu", "data_constr_ref": "Limits"}),
            ("enum", {"compu_method_ref": "Gear_Table"}),
            ("value_type", {"base": "uint8", "compu_method_ref": "Gear_Table"}),
            ("reference_type", {"base": "uint8", "compu_method_ref": "Gear_Table"}),
        ],
    )
    def test_create_data_type_preserves_each_reference_field(
        self, tmp_path: Path, kind: str, arguments: dict[str, Any]
    ) -> None:
        from swcraft_tools.writer.mutations import create_data_type

        path = create_data_type(str(tmp_path), "Probe", kind, description="text", **arguments)
        written = yaml.safe_load(Path(path).read_text(encoding="utf-8"))

        assert {field: written[field] for field in arguments} == arguments
        assert (
            _data_type(written).model_dump()
            == _data_type({"name": "Probe", "kind": kind, "description": "text", **arguments}).model_dump()
        )

    def test_the_component_closures_materialization_preserves_each_reference_field(self, tmp_path: Path) -> None:
        from swcraft_tools.component_exchange.closure import ComponentClosure
        from swcraft_tools.component_exchange.export import _materialize_closure
        from swcraft_tools.schema import SWComponent

        speed = _data_type(
            {
                "name": "Speed",
                "kind": "application_type",
                "unit_ref": "Kmh",
                "compu_method_ref": "C",
                "data_constr_ref": "L",
            }
        )
        gear = _data_type(_enum("table"))
        closure = ComponentClosure(
            swc=SWComponent(name="Probe", handle_termination_and_restart="no_support"), data_types=[gear, speed]
        )
        _materialize_closure(closure, tmp_path)

        assert yaml.safe_load(
            model_element_path(tmp_path, "Speed", kind="application_type").read_text(encoding="utf-8")
        ) == {
            "name": "Speed",
            "kind": "application_type",
            "unit_ref": "Kmh",
            "compu_method_ref": "C",
            "data_constr_ref": "L",
        }
        assert yaml.safe_load(model_element_path(tmp_path, "Gear", kind="enum").read_text(encoding="utf-8")) == {
            "name": "Gear",
            "kind": "enum",
            "compu_method_ref": "Gear_Table",
        }


# Each model with an element or a reference is written

#: One model change per case, each adding an element or a reference field.
GUARDED_CASES: dict[str, tuple[str, dict[str, Any]]] = {
    "units": ("units", {"name": "Kmh", "display_name": "km/h"}),
    "compu_methods": (
        "compu_methods",
        {"name": "Gear_Table", "kind": "texttable", "entries": [{"name": "P", "value": 0}]},
    ),
    "data_constraints": ("data_constraints", {"name": "Limits", "min": 0}),
    "unit_ref": ("application_types", {"name": "Velocity_T", "kind": "application_type", "unit_ref": "Kmh"}),
    "compu_method_ref": (
        "application_types",
        {"name": "Distance_T", "kind": "application_type", "compu_method_ref": "C"},
    ),
    "data_constr_ref": (
        "application_types",
        {"name": "Distance_T", "kind": "application_type", "data_constr_ref": "L"},
    ),
    "enum.compu_method_ref": (
        "application_types",
        {"name": "Gear_T", "kind": "enum", "compu_method_ref": "Gear_Table"},
    ),
    "value_type.compu_method_ref": (
        "implementation_types",
        {"name": "uint8", "kind": "value_type", "base": "uint8_base", "compu_method_ref": "Gear_Table"},
    ),
    # `Speed_T` becomes a reference type over the value type `float32` ([constr_1383]: its chain ends in a
    # value type), still mapped and still in the closure.
    "reference_type.compu_method_ref": (
        "implementation_types",
        {"name": "Speed_T", "kind": "reference_type", "base": "float32", "compu_method_ref": "Gear_Table"},
    ),
}


#: The elements each reference case names, declared beside it, so that the model breaks none of
#: ``check-model``'s rules of the elements, which the writer refuses.
GUARDED_TARGETS: dict[str, tuple[tuple[str, dict[str, Any]], ...]] = {
    "unit_ref": (("units", {"name": "Kmh", "display_name": "km/h"}),),
    "compu_method_ref": (
        ("units", {"name": "Kmh", "display_name": "km/h"}),
        ("compu_methods", {"name": "C", "kind": "linear", "unit": "Kmh"}),
    ),
    "data_constr_ref": (("data_constraints", {"name": "L", "min": 0}),),
    "enum.compu_method_ref": (
        ("compu_methods", {"name": "Gear_Table", "kind": "texttable", "entries": [{"name": "P", "value": 0}]}),
    ),
    "value_type.compu_method_ref": (
        ("compu_methods", {"name": "Gear_Table", "kind": "texttable", "entries": [{"name": "P", "value": 0}]}),
    ),
    "reference_type.compu_method_ref": (
        ("compu_methods", {"name": "Gear_Table", "kind": "texttable", "entries": [{"name": "P", "value": 0}]}),
    ),
}


def _guarded_model(tmp_path: Path, case: str) -> Path:
    model_root = _model(tmp_path)
    layer, payload = GUARDED_CASES[case]
    # An application or implementation type replaces the fixture's element of its name.
    _dump(model_element_path(model_root, payload["name"], layer=layer), payload)
    for target_layer, target in GUARDED_TARGETS.get(case, ()):
        _dump(model_element_path(model_root, target["name"], layer=target_layer), target)
    return model_root


#: Per case, the path the written document holds for it: the element itself for a
#: layer case, the referenced element (and a reference naming it) for a field case.
WRITTEN_PATHS: dict[str, str] = {
    "units": "/DataTypes/Units/Kmh",
    "compu_methods": "/DataTypes/CompuMethods/Gear_Table",
    "data_constraints": "/DataTypes/Constraints/Limits",
    "unit_ref": "/DataTypes/Units/Kmh",
    "compu_method_ref": "/DataTypes/CompuMethods/C",
    "data_constr_ref": "/DataTypes/Constraints/L",
    "enum.compu_method_ref": "/DataTypes/CompuMethods/Gear_Table",
    "value_type.compu_method_ref": "/DataTypes/CompuMethods/Gear_Table",
    "reference_type.compu_method_ref": "/DataTypes/CompuMethods/Gear_Table",
}

#: The cases that state a reference field; the three others add an element alone.
REFERENCE_CASES = sorted(case for case in GUARDED_CASES if case not in NEW_LAYERS)


class TestTheWriterWritesWhatItsInterimGuardRefused:
    def test_the_unchanged_fixture_generates(self, tmp_path: Path) -> None:
        from swcraft_tools.writer.arxml_generation import generate_arxml

        out = tmp_path / "out" / "model.arxml"
        generate_arxml(str(_model(tmp_path)), str(out))

        assert out.is_file()

    @pytest.mark.parametrize("case", sorted(GUARDED_CASES))
    def test_generate_arxml_writes_the_element_and_the_reference(self, tmp_path: Path, case: str) -> None:
        from lxml import etree
        from swcraft_tools.writer.arxml_generation import generate_arxml
        from swcraft_tools.writer.arxml_xml import find_element

        model_root = _guarded_model(tmp_path, case)
        out = tmp_path / "out" / "model.arxml"

        generate_arxml(str(model_root), str(out))

        path = WRITTEN_PATHS[case]
        assert find_element(etree.parse(str(out)).getroot(), path) is not None
        if case in REFERENCE_CASES:
            assert f">{path}</" in out.read_text(encoding="utf-8")

    @pytest.mark.parametrize("case", REFERENCE_CASES)
    def test_the_component_export_writes_the_element_and_the_reference(self, tmp_path: Path, case: str) -> None:
        """The closure follows each reference field into its layer, so the file carries the element it names."""
        from lxml import etree
        from swcraft_tools.component_exchange import export_component
        from swcraft_tools.writer.arxml_xml import find_element

        model_root = _guarded_model(tmp_path, case)
        out = tmp_path / "out.arxml"

        export_component(str(model_root), "SensorSwc", str(out))

        path = WRITTEN_PATHS[case]
        assert find_element(etree.parse(str(out)).getroot(), path) is not None
        assert f">{path}</" in out.read_text(encoding="utf-8")
        assert not Path(f"{out}.tmp").exists()

    @pytest.mark.parametrize("case", sorted(NEW_LAYERS))
    def test_the_component_export_is_not_blocked_by_an_element_its_closure_does_not_name(
        self, tmp_path: Path, case: str
    ) -> None:
        """An element of the three layers that no closure type names stays out of the file and blocks nothing.

        The component file carries the closure, never the whole project.
        """
        from swcraft_tools.component_exchange import export_component

        model_root = _guarded_model(tmp_path, case)
        out = tmp_path / "out.arxml"

        export_component(str(model_root), "SensorSwc", str(out))

        written = out.read_text(encoding="utf-8")
        assert all(
            f"<SHORT-NAME>{package}</SHORT-NAME>" not in written for package in ("Units", "CompuMethods", "Constraints")
        )

    @pytest.mark.parametrize("case", sorted(GUARDED_CASES))
    def test_the_element_rules_load_the_model_and_find_it_consistent(self, tmp_path: Path, case: str) -> None:
        """The cross-element rules of the three element layers find every such model consistent."""
        from swcraft_tools.validator import check_data_description_elements

        result = check_data_description_elements(str(_guarded_model(tmp_path, case)))

        assert result.consistent, result.issues
