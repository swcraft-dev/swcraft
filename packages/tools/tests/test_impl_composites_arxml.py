"""The writer over the implementation record, the implementation array and the pointer.

``generate arxml`` runs with no ``check-model`` gate in front of it, so the writer emits what the three
kinds declare and refuses, with nothing written, what ``check-model`` reports — through the validator's own
predicates (``composite_map_difference``, ``mapped_pointer``, the ``MAP_MAY_*`` kind sets), never a second
statement of them. The expected fragments are derived by hand from the licensed schemas' sequences
(``IMPLEMENTATION-DATA-TYPE``, ``IMPLEMENTATION-DATA-TYPE-ELEMENT``, ``SW-POINTER-TARGET-PROPS``), never
captured from the writer.

What it does **not** reach: the schema's own verdict (``test_arxml_xsd_conformance.py``); byte identity of a
model without the new kinds (``test_impl_composites_byte_identity.py``, over its recorded trees, not every
model); component exchange and the importer; every assertion is over the shapes written here — example-based
evidence, not a proof over every model.
"""

from __future__ import annotations

import ast
import shutil
import typing
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import yaml
from conftest import golden_slice, make_empty_model_root, model_element_storage_name
from impl_composites_support import (
    DATA_ELEMENTS,
    DATA_TYPES,
    MAPPING_SET,
    POINTER_PORT,
    impl_composites_model_root,
)
from lxml import etree
from model_contract_fixtures import load_contract
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.schema import (
    ExternalDataType,
    ImplementationArrayDataType,
    ImplementationRecordDataType,
    PointerDataType,
    ReferenceDataType,
    ValueDataType,
)
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import generate_arxml
from swcraft_tools.writer.arxml_xml import ar_qname, short_name_of

_BASES: list[dict[str, Any]] = [
    {"name": "uint8_base", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"},
    {"name": "uint8", "kind": "value_type", "base": "uint8_base"},
    {"name": "uint16_base", "kind": "base_type", "size": 16, "encoding": "NONE", "native_declaration": "uint16_t"},
    {"name": "uint16", "kind": "value_type", "base": "uint16_base"},
]

_RECORD: dict[str, Any] = {
    "name": "Pair_T",
    "kind": "implementation_record",
    "elements": [{"name": "first", "type": "uint8"}, {"name": "second", "type": "uint16"}],
}
_ARRAY: dict[str, Any] = {
    "name": "Pairs_T",
    "kind": "implementation_array",
    "element_type": "Pair_T",
    "length": 3,
    "element_name": "Item",
}
_POINTER: dict[str, Any] = {"name": "Pair_Ptr", "kind": "pointer", "target": "Pair_T"}
_APP_SCALAR: dict[str, Any] = {"name": "Level", "kind": "application_type"}
_APP_RECORD: dict[str, Any] = {
    "name": "Pair",
    "kind": "record",
    "fields": [{"name": "a", "type": "Level"}, {"name": "b", "type": "Level"}],
}
_APP_ARRAY: dict[str, Any] = {"name": "Pairs", "kind": "array", "element_type": "Pair", "length": 3}


def _put(model_root: Path, layer: str, document: dict[str, Any]) -> None:
    path = model_root / layer / f"{document['name']}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _model(
    tmp_path: Path,
    types: list[dict[str, Any]],
    *,
    interfaces: list[dict[str, Any]] = (),  # type: ignore[assignment]
    maps: list[dict[str, str]] | None = None,
) -> Path:
    """A fresh model root holding the four scalar types, *types*, *interfaces* and one set of *maps*."""
    model_root = make_empty_model_root(tmp_path / "project")
    for document in [*_BASES, *types]:
        # The payload's kind picks the layer directory (a base type: data_types/base/).
        directory = model_element_storage_name(document["name"], kind=document["kind"]).rpartition("/")[0]
        _put(model_root, directory, document)
    for document in interfaces:
        _put(model_root, "interfaces", document)
    if maps is not None:
        _put(
            model_root,
            model_element_storage_name("Maps", layer="mapping_sets").rpartition("/")[0],
            {"name": "Maps", "maps": maps},
        )
    return model_root


def _generated(model_root: Path, tmp_path: Path) -> str:
    output = tmp_path / "out" / "model.arxml"
    generate_arxml(str(model_root), str(output))
    return output.read_text(encoding="utf-8")


#: What ``check-model`` reports on any bare model root :func:`_model` builds, and
#: nothing the writer needs: it declares no root composition and no hardware.
_AMBIENT = frozenset({"composition.no-root", "hardware.layer-absent"})


def _rules(model_root: Path) -> set[str]:
    """Every rule ``check-model`` reports over *model_root*, less the two :data:`_AMBIENT` ones."""
    return {record.rule for record in check_cross_layer_consistency(str(model_root)).records} - _AMBIENT


def _refused(model_root: Path, tmp_path: Path, message: str) -> str:
    """Generate into a fresh directory, assert the refusal matches *message* and nothing was written."""
    fresh = tmp_path / "fresh"
    assert not fresh.exists()
    with pytest.raises(ValueError, match=message) as refusal:
        generate_arxml(str(model_root), str(fresh / "model.arxml"))
    assert not fresh.exists(), "a refused generation wrote into its output directory"
    return str(refusal.value)


def _references(text: str, tag: str) -> list[tuple[str | None, str]]:
    root = etree.fromstring(text.encode("utf-8"))
    return [(node.get("DEST"), str(node.text)) for node in root.iter(ar_qname(tag))]


def _data_types_order(text: str) -> list[str]:
    root = etree.fromstring(text.encode("utf-8"))
    (package,) = [node for node in root.iter(ar_qname("AR-PACKAGE")) if short_name_of(node) == "DataTypes"]
    # The layer packages in emission order, each package's elements in theirs.
    return [
        short_name_of(node)
        for layer in package.find(ar_qname("AR-PACKAGES"))
        for node in layer.find(ar_qname("ELEMENTS"))
    ]


@pytest.fixture(scope="module")
def fixture_text(tmp_path_factory: pytest.TempPathFactory) -> str:
    base = tmp_path_factory.mktemp("impl-composites")
    model_root = impl_composites_model_root(base / "project")
    output = base / "model.arxml"
    generate_arxml(str(model_root), str(output))
    return output.read_text(encoding="utf-8")


class TestTheFixtureModel:
    def test_check_model_accepts_it(self, tmp_path: Path) -> None:
        """The oracles here are worth something only over a model a user could author."""
        result = check_cross_layer_consistency(str(impl_composites_model_root(tmp_path / "project")))
        assert result.consistent, [record.message for record in result.records]

    def test_it_declares_every_new_kind_and_both_composite_maps(self) -> None:
        kinds = {document["kind"] for document in DATA_TYPES}
        assert {"implementation_record", "implementation_array", "pointer", "reference_type"} <= kinds
        assert {entry["implementation"] for entry in MAPPING_SET["maps"]} >= {"WheelSpeeds_T", "WheelSpeedHistory_T"}


# Each emitted category — fragments derived from the schema sequences


class TestEachCategoryIsEmitted:
    def test_an_implementation_record_is_a_structure_of_type_references(self, fixture_text: str) -> None:
        assert golden_slice(fixture_text, "WheelSpeeds_T", "IMPLEMENTATION-DATA-TYPE") == (
            "<IMPLEMENTATION-DATA-TYPE>\n"
            "  <SHORT-NAME>WheelSpeeds_T</SHORT-NAME>\n"
            "  <CATEGORY>STRUCTURE</CATEGORY>\n"
            "  <SUB-ELEMENTS>\n"
            "    <IMPLEMENTATION-DATA-TYPE-ELEMENT>\n"
            "      <SHORT-NAME>front</SHORT-NAME>\n"
            "      <CATEGORY>TYPE_REFERENCE</CATEGORY>\n"
            "      <SW-DATA-DEF-PROPS>\n"
            "        <SW-DATA-DEF-PROPS-VARIANTS>\n"
            "          <SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            '            <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/DataTypes/Implementation/uint16'
            "</IMPLEMENTATION-DATA-TYPE-REF>\n"
            "          </SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            "        </SW-DATA-DEF-PROPS-VARIANTS>\n"
            "      </SW-DATA-DEF-PROPS>\n"
            "    </IMPLEMENTATION-DATA-TYPE-ELEMENT>\n"
            "    <IMPLEMENTATION-DATA-TYPE-ELEMENT>\n"
            "      <SHORT-NAME>rear</SHORT-NAME>\n"
            "      <CATEGORY>TYPE_REFERENCE</CATEGORY>\n"
            "      <SW-DATA-DEF-PROPS>\n"
            "        <SW-DATA-DEF-PROPS-VARIANTS>\n"
            "          <SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            '            <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/DataTypes/Implementation/uint16'
            "</IMPLEMENTATION-DATA-TYPE-REF>\n"
            "          </SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            "        </SW-DATA-DEF-PROPS-VARIANTS>\n"
            "      </SW-DATA-DEF-PROPS>\n"
            "    </IMPLEMENTATION-DATA-TYPE-ELEMENT>\n"
            "  </SUB-ELEMENTS>\n"
            "</IMPLEMENTATION-DATA-TYPE>"
        )

    def test_an_implementation_array_is_one_fixed_size_sub_element(self, fixture_text: str) -> None:
        assert golden_slice(fixture_text, "WheelSpeedHistory_T", "IMPLEMENTATION-DATA-TYPE") == (
            "<IMPLEMENTATION-DATA-TYPE>\n"
            "  <SHORT-NAME>WheelSpeedHistory_T</SHORT-NAME>\n"
            "  <CATEGORY>ARRAY</CATEGORY>\n"
            "  <SUB-ELEMENTS>\n"
            "    <IMPLEMENTATION-DATA-TYPE-ELEMENT>\n"
            "      <SHORT-NAME>Sample</SHORT-NAME>\n"
            "      <CATEGORY>TYPE_REFERENCE</CATEGORY>\n"
            "      <ARRAY-SIZE>4</ARRAY-SIZE>\n"
            "      <ARRAY-SIZE-SEMANTICS>FIXED-SIZE</ARRAY-SIZE-SEMANTICS>\n"
            "      <SW-DATA-DEF-PROPS>\n"
            "        <SW-DATA-DEF-PROPS-VARIANTS>\n"
            "          <SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            '            <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">'
            "/DataTypes/Implementation/WheelSpeeds_T"
            "</IMPLEMENTATION-DATA-TYPE-REF>\n"
            "          </SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            "        </SW-DATA-DEF-PROPS-VARIANTS>\n"
            "      </SW-DATA-DEF-PROPS>\n"
            "    </IMPLEMENTATION-DATA-TYPE-ELEMENT>\n"
            "  </SUB-ELEMENTS>\n"
            "</IMPLEMENTATION-DATA-TYPE>"
        )

    def test_a_pointer_is_a_data_reference_with_its_target_props(self, fixture_text: str) -> None:
        assert golden_slice(fixture_text, "WheelSpeeds_Ptr", "IMPLEMENTATION-DATA-TYPE") == (
            "<IMPLEMENTATION-DATA-TYPE>\n"
            "  <SHORT-NAME>WheelSpeeds_Ptr</SHORT-NAME>\n"
            "  <CATEGORY>DATA_REFERENCE</CATEGORY>\n"
            "  <SW-DATA-DEF-PROPS>\n"
            "    <SW-DATA-DEF-PROPS-VARIANTS>\n"
            "      <SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            "        <SW-POINTER-TARGET-PROPS>\n"
            "          <TARGET-CATEGORY>TYPE_REFERENCE</TARGET-CATEGORY>\n"
            "          <SW-DATA-DEF-PROPS>\n"
            "            <SW-DATA-DEF-PROPS-VARIANTS>\n"
            "              <SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            '                <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">'
            "/DataTypes/Implementation/WheelSpeeds_T"
            "</IMPLEMENTATION-DATA-TYPE-REF>\n"
            "              </SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            "            </SW-DATA-DEF-PROPS-VARIANTS>\n"
            "          </SW-DATA-DEF-PROPS>\n"
            "        </SW-POINTER-TARGET-PROPS>\n"
            "      </SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            "    </SW-DATA-DEF-PROPS-VARIANTS>\n"
            "  </SW-DATA-DEF-PROPS>\n"
            "</IMPLEMENTATION-DATA-TYPE>"
        )

    def test_a_pointer_to_a_pointer_names_the_named_pointer(self, fixture_text: str) -> None:
        fragment = golden_slice(fixture_text, "WheelSpeeds_PtrPtr", "IMPLEMENTATION-DATA-TYPE")
        assert (
            '<IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/DataTypes/Implementation/WheelSpeeds_Ptr'
            "</IMPLEMENTATION-DATA-TYPE-REF>" in fragment
        )
        assert fragment.count("<SW-POINTER-TARGET-PROPS>") == 1, "never an inline pointer-to-pointer"

    def test_an_unreferenced_composite_is_emitted(self, fixture_text: str) -> None:
        """Nothing names ``Spare_T``, and it is still in the document."""
        assert "<SHORT-NAME>Spare_T</SHORT-NAME>" in fixture_text
        assert "<SHORT-NAME>Byte</SHORT-NAME>" in fixture_text

    def test_every_declared_new_kind_is_in_the_document(self, fixture_text: str) -> None:
        emitted = set(_data_types_order(fixture_text))
        assert {document["name"] for document in DATA_TYPES} <= emitted

    def test_a_sub_element_typed_by_an_external_stub_carries_the_stubs_own_pair(self, tmp_path: Path) -> None:
        stub = {
            "name": "Foreign_T",
            "kind": "external_type",
            "external_ref": "/Foreign/Types/Foreign_T",
            "external_dest": "IMPLEMENTATION-DATA-TYPE",
        }
        model_root = _model(
            tmp_path,
            [
                stub,
                {"name": "Wrap_T", "kind": "implementation_record", "elements": [{"name": "x", "type": "Foreign_T"}]},
                {
                    "name": "WrapArr_T",
                    "kind": "implementation_array",
                    "element_type": "Foreign_T",
                    "length": 2,
                    "element_name": "E",
                },
                {"name": "Foreign_Ptr", "kind": "pointer", "target": "Foreign_T"},
            ],
        )
        assert _rules(model_root) == set()
        references = _references(_generated(model_root, tmp_path), "IMPLEMENTATION-DATA-TYPE-REF")
        assert references.count(("IMPLEMENTATION-DATA-TYPE", "/Foreign/Types/Foreign_T")) == 3


# Both halves of every new reference


class TestEveryNewReferenceCarriesItsPathAndItsDest:
    @pytest.mark.parametrize("target", ["Pair_T", "Pairs_T", "Pair_Ptr"])
    def test_a_reference_type_aliases_each_kind(self, tmp_path: Path, target: str) -> None:
        model_root = _model(
            tmp_path, [_RECORD, _ARRAY, _POINTER, {"name": "Alias_T", "kind": "reference_type", "base": target}]
        )
        assert _rules(model_root) == set()
        fragment = golden_slice(_generated(model_root, tmp_path), "Alias_T", "IMPLEMENTATION-DATA-TYPE")
        assert "<CATEGORY>TYPE_REFERENCE</CATEGORY>" in fragment
        assert (
            f'<IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/DataTypes/Implementation/{target}'
            "</IMPLEMENTATION-DATA-TYPE-REF>" in fragment
        )

    def test_a_data_element_typed_by_each_kind(self, fixture_text: str) -> None:
        references = _references(fixture_text, "TYPE-TREF")
        for element in DATA_ELEMENTS:
            assert (
                "IMPLEMENTATION-DATA-TYPE",
                f"/DataTypes/Implementation/{element['implementation_type']}",
            ) in references, element

    def test_a_client_server_argument_typed_by_a_pointer(self, fixture_text: str) -> None:
        fragment = golden_slice(fixture_text, "ReadSpeeds", "CLIENT-SERVER-OPERATION")
        assert (
            '<TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/DataTypes/Implementation/WheelSpeeds_Ptr</TYPE-TREF>'
            in fragment
        )
        assert (
            '<TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/DataTypes/Implementation/WheelSpeeds_PtrPtr</TYPE-TREF>'
            in fragment
        )

    def test_the_pointer_argument_keeps_the_server_com_spec_wrapper(self, fixture_text: str) -> None:
        """A client-server com-spec is per operation, never per argument type (``_emit_com_specs``)."""
        port = golden_slice(fixture_text, POINTER_PORT["name"], "P-PORT-PROTOTYPE")
        assert "<PROVIDED-COM-SPECS>" in port
        assert (
            '<OPERATION-REF DEST="CLIENT-SERVER-OPERATION">/PortInterfaces/IDiagnosticService/ReadSpeeds'
            "</OPERATION-REF>" in port
        )
        assert port.count("<SERVER-COM-SPEC>") == 3, "one per operation: ReadDtc, ClearDtc, ReadSpeeds"

    def test_the_record_and_the_array_map_carry_both_halves_on_both_sides(self, fixture_text: str) -> None:
        fragment = golden_slice(fixture_text, MAPPING_SET["name"], "DATA-TYPE-MAPPING-SET")
        for application, dest in (
            ("WheelSpeeds", "APPLICATION-RECORD-DATA-TYPE"),
            ("WheelSpeedHistory", "APPLICATION-ARRAY-DATA-TYPE"),
        ):
            assert (
                f'<APPLICATION-DATA-TYPE-REF DEST="{dest}">'
                f"/DataTypes/Application/{application}</APPLICATION-DATA-TYPE-REF>" in fragment
            )
        for implementation in ("WheelSpeeds_T", "WheelSpeedHistory_T"):
            assert (
                '<IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">'
                f"/DataTypes/Implementation/{implementation}"
                "</IMPLEMENTATION-DATA-TYPE-REF>" in fragment
            )

    def test_a_map_onto_an_alias_of_a_composite_names_the_alias(self, tmp_path: Path) -> None:
        model_root = _model(
            tmp_path,
            [_RECORD, _APP_SCALAR, _APP_RECORD, {"name": "PairAlias_T", "kind": "reference_type", "base": "Pair_T"}],
            maps=[{"application": "Pair", "implementation": "PairAlias_T"}],
        )
        assert _rules(model_root) == set()
        fragment = golden_slice(_generated(model_root, tmp_path), "Maps", "DATA-TYPE-MAPPING-SET")
        assert (
            '<IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/DataTypes/Implementation/PairAlias_T'
            "</IMPLEMENTATION-DATA-TYPE-REF>" in fragment
        )


# The one dependency-ordered pass


class TestTheOneOrderedPass:
    def test_every_implementation_reference_points_backwards(self, fixture_text: str) -> None:
        order = _data_types_order(fixture_text)
        position = {name: index for index, name in enumerate(order)}
        root = etree.fromstring(fixture_text.encode("utf-8"))
        for node in root.iter(ar_qname("IMPLEMENTATION-DATA-TYPE")):
            owner = position[short_name_of(node)]
            for reference in node.iter(ar_qname("IMPLEMENTATION-DATA-TYPE-REF")):
                target = str(reference.text).rpartition("/")[2]
                assert position[target] < owner, (short_name_of(node), target)

    def test_a_reference_record_reference_chain_read_top_first_is_emitted_bottom_up(self, fixture_text: str) -> None:
        order = _data_types_order(fixture_text)
        chain = ["WheelSpeeds_T", "WheelSpeeds_Ref", "Frame_T", "Frame_Ref"]
        assert [order.index(name) for name in chain] == sorted(order.index(name) for name in chain)

    def test_the_pass_sits_where_the_reference_types_did(self, fixture_text: str) -> None:
        """After the value types, before the application types, composites and maps."""
        order = _data_types_order(fixture_text)
        assert order.index("uint64") < order.index("WheelSpeeds_T") < order.index("WheelSpeed_Kph")
        assert order.index("Frame_Ref") < order.index("WheelSpeeds") < order.index(MAPPING_SET["name"])

    def test_a_record_holding_a_pointer_to_itself_is_refused(self, tmp_path: Path) -> None:
        model_root = _model(
            tmp_path,
            [
                {
                    "name": "Node_T",
                    "kind": "implementation_record",
                    "elements": [{"name": "value", "type": "uint8"}, {"name": "next", "type": "Node_Ptr"}],
                },
                {"name": "Node_Ptr", "kind": "pointer", "target": "Node_T"},
            ],
        )
        assert "data-type.cycle" in _rules(model_root)
        _refused(model_root, tmp_path, "dependency cycle: Node_Ptr -> Node_T -> Node_Ptr")


# The role, name and member refusals


_ROLE_REFUSALS: list[tuple[str, dict[str, Any], str, str]] = [
    (
        "record-element-undeclared",
        {"name": "Bad_T", "kind": "implementation_record", "elements": [{"name": "x", "type": "Nowhere"}]},
        "data-type.implementation-element-undeclared-type",
        r"Implementation record 'Bad_T' element 'x' type 'Nowhere' is not declared in data_types/implementation/",
    ),
    # Contract 8.0.0 retired the five side rules: a member resolves in `implementation_types`
    # alone, so an application type's name there is an undeclared name.
    (
        "record-element-application",
        {"name": "Bad_T", "kind": "implementation_record", "elements": [{"name": "x", "type": "Level"}]},
        "data-type.implementation-element-undeclared-type",
        r"Implementation record 'Bad_T' element 'x' type 'Level' is not declared in data_types/implementation/",
    ),
    # Contract 8.0.0: a member resolves in `implementation_types`, which holds no base type,
    # so a member naming one names a type not declared there.
    (
        "record-element-base-type",
        {"name": "Bad_T", "kind": "implementation_record", "elements": [{"name": "x", "type": "uint8_base"}]},
        "data-type.implementation-element-undeclared-type",
        r"Implementation record 'Bad_T' element 'x' type 'uint8_base' is not declared in data_types/implementation/",
    ),
    (
        "array-element-undeclared",
        {"name": "Bad_T", "kind": "implementation_array", "element_type": "Nowhere", "length": 2, "element_name": "E"},
        "data-type.implementation-array-undeclared-element-type",
        r"Implementation array 'Bad_T' element_type 'Nowhere' is not declared in data_types/implementation/",
    ),
    (
        "array-element-application",
        {"name": "Bad_T", "kind": "implementation_array", "element_type": "Level", "length": 2, "element_name": "E"},
        "data-type.implementation-array-undeclared-element-type",
        r"Implementation array 'Bad_T' element_type 'Level' is not declared in data_types/implementation/",
    ),
    (
        "pointer-target-undeclared",
        {"name": "Bad_Ptr", "kind": "pointer", "target": "Nowhere"},
        "data-type.pointer-undeclared-target",
        r"Pointer 'Bad_Ptr' target 'Nowhere' is not declared in data_types/implementation/",
    ),
    (
        "pointer-target-application",
        {"name": "Bad_Ptr", "kind": "pointer", "target": "Level"},
        "data-type.pointer-undeclared-target",
        r"Pointer 'Bad_Ptr' target 'Level' is not declared in data_types/implementation/",
    ),
    (
        "duplicate-element-name",
        {
            "name": "Bad_T",
            "kind": "implementation_record",
            "elements": [{"name": "x", "type": "uint8"}, {"name": "x", "type": "uint16"}],
        },
        "data-type.implementation-record-duplicate-element-name",
        r"Implementation record 'Bad_T' has duplicate element name\(s\) \['x'\]",
    ),
    (
        "element-name-not-an-identifier",
        {"name": "Bad_T", "kind": "implementation_record", "elements": [{"name": "my x", "type": "uint8"}]},
        "identifier.declared",
        r"Non-conforming SHORT-NAME\(s\) in implementation record 'Bad_T'.*'my x'",
    ),
    (
        "element-name-of-an-array-not-an-identifier",
        {"name": "Bad_T", "kind": "implementation_array", "element_type": "uint8", "length": 2, "element_name": "1st"},
        "identifier.declared",
        r"Non-conforming SHORT-NAME\(s\) in implementation array 'Bad_T'.*'1st'",
    ),
    (
        "alias-onto-an-application-type",
        {"name": "Bad_T", "kind": "reference_type", "base": "Level"},
        "data-type.reference-type-undeclared-base",
        r"Reference type 'Bad_T' base 'Level' is not declared in data_types/implementation/",
    ),
    # Contract 8.0.0: a reference type's base resolves in `implementation_types`, which holds no base type.
    (
        "alias-onto-a-base-type",
        {"name": "Bad_T", "kind": "reference_type", "base": "uint8_base"},
        "data-type.reference-type-undeclared-base",
        r"Reference type 'Bad_T' base 'uint8_base' is not declared in data_types/implementation/",
    ),
]


#: The five rules contract ``8.0.0`` retired: their models now report the role's undeclared-name rule.
_RETIRED_SIDE_RULES = frozenset(
    {
        "data-type.composite-member-implementation-type",
        "data-type.reference-type-base-not-implementation-type",
        "data-type.implementation-element-not-implementation-type",
        "data-type.implementation-array-element-not-implementation-type",
        "data-type.pointer-target-not-implementation-type",
    }
)


class TestTheRolesAreRefusedAsCheckModelReportsThem:
    @pytest.mark.parametrize(
        ("document", "rule", "message"),
        [case[1:] for case in _ROLE_REFUSALS],
        ids=[case[0] for case in _ROLE_REFUSALS],
    )
    def test_refused_with_nothing_written(self, tmp_path: Path, document: dict[str, Any], rule: str, message: str):
        model_root = _model(tmp_path, [_APP_SCALAR, document])
        reported = _rules(model_root)
        assert rule in reported
        assert not reported & _RETIRED_SIDE_RULES
        _refused(model_root, tmp_path, message)

    def test_the_retired_side_rules_are_declared_nowhere(self) -> None:
        declared = {rule["id"] for rule in load_contract()["rules"]}
        assert not {f"rule:{rule}" for rule in _RETIRED_SIDE_RULES} & declared

    @pytest.mark.parametrize("member", ["Pair_T", "Pairs_T", "Pair_Ptr"])
    def test_a_record_field_naming_a_new_kind_is_an_undeclared_type(self, tmp_path: Path, member: str):
        """A record field's ``type`` resolves in ``application_types`` alone (contract ``8.0.0``).

        So an implementation kind's name there is an undeclared name, reported and refused as one.
        """
        record = {"name": "Holder", "kind": "record", "fields": [{"name": "f", "type": member}]}
        model_root = _model(tmp_path, [_RECORD, _ARRAY, _POINTER, record])
        reported = _rules(model_root)
        assert "data-type.record-field-undeclared-type" in reported
        assert not reported & _RETIRED_SIDE_RULES
        _refused(model_root, tmp_path, f"Data type '{member}' referenced by record 'Holder' field 'f' is not declared")

    def test_an_array_element_naming_a_new_kind_is_an_undeclared_type(self, tmp_path: Path) -> None:
        array = {"name": "Holder", "kind": "array", "element_type": "Pair_T", "length": 2}
        model_root = _model(tmp_path, [_RECORD, array])
        reported = _rules(model_root)
        assert "data-type.array-undeclared-element-type" in reported
        assert not reported & _RETIRED_SIDE_RULES
        _refused(model_root, tmp_path, "Data type 'Pair_T' referenced by array 'Holder' element type is not declared")


# The maps, through the shared predicates


#: ``(id, extra types, map, the check-model rule or None, the writer's refusal or None)``.
_MAP_CASES: list[tuple[str, list[dict[str, Any]], dict[str, str], str | None, str | None]] = [
    ("record-onto-record", [], {"application": "Pair", "implementation": "Pair_T"}, None, None),
    ("array-onto-array", [], {"application": "Pairs", "implementation": "Pairs_T"}, None, None),
    (
        "renamed-members-not-judged",
        [
            {
                "name": "Other_T",
                "kind": "implementation_record",
                "elements": [{"name": "zz", "type": "uint16"}, {"name": "yy", "type": "uint8"}],
            }
        ],
        {"application": "Pair", "implementation": "Other_T"},
        None,
        None,
    ),
    (
        "stub-member-not-judged",
        [
            {
                "name": "Foreign_T",
                "kind": "external_type",
                "external_ref": "/Foreign/Foreign_T",
                "external_dest": "IMPLEMENTATION-DATA-TYPE",
            },
            {
                "name": "Half_T",
                "kind": "implementation_record",
                "elements": [{"name": "p", "type": "Foreign_T"}, {"name": "q", "type": "uint8"}],
            },
        ],
        {"application": "Pair", "implementation": "Half_T"},
        None,
        None,
    ),
    (
        "member-count",
        [{"name": "One_T", "kind": "implementation_record", "elements": [{"name": "only", "type": "uint8"}]}],
        {"application": "Pair", "implementation": "One_T"},
        "mapping-set.incompatible-composite-map",
        r"positional check of a composite map refuses: record 'Pair' has 2 fields and implementation record "
        r"'One_T' 1 element",
    ),
    (
        "array-length",
        [
            {
                "name": "Four_T",
                "kind": "implementation_array",
                "element_type": "Pair_T",
                "length": 4,
                "element_name": "Item",
            }
        ],
        {"application": "Pairs", "implementation": "Four_T"},
        "mapping-set.incompatible-composite-map",
        r"length 3 vs 4",
    ),
    (
        "category",
        [],
        {"application": "Pair", "implementation": "Pairs_T"},
        "mapping-set.incompatible-composite-map",
        r"category record 'Pair' vs implementation_array 'Pairs_T'",
    ),
    (
        "member-category",
        [
            {
                "name": "Nested_T",
                "kind": "implementation_record",
                "elements": [{"name": "a", "type": "Pair_T"}, {"name": "b", "type": "uint8"}],
            }
        ],
        {"application": "Pair", "implementation": "Nested_T"},
        "mapping-set.incompatible-composite-map",
        r"member 1 of record 'Pair'",
    ),
    (
        "onto-a-pointer",
        [],
        {"application": "Pair", "implementation": "Pair_Ptr"},
        "mapping-set.pointer-mapped",
        r"reaches the pointer 'Pair_Ptr'.*\[constr_1066\]",
    ),
    (
        "onto-an-alias-of-a-pointer",
        [{"name": "PtrAlias_T", "kind": "reference_type", "base": "Pair_Ptr"}],
        {"application": "Level", "implementation": "PtrAlias_T"},
        "mapping-set.pointer-mapped",
        r"reaches the pointer 'Pair_Ptr' \('PtrAlias_T' -> 'Pair_Ptr'\)",
    ),
    (
        "a-pointer-inside-the-composite",
        [
            {
                "name": "WithPtr_T",
                "kind": "implementation_record",
                "elements": [{"name": "a", "type": "uint8"}, {"name": "b", "type": "Pair_Ptr"}],
            }
        ],
        {"application": "Pair", "implementation": "WithPtr_T"},
        "mapping-set.pointer-mapped",
        r"reaches the pointer 'Pair_Ptr' \('WithPtr_T' -> 'Pair_Ptr'\)",
    ),
    (
        "application-side-an-implementation-record",
        [],
        {"application": "Pair_T", "implementation": "Pair_T"},
        "mapping-set.unknown-application",
        r"maps 'Pair_T', which is not declared; a map's `application` must name a "
        r"declared application_type, enum, record or array",
    ),
    (
        "implementation-side-an-application-record",
        [],
        {"application": "Pair", "implementation": "Pair"},
        "mapping-set.unknown-implementation",
        r"onto 'Pair', which is not declared; a map's `implementation` must name a declared value_type, "
        r"reference_type, implementation_record or implementation_array",
    ),
]


class TestTheMapsAreJudgedByTheSharedPredicates:
    """The writer refuses exactly the maps ``check-model`` reports, and generates the ones it does not judge."""

    @pytest.mark.parametrize(
        ("types", "entry", "rule", "message"),
        [case[1:] for case in _MAP_CASES],
        ids=[case[0] for case in _MAP_CASES],
    )
    def test_the_writer_and_check_model_agree(
        self, tmp_path: Path, types: list[dict[str, Any]], entry: dict[str, str], rule: str | None, message: str | None
    ) -> None:
        model_root = _model(
            tmp_path,
            [_RECORD, _ARRAY, _POINTER, _APP_SCALAR, _APP_RECORD, _APP_ARRAY, *types],
            maps=[entry],
        )
        reported = _rules(model_root)
        if rule is None:
            assert reported == set(), reported
            fragment = golden_slice(_generated(model_root, tmp_path), "Maps", "DATA-TYPE-MAPPING-SET")
            assert f"/DataTypes/Implementation/{entry['implementation']}</IMPLEMENTATION-DATA-TYPE-REF>" in fragment
        else:
            assert reported == {rule}, reported
            assert message is not None
            _refused(model_root, tmp_path, message)

    def test_a_record_and_an_array_stay_optional_to_map(self, tmp_path: Path) -> None:
        """A port speaking an unmapped record is generated: a record or an array need not be mapped."""
        interface = {
            "name": "IPair",
            "kind": "sender_receiver",
            "data_elements": [{"name": "p", "application_type": "Pair"}],
        }
        model_root = _model(tmp_path, [_APP_SCALAR, _APP_RECORD], interfaces=[interface])
        swc = {
            "name": "Speaker",
            "type": "application",
            "ports": [{"name": "pPair", "direction": "provide", "interface": "IPair"}],
            "handle_termination_and_restart": "no_support",
        }
        _put(model_root, "swc/speaker", swc)
        assert "data-type.application-type-unmapped" not in _rules(model_root)
        assert "<SHORT-NAME>Speaker</SHORT-NAME>" in _generated(model_root, tmp_path)

    def test_the_writer_imports_the_validators_statements(self) -> None:
        """One statement per judgement: the writer's refusal module names the validator's predicates."""
        from swcraft_tools.writer import arxml_generation

        # The import statements themselves, read from the syntax tree: a comment
        # or a docstring naming a predicate satisfies nothing here.
        tree = ast.parse(Path(arxml_generation.__file__).read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("swcraft_tools.validator")
            for alias in node.names
        }
        assert {
            "composite_map_difference",
            "mapped_pointer",
            "MAP_MAY_APPLICATION_KINDS",
            "MAP_MAY_IMPLEMENTATION_KINDS",
            "MAP_APPLICATION_KINDS",
        } <= imported
        used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        assert "MAP_IMPLEMENTATION_KINDS" not in used | imported


class TestTheWritersKindTablesAreTheValidators:
    def test_every_implementation_role_kind_resolves_to_an_implementation_data_type(self) -> None:
        from swcraft_tools.validator.compatibility import _IMPLEMENTATION_ROLE_CLASSES
        from swcraft_tools.writer.arxml_generation import _TYPE_REF_DEST

        declared = set(_IMPLEMENTATION_ROLE_CLASSES) - {ExternalDataType}
        assert {kind: _TYPE_REF_DEST.get(kind) for kind in declared} == dict.fromkeys(
            declared, "IMPLEMENTATION-DATA-TYPE"
        )
        assert {
            ImplementationRecordDataType,
            ImplementationArrayDataType,
            PointerDataType,
            ValueDataType,
            ReferenceDataType,
        } == declared

    def test_every_data_type_kind_is_accounted_for_by_a_pass(self) -> None:
        """A kind no pass selects is refused, never left out of the document."""
        from swcraft_tools.schema import DataType
        from swcraft_tools.writer.arxml_generation import _DATA_TYPE_PASS_KINDS

        (union, _discriminator) = typing.get_args(DataType)
        assert _DATA_TYPE_PASS_KINDS == frozenset(typing.get_args(union))


# init


class TestTheInitValues:
    def test_an_implementation_record_value_is_a_record_specification_in_element_order(self, fixture_text: str):
        constant = golden_slice(fixture_text, "IExample_speeds_Init", "CONSTANT-SPECIFICATION")
        assert constant == (
            "<CONSTANT-SPECIFICATION>\n"
            "  <SHORT-NAME>IExample_speeds_Init</SHORT-NAME>\n"
            "  <VALUE-SPEC>\n"
            "    <RECORD-VALUE-SPECIFICATION>\n"
            "      <FIELDS>\n"
            "        <NUMERICAL-VALUE-SPECIFICATION>\n"
            "          <SHORT-LABEL>front</SHORT-LABEL>\n"
            "          <VALUE>1</VALUE>\n"
            "        </NUMERICAL-VALUE-SPECIFICATION>\n"
            "        <NUMERICAL-VALUE-SPECIFICATION>\n"
            "          <SHORT-LABEL>rear</SHORT-LABEL>\n"
            "          <VALUE>2</VALUE>\n"
            "        </NUMERICAL-VALUE-SPECIFICATION>\n"
            "      </FIELDS>\n"
            "    </RECORD-VALUE-SPECIFICATION>\n"
            "  </VALUE-SPEC>\n"
            "</CONSTANT-SPECIFICATION>"
        )

    def test_an_implementation_array_value_is_an_array_specification(self, fixture_text: str) -> None:
        constant = golden_slice(fixture_text, "IExample_history_Init", "CONSTANT-SPECIFICATION")
        assert constant.count("<ARRAY-VALUE-SPECIFICATION>") == 1
        assert constant.count("<RECORD-VALUE-SPECIFICATION>") == 4
        assert "<SHORT-LABEL>" in constant and constant.count("<SHORT-LABEL>front</SHORT-LABEL>") == 4

    def test_an_alias_of_a_composite_takes_the_composites_shape(self, fixture_text: str) -> None:
        constant = golden_slice(fixture_text, "IExample_frame_Init", "CONSTANT-SPECIFICATION")
        assert constant.count("<RECORD-VALUE-SPECIFICATION>") == 2, "Frame_Ref -> Frame_T, speeds -> WheelSpeeds_T"
        assert "<SHORT-LABEL>speeds</SHORT-LABEL>" in constant
        assert "<SHORT-LABEL>count</SHORT-LABEL>" in constant

    @pytest.mark.parametrize(
        ("types", "element_type", "init", "rule", "message"),
        [
            ([_RECORD, _POINTER], "Pair_Ptr", 0, "interface.init-shape-mismatch", r"'Pair_Ptr' is a pointer"),
            (
                [_RECORD, _POINTER, {"name": "PtrAlias_T", "kind": "reference_type", "base": "Pair_Ptr"}],
                "PtrAlias_T",
                0,
                "interface.init-shape-mismatch",
                r"'Pair_Ptr' is a pointer",
            ),
            (
                [_RECORD],
                "Pair_T",
                3,
                "interface.init-shape-mismatch",
                r"is not a map, but 'Pair_T' is an implementation "
                r"record",
            ),
            (
                [_RECORD],
                "Pair_T",
                {"first": 1},
                "interface.init-record-field-mismatch",
                r"does not name the elements implementation record 'Pair_T' declares",
            ),
            (
                [_RECORD, _ARRAY],
                "Pairs_T",
                [{"first": 1, "second": 2}],
                "interface.init-array-length-mismatch",
                r"has 1 member\(s\), but implementation array 'Pairs_T' declares length 3",
            ),
            (
                [_RECORD, _ARRAY],
                "Pairs_T",
                {"first": 1},
                "interface.init-shape-mismatch",
                r"is not a list, but 'Pairs_T' is an implementation array",
            ),
            (
                [_RECORD],
                "Pair_T",
                {"first": "x", "second": 2},
                "interface.init-string-scalar",
                r"init\.first is 'x', a string",
            ),
        ],
        ids=[
            "pointer",
            "alias-of-a-pointer",
            "record-scalar",
            "record-keys",
            "array-length",
            "array-map",
            "string-member",
        ],
    )
    def test_a_value_the_kind_cannot_take_is_refused(
        self, tmp_path: Path, types: list[dict[str, Any]], element_type: str, init: object, rule: str, message: str
    ) -> None:
        interface = {
            "name": "IValued",
            "kind": "sender_receiver",
            "data_elements": [{"name": "v", "implementation_type": element_type, "init": init}],
        }
        model_root = _model(tmp_path, types, interfaces=[interface])
        assert rule in _rules(model_root)
        _refused(model_root, tmp_path, message)

    @pytest.mark.parametrize("through_an_alias", [False, True], ids=["direct", "through-an-alias"])
    def test_a_member_typed_by_an_external_stub_refuses_the_value(self, tmp_path: Path, through_an_alias: bool):
        """``init-on-external-type`` reaches a stub-typed member inside a composite."""
        stub = {
            "name": "Foreign_T",
            "kind": "external_type",
            "external_ref": "/Foreign/Foreign_T",
            "external_dest": "IMPLEMENTATION-DATA-TYPE",
        }
        types: list[dict[str, Any]] = [stub]
        member = "Foreign_T"
        if through_an_alias:
            types.append({"name": "ForeignAlias_T", "kind": "reference_type", "base": "Foreign_T"})
            member = "ForeignAlias_T"
        types.append(
            {
                "name": "Mixed_T",
                "kind": "implementation_record",
                "elements": [{"name": "known", "type": "uint8"}, {"name": "foreign", "type": member}],
            }
        )
        interface = {
            "name": "IValued",
            "kind": "sender_receiver",
            "data_elements": [{"name": "v", "implementation_type": "Mixed_T", "init": {"known": 1, "foreign": 2}}],
        }
        model_root = _model(tmp_path, types, interfaces=[interface])
        assert "interface.init-on-external-type" in _rules(model_root)
        refusal = _refused(model_root, tmp_path, r"init\.foreign .*external type stub 'Foreign_T'")
        assert "(interface.init-on-external-type)" in refusal

    def test_a_member_typed_by_an_external_stub_without_an_init_is_generated(self, tmp_path: Path) -> None:
        stub = {
            "name": "Foreign_T",
            "kind": "external_type",
            "external_ref": "/Foreign/Foreign_T",
            "external_dest": "IMPLEMENTATION-DATA-TYPE",
        }
        mixed = {
            "name": "Mixed_T",
            "kind": "implementation_record",
            "elements": [{"name": "known", "type": "uint8"}, {"name": "foreign", "type": "Foreign_T"}],
        }
        interface = {
            "name": "IValued",
            "kind": "sender_receiver",
            "data_elements": [{"name": "v", "implementation_type": "Mixed_T"}],
        }
        model_root = _model(tmp_path, [stub, mixed], interfaces=[interface])
        assert _rules(model_root) == set()
        assert "<SHORT-NAME>Mixed_T</SHORT-NAME>" in _generated(model_root, tmp_path)


# The editing protocol's committed model generates


class TestAModelTheProtocolCommitsGenerates:
    def test_the_three_kinds_the_application_record_and_its_map(self, tmp_path: Path) -> None:
        project = tmp_path / "protocol-project"
        project.mkdir()
        manifest = yaml.safe_load(fixtures.BASE_MANIFEST.read_text(encoding="utf-8"))
        (project / "swcraft.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8", newline="\n")
        shutil.copytree(fixtures.BASE_MODEL_ROOT, project / "model")

        def create(document: dict[str, Any], layer: str = "implementation_types") -> dict[str, Any]:
            return {"operation": "create", "address": {"layer": layer, "element": document["name"]}, "value": document}

        token = handle_request({"operation": "discover"}, project=str(project))["baseline"]
        response = handle_request(
            {
                "operation": "change_set",
                "baseline": token,
                "intent": "implementation composites",
                "operations": [
                    create(_POINTER),
                    create(_ARRAY),
                    create(_RECORD),
                    create(_APP_SCALAR, "application_types"),
                    create(_APP_RECORD, "application_types"),
                    create(
                        {"name": "PairMaps", "maps": [{"application": "Pair", "implementation": "Pair_T"}]},
                        "mapping_sets",
                    ),
                ],
            },
            project=str(project),
            review=lambda proposal: ApprovalDecision(
                change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=False
            ),
        )
        assert response["status"] == "committed", response

        text = _generated(project / "model", tmp_path)
        for name in ("Pair_T", "Pairs_T", "Pair_Ptr", "PairMaps"):
            assert f"<SHORT-NAME>{name}</SHORT-NAME>" in text
