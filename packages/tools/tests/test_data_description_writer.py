"""The ARXML writer writes a unit, a compu method and a data constraint element, and every reference to one.

The subject is the fixture tree ``packages/tools/tests/fixtures/data_description/model``: every
element kind, every reference field, one same-name pair (an application and an implementation type)
and a declared unit named like the private form an application type generates.

What this module holds, at both versions the writer emits:

* each element's XML -- ``UNIT`` (``SHORT-NAME``, ``DESC``, ``DISPLAY-NAME`` when not empty), the
  ``TEXTTABLE`` (``CATEGORY``, one closed single-point scale per entry with ``SHORT-LABEL`` = ``VT`` =
  the entry's name and its ``DESC``), the ``LINEAR`` (``CATEGORY``, ``UNIT-REF``, one scale
  ``[offset, factor] / [1]``, no label, no limits, internal-to-physical only) and the ``DATA-CONSTR``
  (one rule, ``CONSTR-LEVEL`` when set, ``PHYS-CONSTRS`` with each stated limit, ``INTERVAL-TYPE``
  ``CLOSED``) -- each in its layer's package, the packages in the passes' order;
* each reference field's reference (path and ``DEST``) with no private form generated for that
  property, and ``COMPU-METHOD-REF`` on an implementation type where ``group
  SW-DATA-DEF-PROPS-CONTENT`` places it;
* a private form beside a declared element of the same name, in two packages;
* an enumeration's ``init`` naming an entry of the table it names;
* the refusals, each before anything is written (a failed build leaves no outputs).

**Not reached:** schema validity (``test_arxml_xsd_conformance.py`` under ``test-xsd``); the meaning
of a value (its standard citation's); the importer (``test_data_description_import.py``); the
component export (``test_component_data_description.py``).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT, model_element_path, write_model_element
from lxml import etree

FIXTURE = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "data_description" / "model"

VERSIONS = ("4.2.2", "4.3.0")

AR = "{http://autosar.org/schema/r4.0}"


def _local(element: etree._Element) -> str:
    return etree.QName(element).localname


def _children(element: etree._Element) -> list[str]:
    return [_local(child) for child in element]


def _copy(tmp_path: Path) -> Path:
    model_root = tmp_path / "model"
    shutil.copytree(FIXTURE, model_root)
    return model_root


def _generate(model_root: Path, tmp_path: Path, version: str = "4.2.2") -> etree._Element:
    from swcraft_tools.writer.arxml_generation import generate_arxml

    out = tmp_path / "out" / f"model-{version}.arxml"
    generate_arxml(str(model_root), str(out), version=version)
    return etree.parse(str(out)).getroot()


@pytest.fixture(scope="module", params=VERSIONS)
def document(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> etree._Element:
    """The fixture tree's output at one version, generated once per version."""
    return _generate(FIXTURE, tmp_path_factory.mktemp("data_description"), request.param)


def _find(root: etree._Element, path: str) -> etree._Element:
    from swcraft_tools.writer.arxml_xml import find_element

    found = find_element(root, path)
    assert found is not None, path
    return found


def _absent(root: etree._Element, path: str) -> bool:
    from swcraft_tools.writer.arxml_xml import find_element

    return find_element(root, path) is None


def _one(element: etree._Element, tag: str) -> etree._Element:
    found = element.findall(f".//{AR}{tag}")
    assert len(found) == 1, (tag, len(found))
    return found[0]


def _conditional(element: etree._Element) -> etree._Element:
    return _one(element, "SW-DATA-DEF-PROPS-CONDITIONAL")


def _ref(element: etree._Element, tag: str) -> tuple[str | None, str | None]:
    found = element.find(f"{AR}{tag}")
    assert found is not None, tag
    return found.get("DEST"), found.text


def _desc(element: etree._Element) -> str | None:
    desc = element.find(f"{AR}DESC")
    return None if desc is None else desc.findtext(f"{AR}L-2")


# Each element's XML


class TestEachElementIsWrittenFromItsDeclaration:
    def test_the_layer_packages_follow_the_passes_order(self, document: etree._Element) -> None:
        data_types = _find(document, "/DataTypes")
        packages = [
            package.findtext(f"{AR}SHORT-NAME")
            for package in data_types.find(f"{AR}AR-PACKAGES").findall(f"{AR}AR-PACKAGE")
        ]
        assert packages == [
            "Base",
            "Units",
            "CompuMethods",
            "Constraints",
            "Implementation",
            "Application",
            "MappingSets",
        ]

    def test_a_unit_with_a_display_name_and_a_description(self, document: etree._Element) -> None:
        unit = _find(document, "/DataTypes/Units/ProbeUnitSpeed")
        assert _local(unit) == "UNIT"
        assert _children(unit) == ["SHORT-NAME", "DESC", "DISPLAY-NAME"]
        assert unit.findtext(f"{AR}DISPLAY-NAME") == "km/h"
        assert _desc(unit) == "A unit a type and a linear compu method both name."

    def test_a_unit_with_neither_writes_its_short_name_alone(self, document: etree._Element) -> None:
        assert _children(_find(document, "/DataTypes/Units/ProbeUnitBare")) == ["SHORT-NAME"]

    def test_a_unit_with_a_package_is_written_in_that_sub_package(self, document: etree._Element) -> None:
        unit = _find(document, "/DataTypes/Units/ProbeUnitPackage/ProbeUnitPlaced")
        assert _children(unit) == ["SHORT-NAME", "DISPLAY-NAME"]
        assert unit.findtext(f"{AR}DISPLAY-NAME") == "mm"

    def test_the_units_are_written_in_the_layers_order(self, document: etree._Element, tmp_path: Path) -> None:
        from swcraft_tools.data_description import read_data_description

        units = _find(document, "/DataTypes/Units").find(f"{AR}ELEMENTS")
        declared = [unit.name for unit in read_data_description(str(FIXTURE)).units if unit.package is None]
        assert [element.findtext(f"{AR}SHORT-NAME") for element in units] == declared

    def test_a_texttable_has_one_closed_single_point_scale_per_entry(self, document: etree._Element) -> None:
        method = _find(document, "/DataTypes/CompuMethods/ProbeTable")
        assert _local(method) == "COMPU-METHOD"
        assert _children(method) == ["SHORT-NAME", "DESC", "CATEGORY", "COMPU-INTERNAL-TO-PHYS"]
        assert method.findtext(f"{AR}CATEGORY") == "TEXTTABLE"
        assert _desc(method) == "A table an enumeration, a value type and a reference type all name."
        scales = method.findall(f".//{AR}COMPU-SCALE")
        written = []
        for scale in scales:
            lower, upper = scale.find(f"{AR}LOWER-LIMIT"), scale.find(f"{AR}UPPER-LIMIT")
            assert lower.get("INTERVAL-TYPE") == upper.get("INTERVAL-TYPE") == "CLOSED"
            assert lower.text == upper.text
            label = scale.findtext(f"{AR}SHORT-LABEL")
            assert scale.findtext(f"{AR}COMPU-CONST/{AR}VT") == label
            written.append((label, int(lower.text), _desc(scale)))
        assert written == [("ProbeOff", 0, "The first entry."), ("ProbeOn", 1, None)]
        assert _children(scales[0]) == ["SHORT-LABEL", "DESC", "LOWER-LIMIT", "UPPER-LIMIT", "COMPU-CONST"]

    def test_a_linear_has_its_unit_one_scale_and_no_label_and_no_limits(self, document: etree._Element) -> None:
        method = _find(document, "/DataTypes/CompuMethods/ProbeLinear")
        assert _children(method) == ["SHORT-NAME", "DESC", "CATEGORY", "UNIT-REF", "COMPU-INTERNAL-TO-PHYS"]
        assert method.findtext(f"{AR}CATEGORY") == "LINEAR"
        assert _ref(method, "UNIT-REF") == ("UNIT", "/DataTypes/Units/ProbeUnitSpeed")
        (scale,) = method.findall(f".//{AR}COMPU-SCALE")
        assert _children(scale) == ["COMPU-RATIONAL-COEFFS"]
        numerator = [v.text for v in scale.findall(f".//{AR}COMPU-NUMERATOR/{AR}V")]
        denominator = [v.text for v in scale.findall(f".//{AR}COMPU-DENOMINATOR/{AR}V")]
        assert (numerator, denominator) == (["-10", "0.5"], ["1"])

    def test_a_linear_is_written_in_the_internal_to_physical_direction_only(self, document: etree._Element) -> None:
        for path in ("/DataTypes/CompuMethods/ProbeLinear", "/DataTypes/CompuMethods/ProbeTable"):
            assert _find(document, path).find(f"{AR}COMPU-PHYS-TO-INTERNAL") is None

    def test_a_data_constraint_has_one_rule_with_its_level_and_both_limits(self, document: etree._Element) -> None:
        constr = _find(document, "/DataTypes/Constraints/ProbeLimits")
        assert _local(constr) == "DATA-CONSTR"
        assert _desc(constr) == "Both limits and a level."
        (rule,) = constr.findall(f".//{AR}DATA-CONSTR-RULE")
        assert _children(rule) == ["CONSTR-LEVEL", "PHYS-CONSTRS"]
        assert rule.findtext(f"{AR}CONSTR-LEVEL") == "0"
        limits = rule.find(f"{AR}PHYS-CONSTRS")
        assert [(_local(limit), limit.text, limit.get("INTERVAL-TYPE")) for limit in limits] == [
            ("LOWER-LIMIT", "0", "CLOSED"),
            ("UPPER-LIMIT", "250", "CLOSED"),
        ]

    def test_a_half_open_constraint_with_no_level_writes_its_one_limit(self, document: etree._Element) -> None:
        constr = _find(document, "/DataTypes/Constraints/ProbeConstraintPackage/ProbeHalfOpen")
        (rule,) = constr.findall(f".//{AR}DATA-CONSTR-RULE")
        assert _children(rule) == ["PHYS-CONSTRS"]
        assert [(_local(limit), limit.text) for limit in rule.find(f"{AR}PHYS-CONSTRS")] == [("LOWER-LIMIT", "-5.5")]

    def test_a_constraint_stating_neither_limit_writes_an_empty_phys_constrs(self, document: etree._Element) -> None:
        """As ``range: {}`` writes its ``{type}_Range``."""
        constr = _find(document, "/DataTypes/Constraints/ProbeNoLimit")
        assert _desc(constr) == "Neither limit."
        (rule,) = constr.findall(f".//{AR}DATA-CONSTR-RULE")
        assert _children(rule) == ["PHYS-CONSTRS"]
        assert _children(rule.find(f"{AR}PHYS-CONSTRS")) == []

    def test_an_element_nobody_references_is_written(self, document: etree._Element) -> None:
        """An element named by no type is legal, and written."""
        assert _local(_find(document, "/DataTypes/Constraints/ProbeConstraintPackage/ProbeHalfOpen")) == "DATA-CONSTR"
        assert _local(_find(document, "/DataTypes/Units/ProbeLength_Unit")) == "UNIT"


# Each reference field


class TestEachReferenceFieldWritesItsReference:
    def test_an_application_type_stating_all_three_by_reference(self, document: etree._Element) -> None:
        conditional = _conditional(_find(document, "/DataTypes/Application/ProbeSpeed"))
        assert _children(conditional) == ["COMPU-METHOD-REF", "DATA-CONSTR-REF", "UNIT-REF"]
        assert _ref(conditional, "COMPU-METHOD-REF") == ("COMPU-METHOD", "/DataTypes/CompuMethods/ProbeLinear")
        assert _ref(conditional, "DATA-CONSTR-REF") == ("DATA-CONSTR", "/DataTypes/Constraints/ProbeLimits")
        assert _ref(conditional, "UNIT-REF") == ("UNIT", "/DataTypes/Units/ProbeUnitSpeed")

    @pytest.mark.parametrize("suffix", ["_Unit", "_Compu", "_Range"])
    def test_no_private_form_is_generated_for_a_property_stated_by_reference(
        self, document: etree._Element, suffix: str
    ) -> None:
        assert _absent(document, f"/DataTypes/Application/ProbeSpeed{suffix}")

    def test_a_generated_compu_method_names_the_unit_its_type_states_by_reference(
        self, document: etree._Element
    ) -> None:
        """``ProbeMixed`` states its conversion and range inline and its unit by ``unit_ref``."""
        compu = _find(document, "/DataTypes/Application/ProbeMixed_Compu")
        unit = ("UNIT", "/DataTypes/Units/ProbeUnitPackage/ProbeUnitPlaced")
        assert _ref(compu, "UNIT-REF") == unit
        conditional = _conditional(_find(document, "/DataTypes/Application/ProbeMixed"))
        assert _ref(conditional, "UNIT-REF") == unit
        assert _ref(conditional, "COMPU-METHOD-REF") == ("COMPU-METHOD", "/DataTypes/Application/ProbeMixed_Compu")
        assert _ref(conditional, "DATA-CONSTR-REF") == ("DATA-CONSTR", "/DataTypes/Application/ProbeMixed_Range")
        assert _absent(document, "/DataTypes/Application/ProbeMixed_Unit")

    def test_an_enumeration_naming_a_table_generates_no_compu_method(self, document: etree._Element) -> None:
        conditional = _conditional(_find(document, "/DataTypes/Application/ProbeGear"))
        assert _children(conditional) == ["COMPU-METHOD-REF"]
        assert _ref(conditional, "COMPU-METHOD-REF") == ("COMPU-METHOD", "/DataTypes/CompuMethods/ProbeTable")
        assert _absent(document, "/DataTypes/Application/ProbeGear_CompuMethod")

    def test_a_value_type_writes_its_compu_method_after_its_base(self, document: etree._Element) -> None:
        conditional = _conditional(_find(document, "/DataTypes/Implementation/ProbeU8"))
        assert _children(conditional) == ["BASE-TYPE-REF", "SW-CALIBRATION-ACCESS", "COMPU-METHOD-REF"]
        assert _ref(conditional, "COMPU-METHOD-REF") == ("COMPU-METHOD", "/DataTypes/CompuMethods/ProbeTable")

    def test_a_reference_type_writes_its_compu_method_before_its_target(self, document: etree._Element) -> None:
        conditional = _conditional(_find(document, "/DataTypes/Implementation/ProbeU8Ref"))
        assert _children(conditional) == ["COMPU-METHOD-REF", "IMPLEMENTATION-DATA-TYPE-REF"]
        assert _ref(conditional, "COMPU-METHOD-REF") == ("COMPU-METHOD", "/DataTypes/CompuMethods/ProbeTable")

    @pytest.mark.parametrize("version", VERSIONS)
    def test_a_bounded_value_type_writes_its_compu_method_before_its_own_range(
        self, tmp_path: Path, version: str
    ) -> None:
        """``group SW-DATA-DEF-PROPS-CONTENT``: ``COMPU-METHOD-REF`` before ``DATA-CONSTR-REF``."""
        model_root = _copy(tmp_path)
        write_model_element(
            model_root,
            {
                "name": "ProbeU8",
                "kind": "value_type",
                "base": "ProbeU8Base",
                "compu_method_ref": "ProbeTable",
                "value_max": 1,
            },
        )
        conditional = _conditional(_find(_generate(model_root, tmp_path, version), "/DataTypes/Implementation/ProbeU8"))
        assert _children(conditional) == [
            "BASE-TYPE-REF",
            "SW-CALIBRATION-ACCESS",
            "COMPU-METHOD-REF",
            "DATA-CONSTR-REF",
        ]
        assert _ref(conditional, "DATA-CONSTR-REF") == ("DATA-CONSTR", "/DataTypes/Implementation/ProbeU8_Range")

    def test_every_reference_into_data_types_names_an_element_of_its_dest(self, document: etree._Element) -> None:
        """Every ``*-REF`` with a ``DEST`` the document holds resolves, inside it, to an element of that tag."""
        from swcraft_tools.writer.arxml_xml import find_element

        checked = 0
        for reference in document.iter():
            dest, path = reference.get("DEST"), reference.text
            if dest is None or path is None or not path.startswith("/DataTypes/"):
                continue
            target = find_element(document, path)
            assert target is not None and _local(target) == dest, (path, dest)
            checked += 1
        assert checked >= 20


# Two elements of one name, in two packages


class TestTwoElementsOfOneNameLiveInTwoPackages:
    def test_a_private_form_beside_a_declared_element_of_its_name(self, document: etree._Element) -> None:
        """``ProbeLength``'s generated ``ProbeLength_Unit`` and the declared unit of that name: two paths."""
        generated = _find(document, "/DataTypes/Application/ProbeLength_Unit")
        declared = _find(document, "/DataTypes/Units/ProbeLength_Unit")
        assert _children(generated) == ["SHORT-NAME", "DISPLAY-NAME"]
        assert _children(declared) == ["SHORT-NAME", "DESC", "DISPLAY-NAME"]
        conditional = _conditional(_find(document, "/DataTypes/Application/ProbeLength"))
        assert _ref(conditional, "UNIT-REF") == ("UNIT", "/DataTypes/Application/ProbeLength_Unit")

    def test_an_application_and_an_implementation_type_of_one_name(self, document: etree._Element) -> None:
        assert _local(_find(document, "/DataTypes/Application/ProbePair")) == "APPLICATION-PRIMITIVE-DATA-TYPE"
        assert _local(_find(document, "/DataTypes/Implementation/ProbePair")) == "IMPLEMENTATION-DATA-TYPE"
        assert _ref(_conditional(_find(document, "/DataTypes/Application/ProbePair")), "UNIT-REF") == (
            "UNIT",
            "/DataTypes/Units/ProbeUnitBare",
        )
        assert _ref(_conditional(_find(document, "/DataTypes/Implementation/ProbePair")), "COMPU-METHOD-REF") == (
            "COMPU-METHOD",
            "/DataTypes/CompuMethods/ProbeTable",
        )


# An enumeration's init names an entry of the table it names


class TestAnEnumerationInitNamesAnEntryOfItsTable:
    def test_the_entrys_name_is_written(self, document: etree._Element) -> None:
        """The application value form: the entry's name, one ``VT``, not its value."""
        constant = _find(document, "/PortInterfaces/IProbeSignals_gear_Init")
        assert constant.find(f".//{AR}NUMERICAL-VALUE-SPECIFICATION") is None
        assert (
            constant.findtext(f".//{AR}APPLICATION-VALUE-SPECIFICATION/{AR}SW-VALUE-CONT/{AR}SW-VALUES-PHYS/{AR}VT")
            == "ProbeOn"
        )

    def test_a_name_no_entry_carries_is_refused_and_nothing_is_written(self, tmp_path: Path) -> None:
        from swcraft_tools.validator import check_cross_layer_consistency
        from swcraft_tools.writer.arxml_generation import generate_arxml

        model_root = _copy(tmp_path)
        path = model_element_path(model_root, "IProbeSignals", layer="interfaces")
        interface = yaml.safe_load(path.read_text(encoding="utf-8"))
        interface["data_elements"][3]["init"] = "ProbeMissing"
        path.write_text(yaml.safe_dump(interface, sort_keys=False), encoding="utf-8", newline="\n")
        out = tmp_path / "out" / "model.arxml"

        with pytest.raises(ValueError, match=re.escape("enum 'ProbeGear'; it declares ['ProbeOff', 'ProbeOn']")):
            generate_arxml(str(model_root), str(out))

        assert not out.parent.exists()
        assert any("ProbeMissing" in issue for issue in check_cross_layer_consistency(str(model_root)).issues)

    def test_the_emitter_refuses_an_enumeration_whose_table_is_not_resolved(self) -> None:
        """The emitter tests ``NOT_RESOLVED`` rather than iterate it; the refusals run first."""
        from swcraft_tools.data_description import DataDescription
        from swcraft_tools.schema import EnumDataType
        from swcraft_tools.writer.arxml_generation import _emit_value_specification

        description = DataDescription(
            [EnumDataType(name="ProbeGear", kind="enum", compu_method_ref="ProbeMissing", description="")]
        )
        parent = etree.Element("VALUE-SPEC")

        with pytest.raises(TypeError, match="^enum type 'ProbeGear' names no texttable"):
            _emit_value_specification(parent, "ProbeOn", "ProbeGear", description, application=True, own=True)


# The refusals, each before anything is written


def _write_raw(model_root: Path, layer: str, file_name: str, payload: dict[str, Any]) -> None:
    """Write *payload* under a file name of the test's choosing: two files may declare one name."""
    from swcraft_tools.model_registry import production_registry

    directory = model_root / production_registry().storage(layer).directory
    directory.mkdir(parents=True, exist_ok=True)
    (directory / file_name).write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8", newline="\n")


#: One model change per element rule the writer refuses, with the rule's id.
REFUSALS: dict[str, tuple[str, Any]] = {
    "unit.duplicate-name": (
        "unit.duplicate-name",
        lambda root: _write_raw(root, "units", "ProbeUnitSpeedAgain.yaml", {"name": "ProbeUnitSpeed"}),
    ),
    "compu-method.duplicate-name": (
        "compu-method.duplicate-name",
        lambda root: _write_raw(
            root,
            "compu_methods",
            "ProbeLinearAgain.yaml",
            {"name": "ProbeLinear", "kind": "linear", "unit": "ProbeUnitBare"},
        ),
    ),
    "data-constr.duplicate-name": (
        "data-constr.duplicate-name",
        lambda root: _write_raw(root, "data_constraints", "ProbeLimitsAgain.yaml", {"name": "ProbeLimits"}),
    ),
    "data-type.unknown-unit": (
        "data-type.unknown-unit",
        lambda root: write_model_element(
            root, {"name": "ProbePair", "kind": "application_type", "unit_ref": "ProbeNone"}
        ),
    ),
    "data-type.unknown-compu-method": (
        "data-type.unknown-compu-method",
        lambda root: write_model_element(root, {"name": "ProbeGear", "kind": "enum", "compu_method_ref": "ProbeNone"}),
    ),
    "data-type.unknown-data-constr": (
        "data-type.unknown-data-constr",
        lambda root: write_model_element(
            root, {"name": "ProbePair", "kind": "application_type", "data_constr_ref": "ProbeNone"}
        ),
    ),
    "compu-method.unknown-unit": (
        "compu-method.unknown-unit",
        lambda root: write_model_element(
            root, {"name": "ProbeLinear", "kind": "linear", "unit": "ProbeNone"}, layer="compu_methods"
        ),
    ),
    "data-type.compu-method-wrong-category": (
        "data-type.compu-method-wrong-category",
        lambda root: write_model_element(
            root, {"name": "ProbeU8", "kind": "value_type", "base": "ProbeU8Base", "compu_method_ref": "ProbeLinear"}
        ),
    ),
    "data-type.compu-method-on-unresolved-reference-type": (
        "data-type.compu-method-on-unresolved-reference-type",
        lambda root: (
            write_model_element(root, {"name": "ProbeU8Ptr", "kind": "pointer", "target": "ProbeU8"}),
            write_model_element(
                root,
                {
                    "name": "ProbeU8Ref",
                    "kind": "reference_type",
                    "base": "ProbeU8Ptr",
                    "compu_method_ref": "ProbeTable",
                },
            ),
        ),
    ),
    "compu-method.table-entry-repeated": (
        "compu-method.table-entry-repeated",
        lambda root: write_model_element(
            root,
            {
                "name": "ProbeTable",
                "kind": "texttable",
                "entries": [{"name": "ProbeOff", "value": 0}, {"name": "ProbeOn", "value": 0}],
            },
            layer="compu_methods",
        ),
    ),
    "data-type.conversion-without-unit": (
        "data-type.conversion-without-unit",
        lambda root: write_model_element(
            root, {"name": "ProbeMixed", "kind": "application_type", "conversion": {"factor": 2, "offset": 1}}
        ),
    ),
    "text.xml-unrepresentable-character": (
        "text.xml-unrepresentable-character",
        lambda root: write_model_element(root, {"name": "ProbeUnitBare", "display_name": "km\x01h"}, layer="units"),
    ),
}


class TestTheWriterRefusesBeforeAnythingIsWritten:
    @pytest.mark.parametrize("case", sorted(REFUSALS))
    def test_each_rule_is_refused_in_check_models_words(self, tmp_path: Path, case: str) -> None:
        from swcraft_tools.validator import check_cross_layer_consistency
        from swcraft_tools.writer.arxml_generation import generate_arxml

        rule, change = REFUSALS[case]
        model_root = _copy(tmp_path)
        change(model_root)
        out = tmp_path / "out" / "model.arxml"

        with pytest.raises(ValueError, match=f"rule:{rule}") as refused:
            generate_arxml(str(model_root), str(out))

        assert not out.parent.exists()
        assert str(tmp_path) not in str(refused.value)
        assert rule in {record.rule for record in check_cross_layer_consistency(str(model_root)).records}

    @pytest.mark.parametrize(
        ("layer", "payload", "offender"),
        [
            ("units", {"name": "ProbeUnitBare", "package": "Probe-Package"}, "Probe-Package"),
            (
                "compu_methods",
                {"name": "ProbeTable", "kind": "texttable", "entries": [{"name": "1Probe", "value": 0}]},
                "1Probe",
            ),
            ("data_constraints", {"name": "ProbeLimits", "package": "Probe Package"}, "Probe Package"),
        ],
        ids=["unit-package", "texttable-entry", "constraint-package"],
    )
    def test_a_name_that_is_no_identifier_is_refused(
        self, tmp_path: Path, layer: str, payload: dict[str, Any], offender: str
    ) -> None:
        """``rule:identifier.declared``'s writer twin: an element's package and a table entry's name."""
        from swcraft_tools.validator import check_cross_layer_consistency
        from swcraft_tools.writer.arxml_generation import generate_arxml

        model_root = _copy(tmp_path)
        write_model_element(model_root, payload, layer=layer)
        out = tmp_path / "out" / "model.arxml"

        with pytest.raises(ValueError, match="Non-conforming SHORT-NAME") as refused:
            generate_arxml(str(model_root), str(out))

        assert f"'{offender}'" in str(refused.value)
        assert not out.parent.exists()
        records = check_cross_layer_consistency(str(model_root)).records
        assert any(record.rule == "identifier.declared" and offender in record.message for record in records)

    def test_an_element_name_that_is_no_identifier_is_refused(self, tmp_path: Path) -> None:
        from swcraft_tools.writer.arxml_generation import generate_arxml

        model_root = _copy(tmp_path)
        _write_raw(model_root, "units", "ProbeBadUnit.yaml", {"name": "1ProbeUnit"})
        out = tmp_path / "out" / "model.arxml"

        with pytest.raises(ValueError, match="Unit name: '1ProbeUnit'"):
            generate_arxml(str(model_root), str(out))

        assert not out.parent.exists()

    def test_the_name_guard_labels_each_element_class_and_refuses_any_other(self) -> None:
        """One label per element class, and a class the table does not name is refused."""
        from swcraft_tools.schema import DataConstr, Linear, MappingSet, TextTable, Unit
        from swcraft_tools.writer.elements import _check_data_description_element_names

        for element, label in (
            (Unit.model_construct(name="1Probe"), "Unit"),
            (TextTable.model_construct(name="1Probe", entries=[]), "Compu method"),
            (Linear.model_construct(name="1Probe"), "Compu method"),
            (DataConstr.model_construct(name="1Probe"), "Data constraint"),
        ):
            with pytest.raises(ValueError, match=f"^{label} name '1Probe' is not a legal AUTOSAR Identifier"):
                _check_data_description_element_names("", element)
        with pytest.raises(TypeError, match="^no name guard labels the element kind MappingSet$"):
            _check_data_description_element_names("", MappingSet.model_construct(name="ProbeSet"))


# The fixture tree itself


def test_the_fixture_tree_is_consistent_and_states_every_kind_and_reference_field() -> None:
    """``check-model`` accepts the tree, and it states each of the five kinds and the six reference fields."""
    from swcraft_tools.data_description import read_data_description
    from swcraft_tools.validator import check_cross_layer_consistency

    assert check_cross_layer_consistency(str(FIXTURE)).issues == []
    description = read_data_description(str(FIXTURE))
    kinds = {
        type(element).__name__
        for element in (*description.units, *description.compu_methods, *description.data_constraints)
    }
    assert kinds == {"Unit", "TextTable", "Linear", "DataConstr"}
    assert any(entry for method in description.compu_methods if hasattr(method, "entries") for entry in method.entries)
    stated = {
        (type(dt).__name__, field)
        for dt in description.data_types
        for field in ("unit_ref", "compu_method_ref", "data_constr_ref")
        if getattr(dt, field, None) is not None
    }
    assert stated == {
        ("ApplicationDataType", "unit_ref"),
        ("ApplicationDataType", "compu_method_ref"),
        ("ApplicationDataType", "data_constr_ref"),
        ("EnumDataType", "compu_method_ref"),
        ("ValueDataType", "compu_method_ref"),
        ("ReferenceDataType", "compu_method_ref"),
    }
    assert description.application("ProbePair") is not None
    assert description.implementation("ProbePair") is not None
