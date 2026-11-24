"""The writer's categories, the compu method's unit and the classified com-spec defaults.

* **a composite member's ``CATEGORY`` is its type's** (``[constr_1152]``): an
  ``APPLICATION-RECORD-ELEMENT`` and an array ``ELEMENT`` typed by a record carry
  ``STRUCTURE``, by an array ``ARRAY``, by an application type or an enum ``VALUE``;
* **a ``LINEAR`` compu method refers to the type's unit** (``[constr_1175]``):
  ``UNIT-REF`` to the type's own ``{name}_Unit``, second in
  the ``COMPU-METHOD`` after ``CATEGORY``; a ``conversion`` declared with no
  ``unit`` is ``rule:data-type.conversion-without-unit``, refused by the writer
  at 4.2.2 and 4.3.0 with no file written, by a component export and by the
  emitter itself;
* **no ``FILTER``** on a receiver com-spec (``[constr_1044]``);
* **``HANDLE-OUT-OF-RANGE NONE``** on every sender and receiver com-spec and
  **``HANDLE-TIMEOUT-TYPE NONE``** on every non-queued receiver com-spec, at
  the XSD's positions: each is multiplicity 1 with no default.

What it does **not** reach: the XSD validity of the new shapes
(``test_arxml_xsd_conformance.py``'s ``TestTheWriterDefaultsConform``), and
that a moved golden moved by nothing else (``test_writer_fidelity_refreeze.py``).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import GOLDEN_MODEL_ROOTS, REPO_ROOT, model_element_path, write_model_element
from fidelity_strip_support import strip_counts
from lxml import etree
from swcraft_tools.arxml_version import NAMESPACE
from swcraft_tools.component_exchange import export_all_components, export_component
from swcraft_tools.component_exchange.export import ClosureValidationError
from swcraft_tools.schema import ApplicationDataType, Conversion
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer.arxml_generation import (
    COM_SPEC_SILENT_VALUES,
    _emit_application_compu_method,
    generate_arxml,
)
from swcraft_tools.writer.arxml_xml import ArxmlDocument

NS = f"{{{NAMESPACE}}}"
TESTS = Path(__file__).resolve().parent
EXCHANGE_MODEL_ROOT = TESTS / "fixtures" / "component_exchange" / "model"
INIT_VALUES_MODEL_ROOT = TESTS / "fixtures" / "init_values" / "model"
GOLDEN_DIR = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "arxml_golden"
VERSIONS = ("4.2.2", "4.3.0")
UNITLESS_MESSAGE = (
    "Application type 'Velocity_T' declares a conversion and no unit: SWCraft writes the conversion as a "
    "LINEAR compu method, which must refer to a unit. Declare the type's unit."
)


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _dump(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _local(node: etree._Element) -> str:
    return etree.QName(node).localname


def _named(root: etree._Element, tag: str, name: str) -> etree._Element:
    matches = [node for node in root.iter(f"{NS}{tag}") if node.findtext(f"{NS}SHORT-NAME") == name]
    assert len(matches) == 1, (tag, name, len(matches))
    return matches[0]


def _generate(model_root: Path, out: Path, version: str = "4.2.2") -> etree._Element:
    generate_arxml(str(model_root), str(out), version=version)
    return etree.parse(str(out)).getroot()


def _members_model(root: Path) -> Path:
    """``init_values`` plus a record and an array whose members name each application kind."""
    shutil.copytree(INIT_VALUES_MODEL_ROOT, root)
    write_model_element(
        root,
        {
            "name": "Mixed_T",
            "kind": "record",
            "fields": [
                {"name": "floats", "type": "Floats_T"},
                {"name": "cell", "type": "Cell_T"},
                {"name": "count", "type": "Count_T"},
                {"name": "mode", "type": "Mode_T"},
            ],
        },
    )
    write_model_element(root, {"name": "Grids_T", "kind": "array", "element_type": "Grid_T", "length": 3})
    return root


def _exchange_model(root: Path, **fields_by_type: dict[str, Any]) -> Path:
    """The ``component_exchange`` tree, each named data type given its fields (``None`` removes one)."""
    shutil.copytree(EXCHANGE_MODEL_ROOT, root)
    for name, fields in fields_by_type.items():
        path = model_element_path(root, name, kind="application_type")
        document = _load(path)
        for key, value in fields.items():
            if value is None:
                document.pop(key, None)
            else:
                document[key] = value
        _dump(path, document)
    return root


# ---------------------------------------------------------------------------
# A composite member's CATEGORY is its type's
# ---------------------------------------------------------------------------


class TestTheMemberCategoryIsItsTypes:
    @pytest.mark.parametrize("version", VERSIONS)
    def test_each_member_carries_its_types_category(self, tmp_path: Path, version: str) -> None:
        root = _generate(_members_model(tmp_path / "model"), tmp_path / "out.arxml", version)

        record = _named(root, "APPLICATION-RECORD-DATA-TYPE", "Mixed_T")
        categories = {
            element.findtext(f"{NS}SHORT-NAME"): element.findtext(f"{NS}CATEGORY")
            for element in record.iter(f"{NS}APPLICATION-RECORD-ELEMENT")
        }
        assert categories == {"floats": "ARRAY", "cell": "STRUCTURE", "count": "VALUE", "mode": "VALUE"}
        assert _named(root, "APPLICATION-ARRAY-DATA-TYPE", "Grids_T").findtext(f"{NS}ELEMENT/{NS}CATEGORY") == ("ARRAY")
        # The committed tree's own composite members: an array of records, a record of a record.
        assert _named(root, "APPLICATION-ARRAY-DATA-TYPE", "Grid_T").findtext(f"{NS}ELEMENT/{NS}CATEGORY") == (
            "STRUCTURE"
        )
        assert _named(root, "APPLICATION-RECORD-ELEMENT", "row").findtext(f"{NS}CATEGORY") == "STRUCTURE"

    @pytest.mark.parametrize("version", VERSIONS)
    def test_every_member_category_equals_the_referenced_types(self, tmp_path: Path, version: str) -> None:
        """``[constr_1152]``, read off the document itself: the member and the type it references agree."""
        root = _generate(_members_model(tmp_path / "model"), tmp_path / "out.arxml", version)
        by_path: dict[str, str] = {}
        for tag in ("APPLICATION-PRIMITIVE-DATA-TYPE", "APPLICATION-RECORD-DATA-TYPE", "APPLICATION-ARRAY-DATA-TYPE"):
            for node in root.iter(f"{NS}{tag}"):
                by_path[f"/DataTypes/Application/{node.findtext(f'{NS}SHORT-NAME')}"] = node.findtext(f"{NS}CATEGORY")

        members = [*root.iter(f"{NS}APPLICATION-RECORD-ELEMENT")]
        members += [array.find(f"{NS}ELEMENT") for array in root.iter(f"{NS}APPLICATION-ARRAY-DATA-TYPE")]
        assert len(members) >= 12
        for member in members:
            assert member.findtext(f"{NS}CATEGORY") == by_path[member.findtext(f"{NS}TYPE-TREF")]

    def test_the_implementation_elements_are_unchanged(self, tmp_path: Path) -> None:
        """An ``IMPLEMENTATION-DATA-TYPE-ELEMENT`` keeps ``TYPE_REFERENCE``: the rule is the application view's."""
        model_root = _members_model(tmp_path / "model")
        # The tree declares no implementation composite of its own; these two give the claim elements.
        for document in (
            {
                "name": "Frame_T",
                "kind": "implementation_record",
                "elements": [{"name": "head", "type": "float32"}, {"name": "flag", "type": "boolean"}],
            },
            {
                "name": "Buf_T",
                "kind": "implementation_array",
                "element_type": "float32",
                "length": 4,
                "element_name": "Slot",
            },
        ):
            _dump(model_element_path(model_root, document["name"], kind=document["kind"]), document)
        root = _generate(model_root, tmp_path / "out.arxml")

        assert len(list(root.iter(f"{NS}IMPLEMENTATION-DATA-TYPE-ELEMENT"))) >= 3
        categories = {
            element.findtext(f"{NS}CATEGORY") for element in root.iter(f"{NS}IMPLEMENTATION-DATA-TYPE-ELEMENT")
        }
        assert categories == {"TYPE_REFERENCE"}


# ---------------------------------------------------------------------------
# The LINEAR compu method's UNIT-REF, and the refusal without a unit
# ---------------------------------------------------------------------------


class TestTheCompuMethodRefersToTheUnit:
    @pytest.mark.parametrize("version", VERSIONS)
    def test_unit_ref_is_second_and_names_the_types_own_unit(self, tmp_path: Path, version: str) -> None:
        root = _generate(_exchange_model(tmp_path / "model"), tmp_path / "out.arxml", version)

        compu = _named(root, "COMPU-METHOD", "Velocity_T_Compu")
        assert [_local(child) for child in compu] == ["SHORT-NAME", "CATEGORY", "UNIT-REF", "COMPU-INTERNAL-TO-PHYS"]
        unit_ref = compu.find(f"{NS}UNIT-REF")
        assert unit_ref.text == "/DataTypes/Application/Velocity_T_Unit"
        assert unit_ref.get("DEST") == "UNIT"
        assert _named(root, "UNIT", "Velocity_T_Unit").findtext(f"{NS}DISPLAY-NAME") == "km/h"
        # One element referenced twice: the type's own props name the same unit.
        (type_unit_ref,) = _named(root, "APPLICATION-PRIMITIVE-DATA-TYPE", "Velocity_T").iter(f"{NS}UNIT-REF")
        assert type_unit_ref.text == unit_ref.text

    def test_the_identity_conversion_refers_to_it_too(self, tmp_path: Path) -> None:
        model_root = _exchange_model(tmp_path / "model", Velocity_T={"conversion": {}})
        root = _generate(model_root, tmp_path / "out.arxml")

        assert _named(root, "COMPU-METHOD", "Velocity_T_Compu").findtext(f"{NS}UNIT-REF") == (
            "/DataTypes/Application/Velocity_T_Unit"
        )

    def test_a_placed_type_refers_to_its_placed_unit(self, tmp_path: Path) -> None:
        model_root = _exchange_model(tmp_path / "model", Velocity_T={"package": "Physics"})
        root = _generate(model_root, tmp_path / "out.arxml")

        assert _named(root, "COMPU-METHOD", "Velocity_T_Compu").findtext(f"{NS}UNIT-REF") == (
            "/DataTypes/Application/Physics/Velocity_T_Unit"
        )

    def test_a_unit_without_a_conversion_writes_no_compu_method(self, tmp_path: Path) -> None:
        model_root = _exchange_model(tmp_path / "model", Velocity_T={"conversion": None})
        root = _generate(model_root, tmp_path / "out.arxml")

        assert [node for node in root.iter(f"{NS}COMPU-METHOD") if node.findtext(f"{NS}CATEGORY") == "LINEAR"] == []


class TestAConversionWithoutAUnitIsRefused:
    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize("unit", [None, "", "   "], ids=["absent", "empty", "blank"])
    def test_the_writer_refuses_with_the_rules_message_and_writes_nothing(
        self, tmp_path: Path, version: str, unit: str | None
    ) -> None:
        model_root = _exchange_model(tmp_path / "model", Velocity_T={"unit": unit})
        out = tmp_path / "out.arxml"

        with pytest.raises(ValueError, match="rule:data-type.conversion-without-unit") as caught:
            generate_arxml(str(model_root), str(out), version=version)

        assert UNITLESS_MESSAGE in str(caught.value)
        assert not out.exists()

    def test_check_model_reports_the_same_sentence(self, tmp_path: Path) -> None:
        model_root = _exchange_model(tmp_path / "model", Velocity_T={"unit": None})

        result = check_cross_layer_consistency(str(model_root))

        assert [record.message for record in result.records if record.rule == "data-type.conversion-without-unit"] == [
            UNITLESS_MESSAGE
        ]

    @pytest.mark.parametrize("version", VERSIONS)
    def test_a_component_export_refuses_and_writes_nothing(self, tmp_path: Path, version: str) -> None:
        model_root = _exchange_model(tmp_path / "model", Velocity_T={"unit": None})
        out = tmp_path / "SensorSwc.arxml"

        with pytest.raises(ClosureValidationError) as caught:
            export_component(str(model_root), "SensorSwc", str(out), version=version)

        assert caught.value.issues == [UNITLESS_MESSAGE]
        assert not out.exists()

    def test_all_components_lists_the_refused_one_and_goes_on(self, tmp_path: Path) -> None:
        model_root = _exchange_model(tmp_path / "model", Velocity_T={"unit": None})
        out_dir = tmp_path / "out"

        reports = {report.swc: report for report in export_all_components(str(model_root), str(out_dir))}

        assert reports["SensorSwc"].status != "ok"
        assert UNITLESS_MESSAGE in (reports["SensorSwc"].error or "")
        assert not (out_dir / "SensorSwc.arxml").exists()
        # A component whose closure reaches no such type is exported.
        assert reports["ActuatorSwc"].status == "ok"
        assert (out_dir / "ActuatorSwc.arxml").exists()

    def test_the_emitter_itself_refuses(self) -> None:
        """The last line of defence, for a caller that reaches the emitter without the guard."""
        unitless = ApplicationDataType(name="Velocity_T", kind="application_type", conversion=Conversion())

        with pytest.raises(ValueError, match="rule:data-type.conversion-without-unit") as caught:
            _emit_application_compu_method(ArxmlDocument().data_types, unitless)

        assert UNITLESS_MESSAGE in str(caught.value)


# ---------------------------------------------------------------------------
# No FILTER, and the two multiplicity-1 attributes
# ---------------------------------------------------------------------------

_RECEIVER_ORDER = [
    "DATA-ELEMENT-REF",
    "HANDLE-OUT-OF-RANGE",
    "USES-END-TO-END-PROTECTION",
    "ALIVE-TIMEOUT",
    "ENABLE-UPDATE",
    "HANDLE-NEVER-RECEIVED",
    "HANDLE-TIMEOUT-TYPE",
]
_SENDER_ORDER = ["DATA-ELEMENT-REF", "HANDLE-OUT-OF-RANGE", "USES-END-TO-END-PROTECTION"]
_SILENT_TEXTS = {
    "HANDLE-OUT-OF-RANGE": "NONE",
    "USES-END-TO-END-PROTECTION": "false",
    "ALIVE-TIMEOUT": "0",
    "ENABLE-UPDATE": "false",
    "HANDLE-NEVER-RECEIVED": "false",
    "HANDLE-TIMEOUT-TYPE": "NONE",
}


def _children_without_init(spec: etree._Element) -> list[str]:
    tags = [_local(child) for child in spec]
    return tags[:-1] if tags and tags[-1] == "INIT-VALUE" else tags


class TestTheComSpecDefaults:
    def test_the_table_holds_the_six_attributes_in_the_receivers_order(self) -> None:
        """The writer emits from ``COM_SPEC_SILENT_VALUES``, so its key order is the emission order."""
        assert list(COM_SPEC_SILENT_VALUES) == _RECEIVER_ORDER[1:]
        assert {tag: attribute.silent for tag, attribute in COM_SPEC_SILENT_VALUES.items()} == {
            "HANDLE-OUT-OF-RANGE": "none",
            "USES-END-TO-END-PROTECTION": False,
            "ALIVE-TIMEOUT": 0.0,
            "ENABLE-UPDATE": False,
            "HANDLE-NEVER-RECEIVED": False,
            "HANDLE-TIMEOUT-TYPE": "none",
        }

    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize("model", ["init_values", "component_exchange"])
    def test_each_com_spec_holds_the_silent_values_in_the_xsds_order(
        self, tmp_path: Path, version: str, model: str
    ) -> None:
        source = INIT_VALUES_MODEL_ROOT if model == "init_values" else EXCHANGE_MODEL_ROOT
        root = _generate(source, tmp_path / "out.arxml", version)

        receivers = list(root.iter(f"{NS}NONQUEUED-RECEIVER-COM-SPEC"))
        senders = list(root.iter(f"{NS}NONQUEUED-SENDER-COM-SPEC"))
        assert receivers
        assert senders
        for spec, order in [(spec, _RECEIVER_ORDER) for spec in receivers] + [
            (spec, _SENDER_ORDER) for spec in senders
        ]:
            assert _children_without_init(spec) == order
            for tag in order[1:]:
                assert spec.findtext(f"{NS}{tag}") == _SILENT_TEXTS[tag]

    def test_the_init_value_stays_last(self, tmp_path: Path) -> None:
        root = _generate(INIT_VALUES_MODEL_ROOT, tmp_path / "out.arxml")

        valued = [
            spec
            for tag in ("NONQUEUED-RECEIVER-COM-SPEC", "NONQUEUED-SENDER-COM-SPEC")
            for spec in root.iter(f"{NS}{tag}")
            if spec.find(f"{NS}INIT-VALUE") is not None
        ]
        assert valued
        for spec in valued:
            assert _local(spec[-1]) == "INIT-VALUE"
            if _local(spec) == "NONQUEUED-RECEIVER-COM-SPEC":
                assert _local(spec[-2]) == "HANDLE-TIMEOUT-TYPE"

    @pytest.mark.parametrize("version", VERSIONS)
    def test_no_filter_is_written(self, tmp_path: Path, version: str) -> None:
        for source in (INIT_VALUES_MODEL_ROOT, EXCHANGE_MODEL_ROOT, REPO_ROOT / "examples" / "basic" / "model"):
            root = _generate(source, tmp_path / f"{source.parent.name}.arxml", version)
            assert list(root.iter(f"{NS}FILTER")) == []
            assert list(root.iter(f"{NS}DATA-FILTER-TYPE")) == []


# ---------------------------------------------------------------------------
# The re-frozen goldens carry the moves (their strip to the pre-change digest is
# test_writer_fidelity_refreeze.py's)
# ---------------------------------------------------------------------------


class TestTheGoldensCarryTheMoves:
    @pytest.mark.parametrize("golden", sorted(GOLDEN_MODEL_ROOTS))
    def test_a_populated_golden_carries_the_filter_and_handling_moves(self, golden: str) -> None:
        _, counts = strip_counts((GOLDEN_DIR / f"{golden}.arxml").read_bytes())

        assert counts["filters"] > 0
        assert counts["range-and-timeout-handling"] > 0

    def test_the_composite_golden_carries_the_unit_ref(self) -> None:
        _, counts = strip_counts((GOLDEN_DIR / "composite-4.2.2.arxml").read_bytes())

        assert counts["compu-method-units"] == 1
