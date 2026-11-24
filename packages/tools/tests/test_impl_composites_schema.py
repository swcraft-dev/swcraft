"""The implementation composites and the pointer, declared, loaded, read and serialized.

AUTOSAR defines three implementation categories beside the scalar ones, declared as:

* ``implementation_record`` -- ``STRUCTURE``, ordered ``elements`` of the member kind
  ``implementation_element`` (``name``, ``type``);
* ``implementation_array`` -- ``ARRAY`` with fixed-size semantics, ``element_type``,
  ``length`` and ``element_name`` (the one sub-element's own ``SHORT-NAME``, kept rather
  than invented);
* ``pointer`` -- ``DATA_REFERENCE``, one ``target``.

This module asks what the contract declares, what loads and what does not, what the ARXML
reader keeps and what the serializer writes.

What this module does **not** reach: no cross-element rule is exercised. Whether an
element's type resolves, which kind it resolves to, whether a map pairing a composite is
compatible, whether a pointer is mapped or crosses an application-to-application link, and
whether a pointer graph closes a cycle are ``check-model``'s questions, tested with the
rules that answer them. Nothing here emits ARXML, imports a type from the reader's record,
or edits a model through the protocol.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import validator_package
from model_contract_fixtures import bound_classes, load_binding, load_contract
from pydantic import ValidationError
from swcraft_tools import reader
from swcraft_tools.arxml_reader import ImplDataTypeInfo, read_arxml
from swcraft_tools.editing import diagnosis
from swcraft_tools.schema import (
    ImplementationArrayDataType,
    ImplementationElement,
    ImplementationRecordDataType,
    PointerDataType,
)
from swcraft_tools.validator import CONSISTENCY_RULES
from swcraft_tools.writer.yaml_documents import data_type_to_dict

#: The three new top-level kinds, each with its class and a minimal valid body.
NEW_KINDS: tuple[tuple[str, type, dict[str, Any]], ...] = (
    (
        "implementation_record",
        ImplementationRecordDataType,
        {"elements": [{"name": "Left", "type": "uint16_T"}, {"name": "Right", "type": "uint16_T"}]},
    ),
    (
        "implementation_array",
        ImplementationArrayDataType,
        {"element_type": "uint16_T", "length": 8, "element_name": "Element"},
    ),
    ("pointer", PointerDataType, {"target": "Motor_T"}),
)

#: The cross-element rules contract `7.8.0` declares and contract `8.0.0` keeps,
#: with the validator check that reports each and the function of the check's
#: module `binding.json` binds it to: the check itself, or the step of it that
#: mints the rule.
NEW_CROSS_ELEMENT_RULES: dict[str, tuple[str, str]] = {
    "rule:data-type.implementation-element-undeclared-type": (
        "check_data_type_references",
        "_implementation_record_records",
    ),
    "rule:data-type.implementation-record-duplicate-element-name": (
        "check_data_type_references",
        "_implementation_record_records",
    ),
    "rule:data-type.implementation-array-undeclared-element-type": (
        "check_data_type_references",
        "_implementation_array_records",
    ),
    "rule:data-type.pointer-undeclared-target": ("check_data_type_references", "_pointer_type_records"),
    "rule:mapping-set.incompatible-composite-map": ("check_mapping_set_references", "check_mapping_set_references"),
    "rule:mapping-set.pointer-mapped": ("check_mapping_set_references", "check_mapping_set_references"),
    "rule:connector.pointer-between-application-components": (
        "check_connector_consistency",
        "check_connector_consistency",
    ),
}

#: The element-local rules, each with the kind and the validator method it is bound to.
NEW_ELEMENT_LOCAL_RULES: dict[str, tuple[str, str]] = {
    "rule:implementation_types.implementation_record.name.not-blank": (
        "kind:implementation_types.implementation_record",
        "name_must_not_be_blank",
    ),
    "rule:implementation_types.implementation_record.elements.not-empty": (
        "kind:implementation_types.implementation_record",
        "elements_must_not_be_empty",
    ),
    "rule:implementation_types.implementation_element.name.not-blank": (
        "kind:implementation_types.implementation_element",
        "name_must_not_be_blank",
    ),
    "rule:implementation_types.implementation_element.type.not-blank": (
        "kind:implementation_types.implementation_element",
        "type_must_not_be_blank",
    ),
    "rule:implementation_types.implementation_array.name.not-blank": (
        "kind:implementation_types.implementation_array",
        "name_must_not_be_blank",
    ),
    "rule:implementation_types.implementation_array.element_type.not-blank": (
        "kind:implementation_types.implementation_array",
        "element_type_must_not_be_blank",
    ),
    "rule:implementation_types.implementation_array.element_name.not-blank": (
        "kind:implementation_types.implementation_array",
        "element_name_must_not_be_blank",
    ),
    "rule:implementation_types.implementation_array.length.positive": (
        "kind:implementation_types.implementation_array",
        "length_must_be_positive",
    ),
    "rule:implementation_types.pointer.name.not-blank": ("kind:implementation_types.pointer", "name_must_not_be_blank"),
    "rule:implementation_types.pointer.target-not-blank": ("kind:implementation_types.pointer", "validate_pointer"),
}

#: The kinds an implementation composite's member and a pointer's target may name.
IMPLEMENTATION_KINDS = (
    "kind:implementation_types.value_type",
    "kind:implementation_types.reference_type",
    "kind:implementation_types.implementation_record",
    "kind:implementation_types.implementation_array",
    "kind:implementation_types.pointer",
    "kind:implementation_types.external_type",
)

#: The three new reference fields.
NEW_REFERENCE_FIELDS = (
    "field:implementation_types.implementation_element.type",
    "field:implementation_types.implementation_array.element_type",
    "field:implementation_types.pointer.target",
)

#: The three role rules contract `7.8.0` declared for the new reference fields,
#: and the two older wrong-kind rules it widened to reach the new kinds, all five
#: retired by contract `8.0.0`: each role resolves in one layer, so a name of the
#: other side is the role's undeclared name and no wrong-kind rule is left.
#: Keyed by the retired id; the value is the field its undeclared rule now covers
#: (the two older rules' fields are not this contract's).
RETIRED_ROLE_RULES: dict[str, str | None] = {
    "rule:data-type.implementation-element-not-implementation-type": (
        "field:implementation_types.implementation_element.type"
    ),
    "rule:data-type.implementation-array-element-not-implementation-type": (
        "field:implementation_types.implementation_array.element_type"
    ),
    "rule:data-type.pointer-target-not-implementation-type": "field:implementation_types.pointer.target",
    "rule:data-type.reference-type-base-not-implementation-type": None,
    "rule:data-type.composite-member-implementation-type": None,
}

#: What each widened rule gains, added and never replacing an entry (an ``appliesTo``
#: entry added is additive). The record and the array kinds belong to `application_types`.
WIDENED: dict[str, tuple[str, ...]] = {
    "rule:data-type.cycle": NEW_REFERENCE_FIELDS,
    "rule:mapping-set.unknown-application": ("kind:application_types.record", "kind:application_types.array"),
    "rule:mapping-set.unknown-implementation": (
        "kind:implementation_types.implementation_record",
        "kind:implementation_types.implementation_array",
        "kind:implementation_types.pointer",
    ),
    "rule:interface.init-shape-mismatch": (
        "kind:implementation_types.implementation_record",
        "kind:implementation_types.implementation_array",
        "kind:implementation_types.pointer",
    ),
    "rule:interface.init-record-field-mismatch": (
        "field:implementation_types.implementation_record.elements",
        "field:implementation_types.implementation_element.name",
    ),
    "rule:interface.init-array-length-mismatch": ("field:implementation_types.implementation_array.length",),
    "rule:interface.init-on-external-type": (
        "field:implementation_types.implementation_element.type",
        "field:implementation_types.implementation_array.element_type",
    ),
}


def _kinds() -> dict[str, dict[str, Any]]:
    return {kind["id"]: kind for layer in load_contract()["layers"] for kind in layer["kinds"]}


def _fields(kind_id: str) -> dict[str, dict[str, Any]]:
    return {field["name"]: field for field in _kinds()[kind_id]["fields"]}


def _rules() -> dict[str, dict[str, Any]]:
    return {rule["id"]: rule for rule in load_contract()["rules"]}


# The declaration


class TestTheContractDeclaresTheKinds:
    def test_the_contract_version_is_the_minor_that_adds_them(self) -> None:
        """The kinds were added without a removal, a minor; the assertion is on the shipped version."""
        # Every contract bump moves this pin.
        assert load_contract()["contractVersion"] == "12.7.1"

    @pytest.mark.parametrize(("kind", "model_class", "body"), NEW_KINDS)
    def test_each_kind_is_a_top_level_kind_discriminated_by_kind(
        self, kind: str, model_class: type, body: dict[str, Any]
    ) -> None:
        declared = _kinds()[f"kind:implementation_types.{kind}"]

        assert (declared["kind"], declared["discriminatorField"], declared["memberOnly"]) == (kind, "kind", False)

    @pytest.mark.parametrize(("kind", "model_class", "body"), NEW_KINDS)
    def test_each_kind_is_bound_to_its_class(self, kind: str, model_class: type, body: dict[str, Any]) -> None:
        assert bound_classes()[f"kind:implementation_types.{kind}"] is model_class

    def test_the_member_kind_is_bound_and_member_only(self) -> None:
        declared = _kinds()["kind:implementation_types.implementation_element"]

        assert declared["memberOnly"] is True
        assert declared["memberOf"] == [{"kind": "implementation_record", "field": "elements"}]
        assert bound_classes()["kind:implementation_types.implementation_element"] is ImplementationElement

    def test_the_implementation_record_declares_its_fields_in_order(self) -> None:
        fields = _fields("kind:implementation_types.implementation_record")

        # Contract 7.10.0 appended the optional `package`, last.
        assert list(fields) == ["name", "kind", "elements", "description", "package"]
        assert fields["elements"]["type"] == "list[implementation_element]"
        assert fields["elements"]["collection"] == {
            "kind": "list",
            "memberIdentity": "field",
            "memberKinds": ["implementation_element"],
            "identityField": "name",
        }
        assert fields["elements"]["required"] is True

    def test_the_member_kind_declares_a_name_and_a_type_reference(self) -> None:
        fields = _fields("kind:implementation_types.implementation_element")

        # Contract 7.10.0 appended the optional `description`, last.
        assert list(fields) == ["name", "type", "description"]
        assert (fields["name"]["required"], fields["name"]["immutable"]) == (True, True)
        assert fields["type"]["reference"] == {"layer": "implementation_types", "identityField": "name"}

    def test_the_implementation_array_declares_its_fields_in_order(self) -> None:
        fields = _fields("kind:implementation_types.implementation_array")

        # Contract 7.10.0 appended the optional `package`, last.
        assert list(fields) == ["name", "kind", "element_type", "length", "element_name", "description", "package"]
        assert fields["element_type"]["reference"] == {"layer": "implementation_types", "identityField": "name"}
        assert (fields["length"]["type"], fields["length"]["required"]) == ("integer", True)
        assert (fields["element_name"]["type"], fields["element_name"]["required"]) == ("string", True)

    def test_the_pointer_declares_its_fields_in_order(self) -> None:
        fields = _fields("kind:implementation_types.pointer")

        # Contract 7.10.0 appended the optional `package`, last.
        assert list(fields) == ["name", "kind", "target", "description", "package"]
        assert fields["target"]["reference"] == {"layer": "implementation_types", "identityField": "name"}
        assert fields["target"]["required"] is True

    def test_no_new_field_is_omitted_on_disk_at_its_default(self) -> None:
        """No field of the three kinds is omitted on disk at its default."""
        omitted = [field["id"] for kind in _kinds().values() for field in kind["fields"] if field.get("omitAtDefault")]
        # Every ``omitAtDefault`` field of the contract; a contract change that adds or drops one moves it.
        assert len(omitted) == 38, omitted
        assert not any(
            field_id.startswith(("field:implementation_types.implementation", "field:implementation_types.pointer"))
            for field_id in omitted
        )

    def test_the_layer_description_counts_the_kinds(self) -> None:
        layer = next(layer for layer in load_contract()["layers"] if layer["name"] == "implementation_types")
        top_level = [kind["kind"] for kind in layer["kinds"] if not kind["memberOnly"]]
        members = [kind["kind"] for kind in layer["kinds"] if kind["memberOnly"]]

        # `base_type` lies in the `base_types` layer and the four application
        # kinds, with their member kinds, in `application_types`.
        assert (len(top_level), len(members)) == (6, 1)
        assert layer["description"].startswith("Implementation data types, one file each under")
        assert "Six element kinds discriminated by `kind`" in layer["description"]
        for kind in (*top_level, *members):
            assert f"`{kind}`" in layer["description"], kind


class TestTheContractDeclaresTheRules:
    @pytest.mark.parametrize(("rule_id", "symbols"), sorted(NEW_CROSS_ELEMENT_RULES.items()))
    def test_each_cross_element_rule_is_declared_bound_and_summarised_by_the_validator(
        self, rule_id: str, symbols: tuple[str, str]
    ) -> None:
        """Bound in the check's module to the function that mints it, which the check reaches."""
        check, bound = symbols
        rule = _rules()[rule_id]
        binding = load_binding()["rules"][rule_id]

        assert rule["family"] == "cross-element"
        assert rule["summary"] == CONSISTENCY_RULES[rule_id.removeprefix("rule:")]
        defining = importlib.import_module(vars(importlib.import_module("swcraft_tools.validator"))[check].__module__)
        assert (binding["module"], binding["callable"]) == (defining.__name__, bound)
        assert bound in validator_package.reached_functions(defining, check)

    @pytest.mark.parametrize("rule_id", sorted(NEW_CROSS_ELEMENT_RULES))
    def test_each_cross_element_rule_carries_a_protocol_code(self, rule_id: str) -> None:
        assert rule_id.removeprefix("rule:") in diagnosis.RULE_CODES

    @pytest.mark.parametrize(
        "rule_id",
        [
            "rule:data-type.implementation-element-undeclared-type",
            "rule:data-type.implementation-array-undeclared-element-type",
            "rule:data-type.pointer-undeclared-target",
        ],
    )
    def test_an_undeclared_name_is_a_broken_reference_in_the_data_type_group(self, rule_id: str) -> None:
        mapping = diagnosis.RULE_CODES[rule_id.removeprefix("rule:")]

        assert (mapping.code, mapping.group) == (diagnosis.BROKEN_REFERENCE, "data-type")

    @pytest.mark.parametrize(("rule_id", "field_id"), sorted(RETIRED_ROLE_RULES.items()))
    def test_each_retired_role_rule_is_declared_nowhere(self, rule_id: str, field_id: str | None) -> None:
        """No retired role rule is declared (contract `8.0.0`).

        Its field belongs to its undeclared rule, which resolves in `implementation_types` alone.
        """
        assert rule_id not in _rules()
        assert rule_id not in load_binding()["rules"]
        assert rule_id.removeprefix("rule:") not in CONSISTENCY_RULES
        assert rule_id.removeprefix("rule:") not in diagnosis.RULE_CODES
        if field_id is not None:
            (undeclared,) = [
                rule
                for rule in NEW_CROSS_ELEMENT_RULES
                if "undeclared" in rule and field_id in _rules()[rule]["appliesTo"]
            ]
            assert _rules()[undeclared]["appliesTo"] == [field_id, "layer:implementation_types"]
            # Every kind the retired rule admitted is a kind of that layer, and nothing else is.
            layer = next(layer for layer in load_contract()["layers"] if layer["name"] == "implementation_types")
            assert {kind["id"] for kind in layer["kinds"] if not kind["memberOnly"]} == set(IMPLEMENTATION_KINDS)

    @pytest.mark.parametrize(("rule_id", "bound"), sorted(NEW_ELEMENT_LOCAL_RULES.items()))
    def test_each_element_local_rule_is_bound_to_its_validator(self, rule_id: str, bound: tuple[str, str]) -> None:
        rule = _rules()[rule_id]
        binding = load_binding()["rules"][rule_id]

        assert rule["family"] == "element-local"
        assert (binding["kind"], binding["method"]) == bound
        assert hasattr(bound_classes()[bound[0]], bound[1])

    @pytest.mark.parametrize(("rule_id", "added"), sorted(WIDENED.items()))
    def test_each_widened_rule_gains_the_new_identifiers(self, rule_id: str, added: tuple[str, ...]) -> None:
        applies = _rules()[rule_id]["appliesTo"]

        missing = [entry for entry in added if entry not in applies]
        assert not missing, missing
        assert len(applies) == len(set(applies)), applies

    def test_the_mandatory_mapping_rule_is_not_widened(self) -> None:
        """Records and arrays stay optional to map: ``application-type-unmapped`` keeps its two application kinds."""
        applies = _rules()["rule:data-type.application-type-unmapped"]["appliesTo"]

        assert "kind:application_types.record" not in applies
        assert "kind:application_types.array" not in applies

    def test_no_rule_is_declared_for_a_recursive_pointer_graph_alone(self) -> None:
        """One cause, one finding: ``data-type.cycle`` covers a pointer graph."""
        assert not [rule_id for rule_id in _rules() if "recursive" in rule_id or "pointer-cycle" in rule_id]


# The loader


class TestEachKindLoads:
    @pytest.mark.parametrize(("kind", "model_class", "body"), NEW_KINDS)
    def test_through_its_class(self, kind: str, model_class: type, body: dict[str, Any]) -> None:
        loaded = model_class.model_validate({"name": "Frame_T", "kind": kind, **body})

        assert (loaded.name, loaded.kind, loaded.description) == ("Frame_T", kind, "")

    def test_the_record_keeps_its_elements_in_order(self) -> None:
        loaded = ImplementationRecordDataType.model_validate(
            {
                "name": "Motor_T",
                "kind": "implementation_record",
                "elements": [{"name": "Right", "type": "b_T"}, {"name": "Left", "type": "a_T"}],
            }
        )

        assert [(element.name, element.type) for element in loaded.elements] == [("Right", "b_T"), ("Left", "a_T")]

    def test_a_pointer_to_a_named_pointer_loads(self) -> None:
        """A pointer to a pointer is declared through a named type, never inline."""
        loaded = PointerDataType.model_validate({"name": "FramePtrPtr_T", "kind": "pointer", "target": "FramePtr_T"})

        assert loaded.target == "FramePtr_T"

    def test_through_the_reader(self, tmp_path: Path) -> None:
        directory = tmp_path / "data_types" / "implementation"
        directory.mkdir(parents=True)
        files = {
            "Motor_T": (
                "name: Motor_T\nkind: implementation_record\nelements:\n"
                "  - name: Left\n    type: uint16_T\n  - name: Right\n    type: uint16_T\n"
            ),
            "Samples_T": "name: Samples_T\nkind: implementation_array\nelement_type: uint16_T\nlength: 8\n"
            "element_name: Element\n",
            "MotorPtr_T": "name: MotorPtr_T\nkind: pointer\ntarget: Motor_T\n",
        }
        for name, text in files.items():
            (directory / f"{name}.yaml").write_text(text, encoding="utf-8", newline="\n")

        loaded = {element.name: element for element in reader.read_data_types(str(tmp_path))}

        assert isinstance(loaded["Motor_T"], ImplementationRecordDataType)
        assert isinstance(loaded["Samples_T"], ImplementationArrayDataType)
        assert isinstance(loaded["MotorPtr_T"], PointerDataType)
        assert (loaded["Samples_T"].length, loaded["Samples_T"].element_name) == (8, "Element")


class TestEachKindRefuses:
    @pytest.mark.parametrize(("kind", "model_class", "body"), NEW_KINDS)
    @pytest.mark.parametrize("name", ["", "   "])
    def test_a_blank_name(self, kind: str, model_class: type, body: dict[str, Any], name: str) -> None:
        with pytest.raises(ValidationError, match="name must not be blank"):
            model_class.model_validate({"name": name, "kind": kind, **body})

    @pytest.mark.parametrize(("kind", "model_class", "body"), NEW_KINDS)
    def test_a_name_carrying_a_path_separator(self, kind: str, model_class: type, body: dict[str, Any]) -> None:
        with pytest.raises(ValidationError, match="must not contain path separators"):
            model_class.model_validate({"name": "a/b", "kind": kind, **body})

    @pytest.mark.parametrize(("kind", "model_class", "body"), NEW_KINDS)
    def test_an_undeclared_key(self, kind: str, model_class: type, body: dict[str, Any]) -> None:
        """``extra="forbid"``: a typo is a load failure, not a key dropped in silence."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            model_class.model_validate({"name": "Frame_T", "kind": kind, "unit": "km/h", **body})

    @pytest.mark.parametrize(
        ("kind", "model_class", "sibling_key"),
        [
            ("implementation_record", ImplementationRecordDataType, "fields"),
            ("implementation_array", ImplementationArrayDataType, "base"),
            ("pointer", PointerDataType, "base"),
        ],
    )
    def test_a_sibling_kinds_key(self, kind: str, model_class: type, sibling_key: str) -> None:
        body = dict(next(entry[2] for entry in NEW_KINDS if entry[0] == kind))
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            model_class.model_validate({"name": "Frame_T", "kind": kind, sibling_key: "x", **body})

    def test_an_undeclared_key_on_an_element(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ImplementationElement.model_validate({"name": "Left", "type": "a_T", "init": 0})

    def test_an_empty_elements_list(self) -> None:
        with pytest.raises(ValidationError, match="elements must not be empty"):
            ImplementationRecordDataType.model_validate(
                {"name": "M_T", "kind": "implementation_record", "elements": []}
            )

    @pytest.mark.parametrize(("key", "message"), [("name", "name must not be blank"), ("type", "type must not")])
    def test_a_blank_element_name_or_type(self, key: str, message: str) -> None:
        element = {"name": "Left", "type": "a_T", key: "  "}
        with pytest.raises(ValidationError, match=message):
            ImplementationElement.model_validate(element)

    @pytest.mark.parametrize("length", [0, -1])
    def test_a_length_that_is_not_positive(self, length: int) -> None:
        with pytest.raises(ValidationError, match="length must be > 0"):
            ImplementationArrayDataType.model_validate(
                {
                    "name": "S_T",
                    "kind": "implementation_array",
                    "element_type": "u_T",
                    "length": length,
                    "element_name": "E",
                }
            )

    @pytest.mark.parametrize(("key", "message"), [("element_type", "element_type"), ("element_name", "element_name")])
    def test_a_blank_element_type_or_element_name(self, key: str, message: str) -> None:
        body = {"name": "S_T", "kind": "implementation_array", "element_type": "u_T", "length": 2, "element_name": "E"}
        body[key] = " "
        with pytest.raises(ValidationError, match=f"{message} must not be blank"):
            ImplementationArrayDataType.model_validate(body)

    def test_an_array_without_its_element_name(self) -> None:
        """Required: the source's sub-element name is kept, never invented."""
        with pytest.raises(ValidationError, match="element_name"):
            ImplementationArrayDataType.model_validate(
                {"name": "S_T", "kind": "implementation_array", "element_type": "u_T", "length": 2}
            )

    @pytest.mark.parametrize(
        ("target", "message"),
        [("", "must not be blank"), ("  ", "must not be blank"), ("P_T", "must not equal 'name'")],
    )
    def test_a_blank_or_self_naming_target(self, target: str, message: str) -> None:
        with pytest.raises(ValidationError, match=message):
            PointerDataType.model_validate({"name": "P_T", "kind": "pointer", "target": target})


# The serializer


class TestTheSerializerRoundTripsEachKind:
    @pytest.mark.parametrize(("kind", "model_class", "body"), NEW_KINDS)
    def test_without_a_description(self, kind: str, model_class: type, body: dict[str, Any]) -> None:
        declared = {"name": "Frame_T", "kind": kind, **body}
        written = data_type_to_dict(model_class.model_validate(declared))

        assert written == declared
        assert list(written) == list(declared)

    @pytest.mark.parametrize(("kind", "model_class", "body"), NEW_KINDS)
    def test_with_a_description(self, kind: str, model_class: type, body: dict[str, Any]) -> None:
        declared = {"name": "Frame_T", "kind": kind, **body, "description": "kept"}
        loaded = model_class.model_validate(declared)

        assert model_class.model_validate(data_type_to_dict(loaded)) == loaded
        assert data_type_to_dict(loaded)["description"] == "kept"


# The ARXML reader

_HEADER = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<AUTOSAR xmlns="http://autosar.org/schema/r4.0" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-3-0.xsd">\n'
    "<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>Pkg</SHORT-NAME><ELEMENTS>\n"
)
_FOOTER = "</ELEMENTS></AR-PACKAGE></AR-PACKAGES></AUTOSAR>\n"


def _props(inner: str) -> str:
    return (
        "<SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>"
        f"{inner}"
        "</SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>"
    )


def _impl_ref(name: str) -> str:
    return f'<IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/Pkg/{name}</IMPLEMENTATION-DATA-TYPE-REF>'


def _element(name: str, category: str, inner: str, extra: str = "") -> str:
    return (
        "<IMPLEMENTATION-DATA-TYPE-ELEMENT>"
        f"<SHORT-NAME>{name}</SHORT-NAME><CATEGORY>{category}</CATEGORY>{extra}{_props(inner)}"
        "</IMPLEMENTATION-DATA-TYPE-ELEMENT>"
    )


def _type(name: str, category: str, body: str) -> str:
    return (
        f"<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>{category}</CATEGORY>"
        f"{body}</IMPLEMENTATION-DATA-TYPE>\n"
    )


def _read(tmp_path: Path, *types: str) -> dict[str, ImplDataTypeInfo]:
    source = tmp_path / "composites.arxml"
    source.write_text(_HEADER + "".join(types) + _FOOTER, encoding="utf-8", newline="\n")
    return {info.name: info for info in read_arxml(str(source)).impl_data_types}


class TestTheReaderKeepsAStructure:
    def test_each_sub_element_is_read_at_its_own_node_in_order(self, tmp_path: Path) -> None:
        structure = _type(
            "Motor_T",
            "STRUCTURE",
            "<SUB-ELEMENTS>"
            + _element("Left", "TYPE_REFERENCE", _impl_ref("uint16_T"))
            + _element("Right", "TYPE_REFERENCE", _impl_ref("sint16_T"))
            + "</SUB-ELEMENTS>",
        )

        info = _read(tmp_path, structure)["Motor_T"]

        assert info.category == "STRUCTURE"
        assert [(sub.name, sub.category, sub.impl_type_ref) for sub in info.sub_elements] == [
            ("Left", "TYPE_REFERENCE", "/Pkg/uint16_T"),
            ("Right", "TYPE_REFERENCE", "/Pkg/sint16_T"),
        ]
        # The type's own conditional carries no reference: nothing of a
        # sub-element's is read as the type's.
        assert (info.impl_type_ref, info.base_type_ref) == (None, None)

    def test_an_inline_value_member_keeps_its_base_type_reference(self, tmp_path: Path) -> None:
        structure = _type(
            "Mixed_T",
            "STRUCTURE",
            "<SUB-ELEMENTS>"
            + _element("Raw", "VALUE", '<BASE-TYPE-REF DEST="SW-BASE-TYPE">/Pkg/uint8</BASE-TYPE-REF>')
            + "</SUB-ELEMENTS>",
        )

        (sub,) = _read(tmp_path, structure)["Mixed_T"].sub_elements

        assert (sub.category, sub.base_type_ref, sub.impl_type_ref) == ("VALUE", "/Pkg/uint8", None)

    def test_a_nested_inline_member_is_counted_and_its_children_are_not_the_parents(self, tmp_path: Path) -> None:
        nested = (
            "<IMPLEMENTATION-DATA-TYPE-ELEMENT><SHORT-NAME>Outer</SHORT-NAME><CATEGORY>STRUCTURE</CATEGORY>"
            "<SUB-ELEMENTS>"
            + _element("Inner", "TYPE_REFERENCE", _impl_ref("uint16_T"))
            + "</SUB-ELEMENTS></IMPLEMENTATION-DATA-TYPE-ELEMENT>"
        )
        structure = _type("Nested_T", "STRUCTURE", f"<SUB-ELEMENTS>{nested}</SUB-ELEMENTS>")

        (sub,) = _read(tmp_path, structure)["Nested_T"].sub_elements

        assert (sub.name, sub.category, sub.nested_sub_elements, sub.impl_type_ref) == ("Outer", "STRUCTURE", 1, None)


class TestTheReaderKeepsAnArray:
    def test_the_size_semantics_and_element(self, tmp_path: Path) -> None:
        array = _type(
            "Samples_T",
            "ARRAY",
            "<SUB-ELEMENTS>"
            + _element(
                "Element",
                "TYPE_REFERENCE",
                _impl_ref("uint16_T"),
                extra="<ARRAY-SIZE>8</ARRAY-SIZE><ARRAY-SIZE-SEMANTICS>FIXED-SIZE</ARRAY-SIZE-SEMANTICS>",
            )
            + "</SUB-ELEMENTS>",
        )

        (sub,) = _read(tmp_path, array)["Samples_T"].sub_elements

        assert (sub.name, sub.impl_type_ref, sub.array_size, sub.array_size_semantics, sub.array_size_handling) == (
            "Element",
            "/Pkg/uint16_T",
            8,
            "FIXED-SIZE",
            None,
        )

    def test_variable_size_semantics_and_size_handling_are_kept_as_written(self, tmp_path: Path) -> None:
        array = _type(
            "Dyn_T",
            "ARRAY",
            "<SUB-ELEMENTS>"
            + _element(
                "Element",
                "TYPE_REFERENCE",
                _impl_ref("uint16_T"),
                extra="<ARRAY-SIZE>4</ARRAY-SIZE><ARRAY-SIZE-HANDLING>ALL-INDICES-SAME-ARRAY-SIZE"
                "</ARRAY-SIZE-HANDLING><ARRAY-SIZE-SEMANTICS>VARIABLE-SIZE</ARRAY-SIZE-SEMANTICS>",
            )
            + "</SUB-ELEMENTS>",
        )

        (sub,) = _read(tmp_path, array)["Dyn_T"].sub_elements

        assert (sub.array_size_semantics, sub.array_size_handling) == (
            "VARIABLE-SIZE",
            "ALL-INDICES-SAME-ARRAY-SIZE",
        )

    def test_an_unreadable_size_is_none(self, tmp_path: Path) -> None:
        array = _type(
            "Bad_T",
            "ARRAY",
            "<SUB-ELEMENTS>"
            + _element("Element", "TYPE_REFERENCE", _impl_ref("uint16_T"), extra="<ARRAY-SIZE>eight</ARRAY-SIZE>")
            + "</SUB-ELEMENTS>",
        )

        (sub,) = _read(tmp_path, array)["Bad_T"].sub_elements

        assert sub.array_size is None


class TestTheReaderKeepsAPointer:
    @staticmethod
    def _pointer(name: str, target_props: str, extra: str = "") -> str:
        return _type(
            name,
            "DATA_REFERENCE",
            _props(f"<SW-POINTER-TARGET-PROPS>{target_props}{extra}</SW-POINTER-TARGET-PROPS>"),
        )

    def test_the_target_category_and_reference(self, tmp_path: Path) -> None:
        pointer = self._pointer(
            "FramePtr_T", "<TARGET-CATEGORY>TYPE_REFERENCE</TARGET-CATEGORY>" + _props(_impl_ref("Frame_T"))
        )

        info = _read(tmp_path, pointer)["FramePtr_T"]

        assert (info.category, info.pointer_target_category, info.pointer_target_ref) == (
            "DATA_REFERENCE",
            "TYPE_REFERENCE",
            "/Pkg/Frame_T",
        )
        assert (info.impl_type_ref, info.pointer_target_base_type_ref, info.pointer_target_nested) == (
            None,
            None,
            False,
        )
        assert info.pointer_target_qualifiers == ()
        assert info.sub_elements == ()

    def test_a_void_target_keeps_its_base_type_reference(self, tmp_path: Path) -> None:
        pointer = self._pointer(
            "VoidPtr_T",
            "<TARGET-CATEGORY>VALUE</TARGET-CATEGORY>"
            + _props('<BASE-TYPE-REF DEST="SW-BASE-TYPE">/Pkg/void</BASE-TYPE-REF>'),
        )

        info = _read(tmp_path, pointer)["VoidPtr_T"]

        assert (info.pointer_target_category, info.pointer_target_base_type_ref, info.pointer_target_ref) == (
            "VALUE",
            "/Pkg/void",
            None,
        )

    def test_an_inline_pointer_to_a_pointer_is_marked(self, tmp_path: Path) -> None:
        inner = "<SW-POINTER-TARGET-PROPS><TARGET-CATEGORY>TYPE_REFERENCE</TARGET-CATEGORY>" + _props(
            _impl_ref("Frame_T")
        )
        pointer = self._pointer(
            "PtrPtr_T",
            "<TARGET-CATEGORY>DATA_REFERENCE</TARGET-CATEGORY>" + _props(inner + "</SW-POINTER-TARGET-PROPS>"),
        )

        info = _read(tmp_path, pointer)["PtrPtr_T"]

        assert (info.pointer_target_category, info.pointer_target_nested, info.pointer_target_ref) == (
            "DATA_REFERENCE",
            True,
            None,
        )

    def test_every_other_target_qualifier_is_inventoried(self, tmp_path: Path) -> None:
        pointer = self._pointer(
            "Fn_T",
            "<TARGET-CATEGORY>TYPE_REFERENCE</TARGET-CATEGORY>"
            + _props(_impl_ref("Frame_T") + "<SW-IMPL-POLICY>CONST</SW-IMPL-POLICY>"),
            extra='<FUNCTION-POINTER-SIGNATURE-REF DEST="BSW-MODULE-ENTRY">/Pkg/Fn</FUNCTION-POINTER-SIGNATURE-REF>',
        )

        info = _read(tmp_path, pointer)["Fn_T"]

        assert info.pointer_target_qualifiers == ("SW-IMPL-POLICY", "FUNCTION-POINTER-SIGNATURE-REF")
        assert info.pointer_target_ref == "/Pkg/Frame_T"

    def test_a_value_type_reads_no_pointer_and_no_sub_element(self, tmp_path: Path) -> None:
        value = _type("u_T", "VALUE", _props('<BASE-TYPE-REF DEST="SW-BASE-TYPE">/Pkg/uint8</BASE-TYPE-REF>'))

        info = _read(tmp_path, value)["u_T"]

        assert (info.sub_elements, info.pointer_target_category, info.pointer_target_ref) == ((), None, None)
