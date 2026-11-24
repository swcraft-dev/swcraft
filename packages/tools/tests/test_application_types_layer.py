"""Application types are a layer of their own.

The application kinds and their member kinds belong to ``layer:application_types``
(``data_types/application/``); identity is ``(layer, name)``:

* a data element and an argument state the side of their type: exactly one of ``application_type`` and
  ``implementation_type``, each resolving in its own layer alone;
* every other application role resolves in ``application_types`` alone and every implementation role in
  ``implementation_types`` alone; a name of the other layer is undeclared;
* ``data-type.duplicate-name`` judges one layer: one name in both layers is two elements, each emitted in
  its own package.

What this does **not** reach: the importers and the component exchange; the editing protocol's addresses
of an application type; and a retired-path refusal beyond an application-kind file directly in
``data_types/``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import model_element_path
from lxml import etree
from pydantic import ValidationError
from swcraft_tools.arxml_version import NAMESPACE
from swcraft_tools.contract import load_contract
from swcraft_tools.data_description import read_data_description
from swcraft_tools.editing.diagnosis import RULE_CODES
from swcraft_tools.model_registry import production_registry
from swcraft_tools.model_state import RetiredStorageError
from swcraft_tools.reader import read_data_types, read_layer
from swcraft_tools.schema import (
    ApplicationDataType,
    ArrayDataType,
    CSArgument,
    DataElement,
    EnumDataType,
    RecordDataType,
    ValueDataType,
)
from swcraft_tools.validator import CONSISTENCY_RULES, check_cross_layer_consistency
from swcraft_tools.writer.arxml_generation import generate_arxml
from swcraft_tools.writer.mutations import create_cs_interface, create_data_type, create_interface

NS = f"{{{NAMESPACE}}}"
TESTS = Path(__file__).resolve().parent
EXCHANGE_MODEL_ROOT = TESTS / "fixtures" / "component_exchange" / "model"

RETIRED_RULES = (
    "data-type.composite-member-implementation-type",
    "data-type.reference-type-base-not-implementation-type",
    "data-type.implementation-element-not-implementation-type",
    "data-type.implementation-array-element-not-implementation-type",
    "data-type.pointer-target-not-implementation-type",
)

#: The display order of each of the fifteen layers.
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

#: A synthetic name both layers declare in :class:`TestOneNameInTwoLayers`.
SHARED = "ProbeShared"


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


def _records(model_root: Path) -> list[Any]:
    # The tree declares no hardware layer, which is its own finding; it is no subject here.
    return [
        record
        for record in check_cross_layer_consistency(str(model_root)).records
        if record.rule != "hardware.layer-absent"
    ]


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
    return sorted(found)


def _in_package(out: Path, tag: str, package: str, name: str) -> etree._Element:
    """The one *tag* element named *name* whose enclosing package path is *package*."""
    (node,) = [
        node
        for node in etree.parse(str(out)).getroot().iter(f"{NS}{tag}")
        if node.findtext(f"{NS}SHORT-NAME") == name
        and "/"
        + "/".join(
            ancestor.findtext(f"{NS}SHORT-NAME") for ancestor in reversed(list(node.iterancestors(f"{NS}AR-PACKAGE")))
        )
        == package
    ]
    return node


def _constraint_ref(out: Path, tag: str, package: str, name: str) -> str | None:
    """The ``DATA-CONSTR-REF`` path the *tag* type *name* of *package* carries."""
    (ref,) = _in_package(out, tag, package, name).iter(f"{NS}DATA-CONSTR-REF")
    assert ref.get("DEST") == "DATA-CONSTR"
    return ref.text


def _constraint(out: Path, package: str, name: str) -> tuple[str, str | None, str | None]:
    """The ``(form, lower, upper)`` of the one ``DATA-CONSTR-RULE`` of constraint *name* in *package*."""
    (rule,) = _in_package(out, "DATA-CONSTR", package, name).iter(f"{NS}DATA-CONSTR-RULE")
    (form,) = list(rule)
    return (
        etree.QName(form).localname,
        form.findtext(f"{NS}LOWER-LIMIT"),
        form.findtext(f"{NS}UPPER-LIMIT"),
    )


def _type_tref(out: Path, prototype: str) -> tuple[str | None, str | None]:
    """The ``(DEST, path)`` of the ``TYPE-TREF`` of the data prototype named *prototype*."""
    (node,) = [
        node
        for node in etree.parse(str(out)).getroot().iter(f"{NS}VARIABLE-DATA-PROTOTYPE")
        if node.findtext(f"{NS}SHORT-NAME") == prototype
    ]
    tref = node.find(f"{NS}TYPE-TREF")
    return tref.get("DEST"), tref.text


# The layer's declaration


class TestTheLayersDeclaration:
    def test_application_types_is_a_layer_stored_in_data_types_application(self) -> None:
        layer = production_registry().layer("application_types")

        assert layer.id == "layer:application_types"
        assert layer.storage.directory == "data_types/application"
        assert layer.storage.nesting == "flat"
        assert layer.token == "application_type"
        assert layer.label == "Application Types"

    def test_the_layer_is_discriminated_by_kind_over_the_four_application_kinds(self) -> None:
        layer = production_registry().layer("application_types")

        assert [kind.kind for kind in layer.top_level_kinds] == ["enum", "record", "array", "application_type"]
        assert [kind.model for kind in layer.top_level_kinds] == [
            EnumDataType,
            RecordDataType,
            ArrayDataType,
            ApplicationDataType,
        ]
        assert all(kind.discriminator_field == "kind" for kind in layer.top_level_kinds)
        # The scale limits: a conversion's `limits` and its bound, declared after
        # the conversion they fill.
        assert [kind.kind for kind in layer.kinds if kind.member_only] == [
            "enum_literal",
            "record_field",
            "conversion",
            "conversion_limits",
            "conversion_limit_bound",
            "range",
        ]

    def test_the_implementation_types_layer_declares_none_of_the_eight_kinds(self) -> None:
        """The eight application kinds live in ``application_types`` alone."""
        moved = {"application_type", "enum", "record", "array", "enum_literal", "record_field", "conversion", "range"}
        contract = load_contract()
        implementation_types = next(
            layer for layer in contract["layers"] if layer["id"] == "layer:implementation_types"
        )

        assert not moved & {kind["kind"] for kind in implementation_types["kinds"]}
        assert {production_registry().kind(kind).layer for kind in moved} == {"application_types"}

    def test_fifteen_layers_with_their_final_display_orders_and_the_layers_written_last(self) -> None:
        registry = production_registry()

        assert {name: registry.layer(name).display_order for name in registry.layer_names} == FINAL_DISPLAY_ORDERS
        # The contract order: the data description layers follow `connectors`,
        # `mapping_sets` is written after them, and the three layers of the forms
        # a type names by reference are written after `mapping_sets`.
        assert [layer["name"] for layer in load_contract()["layers"]][-4:] == [
            "mapping_sets",
            "units",
            "compu_methods",
            "data_constraints",
        ]

    @pytest.mark.parametrize(
        ("kind", "field"),
        [("record_field", "type"), ("array", "element_type"), ("map", "application")],
    )
    def test_the_application_roles_target_application_types(self, kind: str, field: str) -> None:
        reference = production_registry().kind(kind).get_field(field).reference

        assert reference is not None
        assert reference.target_layer == "application_types"

    @pytest.mark.parametrize("rule", RETIRED_RULES)
    def test_each_retired_rule_is_declared_nowhere(self, rule: str) -> None:
        contract_rules = {entry["id"] for entry in load_contract()["rules"]}

        assert f"rule:{rule}" not in contract_rules
        assert rule not in CONSISTENCY_RULES
        assert rule not in RULE_CODES

    def test_an_application_kind_file_directly_in_data_types_does_not_load_there(self, tmp_path: Path) -> None:
        """A file directly in ``data_types/`` is a retired layout: the model is refused, not loaded there."""
        _dump(
            tmp_path / "data_types" / "ProbeMode.yaml",
            {"name": "ProbeMode", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]},
        )

        with pytest.raises(RetiredStorageError, match=r"1 element file in 'data_types'"):
            read_data_types(str(tmp_path))
        with pytest.raises(RetiredStorageError, match=r"1 element file in 'data_types'"):
            read_layer(str(tmp_path), "application_types")


# Each role resolves in its own layer


class TestEachRoleResolvesInItsLayer:
    def test_a_record_field_naming_an_implementation_type_names_an_undeclared_type(self, tmp_path: Path) -> None:
        record = {"name": "ProbeRecord", "kind": "record", "fields": [{"name": "raw", "type": "uint8"}]}
        model_root = _model(tmp_path, record)

        rules = _rules(model_root)

        assert rules == ["data-type.record-field-undeclared-type"]
        assert not set(rules) & set(RETIRED_RULES)
        with pytest.raises(ValueError, match=r"'uint8' referenced by record 'ProbeRecord' field 'raw' is not declared"):
            generate_arxml(str(model_root), str(tmp_path / "out.arxml"))

    def test_an_array_element_naming_an_implementation_type_names_an_undeclared_type(self, tmp_path: Path) -> None:
        array = {"name": "ProbeArray", "kind": "array", "element_type": "uint8", "length": 2}
        model_root = _model(tmp_path, array)

        (record,) = _records(model_root)

        assert record.rule == "data-type.array-undeclared-element-type"
        assert record.message.endswith("in the application_types layer.")
        assert record.subjects[0].layer == "application_types"
        with pytest.raises(ValueError, match=r"'uint8' referenced by array 'ProbeArray' element type is not declared"):
            generate_arxml(str(model_root), str(tmp_path / "out.arxml"))

    def test_a_prototypes_type_naming_an_application_type_names_an_undeclared_type(self, tmp_path: Path) -> None:
        interfaces = (
            {
                "name": "IProbeSr",
                "kind": "sender_receiver",
                "data_elements": [{"name": "x", "implementation_type": "Gear_T"}],
            },
            {
                "name": "IProbeCs",
                "kind": "client_server",
                "operations": [
                    {"name": "Op", "arguments": [{"name": "a", "direction": "in", "implementation_type": "Gear_T"}]}
                ],
            },
        )
        model_root = _model(tmp_path, interfaces=interfaces)

        records = _records(model_root)

        assert sorted(record.rule for record in records) == [
            "interface.argument-undeclared-type",
            "interface.data-element-undeclared-type",
        ]
        assert {record.subjects[0].path[-1].value for record in records} == {"implementation_type"}
        with pytest.raises(ValueError, match=r"Data type 'Gear_T' not declared in data_types/implementation/"):
            generate_arxml(str(model_root), str(tmp_path / "out.arxml"))

    def test_a_prototypes_application_type_naming_an_implementation_type_names_an_undeclared_type(
        self, tmp_path: Path
    ) -> None:
        interfaces = (
            {
                "name": "IProbeSr",
                "kind": "sender_receiver",
                "data_elements": [{"name": "x", "application_type": "uint8"}],
            },
            {
                "name": "IProbeCs",
                "kind": "client_server",
                "operations": [
                    {"name": "Op", "arguments": [{"name": "a", "direction": "in", "application_type": "uint8"}]}
                ],
            },
        )
        model_root = _model(tmp_path, interfaces=interfaces)

        records = _records(model_root)

        assert sorted(record.rule for record in records) == [
            "interface.argument-undeclared-type",
            "interface.data-element-undeclared-type",
        ]
        # The finding addresses the field that is set.
        assert {record.subjects[0].path[-1].value for record in records} == {"application_type"}
        with pytest.raises(ValueError, match=r"Data type 'uint8' not declared in data_types/application/"):
            generate_arxml(str(model_root), str(tmp_path / "out.arxml"))


# The two type fields of a prototype


class TestTheTwoTypeFields:
    @pytest.mark.parametrize("fields", [{}, {"implementation_type": "uint8", "application_type": "Gear_T"}])
    def test_exactly_one_of_the_two_is_set(self, fields: dict[str, str]) -> None:
        with pytest.raises(
            ValidationError, match="must declare exactly one of 'application_type' and 'implementation_type'"
        ):
            DataElement(name="x", **fields)
        with pytest.raises(
            ValidationError, match="must declare exactly one of 'application_type' and 'implementation_type'"
        ):
            CSArgument(name="x", direction="in", **fields)

    def test_application_type_must_not_be_blank(self) -> None:
        with pytest.raises(ValidationError, match="application_type must not be blank"):
            DataElement(name="x", application_type="  ")
        with pytest.raises(ValidationError, match="application_type must not be blank"):
            CSArgument(name="x", direction="in", application_type="  ")

    def test_create_interface_writes_the_set_field_in_the_position_type_held(self, tmp_path: Path) -> None:
        model_root = _model(tmp_path)

        written = create_interface(
            str(model_root),
            "IProbeSide",
            [{"name": "phys", "application_type": "Gear_T"}, {"name": "impl", "implementation_type": "uint8"}],
        )

        document = yaml.safe_load(Path(written).read_text(encoding="utf-8"))
        assert [list(element) for element in document["data_elements"]] == [
            ["name", "application_type"],
            ["name", "implementation_type"],
        ]
        assert document["data_elements"][0]["application_type"] == "Gear_T"
        assert document["data_elements"][1]["implementation_type"] == "uint8"
        assert _rules(model_root) == []

    def test_create_cs_interface_writes_the_set_field_in_the_position_type_held(self, tmp_path: Path) -> None:
        model_root = _model(tmp_path)

        written = create_cs_interface(
            str(model_root),
            "IProbeCall",
            [
                {
                    "name": "Op",
                    "arguments": [
                        {"name": "a", "direction": "in", "application_type": "Gear_T"},
                        {"name": "b", "direction": "out", "implementation_type": "uint8"},
                    ],
                }
            ],
        )

        document = yaml.safe_load(Path(written).read_text(encoding="utf-8"))
        arguments = document["operations"][0]["arguments"]
        assert [list(argument) for argument in arguments] == [
            ["name", "direction", "application_type"],
            ["name", "direction", "implementation_type"],
        ]
        assert (arguments[0]["application_type"], arguments[1]["implementation_type"]) == ("Gear_T", "uint8")
        assert _rules(model_root) == []


# One name in two layers


def _shared_pair() -> tuple[dict[str, Any], dict[str, Any]]:
    """An application type and a value type of one name, EACH stating its bounds inline."""
    application = {"name": SHARED, "kind": "application_type", "range": {"min": 0.0, "max": 10.0}}
    value = {"name": SHARED, "kind": "value_type", "base": "uint8_base", "value_min": 0, "value_max": 20}
    return application, value


def _shared_interface() -> dict[str, Any]:
    return {
        "name": "IProbeShared",
        "kind": "sender_receiver",
        "data_elements": [
            {"name": "phys", "application_type": SHARED},
            {"name": "impl", "implementation_type": SHARED},
        ],
    }


class TestOneNameInTwoLayers:
    def test_the_pair_loads_side_by_side(self, tmp_path: Path) -> None:
        model_root = _model(tmp_path, *_shared_pair())

        view = read_data_description(str(model_root))

        assert isinstance(view.application(SHARED), ApplicationDataType)
        assert isinstance(view.implementation(SHARED), ValueDataType)
        assert (model_root / "data_types" / "application" / f"{SHARED}.yaml").is_file()
        assert (model_root / "data_types" / "implementation" / f"{SHARED}.yaml").is_file()

    def test_check_model_accepts_the_pair_with_no_repeat(self, tmp_path: Path) -> None:
        model_root = _model(tmp_path, *_shared_pair(), interfaces=(_shared_interface(),))

        assert _rules(model_root) == []

    def test_the_writer_emits_both_each_in_its_package_with_its_own_range(self, tmp_path: Path) -> None:
        model_root = _model(tmp_path, *_shared_pair(), interfaces=(_shared_interface(),))
        out = tmp_path / "out.arxml"

        generate_arxml(str(model_root), str(out))

        assert _package_of(out, "APPLICATION-PRIMITIVE-DATA-TYPE", SHARED) == ["/DataTypes/Application"]
        assert _package_of(out, "IMPLEMENTATION-DATA-TYPE", SHARED) == ["/DataTypes/Implementation"]
        assert _package_of(out, "DATA-CONSTR", f"{SHARED}_Range") == [
            "/DataTypes/Application",
            "/DataTypes/Implementation",
        ]
        # Written and referenced apart: each type's DATA-CONSTR-REF reaches the
        # constraint of its own package, and that constraint carries its own
        # owner's bounds (the application's 0..10, the value type's 0..20) in its
        # own owner's form (physical against internal).
        assert _constraint_ref(out, "APPLICATION-PRIMITIVE-DATA-TYPE", "/DataTypes/Application", SHARED) == (
            f"/DataTypes/Application/{SHARED}_Range"
        )
        assert _constraint_ref(out, "IMPLEMENTATION-DATA-TYPE", "/DataTypes/Implementation", SHARED) == (
            f"/DataTypes/Implementation/{SHARED}_Range"
        )
        assert _constraint(out, "/DataTypes/Application", f"{SHARED}_Range") == ("PHYS-CONSTRS", "0", "10")
        assert _constraint(out, "/DataTypes/Implementation", f"{SHARED}_Range") == ("INTERNAL-CONSTRS", "0", "20")

    def test_each_prototype_references_the_type_of_its_side(self, tmp_path: Path) -> None:
        model_root = _model(tmp_path, *_shared_pair(), interfaces=(_shared_interface(),))
        out = tmp_path / "out.arxml"

        generate_arxml(str(model_root), str(out))

        assert _type_tref(out, "phys") == ("APPLICATION-PRIMITIVE-DATA-TYPE", f"/DataTypes/Application/{SHARED}")
        assert _type_tref(out, "impl") == ("IMPLEMENTATION-DATA-TYPE", f"/DataTypes/Implementation/{SHARED}")

    def test_two_application_types_of_one_name_are_still_a_repeat_of_the_application_types_layer(
        self, tmp_path: Path
    ) -> None:
        model_root = _model(tmp_path)
        enum = {"name": "ProbeTwice", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]}
        _dump(model_root / "data_types" / "application" / "probe_twice.yaml", enum)
        _dump(model_root / "data_types" / "application" / "probe_twice~1.yaml", enum)

        duplicates = [record for record in _records(model_root) if record.rule == "data-type.duplicate-name"]

        assert duplicates
        assert {(subject.layer, subject.element) for record in duplicates for subject in record.subjects} == {
            ("application_types", "ProbeTwice")
        }
        # The writer's refusal names the layer, as the validator's record does.
        with pytest.raises(ValueError, match=r"Duplicate data type names: 'ProbeTwice' \(application_types\)"):
            generate_arxml(str(model_root), str(tmp_path / "out.arxml"))

    def test_create_data_type_stores_an_application_type_beside_a_value_type_of_its_name(self, tmp_path: Path) -> None:
        _application, value = _shared_pair()
        model_root = _model(tmp_path, value)

        written = create_data_type(str(model_root), SHARED, "enum", literals=[{"name": "OFF", "value": 0}])

        assert Path(written) == model_root / "data_types" / "application" / f"{SHARED}.yaml"
        assert (model_root / "data_types" / "implementation" / f"{SHARED}.yaml").is_file()
        with pytest.raises(ValueError, match="already exists"):
            create_data_type(str(model_root), SHARED, "enum", literals=[{"name": "ON", "value": 1}])


def _retype_port(model_root: Path, component: str, port: str, interface: str) -> None:
    """Point *component*'s *port* at *interface*, rewriting the component's file in place."""
    (path,) = (model_root / "swc").glob(f"*/{component}.yaml")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    (entry,) = [entry for entry in document["ports"] if entry["name"] == port]
    entry["interface"] = interface
    _dump(path, document)


def _speed_interface(name: str, side: str) -> dict[str, Any]:
    """A sender-receiver interface whose one element ``speed`` names :data:`SHARED` on *side*."""
    return {"name": name, "kind": "sender_receiver", "data_elements": [{"name": "speed", side: SHARED}]}


class TestTheSameNamePairOnPorts:
    """The pair put on ports: each prototype is judged by the side it states.

    Neither component names a mapping set, so an application type reaching either port is unmapped.
    """

    def test_an_implementation_typed_element_does_not_raise_the_unmapped_finding(self, tmp_path: Path) -> None:
        """Only ``application_type`` is mapped: the value type of the same name needs no map."""
        model_root = _model(
            tmp_path, *_shared_pair(), interfaces=(_speed_interface("IVehicleSpeed", "implementation_type"),)
        )

        assert "data-type.application-type-unmapped" not in _rules(model_root)

    def test_the_same_element_on_the_application_side_raises_it_for_each_component(self, tmp_path: Path) -> None:
        model_root = _model(
            tmp_path, *_shared_pair(), interfaces=(_speed_interface("IVehicleSpeed", "application_type"),)
        )

        unmapped = [record for record in _records(model_root) if record.rule == "data-type.application-type-unmapped"]

        assert sorted((record.subjects[0].layer, record.subjects[0].element) for record in unmapped) == [
            ("swc", "ActuatorSwc"),
            ("swc", "SensorSwc"),
        ]
        assert all(f"speaks type '{SHARED}'" in record.message for record in unmapped)

    @pytest.mark.parametrize(
        ("provided_side", "required_side"),
        [("application_type", "implementation_type"), ("implementation_type", "application_type")],
    )
    def test_a_connector_between_the_two_sides_is_an_interface_mismatch(
        self, tmp_path: Path, provided_side: str, required_side: str
    ) -> None:
        """``connector.interface-mismatch`` reaches the predicate through the model, memo and all."""
        model_root = _model(
            tmp_path,
            *_shared_pair(),
            interfaces=(
                _speed_interface("IProbeProvided", provided_side),
                _speed_interface("IProbeRequired", required_side),
            ),
        )
        _retype_port(model_root, "SensorSwc", "pSpeed", "IProbeProvided")
        _retype_port(model_root, "ActuatorSwc", "rSpeed", "IProbeRequired")

        mismatches = [record for record in _records(model_root) if record.rule == "connector.interface-mismatch"]

        kind = {"application_type": "application_type", "implementation_type": "value_type"}
        assert len(mismatches) == 1
        assert mismatches[0].message.endswith(
            f"data element 'speed': type '{SHARED}' vs '{SHARED}' — "
            f"kind {kind[required_side]} vs {kind[provided_side]}."
        )

    @pytest.mark.parametrize("side", ["application_type", "implementation_type"])
    def test_a_connector_on_one_side_is_no_mismatch(self, tmp_path: Path, side: str) -> None:
        model_root = _model(
            tmp_path,
            *_shared_pair(),
            interfaces=(_speed_interface("IProbeProvided", side), _speed_interface("IProbeRequired", side)),
        )
        _retype_port(model_root, "SensorSwc", "pSpeed", "IProbeProvided")
        _retype_port(model_root, "ActuatorSwc", "rSpeed", "IProbeRequired")

        assert "connector.interface-mismatch" not in _rules(model_root)
