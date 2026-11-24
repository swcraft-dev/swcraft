"""The two stub kinds at the declaration and at the loader.

A stub stands in for an interface (``kind:interfaces.external_interface``) or an
implementation data type (``kind:implementation_types.external_type``) the ARXML export
declares nowhere. Three questions:

* **what the contract declares**: the two kinds, their fields, their classes, and the
  cross-element and element-local name rules;
* **what loads**: a stub with and without its reference pair (a blank or missing pair is
  ``check-model``'s finding, never a load failure), through the class and the reader;
* **what does not**: a blank or path-carrying name, and any key the stub does not
  declare, so a typo is not dropped in silence.

What this module does **not** reach: nothing here resolves a name across files (whether
a reference to a stub resolves, whether its ``external_dest`` is admitted, whether an
``init`` reaches it are ``check-model``'s questions), and nothing here emits ARXML.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from model_contract_fixtures import bound_classes, load_binding, load_contract
from pydantic import ValidationError
from swcraft_tools import reader
from swcraft_tools.editing import diagnosis
from swcraft_tools.schema import ExternalDataType, ExternalInterface
from swcraft_tools.validator import CONSISTENCY_RULES

#: The two stub kinds, each with the class it binds to and the one content key of a
#: sibling kind a stub must refuse.
STUB_KINDS: tuple[tuple[str, str, type, str], ...] = (
    # An interface stub lists `data_elements` and `operations` by name, so the
    # sibling kinds' key it still refuses is `is_service`.
    ("kind:interfaces.external_interface", "interfaces", ExternalInterface, "is_service"),
    ("kind:implementation_types.external_type", "implementation_types", ExternalDataType, "base"),
)

#: The three cross-element rules the stubs bring.
NEW_CROSS_ELEMENT_RULES = (
    "rule:interface.external-incomplete-reference",
    "rule:data-type.external-incomplete-reference",
    "rule:interface.init-on-external-type",
)

#: The two element-local name rules, one per stub kind.
NEW_ELEMENT_LOCAL_RULES = (
    "rule:interfaces.external_interface.name.not-blank",
    "rule:implementation_types.external_type.name.not-blank",
)


def _kinds() -> dict[str, dict[str, Any]]:
    return {kind["id"]: kind for layer in load_contract()["layers"] for kind in layer["kinds"]}


def _name(kind_id: str) -> str:
    """The bare kind name, which is also the YAML ``kind:`` value."""
    return kind_id.rsplit(".", 1)[1]


def _rules() -> dict[str, dict[str, Any]]:
    return {rule["id"]: rule for rule in load_contract()["rules"]}


# The declaration


class TestTheContractDeclaresTheStubs:
    def test_the_contract_version_is_the_minor_that_adds_them(self) -> None:
        """The shipped contract version; adding kinds and rules without removing any is a minor."""
        # The contract version in force; every contract bump moves it.
        assert load_contract()["contractVersion"] == "12.7.1"

    @pytest.mark.parametrize(("kind_id", "layer", "model_class", "refused_key"), STUB_KINDS)
    def test_each_kind_is_a_top_level_kind_discriminated_by_kind(
        self, kind_id: str, layer: str, model_class: type, refused_key: str
    ) -> None:
        kind = _kinds()[kind_id]

        assert (kind["kind"], kind["discriminatorField"], kind["memberOnly"]) == (_name(kind_id), "kind", False)
        assert kind_id.startswith(f"kind:{layer}.")

    @pytest.mark.parametrize(("kind_id", "layer", "model_class", "refused_key"), STUB_KINDS)
    def test_each_kind_declares_its_five_fields_in_order(
        self, kind_id: str, layer: str, model_class: type, refused_key: str
    ) -> None:
        """Each stub kind declares its fields in order.

        ``name``, ``kind``, the reference pair, (interface stub) the two member-name lists, ``description``, then
        (interface stub) ``satisfies``.
        """
        fields = {field["name"]: field for field in _kinds()[kind_id]["fields"]}
        lists = ["operations", "data_elements"] if model_class is ExternalInterface else []
        linked = ["satisfies"] if model_class is ExternalInterface else []

        assert list(fields) == ["name", "kind", "external_ref", "external_dest", *lists, "description", *linked]
        for name in linked:
            assert fields.pop(name)["type"] == "list[string]", name
        for name in lists:
            assert fields.pop(name)["type"] == "list[string]", name
        assert all(field["type"] == "string" for field in fields.values())
        assert (fields["name"]["required"], fields["name"]["immutable"]) == (True, True)
        assert (fields["kind"]["required"], fields["kind"]["enum"]) == (True, [_name(kind_id)])
        for optional in ("external_ref", "external_dest", "description"):
            assert (fields[optional]["required"], fields[optional]["default"]) == (False, ""), optional
            assert "omitAtDefault" not in fields[optional], optional

    @pytest.mark.parametrize(("kind_id", "layer", "model_class", "refused_key"), STUB_KINDS)
    def test_each_kind_is_bound_to_its_class(
        self, kind_id: str, layer: str, model_class: type, refused_key: str
    ) -> None:
        assert bound_classes()[kind_id] is model_class

    @pytest.mark.parametrize("rule_id", NEW_CROSS_ELEMENT_RULES)
    def test_the_cross_element_rules_are_declared_and_reported_by_the_validator(self, rule_id: str) -> None:
        rule = _rules()[rule_id]

        assert rule["family"] == "cross-element"
        assert rule["summary"] == CONSISTENCY_RULES[rule_id.removeprefix("rule:")]
        assert load_binding()["rules"][rule_id]["module"].rpartition(".")[0] == "swcraft_tools.validator"

    @pytest.mark.parametrize("rule_id", NEW_CROSS_ELEMENT_RULES)
    def test_the_cross_element_rules_carry_a_protocol_code(self, rule_id: str) -> None:
        assert rule_id.removeprefix("rule:") in diagnosis.RULE_CODES

    @pytest.mark.parametrize(
        ("rule_id", "kind_id"),
        [
            ("rule:interface.external-incomplete-reference", "kind:interfaces.external_interface"),
            ("rule:data-type.external-incomplete-reference", "kind:implementation_types.external_type"),
        ],
    )
    def test_each_incomplete_reference_rule_applies_to_its_own_kinds_pair(self, rule_id: str, kind_id: str) -> None:
        prefix = kind_id.replace("kind:", "field:")
        assert _rules()[rule_id]["appliesTo"] == [f"{prefix}.external_ref", f"{prefix}.external_dest"]

    def test_the_init_rule_applies_to_the_init_the_type_and_the_type_stub(self) -> None:
        """The fields the init rule's walk descends through to reach a stub are listed.

        The walk goes through records, arrays, port ``com_specs`` and component variables.
        """
        applies = _rules()["rule:interface.init-on-external-type"]["appliesTo"]

        assert {
            "field:interfaces.data_element.init",
            # The element states its type's side; the walk starts from whichever
            # of the two fields is set.
            "field:interfaces.data_element.application_type",
            "field:interfaces.data_element.implementation_type",
            "field:implementation_types.reference_type.base",
            "kind:implementation_types.external_type",
            "field:implementation_types.implementation_element.type",
            "field:implementation_types.implementation_array.element_type",
            "field:swc.swc.external",
            "field:swc.swc.name",
            "field:swc.swc.ports",
            "field:swc.port.name",
            "field:swc.port.interface",
            "field:swc.port.com_specs",
            "field:swc.port_com_spec.data_element",
            "field:swc.port_com_spec.init",
            "field:interfaces.sender_receiver.data_elements",
            "field:interfaces.data_element.name",
            "field:interfaces.data_element.sw_impl_policy",
            # What the walk dispatches on and descends through to reach a member
            # typed by a stub: a reference chain, a record's elements, an array's element.
            "kind:implementation_types.reference_type",
            "kind:implementation_types.implementation_record",
            "field:implementation_types.implementation_record.elements",
            "kind:implementation_types.implementation_array",
            # A component variable's own `init`, walked the same way.
            "field:swc.swc.variables",
            "field:swc.variable.name",
            "field:swc.variable.init",
            "field:swc.variable.application_type",
            "field:swc.variable.implementation_type",
        } == set(applies)

    @pytest.mark.parametrize("rule_id", NEW_ELEMENT_LOCAL_RULES)
    def test_the_name_rules_are_element_local_and_bound_to_the_name_validator(self, rule_id: str) -> None:
        rule = _rules()[rule_id]
        binding = load_binding()["rules"][rule_id]

        assert rule["family"] == "element-local"
        assert rule["appliesTo"] == ["field:" + rule_id.removeprefix("rule:").removesuffix(".not-blank")]
        assert binding["method"] == "name_must_not_be_blank"

    def test_no_default_is_omitted_on_disk(self) -> None:
        """A stub's reference pair is never left out of the file at its default."""
        omitted = sorted(
            field["id"] for kind in _kinds().values() for field in kind["fields"] if field.get("omitAtDefault")
        )
        # Every `omitAtDefault` field of the contract; a contract adding or retiring one moves it.
        assert len(omitted) == 38, omitted
        assert not any(".external_ref" in field_id or ".external_dest" in field_id for field_id in omitted), omitted


# The loader


class TestAStubLoads:
    @pytest.mark.parametrize(("kind_id", "layer", "model_class", "refused_key"), STUB_KINDS)
    def test_with_its_reference_pair(self, kind_id: str, layer: str, model_class: type, refused_key: str) -> None:
        stub = model_class.model_validate(
            {
                "name": "Stub_T",
                "kind": _name(kind_id),
                "external_ref": "/Pkg/Stub_T",
                "external_dest": "IMPLEMENTATION-DATA-TYPE",
            }
        )

        assert (stub.external_ref, stub.external_dest, stub.description) == (
            "/Pkg/Stub_T",
            "IMPLEMENTATION-DATA-TYPE",
            "",
        )

    @pytest.mark.parametrize(("kind_id", "layer", "model_class", "refused_key"), STUB_KINDS)
    def test_with_a_blank_or_absent_reference_pair(
        self, kind_id: str, layer: str, model_class: type, refused_key: str
    ) -> None:
        """A blank pair loads and is the rule's finding, so a malformed stub keeps the project readable."""
        absent = model_class.model_validate({"name": "Stub_T", "kind": _name(kind_id)})
        blank = model_class.model_validate(
            {"name": "Stub_T", "kind": _name(kind_id), "external_ref": "  ", "external_dest": ""}
        )

        assert (absent.external_ref, absent.external_dest) == ("", "")
        assert (blank.external_ref, blank.external_dest) == ("  ", "")


class TestAStubRefuses:
    @pytest.mark.parametrize(("kind_id", "layer", "model_class", "refused_key"), STUB_KINDS)
    @pytest.mark.parametrize("name", ["", "   "])
    def test_a_blank_name(self, kind_id: str, layer: str, model_class: type, refused_key: str, name: str) -> None:
        with pytest.raises(ValidationError, match="name must not be blank"):
            model_class.model_validate({"name": name, "kind": _name(kind_id)})

    @pytest.mark.parametrize(("kind_id", "layer", "model_class", "refused_key"), STUB_KINDS)
    def test_a_name_carrying_a_path_separator(
        self, kind_id: str, layer: str, model_class: type, refused_key: str
    ) -> None:
        with pytest.raises(ValidationError, match="must not contain path separators"):
            model_class.model_validate({"name": "a/b", "kind": _name(kind_id)})

    @pytest.mark.parametrize(("kind_id", "layer", "model_class", "refused_key"), STUB_KINDS)
    def test_a_content_key_of_a_sibling_kind(
        self, kind_id: str, layer: str, model_class: type, refused_key: str
    ) -> None:
        """A stub's ``is_service`` or ``base`` (its target's content) fails the load, never dropped in silence."""
        with pytest.raises(ValidationError):
            model_class.model_validate({"name": "Stub_T", "kind": _name(kind_id), refused_key: []})

    @pytest.mark.parametrize(("kind_id", "layer", "model_class", "refused_key"), STUB_KINDS)
    def test_an_undeclared_key(self, kind_id: str, layer: str, model_class: type, refused_key: str) -> None:
        with pytest.raises(ValidationError):
            model_class.model_validate({"name": "Stub_T", "kind": _name(kind_id), "extrnal_ref": "/Pkg/Stub_T"})


class TestTheReaderLoadsAStub:
    @staticmethod
    def _write(root: Path, layer: str, text: str) -> None:
        directory = root / layer
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "Stub_T.yaml").write_text(text, encoding="utf-8", newline="\n")

    def test_an_interface_stub_beside_a_declared_interface(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "interfaces",
            "name: Stub_T\nkind: external_interface\nexternal_ref: /Pkg/Stub_T\n"
            "external_dest: CLIENT-SERVER-INTERFACE\n",
        )
        (tmp_path / "interfaces" / "Speed_If.yaml").write_text(
            "name: Speed_If\nkind: sender_receiver\ndata_elements:\n"
            "  - name: speed\n    implementation_type: Speed_T\n",
            encoding="utf-8",
            newline="\n",
        )

        loaded = {element.name: element for element in reader.read_interfaces(str(tmp_path))}

        assert isinstance(loaded["Stub_T"], ExternalInterface)
        assert loaded["Stub_T"].external_dest == "CLIENT-SERVER-INTERFACE"
        assert loaded["Speed_If"].kind == "sender_receiver"

    def test_a_data_type_stub(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "data_types/implementation",
            "name: Stub_T\nkind: external_type\nexternal_ref: /Pkg/Stub_T\nexternal_dest: IMPLEMENTATION-DATA-TYPE\n",
        )

        (loaded,) = reader.read_data_types(str(tmp_path))

        assert isinstance(loaded, ExternalDataType)
        assert loaded.external_ref == "/Pkg/Stub_T"

    def test_a_stub_with_an_undeclared_key_names_its_file(self, tmp_path: Path) -> None:
        self._write(tmp_path, "interfaces", "name: Stub_T\nkind: external_interface\nis_service: false\n")

        with pytest.raises(ValueError, match="Stub_T.yaml"):
            reader.read_interfaces(str(tmp_path))
