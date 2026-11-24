"""Tests for arxml_reader.py: package walker, path index, ImportModel skeleton.

Written against the hand-crafted fixtures in fixtures/arxml/: packages walked recursively, every
recognized element indexed by AUTOSAR path, DESC extraction, unknown-tag skip+report, the reader's error
scenarios (nonexistent file, malformed XML, empty AR-PACKAGES), and what the reader records of values
and number spellings (its SWCraft markers: test_arxml_reader_markers.py). It does not test element
*interpretation* (data types, interfaces, SWCs, ...): that is test_arxml_import_types.py and friends.
``--packages`` selection is not the reader's: `arxml_import.pipeline.run_import` selects after mapping,
covered by `packages/tools/tests/test_arxml_import_cli.py`'s `TestRunImportPackagesClosure`.
"""

from __future__ import annotations

import dataclasses
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from conftest import DIGIT_LIMIT_FLOOR, INIT_VALUES_MODEL_ROOT, int_max_str_digits
from swcraft_tools.arxml_reader import (
    ConstantArray,
    ConstantRecord,
    ConstantScalar,
    DataConstrInfo,
    ImportModel,
    SkipCategory,
    SkipEntry,
    StandardRule,
    ValueForm,
    _read_init_value_ref,
    _read_port_com_spec_inits,
    _read_value_specification,
    parse_float_literal,
    parse_int_literal,
    read_arxml,
)
from swcraft_tools.writer import generate_arxml

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "arxml"
MINIMAL_SWC = FIXTURES_DIR / "minimal_swc.arxml"
MINIMAL_SWC_4_3_0 = FIXTURES_DIR / "minimal_swc_4_3_0.arxml"
UNSUPPORTED_CONTENT = FIXTURES_DIR / "unsupported_content.arxml"
INIT_VALUES = FIXTURES_DIR / "init_values.arxml"


# Packages walked recursively


def test_packages_walked_recursively():
    model = read_arxml(str(MINIMAL_SWC))
    assert model.packages == [
        "/Acme",
        "/Acme/Chassis",
        "/Acme/Chassis/DataTypes",
        "/Acme/Chassis/Interfaces",
        "/Acme/Chassis/Components",
    ]


# Every element indexed by AUTOSAR path


def test_top_level_elements_indexed_by_path():
    model = read_arxml(str(MINIMAL_SWC))
    assert model.path_index["/Acme/Chassis/DataTypes/uint8"].tag == "SW-BASE-TYPE"
    assert model.path_index["/Acme/Chassis/DataTypes/WheelSpeedValue_impl"].tag == "IMPLEMENTATION-DATA-TYPE"
    assert model.path_index["/Acme/Chassis/Interfaces/IWheelSpeed"].tag == "SENDER-RECEIVER-INTERFACE"
    assert model.path_index["/Acme/Chassis/Components/WheelSpeedSensorSwc"].tag == "APPLICATION-SW-COMPONENT-TYPE"
    assert model.path_index["/Acme/Chassis/Components/WheelSpeedSensorSwc_Implementation"].tag == "SWC-IMPLEMENTATION"


def test_nested_named_elements_indexed_by_path():
    """Grouping containers (PORTS, DATA-ELEMENTS, RUNNABLES, EVENTS, ...) are transparent to path building.

    Named descendants get the paths the fixture's *-REF values use."""
    model = read_arxml(str(MINIMAL_SWC))

    data_element = model.path_index["/Acme/Chassis/Interfaces/IWheelSpeed/speedValue"]
    assert data_element.tag == "VARIABLE-DATA-PROTOTYPE"

    p_port = model.path_index["/Acme/Chassis/Components/WheelSpeedSensorSwc/pWheelSpeed"]
    assert p_port.tag == "P-PORT-PROTOTYPE"
    r_port = model.path_index["/Acme/Chassis/Components/WheelSpeedSensorSwc/rWheelSpeedEcho"]
    assert r_port.tag == "R-PORT-PROTOTYPE"

    behavior_path = "/Acme/Chassis/Components/WheelSpeedSensorSwc/WheelSpeedSensorSwc_InternalBehavior"
    runnable = model.path_index[f"{behavior_path}/WheelSpeedSensorRun"]
    assert runnable.tag == "RUNNABLE-ENTITY"
    timing_event = model.path_index[f"{behavior_path}/TMT_WheelSpeedSensorRun"]
    assert timing_event.tag == "TIMING-EVENT"

    # The path index is exactly what a *-REF value in the fixture points at: reference
    # resolution walks it.
    start_on_event_ref = timing_event.node.findtext("{http://autosar.org/schema/r4.0}START-ON-EVENT-REF")
    assert start_on_event_ref == f"{behavior_path}/WheelSpeedSensorRun"
    assert start_on_event_ref in model.path_index


# DESC extraction


def test_desc_extracted_for_multiple_element_kinds():
    model = read_arxml(str(MINIMAL_SWC))
    assert model.path_index["/Acme/Chassis/Components/WheelSpeedSensorSwc"].description == (
        "Wheel speed sensor application component (anonymized fixture)."
    )
    assert model.path_index["/Acme/Chassis/Interfaces/IWheelSpeed"].description == (
        "Wheel speed sender/receiver interface (anonymized fixture)."
    )
    assert model.path_index["/Acme/Chassis/Components/WheelSpeedSensorSwc/pWheelSpeed"].description == (
        "Provides the current wheel speed reading."
    )


def test_desc_defaults_to_empty_string_when_absent():
    model = read_arxml(str(MINIMAL_SWC))
    # RUNNABLE-ENTITY in the fixture carries no DESC.
    behavior_path = "/Acme/Chassis/Components/WheelSpeedSensorSwc/WheelSpeedSensorSwc_InternalBehavior"
    runnable = model.path_index[f"{behavior_path}/WheelSpeedSensorRun"]
    assert runnable.description == ""


# Unknown tags inside ELEMENTS -> SkipLog, never a crash


def test_unknown_vendor_tag_recorded_in_skip_log():
    model = read_arxml(str(UNSUPPORTED_CONTENT))
    unknown_entries = [e for e in model.skip_log if e.tag == "ACME-EXPORT-METADATA"]
    assert len(unknown_entries) == 1
    entry = unknown_entries[0]
    assert entry.path == "/Acme/Chassis/Components/RhapsodyExportMeta"
    assert "unrecognized" in entry.reason.lower()
    # Skipped means genuinely absent from the path index, not indexed-anyway.
    assert entry.path not in model.path_index


def test_known_but_deferred_tags_are_not_skip_logged():
    """The reader indexes CONSTANT-SPECIFICATION rather than flagging it as unknown.

    It is a recognized ELEMENTS tag; its interpretation is the `arxml_import` package's."""
    model = read_arxml(str(UNSUPPORTED_CONTENT))
    assert "/Acme/Chassis/Constants/DefaultBrakeBias" in model.path_index
    assert not any(e.tag == "CONSTANT-SPECIFICATION" for e in model.skip_log)


def test_skip_log_length_and_truthiness():
    model = read_arxml(str(UNSUPPORTED_CONTENT))
    # 1 unrecognized vendor tag (ACME-EXPORT-METADATA) + 1 ADMIN-DATA/SDG blob
    # on DiagnosticBridgeSwc, skip+reported because nothing is silently dropped.
    assert len(model.skip_log) == 2
    assert bool(model.skip_log) is True

    empty_model = ImportModel(source_path="unused")
    assert len(empty_model.skip_log) == 0
    assert bool(empty_model.skip_log) is False


# Malformed or unrecognized content is skip+reported, never dropped in silence


def test_duplicate_full_autosar_path_is_skip_reported_not_silently_overwritten(tmp_path):
    """Of two elements with one full AUTOSAR path, the first is kept in the path index.

    The collision is skip+reported; the second never overwrites the first without a report line."""
    bad_file = tmp_path / "dup_path.arxml"
    bad_file.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<AUTOSAR xmlns="http://autosar.org/schema/r4.0">\n'
        "  <AR-PACKAGES>\n"
        "    <AR-PACKAGE>\n"
        "      <SHORT-NAME>Pkg</SHORT-NAME>\n"
        "      <ELEMENTS>\n"
        "        <APPLICATION-SW-COMPONENT-TYPE>\n"
        "          <SHORT-NAME>DupSwc</SHORT-NAME>\n"
        '          <DESC><L-2 L="FOR-ALL">first declaration</L-2></DESC>\n'
        "        </APPLICATION-SW-COMPONENT-TYPE>\n"
        "        <APPLICATION-SW-COMPONENT-TYPE>\n"
        "          <SHORT-NAME>DupSwc</SHORT-NAME>\n"
        '          <DESC><L-2 L="FOR-ALL">second declaration</L-2></DESC>\n'
        "        </APPLICATION-SW-COMPONENT-TYPE>\n"
        "      </ELEMENTS>\n"
        "    </AR-PACKAGE>\n"
        "  </AR-PACKAGES>\n"
        "</AUTOSAR>\n",
        encoding="utf-8",
    )

    model = read_arxml(str(bad_file))

    assert model.path_index["/Pkg/DupSwc"].description == "first declaration"
    assert len(model.swcs) == 1
    assert model.swcs[0].description == "first declaration"

    dup_entries = [e for e in model.skip_log if e.path == "/Pkg/DupSwc"]
    assert len(dup_entries) == 1
    assert "duplicate" in dup_entries[0].reason.lower()


def test_malformed_compu_scale_missing_vt_is_skip_reported(tmp_path):
    """A malformed TEXTTABLE COMPU-SCALE is dropped from `scales` and skip+reported.

    Malformed is missing its VT, or with an unparseable or missing LOWER-LIMIT; it is never omitted without a report
    line."""
    bad_file = tmp_path / "bad_compu_scale.arxml"
    bad_file.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<AUTOSAR xmlns="http://autosar.org/schema/r4.0">\n'
        "  <AR-PACKAGES>\n"
        "    <AR-PACKAGE>\n"
        "      <SHORT-NAME>Pkg</SHORT-NAME>\n"
        "      <ELEMENTS>\n"
        "        <COMPU-METHOD>\n"
        "          <SHORT-NAME>BadEnum_CompuMethod</SHORT-NAME>\n"
        "          <CATEGORY>TEXTTABLE</CATEGORY>\n"
        "          <COMPU-INTERNAL-TO-PHYS>\n"
        "            <COMPU-SCALES>\n"
        "              <COMPU-SCALE>\n"
        '                <LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT>\n'
        "                <COMPU-CONST>\n"
        "                  <VT>OFF</VT>\n"
        "                </COMPU-CONST>\n"
        "              </COMPU-SCALE>\n"
        "              <COMPU-SCALE>\n"
        "                <!-- Missing VT literal: malformed. -->\n"
        '                <LOWER-LIMIT INTERVAL-TYPE="CLOSED">1</LOWER-LIMIT>\n'
        "              </COMPU-SCALE>\n"
        "            </COMPU-SCALES>\n"
        "          </COMPU-INTERNAL-TO-PHYS>\n"
        "        </COMPU-METHOD>\n"
        "      </ELEMENTS>\n"
        "    </AR-PACKAGE>\n"
        "  </AR-PACKAGES>\n"
        "</AUTOSAR>\n",
        encoding="utf-8",
    )

    model = read_arxml(str(bad_file))

    assert len(model.compu_methods) == 1
    assert len(model.compu_methods[0].scales) == 1
    assert model.compu_methods[0].scales[0].literal == "OFF"
    malformed_entries = [e for e in model.skip_log if e.tag == "COMPU-SCALE"]
    assert len(malformed_entries) == 1
    assert "malformed" in malformed_entries[0].reason.lower()


def test_a_table_scale_records_whether_it_states_a_symbol_and_nothing_of_its_text(tmp_path):
    """A table scale records whether it states a ``SYMBOL`` and nothing of its text.

    The flag lets a text under a table one of whose scales states a ``SYMBOL`` be named. The ``SYMBOL`` itself is the
    closed table's ``unread`` item and is reported."""
    source = tmp_path / "symbol_scale.arxml"
    source.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<AUTOSAR xmlns="http://autosar.org/schema/r4.0">\n'
        "  <AR-PACKAGES><AR-PACKAGE><SHORT-NAME>Pkg</SHORT-NAME><ELEMENTS>\n"
        "    <COMPU-METHOD><SHORT-NAME>Mode_CompuMethod</SHORT-NAME><CATEGORY>TEXTTABLE</CATEGORY>\n"
        "      <COMPU-INTERNAL-TO-PHYS><COMPU-SCALES>\n"
        '        <COMPU-SCALE><SYMBOL>MODE_OFF</SYMBOL><LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT>'
        '<UPPER-LIMIT INTERVAL-TYPE="CLOSED">0</UPPER-LIMIT><COMPU-CONST><VT>OFF</VT></COMPU-CONST></COMPU-SCALE>\n'
        '        <COMPU-SCALE><LOWER-LIMIT INTERVAL-TYPE="CLOSED">1</LOWER-LIMIT>'
        '<UPPER-LIMIT INTERVAL-TYPE="CLOSED">1</UPPER-LIMIT><COMPU-CONST><VT>ON</VT></COMPU-CONST></COMPU-SCALE>\n'
        "      </COMPU-SCALES></COMPU-INTERNAL-TO-PHYS>\n"
        "    </COMPU-METHOD>\n"
        "  </ELEMENTS></AR-PACKAGE></AR-PACKAGES>\n"
        "</AUTOSAR>\n",
        encoding="utf-8",
    )

    model = read_arxml(str(source))

    (method,) = model.compu_methods
    assert [(scale.literal, scale.states_symbol) for scale in method.scales] == [("OFF", True), ("ON", False)]
    assert not any("MODE_OFF" in repr(dataclasses.astuple(scale)) for scale in method.scales)
    assert [entry.tag for entry in model.skip_log if entry.tag == "SYMBOL"] == ["SYMBOL"]


def test_unrecognized_port_kind_is_skip_reported(tmp_path):
    """An unrecognized port kind in a SWC's PORTS block is skip+reported like an unknown top-level tag."""
    bad_file = tmp_path / "bad_port_kind.arxml"
    bad_file.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<AUTOSAR xmlns="http://autosar.org/schema/r4.0">\n'
        "  <AR-PACKAGES>\n"
        "    <AR-PACKAGE>\n"
        "      <SHORT-NAME>Pkg</SHORT-NAME>\n"
        "      <ELEMENTS>\n"
        "        <APPLICATION-SW-COMPONENT-TYPE>\n"
        "          <SHORT-NAME>WeirdSwc</SHORT-NAME>\n"
        "          <PORTS>\n"
        "            <MODE-SWITCH-PORT-PROTOTYPE>\n"
        "              <SHORT-NAME>mpWeird</SHORT-NAME>\n"
        "            </MODE-SWITCH-PORT-PROTOTYPE>\n"
        "          </PORTS>\n"
        "        </APPLICATION-SW-COMPONENT-TYPE>\n"
        "      </ELEMENTS>\n"
        "    </AR-PACKAGE>\n"
        "  </AR-PACKAGES>\n"
        "</AUTOSAR>\n",
        encoding="utf-8",
    )

    model = read_arxml(str(bad_file))

    assert len(model.swcs) == 1
    assert model.swcs[0].ports == []
    entries = [e for e in model.skip_log if e.tag == "MODE-SWITCH-PORT-PROTOTYPE"]
    assert len(entries) == 1
    assert entries[0].path == "/Pkg/WeirdSwc/mpWeird"
    assert "unrecognized port kind" in entries[0].reason.lower()


def test_unrecognized_event_kind_is_skip_reported(tmp_path):
    """An unrecognized event kind in a SWC's EVENTS block is skip+reported, never ignored silently."""
    bad_file = tmp_path / "bad_event_kind.arxml"
    bad_file.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<AUTOSAR xmlns="http://autosar.org/schema/r4.0">\n'
        "  <AR-PACKAGES>\n"
        "    <AR-PACKAGE>\n"
        "      <SHORT-NAME>Pkg</SHORT-NAME>\n"
        "      <ELEMENTS>\n"
        "        <APPLICATION-SW-COMPONENT-TYPE>\n"
        "          <SHORT-NAME>WeirdSwc</SHORT-NAME>\n"
        "          <INTERNAL-BEHAVIORS>\n"
        "            <SWC-INTERNAL-BEHAVIOR>\n"
        "              <SHORT-NAME>WeirdSwc_Behavior</SHORT-NAME>\n"
        "              <EVENTS>\n"
        "                <DATA-RECEIVE-ERROR-EVENT>\n"
        "                  <SHORT-NAME>weirdEvent</SHORT-NAME>\n"
        "                </DATA-RECEIVE-ERROR-EVENT>\n"
        "              </EVENTS>\n"
        "            </SWC-INTERNAL-BEHAVIOR>\n"
        "          </INTERNAL-BEHAVIORS>\n"
        "        </APPLICATION-SW-COMPONENT-TYPE>\n"
        "      </ELEMENTS>\n"
        "    </AR-PACKAGE>\n"
        "  </AR-PACKAGES>\n"
        "</AUTOSAR>\n",
        encoding="utf-8",
    )

    model = read_arxml(str(bad_file))

    assert len(model.swcs) == 1
    assert model.swcs[0].events == []
    entries = [e for e in model.skip_log if e.tag == "DATA-RECEIVE-ERROR-EVENT"]
    assert len(entries) == 1
    assert "unrecognized event kind" in entries[0].reason.lower()


def test_unrecognized_connector_kind_is_skip_reported(tmp_path):
    """An unrecognized connector kind in a composition's CONNECTORS block is skip+reported, never ignored silently."""
    bad_file = tmp_path / "bad_connector_kind.arxml"
    bad_file.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<AUTOSAR xmlns="http://autosar.org/schema/r4.0">\n'
        "  <AR-PACKAGES>\n"
        "    <AR-PACKAGE>\n"
        "      <SHORT-NAME>Pkg</SHORT-NAME>\n"
        "      <ELEMENTS>\n"
        "        <COMPOSITION-SW-COMPONENT-TYPE>\n"
        "          <SHORT-NAME>WeirdComposition</SHORT-NAME>\n"
        "          <CONNECTORS>\n"
        "            <PASS-THROUGH-SW-CONNECTOR>\n"
        "              <SHORT-NAME>weirdConnector</SHORT-NAME>\n"
        "            </PASS-THROUGH-SW-CONNECTOR>\n"
        "          </CONNECTORS>\n"
        "        </COMPOSITION-SW-COMPONENT-TYPE>\n"
        "      </ELEMENTS>\n"
        "    </AR-PACKAGE>\n"
        "  </AR-PACKAGES>\n"
        "</AUTOSAR>\n",
        encoding="utf-8",
    )

    model = read_arxml(str(bad_file))

    assert len(model.compositions) == 1
    assert model.compositions[0].assembly_connectors == []
    assert model.compositions[0].delegation_connectors == []
    entries = [e for e in model.skip_log if e.tag == "PASS-THROUGH-SW-CONNECTOR"]
    assert len(entries) == 1
    assert "unrecognized connector kind" in entries[0].reason.lower()


def test_admin_data_sdg_is_skip_reported(tmp_path):
    """ADMIN-DATA/SDG vendor metadata is skip+reported as present, not imported or preserved, never dropped."""
    bad_file = tmp_path / "admin_data.arxml"
    bad_file.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<AUTOSAR xmlns="http://autosar.org/schema/r4.0">\n'
        "  <AR-PACKAGES>\n"
        "    <AR-PACKAGE>\n"
        "      <SHORT-NAME>Pkg</SHORT-NAME>\n"
        "      <ELEMENTS>\n"
        "        <APPLICATION-SW-COMPONENT-TYPE>\n"
        "          <SHORT-NAME>MetaSwc</SHORT-NAME>\n"
        "          <ADMIN-DATA>\n"
        "            <SDGS>\n"
        '              <SDG GID="VendorExtension">\n'
        '                <SD GID="Tool">SomeExportTool</SD>\n'
        "              </SDG>\n"
        "            </SDGS>\n"
        "          </ADMIN-DATA>\n"
        "        </APPLICATION-SW-COMPONENT-TYPE>\n"
        "      </ELEMENTS>\n"
        "    </AR-PACKAGE>\n"
        "  </AR-PACKAGES>\n"
        "</AUTOSAR>\n",
        encoding="utf-8",
    )

    model = read_arxml(str(bad_file))

    assert len(model.swcs) == 1  # the SWC itself still imports fine
    entries = [e for e in model.skip_log if e.tag == "ADMIN-DATA"]
    assert len(entries) == 1
    assert "MetaSwc" in entries[0].reason
    assert "no swcraft representation" in entries[0].reason.lower()


# Error scenarios


def test_nonexistent_file_raises_file_not_found_error():
    with pytest.raises(FileNotFoundError):
        read_arxml(str(FIXTURES_DIR / "does_not_exist.arxml"))


def test_malformed_xml_raises_clean_value_error(tmp_path):
    bad_file = tmp_path / "broken.arxml"
    bad_file.write_text("<AUTOSAR><AR-PACKAGES><AR-PACKAGE></AUTOSAR>", encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        read_arxml(str(bad_file))

    message = str(excinfo.value)
    # A clean, single-line message, not a raw ElementTree/expat traceback.
    assert "Traceback" not in message
    assert str(bad_file) in message


def test_empty_ar_packages_is_valid_empty_model(tmp_path):
    empty_file = tmp_path / "empty.arxml"
    empty_file.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<AUTOSAR xmlns="http://autosar.org/schema/r4.0">\n'
        "  <AR-PACKAGES>\n"
        "  </AR-PACKAGES>\n"
        "</AUTOSAR>\n",
        encoding="utf-8",
    )

    model = read_arxml(str(empty_file))

    assert model.packages == []
    assert model.path_index == {}
    assert len(model.skip_log) == 0


def test_no_ar_packages_element_is_valid_empty_model(tmp_path):
    no_packages_file = tmp_path / "no_packages.arxml"
    no_packages_file.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<AUTOSAR xmlns="http://autosar.org/schema/r4.0" />\n', encoding="utf-8"
    )

    model = read_arxml(str(no_packages_file))

    assert model.packages == []
    assert model.path_index == {}


# parse_int_literal -- hex/binary/octal COMPU-SCALE/LOWER-LIMIT literals, unit-tested here because
# otherwise only the real-export suite outside the fast gate reaches them.


#: Both grammars a number field is read by (``arxml_reader.NumberGrammar``).
GRAMMARS = ("pattern", "formula")


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_parse_int_literal_decimal(grammar):
    assert parse_int_literal("8", grammar=grammar) == 8


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_parse_int_literal_negative_decimal(grammar):
    assert parse_int_literal("-3", grammar=grammar) == -3


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_parse_int_literal_hex_lowercase(grammar):
    assert parse_int_literal("0x00", grammar=grammar) == 0
    assert parse_int_literal("0x1f", grammar=grammar) == 31


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_parse_int_literal_hex_uppercase(grammar):
    assert parse_int_literal("0X1F", grammar=grammar) == 31


def test_parse_int_literal_negative_hex_is_a_formula_spelling_only():
    """A negative hexadecimal is a formula spelling only.

    The pattern signs a decimal only (XSD R4.3.0:86066, R4.2.2:74776); the
    formula's sign is a unary operator before any literal (GST R4.3.0:4667-4668)."""
    assert parse_int_literal("-0x0A", grammar="formula") == -10
    assert parse_int_literal("-0x0A", grammar="pattern") is None


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_parse_int_literal_binary(grammar):
    assert parse_int_literal("0b101", grammar=grammar) == 5


def test_parse_int_literal_negative_binary_is_a_formula_spelling_only():
    assert parse_int_literal("-0b101", grammar="formula") == -5
    assert parse_int_literal("-0b101", grammar="pattern") is None


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_parse_int_literal_refuses_a_power_of_two_base_it_could_not_write(grammar):
    """The integer-literal helper refuses a power-of-two base too wide to write.

    It reads COMPU-SCALE limits, ``SW-ARRAYSIZE`` and a primitive's size. Bases 16 and 2 are exempt from
    ``sys.get_int_max_str_digits()``, but the value leaves as a decimal YAML scalar and ``yaml.safe_dump`` would raise
    mid-import. These widths exceed the limit on either count, so which width is bounded is
    :func:`test_parse_int_literal_bounds_the_decimal_width_and_not_the_source_width`'s question."""
    with int_max_str_digits(DIGIT_LIMIT_FLOOR):
        assert parse_int_literal("0x" + "f" * (DIGIT_LIMIT_FLOOR + 1), grammar=grammar) is None
        assert parse_int_literal("0b" + "1" * (DIGIT_LIMIT_FLOOR * 4), grammar=grammar) is None
        # Octal is exempt too: `int(digits, 8)` reads any width, so the guard alone stops it.
        assert parse_int_literal("0" + "7" * (DIGIT_LIMIT_FLOOR * 2), grammar=grammar) is None
        assert parse_int_literal("9" * (DIGIT_LIMIT_FLOOR + 1), grammar=grammar) is None
        # Non-vacuity: an ordinary literal of each base still reads under the
        # same pinned limit.
        assert parse_int_literal("0x1f", grammar=grammar) == 31
        assert parse_int_literal("0b101", grammar=grammar) == 5
        assert parse_int_literal("037", grammar=grammar) == 31
        assert parse_int_literal("31", grammar=grammar) == 31


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_parse_int_literal_bounds_the_decimal_width_and_not_the_source_width(grammar):
    """The decimal width is bounded, not the source width, at the pair where the two guards disagree.

    The pair is adjacent (see :data:`_WIDEST_HEX_RUN_THAT_FITS`). The oracle is ``str()`` outside the pinned limit,
    independent of the guard's arithmetic and exact where a ``bit_length()`` estimate would not be."""
    fits = "f" * _WIDEST_HEX_RUN_THAT_FITS
    over = "f" * (_WIDEST_HEX_RUN_THAT_FITS + 1)
    assert len(str(int(fits, 16))) == DIGIT_LIMIT_FLOOR
    assert len(str(int(over, 16))) == DIGIT_LIMIT_FLOOR + 1
    assert len(fits) < DIGIT_LIMIT_FLOOR and len(over) < DIGIT_LIMIT_FLOOR

    with int_max_str_digits(DIGIT_LIMIT_FLOOR):
        assert parse_int_literal("0x" + fits, grammar=grammar) == int(fits, 16)
        assert parse_int_literal("0x" + over, grammar=grammar) is None


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_parse_int_literal_reads_a_wide_hex_value_when_the_limit_is_disabled(grammar):
    """A disabled digit limit reads a wide hexadecimal value.

    0 means *no* limit (``PYTHONINTMAXSTRDIGITS=0``), and the guard inherits that from ``str()``: ``yaml.safe_dump``
    writes the value fine there, so refusing it would drop a literal."""
    digits = "f" * (DIGIT_LIMIT_FLOOR + 1)
    with int_max_str_digits(0):
        assert parse_int_literal("0x" + digits, grammar=grammar) == int(digits, 16)


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_parse_int_literal_reads_a_leading_zero_as_octal(grammar):
    """``010`` is the standard's octal eight in both grammars, never the decimal ten.

    ``0[0-7]+``, XSD R4.3.0:86078 / R4.2.2:74788; ``OctIntegerLiteral``, GST R4.3.0:4782-4783 / R4.2.2:3936-3937.
    Python's ``int(text, 0)`` is no help: it rejects ``010`` as an incomplete octal prefix."""
    assert parse_int_literal("010", grammar=grammar) == 8
    assert parse_int_literal("00", grammar=grammar) == 0
    assert parse_int_literal("0", grammar=grammar) == 0


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_parse_int_literal_none_returns_none(grammar):
    assert parse_int_literal(None, grammar=grammar) is None


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_parse_int_literal_empty_string_returns_none(grammar):
    assert parse_int_literal("", grammar=grammar) is None
    assert parse_int_literal("   ", grammar=grammar) is None


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_parse_int_literal_unparsable_returns_none_not_raises(grammar):
    assert parse_int_literal("not-a-number", grammar=grammar) is None


# parse_float_literal -- TIMING-EVENT PERIOD and RUNNABLE-ENTITY MINIMUM-START-INTERVAL
# are read through it, so non-numeric text is a reportable value and not an uncaught
# ValueError aborting the import; it also rejects the non-finite values (NaN/Inf)
# Python's own float() accepts.


def test_parse_float_literal_decimal():
    assert parse_float_literal("8") == 8.0
    assert parse_float_literal("0.5") == 0.5


def test_parse_float_literal_negative():
    assert parse_float_literal("-3.25") == -3.25


def test_parse_float_literal_none_returns_none():
    assert parse_float_literal(None) is None


def test_parse_float_literal_empty_string_returns_none():
    assert parse_float_literal("") is None
    assert parse_float_literal("   ") is None


def test_parse_float_literal_unparsable_returns_none_not_raises():
    assert parse_float_literal("not-a-number") is None


def test_parse_float_literal_nan_returns_none():
    assert parse_float_literal("nan") is None
    assert parse_float_literal("NaN") is None


def test_parse_float_literal_inf_returns_none():
    assert parse_float_literal("inf") is None
    assert parse_float_literal("-inf") is None


# AUTOSAR 4.3.0 input

#: ImportModel fields this comparison handles specially. ``source_path`` (the filename) and
#: ``declared_schema`` (the ``xsi:schemaLocation`` label, never a traversal branch; its own test is
#: below) are what the twins are *supposed* to differ in; ``path_index`` holds live ``ET.Element``
#: nodes, which never compare equal, so it gets its own projection.
_UNCOMPARABLE_IMPORT_MODEL_FIELDS = {"source_path", "declared_schema", "path_index"}


def _import_model_projection(model: ImportModel) -> dict:
    """A comparable projection of an ``ImportModel``.

    Enumerates the dataclass fields rather than listing them, so a field added to the reader is
    compared here automatically.
    """
    projection = {
        field.name: getattr(model, field.name)
        for field in dataclasses.fields(model)
        if field.name not in _UNCOMPARABLE_IMPORT_MODEL_FIELDS
    }
    projection["path_index"] = {path: (element.tag, element.description) for path, element in model.path_index.items()}
    return projection


class TestReadsA4_3_0LabelledFile:
    """``read_arxml()`` reads a file labelled AUTOSAR 4.3.0 exactly as it reads its 4.2.2 twin.

    ``AR_NS`` is stable across the AUTOSAR 4.x line, and ``xsi:schemaLocation`` is read once, as a
    label (``declared_schema``), and branches on nothing.

    **Does not reach** the dialect of a real 4.3.0 export: ``minimal_swc_4_3_0.arxml`` is hand-crafted
    to SWCraft's own shapes. It shows that nothing in the reader is gated on the declared version, and
    the skip logs are compared entry for entry, so a newly skipped element fails by name.
    """

    def test_the_fixture_really_declares_4_3_0(self):
        """Guards the guard: a twin that lost its relabel would make every test below vacuous."""
        header = MINIMAL_SWC_4_3_0.read_text(encoding="utf-8")
        assert "AUTOSAR_4-3-0.xsd" in header
        assert "AUTOSAR_4-2-2.xsd" not in header

    def test_reads_the_same_model_as_its_4_2_2_twin(self):
        at_4_3_0 = read_arxml(str(MINIMAL_SWC_4_3_0))
        at_4_2_2 = read_arxml(str(MINIMAL_SWC))

        assert _import_model_projection(at_4_3_0) == _import_model_projection(at_4_2_2)

    def test_source_path_is_the_file_actually_read(self):
        """A field that must differ — otherwise the comparison above proves nothing."""
        assert read_arxml(str(MINIMAL_SWC_4_3_0)).source_path == str(MINIMAL_SWC_4_3_0)

    def test_the_declared_schema_is_each_twins_own_label(self):
        """The other field that differs, and only as a label: the models above compare equal."""
        assert read_arxml(str(MINIMAL_SWC_4_3_0)).declared_schema == "AUTOSAR_4-3-0.xsd"
        assert read_arxml(str(MINIMAL_SWC)).declared_schema == "AUTOSAR_4-2-2.xsd"

    def test_nothing_is_skipped(self):
        """No element of a 4.3.0 file goes unread. A finding here would be named, not silent."""
        model = read_arxml(str(MINIMAL_SWC_4_3_0))
        assert [(entry.path, entry.tag, entry.reason) for entry in model.skip_log] == []

    def test_every_layer_of_the_fixture_is_indexed(self):
        """Every layer is spelled out, since an equality also holds for two equally *empty* results.

        These are the elements a reader that bailed on an unknown version would be missing."""
        model = read_arxml(str(MINIMAL_SWC_4_3_0))

        assert model.packages == [
            "/Acme",
            "/Acme/Chassis",
            "/Acme/Chassis/DataTypes",
            "/Acme/Chassis/Interfaces",
            "/Acme/Chassis/Components",
        ]
        assert [t.name for t in model.sw_base_types] == ["uint8"]
        assert [t.name for t in model.impl_data_types] == ["WheelSpeedValue_impl"]
        assert [i.name for i in model.sr_interfaces] == ["IWheelSpeed"]
        assert [s.name for s in model.swcs] == ["WheelSpeedSensorSwc"]
        swc = model.swcs[0]
        assert [p.name for p in swc.ports] == ["pWheelSpeed", "rWheelSpeedEcho"]
        assert [r.name for r in swc.runnables] == ["WheelSpeedSensorRun"]
        assert [(e.name, e.kind, e.period) for e in swc.events] == [("TMT_WheelSpeedSensorRun", "TIMING-EVENT", 0.01)]

    assert parse_float_literal("Infinity") is None


# CONSTANT-SPECIFICATION values, the element-level INIT-VALUE reference, and the
# port COM-SPEC references the mapper compares it against.


def _value_spec(fragment: str):
    """Read one value-specification fragment, namespace and all."""
    return _read_value_specification(ET.fromstring(fragment))


_NS_ATTR = ' xmlns="http://autosar.org/schema/r4.0"'

#: The widest run of hexadecimal ``f`` whose DECIMAL form still fits in ``DIGIT_LIMIT_FLOOR``
#: digits (531 at the 640-digit floor), measured with ``str()`` outside the pinned limit. The guards
#: bound the DECIMAL width; a wrong guard would bound the SOURCE width. Hexadecimal is denser
#: (``log(16)/log(10)`` = 1.204…), so the two differ only in ``limit/1.204 < n <= limit``; this
#: constant and ``+ 1`` are that window's top edge, one digit apart, so the pair catches the wrong
#: guard and an off-by-one in either direction.
_WIDEST_HEX_RUN_THAT_FITS = next(
    n for n in range(1, DIGIT_LIMIT_FLOOR + 2) if len(str(int("f" * (n + 1), 16))) > DIGIT_LIMIT_FLOOR
)


class TestReadsTheFourValueSpecificationShapes:
    """The four value-specification tags the reader reads.

    They are read from the fixture rather than from an invented tree, so the shapes stay a file's."""

    def test_the_eight_constants_are_extracted_with_their_values(self):
        model = read_arxml(str(INIT_VALUES))
        by_name = {constant.name: constant for constant in model.constant_specs}
        assert set(by_name) == {
            "SR_IScalars_counter_Init",
            "SR_IScalars_level_Init",
            "SR_IScalars_gear_Init",
            "SR_IScalars_speed_Init",
            "SR_IScalars_mode_Init",
            "SR_IComposites_triple_Init",
            "SR_IComposites_point_Init",
            "SR_IComposites_pairs_Init",
        }
        assert all(constant.unreadable_reason is None for constant in by_name.values())

    def test_an_application_value_specification_is_read_from_sw_values_phys(self):
        """The value is read from ``SW-VALUES-PHYS``, not from its ``SW-ARRAYSIZE`` sibling.

        The sibling is the array size and is 1 for every one of the fixture's scalars."""
        model = read_arxml(str(INIT_VALUES))
        counter = next(c for c in model.constant_specs if c.name == "SR_IScalars_counter_Init")
        assert counter.value == ConstantScalar(value=255, radix_source="0xff")

    def test_hexadecimal_is_read_as_its_value_and_not_as_its_spelling(self):
        """``0xff`` imports as 255 without its radix, and the raw spelling stays on ``radix_source``.

        With it `map_interfaces` can report one lossiness line per element rather than rewrite silently."""
        model = read_arxml(str(INIT_VALUES))
        counter = next(c for c in model.constant_specs if c.name == "SR_IScalars_counter_Init")
        assert counter.value == ConstantScalar(value=255, radix_source="0xff")
        triple = next(c for c in model.constant_specs if c.name == "SR_IComposites_triple_Init")
        assert triple.value.members[2] == ConstantScalar(value=16, radix_source="0x10")

    def test_a_decimal_spelling_records_no_hexadecimal_source(self):
        """The non-vacuity control: a value written in decimal carries no ``radix_source``.

        The report therefore counts real rewrites and not every value read."""
        model = read_arxml(str(INIT_VALUES))
        level = next(c for c in model.constant_specs if c.name == "SR_IScalars_level_Init")
        assert level.value == ConstantScalar(value=7, radix_source=None)

    def test_an_enumerations_text_is_read_from_its_vt(self):
        """A ``VT`` is a text, in the application form."""
        model = read_arxml(str(INIT_VALUES))
        gear = next(c for c in model.constant_specs if c.name == "SR_IScalars_gear_Init")
        assert gear.value == ConstantScalar(value="DRIVE")
        assert (gear.value.form, gear.value.unit_ref) == (ValueForm.APPLICATION, None)

    def test_a_text_value_is_read_from_its_value(self):
        """A ``TEXT-VALUE-SPECIFICATION``'s ``VALUE`` is a text, in the text form."""
        model = read_arxml(str(INIT_VALUES))
        mode = next(c for c in model.constant_specs if c.name == "SR_IScalars_mode_Init")
        assert (mode.value, mode.value.form) == (ConstantScalar(value="ON"), ValueForm.TEXT)

    def test_a_values_unit_reference_is_read_beside_its_number(self):
        model = read_arxml(str(INIT_VALUES))
        speed = next(c for c in model.constant_specs if c.name == "SR_IScalars_speed_Init")
        assert speed.value == ConstantScalar(value=30)
        assert (speed.value.form, speed.value.unit_ref) == (
            ValueForm.APPLICATION,
            "/Acme/Chassis/Units/KilometrePerHour",
        )

    def test_a_numerical_value_specification_is_read_from_its_value_child(self):
        model = read_arxml(str(INIT_VALUES))
        level = next(c for c in model.constant_specs if c.name == "SR_IScalars_level_Init")
        assert level.value == ConstantScalar(value=7)

    def test_an_array_value_specification_recurses_into_its_elements(self):
        model = read_arxml(str(INIT_VALUES))
        triple = next(c for c in model.constant_specs if c.name == "SR_IComposites_triple_Init")
        assert triple.value == ConstantArray(
            members=[
                ConstantScalar(value=0),
                ConstantScalar(value=1),
                ConstantScalar(value=16, radix_source="0x10"),
            ]
        )

    def test_a_record_value_specification_keeps_its_labels_without_using_them(self):
        model = read_arxml(str(INIT_VALUES))
        point = next(c for c in model.constant_specs if c.name == "SR_IComposites_point_Init")
        assert isinstance(point.value, ConstantRecord)
        assert [field.label for field in point.value.fields] == [
            "SR_IComposites_point_LongDecorativeXName_Init",
            "SR_IComposites_point_LongDecorativeYName_Init",
        ]
        assert [field.value for field in point.value.fields] == [
            ConstantScalar(value=5),
            ConstantScalar(value=10, radix_source="0x0a"),
        ]

    def test_an_unlabelled_record_member_reads_as_a_none_label(self):
        """A member without the optional SHORT-LABEL reads as a ``None`` label.

        The reader records the absence rather than inventing a name; matching is positional and happens in the mapper.
        """
        model = read_arxml(str(INIT_VALUES))
        pairs = next(c for c in model.constant_specs if c.name == "SR_IComposites_pairs_Init")
        assert isinstance(pairs.value, ConstantArray)
        assert [field.label for member in pairs.value.members for field in member.fields] == [None] * 4

    def test_the_reader_logs_no_skip_of_its_own_for_a_constant_it_read(self):
        """The constant is read and skip-logged nowhere here.

        There is no constants layer, and the report line is minted one layer up, in `arxml_import/report.py` (asserted
        in `test_arxml_import_cli.py`), so logging it here would double-report every constant."""
        model = read_arxml(str(INIT_VALUES))
        assert not any(entry.tag == "CONSTANT-SPECIFICATION" for entry in model.skip_log)


class TestReadsTheInitValueReferences:
    def test_a_data_element_carries_its_own_constant_path(self):
        model = read_arxml(str(INIT_VALUES))
        scalars = next(i for i in model.sr_interfaces if i.name == "IScalars")
        assert [(de.name, de.init_ref) for de in scalars.data_elements] == [
            ("counter", "/Acme/Chassis/Constants/SR_IScalars_counter_Init"),
            ("level", "/Acme/Chassis/Constants/SR_IScalars_level_Init"),
            ("gear", "/Acme/Chassis/Constants/SR_IScalars_gear_Init"),
            ("speed", "/Acme/Chassis/Constants/SR_IScalars_speed_Init"),
            ("mode", "/Acme/Chassis/Constants/SR_IScalars_mode_Init"),
            ("uninitialized", None),
        ]

    def test_a_port_com_spec_init_is_read_with_the_element_it_names(self):
        model = read_arxml(str(INIT_VALUES))
        swc = next(s for s in model.swcs if s.name == "InitValueSwc")
        inits = [(c.com_spec_tag, c.data_element_ref, c.constant_ref) for p in swc.ports for c in p.com_spec_inits]
        assert inits == [
            (
                "NONQUEUED-SENDER-COM-SPEC",
                "/Acme/Chassis/Interfaces/IScalars/counter",
                "/Acme/Chassis/Constants/SR_IScalars_counter_Init",
            ),
            (
                "NONQUEUED-RECEIVER-COM-SPEC",
                "/Acme/Chassis/Interfaces/IComposites/point",
                "/Acme/Chassis/Constants/SR_IComposites_point_Init",
            ),
        ]

    def test_a_port_com_spec_without_an_init_value_contributes_nothing(self):
        """A COM-SPEC without an init value contributes no entry.

        A COM-SPEC without one is common, and there is nothing to
        compare -- so they must not appear as entries with a None constant,
        which the mapper reads as an incomparable port init."""
        model = read_arxml(str(MINIMAL_SWC))
        assert [c for s in model.swcs for p in s.ports for c in p.com_spec_inits] == []

    @pytest.mark.parametrize(
        ("port_tag", "block_tag", "com_spec_tag", "init_tag", "subject_ref", "subject_dest"),
        [
            (
                "P-PORT-PROTOTYPE",
                "PROVIDED-COM-SPECS",
                "PARAMETER-PROVIDE-COM-SPEC",
                "INIT-VALUE",
                "PARAMETER-REF",
                "PARAMETER-DATA-PROTOTYPE",
            ),
            (
                "R-PORT-PROTOTYPE",
                "REQUIRED-COM-SPECS",
                "PARAMETER-REQUIRE-COM-SPEC",
                "INIT-VALUE",
                "PARAMETER-REF",
                "PARAMETER-DATA-PROTOTYPE",
            ),
            (
                "R-PORT-PROTOTYPE",
                "REQUIRED-COM-SPECS",
                "NV-REQUIRE-COM-SPEC",
                "INIT-VALUE",
                "VARIABLE-REF",
                "VARIABLE-DATA-PROTOTYPE",
            ),
            (
                "P-PORT-PROTOTYPE",
                "PROVIDED-COM-SPECS",
                "NV-PROVIDE-COM-SPEC",
                "RAM-BLOCK-INIT-VALUE",
                "VARIABLE-REF",
                "VARIABLE-DATA-PROTOTYPE",
            ),
        ],
    )
    def test_a_com_spec_that_initializes_no_data_element_is_not_read(
        self,
        port_tag: str,
        block_tag: str,
        com_spec_tag: str,
        init_tag: str,
        subject_ref: str,
        subject_dest: str,
    ):
        """Parameter and NV COM-SPECs, which name no DATA-ELEMENT-REF (AUTOSAR defines none on them), are not read.

        Every tuple is a shape the licensed schemas admit; the init tag is parametrized because
        NV-PROVIDE-COM-SPEC spells it RAM-/ROM-BLOCK-INIT-VALUE, so the reader filters by *kind*,
        not by an `INIT-VALUE`'s presence. Read, each would be reported as a "missing or blank
        DATA-ELEMENT-REF" about an element that is not defective.
        """
        fragment = (
            f"<{port_tag}{_NS_ATTR}>"
            "<SHORT-NAME>pCal</SHORT-NAME>"
            f"<{block_tag}><{com_spec_tag}>"
            f"<{init_tag}><CONSTANT-REFERENCE>"
            '<CONSTANT-REF DEST="CONSTANT-SPECIFICATION">/Pkg/Constants/K</CONSTANT-REF>'
            f"</CONSTANT-REFERENCE></{init_tag}>"
            f'<{subject_ref} DEST="{subject_dest}">/Pkg/If/PIf/pCal</{subject_ref}>'
            f"</{com_spec_tag}></{block_tag}>"
            f"</{port_tag}>"
        )
        assert _read_port_com_spec_inits(ET.fromstring(fragment), "/Pkg/Swc/pCal", port_position=0) == []

    def test_the_two_data_element_kinds_are_still_read_from_the_same_fragment_shape(self):
        """The non-vacuity control: a P-PORT built the same way with a NONQUEUED tag and a DATA-ELEMENT-REF *is* read.

        So the absences above are the filter and not a broken fragment. Its children are in AUTOSAR's order for this
        kind (DATA-ELEMENT-REF before INIT-VALUE), so neither fragment is one the schema refuses."""
        fragment = (
            f"<P-PORT-PROTOTYPE{_NS_ATTR}>"
            "<SHORT-NAME>pValue</SHORT-NAME>"
            "<PROVIDED-COM-SPECS><NONQUEUED-SENDER-COM-SPEC>"
            '<DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">/Pkg/If/SRIf/value</DATA-ELEMENT-REF>'
            "<INIT-VALUE><CONSTANT-REFERENCE>"
            '<CONSTANT-REF DEST="CONSTANT-SPECIFICATION">/Pkg/Constants/K</CONSTANT-REF>'
            "</CONSTANT-REFERENCE></INIT-VALUE>"
            "</NONQUEUED-SENDER-COM-SPEC></PROVIDED-COM-SPECS>"
            "</P-PORT-PROTOTYPE>"
        )
        inits = _read_port_com_spec_inits(ET.fromstring(fragment), "/Pkg/Swc/pValue", port_position=0)
        assert [(c.com_spec_tag, c.data_element_ref, c.constant_ref) for c in inits] == [
            ("NONQUEUED-SENDER-COM-SPEC", "/Pkg/If/SRIf/value", "/Pkg/Constants/K")
        ]


def _application(physical: str, *, category: str | None = "VALUE", cont_before: str = "", label: str = "") -> str:
    """An ``APPLICATION-VALUE-SPECIFICATION`` fragment whose ``SW-VALUES-PHYS`` holds *physical*.

    *cont_before* is written inside ``SW-VALUE-CONT`` before ``SW-VALUES-PHYS``, in
    the schema's order (``UNIT-REF``, ``UNIT-DISPLAY-NAME``, ``SW-ARRAYSIZE``).
    """
    category_el = f"<CATEGORY>{category}</CATEGORY>" if category is not None else ""
    label_el = f"<SHORT-LABEL>{label}</SHORT-LABEL>" if label else ""
    return (
        f"<APPLICATION-VALUE-SPECIFICATION{_NS_ATTR}>{label_el}{category_el}<SW-VALUE-CONT>{cont_before}"
        f"<SW-VALUES-PHYS>{physical}</SW-VALUES-PHYS></SW-VALUE-CONT></APPLICATION-VALUE-SPECIFICATION>"
    )


class TestReadsEveryValueForm:
    """A leaf records the form it had, its text where it is one, and the unit its ``SW-VALUE-CONT`` names.

    None of the three is part of the value's equality (the label's precedent): what is compared is the number or the
    text."""

    def test_the_application_form_with_a_number(self):
        value, reason = _value_spec(_application("<V>12</V>"))
        assert reason is None
        assert value == ConstantScalar(value=12)
        assert (value.form, value.unit_ref) == (ValueForm.APPLICATION, None)

    def test_the_numerical_form(self):
        value, reason = _value_spec(
            f"<NUMERICAL-VALUE-SPECIFICATION{_NS_ATTR}><VALUE>12</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
        )
        assert reason is None
        assert value == ConstantScalar(value=12)
        assert (value.form, value.unit_ref) == (ValueForm.NUMERICAL, None)

    def test_the_application_form_with_a_vt_is_a_text(self):
        value, reason = _value_spec(_application("<VT>DRIVE</VT>", label="mode"))
        assert reason is None
        assert value == ConstantScalar(value="DRIVE")
        assert (value.form, value.label, value.radix_source) == (ValueForm.APPLICATION, "mode", None)

    def test_a_vt_is_read_verbatim(self):
        """``VT`` is a verbatim string in both schemas.

        The text is the value as written, so a padded text is not the literal its trimmed form names."""
        value, reason = _value_spec(_application("<VT> DRIVE </VT>"))
        assert reason is None
        assert value == ConstantScalar(value=" DRIVE ")

    def test_a_vt_that_spells_a_number_stays_a_text(self):
        value, reason = _value_spec(_application("<VT>1</VT>"))
        assert reason is None
        assert value == ConstantScalar(value="1")
        assert value != ConstantScalar(value=1)

    def test_a_text_value_specification_is_a_text(self):
        value, reason = _value_spec(
            f"<TEXT-VALUE-SPECIFICATION{_NS_ATTR}><SHORT-LABEL>gear</SHORT-LABEL><VALUE>PARK</VALUE>"
            "</TEXT-VALUE-SPECIFICATION>"
        )
        assert reason is None
        assert value == ConstantScalar(value="PARK")
        assert (value.form, value.label, value.unit_ref) == (ValueForm.TEXT, "gear", None)

    def test_a_value_unit_reference_is_recorded(self):
        value, reason = _value_spec(
            _application("<V>30</V>", cont_before='<UNIT-REF DEST="UNIT">/Pkg/Units/KmPerHour</UNIT-REF>')
        )
        assert reason is None
        assert value == ConstantScalar(value=30)
        assert value.unit_ref == "/Pkg/Units/KmPerHour"

    def test_the_form_and_the_unit_are_not_part_of_the_value(self):
        """Two values that differ only in form or unit are one value.

        The judgement of a form and of a unit is the importer's, by the type."""
        numerical = ConstantScalar(value=3)
        application = ConstantScalar(value=3, form=ValueForm.APPLICATION, unit_ref="/Pkg/Units/U")
        assert numerical == application

    def test_an_absent_category_is_read(self):
        """An application value without ``CATEGORY`` is read.

        The schemas admit it (``minOccurs="0"``), and ``VALUE`` is the category of a single value."""
        value, reason = _value_spec(_application("<V>4</V>", category=None))
        assert reason is None
        assert value == ConstantScalar(value=4)

    @pytest.mark.parametrize(
        "cont_before",
        [
            "<SW-ARRAYSIZE><V>1</V></SW-ARRAYSIZE>",
            "<SW-ARRAYSIZE><V>3</V></SW-ARRAYSIZE>",
            "<UNIT-DISPLAY-NAME>km/h</UNIT-DISPLAY-NAME>",
        ],
    )
    def test_an_array_size_and_a_unit_display_name_are_ignored_as_before(self, cont_before: str):
        """The reader ignores both."""
        value, reason = _value_spec(_application("<V>4</V>", cont_before=cont_before))
        assert reason is None
        assert value == ConstantScalar(value=4)

    def test_axis_contents_are_ignored_as_before(self):
        value, reason = _value_spec(
            f"<APPLICATION-VALUE-SPECIFICATION{_NS_ATTR}><CATEGORY>VALUE</CATEGORY><SW-AXIS-CONTS/>"
            "<SW-VALUE-CONT><SW-VALUES-PHYS><V>4</V></SW-VALUES-PHYS></SW-VALUE-CONT>"
            "</APPLICATION-VALUE-SPECIFICATION>"
        )
        assert reason is None
        assert value == ConstantScalar(value=4)

    def test_a_record_member_and_an_array_member_keep_their_forms(self):
        value, reason = _value_spec(
            f"<RECORD-VALUE-SPECIFICATION{_NS_ATTR}><FIELDS>"
            "<APPLICATION-VALUE-SPECIFICATION><SHORT-LABEL>a</SHORT-LABEL><CATEGORY>VALUE</CATEGORY>"
            "<SW-VALUE-CONT><SW-VALUES-PHYS><VT>ON</VT></SW-VALUES-PHYS></SW-VALUE-CONT>"
            "</APPLICATION-VALUE-SPECIFICATION>"
            "<ARRAY-VALUE-SPECIFICATION><ELEMENTS>"
            "<NUMERICAL-VALUE-SPECIFICATION><VALUE>1</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
            "<TEXT-VALUE-SPECIFICATION><VALUE>OFF</VALUE></TEXT-VALUE-SPECIFICATION>"
            "</ELEMENTS></ARRAY-VALUE-SPECIFICATION>"
            "</FIELDS></RECORD-VALUE-SPECIFICATION>"
        )
        assert reason is None
        assert isinstance(value, ConstantRecord)
        text, array = value.fields
        assert (text.label, text.value, text.value.form) == ("a", ConstantScalar(value="ON"), ValueForm.APPLICATION)
        assert array.value == ConstantArray(members=[ConstantScalar(value=1), ConstantScalar(value="OFF")])
        assert [member.form for member in array.value.members] == [ValueForm.NUMERICAL, ValueForm.TEXT]


class TestRefusesAValueShapeItCannotRead:
    """Every refusal names what was actually there; none of these shapes is guessed at."""

    def test_a_tag_that_is_not_a_value_specification_is_named(self):
        value, reason = _value_spec(
            f"<APPLICATION-RULE-BASED-VALUE-SPECIFICATION{_NS_ATTR}><CATEGORY>VALUE</CATEGORY>"
            "</APPLICATION-RULE-BASED-VALUE-SPECIFICATION>"
        )
        assert value is None
        assert reason == "APPLICATION-RULE-BASED-VALUE-SPECIFICATION is not a value specification this reader reads"

    def test_two_sw_values_phys_entries_are_refused_with_their_count(self):
        value, reason = _value_spec(_application("<V>1</V><V>2</V>", category=None))
        assert value is None
        assert reason == (
            "APPLICATION-VALUE-SPECIFICATION carries 2 SW-VALUES-PHYS entries (V, V); only a single V or VT is read"
        )

    @pytest.mark.parametrize(
        ("physical", "described"),
        [
            ("<V>1</V><VT>ON</VT>", "2 SW-VALUES-PHYS entries (V, VT)"),
            ("<VT>ON</VT><VT>OFF</VT>", "2 SW-VALUES-PHYS entries (VT, VT)"),
            ("<VF>1</VF>", "1 SW-VALUES-PHYS entries (VF)"),
            ("<VTF><V>1</V></VTF>", "1 SW-VALUES-PHYS entries (VTF)"),
            ("<VG><V>1</V></VG>", "1 SW-VALUES-PHYS entries (VG)"),
            ("", "0 SW-VALUES-PHYS entries"),
        ],
    )
    def test_anything_but_exactly_one_v_or_vt_is_refused(self, physical: str, described: str):
        """Anything but exactly one ``V`` or ``VT`` is refused.

        A ``V`` beside a ``VT`` was read as the ``V`` before, the ``VT``
        dropped in silence; a lone ``VT`` was refused as carrying no ``V``."""
        value, reason = _value_spec(_application(physical))
        assert value is None
        assert reason == f"APPLICATION-VALUE-SPECIFICATION carries {described}; only a single V or VT is read"

    def test_an_application_value_specification_with_no_cont_is_refused(self):
        value, reason = _value_spec(
            f"<APPLICATION-VALUE-SPECIFICATION{_NS_ATTR}><CATEGORY>VALUE</CATEGORY></APPLICATION-VALUE-SPECIFICATION>"
        )
        assert value is None
        assert reason == (
            "APPLICATION-VALUE-SPECIFICATION carries 0 SW-VALUES-PHYS entries; only a single V or VT is read"
        )

    @pytest.mark.parametrize("category", ["STRING", "CURVE", "VAL_BLK", "value", "", "VALUE\u00a0"])
    def test_a_category_other_than_value_is_refused(self, category: str):
        """The compound categories and ``STRING`` are not read.

        The refusal is the reader's own, which the importer records as not supported."""
        value, reason = _value_spec(_application("<V>1</V>", category=category))
        assert value is None
        assert reason == (
            f"APPLICATION-VALUE-SPECIFICATION of CATEGORY '{category}' is not read; only the single value of "
            "CATEGORY VALUE is"
        )

    def test_xml_whitespace_around_the_category_is_still_read(self):
        """XML's own white space around the ``CATEGORY`` token is stripped, and only it.

        A no-break space, above, is no white space of XML's and leaves no ``VALUE``."""
        value, reason = _value_spec(_application("<V>1</V>", category=" \t\r\nVALUE\n"))
        assert reason is None
        assert value is not None

    @pytest.mark.parametrize("text", ["", "   "])
    def test_an_empty_text_is_refused(self, text: str):
        for fragment in (
            _application(f"<VT>{text}</VT>"),
            f"<TEXT-VALUE-SPECIFICATION{_NS_ATTR}><VALUE>{text}</VALUE></TEXT-VALUE-SPECIFICATION>",
        ):
            value, reason = _value_spec(fragment)
            assert value is None, fragment
            assert reason == "a value specification carries no value text", fragment

    def test_a_text_value_specification_with_no_value_is_refused(self):
        value, reason = _value_spec(
            f"<TEXT-VALUE-SPECIFICATION{_NS_ATTR}><SHORT-LABEL>x</SHORT-LABEL></TEXT-VALUE-SPECIFICATION>"
        )
        assert value is None
        assert reason == "TEXT-VALUE-SPECIFICATION carries no VALUE"

    def test_a_numerical_value_specification_with_no_value_is_refused(self):
        value, reason = _value_spec(
            f"<NUMERICAL-VALUE-SPECIFICATION{_NS_ATTR}><SHORT-LABEL>x</SHORT-LABEL></NUMERICAL-VALUE-SPECIFICATION>"
        )
        assert value is None
        assert reason == "NUMERICAL-VALUE-SPECIFICATION carries no VALUE"

    def test_a_non_numeric_value_is_refused_rather_than_kept_as_a_string(self):
        value, reason = _value_spec(
            f"<NUMERICAL-VALUE-SPECIFICATION{_NS_ATTR}><VALUE>GEAR_NEUTRAL</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
        )
        assert value is None
        assert "GEAR_NEUTRAL" in reason

    @pytest.mark.parametrize("spelling", ["nan", "inf", "-inf", "Infinity"])
    def test_a_non_numeric_infinity_spelling_is_refused(self, spelling: str):
        """A non-numeric infinity spelling is refused as a reportable skip.

        ``schema.ModelElement``'s ``allow_inf_nan=False`` would raise on it mid-import, turning the skip into an
        exception; these spellings never reach the ``math.isfinite`` guard."""
        value, reason = _value_spec(
            f"<NUMERICAL-VALUE-SPECIFICATION{_NS_ATTR}><VALUE>{spelling}</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
        )
        assert value is None, spelling
        assert reason == f"value '{spelling}' is not a number this reader can read"

    @pytest.mark.parametrize("spelling", ["1e400", "-1e400"])
    def test_a_finite_looking_spelling_that_overflows_is_refused_by_the_guard(self, spelling: str):
        """The ``math.isfinite`` branch: ``1e400`` reads as ``inf`` and is refused.

        It would raise in ``DataElement(init=inf)``; the reason is compared whole so that deleting the guard fails here.
        """
        value, reason = _value_spec(
            f"<NUMERICAL-VALUE-SPECIFICATION{_NS_ATTR}><VALUE>{spelling}</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
        )
        assert value is None
        assert reason == f"value '{spelling}' is not a finite number"

    def test_a_spelling_that_underflows_to_zero_is_not_carried_and_says_so(self):
        """``1e-400`` underflows to ``0.0``, finite and **not** the number the source states, so it is not carried.

        It is named: the guard refuses anything ``float()`` rounds."""
        value, reason = _value_spec(
            f"<NUMERICAL-VALUE-SPECIFICATION{_NS_ATTR}><VALUE>1e-400</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
        )
        assert value is None
        assert reason == (
            "value '1e-400' is a number SWCraft cannot hold exactly: it holds an IEEE 754 double, and the "
            "nearest one is 0.0, so the value is not carried rather than rounded"
        )

    @pytest.mark.parametrize(
        ("spelling", "nearest"),
        [
            ("16777217.000000001", "16777217.0"),
            ("0.10000000000000001", "0.1"),
            ("3.14159265358979323846", "3.141592653589793"),
            ("-1e-400", "-0.0"),
            ("9007199254740993.0", "9007199254740992.0"),
        ],
    )
    def test_a_float_spelling_no_double_holds_exactly_is_not_carried_and_says_so(self, spelling: str, nearest: str):
        for fragment in (
            f"<NUMERICAL-VALUE-SPECIFICATION{_NS_ATTR}><VALUE>{spelling}</VALUE></NUMERICAL-VALUE-SPECIFICATION>",
            f"<APPLICATION-VALUE-SPECIFICATION{_NS_ATTR}><SW-VALUE-CONT><SW-VALUES-PHYS><V>{spelling}</V>"
            "</SW-VALUES-PHYS></SW-VALUE-CONT></APPLICATION-VALUE-SPECIFICATION>",
        ):
            value, reason = _value_spec(fragment)
            assert value is None, fragment
            assert reason == (
                f"value '{spelling}' is a number SWCraft cannot hold exactly: it holds an IEEE 754 double, and the "
                f"nearest one is {nearest}, so the value is not carried rather than rounded"
            )

    @pytest.mark.parametrize(
        ("spelling", "expected"),
        [
            ("0.1", 0.1),
            ("0.10", 0.1),
            ("1.5e3", 1500.0),
            ("1E3", 1000.0),
            ("-0.0", 0.0),
            ("0.30000000000000004", 0.30000000000000004),
            ("5e-324", 5e-324),
            ("1.7976931348623157e308", 1.7976931348623157e308),
        ],
    )
    def test_a_float_spelling_a_double_holds_is_still_read(self, spelling: str, expected: float):
        """The boundary control: the rule refuses a rounding, never a float."""
        value, reason = _value_spec(
            f"<NUMERICAL-VALUE-SPECIFICATION{_NS_ATTR}><VALUE>{spelling}</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
        )
        assert reason is None
        assert value == ConstantScalar(value=expected)
        assert type(value.value) is float

    @pytest.mark.parametrize("spelling", ["9007199254740993", "18446744073709551615", "-9223372036854775809"])
    def test_an_integer_spelling_above_two_to_the_fifty_third_is_the_integer(self, spelling: str):
        value, reason = _value_spec(
            f"<NUMERICAL-VALUE-SPECIFICATION{_NS_ATTR}><VALUE>{spelling}</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
        )
        assert reason is None
        assert value == ConstantScalar(value=int(spelling))
        assert type(value.value) is int

    def test_a_decimal_run_above_the_interpreters_digit_limit_is_a_reason_not_an_exception(self):
        """CPython's ``int(s)`` digit-limit ``ValueError`` becomes one skipped constant with a reason, not an abort.

        The limit is pinned for the run: input and message derived from one live call would pass under every limit,
        including the disabled ``0``."""
        with int_max_str_digits(DIGIT_LIMIT_FLOOR):
            digits = "9" * (DIGIT_LIMIT_FLOOR + 1)
            value, reason = _value_spec(
                f"<NUMERICAL-VALUE-SPECIFICATION{_NS_ATTR}><VALUE>{digits}</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
            )
        assert value is None
        assert reason == (
            f"value has {DIGIT_LIMIT_FLOOR + 1} digits, above this interpreter's "
            f"{DIGIT_LIMIT_FLOOR}-digit limit for reading an integer"
        )

    def test_a_hexadecimal_run_whose_decimal_form_is_unwritable_is_a_reason(self):
        """The hexadecimal spelling needs its own bound, on the decimal width.

        ``int(x, 16)`` is exempt from the digit limit, but the value is written back as a decimal YAML
        scalar: unbounded, ``yaml.safe_dump`` fails after the data-type files are written, leaving a
        half-written project.

        **Does not distinguish** which width the guard bounds; the pair
        :meth:`test_the_widest_hexadecimal_run_whose_decimal_form_fits_is_read` and its ``+ 1``
        sibling does.
        """
        digits = "f" * (DIGIT_LIMIT_FLOOR + 1)
        assert len(str(int(digits, 16))) > DIGIT_LIMIT_FLOOR
        with int_max_str_digits(DIGIT_LIMIT_FLOOR):
            value, reason = _value_spec(
                f"<NUMERICAL-VALUE-SPECIFICATION{_NS_ATTR}><VALUE>0x{digits}</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
            )
        assert value is None
        assert reason == (
            f"value has {DIGIT_LIMIT_FLOOR + 1} hexadecimal digits, whose decimal form is above "
            f"this interpreter's {DIGIT_LIMIT_FLOOR}-digit limit for writing an integer"
        )

    @pytest.mark.parametrize(
        ("fragment", "digits", "word"),
        [
            pytest.param(
                f"<NUMERICAL-VALUE-SPECIFICATION{_NS_ATTR}><VALUE>0b{'1' * (DIGIT_LIMIT_FLOOR * 4)}</VALUE>"
                "</NUMERICAL-VALUE-SPECIFICATION>",
                DIGIT_LIMIT_FLOOR * 4,
                "binary",
                id="binary-formula",
            ),
            pytest.param(
                _application(f"<V>0{'7' * (DIGIT_LIMIT_FLOOR * 2)}</V>"),
                DIGIT_LIMIT_FLOOR * 2,
                "octal",
                id="octal-pattern",
            ),
        ],
    )
    def test_an_octal_or_binary_run_too_wide_to_write_is_refused_by_its_radix(
        self, fragment: str, digits: int, word: str
    ):
        """The same guard on base 8 and base 2, exempt from the digit limit like base 16, each named in the reason."""
        with int_max_str_digits(DIGIT_LIMIT_FLOOR):
            value, reason = _value_spec(fragment)
        assert value is None
        assert reason == (
            f"value has {digits} {word} digits, whose decimal form is above "
            f"this interpreter's {DIGIT_LIMIT_FLOOR}-digit limit for writing an integer"
        )

    def test_a_hexadecimal_run_whose_decimal_form_fits_is_read(self):
        """The non-vacuity control: a hexadecimal run under the DECIMAL-width bound still reads.

        100 hex digits are 121 decimal ones, measured here."""
        digits = "f" * 100
        assert len(str(int(digits, 16))) < DIGIT_LIMIT_FLOOR
        with int_max_str_digits(DIGIT_LIMIT_FLOOR):
            value, reason = _value_spec(
                f"<NUMERICAL-VALUE-SPECIFICATION{_NS_ATTR}><VALUE>0x{digits}</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
            )
        assert reason is None
        assert value.value == int(digits, 16)

    def test_the_widest_hexadecimal_run_whose_decimal_form_fits_is_read(self):
        """The lower half of the adjacent pair (see :data:`_WIDEST_HEX_RUN_THAT_FITS`) is read by either guard.

        531 source digits are exactly 640 decimal ones; its sibling below is where the guards part."""
        digits = "f" * _WIDEST_HEX_RUN_THAT_FITS
        assert len(digits) < DIGIT_LIMIT_FLOOR
        assert len(str(int(digits, 16))) == DIGIT_LIMIT_FLOOR

        with int_max_str_digits(DIGIT_LIMIT_FLOOR):
            value, reason = _value_spec(
                f"<NUMERICAL-VALUE-SPECIFICATION{_NS_ATTR}><VALUE>0x{digits}</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
            )

        assert reason is None
        assert value.value == int(digits, 16)
        assert value.radix_source == f"0x{digits}"

    def test_one_hexadecimal_digit_wider_is_refused_although_the_source_still_fits(self):
        """The upper half of the pair is refused: 532 source digits fit the source count but are 641 decimal ones.

        So the shipped guard refuses what a source-width guard would read and abort on."""
        digits = "f" * (_WIDEST_HEX_RUN_THAT_FITS + 1)
        assert len(digits) < DIGIT_LIMIT_FLOOR
        assert len(str(int(digits, 16))) == DIGIT_LIMIT_FLOOR + 1

        with int_max_str_digits(DIGIT_LIMIT_FLOOR):
            value, reason = _value_spec(
                f"<NUMERICAL-VALUE-SPECIFICATION{_NS_ATTR}><VALUE>0x{digits}</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
            )

        assert value is None
        assert reason == (
            f"value has {len(digits)} hexadecimal digits, whose decimal form is above "
            f"this interpreter's {DIGIT_LIMIT_FLOOR}-digit limit for writing an integer"
        )

    def test_a_disabled_digit_limit_reads_a_hexadecimal_run_of_any_width(self):
        """The hexadecimal half of :meth:`test_a_disabled_digit_limit_still_reads_an_ordinary_value`.

        ``str()`` does not raise with the limit disabled, so nothing is refused."""
        digits = "f" * (DIGIT_LIMIT_FLOOR + 1)
        with int_max_str_digits(0):
            value, reason = _value_spec(
                f"<NUMERICAL-VALUE-SPECIFICATION{_NS_ATTR}><VALUE>0x{digits}</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
            )
        assert reason is None
        assert value.value == int(digits, 16)

    @pytest.mark.parametrize("spelling", ["255", "9" * (DIGIT_LIMIT_FLOOR + 1)])
    def test_a_disabled_digit_limit_still_reads_an_ordinary_value(self, spelling: str):
        """``sys.get_int_max_str_digits()`` answers **0** when the limit is disabled.

        A bare ``digits > limit`` would refuse every decimal init there: a model landing without its
        values while the import exits 0.
        """
        with int_max_str_digits(0):
            value, reason = _value_spec(
                f"<NUMERICAL-VALUE-SPECIFICATION{_NS_ATTR}><VALUE>{spelling}</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
            )
        assert reason is None
        assert value == ConstantScalar(value=int(spelling))

    def test_an_underscore_separated_number_is_refused(self):
        """An underscore-separated number is refused.

        ``int("1_000")`` is 1000 in Python and is not an ARXML spelling --
        the anchored patterns exist so the reader does not inherit Python's
        literal grammar."""
        value, reason = _value_spec(
            f"<NUMERICAL-VALUE-SPECIFICATION{_NS_ATTR}><VALUE>1_000</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
        )
        assert value is None
        assert "1_000" in reason

    def test_a_float_value_is_read_as_a_float(self):
        """The success half of the same branch: a nested float is read.

        A float such as ``0.0``, nested, is a value AUTOSAR allows, so the branch is reachable."""
        value, reason = _value_spec(
            f"<NUMERICAL-VALUE-SPECIFICATION{_NS_ATTR}><VALUE>0.0</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
        )
        assert reason is None
        assert value == ConstantScalar(value=0.0)
        assert isinstance(value.value, float)

    def test_a_refusal_inside_a_composite_propagates_with_its_own_reason(self):
        value, reason = _value_spec(
            f"<ARRAY-VALUE-SPECIFICATION{_NS_ATTR}><ELEMENTS>"
            "<NUMERICAL-VALUE-SPECIFICATION><VALUE>1</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
            "<TEXT-VALUE-SPECIFICATION><VALUE>x</VALUE></TEXT-VALUE-SPECIFICATION>"
            "<APPLICATION-VALUE-SPECIFICATION><CATEGORY>VALUE</CATEGORY><SW-VALUE-CONT><SW-VALUES-PHYS>"
            "<VF>1</VF></SW-VALUES-PHYS></SW-VALUE-CONT></APPLICATION-VALUE-SPECIFICATION>"
            "</ELEMENTS></ARRAY-VALUE-SPECIFICATION>"
        )
        assert value is None
        assert reason == (
            "APPLICATION-VALUE-SPECIFICATION carries 1 SW-VALUES-PHYS entries (VF); only a single V or VT is read"
        )


class TestRefusesAnInitValueReferenceItCannotFollow:
    """`_read_init_value_ref`'s three answers, and the skip the caller makes of the reason.

    The refusals are the shapes a file writing values inline would most plausibly use, each skipped
    with a reason naming what was there, never guessed at.
    """

    @staticmethod
    def _init_value_ref(fragment: str):
        return _read_init_value_ref(ET.fromstring(fragment))

    def test_no_init_value_at_all_is_neither_a_reference_nor_a_finding(self):
        """The ordinary case: an element without an init value.

        Both halves None, so the caller logs nothing."""
        constant_ref, reason = self._init_value_ref(
            f"<VARIABLE-DATA-PROTOTYPE{_NS_ATTR}><SHORT-NAME>x</SHORT-NAME></VARIABLE-DATA-PROTOTYPE>"
        )
        assert (constant_ref, reason) == (None, None)

    def test_a_reference_is_read_as_its_constant_ref_path(self):
        constant_ref, reason = self._init_value_ref(
            f"<VARIABLE-DATA-PROTOTYPE{_NS_ATTR}><INIT-VALUE><CONSTANT-REFERENCE>"
            "<CONSTANT-REF>/Acme/Constants/C</CONSTANT-REF>"
            "</CONSTANT-REFERENCE></INIT-VALUE></VARIABLE-DATA-PROTOTYPE>"
        )
        assert (constant_ref, reason) == ("/Acme/Constants/C", None)

    def test_an_inline_value_specification_names_the_tag_it_found(self):
        constant_ref, reason = self._init_value_ref(
            f"<VARIABLE-DATA-PROTOTYPE{_NS_ATTR}><INIT-VALUE>"
            "<NUMERICAL-VALUE-SPECIFICATION><VALUE>1</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
            "</INIT-VALUE></VARIABLE-DATA-PROTOTYPE>"
        )
        assert constant_ref is None
        assert reason == "INIT-VALUE is not a CONSTANT-REFERENCE (it carries NUMERICAL-VALUE-SPECIFICATION)"

    def test_an_empty_init_value_says_it_carries_nothing(self):
        constant_ref, reason = self._init_value_ref(
            f"<VARIABLE-DATA-PROTOTYPE{_NS_ATTR}><INIT-VALUE/></VARIABLE-DATA-PROTOTYPE>"
        )
        assert constant_ref is None
        assert reason == "INIT-VALUE is not a CONSTANT-REFERENCE (it carries nothing)"

    def test_a_reference_with_no_constant_ref_is_named_as_that(self):
        constant_ref, reason = self._init_value_ref(
            f"<VARIABLE-DATA-PROTOTYPE{_NS_ATTR}><INIT-VALUE>"
            "<CONSTANT-REFERENCE/></INIT-VALUE></VARIABLE-DATA-PROTOTYPE>"
        )
        assert constant_ref is None
        assert reason == "INIT-VALUE's CONSTANT-REFERENCE carries no CONSTANT-REF"


class TestTheSkipTheUnreadableInitValueFeeds:
    """What `_read_sr_interface` does with that reason.

    One skip line naming the element, and the element itself still imported without its init."""

    _SOURCE = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<AUTOSAR{_NS_ATTR} xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        "<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>Acme</SHORT-NAME><ELEMENTS>"
        "<SENDER-RECEIVER-INTERFACE><SHORT-NAME>IInline</SHORT-NAME><DATA-ELEMENTS>"
        "<VARIABLE-DATA-PROTOTYPE><SHORT-NAME>speed</SHORT-NAME>"
        "<TYPE-TREF>/Acme/uint8</TYPE-TREF>"
        "<INIT-VALUE><NUMERICAL-VALUE-SPECIFICATION><VALUE>7</VALUE>"
        "</NUMERICAL-VALUE-SPECIFICATION></INIT-VALUE>"
        "</VARIABLE-DATA-PROTOTYPE>"
        "</DATA-ELEMENTS></SENDER-RECEIVER-INTERFACE>"
        "</ELEMENTS></AR-PACKAGE></AR-PACKAGES></AUTOSAR>\n"
    )

    def _model(self, tmp_path: Path):
        source = tmp_path / "inline_init.arxml"
        source.write_text(self._SOURCE, encoding="utf-8", newline="\n")
        return read_arxml(str(source))

    def test_the_skip_line_names_the_element_the_tag_and_the_reason(self, tmp_path: Path):
        entries = [e for e in self._model(tmp_path).skip_log if e.tag == "INIT-VALUE"]
        assert len(entries) == 1
        assert entries[0].path == "/Acme/IInline/speed"
        assert entries[0].reason == (
            "INIT-VALUE is not a CONSTANT-REFERENCE (it carries NUMERICAL-VALUE-SPECIFICATION)"
        )

    def test_the_element_still_lands_without_its_init_reference(self, tmp_path: Path):
        """The element lands with its init recorded as UNREADABLE, never as absent or guessed.

        The field is optional; a guessed init is not. The merge comparison thus keeps an element whose declaration was
        not read apart from one that declares nothing."""
        element = self._model(tmp_path).sr_interfaces[0].data_elements[0]
        assert (element.name, element.init_ref, element.init_unreadable) == ("speed", None, True)

    def test_an_element_with_no_init_value_is_not_marked_unreadable(self):
        """The non-vacuity control: the ordinary no-init element stays False.

        The flag separates the two Nones, so the ordinary no-init element must stay False."""
        model = read_arxml(str(INIT_VALUES))
        scalars = next(i for i in model.sr_interfaces if i.name == "IScalars")
        uninitialized = next(de for de in scalars.data_elements if de.name == "uninitialized")
        assert (uninitialized.init_ref, uninitialized.init_unreadable) == (None, False)


# The reader over the WRITER's own output


@pytest.fixture(scope="module")
def swcraft_written_init_values(tmp_path_factory) -> ImportModel:
    """``fixtures/init_values/model`` generated once, and read back."""
    output = tmp_path_factory.mktemp("init-values") / "model.arxml"
    generate_arxml(str(INIT_VALUES_MODEL_ROOT), str(output))
    return read_arxml(str(output))


class TestReadsTheWritersOwnConstants:
    def test_every_constant_is_found_in_the_port_interfaces_package(self, swcraft_written_init_values) -> None:
        """The reader indexes a constant by its path and has no opinion about which package that path names."""
        paths = {constant.path for constant in swcraft_written_init_values.constant_specs}

        assert paths, "the writer emitted no CONSTANT-SPECIFICATION at all"
        assert all(path.startswith("/PortInterfaces/") for path in paths), sorted(paths)
        assert "/PortInterfaces/ISignals_speed_Init" in paths
        assert all(constant.unreadable_reason is None for constant in swcraft_written_init_values.constant_specs)

    def test_the_element_without_an_init_has_no_constant_of_its_own(self, swcraft_written_init_values) -> None:
        """The non-vacuity control on the constants.

        A writer emitting one constant per data element would satisfy every assertion above."""
        names = {constant.name for constant in swcraft_written_init_values.constant_specs}

        assert "ISignals_plain_Init" not in names

    def test_a_top_level_numerical_value_specification_is_read_as_a_scalar(self, swcraft_written_init_values) -> None:
        """A top-level ``NUMERICAL-VALUE-SPECIFICATION`` is read as a scalar.

        The reader fixture writes ``APPLICATION-VALUE-SPECIFICATION`` at the top level and ``NUMERICAL-`` only when
        nested; SWCraft writes ``NUMERICAL-`` everywhere, the position the committed reader fixture does not exercise.
        """
        speed = next(c for c in swcraft_written_init_values.constant_specs if c.name == "ISignals_speed_Init")

        assert speed.value == ConstantScalar(value=7, radix_source=None)

    def test_a_float_leaf_is_read_back_as_a_float(self, swcraft_written_init_values) -> None:
        """``100.0`` reads back as a float.

        ``100.0`` is the row the appended ``.0`` exists for: without it the
        emitted text would be ``100`` and the reader's decimal-first grammar
        would hand back an ``int``."""
        whole = next(c for c in swcraft_written_init_values.constant_specs if c.name == "ISignals_whole_Init")

        assert whole.value == ConstantScalar(value=100.0, radix_source=None)
        assert isinstance(whole.value.value, float)

    def test_a_record_is_read_with_the_declared_field_names_as_labels(self, swcraft_written_init_values) -> None:
        """SWCraft labels every record member with its declared field name, and the reader carries the labels.

        It records them without matching on them (matching is positional, in the mapper), so this asserts what is
        *carried*."""
        limits = next(c for c in swcraft_written_init_values.constant_specs if c.name == "ISignals_limits_Init")

        assert isinstance(limits.value, ConstantRecord)
        assert [field.label for field in limits.value.fields] == ["low", "high", "tiny", "whole"]
        assert [field.value for field in limits.value.fields] == [
            ConstantScalar(value=-1e20),
            ConstantScalar(value=1e20),
            ConstantScalar(value=1e-7),
            ConstantScalar(value=100.0),
        ]

    def test_an_array_member_carries_no_label(self, swcraft_written_init_values) -> None:
        """A record member is labelled and an array member is not; the reader reports the absence, inventing none."""
        pair = next(c for c in swcraft_written_init_values.constant_specs if c.name == "ISignals_bytes_Init")

        assert pair.value == ConstantArray(members=[ConstantScalar(value=1), ConstantScalar(value=2)])

    def test_each_valued_data_element_carries_its_own_constants_path(self, swcraft_written_init_values) -> None:
        signals = next(i for i in swcraft_written_init_values.sr_interfaces if i.name == "ISignals")

        by_name = {element.name: element.init_ref for element in signals.data_elements}

        assert by_name["speed"] == "/PortInterfaces/ISignals_speed_Init"
        assert by_name["plain"] is None

    def test_both_com_spec_kinds_carry_the_same_reference_as_the_prototype(self, swcraft_written_init_values) -> None:
        """Both COM-SPEC kinds repeat the element's reference for a port whose model states no value of its own.

        A port's own ``init`` is written in place instead. Both kinds are present: the fixture has one component with a
        provide *and* a require port."""
        swc = next(s for s in swcraft_written_init_values.swcs if s.name == "SignalsSwc")
        speed_inits = [
            (com_spec.com_spec_tag, com_spec.constant_ref)
            for port in swc.ports
            for com_spec in port.com_spec_inits
            if com_spec.data_element_ref == "/PortInterfaces/ISignals/speed"
        ]

        assert sorted(speed_inits) == [
            ("NONQUEUED-RECEIVER-COM-SPEC", "/PortInterfaces/ISignals_speed_Init"),
            ("NONQUEUED-SENDER-COM-SPEC", "/PortInterfaces/ISignals_speed_Init"),
        ]

    def test_the_element_without_an_init_contributes_no_com_spec_entry(self, swcraft_written_init_values) -> None:
        swc = next(s for s in swcraft_written_init_values.swcs if s.name == "SignalsSwc")

        assert not [
            com_spec
            for port in swc.ports
            for com_spec in port.com_spec_inits
            if com_spec.data_element_ref.endswith("/plain")
        ]

    def test_the_reader_skip_logs_nothing_for_a_document_it_wrote_itself(self, swcraft_written_init_values) -> None:
        """``CONSTANT-SPECIFICATION`` is a known tag, so the writer's constants are read, not skipped.

        Their report-only line is minted in ``arxml_import/report.py``."""
        assert [f"{entry.tag} at {entry.path}: {entry.reason}" for entry in swcraft_written_init_values.skip_log] == []


# A base type's three comparison-only properties


class TestABaseTypesComparisonOnlyProperties:
    """``BYTE-ORDER``, ``MEM-ALIGNMENT`` and ``CATEGORY`` are read, and only for the merge comparison.

    Two declarations of one name are one element only when every property ``[constr_1220]`` names
    is identical, and a merge that cannot see a property assumes it equal. The reader carries the
    three **unparsed and stripped of the surrounding whitespace alone**; ``base_type`` has no field
    for any of them.

    **Does not reach:** whether the comparison consults the three (its own tests do), and an export
    whose ``MEM-ALIGNMENT`` differs only in spelling (``8`` against ``08``), which refuses the merge --
    fail-closed, at the price of **both** members and every reference into them; an exporter's
    indentation, unlike spelling, is stripped by ``_declared_text``.
    """

    _SOURCE = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<AUTOSAR xmlns="http://autosar.org/schema/r4.0">\n'
        "  <AR-PACKAGES>\n"
        "    <AR-PACKAGE>\n"
        "      <SHORT-NAME>Pkg</SHORT-NAME>\n"
        "      <ELEMENTS>\n"
        "        <SW-BASE-TYPE>\n"
        "          <SHORT-NAME>Declared</SHORT-NAME>\n"
        "          <CATEGORY>FIXED_LENGTH</CATEGORY>\n"
        "          <BASE-TYPE-SIZE>16</BASE-TYPE-SIZE>\n"
        "          <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>\n"
        "          <MEM-ALIGNMENT>8</MEM-ALIGNMENT>\n"
        "          <BYTE-ORDER>MOST-SIGNIFICANT-BYTE-FIRST</BYTE-ORDER>\n"
        "          <NATIVE-DECLARATION>uint16</NATIVE-DECLARATION>\n"
        "        </SW-BASE-TYPE>\n"
        "        <SW-BASE-TYPE>\n"
        "          <SHORT-NAME>Silent</SHORT-NAME>\n"
        "          <BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>\n"
        "          <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>\n"
        "          <NATIVE-DECLARATION>uint8</NATIVE-DECLARATION>\n"
        "        </SW-BASE-TYPE>\n"
        "      </ELEMENTS>\n"
        "    </AR-PACKAGE>\n"
        "  </AR-PACKAGES>\n"
        "</AUTOSAR>\n"
    )

    @pytest.fixture
    def base_types(self, tmp_path: Path) -> dict[str, object]:
        source = tmp_path / "base_types.arxml"
        source.write_text(self._SOURCE, encoding="utf-8", newline="\n")
        return {info.name: info for info in read_arxml(str(source)).sw_base_types}

    def test_all_three_are_carried_verbatim_when_the_export_declares_them(self, base_types) -> None:
        declared = base_types["Declared"]
        assert declared.byte_order == "MOST-SIGNIFICANT-BYTE-FIRST"
        assert declared.mem_alignment == "8"
        assert declared.category == "FIXED_LENGTH"

    def test_all_three_are_none_when_the_export_declares_none_of_them(self, base_types) -> None:
        """``None`` and not ``""`` when the export declares none of the three.

        An absent property and one declared empty are different
        facts, and only the first is *"the export does not say"*. The three properties the
        reader already read use ``or ""`` because the mapping writes them into a model field
        that must be a string; these three reach no field."""
        silent = base_types["Silent"]
        assert silent.byte_order is None
        assert silent.mem_alignment is None
        assert silent.category is None

    def test_the_three_the_reader_already_read_are_unmoved(self, base_types) -> None:
        """The extension adds three fields and changes none.

        A regression here would make every assertion above true of a reader that had stopped reading the rest."""
        declared, silent = base_types["Declared"], base_types["Silent"]
        assert (declared.size, declared.encoding, declared.native_declaration) == (16, "NONE", "uint16")
        assert (silent.size, silent.encoding, silent.native_declaration) == (8, "NONE", "uint8")

    _PRETTY_PRINTED = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<AUTOSAR xmlns="http://autosar.org/schema/r4.0">\n'
        "  <AR-PACKAGES>\n"
        "    <AR-PACKAGE>\n"
        "      <SHORT-NAME>Pkg</SHORT-NAME>\n"
        "      <ELEMENTS>\n"
        "        <SW-BASE-TYPE>\n"
        "          <SHORT-NAME>Spread</SHORT-NAME>\n"
        "          <CATEGORY>\n            FIXED_LENGTH\n          </CATEGORY>\n"
        "          <BASE-TYPE-SIZE>16</BASE-TYPE-SIZE>\n"
        "          <BASE-TYPE-ENCODING>\n            NONE\n          </BASE-TYPE-ENCODING>\n"
        "          <MEM-ALIGNMENT>\n          </MEM-ALIGNMENT>\n"
        "          <BYTE-ORDER>\n            MOST-SIGNIFICANT-BYTE-FIRST\n          </BYTE-ORDER>\n"
        "          <NATIVE-DECLARATION>\n            uint16\n          </NATIVE-DECLARATION>\n"
        "        </SW-BASE-TYPE>\n"
        "      </ELEMENTS>\n"
        "    </AR-PACKAGE>\n"
        "  </AR-PACKAGES>\n"
        "</AUTOSAR>\n"
    )

    def test_a_whitespace_collapsing_property_is_read_without_the_exporters_indentation(self, tmp_path: Path) -> None:
        """*Unparsed* is not *raw*: a whitespace-collapsing property reads without the exporter's indentation.

        Each has a whitespace-collapsing schema type, so a value on its own line declares what an inline one does and
        the merge comparison may not call that a difference (a conflicting group imports neither); it reads as the
        ``""`` of its inline empty twin."""
        source = tmp_path / "pretty_printed.arxml"
        source.write_text(self._PRETTY_PRINTED, encoding="utf-8", newline="\n")

        spread = read_arxml(str(source)).sw_base_types[0]

        assert spread.category == "FIXED_LENGTH"
        assert spread.byte_order == "MOST-SIGNIFICANT-BYTE-FIRST"
        assert spread.mem_alignment == ""

    def test_the_native_declaration_and_the_encoding_keep_their_whitespace_because_xml_does(
        self, tmp_path: Path
    ) -> None:
        """The two exceptions, ``NATIVE-DECLARATION`` and ``BASE-TYPE-ENCODING``, keep their whitespace.

        Both are schema *strings* in both licensed schemas, so XML preserves their whitespace, and they reach a written
        model field. The reader carries each as written (``test_arxml_import_cli.py`` and
        ``test_encoding_round_trip.py`` pin that) and ``merge_verdict._signature`` strips both for itself; stripping on
        the way in would change a model value with no line."""
        source = tmp_path / "pretty_printed.arxml"
        source.write_text(self._PRETTY_PRINTED, encoding="utf-8", newline="\n")

        spread = read_arxml(str(source)).sw_base_types[0]

        assert spread.native_declaration == "\n            uint16\n          "
        assert spread.encoding == "\n            NONE\n          "


# The reader carries the forms whole

#: A hand-built 4.3.0 file of plainly synthetic names: a DATA-CONSTR whose one rule states a
#: CONSTR-LEVEL, a VALUE and a TYPE_REFERENCE implementation type each naming a TEXTTABLE method,
#: a VALUE type naming none, and one method per scale-label case.
_FORMS = """<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
 xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-3-0.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>ProbeTypes</SHORT-NAME>
      <ELEMENTS>
        <SW-BASE-TYPE>
          <SHORT-NAME>ProbeBase</SHORT-NAME>
          <CATEGORY>FIXED_LENGTH</CATEGORY>
          <BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>
          <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
          <NATIVE-DECLARATION>uint8</NATIVE-DECLARATION>
        </SW-BASE-TYPE>
        <DATA-CONSTR>
          <SHORT-NAME>ProbeLevelled</SHORT-NAME>
          <DATA-CONSTR-RULES>
            <DATA-CONSTR-RULE>
              <CONSTR-LEVEL>{level}</CONSTR-LEVEL>
              <PHYS-CONSTRS>
                <LOWER-LIMIT>0</LOWER-LIMIT>
                <UPPER-LIMIT>10</UPPER-LIMIT>
              </PHYS-CONSTRS>
            </DATA-CONSTR-RULE>
          </DATA-CONSTR-RULES>
        </DATA-CONSTR>
        <DATA-CONSTR>
          <SHORT-NAME>ProbeUnlevelled</SHORT-NAME>
          <DATA-CONSTR-RULES>
            <DATA-CONSTR-RULE>
              <PHYS-CONSTRS>
                <LOWER-LIMIT>0</LOWER-LIMIT>
              </PHYS-CONSTRS>
            </DATA-CONSTR-RULE>
          </DATA-CONSTR-RULES>
        </DATA-CONSTR>
        <IMPLEMENTATION-DATA-TYPE>
          <SHORT-NAME>ProbeValue</SHORT-NAME>
          <CATEGORY>VALUE</CATEGORY>
          <SW-DATA-DEF-PROPS>
            <SW-DATA-DEF-PROPS-VARIANTS>
              <SW-DATA-DEF-PROPS-CONDITIONAL>
                <BASE-TYPE-REF DEST="SW-BASE-TYPE">/ProbeTypes/ProbeBase</BASE-TYPE-REF>
                <COMPU-METHOD-REF DEST="COMPU-METHOD">/ProbeTypes/ProbeTable</COMPU-METHOD-REF>
              </SW-DATA-DEF-PROPS-CONDITIONAL>
            </SW-DATA-DEF-PROPS-VARIANTS>
          </SW-DATA-DEF-PROPS>
        </IMPLEMENTATION-DATA-TYPE>
        <IMPLEMENTATION-DATA-TYPE>
          <SHORT-NAME>ProbeReference</SHORT-NAME>
          <CATEGORY>TYPE_REFERENCE</CATEGORY>
          <SW-DATA-DEF-PROPS>
            <SW-DATA-DEF-PROPS-VARIANTS>
              <SW-DATA-DEF-PROPS-CONDITIONAL>
                <COMPU-METHOD-REF DEST="COMPU-METHOD">/ProbeTypes/ProbeTable</COMPU-METHOD-REF>
                <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE"
                  >/ProbeTypes/ProbeValue</IMPLEMENTATION-DATA-TYPE-REF>
              </SW-DATA-DEF-PROPS-CONDITIONAL>
            </SW-DATA-DEF-PROPS-VARIANTS>
          </SW-DATA-DEF-PROPS>
        </IMPLEMENTATION-DATA-TYPE>
        <IMPLEMENTATION-DATA-TYPE>
          <SHORT-NAME>ProbePlain</SHORT-NAME>
          <CATEGORY>VALUE</CATEGORY>
          <SW-DATA-DEF-PROPS>
            <SW-DATA-DEF-PROPS-VARIANTS>
              <SW-DATA-DEF-PROPS-CONDITIONAL>
                <BASE-TYPE-REF DEST="SW-BASE-TYPE">/ProbeTypes/ProbeBase</BASE-TYPE-REF>
              </SW-DATA-DEF-PROPS-CONDITIONAL>
            </SW-DATA-DEF-PROPS-VARIANTS>
          </SW-DATA-DEF-PROPS>
        </IMPLEMENTATION-DATA-TYPE>
        <COMPU-METHOD>
          <SHORT-NAME>ProbeTable</SHORT-NAME>
          <CATEGORY>TEXTTABLE</CATEGORY>
          <COMPU-INTERNAL-TO-PHYS>
            <COMPU-SCALES>
              <COMPU-SCALE>
                <SHORT-LABEL>ProbeOff</SHORT-LABEL>
                <LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT>
                <UPPER-LIMIT INTERVAL-TYPE="CLOSED">0</UPPER-LIMIT>
                <COMPU-CONST><VT>ProbeOff</VT></COMPU-CONST>
              </COMPU-SCALE>
            </COMPU-SCALES>
          </COMPU-INTERNAL-TO-PHYS>
        </COMPU-METHOD>
        <COMPU-METHOD>
          <SHORT-NAME>ProbeRelabelled</SHORT-NAME>
          <CATEGORY>TEXTTABLE</CATEGORY>
          <COMPU-INTERNAL-TO-PHYS>
            <COMPU-SCALES>
              <COMPU-SCALE>
                <SHORT-LABEL>ProbeOtherLabel</SHORT-LABEL>
                <LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT>
                <UPPER-LIMIT INTERVAL-TYPE="CLOSED">0</UPPER-LIMIT>
                <COMPU-CONST><VT>ProbeOn</VT></COMPU-CONST>
              </COMPU-SCALE>
            </COMPU-SCALES>
          </COMPU-INTERNAL-TO-PHYS>
        </COMPU-METHOD>
        <COMPU-METHOD>
          <SHORT-NAME>ProbeLinear</SHORT-NAME>
          <CATEGORY>{linear_category}</CATEGORY>
          <COMPU-INTERNAL-TO-PHYS>
            <COMPU-SCALES>
              <COMPU-SCALE>
                <SHORT-LABEL>ProbeScaleLabel</SHORT-LABEL>
                <LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT>
                <UPPER-LIMIT INTERVAL-TYPE="CLOSED">10</UPPER-LIMIT>
                <COMPU-RATIONAL-COEFFS>
                  <COMPU-NUMERATOR><V>0</V><V>1</V></COMPU-NUMERATOR>
                  <COMPU-DENOMINATOR><V>1</V></COMPU-DENOMINATOR>
                </COMPU-RATIONAL-COEFFS>
              </COMPU-SCALE>
            </COMPU-SCALES>
          </COMPU-INTERNAL-TO-PHYS>
        </COMPU-METHOD>
      </ELEMENTS>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""


def _forms_model(tmp_path: Path, *, level: str = "0", linear_category: str = "LINEAR") -> ImportModel:
    source = tmp_path / "forms.arxml"
    source.write_text(_FORMS.format(level=level, linear_category=linear_category), encoding="utf-8", newline="\n")
    return read_arxml(str(source))


def _entries_at(model: ImportModel, path: str, tag: str) -> list[SkipEntry]:
    return [entry for entry in model.skip_log if (entry.path, entry.tag) == (path, tag)]


class TestTheReaderCarriesTheFormsWhole:
    """The reader carries a rule's ``CONSTR-LEVEL`` and an implementation type's ``COMPU-METHOD-REF``.

    A ``LINEAR`` scale's ``SHORT-LABEL`` is a section-1 line citing ``[constr_1375]``. The reader maps nothing, so
    family F2's and family F9's lines stay beside the records in its own skip log; both importers then leave out the
    ones they carry (``PathRegistry.carried_reader_lines``, held by ``test_data_description_import.py``). What this does
    not reach: the importers, and a category other than ``LINEAR`` and ``TEXTTABLE`` (its label keeps its section-2
    line)."""

    @pytest.mark.parametrize(("text", "level"), [("0", 0), ("2", 2), (" 1 ", 1), ("0x3", 3)])
    def test_a_rules_constr_level_is_read_into_the_record(self, tmp_path: Path, text: str, level: int) -> None:
        constraints = {info.name: info for info in _forms_model(tmp_path, level=text).data_constrs}
        assert constraints["ProbeLevelled"].level == level
        assert constraints["ProbeLevelled"].level_text == text.strip()
        assert (constraints["ProbeUnlevelled"].level, constraints["ProbeUnlevelled"].level_text) == (None, None)

    def test_a_level_no_integer_states_keeps_its_text_and_no_value(self, tmp_path: Path) -> None:
        levelled = next(info for info in _forms_model(tmp_path, level="high").data_constrs if info.level_text)
        assert (levelled.level, levelled.level_text) == (None, "high")

    def test_an_implementation_types_compu_method_ref_is_read_into_the_record(self, tmp_path: Path) -> None:
        types = {info.name: info for info in _forms_model(tmp_path).impl_data_types}
        assert types["ProbeValue"].compu_method_ref == "/ProbeTypes/ProbeTable"
        assert types["ProbeReference"].compu_method_ref == "/ProbeTypes/ProbeTable"
        assert types["ProbePlain"].compu_method_ref is None

    def test_family_f2_and_f9_lines_stay_beside_the_records(self, tmp_path: Path) -> None:
        """The reader maps nothing, so each read value keeps its family's line in the reader's own log."""
        model = _forms_model(tmp_path)
        for name in ("ProbeValue", "ProbeReference"):
            (f2,) = _entries_at(model, f"/ProbeTypes/{name}/SW-DATA-DEF-PROPS", "COMPU-METHOD-REF")
            assert (f2.category, f2.rule, f2.effect) == (SkipCategory.NOT_SUPPORTED, None, "abort")
            assert "COMPU-METHOD-REF of an IMPLEMENTATION-DATA-TYPE" in f2.reason
        assert _entries_at(model, "/ProbeTypes/ProbePlain/SW-DATA-DEF-PROPS", "COMPU-METHOD-REF") == []
        (f9,) = _entries_at(model, "/ProbeTypes/ProbeLevelled/DATA-CONSTR-RULES", "CONSTR-LEVEL")
        assert (f9.category, f9.rule, f9.effect) == (SkipCategory.NOT_SUPPORTED, None, "report")
        assert _entries_at(model, "/ProbeTypes/ProbeUnlevelled/DATA-CONSTR-RULES", "CONSTR-LEVEL") == []

    def test_a_linear_scales_short_label_is_a_section_1_line_citing_constr_1375(self, tmp_path: Path) -> None:
        model = _forms_model(tmp_path)
        (line,) = _entries_at(model, "/ProbeTypes/ProbeLinear/COMPU-INTERNAL-TO-PHYS", "SHORT-LABEL")
        assert (line.category, line.rule, line.effect) == (
            SkipCategory.EXPORT_DEFECT,
            StandardRule.COMPU_ATTRIBUTES_BY_CATEGORY,
            "report",
        )
        assert StandardRule.COMPU_ATTRIBUTES_BY_CATEGORY.value == "constr_1375"
        # Still family F10's line by its mark and its key: descriptive, a label changes no behaviour.
        assert line.reason.startswith("SHORT-LABEL of a LINEAR COMPU-SCALE not carried")

    def test_a_texttable_label_equal_to_its_vt_is_no_line_and_one_differing_keeps_todays(self, tmp_path: Path) -> None:
        model = _forms_model(tmp_path)
        assert _entries_at(model, "/ProbeTypes/ProbeTable/COMPU-INTERNAL-TO-PHYS", "SHORT-LABEL") == []
        (line,) = _entries_at(model, "/ProbeTypes/ProbeRelabelled/COMPU-INTERNAL-TO-PHYS", "SHORT-LABEL")
        assert (line.category, line.rule, line.effect) == (SkipCategory.NOT_SUPPORTED, None, "report")
        assert line.reason == (
            "SHORT-LABEL of a TEXTTABLE COMPU-SCALE not carried: SWCraft writes the literal as the label"
        )

    @pytest.mark.parametrize("category", ["SCALE_LINEAR", "IDENTICAL"])
    def test_another_categorys_label_keeps_todays_section_2_line(self, tmp_path: Path, category: str) -> None:
        """The member is cited for the ``LINEAR`` column alone; another category's label keeps its section-2 line."""
        model = _forms_model(tmp_path, linear_category=category)
        (line,) = _entries_at(model, "/ProbeTypes/ProbeLinear/COMPU-INTERNAL-TO-PHYS", "SHORT-LABEL")
        assert (line.category, line.rule) == (SkipCategory.NOT_SUPPORTED, None)
        assert (
            line.reason == f"SHORT-LABEL of a {category} COMPU-SCALE not carried: SWCraft writes no label on the scale"
        )


# Every number field is read by the grammar of its XSD type, per call site
# (arxml_reader.NumberGrammar): the pattern of NUMERICAL-VALUE and the integer
# types, where an octal spelling is unsigned; the formula of LIMIT and the
# *-VALUE-VARIATION-POINT types, whose one sign is the unary operator. Each case
# goes through the field's own ARXML shape, never the helper alone.


def _typed(value: object) -> tuple[object, type]:
    """*value* with its type: ``8 == 8.0`` in Python, and which of the two is read is part of the answer."""
    return value, type(value)


def _document(tmp_path: Path, elements: str) -> ImportModel:
    """Read one package whose ``ELEMENTS`` hold *elements*."""
    source = tmp_path / "numbers.arxml"
    source.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<AUTOSAR xmlns="http://autosar.org/schema/r4.0"><AR-PACKAGES><AR-PACKAGE>'
        f"<SHORT-NAME>Pkg</SHORT-NAME><ELEMENTS>{elements}</ELEMENTS>"
        "</AR-PACKAGE></AR-PACKAGES></AUTOSAR>\n",
        encoding="utf-8",
    )
    return read_arxml(str(source))


def _texttable_lower(tmp_path: Path, text: str) -> int | None:
    """A ``TEXTTABLE`` scale's ``LOWER-LIMIT`` -- an ``AR:LIMIT``, a formula field."""
    model = _document(
        tmp_path,
        "<COMPU-METHOD><SHORT-NAME>Table</SHORT-NAME><CATEGORY>TEXTTABLE</CATEGORY><COMPU-INTERNAL-TO-PHYS>"
        f'<COMPU-SCALES><COMPU-SCALE><LOWER-LIMIT INTERVAL-TYPE="CLOSED">{text}</LOWER-LIMIT>'
        "<COMPU-CONST><VT>ON</VT></COMPU-CONST></COMPU-SCALE></COMPU-SCALES></COMPU-INTERNAL-TO-PHYS></COMPU-METHOD>",
    )
    (compu,) = model.compu_methods
    return compu.scales[0].value if compu.scales else None


def _phys_lower(tmp_path: Path, text: str) -> tuple[int | float | None, frozenset[str]]:
    """A ``PHYS-CONSTRS`` ``LOWER-LIMIT`` -- an ``AR:LIMIT``, a formula field -- and what was unreadable."""
    model = _document(
        tmp_path,
        "<DATA-CONSTR><SHORT-NAME>Range</SHORT-NAME><DATA-CONSTR-RULES><DATA-CONSTR-RULE><PHYS-CONSTRS>"
        f'<LOWER-LIMIT INTERVAL-TYPE="CLOSED">{text}</LOWER-LIMIT>'
        "</PHYS-CONSTRS></DATA-CONSTR-RULE></DATA-CONSTR-RULES></DATA-CONSTR>",
    )
    (constr,) = model.data_constrs
    return constr.phys_lower, constr.unreadable_limits


def _numerical_value(text: str) -> int | float | None:
    """A ``NUMERICAL-VALUE-SPECIFICATION``'s ``VALUE`` -- a ``NUMERICAL-VALUE-VARIATION-POINT``, a formula field."""
    value, _reason = _value_spec(
        f"<NUMERICAL-VALUE-SPECIFICATION{_NS_ATTR}><VALUE>{text}</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
    )
    return value.value if value is not None else None


def _application_value(text: str) -> int | float | None:
    """An ``APPLICATION-VALUE-SPECIFICATION``'s ``SW-VALUES-PHYS/V`` -- a ``NUMERICAL-VALUE``, the pattern."""
    value, _reason = _value_spec(_application(f"<V>{text}</V>"))
    return value.value if value is not None else None


def _base_type_size(tmp_path: Path, text: str) -> int | None:
    """``SW-BASE-TYPE/BASE-TYPE-SIZE`` -- a ``POSITIVE-INTEGER``, the pattern."""
    source = tmp_path / "base_type.arxml"
    original = MINIMAL_SWC.read_text(encoding="utf-8")
    assert original.count("<BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>") == 1
    source.write_text(
        original.replace("<BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>", f"<BASE-TYPE-SIZE>{text}</BASE-TYPE-SIZE>"),
        encoding="utf-8",
        newline="\n",
    )
    (base,) = read_arxml(str(source)).sw_base_types
    return base.size


class TestEveryNumberFieldIsReadByItsOwnGrammar:
    """Every number field is read by its own grammar.

    ``010`` is the standard's octal eight in both grammars; ``08`` and ``010.5`` are
    spellings of neither; a signed octal or hexadecimal is a formula spelling only."""

    @pytest.mark.parametrize(("text", "expected"), [("010", 8), ("0", 0), ("-010", -8), ("-0x10", -16), ("08", None)])
    def test_a_texttable_scale_limit_is_a_formula(self, tmp_path: Path, text: str, expected: int | None) -> None:
        assert _typed(_texttable_lower(tmp_path, text)) == _typed(expected)

    def test_a_texttable_scale_limit_outside_the_grammar_is_reported_not_read(self, tmp_path: Path) -> None:
        model = _document(
            tmp_path,
            "<COMPU-METHOD><SHORT-NAME>Table</SHORT-NAME><CATEGORY>TEXTTABLE</CATEGORY><COMPU-INTERNAL-TO-PHYS>"
            '<COMPU-SCALES><COMPU-SCALE><LOWER-LIMIT INTERVAL-TYPE="CLOSED">010.5</LOWER-LIMIT>'
            "<COMPU-CONST><VT>ON</VT></COMPU-CONST></COMPU-SCALE></COMPU-SCALES></COMPU-INTERNAL-TO-PHYS></COMPU-METHOD>",
        )
        (compu,) = model.compu_methods
        assert compu.scales == []
        assert [(entry.path, entry.tag, entry.reason) for entry in model.skip_log.entries] == [
            (
                "/Pkg/Table/COMPU-INTERNAL-TO-PHYS",
                "COMPU-SCALE",
                "malformed COMPU-SCALE [0] (LOWER-LIMIT='010.5', VT='ON') -- entry dropped, enum literal lost",
            )
        ]

    @pytest.mark.parametrize(
        ("text", "expected"), [("010", 8.0), ("0", 0.0), ("-010", -8.0), ("-0x10", -16.0), ("+010", 8.0)]
    )
    def test_a_physical_constraint_limit_is_a_formula(self, tmp_path: Path, text: str, expected: float) -> None:
        value, unreadable = _phys_lower(tmp_path, text)
        assert (_typed(value), unreadable) == (_typed(expected), frozenset())

    @pytest.mark.parametrize("text", ["08", "010.5", "--010", "1+1"])
    def test_a_physical_constraint_limit_outside_the_grammar_is_unreadable(self, tmp_path: Path, text: str) -> None:
        assert _phys_lower(tmp_path, text) == (None, frozenset({"LOWER-LIMIT"}))

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("010", 8),
            ("0", 0),
            ("-010", -8),
            ("-0x10", -16),
            (".5", 0.5),
            ("-40.0", -40.0),
            ("08", None),
            ("010.5", None),
        ],
    )
    def test_a_numerical_specification_value_is_a_formula(self, text: str, expected: int | float | None) -> None:
        assert _typed(_numerical_value(text)) == _typed(expected)

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("010", 8),
            ("0", 0),
            ("-0", 0),
            ("+0", 0),
            ("-010", None),
            ("-0x10", None),
            ("0x10", 16),
            ("08", None),
            ("010.5", None),
        ],
    )
    def test_an_application_value_is_the_pattern(self, text: str, expected: int | None) -> None:
        """``SW-VALUES-PHYS/V`` is a ``NUMERICAL-VALUE``, whose decimal ``[+\\-]?[0-9]`` spells ``-0`` and ``+0``."""
        assert _typed(_application_value(text)) == _typed(expected)

    def test_an_octal_init_keeps_its_spelling_for_the_radix_line(self) -> None:
        """The radix report line names every re-spelled radix, octal and binary as well as hexadecimal."""
        value, reason = _value_spec(_application("<V>010</V>"))
        assert reason is None and value is not None
        assert (value.value, value.radix_source) == (8, "010")
        value, reason = _value_spec(_application("<V>0b11</V>"))
        assert reason is None and value is not None
        assert (value.value, value.radix_source) == (3, "0b11")

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("010", 8),
            ("0x10", 16),
            ("0", 0),
            ("8", 8),
            ("-010", None),
            ("08", None),
            ("-0", None),
            ("+0", None),
            ("+8", None),
            ("-8", None),
        ],
    )
    def test_a_base_type_size_is_the_positive_integer_pattern(
        self, tmp_path: Path, text: str, expected: int | None
    ) -> None:
        """``BASE-TYPE-SIZE`` is a ``POSITIVE-INTEGER`` (XSD R4.3.0:86393; R4.2.2:75075).

        Its pattern ``[1-9][0-9]*|0[xX]...|0[bB]...|0[0-7]*`` admits no sign at all: ``+8`` and ``-8`` are no
        spelling of it, and zero is the octal ``0[0-7]*`` alone."""
        assert _typed(_base_type_size(tmp_path, text)) == _typed(expected)

    @pytest.mark.parametrize(("text", "expected"), [("010", 8), ("-010", -8), ("+0x10", 16), ("08", None)])
    def test_an_array_size_is_a_formula(self, tmp_path: Path, text: str, expected: int | None) -> None:
        """``IMPLEMENTATION-DATA-TYPE-ELEMENT/ARRAY-SIZE`` is a ``POSITIVE-INTEGER-VALUE-VARIATION-POINT``."""
        model = _document(
            tmp_path,
            "<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>Arr</SHORT-NAME><CATEGORY>ARRAY</CATEGORY><SUB-ELEMENTS>"
            "<IMPLEMENTATION-DATA-TYPE-ELEMENT><SHORT-NAME>e</SHORT-NAME><CATEGORY>TYPE_REFERENCE</CATEGORY>"
            f"<ARRAY-SIZE>{text}</ARRAY-SIZE></IMPLEMENTATION-DATA-TYPE-ELEMENT></SUB-ELEMENTS>"
            "</IMPLEMENTATION-DATA-TYPE>",
        )
        (impl,) = model.impl_data_types
        assert _typed(impl.sub_elements[0].array_size) == _typed(expected)

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("010", 8), ("-010", None), ("-0x10", None), ("-8", -8), ("+8", 8), ("0", 0), ("-0", None), ("+0", None)],
    )
    def test_a_constraint_level_is_the_integer_pattern(self, tmp_path: Path, text: str, expected: int | None) -> None:
        """``DATA-CONSTR-RULE/CONSTR-LEVEL`` is an ``INTEGER`` (XSD R4.3.0:84188; R4.2.2:72915).

        A prefixed literal is unsigned there, and so is zero -- ``[+\\-]?[1-9][0-9]*`` signs a
        nonzero decimal only, and ``0`` is the octal ``0[0-7]*`` -- so ``-0`` and ``+0`` are no
        integer spelling, where a ``NUMERICAL-VALUE`` spells both."""
        model = _document(
            tmp_path,
            "<DATA-CONSTR><SHORT-NAME>Range</SHORT-NAME><DATA-CONSTR-RULES><DATA-CONSTR-RULE>"
            f"<CONSTR-LEVEL>{text}</CONSTR-LEVEL><PHYS-CONSTRS><LOWER-LIMIT>0</LOWER-LIMIT></PHYS-CONSTRS>"
            "</DATA-CONSTR-RULE></DATA-CONSTR-RULES></DATA-CONSTR>",
        )
        (constr,) = model.data_constrs
        assert (_typed(constr.level), constr.level_text) == (_typed(expected), text)

    def test_a_time_value_is_a_double_where_010_is_ten(self, tmp_path: Path) -> None:
        """``TIME-VALUE`` restricts ``xsd:double`` (XSD R4.3.0:88606; R4.2.2:76972), which has no octal form."""
        source = tmp_path / "period.arxml"
        original = MINIMAL_SWC.read_text(encoding="utf-8")
        assert original.count("<PERIOD>0.01</PERIOD>") == 1
        source.write_text(
            original.replace("<PERIOD>0.01</PERIOD>", "<PERIOD>010</PERIOD>"), encoding="utf-8", newline="\n"
        )
        (event,) = read_arxml(str(source)).swcs[0].events
        assert event.period == 10.0

    #: ``xsd:double``'s lexical space (XML Schema Part 2, 3.2.5.1; ``TIME-VALUE--SIMPLE`` restricts it with no
    #: facet of its own, XSD R4.3.0:88606-88608; R4.2.2:76972-76974): a decimal mantissa of ASCII digits, an
    #: optional ``e``/``E`` exponent, ``INF``, ``-INF`` and ``NaN``, after the fixed ``collapse`` facet has
    #: removed the XML whitespace (space, tab, CR, LF) around it. Python's ``float()`` reads more: ``_``
    #: between digits, Unicode digits and Unicode whitespace, ``infinity`` and ``nan`` in any case.
    _DOUBLE_READ = (("1.5", 1.5), ("1e3", 1000.0), (" 1.5\n", 1.5), ("\t-2.5E-1\r", -0.25), (".5", 0.5), ("5.", 5.0))
    _DOUBLE_REFUSED = ("1_000", "\u00a01.5\u00a0", "infinity", "inf", "nan", "\uff11", "1 000", "0x10", "+INF")
    _DOUBLE_NON_FINITE = ("INF", "-INF", "NaN")

    def _period(self, tmp_path: Path, text: str) -> float | None:
        source = tmp_path / "period.arxml"
        original = MINIMAL_SWC.read_text(encoding="utf-8")
        source.write_text(
            original.replace("<PERIOD>0.01</PERIOD>", f"<PERIOD>{text}</PERIOD>"), encoding="utf-8", newline="\n"
        )
        (event,) = read_arxml(str(source)).swcs[0].events
        return event.period

    def _min_start_interval(self, tmp_path: Path, text: str) -> float | None:
        source = tmp_path / "interval.arxml"
        original = MINIMAL_SWC_4_3_0.read_text(encoding="utf-8")
        spelled = "<MINIMUM-START-INTERVAL>0.01</MINIMUM-START-INTERVAL>"
        assert original.count(spelled) == 1
        source.write_text(
            original.replace(spelled, f"<MINIMUM-START-INTERVAL>{text}</MINIMUM-START-INTERVAL>"),
            encoding="utf-8",
            newline="\n",
        )
        (runnable,) = read_arxml(str(source)).swcs[0].runnables
        return runnable.min_start_interval

    @pytest.mark.parametrize(("text", "expected"), _DOUBLE_READ)
    def test_a_time_value_reads_the_xsd_double_spellings(self, tmp_path: Path, text: str, expected: float) -> None:
        assert self._period(tmp_path, text) == expected
        assert self._min_start_interval(tmp_path, text) == expected

    @pytest.mark.parametrize("text", _DOUBLE_REFUSED + _DOUBLE_NON_FINITE)
    def test_a_time_value_outside_the_xsd_double_finite_spellings_is_no_number(self, tmp_path: Path, text: str) -> None:
        assert self._period(tmp_path, text) is None
        assert self._min_start_interval(tmp_path, text) is None


class TestAFormulaSkipsWhitespaceBetweenItsSignAndItsLiteral:
    """In a formula field, whitespace between the unary sign and its literal is skipped: ``- 8`` is -8.

    The formula language's ``WS`` token is hidden between any two tokens (GST R4.3.0:4809 -- space, tab, CR, LF;
    R4.2.2:3957-3958 -- space, tab, CR; the superset read at both releases, as the patterns' spellings are), so between
    the unary ``SumOperator`` and its literal it is no part of the number. A literal is one token, so whitespace inside
    one is never skipped; a pattern field is unchanged (its sign is part of the decimal literal)."""

    @pytest.mark.parametrize(
        ("text", "expected"), [("- 010", -8), ("-\t8", -8), ("+ 0x10", 16), ("-\r\n0b11", -3), ("- 0 10", None)]
    )
    def test_a_texttable_scale_limit(self, tmp_path: Path, text: str, expected: int | None) -> None:
        assert _typed(_texttable_lower(tmp_path, text)) == _typed(expected)

    @pytest.mark.parametrize(("text", "expected"), [("- 8.5", -8.5), ("-  010", -8.0), ("+\t.5", 0.5)])
    def test_a_physical_constraint_limit(self, tmp_path: Path, text: str, expected: float) -> None:
        value, unreadable = _phys_lower(tmp_path, text)
        assert (_typed(value), unreadable) == (_typed(expected), frozenset())

    @pytest.mark.parametrize("text", ["- 8 .5", "-0 x10", "1 0", "- -8", "8 -"])
    def test_whitespace_inside_a_literal_or_a_second_sign_is_no_number(self, tmp_path: Path, text: str) -> None:
        assert _phys_lower(tmp_path, text) == (None, frozenset({"LOWER-LIMIT"}))

    @pytest.mark.parametrize(
        ("text", "expected"), [("- 010", -8), ("- 0x10", -16), ("-\n40.0", -40.0), ("+ 1e3", 1000.0), ("1 0", None)]
    )
    def test_a_numerical_specification_value(self, text: str, expected: int | float | None) -> None:
        assert _numerical_value(text) == expected

    @pytest.mark.parametrize("text", ["- 8", "+ 8", "- 8.5"])
    def test_a_pattern_field_is_unchanged(self, tmp_path: Path, text: str) -> None:
        assert _application_value(text) is None
        assert parse_int_literal(text, grammar="pattern") is None

    def test_the_helper_reads_both_grammars_apart(self) -> None:
        assert parse_int_literal("- 010", grammar="formula") == -8
        assert parse_int_literal("- 010", grammar="pattern") is None
        assert parse_int_literal("-8", grammar="pattern") == -8


# A PHYS-CONSTRS limit's INTERVAL-TYPE


def _phys_constrs(tmp_path: Path, *limits: tuple[str, str | None, str]) -> DataConstrInfo:
    """One ``DATA-CONSTR`` whose first rule's ``PHYS-CONSTRS`` holds *limits*: (tag, ``INTERVAL-TYPE``, text)."""
    body = "".join(
        f"<{tag}>{text}</{tag}>" if interval is None else f'<{tag} INTERVAL-TYPE="{interval}">{text}</{tag}>'
        for tag, interval, text in limits
    )
    model = _document(
        tmp_path,
        "<DATA-CONSTR><SHORT-NAME>Range</SHORT-NAME><DATA-CONSTR-RULES><DATA-CONSTR-RULE><PHYS-CONSTRS>"
        f"{body}</PHYS-CONSTRS></DATA-CONSTR-RULE></DATA-CONSTR-RULES></DATA-CONSTR>",
    )
    (constr,) = model.data_constrs
    return constr


class TestAPhysicalLimitsIntervalIsRead:
    """``DataConstrInfo`` carries each ``PHYS-CONSTRS`` limit's ``INTERVAL-TYPE`` beside its text.

    An own-side infinity -- the side's infinite spelling, or ``INFINITE`` whatever number it
    holds -- is the absent bound and neither unreadable nor inexact; ``NaN`` and the other
    side's spelling stay unreadable, under every interval.
    """

    def test_the_intervals_are_carried_as_spelled_and_absent_is_none(self, tmp_path: Path) -> None:
        constr = _phys_constrs(tmp_path, ("LOWER-LIMIT", " OPEN ", "1"), ("UPPER-LIMIT", None, "9"))

        assert (constr.phys_lower_interval, constr.phys_upper_interval) == ("OPEN", None)
        assert (constr.phys_lower, constr.phys_upper) == (1.0, 9.0)

    @pytest.mark.parametrize(
        ("tag", "interval", "text"),
        [
            pytest.param("LOWER-LIMIT", None, "-INF", id="lower-minus-inf"),
            pytest.param("UPPER-LIMIT", "CLOSED", "INF", id="upper-inf"),
            pytest.param("UPPER-LIMIT", None, "+INF", id="upper-plus-inf"),
            pytest.param("LOWER-LIMIT", "INFINITE", "5", id="lower-infinite-with-a-number"),
            pytest.param("UPPER-LIMIT", "INFINITE", "16777217.000000001", id="upper-infinite-with-an-inexact-number"),
        ],
    )
    def test_an_own_side_infinity_is_the_absent_bound_and_no_defect(
        self, tmp_path: Path, tag: str, interval: str | None, text: str
    ) -> None:
        other = "UPPER-LIMIT" if tag == "LOWER-LIMIT" else "LOWER-LIMIT"
        constr = _phys_constrs(tmp_path, (tag, interval, text), (other, None, "7"))

        bounds = {"LOWER-LIMIT": constr.phys_lower, "UPPER-LIMIT": constr.phys_upper}
        assert (bounds[tag], bounds[other]) == (None, 7.0)
        assert (constr.unreadable_limits, constr.inexact_limits) == (frozenset(), frozenset())

    @pytest.mark.parametrize(
        ("tag", "interval", "text"),
        [
            pytest.param("LOWER-LIMIT", None, "NaN", id="nan"),
            pytest.param("LOWER-LIMIT", "INFINITE", "NaN", id="nan-infinite"),
            pytest.param("UPPER-LIMIT", "INFINITE", " nan ", id="nan-infinite-any-case"),
            pytest.param("LOWER-LIMIT", None, "INF", id="lower-inf"),
            pytest.param("LOWER-LIMIT", "INFINITE", "+INF", id="lower-plus-inf-infinite"),
            pytest.param("UPPER-LIMIT", "INFINITE", "-INF", id="upper-minus-inf-infinite"),
        ],
    )
    def test_nan_and_the_other_sides_infinity_stay_unreadable(
        self, tmp_path: Path, tag: str, interval: str | None, text: str
    ) -> None:
        constr = _phys_constrs(tmp_path, (tag, interval, text))

        assert constr.unreadable_limits == frozenset({tag})
