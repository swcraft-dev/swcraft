"""Base types are a layer of their own: ``layer:base_types``, stored in ``data_types/base/``.

The layer holds the one ``base_type`` kind and declares no discriminator. Identity is
``(layer, name)``:

* ``value_type.base`` resolves in ``base_types`` **alone** -- a ``base`` naming a type of any
  other layer names an undeclared base type;
* every other type role resolves in ``application_types`` or ``implementation_types``, neither of
  which holds a base type, so the five rules that refused a base type in a role are retired
  (``interface.data-element-base-type``, ``interface.argument-base-type``,
  ``data-type.record-field-base-type``, ``data-type.array-element-base-type``,
  ``data-type.value-type-base-not-base-type``) and their former models are an undeclared name of
  the role's layer;
* ``data-type.duplicate-name`` judges one layer: a base type and a value type of one name load
  side by side, two base types of one name are a repeat.

Does not reach: the application types layer (``test_application_types_layer.py``); the refusal
of a file at a retired path beyond the one ``data_types/`` case read here; and the importers' names,
reserved per layer (``test_importer_names_per_layer.py``).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import model_element_path
from lxml import etree
from swcraft_tools.arxml_version import NAMESPACE
from swcraft_tools.contract import load_contract
from swcraft_tools.model_registry import production_registry
from swcraft_tools.model_state import RetiredStorageError
from swcraft_tools.reader import read_data_types, read_layer
from swcraft_tools.schema import BaseDataType, ValueDataType
from swcraft_tools.validator import CONSISTENCY_RULES, check_cross_layer_consistency
from swcraft_tools.writer.arxml_generation import generate_arxml
from swcraft_tools.writer.mutations import create_data_type

NS = f"{{{NAMESPACE}}}"
TESTS = Path(__file__).resolve().parent
EXCHANGE_MODEL_ROOT = TESTS / "fixtures" / "component_exchange" / "model"

RETIRED_RULES = (
    "interface.data-element-base-type",
    "interface.argument-base-type",
    "data-type.record-field-base-type",
    "data-type.array-element-base-type",
    "data-type.value-type-base-not-base-type",
)

#: The display orders: the seven data description layers take 4-10 in their contract order,
#: the four layers after them 11-14.
FINAL_DISPLAY_ORDERS = {
    "requirements": 0,
    "compositions": 1,
    "swc": 2,
    "interfaces": 3,
    "application_types": 4,
    "implementation_types": 5,
    "base_types": 6,
    "units": 7,
    "compu_methods": 8,
    "data_constraints": 9,
    "mapping_sets": 10,
    "connectors": 11,
    "tasks": 12,
    "sequences": 13,
    "hardware": 14,
}


def _dump(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _model(tmp_path: Path, *types: dict[str, Any], interfaces: tuple[dict[str, Any], ...] = ()) -> Path:
    """The ``component_exchange`` tree with *types* and *interfaces* added, each where its kind is stored."""
    model_root = tmp_path / "model"
    shutil.copytree(EXCHANGE_MODEL_ROOT, model_root)
    for document in types:
        _dump(model_element_path(model_root, document["name"], kind=document["kind"]), document)
    for document in interfaces:
        _dump(model_root / "interfaces" / f"{document['name']}.yaml", document)
    return model_root


def _base_type(name: str) -> dict[str, Any]:
    return {"name": name, "kind": "base_type", "size": 16, "encoding": "NONE", "native_declaration": "uint16"}


def _value_type(name: str, base: str) -> dict[str, Any]:
    return {"name": name, "kind": "value_type", "base": base}


def _records(model_root: Path) -> list[Any]:
    return list(check_cross_layer_consistency(str(model_root)).records)


def _rules(model_root: Path) -> list[str]:
    return [record.rule for record in _records(model_root)]


def _package_of(out: Path, tag: str, name: str) -> list[str]:
    """The package path of every *tag* element named *name* in *out*."""
    found = []
    for node in etree.parse(str(out)).getroot().iter(f"{NS}{tag}"):
        if node.findtext(f"{NS}SHORT-NAME") != name:
            continue
        segments = []
        parent = node.getparent()
        while parent is not None:
            if parent.tag == f"{NS}AR-PACKAGE":
                segments.append(parent.findtext(f"{NS}SHORT-NAME"))
            parent = parent.getparent()
        found.append("/" + "/".join(reversed(segments)))
    return found


# The layer's declaration


class TestTheLayersDeclaration:
    def test_base_types_is_a_layer_stored_in_data_types_base(self) -> None:
        layer = production_registry().layer("base_types")

        assert layer.id == "layer:base_types"
        assert layer.storage.directory == "data_types/base"
        assert layer.storage.nesting == "flat"
        assert layer.token == "base_type"
        assert [kind.id for kind in layer.kinds] == ["kind:base_types.base_type"]
        assert layer.kinds[0].model is BaseDataType

    def test_the_layer_declares_no_discriminator_and_keeps_kind_as_an_ordinary_literal(self) -> None:
        (kind,) = production_registry().layer("base_types").kinds

        assert kind.discriminator_field is None
        assert kind.get_field("kind").enum == ("base_type",)

    def test_the_implementation_types_layer_does_not_declare_the_kind(self) -> None:
        """The implementation types layer holds no ``base_type`` kind."""
        contract = load_contract()
        implementation_types = next(
            layer for layer in contract["layers"] if layer["id"] == "layer:implementation_types"
        )

        assert "base_type" not in {kind["kind"] for kind in implementation_types["kinds"]}
        assert production_registry().kind("base_type").layer == "base_types"

    def test_fifteen_layers_with_their_final_display_orders(self) -> None:
        registry = production_registry()

        assert {name: registry.layer(name).display_order for name in registry.layer_names} == FINAL_DISPLAY_ORDERS

    def test_value_type_base_targets_base_types(self) -> None:
        field = production_registry().kind("value_type").get_field("base")

        assert field.reference is not None
        assert field.reference.target_layer == "base_types"

    @pytest.mark.parametrize("rule", RETIRED_RULES)
    def test_each_retired_rule_is_declared_nowhere(self, rule: str) -> None:
        from swcraft_tools.editing.diagnosis import RULE_CODES

        contract_rules = {entry["id"] for entry in load_contract()["rules"]}

        assert f"rule:{rule}" not in contract_rules
        assert rule not in CONSISTENCY_RULES
        assert rule not in RULE_CODES

    def test_a_base_type_file_directly_in_data_types_does_not_load_there(self, tmp_path: Path) -> None:
        """No layer stores a file directly in ``data_types/``: every read refuses it as a retired layout."""
        _dump(tmp_path / "data_types" / "probe_w16.yaml", _base_type("probe_w16"))

        with pytest.raises(RetiredStorageError):
            read_data_types(str(tmp_path))
        with pytest.raises(RetiredStorageError):
            read_layer(str(tmp_path), "base_types")


# value_type.base resolves in base_types alone; every other role never does


class TestEachRoleResolvesInItsLayer:
    def test_a_value_type_whose_base_names_a_value_type_names_an_undeclared_base(self, tmp_path: Path) -> None:
        model_root = _model(tmp_path, _value_type("ProbeOver", "uint8"))

        rules = _rules(model_root)

        assert rules.count("data-type.value-type-undeclared-base") == 1
        assert "data-type.value-type-base-not-base-type" not in rules

    def test_the_writer_refuses_that_value_type_as_an_undeclared_base(self, tmp_path: Path) -> None:
        model_root = _model(tmp_path, _value_type("ProbeOver", "uint8"))

        with pytest.raises(ValueError, match=r"base type 'uint8' is not declared in data_types/base/"):
            generate_arxml(str(model_root), str(tmp_path / "out.arxml"))

    def test_a_data_element_naming_a_base_type_names_an_undeclared_type(self, tmp_path: Path) -> None:
        interface = {
            "name": "IProbeBase",
            "kind": "sender_receiver",
            "data_elements": [{"name": "level", "implementation_type": "uint8_base"}],
        }
        model_root = _model(tmp_path, interfaces=(interface,))

        rules = _rules(model_root)

        assert "interface.data-element-undeclared-type" in rules
        assert not set(rules) & set(RETIRED_RULES)

    def test_an_argument_naming_a_base_type_names_an_undeclared_type(self, tmp_path: Path) -> None:
        interface = {
            "name": "IProbeCall",
            "kind": "client_server",
            "operations": [
                {
                    "name": "Probe",
                    "arguments": [{"name": "raw", "direction": "in", "implementation_type": "uint8_base"}],
                }
            ],
        }
        model_root = _model(tmp_path, interfaces=(interface,))

        rules = _rules(model_root)

        assert "interface.argument-undeclared-type" in rules
        assert not set(rules) & set(RETIRED_RULES)

    def test_a_record_field_and_an_array_element_naming_a_base_type_name_an_undeclared_type(
        self, tmp_path: Path
    ) -> None:
        record = {"name": "ProbeRecord", "kind": "record", "fields": [{"name": "raw", "type": "uint8_base"}]}
        array = {"name": "ProbeArray", "kind": "array", "element_type": "uint8_base", "length": 2}
        model_root = _model(tmp_path, record, array)

        rules = _rules(model_root)

        assert "data-type.record-field-undeclared-type" in rules
        assert "data-type.array-undeclared-element-type" in rules
        assert not set(rules) & set(RETIRED_RULES)


# A value type and a base type of one name; duplicate-name per layer


class TestOneNameInTwoLayers:
    def test_a_value_type_and_a_base_type_of_one_name_load_side_by_side(self, tmp_path: Path) -> None:
        from swcraft_tools.data_description import read_data_description

        model_root = _model(tmp_path, _base_type("ProbeWord"), _value_type("ProbeWord", "ProbeWord"))

        view = read_data_description(str(model_root))

        assert isinstance(view.base("ProbeWord"), BaseDataType)
        assert isinstance(view.implementation("ProbeWord"), ValueDataType)
        # `application()` answers the application_types layer alone, which declares neither.
        assert view.application("ProbeWord") is None
        assert (model_root / "data_types" / "base" / "ProbeWord.yaml").is_file()
        assert (model_root / "data_types" / "implementation" / "ProbeWord.yaml").is_file()

    def test_check_model_accepts_the_pair_with_no_repeat_and_no_cycle(self, tmp_path: Path) -> None:
        model_root = _model(tmp_path, _base_type("ProbeWord"), _value_type("ProbeWord", "ProbeWord"))

        # The tree declares no hardware layer, which is its own finding and the only one.
        assert [rule for rule in _rules(model_root) if rule != "hardware.layer-absent"] == []

    def test_the_writer_emits_both_each_in_its_package(self, tmp_path: Path) -> None:
        model_root = _model(tmp_path, _base_type("ProbeWord"), _value_type("ProbeWord", "ProbeWord"))
        out = tmp_path / "out.arxml"

        generate_arxml(str(model_root), str(out))

        assert _package_of(out, "SW-BASE-TYPE", "ProbeWord") == ["/DataTypes/Base"]
        assert _package_of(out, "IMPLEMENTATION-DATA-TYPE", "ProbeWord") == ["/DataTypes/Implementation"]
        refs = [node.text for node in etree.parse(str(out)).getroot().iter(f"{NS}BASE-TYPE-REF")]
        assert "/DataTypes/Base/ProbeWord" in refs

    def test_two_base_types_of_one_name_are_still_a_repeat_of_the_base_types_layer(self, tmp_path: Path) -> None:
        model_root = _model(tmp_path)
        _dump(model_root / "data_types" / "base" / "probe_twice.yaml", _base_type("ProbeTwice"))
        _dump(model_root / "data_types" / "base" / "probe_twice~1.yaml", _base_type("ProbeTwice"))

        duplicates = [record for record in _records(model_root) if record.rule == "data-type.duplicate-name"]

        assert duplicates
        assert {(subject.layer, subject.element) for record in duplicates for subject in record.subjects} == {
            ("base_types", "ProbeTwice")
        }
        # The writer's refusal names the layer, as the validator's record does.
        with pytest.raises(ValueError, match=r"Duplicate data type names: 'ProbeTwice' \(base_types\)"):
            generate_arxml(str(model_root), str(tmp_path / "out.arxml"))

    def test_create_data_type_stores_a_base_type_beside_a_value_type_of_its_name(self, tmp_path: Path) -> None:
        model_root = _model(tmp_path, _value_type("ProbeWord", "uint16_base"))

        written = create_data_type(
            str(model_root), "ProbeWord", "base_type", size=16, encoding="NONE", native_declaration="uint16"
        )

        assert Path(written) == model_root / "data_types" / "base" / "ProbeWord.yaml"
        with pytest.raises(ValueError, match="already exists"):
            create_data_type(str(model_root), "ProbeWord", "base_type", size=8, encoding="NONE")
