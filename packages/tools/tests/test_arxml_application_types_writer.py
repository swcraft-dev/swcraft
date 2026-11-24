"""The writer's emission of application types and mapping sets.

What this module is the oracle for:

* the **bytes** of the ``APPLICATION-PRIMITIVE-DATA-TYPE`` of category ``VALUE``, the ``UNIT``, the
  ``LINEAR`` ``COMPU-METHOD``, the ``DATA-CONSTR``, the ``DATA-TYPE-MAPPING-SET`` and the behaviour's
  ``DATA-TYPE-MAPPING-REFS``, asserted as text so a reordered child is a failure;
* the **arithmetic** of the one conversion, computed by hand in
  :class:`TestTheConversionCoefficientsAreInTheOrderTheStandardReads`: the only oracle against swapped
  coefficients, as both orders are XSD-valid;
* the **refusal** a composite member naming an implementation type gets, and that it leaves nothing on disk;
* the writer invents no ``{name}_AppView`` and no ``DataTypeMappingSet``, and neither name is reserved for an
  authored element.

**Not** an oracle for the schema's containment (``test_arxml_xsd_conformance.py``), nor a byte-identity
gate for models without the feature (the goldens and ``test_application_types_byte_identity.py``).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import golden_slice, make_empty_model_root, model_element_path, write_model_element
from lxml import etree
from swcraft_tools.data_description import DataDescription
from swcraft_tools.schema import ApplicationDataType, Conversion, MappingSet, ValueRange
from swcraft_tools.writer.arxml_generation import (
    _emit_application_type,
    _emit_mapping_set,
    build_workspace,
    generate_arxml,
    generated_application_forms,
)
from swcraft_tools.writer.arxml_xml import ArxmlDocument, serialize_fragment

pytestmark = pytest.mark.xdist_group("application_types_writer")


def _dump(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def _base_types(model_root: Path) -> None:
    """The two implementation types every model below maps onto."""
    dt = model_root / "data_types"
    _dump(
        dt / "base" / "uint16_base.yaml",
        {"name": "uint16_base", "kind": "base_type", "size": 16, "encoding": "NONE", "native_declaration": "uint16"},
    )
    _dump(dt / "implementation" / "uint16.yaml", {"name": "uint16", "kind": "value_type", "base": "uint16_base"})


def _emit_into_data_types(emit: Any, *args: Any) -> str:
    """Run one emitter into a throwaway ``DataTypes`` package; serialize **every** element it added.

    An application type's ``UNIT``, ``COMPU-METHOD`` and ``DATA-CONSTR`` are siblings appended before it.
    """
    document = ArxmlDocument()
    emit(document.data_types, *args)
    return "\n".join(serialize_fragment(node) for node in document.data_types.elements)


# The application type, subset by subset


class TestTheApplicationTypeEmitsWhatItDeclaresAndNothingElse:
    """Each of ``unit``, ``conversion`` and ``range`` is emitted only when declared."""

    def test_a_type_with_no_physics_carries_the_category_alone(self) -> None:
        declared = ApplicationDataType(name="Bare", kind="application_type")
        assert _emit_into_data_types(_emit_application_type, declared) == (
            "<APPLICATION-PRIMITIVE-DATA-TYPE>\n"
            "  <SHORT-NAME>Bare</SHORT-NAME>\n"
            "  <CATEGORY>VALUE</CATEGORY>\n"
            "</APPLICATION-PRIMITIVE-DATA-TYPE>"
        )

    @pytest.mark.parametrize("unit", ["  km/h  ", "km/h\n", "\tdeg C", "m  /  s"])
    def test_a_unit_is_written_exactly_as_the_model_holds_it(self, unit: str) -> None:
        """The writer changes no value: a stripped ``DISPLAY-NAME`` would be a unit changed on its way out.

        The reader's half and the round trip are ``test_unit_round_trip.py``.
        """
        declared = ApplicationDataType(name="Speed", kind="application_type", unit=unit)
        document = ArxmlDocument()
        _emit_application_type(document.data_types, declared)
        (node,) = [n for n in document.data_types.elements if n.tag.endswith("}UNIT")]
        assert node.findtext("{*}DISPLAY-NAME") == unit

    def test_a_unit_of_blanks_is_still_no_unit(self) -> None:
        declared = ApplicationDataType(name="Speed", kind="application_type", unit="   ")
        assert "<UNIT" not in _emit_into_data_types(_emit_application_type, declared)

    def test_a_declared_unit_adds_the_unit_element_and_its_reference(self) -> None:
        declared = ApplicationDataType(name="Speed", kind="application_type", unit="km/h")
        assert _emit_into_data_types(_emit_application_type, declared) == (
            "<UNIT>\n"
            "  <SHORT-NAME>Speed_Unit</SHORT-NAME>\n"
            "  <DISPLAY-NAME>km/h</DISPLAY-NAME>\n"
            "</UNIT>\n"
            "<APPLICATION-PRIMITIVE-DATA-TYPE>\n"
            "  <SHORT-NAME>Speed</SHORT-NAME>\n"
            "  <CATEGORY>VALUE</CATEGORY>\n"
            "  <SW-DATA-DEF-PROPS>\n"
            "    <SW-DATA-DEF-PROPS-VARIANTS>\n"
            "      <SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            '        <UNIT-REF DEST="UNIT">/DataTypes/Application/Speed_Unit</UNIT-REF>\n'
            "      </SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            "    </SW-DATA-DEF-PROPS-VARIANTS>\n"
            "  </SW-DATA-DEF-PROPS>\n"
            "</APPLICATION-PRIMITIVE-DATA-TYPE>"
        )

    def test_a_declared_range_adds_the_data_constr_and_its_reference(self) -> None:
        declared = ApplicationDataType(name="Speed", kind="application_type", range=ValueRange(min=0.0, max=400.0))
        assert _emit_into_data_types(_emit_application_type, declared) == (
            "<DATA-CONSTR>\n"
            "  <SHORT-NAME>Speed_Range</SHORT-NAME>\n"
            "  <DATA-CONSTR-RULES>\n"
            "    <DATA-CONSTR-RULE>\n"
            "      <PHYS-CONSTRS>\n"
            '        <LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT>\n'
            '        <UPPER-LIMIT INTERVAL-TYPE="CLOSED">400</UPPER-LIMIT>\n'
            "      </PHYS-CONSTRS>\n"
            "    </DATA-CONSTR-RULE>\n"
            "  </DATA-CONSTR-RULES>\n"
            "</DATA-CONSTR>\n"
            "<APPLICATION-PRIMITIVE-DATA-TYPE>\n"
            "  <SHORT-NAME>Speed</SHORT-NAME>\n"
            "  <CATEGORY>VALUE</CATEGORY>\n"
            "  <SW-DATA-DEF-PROPS>\n"
            "    <SW-DATA-DEF-PROPS-VARIANTS>\n"
            "      <SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            '        <DATA-CONSTR-REF DEST="DATA-CONSTR">/DataTypes/Application/Speed_Range</DATA-CONSTR-REF>\n'
            "      </SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            "    </SW-DATA-DEF-PROPS-VARIANTS>\n"
            "  </SW-DATA-DEF-PROPS>\n"
            "</APPLICATION-PRIMITIVE-DATA-TYPE>"
        )

    def test_a_half_open_range_emits_the_declared_bound_alone(self) -> None:
        declared = ApplicationDataType(name="Speed", kind="application_type", range=ValueRange(min=0.0))
        fragment = _emit_into_data_types(_emit_application_type, declared)
        assert '<LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT>' in fragment
        assert "UPPER-LIMIT" not in fragment

    def test_all_three_are_emitted_in_the_schemas_own_order(self) -> None:
        declared = ApplicationDataType(
            name="Speed",
            kind="application_type",
            unit="km/h",
            conversion=Conversion(factor=0.1, offset=0.0),
            range=ValueRange(min=0.0, max=400.0),
        )
        fragment = _emit_into_data_types(_emit_application_type, declared)
        type_part = fragment[fragment.index("<APPLICATION-PRIMITIVE-DATA-TYPE>") :]
        conditional = [line.strip() for line in type_part.splitlines() if line.strip().endswith("-REF>")]
        # `group SW-DATA-DEF-PROPS-CONTENT` is one xsd:sequence, and it orders
        # the three COMPU-METHOD-REF, DATA-CONSTR-REF, UNIT-REF at 4.2.2 and at
        # 4.3.0 alike.
        assert [entry.split(" ")[0] for entry in conditional] == [
            "<COMPU-METHOD-REF",
            "<DATA-CONSTR-REF",
            "<UNIT-REF",
        ]
        assert fragment.index("<UNIT>") < fragment.index("<COMPU-METHOD>") < fragment.index("<DATA-CONSTR>")
        assert fragment.index("<DATA-CONSTR>") < fragment.index("<APPLICATION-PRIMITIVE-DATA-TYPE>")

    def test_the_presence_rule_is_the_writers_own_and_reads_the_three_fields(self) -> None:
        assert generated_application_forms(ApplicationDataType(name="X", kind="application_type")) == frozenset()
        assert (
            generated_application_forms(ApplicationDataType(name="X", kind="application_type", unit="  "))
            == frozenset()
        )
        assert generated_application_forms(
            ApplicationDataType(name="X", kind="application_type", conversion=Conversion())
        ) == frozenset({"_Compu"})


class TestTheConversionCoefficientsAreInTheOrderTheStandardReads:
    """The hand-computed physical value: the oracle against swapped coefficients.

    ``COMPU-RATIONAL-COEFFS`` spells ``physical = N(internal) / D(internal)``, coefficients listed **lowest
    power first**; one LINEAR scale gives the numerator ``[offset, factor]`` and the denominator ``[1]``.
    For ``factor: 0.1``, ``offset: -40`` and internal ``650``, **by hand**:

        physical = (-40 + 0.1 * 650) / 1 = 25

    * offset/factor swapped (numerator ``[0.1, -40]``) gives ``0.1 + (-40) * 650 = -25999.9``;
    * numerator/denominator swapped (``N = 1``, ``D = -40 + 0.1 * 650``) gives ``1 / 25 = 0.04``.

    Both wrong documents are XSD-valid, so no schema check reaches either.
    """

    #: The declaration, its two coefficients and the arithmetic above as data.
    FACTOR = 0.1
    OFFSET = -40.0
    INTERNAL = 650
    PHYSICAL = 25.0

    def test_the_numerator_lists_offset_then_factor_and_the_denominator_is_one(self) -> None:
        declared = ApplicationDataType(
            name="Temp",
            kind="application_type",
            unit="degC",
            conversion=Conversion(factor=self.FACTOR, offset=self.OFFSET),
        )
        assert _emit_into_data_types(_emit_application_type, declared) == (
            "<UNIT>\n"
            "  <SHORT-NAME>Temp_Unit</SHORT-NAME>\n"
            "  <DISPLAY-NAME>degC</DISPLAY-NAME>\n"
            "</UNIT>\n"
            "<COMPU-METHOD>\n"
            "  <SHORT-NAME>Temp_Compu</SHORT-NAME>\n"
            "  <CATEGORY>LINEAR</CATEGORY>\n"
            '  <UNIT-REF DEST="UNIT">/DataTypes/Application/Temp_Unit</UNIT-REF>\n'
            "  <COMPU-INTERNAL-TO-PHYS>\n"
            "    <COMPU-SCALES>\n"
            "      <COMPU-SCALE>\n"
            "        <COMPU-RATIONAL-COEFFS>\n"
            "          <COMPU-NUMERATOR>\n"
            "            <V>-40</V>\n"
            "            <V>0.1</V>\n"
            "          </COMPU-NUMERATOR>\n"
            "          <COMPU-DENOMINATOR>\n"
            "            <V>1</V>\n"
            "          </COMPU-DENOMINATOR>\n"
            "        </COMPU-RATIONAL-COEFFS>\n"
            "      </COMPU-SCALE>\n"
            "    </COMPU-SCALES>\n"
            "  </COMPU-INTERNAL-TO-PHYS>\n"
            "</COMPU-METHOD>\n"
            "<APPLICATION-PRIMITIVE-DATA-TYPE>\n"
            "  <SHORT-NAME>Temp</SHORT-NAME>\n"
            "  <CATEGORY>VALUE</CATEGORY>\n"
            "  <SW-DATA-DEF-PROPS>\n"
            "    <SW-DATA-DEF-PROPS-VARIANTS>\n"
            "      <SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            '        <COMPU-METHOD-REF DEST="COMPU-METHOD">/DataTypes/Application/Temp_Compu</COMPU-METHOD-REF>\n'
            '        <UNIT-REF DEST="UNIT">/DataTypes/Application/Temp_Unit</UNIT-REF>\n'
            "      </SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            "    </SW-DATA-DEF-PROPS-VARIANTS>\n"
            "  </SW-DATA-DEF-PROPS>\n"
            "</APPLICATION-PRIMITIVE-DATA-TYPE>"
        )

    def test_the_emitted_coefficients_reproduce_the_hand_computed_value(self) -> None:
        """Read the two ``V`` back out of the document and evaluate the polynomial."""
        declared = ApplicationDataType(
            name="Temp",
            kind="application_type",
            unit="degC",
            conversion=Conversion(factor=self.FACTOR, offset=self.OFFSET),
        )
        fragment = _emit_into_data_types(_emit_application_type, declared)
        compu = fragment[fragment.index("<COMPU-METHOD>") :]
        root = etree.fromstring(compu.split("</COMPU-METHOD>")[0] + "</COMPU-METHOD>")
        numerator = [float(v.text) for v in root.iter("V")][:2]
        denominator = [float(v.text) for v in root.iter("V")][2:]
        assert denominator == [1.0]
        assert numerator[0] + numerator[1] * self.INTERNAL == self.PHYSICAL

    def test_a_linear_element_lists_offset_then_factor_and_the_denominator_is_one(self) -> None:
        """The ``linear`` element lists ``[offset, factor] / [1]``, written by the helper ``{name}_Compu`` uses."""
        from swcraft_tools.schema import Linear
        from swcraft_tools.writer.arxml_generation import _emit_linear_element

        declared = Linear(
            name="ProbeTempLinear", kind="linear", unit="ProbeTempUnit", factor=self.FACTOR, offset=self.OFFSET
        )
        assert _emit_into_data_types(_emit_linear_element, declared) == (
            "<COMPU-METHOD>\n"
            "  <SHORT-NAME>ProbeTempLinear</SHORT-NAME>\n"
            "  <CATEGORY>LINEAR</CATEGORY>\n"
            '  <UNIT-REF DEST="UNIT">/DataTypes/Units/ProbeTempUnit</UNIT-REF>\n'
            "  <COMPU-INTERNAL-TO-PHYS>\n"
            "    <COMPU-SCALES>\n"
            "      <COMPU-SCALE>\n"
            "        <COMPU-RATIONAL-COEFFS>\n"
            "          <COMPU-NUMERATOR>\n"
            "            <V>-40</V>\n"
            "            <V>0.1</V>\n"
            "          </COMPU-NUMERATOR>\n"
            "          <COMPU-DENOMINATOR>\n"
            "            <V>1</V>\n"
            "          </COMPU-DENOMINATOR>\n"
            "        </COMPU-RATIONAL-COEFFS>\n"
            "      </COMPU-SCALE>\n"
            "    </COMPU-SCALES>\n"
            "  </COMPU-INTERNAL-TO-PHYS>\n"
            "</COMPU-METHOD>"
        )

    def test_the_linear_elements_coefficients_reproduce_the_hand_computed_value(self) -> None:
        """Read the element's two ``V`` back and evaluate the polynomial: ``25``, the hand-computed value."""
        from swcraft_tools.schema import Linear
        from swcraft_tools.writer.arxml_generation import _emit_linear_element

        declared = Linear(
            name="ProbeTempLinear", kind="linear", unit="ProbeTempUnit", factor=self.FACTOR, offset=self.OFFSET
        )
        root = etree.fromstring(_emit_into_data_types(_emit_linear_element, declared))
        values = [float(v.text) for v in root.iter("V")]
        numerator, denominator = values[:2], values[2:]
        assert denominator == [1.0]
        assert numerator[0] + numerator[1] * self.INTERNAL == self.PHYSICAL


# The mapping set and the behaviour's references


class TestTheMappingSetIsEmittedAsDeclared:
    def test_two_maps_come_out_in_model_order(self, tmp_path: Path) -> None:
        model_root = make_empty_model_root(tmp_path)
        _base_types(model_root)
        write_model_element(model_root, {"name": "Mode", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]})
        write_model_element(model_root, {"name": "Speed", "kind": "application_type"})
        write_model_element(
            model_root,
            {
                "name": "Sets",
                "maps": [
                    {"application": "Speed", "implementation": "uint16"},
                    {"application": "Mode", "implementation": "uint16"},
                ],
            },
            layer="mapping_sets",
        )
        out = tmp_path / "model.arxml"
        generate_arxml(str(model_root), str(out))
        assert golden_slice(out.read_text(encoding="utf-8"), "Sets") == (
            "<DATA-TYPE-MAPPING-SET>\n"
            "  <SHORT-NAME>Sets</SHORT-NAME>\n"
            "  <DATA-TYPE-MAPS>\n"
            "    <DATA-TYPE-MAP>\n"
            '      <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">'
            "/DataTypes/Application/Speed</APPLICATION-DATA-TYPE-REF>\n"
            '      <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">'
            "/DataTypes/Implementation/uint16</IMPLEMENTATION-DATA-TYPE-REF>\n"
            "    </DATA-TYPE-MAP>\n"
            "    <DATA-TYPE-MAP>\n"
            '      <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">'
            "/DataTypes/Application/Mode</APPLICATION-DATA-TYPE-REF>\n"
            '      <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">'
            "/DataTypes/Implementation/uint16</IMPLEMENTATION-DATA-TYPE-REF>\n"
            "    </DATA-TYPE-MAP>\n"
            "  </DATA-TYPE-MAPS>\n"
            "</DATA-TYPE-MAPPING-SET>"
        )

    def test_a_set_with_no_maps_carries_no_container(self) -> None:
        assert _emit_into_data_types(_emit_mapping_set, MappingSet(name="Empty"), {}) == (
            "<DATA-TYPE-MAPPING-SET>\n  <SHORT-NAME>Empty</SHORT-NAME>\n</DATA-TYPE-MAPPING-SET>"
        )

    def test_a_map_naming_an_undeclared_type_is_refused(self, tmp_path: Path) -> None:
        model_root = make_empty_model_root(tmp_path)
        _base_types(model_root)
        write_model_element(
            model_root,
            {"name": "Sets", "maps": [{"application": "Ghost", "implementation": "uint16"}]},
            layer="mapping_sets",
        )
        with pytest.raises(ValueError, match="Ghost"):
            build_workspace(str(model_root))


class TestTheBehaviourReferencesTheSetsTheComponentNames:
    def _model(self, tmp_path: Path, mappings: list[str]) -> Path:
        model_root = make_empty_model_root(tmp_path)
        _base_types(model_root)
        write_model_element(model_root, {"name": "Speed", "kind": "application_type"})
        write_model_element(
            model_root,
            {"name": "SetA", "maps": [{"application": "Speed", "implementation": "uint16"}]},
            layer="mapping_sets",
        )
        write_model_element(model_root, {"name": "SetB", "maps": []}, layer="mapping_sets")
        _dump(
            model_root / "interfaces" / "ISpeed.yaml",
            {
                "name": "ISpeed",
                "kind": "sender_receiver",
                "data_elements": [{"name": "v", "application_type": "Speed"}],
            },
        )
        _dump(
            model_root / "swc" / "app" / "Swc.yaml",
            {
                "name": "Swc",
                "group": "app",
                "ports": [{"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}],
                "data_type_mappings": mappings,
                "handle_termination_and_restart": "no_support",
            },
        )
        _dump(
            model_root / "compositions" / "Root.yaml",
            {"name": "Root", "title": "Root", "root": True, "members": [{"name": "Swc", "type": "Swc"}]},
        )
        return model_root

    def test_the_refs_are_emitted_in_declared_order_before_the_events(self, tmp_path: Path) -> None:
        model_root = self._model(tmp_path, ["SetB", "SetA"])
        out = tmp_path / "model.arxml"
        generate_arxml(str(model_root), str(out))
        behavior = golden_slice(out.read_text(encoding="utf-8"), "Swc_InternalBehavior")
        # The behaviour's SECOND child: `group INTERNAL-BEHAVIOR` precedes `group SWC-INTERNAL-BEHAVIOR`
        # in the schema's one sequence. The `no_support` line follows it; no `PORT-API-OPTIONS` does.
        assert behavior.startswith(
            "<SWC-INTERNAL-BEHAVIOR>\n"
            "  <SHORT-NAME>Swc_InternalBehavior</SHORT-NAME>\n"
            "  <DATA-TYPE-MAPPING-REFS>\n"
            '    <DATA-TYPE-MAPPING-REF DEST="DATA-TYPE-MAPPING-SET">'
            "/DataTypes/MappingSets/SetB</DATA-TYPE-MAPPING-REF>\n"
            '    <DATA-TYPE-MAPPING-REF DEST="DATA-TYPE-MAPPING-SET">'
            "/DataTypes/MappingSets/SetA</DATA-TYPE-MAPPING-REF>\n"
            "  </DATA-TYPE-MAPPING-REFS>\n"
            "  <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>\n"
            "  <SUPPORTS-MULTIPLE-INSTANTIATION>"
        )

    def test_a_component_naming_no_set_carries_no_container(self, tmp_path: Path) -> None:
        # A VALUE type, not an application type: a component whose ports speak an unpaired physical
        # type is refused (`rule:data-type.application-type-unmapped`), which is not this test's subject.
        model_root = self._model(tmp_path, [])
        _dump(
            model_root / "interfaces" / "ISpeed.yaml",
            {
                "name": "ISpeed",
                "kind": "sender_receiver",
                "data_elements": [{"name": "v", "implementation_type": "uint16"}],
            },
        )
        out = tmp_path / "model.arxml"
        generate_arxml(str(model_root), str(out))
        assert "DATA-TYPE-MAPPING-REF" not in out.read_text(encoding="utf-8")

    def test_a_component_naming_an_undeclared_set_is_refused(self, tmp_path: Path) -> None:
        model_root = self._model(tmp_path, ["Ghost"])
        with pytest.raises(ValueError, match="Ghost"):
            build_workspace(str(model_root))


# The application DEST on a data prototype, and an init under one


class TestAnApplicationTypeIsReferencedAsAnApplicationType:
    def _model(self, tmp_path: Path, *, init: Any | None = None) -> Path:
        model_root = make_empty_model_root(tmp_path)
        _base_types(model_root)
        write_model_element(model_root, {"name": "Speed", "kind": "application_type", "unit": "km/h"})
        write_model_element(model_root, {"name": "Buffer", "kind": "array", "element_type": "Speed", "length": 2})
        element: dict[str, Any] = {"name": "v", "application_type": "Speed"}
        if init is not None:
            element["init"] = init
        _dump(
            model_root / "interfaces" / "ISpeed.yaml",
            {"name": "ISpeed", "kind": "sender_receiver", "data_elements": [element]},
        )
        return model_root

    def test_a_data_element_names_it_with_an_application_dest(self, tmp_path: Path) -> None:
        model_root = self._model(tmp_path)
        out = tmp_path / "model.arxml"
        generate_arxml(str(model_root), str(out))
        assert (
            '<TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/DataTypes/Application/Speed</TYPE-TREF>'
            in out.read_text(encoding="utf-8")
        )

    def test_an_array_element_names_it_directly_and_no_view_is_invented(self, tmp_path: Path) -> None:
        model_root = self._model(tmp_path)
        out = tmp_path / "model.arxml"
        generate_arxml(str(model_root), str(out))
        text = out.read_text(encoding="utf-8")
        assert "_AppView" not in text
        assert "DataTypeMappingSet" not in text
        assert "/DataTypes/Application/Speed</TYPE-TREF>" in golden_slice(text, "Buffer")

    def test_an_init_under_an_application_type_is_an_application_value_with_its_types_unit(
        self, tmp_path: Path
    ) -> None:
        """A prototype's own value of an application primitive is an ``APPLICATION-VALUE-SPECIFICATION``.

        Per ``[constr_1221]``, with ``CATEGORY`` ``VALUE``, no ``SW-ARRAYSIZE``, and the ``UNIT-REF`` the type's own
        properties name.
        """
        model_root = self._model(tmp_path, init=12.5)
        out = tmp_path / "model.arxml"
        generate_arxml(str(model_root), str(out))
        text = out.read_text(encoding="utf-8")
        assert golden_slice(text, "ISpeed_v_Init") == (
            "<CONSTANT-SPECIFICATION>\n"
            "  <SHORT-NAME>ISpeed_v_Init</SHORT-NAME>\n"
            "  <VALUE-SPEC>\n"
            "    <APPLICATION-VALUE-SPECIFICATION>\n"
            "      <CATEGORY>VALUE</CATEGORY>\n"
            "      <SW-VALUE-CONT>\n"
            '        <UNIT-REF DEST="UNIT">/DataTypes/Application/Speed_Unit</UNIT-REF>\n'
            "        <SW-VALUES-PHYS>\n"
            "          <V>12.5</V>\n"
            "        </SW-VALUES-PHYS>\n"
            "      </SW-VALUE-CONT>\n"
            "    </APPLICATION-VALUE-SPECIFICATION>\n"
            "  </VALUE-SPEC>\n"
            "</CONSTANT-SPECIFICATION>"
        )
        # The same unit the type's own SW-DATA-DEF-PROPS names: one element referenced twice.
        assert '<UNIT-REF DEST="UNIT">/DataTypes/Application/Speed_Unit</UNIT-REF>' in golden_slice(text, "Speed")


# The refusal, and the retirement


class TestACompositeMemberOverAnImplementationTypeIsRefusedAsUndeclaredAndNothingIsWritten:
    """A composite member naming an implementation type names no declared application type.

    A record field's ``type`` and an array's ``element_type`` resolve in the ``application_types`` layer alone.
    """

    def _model(self, tmp_path: Path) -> Path:
        model_root = make_empty_model_root(tmp_path)
        _base_types(model_root)
        write_model_element(model_root, {"name": "Pose", "kind": "record", "fields": [{"name": "x", "type": "uint16"}]})
        return model_root

    def test_the_refusal_names_the_member_and_the_undeclared_name(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError) as excinfo:
            build_workspace(str(self._model(tmp_path)))
        message = str(excinfo.value)
        assert "Pose" in message and "uint16" in message
        assert "is not declared" in message
        assert "composite-member-implementation-type" not in message

    def test_nothing_is_written(self, tmp_path: Path) -> None:
        out = tmp_path / "out" / "model.arxml"
        with pytest.raises(ValueError):
            generate_arxml(str(self._model(tmp_path)), str(out))
        assert not out.exists()

    def test_an_array_element_over_a_reference_type_is_refused_too(self, tmp_path: Path) -> None:
        model_root = make_empty_model_root(tmp_path)
        _base_types(model_root)
        write_model_element(model_root, {"name": "uint16_ref", "kind": "reference_type", "base": "uint16"})
        write_model_element(model_root, {"name": "Buf", "kind": "array", "element_type": "uint16_ref", "length": 2})
        with pytest.raises(ValueError, match="uint16_ref"):
            build_workspace(str(model_root))


class TestTheTwoInventedNamesAreRetired:
    def test_the_writer_declares_neither_constant(self) -> None:
        import swcraft_tools.writer.arxml_generation as generation

        assert not hasattr(generation, "APP_TYPE_SUFFIX")
        assert not hasattr(generation, "DATA_TYPE_MAPPING_SET_NAME")

    def test_a_type_named_like_the_retired_view_is_accepted(self, tmp_path: Path) -> None:
        model_root = make_empty_model_root(tmp_path)
        _base_types(model_root)
        write_model_element(model_root, {"name": "uint16_AppView", "kind": "value_type", "base": "uint16_base"})
        write_model_element(model_root, {"name": "DataTypeMappingSet", "kind": "value_type", "base": "uint16_base"})
        build_workspace(str(model_root))


class TestTheWritersOwnInitRefusalsForAnApplicationType:
    """`_init_value_refusals`' `ApplicationDataType` arm, branch by branch.

    The branches the writer's side alone owns: list/map, `str`, the `bool` acceptance and the unwritable
    integer. A unit test, as the end-to-end route needs a mapping set and a component to reach them.
    """

    TYPE_NAME = "Spd_T"

    def _refusals(self, value: object) -> list[str]:
        from swcraft_tools.writer.arxml_generation import _init_value_refusals

        declared = ApplicationDataType(name=self.TYPE_NAME, kind="application_type", unit="km/h")
        return _init_value_refusals(
            value,
            self.TYPE_NAME,
            DataDescription.last_declared([declared]),
            "init",
            frozenset(),
            application=True,
        )

    @pytest.mark.parametrize("value", [[1, 2], {"a": 1}], ids=["a list", "a map"])
    def test_a_composite_value_under_a_scalar_type_is_refused(self, value: object) -> None:
        refusals = self._refusals(value)

        assert len(refusals) == 1
        assert "takes a scalar" in refusals[0]
        assert self.TYPE_NAME in refusals[0]

    def test_a_string_is_refused_under_the_writability_rule(self) -> None:
        refusals = self._refusals("abc")

        assert len(refusals) == 1
        assert "interface.init-string-scalar" in refusals[0]

    @pytest.mark.parametrize("value", [True, False], ids=["true", "false"])
    def test_a_bool_is_accepted(self, value: bool) -> None:
        """A deliberate asymmetry: a `bool` has a spelling (`1`/`0`), and its `range` is `check_init_conformance`'s."""
        assert self._refusals(value) == []

    def test_an_integer_this_interpreter_cannot_write_is_refused(self) -> None:
        limit = sys.get_int_max_str_digits()
        if not limit:
            pytest.skip("this interpreter has no integer-to-string limit, so nothing is unwritable")

        # Built by arithmetic, not by `int(str)`: converting the literal is
        # itself over the limit and raises before the walk is reached.
        refusals = self._refusals(10 ** (limit + 1))

        assert len(refusals) == 1
        assert "interface.init-unwritable-integer" in refusals[0]

    @pytest.mark.parametrize("value", [0, -1, 2.5], ids=["zero", "negative", "float"])
    def test_an_ordinary_number_is_accepted(self, value: object) -> None:
        assert self._refusals(value) == []


# An unpaired physical type and the data description packages: the writer's
# refusals, the restatement they rest on and the one package answer, pinned
# against the validator


_INTERFACES: list[dict[str, Any]] = [
    {
        "name": "ISr",
        "kind": "sender_receiver",
        "data_elements": [
            {"name": "a", "application_type": "Speed"},
            {"name": "b", "implementation_type": "uint16"},
            {"name": "c", "application_type": "Speed"},
        ],
    },
    {
        "name": "ICs",
        "kind": "client_server",
        "operations": [
            {"name": "NoArgs"},
            {
                "name": "Op",
                "arguments": [
                    {"name": "x", "direction": "in", "application_type": "Speed"},
                    {"name": "y", "direction": "out", "application_type": "Mode"},
                    {"name": "z", "direction": "inout", "application_type": "Speed"},
                ],
            },
            {"name": "Op2", "arguments": [{"name": "w", "direction": "in", "implementation_type": "uint16"}]},
        ],
    },
]


def _interface(declared: dict[str, Any]) -> Any:
    from pydantic import TypeAdapter
    from swcraft_tools.schema import Interface

    return TypeAdapter(Interface).validate_python(declared)


def _data_type(declared: dict[str, Any]) -> Any:
    from pydantic import TypeAdapter
    from swcraft_tools.schema import DataType

    return TypeAdapter(DataType).validate_python(declared)


class TestTheWriterAndTheValidatorReadOnePortTypeList:
    """``_interface_member_application_types`` and ``validator._port_member_application_types`` are one list.

    The writer cannot import the validator at module level, so each keeps a copy; the two unpaired-type
    refusals agree only through this sequence equality, repeats and order included.
    """

    @pytest.mark.parametrize("declared", _INTERFACES, ids=[i["name"] for i in _INTERFACES])
    def test_both_sides_yield_the_same_sequence(self, declared: dict[str, Any]) -> None:
        from swcraft_tools.validator.data_types import _port_member_application_types as reported
        from swcraft_tools.writer.arxml_generation import _interface_member_application_types as refused

        interface = _interface(declared)

        assert list(refused(interface)) == list(reported(interface))

    def test_the_matrix_is_not_vacuous(self) -> None:
        from swcraft_tools.writer.arxml_generation import _interface_member_application_types

        yielded = [list(_interface_member_application_types(_interface(declared))) for declared in _INTERFACES]

        # A prototype states the side: only an `application_type` is yielded,
        # so the `uint16` member of each interface (a `type`) is not.
        assert yielded == [["Speed", "Speed"], ["Speed", "Mode", "Speed"]]


_DATA_TYPES: list[dict[str, Any]] = [
    {"name": "b", "kind": "base_type", "size": 16, "encoding": "NONE", "native_declaration": "uint16"},
    {"name": "v", "kind": "value_type", "base": "b"},
    {"name": "r", "kind": "reference_type", "base": "v"},
    {"name": "e", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]},
    {"name": "rec", "kind": "record", "fields": [{"name": "f", "type": "a0"}]},
    {"name": "arr", "kind": "array", "element_type": "a0", "length": 2},
    {"name": "a0", "kind": "application_type"},
    {"name": "a1", "kind": "application_type", "unit": "km/h"},
    {"name": "a2", "kind": "application_type", "unit": "   "},
    {"name": "a3", "kind": "application_type", "conversion": {}},
    {"name": "a4", "kind": "application_type", "range": {"min": 0.0, "max": 1.0}},
    {"name": "a5", "kind": "application_type", "unit": "m", "conversion": {"factor": 2.0}, "range": {}},
    # A declared name that another type also generates: claimed once, and the
    # declared claim wins the clause on both sides.
    {"name": "e_CompuMethod", "kind": "value_type", "base": "b"},
]


def _placements(text: str) -> set[tuple[str, str, str]]:
    """Every packaged element of *text* as ``(package path, tag, SHORT-NAME)``, the path without a leading slash."""
    placements: set[tuple[str, str, str]] = set()
    for element in etree.fromstring(text.encode("utf-8")).iter():
        if not isinstance(element.tag, str) or etree.QName(element).localname == "AR-PACKAGE":
            continue
        short_name = next(
            (
                child.text
                for child in element
                if isinstance(child.tag, str) and etree.QName(child).localname == "SHORT-NAME"
            ),
            None,
        )
        parent = element.getparent()
        if short_name is None or parent is None or etree.QName(parent).localname != "ELEMENTS":
            continue
        packages = [
            next(child.text for child in ancestor if etree.QName(child).localname == "SHORT-NAME")
            for ancestor in element.iterancestors()
            if etree.QName(ancestor).localname == "AR-PACKAGE"
        ]
        placements.add(("/".join(reversed(packages)), etree.QName(element).localname, short_name))
    return placements


class TestEachTypeAndEveryFormItMintsLieInTheOneAnsweredPackage:
    """``validator.data_description_package_path`` against the writer's emitted packages.

    Every declared type and every form it mints lies where that one answer says, including ``e_CompuMethod``
    beside the enum ``e``'s generated ``COMPU-METHOD`` of the same name, which are no siblings.
    """

    #: The matrix, with ``a3`` given the unit a written conversion must refer to
    #: (``rule:data-type.conversion-without-unit``), so the model generates.
    _GENERATED = [{**d, "unit": "s"} if d["name"] == "a3" else d for d in _DATA_TYPES]

    def _generated(self, tmp_path: Path) -> set[tuple[str, str, str]]:
        model_root = make_empty_model_root(tmp_path)
        for declared in self._GENERATED:
            _dump(model_element_path(model_root, declared["name"], kind=declared["kind"]), declared)
        write_model_element(model_root, {"name": "Sets", "maps": []}, layer="mapping_sets")
        out = tmp_path / "model.arxml"
        generate_arxml(str(model_root), str(out))
        return _placements(out.read_text(encoding="utf-8"))

    def test_every_declared_type_lies_in_its_layers_package(self, tmp_path: Path) -> None:
        from swcraft_tools.validator import data_description_package_path

        placements = self._generated(tmp_path)

        for declared in self._GENERATED:
            expected = data_description_package_path(_data_type(declared))
            # A form's tags are left out: `e`'s COMPU-METHOD shares `e_CompuMethod`'s name.
            homes = {
                package
                for package, tag, name in placements
                if name == declared["name"] and tag not in {"UNIT", "COMPU-METHOD", "DATA-CONSTR"}
            }
            assert homes == {expected}, declared["name"]

    def test_every_generated_form_lies_in_its_owners_package(self, tmp_path: Path) -> None:
        from swcraft_tools.validator import data_description_package_path

        placements = self._generated(tmp_path)
        application = data_description_package_path(_data_type(_DATA_TYPES[3]))
        forms = {
            ("e_CompuMethod", "COMPU-METHOD"),
            ("a1_Unit", "UNIT"),
            ("a3_Unit", "UNIT"),
            ("a3_Compu", "COMPU-METHOD"),
            ("a4_Range", "DATA-CONSTR"),
            ("a5_Unit", "UNIT"),
            ("a5_Compu", "COMPU-METHOD"),
            ("a5_Range", "DATA-CONSTR"),
        }

        for name, tag in forms:
            assert {
                package for package, emitted_tag, emitted in placements if (emitted, emitted_tag) == (name, tag)
            } == {application}, name
        # Not vacuous: no form the matrix does not declare a field for is minted anywhere.
        assert not {name for _p, _t, name in placements} & {"a0_Unit", "a2_Unit", "rec_CompuMethod"}

    def test_the_value_type_named_like_the_enums_form_lies_in_another_package(self, tmp_path: Path) -> None:
        placements = self._generated(tmp_path)

        assert {(package, tag) for package, tag, name in placements if name == "e_CompuMethod"} == {
            ("DataTypes/Application", "COMPU-METHOD"),
            ("DataTypes/Implementation", "IMPLEMENTATION-DATA-TYPE"),
        }

    def test_the_mapping_set_lies_in_its_own_package(self, tmp_path: Path) -> None:
        placements = self._generated(tmp_path)

        assert ("DataTypes/MappingSets", "DATA-TYPE-MAPPING-SET", "Sets") in placements


class TestTheWriterRefusesAPhysicalTypeNoNamedSetPairs:
    """``_check_application_type_mappings``: an unpaired physical type at the write boundary.

    Refused before anything is emitted, every offending ``(component, type)`` pair in one message, the
    pairs ``check-model`` reports.
    """

    def _model(self, tmp_path: Path, maps: list[dict[str, str]]) -> Path:
        model_root = make_empty_model_root(tmp_path)
        _base_types(model_root)
        write_model_element(model_root, {"name": "Speed", "kind": "application_type"})
        write_model_element(model_root, {"name": "Mode", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]})
        write_model_element(model_root, {"name": "Set", "maps": maps}, layer="mapping_sets")
        _dump(
            model_root / "interfaces" / "ISpeed.yaml",
            {
                "name": "ISpeed",
                "kind": "sender_receiver",
                "data_elements": [{"name": "v", "application_type": "Speed"}],
            },
        )
        _dump(
            model_root / "interfaces" / "IMode.yaml",
            {
                "name": "IMode",
                "kind": "client_server",
                "operations": [
                    {"name": "Get", "arguments": [{"name": "m", "direction": "out", "application_type": "Mode"}]}
                ],
            },
        )
        _dump(
            model_root / "swc" / "app" / "Swc.yaml",
            {
                "name": "Swc",
                "group": "app",
                "ports": [
                    {"name": "pSpeed", "direction": "provide", "interface": "ISpeed"},
                    {"name": "rMode", "direction": "require", "interface": "IMode"},
                ],
                "data_type_mappings": ["Set"],
                "handle_termination_and_restart": "no_support",
            },
        )
        _dump(
            model_root / "compositions" / "Root.yaml",
            {"name": "Root", "title": "Root", "root": True, "members": [{"name": "Swc", "type": "Swc"}]},
        )
        return model_root

    def test_every_unpaired_type_is_named_in_one_message_and_nothing_is_written(self, tmp_path: Path) -> None:
        out = tmp_path / "out" / "model.arxml"

        with pytest.raises(ValueError) as excinfo:
            generate_arxml(str(self._model(tmp_path, [])), str(out))

        assert str(excinfo.value) == (
            "Component(s) speak a physical type no mapping set they name pairs with an implementation type: "
            "'Swc' speaks 'Speed', 'Swc' speaks 'Mode' ('swcraft check-model' reports the same model)."
        )
        assert not out.exists()

    def test_check_model_reports_the_same_pairs(self, tmp_path: Path) -> None:
        from swcraft_tools.validator import check_application_type_mapping

        result = check_application_type_mapping(str(self._model(tmp_path, [])))

        assert {record.rule for record in result.records} == {"data-type.application-type-unmapped"}
        assert len(result.records) == 2
        assert all("'Swc'" in record.message for record in result.records)
        assert sum("'Speed'" in record.message for record in result.records) == 1
        assert sum("'Mode'" in record.message for record in result.records) == 1

    def test_a_pair_for_one_type_leaves_the_other_named(self, tmp_path: Path) -> None:
        model_root = self._model(tmp_path, [{"application": "Speed", "implementation": "uint16"}])

        with pytest.raises(ValueError, match=r"implementation type: 'Swc' speaks 'Mode' \('swcraft"):
            build_workspace(str(model_root))

    def test_both_types_paired_is_generated(self, tmp_path: Path) -> None:
        model_root = self._model(
            tmp_path,
            [
                {"application": "Speed", "implementation": "uint16"},
                {"application": "Mode", "implementation": "uint16"},
            ],
        )
        out = tmp_path / "model.arxml"

        generate_arxml(str(model_root), str(out))

        assert out.exists()


class TestASetMayTakeANameADataTypeTakes:
    """A mapping set named like a type or a generated form is generated.

    A set lies in ``/DataTypes/MappingSets`` and a type — with every form it mints — in its own
    layer's package, so the two are no siblings; this class pins the acceptance on both sides.
    """

    def _model(self, tmp_path: Path, set_name: str) -> Path:
        model_root = make_empty_model_root(tmp_path)
        _base_types(model_root)
        write_model_element(model_root, {"name": "Mode", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]})
        write_model_element(model_root, {"name": "Speed", "kind": "application_type", "unit": "m"})
        write_model_element(model_root, {"name": set_name, "maps": []}, layer="mapping_sets")
        return model_root

    @pytest.mark.parametrize(
        ("set_name", "occupant"),
        [
            ("uint16", ("DataTypes/Implementation", "IMPLEMENTATION-DATA-TYPE")),
            ("Mode_CompuMethod", ("DataTypes/Application", "COMPU-METHOD")),
            ("Speed_Unit", ("DataTypes/Application", "UNIT")),
            ("Speed_Compu", None),
        ],
    )
    def test_the_set_is_emitted_beside_the_type_in_its_own_package(
        self, tmp_path: Path, set_name: str, occupant: tuple[str, str] | None
    ) -> None:
        out = tmp_path / "out" / "model.arxml"

        generate_arxml(str(self._model(tmp_path, set_name)), str(out))

        placements = {
            (package, tag) for package, tag, name in _placements(out.read_text(encoding="utf-8")) if name == set_name
        }
        expected = {("DataTypes/MappingSets", "DATA-TYPE-MAPPING-SET")}
        if occupant is not None:
            expected.add(occupant)
        assert placements == expected

    @pytest.mark.parametrize("set_name", ["uint16", "Mode_CompuMethod", "Speed_Unit"])
    def test_check_model_reports_nothing_for_it(self, tmp_path: Path, set_name: str) -> None:
        from swcraft_tools.validator import check_cross_layer_consistency

        result = check_cross_layer_consistency(str(self._model(tmp_path, set_name)))

        # The model has no composition, so only the data description rules are read.
        assert {
            record.rule for record in result.records if record.rule.startswith(("data-type.", "mapping-set."))
        } == set()


#: The shapes the writer's double-mapping refusal and
#: ``rule:data-type.application-type-double-mapped`` are compared over: the sets
#: each declares, the sets ``Swc`` names, and the ``(component, type)`` pairs
#: both must name. Two shapes refuse nothing, so the equality is not vacuous in
#: either direction.
_DOUBLE_MAPPING_SHAPES: dict[str, tuple[dict[str, list[tuple[str, str]]], list[str], set[tuple[str, str]]]] = {
    "two-sets-agree": ({"A": [("Speed", "uint16")], "B": [("Speed", "uint16")]}, ["A", "B"], set()),
    "two-sets-disagree": ({"A": [("Speed", "uint16")], "B": [("Speed", "Raw")]}, ["A", "B"], {("Swc", "Speed")}),
    "the-disagreeing-set-not-named": ({"A": [("Speed", "uint16")], "B": [("Speed", "Raw")]}, ["A"], set()),
    "three-ways": (
        {"A": [("Speed", "uint16")], "B": [("Speed", "Raw")], "C": [("Speed", "Raw2")]},
        ["A", "B", "C"],
        {("Swc", "Speed")},
    ),
    # No port speaks `Mode`: the constraint is about the behaviour's mapping,
    # and the writer emits every map of every set it names.
    "a-type-no-port-speaks": (
        {"A": [("Speed", "uint16"), ("Mode", "uint16")], "B": [("Mode", "Raw")]},
        ["A", "B"],
        {("Swc", "Mode")},
    ),
    # An undeclared set pairs nothing; the two declared ones still disagree.
    "beside-an-undeclared-set": (
        {"A": [("Speed", "uint16")], "B": [("Speed", "Raw")]},
        ["A", "Nope", "B"],
        {("Swc", "Speed")},
    ),
}


class TestTheWriterRefusesADoubleMapping:
    """``_check_application_type_double_mappings``: ``[constr_1004]``'s *exactly one* half.

    The writer calls the validator's ``application_type_double_mappings``; the two refuse the same pairs
    over :data:`_DOUBLE_MAPPING_SHAPES`, and the refusal leaves nothing on disk.
    """

    def _model(self, tmp_path: Path, sets: dict[str, list[tuple[str, str]]], named: list[str]) -> Path:
        model_root = make_empty_model_root(tmp_path)
        _base_types(model_root)
        for extra in ("Raw", "Raw2"):
            write_model_element(model_root, {"name": extra, "kind": "value_type", "base": "uint16_base"})
        write_model_element(model_root, {"name": "Speed", "kind": "application_type"})
        write_model_element(model_root, {"name": "Mode", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]})
        for name, maps in sets.items():
            write_model_element(
                model_root,
                {"name": name, "maps": [{"application": a, "implementation": i} for a, i in maps]},
                layer="mapping_sets",
            )
        _dump(
            model_root / "interfaces" / "ISpeed.yaml",
            {
                "name": "ISpeed",
                "kind": "sender_receiver",
                "data_elements": [{"name": "v", "application_type": "Speed"}],
            },
        )
        _dump(
            model_root / "swc" / "app" / "Swc.yaml",
            {
                "name": "Swc",
                "group": "app",
                "ports": [{"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}],
                "data_type_mappings": named,
                "handle_termination_and_restart": "no_support",
            },
        )
        _dump(
            model_root / "compositions" / "Root.yaml",
            {"name": "Root", "title": "Root", "root": True, "members": [{"name": "Swc", "type": "Swc"}]},
        )
        return model_root

    @pytest.mark.parametrize("shape", sorted(_DOUBLE_MAPPING_SHAPES))
    def test_the_writer_and_check_model_refuse_the_same_pairs(self, tmp_path: Path, shape: str) -> None:
        from swcraft_tools.reader import read_mapping_sets, read_swc_model
        from swcraft_tools.validator import check_application_type_double_mapping
        from swcraft_tools.writer.arxml_generation import _check_application_type_double_mappings

        sets, named, expected = _DOUBLE_MAPPING_SHAPES[shape]
        model_root = self._model(tmp_path, sets, named)

        reported = set()
        for record in check_application_type_double_mapping(str(model_root)).records:
            match = re.search(r"maps type '(\w+)'", record.message)
            assert match is not None, record.message
            reported.add((record.subjects[0].element, match.group(1)))
        try:
            _check_application_type_double_mappings(read_swc_model(str(model_root)), read_mapping_sets(str(model_root)))
            refused: set[tuple[str, str]] = set()
        except ValueError as error:
            refused = set(re.findall(r"'(\w+)' maps '(\w+)' onto", str(error)))

        assert reported == refused == expected

    def test_the_refusal_names_every_implementation_type_and_nothing_is_written(self, tmp_path: Path) -> None:
        sets, named, _expected = _DOUBLE_MAPPING_SHAPES["three-ways"]
        out = tmp_path / "out" / "model.arxml"

        with pytest.raises(ValueError) as excinfo:
            generate_arxml(str(self._model(tmp_path, sets, named)), str(out))

        assert str(excinfo.value) == (
            "Component(s) whose mapping sets pair one application type with more than one implementation type, "
            "where [constr_1004] allows exactly one: 'Swc' maps 'Speed' onto 'uint16' and 'Raw' and 'Raw2' "
            "('swcraft check-model' reports the same model)."
        )
        assert not out.exists()

    def test_two_sets_that_agree_are_generated_with_both_references(self, tmp_path: Path) -> None:
        sets, named, _expected = _DOUBLE_MAPPING_SHAPES["two-sets-agree"]
        out = tmp_path / "model.arxml"

        generate_arxml(str(self._model(tmp_path, sets, named)), str(out))

        text = out.read_text(encoding="utf-8")
        assert text.count("<DATA-TYPE-MAPPING-REF ") == 2
        assert text.count("<DATA-TYPE-MAP>") == 2
