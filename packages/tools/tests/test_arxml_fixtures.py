"""Tests for the hand-crafted, anonymized ARXML fixtures under fixtures/arxml/.

The fixtures mirror structural shapes AUTOSAR tool exports use, with invented names only. This module
pins that each is well-formed, declares its expected schema version and holds the element kinds its
filename promises, so an accidental edit does not silently break the tests written against them. The
version is a per-fixture mapping, not ``AUTOSAR_4-2-2.xsd`` for every file: 4.2.2 fixtures and their
4.3.0 twins share the directory.

The expected filenames are literal rather than read from ``swcraft_tools.arxml_version``: this module is
an independent oracle, so a wrong entry in the writer's version table cannot move its expectations.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

AR_NS = "http://autosar.org/schema/r4.0"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
NS = {"ar": AR_NS, "xsi": XSI_NS}

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "arxml"

#: Fixture filename -> the XSD filename its ``xsi:schemaLocation`` must name.
#: Exhaustive over the directory by :func:`test_every_fixture_file_has_an_expected_schema`.
FIXTURE_SCHEMA_FILENAMES = {
    "minimal_swc.arxml": "AUTOSAR_4-2-2.xsd",
    "data_types.arxml": "AUTOSAR_4-2-2.xsd",
    "client_server.arxml": "AUTOSAR_4-2-2.xsd",
    "composition_nested.arxml": "AUTOSAR_4-2-2.xsd",
    "unsupported_content.arxml": "AUTOSAR_4-2-2.xsd",
    "init_values.arxml": "AUTOSAR_4-2-2.xsd",
    "minimal_swc_4_3_0.arxml": "AUTOSAR_4-3-0.xsd",
    "data_types_4_3_0.arxml": "AUTOSAR_4-3-0.xsd",
}

#: Every XSD filename any fixture may declare. A fixture must name its own and none of the others, so a
#: half-applied relabel (both tokens present) fails.
ALL_SCHEMA_FILENAMES = sorted(set(FIXTURE_SCHEMA_FILENAMES.values()))

FIXTURE_NAMES = list(FIXTURE_SCHEMA_FILENAMES)

#: 4.3.0 twin -> the 4.2.2 fixture whose element content it reproduces exactly (:class:`TestVersionTwins`).
VERSION_TWINS = {
    "minimal_swc_4_3_0.arxml": "minimal_swc.arxml",
    "data_types_4_3_0.arxml": "data_types.arxml",
}


def _ar(tag: str) -> str:
    """Build a namespace-qualified AUTOSAR tag for ElementTree find/findall."""
    return f"{{{AR_NS}}}{tag}"


def _parse(name: str) -> ET.ElementTree:
    return ET.parse(FIXTURES_DIR / name)


def _short_names(root: ET.Element, tag: str) -> list[str]:
    return [el.findtext(_ar("SHORT-NAME")) for el in root.iter(_ar(tag))]


# Well-formedness and schema declaration — every fixture


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_exists(name):
    path = FIXTURES_DIR / name
    assert path.is_file(), f"Expected fixture {path} to exist"


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_is_well_formed_xml(name):
    tree = _parse(name)
    root = tree.getroot()
    assert root.tag == _ar("AUTOSAR")


def test_every_fixture_file_has_an_expected_schema():
    """FIXTURE_SCHEMA_FILENAMES covers the directory exactly, so no fixture escapes the parametrized checks."""
    on_disk = {path.name for path in FIXTURES_DIR.glob("*.arxml")}
    assert on_disk == set(FIXTURE_SCHEMA_FILENAMES), (
        "fixtures/arxml/ and FIXTURE_SCHEMA_FILENAMES disagree; "
        f"unlisted on disk: {sorted(on_disk - set(FIXTURE_SCHEMA_FILENAMES))}, "
        f"listed but missing: {sorted(set(FIXTURE_SCHEMA_FILENAMES) - on_disk)}"
    )


@pytest.mark.parametrize(("name", "schema_filename"), sorted(FIXTURE_SCHEMA_FILENAMES.items()))
def test_fixture_declares_its_expected_schema(name, schema_filename):
    """Each fixture declares the r4.0 namespace and exactly its own expected XSD filename.

    The other versions' filenames must be absent: a half-applied relabel would satisfy a bare ``in`` check.
    """
    root = _parse(name).getroot()
    schema_location = root.attrib.get(f"{{{XSI_NS}}}schemaLocation", "")
    assert AR_NS in schema_location
    assert schema_filename in schema_location
    for other in ALL_SCHEMA_FILENAMES:
        if other != schema_filename:
            assert other not in schema_location, f"{name}: unexpected {other} in {schema_location!r}"


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_has_nested_ar_packages(name):
    root = _parse(name).getroot()
    # Every fixture nests AR-PACKAGE inside AR-PACKAGE (Acme/Chassis/...), mirroring a
    # tool export's deep package hierarchy.
    outer_packages = root.find(_ar("AR-PACKAGES"))
    assert outer_packages is not None
    top_package = outer_packages.find(_ar("AR-PACKAGE"))
    assert top_package is not None
    assert top_package.findtext(_ar("SHORT-NAME")) == "Acme"
    nested = top_package.find(_ar("AR-PACKAGES"))
    assert nested is not None, f"{name}: expected nested AR-PACKAGES under Acme"


# minimal_swc.arxml


class TestMinimalSwc:
    NAME = "minimal_swc.arxml"

    def test_one_swc(self):
        root = _parse(self.NAME).getroot()
        swcs = _short_names(root, "APPLICATION-SW-COMPONENT-TYPE")
        assert swcs == ["WheelSpeedSensorSwc"]

    def test_two_ports_one_provide_one_require(self):
        root = _parse(self.NAME).getroot()
        p_ports = root.findall(f".//{_ar('P-PORT-PROTOTYPE')}")
        r_ports = root.findall(f".//{_ar('R-PORT-PROTOTYPE')}")
        assert len(p_ports) == 1
        assert len(r_ports) == 1

    def test_one_sr_interface(self):
        root = _parse(self.NAME).getroot()
        interfaces = _short_names(root, "SENDER-RECEIVER-INTERFACE")
        assert interfaces == ["IWheelSpeed"]

    def test_impl_types_present(self):
        root = _parse(self.NAME).getroot()
        assert root.find(f".//{_ar('SW-BASE-TYPE')}") is not None
        assert root.find(f".//{_ar('IMPLEMENTATION-DATA-TYPE')}") is not None

    def test_runnable_and_timing_event(self):
        root = _parse(self.NAME).getroot()
        assert root.find(f".//{_ar('RUNNABLE-ENTITY')}") is not None
        assert root.find(f".//{_ar('TIMING-EVENT')}") is not None
        period = root.findtext(f".//{_ar('TIMING-EVENT')}/{_ar('PERIOD')}")
        assert period == "0.01"

    def test_desc_texts_present(self):
        root = _parse(self.NAME).getroot()
        descs = root.findall(f".//{_ar('DESC')}")
        assert len(descs) >= 4


# data_types.arxml


class TestDataTypes:
    NAME = "data_types.arxml"

    def test_base_types_present(self):
        root = _parse(self.NAME).getroot()
        assert len(root.findall(f".//{_ar('SW-BASE-TYPE')}")) >= 2

    def test_impl_types_value_and_type_reference_categories(self):
        root = _parse(self.NAME).getroot()
        categories = {el.findtext(_ar("CATEGORY")) for el in root.iter(_ar("IMPLEMENTATION-DATA-TYPE"))}
        assert "VALUE" in categories
        assert "TYPE_REFERENCE" in categories

    def test_application_primitive_record_array_present(self):
        root = _parse(self.NAME).getroot()
        assert root.find(f".//{_ar('APPLICATION-PRIMITIVE-DATA-TYPE')}") is not None
        assert root.find(f".//{_ar('APPLICATION-RECORD-DATA-TYPE')}") is not None
        assert root.find(f".//{_ar('APPLICATION-ARRAY-DATA-TYPE')}") is not None

    def test_texttable_and_non_texttable_compu_methods(self):
        root = _parse(self.NAME).getroot()
        categories = [el.findtext(_ar("CATEGORY")) for el in root.iter(_ar("COMPU-METHOD"))]
        assert categories.count("TEXTTABLE") == 2
        assert "LINEAR" in categories

    def test_data_type_mapping_set_present(self):
        root = _parse(self.NAME).getroot()
        mapping_sets = root.findall(f".//{_ar('DATA-TYPE-MAPPING-SET')}")
        assert len(mapping_sets) == 1
        maps = mapping_sets[0].findall(f".//{_ar('DATA-TYPE-MAP')}")
        assert len(maps) == 1
        app_ref = maps[0].findtext(_ar("APPLICATION-DATA-TYPE-REF"))
        impl_ref = maps[0].findtext(_ar("IMPLEMENTATION-DATA-TYPE-REF"))
        assert app_ref == "/Acme/Chassis/ApplicationTypes/BrakeModeEnum"
        assert impl_ref == "/Acme/Chassis/Types/BrakeModeEnum"

    def test_app_impl_twin_collision_shape(self):
        """BrakeModeEnum: one impl + one app type, same bare name, linked via mapping set."""
        root = _parse(self.NAME).getroot()
        impl_names = _short_names(root, "IMPLEMENTATION-DATA-TYPE")
        app_names = _short_names(root, "APPLICATION-PRIMITIVE-DATA-TYPE")
        assert impl_names.count("BrakeModeEnum") == 1
        assert app_names.count("BrakeModeEnum") == 1
        mapping_sets = root.findall(f".//{_ar('DATA-TYPE-MAPPING-SET')}")
        linked_pairs = {
            (
                m.findtext(_ar("APPLICATION-DATA-TYPE-REF")),
                m.findtext(_ar("IMPLEMENTATION-DATA-TYPE-REF")),
            )
            for ms in mapping_sets
            for m in ms.findall(f".//{_ar('DATA-TYPE-MAP')}")
        }
        assert (
            "/Acme/Chassis/ApplicationTypes/BrakeModeEnum",
            "/Acme/Chassis/Types/BrakeModeEnum",
        ) in linked_pairs

    def test_unrelated_bare_name_collision_shape(self):
        """DoorStateEnum: one impl + one app type, same bare name, NOT linked via mapping set."""
        root = _parse(self.NAME).getroot()
        impl_names = _short_names(root, "IMPLEMENTATION-DATA-TYPE")
        app_names = _short_names(root, "APPLICATION-PRIMITIVE-DATA-TYPE")
        assert impl_names.count("DoorStateEnum") == 1
        assert app_names.count("DoorStateEnum") == 1
        mapping_sets = root.findall(f".//{_ar('DATA-TYPE-MAPPING-SET')}")
        linked_app_refs = {
            m.findtext(_ar("APPLICATION-DATA-TYPE-REF"))
            for ms in mapping_sets
            for m in ms.findall(f".//{_ar('DATA-TYPE-MAP')}")
        }
        assert "/Acme/Chassis/ApplicationTypes/DoorStateEnum" not in linked_app_refs


# client_server.arxml


class TestClientServer:
    NAME = "client_server.arxml"

    def test_one_cs_interface_with_two_operations(self):
        root = _parse(self.NAME).getroot()
        interfaces = root.findall(f".//{_ar('CLIENT-SERVER-INTERFACE')}")
        assert len(interfaces) == 1
        assert interfaces[0].findtext(_ar("SHORT-NAME")) == "IBrakeControlService"
        operations = interfaces[0].findall(f".//{_ar('CLIENT-SERVER-OPERATION')}")
        assert len(operations) == 2

    def test_arguments_cover_all_three_directions(self):
        root = _parse(self.NAME).getroot()
        directions = {el.findtext(_ar("DIRECTION")) for el in root.iter(_ar("ARGUMENT-DATA-PROTOTYPE"))}
        assert directions == {"IN", "OUT", "INOUT"}

    def test_operation_invoked_event_present(self):
        root = _parse(self.NAME).getroot()
        events = root.findall(f".//{_ar('OPERATION-INVOKED-EVENT')}")
        assert len(events) == 1
        op_iref = events[0].find(_ar("OPERATION-IREF"))
        assert op_iref is not None
        assert (
            op_iref.findtext(_ar("TARGET-REQUIRED-OPERATION-REF"))
            == "/Acme/Chassis/Interfaces/IBrakeControlService/SetBrakeMode"
        )


# composition_nested.arxml


class TestCompositionNested:
    NAME = "composition_nested.arxml"

    def test_two_level_composition_nesting(self):
        root = _parse(self.NAME).getroot()
        compositions = _short_names(root, "COMPOSITION-SW-COMPONENT-TYPE")
        assert set(compositions) == {"InnerComposition", "OuterComposition"}

    def test_assembly_connector_present(self):
        root = _parse(self.NAME).getroot()
        assembly = root.findall(f".//{_ar('ASSEMBLY-SW-CONNECTOR')}")
        assert len(assembly) == 1

    def test_delegation_connectors_resolvable_and_dangling(self):
        root = _parse(self.NAME).getroot()
        delegations = root.findall(f".//{_ar('DELEGATION-SW-CONNECTOR')}")
        assert len(delegations) == 2
        targets = {
            d.findtext(_ar("SHORT-NAME")): d.findtext(
                f"{_ar('INNER-PORT-IREF')}/{_ar('P-PORT-IN-COMPOSITION-INSTANCE-REF')}/{_ar('TARGET-P-PORT-REF')}"
            )
            for d in delegations
        }
        # DEL_Front's inner target is a real port on SensorSwc.
        assert targets["DEL_Front"] == "/Acme/Chassis/Components/SensorSwc/pValue"
        # DEL_Rear's inner target ("pValueGhost") is not declared on SensorSwc anywhere
        # in this file — a genuinely dangling reference.
        swc_port_names = {
            el.findtext(_ar("SHORT-NAME"))
            for swc in root.iter(_ar("APPLICATION-SW-COMPONENT-TYPE"))
            for el in swc.iter(_ar("P-PORT-PROTOTYPE"))
        }
        dangling_target = targets["DEL_Rear"]
        assert dangling_target.rsplit("/", 1)[-1] not in swc_port_names

    def test_two_prototypes_of_same_swc_type(self):
        root = _parse(self.NAME).getroot()
        inner = next(
            c
            for c in root.iter(_ar("COMPOSITION-SW-COMPONENT-TYPE"))
            if c.findtext(_ar("SHORT-NAME")) == "InnerComposition"
        )
        prototypes = inner.findall(f".//{_ar('SW-COMPONENT-PROTOTYPE')}")
        type_refs = {p.findtext(_ar("TYPE-TREF")) for p in prototypes}
        assert len(prototypes) == 2
        assert type_refs == {"/Acme/Chassis/Components/SensorSwc"}


# unsupported_content.arxml


class TestUnsupportedContent:
    NAME = "unsupported_content.arxml"

    def test_constant_specification_present(self):
        root = _parse(self.NAME).getroot()
        constants = root.findall(f".//{_ar('CONSTANT-SPECIFICATION')}")
        assert len(constants) == 1
        assert constants[0].findtext(_ar("SHORT-NAME")) == "DefaultBrakeBias"

    def test_admin_data_sdg_present(self):
        root = _parse(self.NAME).getroot()
        admin_data = root.findall(f".//{_ar('ADMIN-DATA')}")
        assert len(admin_data) == 1
        sdgs = admin_data[0].findall(f".//{_ar('SDG')}")
        assert len(sdgs) == 1
        assert sdgs[0].attrib.get("GID") == "ACME-VENDOR-EXTENSION"

    def test_pr_port_present(self):
        root = _parse(self.NAME).getroot()
        pr_ports = root.findall(f".//{_ar('PR-PORT-PROTOTYPE')}")
        assert len(pr_ports) == 1
        assert pr_ports[0].findtext(_ar("SHORT-NAME")) == "prDiagnosticChannel"
        assert pr_ports[0].find(_ar("PROVIDED-INTERFACE-TREF")) is not None
        assert pr_ports[0].find(_ar("REQUIRED-INTERFACE-TREF")) is not None

    def test_unknown_vendor_tag_present(self):
        root = _parse(self.NAME).getroot()
        # ACME-EXPORT-METADATA is not in the AUTOSAR 4.2.2 schema; a namespace-qualified search must still
        # find it, though no AUTOSAR-aware reader knows it.
        unknown = root.findall(f".//{_ar('ACME-EXPORT-METADATA')}")
        assert len(unknown) == 1
        assert unknown[0].findtext(_ar("SHORT-NAME")) == "RhapsodyExportMeta"
        # Sits directly inside an ELEMENTS block, like a real unrecognized vendor element.
        elements_parents = [parent for parent in root.iter(_ar("ELEMENTS")) if unknown[0] in list(parent)]
        assert len(elements_parents) == 1


class TestInitValues:
    """The init-value fixture holds the four value-specification shapes and two lexical ones.

    The lexical ones are hexadecimal spellings and unlabelled record members.
    """

    NAME = "init_values.arxml"

    def test_all_four_value_specification_tags_are_present(self):
        root = _parse(self.NAME).getroot()
        counts = {
            tag: len(root.findall(f".//{_ar(tag)}"))
            for tag in (
                "APPLICATION-VALUE-SPECIFICATION",
                "NUMERICAL-VALUE-SPECIFICATION",
                "ARRAY-VALUE-SPECIFICATION",
                "RECORD-VALUE-SPECIFICATION",
            )
        }
        assert all(count > 0 for count in counts.values()), counts

    def test_every_constant_follows_the_derivable_name(self):
        """The constants follow ``{Interface}_{Element}_Init``; ``SR_`` is the interface names' own prefix."""
        root = _parse(self.NAME).getroot()
        names = _short_names(root, "CONSTANT-SPECIFICATION")
        assert names, "the fixture must carry constants"
        assert all(name.startswith("SR_") and name.endswith("_Init") for name in names), names

    def test_hexadecimal_spellings_are_present(self):
        """The fixture contains a value the importer must re-spell.

        Without one the importer's normalization test is vacuous.
        """
        text = (FIXTURES_DIR / self.NAME).read_text(encoding="utf-8")
        assert "0xff" in text
        assert "0x10" in text

    def test_one_record_constant_carries_unlabelled_members(self):
        """One record constant carries members without a SHORT-LABEL.

        AUTOSAR makes a member's SHORT-LABEL optional, which is why the importer matches record members positionally.
        """
        root = _parse(self.NAME).getroot()
        records = root.findall(f".//{_ar('RECORD-VALUE-SPECIFICATION')}")
        unlabelled = [
            member
            for record in records
            for fields in record.findall(_ar("FIELDS"))
            for member in list(fields)
            if member.find(_ar("SHORT-LABEL")) is None
        ]
        assert len(unlabelled) == 4, len(unlabelled)

    def test_the_port_com_specs_reference_the_same_constants_as_the_elements(self):
        """The clean path: no com-spec overrides its element's constant.

        The divergent cases are test_arxml_import_interfaces.py's.
        """
        root = _parse(self.NAME).getroot()
        element_refs = {
            vdp.findtext(_ar("SHORT-NAME")): vdp.find(_ar("INIT-VALUE"))
            .find(_ar("CONSTANT-REFERENCE"))
            .findtext(_ar("CONSTANT-REF"))
            for vdp in root.findall(f".//{_ar('VARIABLE-DATA-PROTOTYPE')}")
            if vdp.find(_ar("INIT-VALUE")) is not None
        }
        com_spec_refs = [
            (
                com_spec.findtext(_ar("DATA-ELEMENT-REF")).rsplit("/", 1)[1],
                com_spec.find(_ar("INIT-VALUE")).find(_ar("CONSTANT-REFERENCE")).findtext(_ar("CONSTANT-REF")),
            )
            for tag in ("NONQUEUED-SENDER-COM-SPEC", "NONQUEUED-RECEIVER-COM-SPEC")
            for com_spec in root.findall(f".//{_ar(tag)}")
        ]
        assert com_spec_refs, "the fixture must carry port-level COM-SPEC init values"
        for element_name, constant_ref in com_spec_refs:
            assert element_refs[element_name] == constant_ref


# 4.3.0 twins


def _tree_shape(element: ET.Element) -> tuple:
    """A comparable projection of an element: tag, attributes, texts, children -- recursively.

    Comments are not parsed, so the twins' differing header comments are ignored.
    """
    return (
        element.tag,
        dict(sorted(element.attrib.items())),
        (element.text or "").strip(),
        (element.tail or "").strip(),
        tuple(_tree_shape(child) for child in element),
    )


class TestVersionTwins:
    """Each 4.3.0 fixture is its 4.2.2 counterpart with one token changed -- nothing else.

    The reader is version-agnostic (the ``r4.0`` namespace spans 4.x), so a twin proves only label
    tolerance, and only while its content stays the same document. Not reached: the dialect of a real 4.3.0
    export.
    """

    @pytest.mark.parametrize(("twin", "original"), sorted(VERSION_TWINS.items()))
    def test_twin_content_is_identical_apart_from_the_schema_location(self, twin, original):
        twin_root = _parse(twin).getroot()
        original_root = _parse(original).getroot()

        schema_attr = f"{{{XSI_NS}}}schemaLocation"
        assert twin_root.attrib[schema_attr] != original_root.attrib[schema_attr]

        twin_shape = _tree_shape(twin_root)
        original_shape = _tree_shape(original_root)
        # The root's own attributes are where the declared version lives; compare everything else.
        assert twin_shape[0] == original_shape[0]
        assert twin_shape[2:] == original_shape[2:], f"{twin} and {original} have diverged in content"

    @pytest.mark.parametrize(("twin", "original"), sorted(VERSION_TWINS.items()))
    def test_root_attributes_differ_only_in_the_schema_location(self, twin, original):
        schema_attr = f"{{{XSI_NS}}}schemaLocation"
        twin_attribs = dict(_parse(twin).getroot().attrib)
        original_attribs = dict(_parse(original).getroot().attrib)
        del twin_attribs[schema_attr]
        del original_attribs[schema_attr]
        assert twin_attribs == original_attribs
