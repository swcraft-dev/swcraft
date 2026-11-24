"""Every skipped element carries whose problem it is: the entry, the enums, the release, the gate.

The import report sorts its losses into three sections -- defects of the export, not supported by SWCraft, outside
the file -- and the section is a **field of the skip entry**, chosen where the entry is recorded and never guessed
from its text. This module holds:

- the closed :class:`SkipCategory` and :class:`StandardRule`: a defect cites exactly one rule, nothing else any;
- :meth:`SkipLog.first_at`, :meth:`SkipLog.record_chained` and :func:`merge.record_chained_to`, through which a
  chained entry inherits its root's category and rule (``chain_skip`` decides by the cause);
- the declared release (``ImportModel.declared_schema``, ``declared_release``), read as the XSD gate reads it, and
  ``ImportModel.variant_rich``; :func:`cite`, the one printed citation;
- the reader's sites: a duplicate full path is ``[constr_2508]`` unless the file is variant-rich;
- the mappers' declared / outside-the-file splits, each half with its own sentence, and their cited cases;
- connectors and compositions: a same-direction assembly is ``[TPS_SWCT_01113]`` unless its ends' declared
  interface kinds differ (``[constr_1036]``); SWCraft's own flatter name spaces stay section 2;
- chain inheritance, a section-1 root included;
- the AST gates over every record site's category and effect.
"""

from __future__ import annotations

import ast
import functools
from collections import Counter
from pathlib import Path

import pytest
from arxml_import_shapes_support import raw_element
from behaviour_losses_support import BEHAVIOUR, MINIMAL_SWC, NS, RUNNABLE, add_xml
from lxml import etree
from project_support import init_project
from swcraft_tools.arxml_import import map_compositions, map_data_types, map_interfaces, map_swcs
from swcraft_tools.arxml_import.merge import (
    CONFLICTING,
    LAYER_INTERFACES,
    chained_reason,
    narrow_to_selection,
    record_chained_to,
)
from swcraft_tools.arxml_import.merge_verdict import decide_merges
from swcraft_tools.arxml_import.swc_mapping import map_ports
from swcraft_tools.arxml_import.type_mapping import UNNAMED_MEMBER, _map_init_value, map_mapping_sets
from swcraft_tools.arxml_import.unresolved import ExternalStub, UnresolvedReferences
from swcraft_tools.arxml_reader import (
    FALLBACK_JUDGING_RELEASE,
    VARIANT_RICH_DUPLICATE_CLAUSE,
    AppArrayDataTypeInfo,
    AppPrimitiveDataTypeInfo,
    AppRecordDataTypeInfo,
    AssemblyConnectorInfo,
    ComponentPrototypeInfo,
    CompuMethodInfo,
    CompuScaleInfo,
    ConnectorPortRef,
    ConstantRecord,
    ConstantRecordField,
    ConstantScalar,
    ConstantSpecInfo,
    CSArgumentInfo,
    CSInterfaceInfo,
    CSOperationInfo,
    DataTypeMappingSetInfo,
    DelegationConnectorInfo,
    ImplDataTypeInfo,
    ImplSubElementInfo,
    ImportModel,
    PortComSpecInitInfo,
    RecordElementInfo,
    SkipCategory,
    SkipEntry,
    SkipLog,
    SkipReason,
    SRDataElementInfo,
    SRInterfaceInfo,
    StandardRule,
    SwBaseTypeInfo,
    SwcAccessInfo,
    SwcEventInfo,
    SwcInfo,
    SwcPortInfo,
    SwcRunnableInfo,
    blank_short_name,
    cite,
    duplicate_name_reason,
    not_supported,
    read_arxml,
)
from swcraft_tools.arxml_validation import _declared_schema_filename
from swcraft_tools.arxml_version import NAMESPACE, XSI_NAMESPACE, ArxmlVersion
from swcraft_tools.component_exchange import LossyComponentImportError, import_component
from swcraft_tools.data_description import DataDescription
from swcraft_tools.schema import EnumDataType, EnumLiteral
from test_arxml_import_compositions import (
    COMPOSITION_DEST,
    _a_composition_named_like_a_component,
    _add_swc,
    _base_inline_model,
    _composition_info,
    _map_full,
    _prototype,
)
from test_arxml_import_interfaces import _constant, _model_with_types, _one_init, _with_interface
from test_arxml_import_types import _a_base_type_named_where_an_implementation_type_belongs
from test_component_import import _export_target

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "swcraft_tools"


# ---------------------------------------------------------------------------
# The entry and the two enums
# ---------------------------------------------------------------------------


class TestTheEnums:
    def test_the_category_has_exactly_three_members(self) -> None:
        assert [member.name for member in SkipCategory] == ["EXPORT_DEFECT", "NOT_SUPPORTED", "OUTSIDE_FILE"]

    def test_the_rule_table_is_the_closed_set_of_twenty_one(self) -> None:
        """The closed rule table; each rule read in both releases, the same id and the same normative text.

        ``[constr_1202]`` and ``[constr_1204]`` are covered by ``[TPS_SWCT_01113]``; ``[constr_1221]``,
        ``[TPS_GST_00020]`` and ``[TPS_GST_00057]`` are the mapper defects' rules. ``[constr_1017]`` (SWCT
        R4.2.2 :15613, R4.3.0 :16760); ``[TPS_SWCT_01219]`` (R4.2.2 :7734, R4.3.0 :8221), ``[constr_1174]``
        (:4016, :4369) and ``[constr_1277]`` (:25448, :28306), the rules ``check-model`` refuses an imported
        model by; ``[constr_2025]`` (System Template R4.2.2 :26114, R4.3.0 :30512) and the ``CIdentifier``
        primitive's class table (GST R4.2.2 Table 4.31 :3203, R4.3.0 Table 4.49 :3910) -- the one value that
        is not an id, since no rule restates the pattern; ``[constr_1375]`` (SWCT R4.2.2 :17401 with Table
        5.74, R4.3.0 :18891 with Table 5.78), cited for its ``CompuScale.shortLabel`` row, ``N/A`` in the
        ``LINEAR`` column; ``[constr_1225]`` (SWCT R4.2.2 :15421, R4.3.0 :16544) and ``[constr_1284]``
        (:20008, :21951) -- the second the one member whose list of categories differs, cited only where
        the two agree; ``[constr_1385]`` (SWCT R4.2.2 :21119, R4.3.0 :23522); ``[constr_1221]`` for its two
        sentences (:21113, :23511)."""
        assert {member.name: member.value for member in StandardRule} == {
            "INSTANCE_REF_CONTEXT": "constr_2530",
            "INSTANCE_REF_PATH": "TPS_GST_00162",
            "SHORT_NAME_UNIQUE": "constr_2508",
            "DELEGATION_SAME_KIND": "constr_1032",
            "ONE_CONNECTOR_PER_PAIR": "constr_1086",
            "DELEGATION_INSIDE_COMPOSITION": "constr_1088",
            "CONNECT_PROVIDE_TO_REQUIRE": "TPS_SWCT_01113",
            "CONNECT_SAME_INTERFACE_KIND": "constr_1036",
            "APPLICATION_VALUE_FOR_AN_APPLICATION_PRIMITIVE": "constr_1221",
            "TABLE_INIT_IS_A_TEXT_VALUE": "constr_1225",
            "TEXT_VALUE_UNDER_A_TABLE_ONLY": "constr_1284",
            "NO_APPLICATION_VALUE_FOR_AN_IMPLEMENTATION_TYPE": "constr_1385",
            "REFERENCE_DESTINATION_TYPE": "TPS_GST_00020",
            "SHORT_NAME_REQUIRED": "TPS_GST_00057",
            "CALIBRATION_ACCESS_SUPPORTED": "constr_1017",
            "QUEUED_COM_SPEC_DEFINED": "TPS_SWCT_01219",
            "NO_SERVICE_PORT_ON_A_COMPOSITION": "constr_1174",
            "NO_QUEUED_RECEIVE_BY_VALUE": "constr_1277",
            "SYMBOL_UNIQUE": "constr_2025",
            "C_IDENTIFIER_FORM": "GST primitive CIdentifier",
            "COMPU_ATTRIBUTES_BY_CATEGORY": "constr_1375",
            # `[TPS_SWCT_01521]` (SWCT R4.2.2 :24930, R4.3.0 :27691) and `[constr_2026]` (:24934, :27695).
            "LOCAL_VARIABLE_REFERENCE": "TPS_SWCT_01521",
            "LOCAL_VARIABLE_OWNER": "constr_2026",
            # `[constr_2020]` (SWCT R4.2.2 :25331, R4.3.0 :28449) and `[constr_1103]` (:7950, :8450).
            "NO_QUEUED_IMPLICIT_READ": "constr_2020",
            "NO_ENABLE_UPDATE_FOR_AN_IMPLICIT_READ": "constr_1103",
            # `[constr_2022]` (SWCT R4.2.2 :25868, R4.3.0 :28782), `[constr_2023]` (:25875, :28789) and
            # `[constr_1259]` (:24057, :26717); `[constr_2006]` (:26048, :28964), whose count differs (exactly one,
            # at most one) and which is cited only where the release it names forbids what the file states.
            "CALL_SYNCHRONY_EXCLUSIVE": "constr_2022",
            "CALL_TIMEOUTS_IDENTICAL": "constr_2023",
            "NO_RESULT_POINT_IN_AN_INIT_RUNNABLE": "constr_1259",
            "ONE_RESULT_POINT_PER_CALL": "constr_2006",
        }


class TestTheEntry:
    def test_record_without_a_category_is_a_type_error(self) -> None:
        with pytest.raises(TypeError):
            SkipLog().record("/P/X", "TAG", "reason", effect="abort")  # type: ignore[call-arg]

    def test_an_entry_without_a_category_is_a_type_error(self) -> None:
        with pytest.raises(TypeError):
            SkipEntry(path="/P/X", tag="TAG", reason="reason", effect="abort")  # type: ignore[call-arg]

    def test_a_defect_without_a_rule_raises(self) -> None:
        with pytest.raises(ValueError, match="must cite"):
            SkipLog().record("/P/X", "TAG", "reason", category=SkipCategory.EXPORT_DEFECT, effect="abort")

    @pytest.mark.parametrize("category", [SkipCategory.NOT_SUPPORTED, SkipCategory.OUTSIDE_FILE])
    def test_a_non_defect_with_a_rule_raises(self, category: SkipCategory) -> None:
        with pytest.raises(ValueError, match="only an export defect"):
            SkipLog().record(
                "/P/X", "TAG", "reason", category=category, rule=StandardRule.SHORT_NAME_UNIQUE, effect="abort"
            )

    def test_a_defect_with_its_rule_is_recorded(self) -> None:
        entry = SkipLog().record(
            "/P/X",
            "TAG",
            "reason",
            category=SkipCategory.EXPORT_DEFECT,
            rule=StandardRule.SHORT_NAME_UNIQUE,
            effect="abort",
        )
        assert (entry.category, entry.rule) == (SkipCategory.EXPORT_DEFECT, StandardRule.SHORT_NAME_UNIQUE)

    def test_a_category_that_is_not_the_enum_raises(self) -> None:
        with pytest.raises(TypeError):
            SkipLog().record("/P/X", "TAG", "reason", category="not-supported", effect="abort")  # type: ignore[arg-type]

    def test_a_skip_reason_applies_the_same_rule(self) -> None:
        with pytest.raises(ValueError):
            SkipReason("text", SkipCategory.EXPORT_DEFECT)
        with pytest.raises(ValueError):
            SkipReason("text", SkipCategory.OUTSIDE_FILE, StandardRule.INSTANCE_REF_PATH)
        SkipReason("text", SkipCategory.EXPORT_DEFECT, StandardRule.INSTANCE_REF_PATH)

    def test_record_reason_records_the_reason_whole(self) -> None:
        log = SkipLog()
        reason = SkipReason("text", SkipCategory.EXPORT_DEFECT, StandardRule.INSTANCE_REF_PATH)
        entry = log.record_reason("/P/X", "TAG", reason, prefix="inner end: ", effect="abort")
        assert (entry.reason, entry.category, entry.rule) == (
            "inner end: text",
            SkipCategory.EXPORT_DEFECT,
            StandardRule.INSTANCE_REF_PATH,
        )
        assert log.first_at("/P/X") is entry


class TestFirstAt:
    def test_the_first_entry_at_a_path_wins(self) -> None:
        log = SkipLog()
        first = log.record("/P/X", "TAG", "first", category=SkipCategory.OUTSIDE_FILE, effect="abort")
        log.record("/P/X", "TAG", "second", category=SkipCategory.NOT_SUPPORTED, effect="abort")
        assert log.first_at("/P/X") is first

    def test_a_path_nothing_was_recorded_at_answers_none(self) -> None:
        log = SkipLog()
        log.record("/P/X", "TAG", "reason", category=SkipCategory.NOT_SUPPORTED, effect="abort")
        assert log.first_at("/P/Y") is None

    def test_the_index_takes_no_part_in_equality(self) -> None:
        """Equal entries, different indexes: a log built from its entries has indexed nothing."""
        filled = SkipLog()
        entry = filled.record("/P/X", "TAG", "reason", category=SkipCategory.NOT_SUPPORTED, effect="abort")
        built = SkipLog(entries=[entry])
        assert built.first_at("/P/X") is None and filled.first_at("/P/X") is entry
        assert built == filled


class TestChains:
    def test_record_chained_copies_the_roots_category_and_rule(self) -> None:
        log = SkipLog()
        root = log.record(
            "/P/C/conn", "ASSEMBLY-SW-CONNECTOR", "root", category=SkipCategory.EXPORT_DEFECT,
            rule=StandardRule.ONE_CONNECTOR_PER_PAIR, effect="abort",
        )  # fmt: skip
        chained = log.record_chained("/P/C/x", "TAG", "chain-skipped: root", root=root)
        assert (chained.category, chained.rule, chained.reason) == (
            SkipCategory.EXPORT_DEFECT,
            StandardRule.ONE_CONNECTOR_PER_PAIR,
            "chain-skipped: root",
        )

    def test_record_chained_to_inherits_from_an_entry_at_the_cause(self) -> None:
        log = SkipLog()
        log.record("/P/Cause", "TAG", "root", category=SkipCategory.OUTSIDE_FILE, effect="abort")
        entry = record_chained_to(log, frozenset({"/P/Cause"}), "/P/X", "TAG", "reason", cause_path="/P/Cause")
        assert (entry.category, entry.rule) == (SkipCategory.OUTSIDE_FILE, None)

    def test_record_chained_to_an_undeclared_cause_is_outside_the_file(self) -> None:
        log = SkipLog()
        entry = record_chained_to(log, frozenset({"/P/Other"}), "/P/X", "TAG", "reason", cause_path="/P/Cause")
        assert (entry.category, entry.rule) == (SkipCategory.OUTSIDE_FILE, None)

    def test_record_chained_to_a_declared_cause_with_no_entry_is_not_supported(self) -> None:
        log = SkipLog()
        entry = record_chained_to(log, frozenset({"/P/Cause"}), "/P/X", "TAG", "reason", cause_path="/P/Cause")
        assert (entry.category, entry.rule) == (SkipCategory.NOT_SUPPORTED, None)

    def test_the_chained_entry_is_indexed_like_any_other(self) -> None:
        log = SkipLog()
        entry = record_chained_to(log, frozenset(), "/P/X", "TAG", "reason", cause_path="/P/Cause")
        assert log.first_at("/P/X") is entry


# ---------------------------------------------------------------------------
# The release a file declares, and whether it is variant-rich
# ---------------------------------------------------------------------------

_NO_ATTRIBUTE = object()


def _write_arxml(tmp_path: Path, schema_location: object, body: str = "") -> Path:
    attribute = "" if schema_location is _NO_ATTRIBUTE else f' xsi:schemaLocation="{schema_location}"'
    text = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<AUTOSAR xmlns="{NAMESPACE}" xmlns:xsi="{XSI_NAMESPACE}"{attribute}>\n'
        "  <AR-PACKAGES><AR-PACKAGE><SHORT-NAME>Pkg</SHORT-NAME>"
        f"{body}</AR-PACKAGE></AR-PACKAGES>\n"
        "</AUTOSAR>\n"
    )
    path = tmp_path / "input.arxml"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


#: The ``xsi:schemaLocation`` values:
#: ``(id, attribute or _NO_ATTRIBUTE, declared_schema, declared_release)``.
SCHEMA_LOCATIONS = [
    ("4.2.2", f"{NAMESPACE} AUTOSAR_4-2-2.xsd", "AUTOSAR_4-2-2.xsd", ArxmlVersion.V4_2_2),
    ("4.3.0", f"{NAMESPACE} AUTOSAR_4-3-0.xsd", "AUTOSAR_4-3-0.xsd", ArxmlVersion.V4_3_0),
    ("upper-case-4.3.0", f"{NAMESPACE} AUTOSAR_4-3-0.XSD", "AUTOSAR_4-3-0.XSD", ArxmlVersion.V4_3_0),
    ("with-a-directory", f"{NAMESPACE} schemas/AUTOSAR_4-2-2.xsd", "AUTOSAR_4-2-2.xsd", ArxmlVersion.V4_2_2),
    ("later-release", f"{NAMESPACE} AUTOSAR_00051.xsd", "AUTOSAR_00051.xsd", None),
    ("pair-without-the-autosar-namespace", "http://example.com/other AUTOSAR_4-2-2.xsd", None, None),
    (
        "autosar-pair-second",
        f"http://example.com/other other.xsd {NAMESPACE} AUTOSAR_4-3-0.xsd",
        "AUTOSAR_4-3-0.xsd",
        ArxmlVersion.V4_3_0,
    ),
    ("sole-token", "AUTOSAR_4-3-0.xsd", "AUTOSAR_4-3-0.xsd", ArxmlVersion.V4_3_0),
    ("empty-attribute", "", None, None),
    ("no-attribute", _NO_ATTRIBUTE, None, None),
]


class TestTheDeclaredRelease:
    @pytest.mark.parametrize(
        ("schema_location", "declared_schema", "declared_release"),
        [row[1:] for row in SCHEMA_LOCATIONS],
        ids=[row[0] for row in SCHEMA_LOCATIONS],
    )
    def test_the_reader_reads_what_the_file_declares(
        self,
        tmp_path: Path,
        schema_location: object,
        declared_schema: str | None,
        declared_release: ArxmlVersion | None,
    ) -> None:
        model = read_arxml(str(_write_arxml(tmp_path, schema_location)))
        assert model.declared_schema == declared_schema
        assert model.declared_release == declared_release

    @pytest.mark.parametrize(
        "schema_location", [row[1] for row in SCHEMA_LOCATIONS], ids=[row[0] for row in SCHEMA_LOCATIONS]
    )
    def test_the_reading_equals_the_xsd_gates(self, tmp_path: Path, schema_location: object) -> None:
        """The reader's label and ``arxml_validation``'s reading agree on every row of the table.

        Both call ``arxml_version.schema_filename_from_location``, each on the
        attribute its own parser returns (``ElementTree`` here, ``lxml`` in the
        XSD gate): this holds that the two parsers hand it the same value.
        """
        path = _write_arxml(tmp_path, schema_location)
        assert read_arxml(str(path)).declared_schema == _declared_schema_filename(etree.parse(str(path)))

    def test_a_file_without_packages_still_carries_its_label(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.arxml"
        path.write_text(
            f'<AUTOSAR xmlns="{NAMESPACE}" xmlns:xsi="{XSI_NAMESPACE}" '
            f'xsi:schemaLocation="{NAMESPACE} AUTOSAR_4-2-2.xsd"/>\n',
            encoding="utf-8",
            newline="\n",
        )
        assert read_arxml(str(path)).declared_release == ArxmlVersion.V4_2_2


class TestVariantRich:
    def test_a_file_without_a_variation_point_is_not_variant_rich(self, tmp_path: Path) -> None:
        model = read_arxml(str(_write_arxml(tmp_path, f"{NAMESPACE} AUTOSAR_4-2-2.xsd")))
        assert model.variant_rich is False

    def test_a_file_carrying_one_variation_point_is_variant_rich(self, tmp_path: Path) -> None:
        body = (
            "<ELEMENTS><APPLICATION-SW-COMPONENT-TYPE><SHORT-NAME>Swc</SHORT-NAME>"
            "<VARIATION-POINT><SHORT-LABEL>vp</SHORT-LABEL></VARIATION-POINT>"
            "</APPLICATION-SW-COMPONENT-TYPE></ELEMENTS>"
        )
        model = read_arxml(str(_write_arxml(tmp_path, f"{NAMESPACE} AUTOSAR_4-2-2.xsd", body)))
        assert model.variant_rich is True

    def test_a_variation_point_outside_the_autosar_namespace_does_not_count(self, tmp_path: Path) -> None:
        body = '<VARIATION-POINT xmlns="http://example.com/other"/>'
        model = read_arxml(str(_write_arxml(tmp_path, f"{NAMESPACE} AUTOSAR_4-2-2.xsd", body)))
        assert model.variant_rich is False


class TestCite:
    def test_the_citation_names_the_files_release(self) -> None:
        assert cite(StandardRule.INSTANCE_REF_CONTEXT, ArxmlVersion.V4_2_2) == "R4.2.2 [constr_2530]"
        assert cite(StandardRule.INSTANCE_REF_PATH, ArxmlVersion.V4_3_0) == "R4.3.0 [TPS_GST_00162]"

    def test_the_one_value_that_is_not_an_id_is_cited_the_same_way(self) -> None:
        assert cite(StandardRule.C_IDENTIFIER_FORM, ArxmlVersion.V4_2_2) == "R4.2.2 [GST primitive CIdentifier]"

    def test_no_release_swcraft_writes_is_cited_from_the_fallback(self) -> None:
        assert FALLBACK_JUDGING_RELEASE == ArxmlVersion.V4_3_0
        assert cite(StandardRule.SHORT_NAME_UNIQUE, None) == "R4.3.0 [constr_2508]"


# ---------------------------------------------------------------------------
# The reader's sites: one family in section 1, the rest SWCraft's
# ---------------------------------------------------------------------------

_FOREIGN = "http://example.com/other"


def _read_minimal(tmp_path: Path, mutate) -> list[SkipEntry]:
    """Read ``minimal_swc.arxml`` after *mutate(root, behaviour)*; its skip entries."""
    tree = etree.parse(str(MINIMAL_SWC))
    root = tree.getroot()
    mutate(root, next(root.iter(f"{NS}SWC-INTERNAL-BEHAVIOR")))
    path = tmp_path / "source.arxml"
    path.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    return list(read_arxml(str(path)).skip_log)


def _add_duplicate_runnable(behaviour: etree._Element, *, variation_point: bool) -> None:
    """A second runnable of the fixture runnable's name; optionally carrying the file's one VARIATION-POINT."""
    point = "<VARIATION-POINT><SHORT-LABEL>vp</SHORT-LABEL></VARIATION-POINT>" if variation_point else ""
    add_xml(
        behaviour.find(f"{NS}RUNNABLES"),
        f"<RUNNABLE-ENTITY><SHORT-NAME>{RUNNABLE.rsplit('/', 1)[1]}</SHORT-NAME>{point}</RUNNABLE-ENTITY>",
    )


def _duplicate_entry(entries: list[SkipEntry]) -> SkipEntry:
    (entry,) = [entry for entry in entries if entry.reason.startswith("duplicate full AUTOSAR path")]
    assert (entry.path, entry.tag) == (RUNNABLE, "RUNNABLE-ENTITY")
    return entry


class TestDuplicateNameReason:
    def test_a_file_without_variation_points_breaks_the_unique_short_name_rule(self) -> None:
        reason = duplicate_name_reason("two of one name", variant_rich=False)
        assert (reason.text, reason.category, reason.rule) == (
            "two of one name",
            SkipCategory.EXPORT_DEFECT,
            StandardRule.SHORT_NAME_UNIQUE,
        )

    def test_a_variant_rich_file_is_swcrafts_and_says_why(self) -> None:
        reason = duplicate_name_reason("two of one name", variant_rich=True)
        assert (reason.text, reason.category, reason.rule) == (
            f"two of one name {VARIANT_RICH_DUPLICATE_CLAUSE}",
            SkipCategory.NOT_SUPPORTED,
            None,
        )
        assert "[" not in reason.text, "a section-2 line carries no citation in its sentence"


class TestTheReadersSites:
    """At the reader: the duplicate full path is ``[constr_2508]``, every other site is SWCraft's."""

    def test_a_duplicate_full_path_in_a_file_without_variation_points_is_an_export_defect(self, tmp_path: Path) -> None:
        entry = _duplicate_entry(
            _read_minimal(tmp_path, lambda root, b: _add_duplicate_runnable(b, variation_point=False))
        )
        assert (entry.category, entry.rule) == (SkipCategory.EXPORT_DEFECT, StandardRule.SHORT_NAME_UNIQUE)
        assert entry.reason == (
            "duplicate full AUTOSAR path (already indexed as 'RUNNABLE-ENTITY') -- malformed export, "
            "this declaration is dropped"
        )

    def test_the_same_duplicate_in_a_variant_rich_file_is_not_supported(self, tmp_path: Path) -> None:
        entry = _duplicate_entry(
            _read_minimal(tmp_path, lambda root, b: _add_duplicate_runnable(b, variation_point=True))
        )
        assert (entry.category, entry.rule) == (SkipCategory.NOT_SUPPORTED, None)
        assert entry.reason == (
            "duplicate full AUTOSAR path (already indexed as 'RUNNABLE-ENTITY') -- this declaration is dropped "
            f"{VARIANT_RICH_DUPLICATE_CLAUSE}"
        )

    def test_an_element_outside_the_autosar_namespace_stays_not_supported(self, tmp_path: Path) -> None:
        def mutate(root: etree._Element, behaviour: etree._Element) -> None:
            behaviour.append(etree.Element(f"{{{_FOREIGN}}}Vendor"))

        entries = _read_minimal(tmp_path, mutate)
        (entry,) = [entry for entry in entries if entry.tag == f"{{{_FOREIGN}}}Vendor"]
        assert (entry.category, entry.rule) == (SkipCategory.NOT_SUPPORTED, None)

    def test_an_unrecognized_element_tag_stays_not_supported(self, tmp_path: Path) -> None:
        def mutate(root: etree._Element, behaviour: etree._Element) -> None:
            add_xml(
                next(root.iter(f"{NS}ELEMENTS")),
                "<SENSOR-ACTUATOR-SW-COMPONENT-TYPE><SHORT-NAME>Sensor</SHORT-NAME></SENSOR-ACTUATOR-SW-COMPONENT-TYPE>",
            )

        entries = _read_minimal(tmp_path, mutate)
        (entry,) = [entry for entry in entries if entry.tag == "SENSOR-ACTUATOR-SW-COMPONENT-TYPE"]
        assert entry.reason == "unrecognized element tag inside ELEMENTS"
        assert (entry.category, entry.rule) == (SkipCategory.NOT_SUPPORTED, None)

    def test_an_unread_behaviour_child_stays_not_supported(self, tmp_path: Path) -> None:
        def mutate(root: etree._Element, behaviour: etree._Element) -> None:
            add_xml(
                behaviour,
                "<PER-INSTANCE-PARAMETERS><PARAMETER-DATA-PROTOTYPE><SHORT-NAME>Parameter</SHORT-NAME>"
                "</PARAMETER-DATA-PROTOTYPE></PER-INSTANCE-PARAMETERS>",
            )

        entries = _read_minimal(tmp_path, mutate)
        (entry,) = [entry for entry in entries if entry.tag == "PARAMETER-DATA-PROTOTYPE"]
        assert entry.path == f"{BEHAVIOUR}/Parameter"
        assert (entry.category, entry.rule) == (SkipCategory.NOT_SUPPORTED, None)


# ---------------------------------------------------------------------------
# The type, interface and component mappers: the declared / outside-the-file
# splits, the duplicate names, and the cited mapper defects
# ---------------------------------------------------------------------------

_SWC_PATH = "/Acme/Components/Ctrl"
_BASE = "/Acme/Types/uint8"
_COUNTER = "/Acme/Types/Counter"
_INIT = "/Acme/Constants/SigInit"


def _swc_model(
    *,
    ports: list[SwcPortInfo] | None = None,
    runnables: list[SwcRunnableInfo] | None = None,
    events: list[SwcEventInfo] | None = None,
    variant_rich: bool = False,
) -> ImportModel:
    """One component at :data:`_SWC_PATH` (its path in the path index) and nothing else."""
    model = ImportModel(source_path="inline-test", variant_rich=variant_rich)
    model.swcs.append(
        SwcInfo(
            path=_SWC_PATH,
            name="Ctrl",
            description="",
            ports=ports or [],
            runnables=runnables or [],
            events=events or [],
        )
    )
    model.path_index[_SWC_PATH] = raw_element(_SWC_PATH)
    return model


def _runnable(name: str = "Run", accesses: list[SwcAccessInfo] | None = None) -> SwcRunnableInfo:
    return SwcRunnableInfo(
        path=f"{_SWC_PATH}/IB/{name}", name=name, min_start_interval=0.0, description="", accesses=accesses or []
    )


def _write_access(port_ref: str) -> SwcAccessInfo:
    return SwcAccessInfo(
        path=f"{_SWC_PATH}/IB/Run/W_value",
        name="W_value",
        kind="DATA-SEND-POINTS",
        tag="VARIABLE-ACCESS",
        port_ref=port_ref,
        target_ref="/Acme/Interfaces/ISig/value",
    )


def _operation_event(port_ref: str) -> SwcEventInfo:
    return SwcEventInfo(
        path=f"{_SWC_PATH}/IB/OnCall",
        name="OnCall",
        kind="OPERATION-INVOKED-EVENT",
        runnable_ref=f"{_SWC_PATH}/IB/Run",
        period=None,
        context_port_ref=port_ref,
        target_operation_ref="/Acme/Interfaces/IServe/Ping",
    )


def _one_swc_skip(model: ImportModel, path: str) -> SkipEntry:
    _swcs, skips = map_swcs(model, interfaces=[], port_inits={}, variable_types={})
    (entry,) = [entry for entry in skips if entry.path == path]
    return entry


def _declared_elsewhere_by_the_reader(model: ImportModel, path: str) -> None:
    """*path* declared in the file under a kind the reader reports and does not hand on."""
    model.skip_log.record(
        path, "UNKNOWN-PORT-PROTOTYPE", "unrecognized port kind inside PORTS", category=_NS, effect="abort"
    )


_NS = SkipCategory.NOT_SUPPORTED


class TestAnAccessOrEventWhosePortIsNoPortOfItsComponent:
    """``port_by_path`` holds every raw port, so the split is by where the reference points."""

    @pytest.mark.parametrize("subject", ["access", "event"])
    def test_a_port_under_its_component_declared_nowhere_is_outside_the_file(self, subject: str) -> None:
        port_ref = f"{_SWC_PATH}/pGhost"
        model, path = self._model(subject, port_ref)
        entry = _one_swc_skip(model, path)
        assert (entry.category, entry.rule) == (SkipCategory.OUTSIDE_FILE, None)
        assert entry.reason == f"port 'pGhost' is not declared on SWC 'Ctrl' (reference {port_ref})"

    @pytest.mark.parametrize("subject", ["access", "event"])
    def test_a_port_under_another_parent_breaks_the_context_rule(self, subject: str) -> None:
        port_ref = "/Acme/Components/Other/pSig"
        model, path = self._model(subject, port_ref)
        entry = _one_swc_skip(model, path)
        assert (entry.category, entry.rule) == (SkipCategory.EXPORT_DEFECT, StandardRule.INSTANCE_REF_CONTEXT)
        noun = "writes access" if subject == "access" else "operation event"
        assert entry.reason == (
            f"{noun} names the port {port_ref}, which is not a port of its own SWC 'Ctrl' ({_SWC_PATH})"
        )

    @pytest.mark.parametrize("subject", ["access", "event"])
    def test_a_port_under_its_component_the_reader_did_not_hand_on_is_swcrafts(self, subject: str) -> None:
        port_ref = f"{_SWC_PATH}/pOdd"
        model, path = self._model(subject, port_ref)
        _declared_elsewhere_by_the_reader(model, port_ref)
        entry = _one_swc_skip(model, path)
        assert (entry.category, entry.rule) == (_NS, None)
        assert entry.reason == f"port 'pOdd' is declared on SWC 'Ctrl' but was not imported (reference {port_ref})"

    @staticmethod
    def _model(subject: str, port_ref: str) -> tuple[ImportModel, str]:
        if subject == "access":
            access = _write_access(port_ref)
            return _swc_model(runnables=[_runnable(accesses=[access])]), access.path
        event = _operation_event(port_ref)
        return _swc_model(runnables=[_runnable()], events=[event]), event.path


class TestAPortWhoseInterfaceDidNotResolve:
    """The port-interface split, ``import component``'s half: no policy is passed."""

    def test_an_interface_the_file_declares_nowhere_is_outside_the_file(self) -> None:
        port = SwcPortInfo(
            path=f"{_SWC_PATH}/rSig", name="rSig", kind="R-PORT-PROTOTYPE", interface_ref="/Vendor/ISig", description=""
        )
        entry = _one_swc_skip(_swc_model(ports=[port]), port.path)
        assert (entry.category, entry.rule) == (SkipCategory.OUTSIDE_FILE, None)
        assert entry.reason == "unresolved reference /Vendor/ISig (the file declares no interface at that path)"

    def test_a_declared_interface_swcraft_skipped_is_the_chain_half(self) -> None:
        port = SwcPortInfo(
            path=f"{_SWC_PATH}/rMode",
            name="rMode",
            kind="R-PORT-PROTOTYPE",
            interface_ref="/Acme/IMode",
            description="",
        )
        model = _swc_model(ports=[port])
        model.skip_log.record(
            "/Acme/IMode",
            "MODE-SWITCH-INTERFACE",
            "unrecognized element tag inside ELEMENTS",
            category=_NS,
            effect="abort",
        )
        entry = _one_swc_skip(model, port.path)
        assert (entry.category, entry.rule) == (_NS, None)
        assert entry.reason == "unresolved reference /Acme/IMode (interface was skipped)"


def _sig_types(model: ImportModel) -> None:
    model.sw_base_types.append(
        SwBaseTypeInfo(path=_BASE, name="uint8", size=8, encoding="NONE", native_declaration="uint8", description="")
    )
    model.impl_data_types.append(
        ImplDataTypeInfo(
            path=_COUNTER, name="Counter", category="VALUE", base_type_ref=_BASE, impl_type_ref=None, description=""
        )
    )
    model.constant_specs.append(
        ConstantSpecInfo(path=_INIT, name="SigInit", value=ConstantScalar(value=3), unreadable_reason=None)
    )


def _com_spec_model(*, trees: dict[str, str], port_interface: str, element_ref: str) -> ImportModel:
    """``ISig`` once per entry of *trees* (package → element name), and one port COM-SPEC naming *element_ref*."""
    model = ImportModel(source_path="inline-test")
    _sig_types(model)
    for tree, element in trees.items():
        model.sr_interfaces.append(
            SRInterfaceInfo(
                path=f"/Acme/{tree}/ISig",
                name="ISig",
                description="",
                data_elements=[SRDataElementInfo(name=element, type_ref=_COUNTER)],
            )
        )
    model.swcs.append(
        SwcInfo(
            path=_SWC_PATH,
            name="Ctrl",
            description="",
            ports=[
                SwcPortInfo(
                    path=f"{_SWC_PATH}/rSig",
                    name="rSig",
                    kind="R-PORT-PROTOTYPE",
                    interface_ref=port_interface,
                    description="",
                    com_spec_inits=[
                        PortComSpecInitInfo(
                            port_path=f"{_SWC_PATH}/rSig",
                            com_spec_tag="NONQUEUED-RECEIVER-COM-SPEC",
                            data_element_ref=element_ref,
                            constant_ref=_INIT,
                            unreadable_reason=None,
                        )
                    ],
                )
            ],
            runnables=[],
            events=[],
        )
    )
    for declaration in (*model.sr_interfaces, *model.swcs):
        model.path_index[declaration.path] = raw_element(declaration.path)
    return model


def _com_spec_entry(model: ImportModel, decisions) -> SkipEntry:
    unresolved = UnresolvedReferences(model, decisions)
    forms: list = []
    data_types, registry, _ = map_data_types(model, merges=decisions, unresolved=unresolved, elements=forms)
    _interfaces, skips = map_interfaces(
        model, registry, data_types, merges=decisions, unresolved=unresolved, elements=forms
    )
    (entry,) = [entry for entry in skips if entry.path.endswith("/REQUIRED-COM-SPECS") and entry.tag == "INIT-VALUE"]
    return entry


def _cause_of(entry: SkipEntry) -> str:
    """The parenthesis after *"mapped no data element under that path"*."""
    return entry.reason.split("under that path (", 1)[1].split("), so there is no data element", 1)[0]


class TestAnUnreadPortComSpecInitNamesOneCause:
    """The COM-spec init split: each half its own sentence, and SWCraft's halves stay section 2."""

    def test_a_conflicting_interface_is_swcrafts(self) -> None:
        model = _com_spec_model(
            trees={"TreeA": "alpha", "TreeB": "beta"},
            port_interface="/Acme/TreeB/ISig",
            element_ref="/Acme/TreeB/ISig/beta",
        )
        entry = _com_spec_entry(model, decide_merges(model))
        assert (entry.category, entry.rule) == (_NS, None)
        assert _cause_of(entry) == (
            "its interface '/Acme/TreeB/ISig' is one of several conflicting declarations of one name, "
            "none of which was imported"
        )

    def test_an_interface_the_selection_left_out_is_swcrafts(self) -> None:
        model = _com_spec_model(
            trees={"TreeA": "alpha", "TreeB": "beta"},
            port_interface="/Acme/TreeB/ISig",
            element_ref="/Acme/TreeB/ISig/beta",
        )
        decisions = narrow_to_selection(
            decide_merges(model), selected=frozenset({"/Acme/TreeA/ISig", _SWC_PATH}), reached=frozenset()
        )
        entry = _com_spec_entry(model, decisions)
        assert (entry.category, entry.rule) == (_NS, None)
        assert _cause_of(entry) == "its interface '/Acme/TreeB/ISig' is a declaration the package selection left out"

    def test_a_merged_groups_other_member_is_swcrafts(self) -> None:
        model = _com_spec_model(
            trees={"TreeA": "sig", "TreeB": "sig"},
            port_interface="/Acme/TreeB/ISig",
            element_ref="/Acme/TreeB/ISig/sig",
        )
        entry = _com_spec_entry(model, decide_merges(model))
        assert (entry.category, entry.rule) == (_NS, None)
        assert _cause_of(entry) == (
            "its interface '/Acme/TreeB/ISig' was merged with another declaration of the same name and the "
            "element was mapped under that one"
        )

    def test_an_imported_interface_without_that_element_is_swcrafts(self) -> None:
        model = _com_spec_model(
            trees={"TreeA": "sig"}, port_interface="/Acme/TreeA/ISig", element_ref="/Acme/TreeA/ISig/ghost"
        )
        entry = _com_spec_entry(model, decide_merges(model))
        assert (entry.category, entry.rule) == (_NS, None)
        assert _cause_of(entry) == (
            "its interface '/Acme/TreeA/ISig' was imported and declares no data element of that name"
        )

    def test_without_a_policy_an_undeclared_interface_is_outside_the_file(self) -> None:
        model = _com_spec_model(trees={}, port_interface="/Vendor/ISig", element_ref="/Vendor/ISig/value")
        forms: list = []
        data_types, registry, _ = map_data_types(model, elements=forms)
        _interfaces, skips = map_interfaces(model, registry, data_types, elements=forms)
        (entry,) = [
            entry for entry in skips if entry.path.endswith("/REQUIRED-COM-SPECS") and entry.tag == "INIT-VALUE"
        ]
        assert (entry.category, entry.rule) == (SkipCategory.OUTSIDE_FILE, None)
        assert _cause_of(entry) == "the file declares no interface at '/Vendor/ISig'"


class TestADuplicateNameUnderOneParent:
    """``[constr_2508]`` for a duplicate port or runnable, unless the file is variant-rich."""

    @staticmethod
    def _ports() -> list[SwcPortInfo]:
        return [
            SwcPortInfo(
                path=f"{_SWC_PATH}/pSig", name="pSig", kind="P-PORT-PROTOTYPE", interface_ref=None, description=""
            )
            for _ in range(2)
        ]

    @pytest.mark.parametrize("variant_rich", [False, True])
    def test_a_duplicate_port_on_a_component(self, variant_rich: bool) -> None:
        _swcs, skips = map_swcs(
            _swc_model(ports=self._ports(), variant_rich=variant_rich), interfaces=[], port_inits={}, variable_types={}
        )
        (entry,) = [entry for entry in skips if entry.reason.startswith("duplicate port name")]
        self._assert_duplicate(entry, "duplicate port name 'pSig' on SWC 'Ctrl'", variant_rich=variant_rich)

    @pytest.mark.parametrize("variant_rich", [False, True])
    def test_map_ports_reads_its_keyword(self, variant_rich: bool) -> None:
        """The keyword ``map_compositions`` passes for a boundary port, answered the same way."""
        skip_log = SkipLog()
        map_ports(
            "Outer",
            self._ports(),
            {},
            skip_log,
            variant_rich=variant_rich,
            declared=frozenset(),
            chain_logs=(),
            port_inits={},
        )
        (entry,) = [entry for entry in skip_log if entry.reason.startswith("duplicate port name")]
        self._assert_duplicate(entry, "duplicate port name 'pSig' on SWC 'Outer'", variant_rich=variant_rich)

    @pytest.mark.parametrize("variant_rich", [False, True])
    def test_a_duplicate_runnable(self, variant_rich: bool) -> None:
        model = _swc_model(runnables=[_runnable(), _runnable()], variant_rich=variant_rich)
        _swcs, skips = map_swcs(model, interfaces=[], port_inits={}, variable_types={})
        (entry,) = [entry for entry in skips if entry.tag == "RUNNABLE-ENTITY"]
        self._assert_duplicate(entry, "duplicate runnable name 'Run' on SWC 'Ctrl'", variant_rich=variant_rich)

    @staticmethod
    def _assert_duplicate(entry: SkipEntry, text: str, *, variant_rich: bool) -> None:
        if variant_rich:
            assert (entry.category, entry.rule) == (_NS, None)
            assert entry.reason == f"{text} {VARIANT_RICH_DUPLICATE_CLAUSE}"
        else:
            assert (entry.category, entry.rule) == (SkipCategory.EXPORT_DEFECT, StandardRule.SHORT_NAME_UNIQUE)
            assert entry.reason == text


class TestAPortWithNoInterfaceReferenceStaysSwcrafts:
    """Section 2, with the reason the standard gives, and no citation (section 2 carries none).

    The standardized schema leaves the reference's minimum multiplicity
    unenforced so that a file may carry partial information
    (``[TPS_GST_00057]``), so the file breaks no rule.
    """

    REASON = (
        "port has no interface reference -- the standard lets a file leave it out (partial information), "
        "and SWCraft cannot import a port without an interface"
    )

    @pytest.mark.parametrize("kind", ["P-PORT-PROTOTYPE", "R-PORT-PROTOTYPE"])
    def test_a_port_with_no_interface_reference(self, kind: str) -> None:
        port = SwcPortInfo(path=f"{_SWC_PATH}/pSig", name="pSig", kind=kind, interface_ref=None, description="")
        entry = _one_swc_skip(_swc_model(ports=[port]), port.path)
        assert entry.reason == self.REASON
        assert (entry.category, entry.rule) == (_NS, None)


def _gear_enum(model: ImportModel) -> None:
    """``Gear``, an application enumeration of the two literals ``DRIVE`` = 1 and ``REVERSE`` = 2."""
    model.compu_methods.append(
        CompuMethodInfo(
            path="/Pkg/CompuMethods/Gear_CompuMethod",
            name="Gear_CompuMethod",
            category="TEXTTABLE",
            scales=[CompuScaleInfo(value=1, literal="DRIVE"), CompuScaleInfo(value=2, literal="REVERSE")],
            description="",
        )
    )
    model.app_primitive_data_types.append(
        AppPrimitiveDataTypeInfo(
            path="/Pkg/AppTypes/Gear",
            name="Gear",
            compu_method_ref="/Pkg/CompuMethods/Gear_CompuMethod",
            description="",
        )
    )


class TestAnEnumInitThatMatchesNoLiteralIsCited:
    """``[constr_1221]`` asks an enumeration's initial value to match one of its literals.

    A number one single scale of the file's table names is read as that literal. A prototype's own value
    given as a number no scale names is section 1 (``[constr_1221]``); a member given as such a number is
    SWCraft's own line (section 2); the two-literals case is never asked of the mapper.
    """

    @staticmethod
    def _init_entry(model: ImportModel) -> SkipEntry:
        init, skips = _one_init(model)
        assert init is None
        (entry,) = list(skips)
        assert (entry.path, entry.tag) == ("/Pkg/Interfaces/IValue/value", "INIT-VALUE")
        return entry

    def test_the_elements_own_init(self) -> None:
        model = _model_with_types()
        _gear_enum(model)
        _with_interface(model, type_ref="/Pkg/AppTypes/Gear", init_ref="/Pkg/Constants/SR_IValue_value_Init")
        _constant(model, ConstantScalar(value=7))
        entry = self._init_entry(model)
        assert entry.reason == (
            "the init value is not carried: not an application value whose text names one scale of its "
            "enumeration, nor a number its table reads as one (1 leaf)"
        )
        assert (entry.category, entry.rule) == (_ED, StandardRule.APPLICATION_VALUE_FOR_AN_APPLICATION_PRIMITIVE)

    def test_an_enum_nested_in_a_records_field(self) -> None:
        model = _model_with_types()
        _gear_enum(model)
        model.app_record_data_types.append(
            AppRecordDataTypeInfo(
                path="/Pkg/AppTypes/GearState",
                name="GearState",
                elements=[RecordElementInfo(name="gear", type_ref="/Pkg/AppTypes/Gear")],
                description="",
            )
        )
        _with_interface(model, type_ref="/Pkg/AppTypes/GearState", init_ref="/Pkg/Constants/SR_IValue_value_Init")
        _constant(model, ConstantRecord(fields=[ConstantRecordField(label=None, value=ConstantScalar(value=7))]))
        entry = self._init_entry(model)
        assert entry.reason == (
            "the init value is not carried: an enumeration member given as a number, which no one single-point "
            "scale of a whole table without a SYMBOL names (1 leaf)"
        )
        assert (entry.category, entry.rule) == (SkipCategory.NOT_SUPPORTED, None)

    def test_two_literals_of_the_value_are_never_asked(self) -> None:
        """Two literals of the value are never asked, so the line is SWCraft's.

        No ARXML route reaches it (``texttable_defect`` refuses the table first), so asked directly: the mapper reads no
        number as a literal (only the walk reads one, through the file's table), so the ambiguity is not met."""
        ambiguous = EnumDataType(
            name="Gear",
            kind="enum",
            literals=[EnumLiteral(name="DRIVE", value=1), EnumLiteral(name="FORWARD", value=1)],
        )
        mapped, reason = _map_init_value(
            ConstantScalar(value=1), "Gear", DataDescription.last_declared([ambiguous]), "init", application=True
        )
        assert mapped is None
        assert reason == SkipReason(
            "init: enum type 'Gear' takes the name of one of its literals, and SWCraft reads the number 1 as a "
            "literal only through the file's own table",
            SkipCategory.NOT_SUPPORTED,
        )


_BASE_REF = "/Pkg/Base/float32"


def _base_typed_everywhere() -> ImportModel:
    """``_a_base_type_named_where_an_implementation_type_belongs``, plus an operation argument and a port.

    The base type is named by a data element (``/Pkg/If/IRaw``), a record
    field (``/Pkg/App/Rec``), an array element (``/Pkg/App/Arr``), a
    ``TYPE_REFERENCE``'s ``IMPLEMENTATION-DATA-TYPE-REF`` (``/Pkg/Types/Ref``)
    and here an argument (``/Pkg/If/ICall``); ``/Pkg/Swc/Ctrl`` requires
    ``IRaw``.
    """
    model = _a_base_type_named_where_an_implementation_type_belongs()
    model.cs_interfaces.append(
        CSInterfaceInfo(
            path="/Pkg/If/ICall",
            name="ICall",
            description="",
            operations=[
                CSOperationInfo(name="Op", arguments=[CSArgumentInfo(name="a", type_ref=_BASE_REF, direction="IN")])
            ],
        )
    )
    model.swcs.append(
        SwcInfo(
            path="/Pkg/Swc/Ctrl",
            name="Ctrl",
            description="",
            ports=[
                SwcPortInfo(
                    path="/Pkg/Swc/Ctrl/rRaw",
                    name="rRaw",
                    kind="R-PORT-PROTOTYPE",
                    interface_ref="/Pkg/If/IRaw",
                    description="",
                )
            ],
            runnables=[],
            events=[],
        )
    )
    for path in ("/Pkg/If/IRaw", "/Pkg/If/ICall", "/Pkg/Swc/Ctrl"):
        model.path_index[path] = raw_element(path)
    return model


class TestADataPrototypeTypedByABaseTypeIsCited:
    """``[TPS_GST_00020]`` -- a reference names its destination type, and a SW-BASE-TYPE is none of them.

    ``AutosarDataPrototype.type`` is an ``AutosarDataType``,
    ``ApplicationCompositeElementDataPrototype.type`` an ``ApplicationDataType``
    and ``SwDataDefProps.implementationDataType`` an ``ImplementationDataType``
    (the class tables, both releases); a ``SwBaseType`` is none of the three.
    """

    DATA_PROTOTYPE = (
        f"references the SW-BASE-TYPE {_BASE_REF} directly -- a data prototype's type is an AutosarDataType "
        "(an application or implementation data type), which a SW-BASE-TYPE is not"
    )

    @staticmethod
    def _mapped() -> tuple[ImportModel, SkipLog, SkipLog]:
        model = _base_typed_everywhere()
        forms: list = []
        data_types, registry, type_skips = map_data_types(model, elements=forms)
        _interfaces, iface_skips = map_interfaces(model, registry, data_types, elements=forms)
        return model, type_skips, iface_skips

    @pytest.mark.parametrize(
        ("path", "subject"),
        [("/Pkg/If/IRaw", "data element 'raw'"), ("/Pkg/If/ICall", "operation 'Op' argument 'a'")],
        ids=["data-element", "argument"],
    )
    def test_a_data_element_or_an_argument(self, path: str, subject: str) -> None:
        _model, _type_skips, iface_skips = self._mapped()
        entry = iface_skips.first_at(path)
        assert entry is not None
        assert entry.reason == f"{subject}: {self.DATA_PROTOTYPE}"
        assert (entry.category, entry.rule) == (_ED, StandardRule.REFERENCE_DESTINATION_TYPE)

    @pytest.mark.parametrize(
        ("path", "reason"),
        [
            (
                "/Pkg/App/Rec",
                f"field 'x' is typed by the SW-BASE-TYPE {_BASE_REF} directly -- a record element's type is an "
                "ApplicationDataType, which a SW-BASE-TYPE is not",
            ),
            (
                "/Pkg/App/Arr",
                f"element typed by the SW-BASE-TYPE {_BASE_REF} directly -- an array element's type is an "
                "ApplicationDataType, which a SW-BASE-TYPE is not",
            ),
            (
                "/Pkg/Types/Ref",
                f"TYPE_REFERENCE names the SW-BASE-TYPE {_BASE_REF}; a reference type rests on an implementation "
                "data type, never on a base type",
            ),
        ],
        ids=["record-field", "array-element", "implementation-type-reference"],
    )
    def test_a_record_field_an_array_element_and_an_implementation_reference(self, path: str, reason: str) -> None:
        _model, type_skips, _iface_skips = self._mapped()
        entry = type_skips.first_at(path)
        assert entry is not None
        assert entry.reason == reason
        assert (entry.category, entry.rule) == (_ED, StandardRule.REFERENCE_DESTINATION_TYPE)

    def test_the_port_on_the_skipped_interface_carries_the_rule(self) -> None:
        model, _type_skips, iface_skips = self._mapped()
        forms: list = []
        data_types, registry, _ = map_data_types(model, elements=forms)
        interfaces, iface_skips = map_interfaces(model, registry, data_types, elements=forms)
        _swcs, swc_skips = map_swcs(model, interfaces, iface_skip_log=iface_skips, port_inits={}, variable_types={})
        entry = swc_skips.first_at("/Pkg/Swc/Ctrl/rRaw")
        assert entry is not None
        assert entry.reason == "unresolved reference /Pkg/If/IRaw (interface was skipped)"
        assert (entry.category, entry.rule) == (_ED, StandardRule.REFERENCE_DESTINATION_TYPE)


_SHARED_IMPL_REF = "/Pkg/Impl/ProbeShared"


def _composites_typed_by_an_implementation_type(*, same_name_application_type: bool) -> ImportModel:
    """A record field and an array element typed by the IMPLEMENTATION-DATA-TYPE ``ProbeShared``.

    ``ProbeShared`` is a ``VALUE`` implementation type over a base type, so it
    registers in the implementation layer. With *same_name_application_type* an
    APPLICATION-PRIMITIVE-DATA-TYPE of the same name is declared beside it (no
    mapping set pairs the two, so both are kept): the shape in which writing the
    member's *name* would bind it to the application type of that name.
    """
    model = ImportModel(source_path="inline-test")
    model.sw_base_types.append(
        SwBaseTypeInfo(
            path="/Pkg/Base/ProbeOctet",
            name="ProbeOctet",
            size=8,
            encoding="NONE",
            native_declaration="uint8",
            description="",
        )
    )
    model.impl_data_types.append(
        ImplDataTypeInfo(
            path=_SHARED_IMPL_REF,
            name="ProbeShared",
            category="VALUE",
            base_type_ref="/Pkg/Base/ProbeOctet",
            impl_type_ref=None,
            description="",
        )
    )
    if same_name_application_type:
        model.app_primitive_data_types.append(
            AppPrimitiveDataTypeInfo(
                path="/Pkg/App/ProbeShared", name="ProbeShared", compu_method_ref=None, description=""
            )
        )
    model.app_record_data_types.append(
        AppRecordDataTypeInfo(
            path="/Pkg/App/ProbeRec",
            name="ProbeRec",
            elements=[RecordElementInfo(name="x", type_ref=_SHARED_IMPL_REF)],
            description="",
        )
    )
    model.app_array_data_types.append(
        AppArrayDataTypeInfo(
            path="/Pkg/App/ProbeArr",
            name="ProbeArr",
            element_type_ref=_SHARED_IMPL_REF,
            max_number_of_elements=2,
            description="",
        )
    )
    return model


class TestACompositeMemberTypedByAnImplementationTypeIsCited:
    """A record field or an array element typed by an IMPLEMENTATION-DATA-TYPE is refused.

    The sibling of :class:`TestADataPrototypeTypedByABaseTypeIsCited`'s composite
    rows: an ``ApplicationCompositeElementDataPrototype``'s type is an
    ``ApplicationDataType`` (``[TPS_GST_00020]``'s destination type), which an
    ``ImplementationDataType`` is not. The refusal is asked of the path's layer
    (``registry.layer_of``), so it must fire whether or not an application type of
    the implementation type's name exists -- were the member written as
    ``type: ProbeShared`` it would bind to that application type silently.
    """

    @pytest.mark.parametrize("same_name_application_type", [False, True], ids=["alone", "beside-same-name-app"])
    @pytest.mark.parametrize(
        ("path", "tag", "reason"),
        [
            (
                "/Pkg/App/ProbeRec",
                "APPLICATION-RECORD-DATA-TYPE",
                f"field 'x' is typed by the IMPLEMENTATION-DATA-TYPE {_SHARED_IMPL_REF} -- a record element's type "
                "is an ApplicationDataType, which an ImplementationDataType is not",
            ),
            (
                "/Pkg/App/ProbeArr",
                "APPLICATION-ARRAY-DATA-TYPE",
                f"element typed by the IMPLEMENTATION-DATA-TYPE {_SHARED_IMPL_REF} -- an array element's type is "
                "an ApplicationDataType, which an ImplementationDataType is not",
            ),
        ],
        ids=["record-field", "array-element"],
    )
    def test_the_composite_is_skipped_with_the_cited_reason(
        self, path: str, tag: str, reason: str, same_name_application_type: bool
    ) -> None:
        model = _composites_typed_by_an_implementation_type(same_name_application_type=same_name_application_type)

        data_types, _registry, type_skips = map_data_types(model, elements=[])

        entry = type_skips.first_at(path)
        assert entry is not None
        assert (entry.tag, entry.reason) == (tag, reason)
        assert (entry.category, entry.rule) == (_ED, StandardRule.REFERENCE_DESTINATION_TYPE)
        assert path.rsplit("/", 1)[-1] not in {dt.name for dt in data_types}

    def test_the_implementation_type_and_its_same_name_application_type_are_still_imported(self) -> None:
        """The refusal is the composite's alone: neither member of the pair is touched by it."""
        data_types, _registry, _type_skips = map_data_types(
            _composites_typed_by_an_implementation_type(same_name_application_type=True), elements=[]
        )

        assert sorted((dt.kind, dt.name) for dt in data_types if dt.name == "ProbeShared") == [
            ("application_type", "ProbeShared"),
            ("value_type", "ProbeShared"),
        ]


# ---------------------------------------------------------------------------
# Connectors and compositions: the section-1 families on a connector or a
# delegation, the outside-the-file halves, and SWCraft's own name spaces
# staying section 2
# ---------------------------------------------------------------------------

_COMP = "/Pkg/Compositions/Comp1"
_PROTO_A = f"{_COMP}/SwcA_1"
_PROTO_B = f"{_COMP}/SwcB_1"
_PORT_A = "/Pkg/Components/SwcA/pA"
_PORT_B = "/Pkg/Components/SwcB/pB"
_STUB_TYPE = "/Vendor/Ext/ExtSwc"
_STUB_PROTO = f"{_COMP}/Ext"
_ED = SkipCategory.EXPORT_DEFECT
_OF = SkipCategory.OUTSIDE_FILE


def _with_cs_interface(model: ImportModel) -> None:
    """``IServe``, one client-server interface of one operation, beside the shared ``ISig``."""
    model.cs_interfaces.append(
        CSInterfaceInfo(
            path="/Pkg/Interfaces/IServe",
            name="IServe",
            description="",
            operations=[
                CSOperationInfo(
                    name="Ping", arguments=[CSArgumentInfo(name="arg", type_ref="/Pkg/Types/Sig_impl", direction="IN")]
                )
            ],
        )
    )


def _end(component_ref: str, port_ref: str, dest: str | None = None, stated: str | None = None) -> ConnectorPortRef:
    return ConnectorPortRef(component_ref=component_ref, port_ref=port_ref, dest=dest, stated_direction=stated)


def _assembly(
    name: str, provider: ConnectorPortRef, requester: ConnectorPortRef, *, comp: str = _COMP
) -> AssemblyConnectorInfo:
    return AssemblyConnectorInfo(path=f"{comp}/{name}", name=name, provider=provider, requester=requester)


def _delegation(name: str, outer_port_ref: str | None, inner: ConnectorPortRef) -> DelegationConnectorInfo:
    return DelegationConnectorInfo(path=f"{_COMP}/{name}", name=name, inner=inner, outer_port_ref=outer_port_ref)


def _two_swcs(
    kind_a: str = "P-PORT-PROTOTYPE",
    kind_b: str = "R-PORT-PROTOTYPE",
    *,
    interface_a: str = "/Pkg/Interfaces/ISig",
    interface_b: str = "/Pkg/Interfaces/ISig",
) -> ImportModel:
    """``SwcA`` (port ``pA``) and ``SwcB`` (port ``pB``), each port on the interface given."""
    model, _paths = _base_inline_model()
    _with_cs_interface(model)
    _add_swc(model, "/Pkg/Components/SwcA", "SwcA", "pA", kind_a, interface_ref=interface_a)
    _add_swc(model, "/Pkg/Components/SwcB", "SwcB", "pB", kind_b, interface_ref=interface_b)
    return model


def _comp1(
    model: ImportModel,
    *,
    ports: list[SwcPortInfo] | None = None,
    prototypes: list[ComponentPrototypeInfo] | None = None,
    assembly: list[AssemblyConnectorInfo] | None = None,
    delegations: list[DelegationConnectorInfo] | None = None,
) -> None:
    """``Comp1``, the one root, placing ``SwcA`` and ``SwcB`` unless told otherwise."""
    model.compositions.append(
        _composition_info(
            _COMP,
            "Comp1",
            ports=ports,
            prototypes=prototypes
            if prototypes is not None
            else [
                _prototype(_PROTO_A, "SwcA_1", "/Pkg/Components/SwcA"),
                _prototype(_PROTO_B, "SwcB_1", "/Pkg/Components/SwcB"),
            ],
            assembly=assembly,
            delegations=delegations,
        )
    )


def _composition_skip(model: ImportModel, path: str) -> SkipEntry:
    _mapped, skips, _registry, _swcs = _map_full(model)
    (entry,) = [entry for entry in skips if entry.path == path]
    return entry


def _boundary_port(name: str, kind: str = "P-PORT-PROTOTYPE") -> SwcPortInfo:
    return SwcPortInfo(
        path=f"{_COMP}/{name}", name=name, kind=kind, interface_ref="/Pkg/Interfaces/ISig", description=""
    )


def _every_skip(model: ImportModel) -> list[SkipEntry]:
    """Every entry the five mappers record over *model*, with no merge or unresolved-reference policy."""
    forms: list = []
    data_types, registry, type_skips = map_data_types(model, elements=forms)
    _sets, set_skips = map_mapping_sets(model, registry, data_types)
    interfaces, iface_skips = map_interfaces(model, registry, data_types, elements=forms)
    swcs, swc_skips = map_swcs(model, interfaces, iface_skip_log=iface_skips, port_inits={}, variable_types={})
    _mapped, composition_skips = map_compositions(
        model, swcs, interfaces, iface_skips, registry, swc_skip_log=swc_skips
    )
    return [*type_skips, *set_skips, *iface_skips, *swc_skips, *composition_skips]


_BLANK = "missing or blank SHORT-NAME"
_IMPL = "/Pkg/Types/Sig_impl"


def _blank_composition(model: ImportModel) -> None:
    """Placed inside ``Comp1``, so the root survives and the import goes on."""
    _comp1(
        model,
        prototypes=[
            _prototype(_PROTO_A, "SwcA_1", "/Pkg/Components/SwcA"),
            _prototype(f"{_COMP}/Blank_1", "Blank_1", "/Pkg/Compositions/", dest=COMPOSITION_DEST),
        ],
    )
    model.compositions.append(_composition_info("/Pkg/Compositions/", ""))


def _blank_prototype(model: ImportModel) -> None:
    _comp1(model, prototypes=[_prototype(f"{_COMP}/", "", "/Pkg/Components/SwcA")])


def _blank_connector(model: ImportModel) -> None:
    _comp1(model, assembly=[_assembly("", _end(_PROTO_A, _PORT_A), _end(_PROTO_B, _PORT_B))])


def _blank_delegation(model: ImportModel) -> None:
    _comp1(
        model,
        ports=[_boundary_port("pOuter")],
        delegations=[_delegation("", f"{_COMP}/pOuter", _end(_PROTO_A, _PORT_A))],
    )


def _blank_port(model: ImportModel) -> None:
    _add_swc(model, "/Pkg/Components/SwcC", "SwcC", "", "P-PORT-PROTOTYPE")


def _blank_component(model: ImportModel) -> None:
    _add_swc(model, "/Pkg/Components/", "", "pX", "P-PORT-PROTOTYPE")


def _blank_base_type(model: ImportModel) -> None:
    model.sw_base_types.append(
        SwBaseTypeInfo(path="/Pkg/Types/", name="", size=8, encoding="NONE", native_declaration="uint8", description="")
    )


def _blank_implementation_type(model: ImportModel) -> None:
    model.impl_data_types.append(
        ImplDataTypeInfo(
            path="/Pkg/Types/", name="", category="VALUE", base_type_ref="/Pkg/Types/uint8", impl_type_ref=None,
            description="",
        )
    )  # fmt: skip


def _blank_structure_member(model: ImportModel) -> None:
    member = ImplSubElementInfo(name="", category="TYPE_REFERENCE", impl_type_ref=_IMPL, base_type_ref=None)
    model.impl_data_types.append(
        ImplDataTypeInfo(
            path="/Pkg/Types/Pair", name="Pair", category="STRUCTURE", base_type_ref=None, impl_type_ref=None,
            description="", sub_elements=(member,),
        )
    )  # fmt: skip


def _blank_application_primitive(model: ImportModel) -> None:
    model.app_primitive_data_types.append(
        AppPrimitiveDataTypeInfo(path="/Pkg/AppTypes/", name="", compu_method_ref=None, description="")
    )


def _blank_record(model: ImportModel) -> None:
    model.app_record_data_types.append(
        AppRecordDataTypeInfo(
            path="/Pkg/AppTypes/", name="", elements=[RecordElementInfo(name="f", type_ref=_IMPL)], description=""
        )
    )


def _blank_record_element(model: ImportModel) -> None:
    model.app_record_data_types.append(
        AppRecordDataTypeInfo(
            path="/Pkg/AppTypes/Rec", name="Rec", elements=[RecordElementInfo(name="", type_ref=_IMPL)], description=""
        )
    )


def _blank_array(model: ImportModel) -> None:
    model.app_array_data_types.append(
        AppArrayDataTypeInfo(
            path="/Pkg/AppTypes/", name="", element_type_ref=_IMPL, max_number_of_elements=2, description=""
        )
    )


def _blank_mapping_set(model: ImportModel) -> None:
    model.data_type_mapping_sets.append(DataTypeMappingSetInfo(path="/Pkg/Maps/", name="", maps=[]))


def _blank_sr_interface(model: ImportModel) -> None:
    model.sr_interfaces.append(
        SRInterfaceInfo(
            path="/Pkg/Interfaces/",
            name="",
            description="",
            data_elements=[SRDataElementInfo(name="d", type_ref=_IMPL)],
        )
    )


def _blank_data_element(model: ImportModel) -> None:
    model.sr_interfaces.append(
        SRInterfaceInfo(
            path="/Pkg/Interfaces/IBlank",
            name="IBlank",
            description="",
            data_elements=[SRDataElementInfo(name="", type_ref=_IMPL)],
        )
    )


def _blank_cs_interface(model: ImportModel) -> None:
    model.cs_interfaces.append(
        CSInterfaceInfo(
            path="/Pkg/Interfaces/", name="", description="", operations=[CSOperationInfo(name="Op", arguments=[])]
        )
    )


def _blank_operation(model: ImportModel) -> None:
    model.cs_interfaces.append(
        CSInterfaceInfo(
            path="/Pkg/Interfaces/ICall",
            name="ICall",
            description="",
            operations=[CSOperationInfo(name="", arguments=[])],
        )
    )


def _blank_argument(model: ImportModel) -> None:
    argument = CSArgumentInfo(name="", type_ref=_IMPL, direction="IN")
    model.cs_interfaces.append(
        CSInterfaceInfo(
            path="/Pkg/Interfaces/ICall", name="ICall", description="",
            operations=[CSOperationInfo(name="Op", arguments=[argument])],
        )
    )  # fmt: skip


#: The nineteen missing or blank ``SHORT-NAME`` sites: (id, the mutation, the path, the tag, the unchanged sentence).
_BLANK_NAME_SITES = [
    (
        "composition",
        _blank_composition,
        "/Pkg/Compositions/",
        "COMPOSITION-SW-COMPONENT-TYPE",
        f"composition has {_BLANK}",
    ),
    (
        "component-prototype",
        _blank_prototype,
        f"{_COMP}/",
        "SW-COMPONENT-PROTOTYPE",
        f"component prototype has {_BLANK}",
    ),
    ("assembly-connector", _blank_connector, f"{_COMP}/", "ASSEMBLY-SW-CONNECTOR", f"connector has {_BLANK}"),
    ("delegation", _blank_delegation, f"{_COMP}/", "DELEGATION-SW-CONNECTOR", f"delegation has {_BLANK}"),
    ("port", _blank_port, "/Pkg/Components/SwcC/", "P-PORT-PROTOTYPE", f"port has {_BLANK}"),
    ("component", _blank_component, "/Pkg/Components/", "APPLICATION-SW-COMPONENT-TYPE", _BLANK),
    ("base-type", _blank_base_type, "/Pkg/Types/", "SW-BASE-TYPE", _BLANK),
    ("implementation-type", _blank_implementation_type, "/Pkg/Types/", "IMPLEMENTATION-DATA-TYPE", _BLANK),
    (
        "implementation-sub-element",
        _blank_structure_member,
        "/Pkg/Types/Pair",
        "IMPLEMENTATION-DATA-TYPE",
        f"STRUCTURE has a sub-element with {_BLANK}, and this release does not import it",
    ),
    (
        "application-primitive",
        _blank_application_primitive,
        "/Pkg/AppTypes/",
        "APPLICATION-PRIMITIVE-DATA-TYPE",
        _BLANK,
    ),
    ("application-record", _blank_record, "/Pkg/AppTypes/", "APPLICATION-RECORD-DATA-TYPE", _BLANK),
    (
        "record-element",
        _blank_record_element,
        "/Pkg/AppTypes/Rec",
        "APPLICATION-RECORD-DATA-TYPE",
        f"a record element has {_BLANK}",
    ),
    ("application-array", _blank_array, "/Pkg/AppTypes/", "APPLICATION-ARRAY-DATA-TYPE", _BLANK),
    ("mapping-set", _blank_mapping_set, "/Pkg/Maps/", "DATA-TYPE-MAPPING-SET", _BLANK),
    ("sr-interface", _blank_sr_interface, "/Pkg/Interfaces/", "SENDER-RECEIVER-INTERFACE", _BLANK),
    (
        "data-element",
        _blank_data_element,
        "/Pkg/Interfaces/IBlank",
        "SENDER-RECEIVER-INTERFACE",
        f"a data element has {_BLANK}",
    ),
    ("cs-interface", _blank_cs_interface, "/Pkg/Interfaces/", "CLIENT-SERVER-INTERFACE", _BLANK),
    (
        "operation",
        _blank_operation,
        f"/Pkg/Interfaces/ICall/{UNNAMED_MEMBER}",
        "CLIENT-SERVER-OPERATION",
        f"an operation has {_BLANK}",
    ),
    (
        "argument",
        _blank_argument,
        "/Pkg/Interfaces/ICall/Op",
        "CLIENT-SERVER-OPERATION",
        f"operation 'Op': an argument has {_BLANK}",
    ),
]


class TestAMissingOrBlankShortNameIsCited:
    """``[TPS_GST_00057]`` -- ``Referrable.shortName`` has multiplicity 1 and ``enforceMinMultiplicity``.

    Every site whose sentence reports a missing or blank ``SHORT-NAME`` records
    a defect of the export; each sentence is unchanged.
    """

    def test_the_sites_are_nineteen(self) -> None:
        assert len(_BLANK_NAME_SITES) == 19

    @pytest.mark.parametrize(
        ("mutate", "path", "tag", "reason"),
        [pytest.param(*site[1:], id=site[0]) for site in _BLANK_NAME_SITES],
    )
    def test_each_site(self, mutate, path: str, tag: str, reason: str) -> None:
        model = _two_swcs()
        mutate(model)
        entries = [entry for entry in _every_skip(model) if (entry.path, entry.tag) == (path, tag)]
        assert entries, f"no entry at {path} ({tag})"
        (entry,) = [entry for entry in entries if entry.reason == reason]
        assert (entry.category, entry.rule) == (_ED, StandardRule.SHORT_NAME_REQUIRED)


class TestAComponentImportCitesABlankPortName:
    """``import component`` aborts on a blank port name, and lists it under *Defects of the export*."""

    def test_the_abort_names_the_rule_in_the_files_release(self, tmp_path: Path) -> None:
        _source, arxml = _export_target(tmp_path)
        text = arxml.read_text(encoding="utf-8")
        assert text.count("<SHORT-NAME>rCalib</SHORT-NAME>") == 1
        arxml.write_text(
            text.replace("<SHORT-NAME>rCalib</SHORT-NAME>", "<SHORT-NAME></SHORT-NAME>"), encoding="utf-8", newline="\n"
        )
        project = init_project(tmp_path / "proj")
        with pytest.raises(LossyComponentImportError) as caught:
            import_component(str(arxml), str(project / "model"))
        line = (
            "R-PORT-PROTOTYPE /ComponentTypes/TargetSwc/ (R4.2.2 [TPS_GST_00057]): port has missing or blank SHORT-NAME"
        )
        assert line in caught.value.skip_lines
        assert f"Defects of the export (1; the file breaks a rule of the standard, cited): {line}" in str(caught.value)


class TestAConnectorEndBreaksTheInstanceReferenceRules:
    """Section 1 on an end: ``[TPS_GST_00162]`` for a target, ``[constr_2530]`` for a context."""

    def test_a_stub_end_whose_target_its_type_does_not_own(self) -> None:
        model = _two_swcs()
        ac = _assembly(
            "Ext_to_B",
            _end(_STUB_PROTO, "/Vendor/Other/ExtSwc/pOut", "P-PORT-PROTOTYPE", "provide"),
            _end(_PROTO_B, _PORT_B),
        )
        _comp1(
            model,
            prototypes=[
                _prototype(_PROTO_B, "SwcB_1", "/Pkg/Components/SwcB"),
                _prototype(_STUB_PROTO, "Ext", _STUB_TYPE),
            ],
            assembly=[ac],
        )
        entry = _composition_skip(model, ac.path)
        assert (entry.category, entry.rule) == (_ED, StandardRule.INSTANCE_REF_PATH)
        assert entry.reason == (
            "provider end: unresolved reference /Vendor/Other/ExtSwc/pOut (target port path is not owned by the "
            f"context component's type: member 'Ext' is the external stub 'ExtSwc_ext0' typed {_STUB_TYPE}; no port is "
            "minted from it)"
        )

    def test_a_mapped_members_end_naming_another_types_port(self) -> None:
        model = _two_swcs()
        ac = _assembly("A_to_B", _end(_PROTO_A, "/Pkg/Components/SwcB/pA"), _end(_PROTO_B, _PORT_B))
        _comp1(model, assembly=[ac])
        entry = _composition_skip(model, ac.path)
        assert (entry.category, entry.rule) == (_ED, StandardRule.INSTANCE_REF_PATH)
        assert entry.reason == (
            "provider end: unresolved reference /Pkg/Components/SwcB/pA (not a port of member 'SwcA_1's type 'SwcA')"
        )

    def test_a_context_component_that_is_not_a_member(self) -> None:
        model = _two_swcs()
        ac = _assembly("A_to_B", _end(_PROTO_A, _PORT_A), _end("/Pkg/Compositions/Other/SwcB_1", _PORT_B))
        _comp1(model, assembly=[ac])
        entry = _composition_skip(model, ac.path)
        assert (entry.category, entry.rule) == (_ED, StandardRule.INSTANCE_REF_CONTEXT)
        assert (
            entry.reason
            == "requester end: unresolved reference /Pkg/Compositions/Other/SwcB_1 (not a member of 'Comp1')"
        )


class TestAConnectorOrDelegationBreaksAConnectionRule:
    def test_a_duplicate_connector_link(self) -> None:
        model = _two_swcs()
        first = _assembly("A_to_B", _end(_PROTO_A, _PORT_A), _end(_PROTO_B, _PORT_B))
        second = _assembly("B_from_A", _end(_PROTO_A, _PORT_A), _end(_PROTO_B, _PORT_B))
        _comp1(model, assembly=[first, second])
        entry = _composition_skip(model, second.path)
        assert (entry.category, entry.rule) == (_ED, StandardRule.ONE_CONNECTOR_PER_PAIR)
        assert entry.reason.startswith("duplicate connector link SwcA.pA -> SwcB.pB inside composition 'Comp1'")

    def test_a_duplicate_delegation_link(self) -> None:
        model = _two_swcs()
        first = _delegation("D1", f"{_COMP}/pOuter", _end(_PROTO_A, _PORT_A))
        second = _delegation("D2", f"{_COMP}/pOuter", _end(_PROTO_A, _PORT_A))
        _comp1(model, ports=[_boundary_port("pOuter")], delegations=[first, second])
        entry = _composition_skip(model, second.path)
        assert (entry.category, entry.rule) == (_ED, StandardRule.ONE_CONNECTOR_PER_PAIR)
        assert entry.reason.startswith("duplicate delegation link pOuter -> SwcA_1.pA inside composition 'Comp1'")

    def test_a_delegation_between_a_provide_and_a_require_port(self) -> None:
        model = _two_swcs()
        deleg = _delegation("D1", f"{_COMP}/pOuter", _end(_PROTO_B, _PORT_B))
        _comp1(model, ports=[_boundary_port("pOuter")], delegations=[deleg])
        entry = _composition_skip(model, deleg.path)
        assert (entry.category, entry.rule) == (_ED, StandardRule.DELEGATION_SAME_KIND)
        assert "a delegation's two ports must declare the same direction" in entry.reason


_SR = "/Pkg/Interfaces/ISig"
_CS = "/Pkg/Interfaces/IServe"


class TestASameDirectionAssemblyIsCitedByItsEndsKinds:
    """Two declared interface kinds that differ are ``[constr_1036]``.

    Every other pair -- one kind on both ends, or an end whose interface the file does not declare -- is
    ``[TPS_SWCT_01113]``, one sentence."""

    SENTENCE = "resolved endpoints are both '{direction}' -- a connector needs exactly one provide and one require end"

    @pytest.mark.parametrize("interface", [_SR, _CS], ids=["sender-receiver", "client-server"])
    @pytest.mark.parametrize("kind", ["P-PORT-PROTOTYPE", "R-PORT-PROTOTYPE"], ids=["provide", "require"])
    def test_one_kind_on_both_ends(self, kind: str, interface: str) -> None:
        entry = self._skip(kind, interface, interface)
        assert (entry.category, entry.rule) == (_ED, StandardRule.CONNECT_PROVIDE_TO_REQUIRE)
        direction = "provide" if kind.startswith("P") else "require"
        assert entry.reason == self.SENTENCE.format(direction=direction)

    @pytest.mark.parametrize(
        ("kind", "interface_a", "interface_b", "phrase"),
        [
            pytest.param(
                "P-PORT-PROTOTYPE",
                _SR,
                _CS,
                "one typed by a sender/receiver and one by a client/server interface",
                id="sr-provide-with-cs-provide",
            ),
            pytest.param(
                "R-PORT-PROTOTYPE",
                _CS,
                _SR,
                "one typed by a client/server and one by a sender/receiver interface",
                id="cs-require-with-sr-require",
            ),
        ],
    )
    def test_a_mixed_kind_pair_is_the_interface_kind_rule(
        self, kind: str, interface_a: str, interface_b: str, phrase: str
    ) -> None:
        entry = self._skip(kind, interface_a, interface_b)
        assert (entry.category, entry.rule) == (_ED, StandardRule.CONNECT_SAME_INTERFACE_KIND)
        direction = "provide" if kind.startswith("P") else "require"
        assert entry.reason == (
            f"resolved endpoints are both '{direction}', {phrase} -- ports typed by interfaces of different kinds "
            "cannot be connected"
        )

    @pytest.mark.parametrize("kind", ["P-PORT-PROTOTYPE", "R-PORT-PROTOTYPE"], ids=["provide", "require"])
    def test_an_end_on_an_interface_the_file_does_not_declare(self, kind: str) -> None:
        """The measured shape: the undeclared interface is kept as its path."""
        model = _two_swcs(kind, kind, interface_b="/Bsw/Vendor/IGlobalTime")
        ac = _assembly("A_to_B", _end(_PROTO_A, _PORT_A), _end(_PROTO_B, _PORT_B))
        _comp1(model, assembly=[ac])
        decisions = decide_merges(model)
        _mapped, skips, _registry, _swcs = _map_full_with_policy(model, decisions)
        (entry,) = [entry for entry in skips if entry.path == ac.path]
        assert (entry.category, entry.rule) == (_ED, StandardRule.CONNECT_PROVIDE_TO_REQUIRE)
        direction = "provide" if kind.startswith("P") else "require"
        assert entry.reason == self.SENTENCE.format(direction=direction)

    @staticmethod
    def _skip(kind: str, interface_a: str, interface_b: str) -> SkipEntry:
        model = _two_swcs(kind, kind, interface_a=interface_a, interface_b=interface_b)
        ac = _assembly("A_to_B", _end(_PROTO_A, _PORT_A), _end(_PROTO_B, _PORT_B))
        _comp1(model, assembly=[ac])
        return _composition_skip(model, ac.path)


def _map_full_with_policy(model: ImportModel, decisions):
    """``run_import``'s mapping order with both policies, so an undeclared interface is kept as its path."""
    unresolved = UnresolvedReferences(model, decisions)
    forms: list = []
    data_types, registry, _ = map_data_types(model, merges=decisions, unresolved=unresolved, elements=forms)
    interfaces, iface_skips = map_interfaces(
        model, registry, data_types, merges=decisions, unresolved=unresolved, elements=forms
    )
    swcs, swc_skips = map_swcs(
        model,
        interfaces,
        iface_skip_log=iface_skips,
        merges=decisions,
        unresolved=unresolved,
        port_inits={},
        variable_types={},
    )
    mapped, skips = map_compositions(
        model,
        swcs,
        interfaces,
        iface_skips,
        registry,
        merges=decisions,
        unresolved=unresolved,
        swc_skip_log=swc_skips,
    )
    return mapped, skips, registry, swcs


class TestADuplicateNameInsideOneComposition:
    """``[constr_2508]`` for a member or a connector SHORT-NAME of one composition, unless the file is variant-rich."""

    @pytest.mark.parametrize("variant_rich", [False, True])
    def test_a_duplicate_member_name(self, variant_rich: bool) -> None:
        model = _two_swcs()
        model.variant_rich = variant_rich
        twin = _prototype(f"{_COMP}/SwcA_1_twin", "SwcA_1", "/Pkg/Components/SwcB")
        _comp1(model, prototypes=[_prototype(_PROTO_A, "SwcA_1", "/Pkg/Components/SwcA"), twin])
        entry = _composition_skip(model, twin.path)
        _assert_duplicate(entry, "duplicate member name 'SwcA_1' in composition 'Comp1'", variant_rich=variant_rich)

    @pytest.mark.parametrize("variant_rich", [False, True])
    def test_a_duplicate_assembly_connector_name(self, variant_rich: bool) -> None:
        model = _two_swcs()
        model.variant_rich = variant_rich
        first = _assembly("A_to_B", _end(_PROTO_A, _PORT_A), _end(_PROTO_B, _PORT_B))
        second = AssemblyConnectorInfo(
            path=f"{_COMP}/A_to_B_twin", name="A_to_B", provider=first.provider, requester=first.requester
        )
        _comp1(model, assembly=[first, second])
        entry = _composition_skip(model, second.path)
        _assert_duplicate(
            entry,
            f"duplicate connector SHORT-NAME 'A_to_B' inside composition 'Comp1' (already declared by {first.path})",
            variant_rich=variant_rich,
        )

    @pytest.mark.parametrize("variant_rich", [False, True])
    def test_a_delegation_named_like_a_connector_of_its_composition(self, variant_rich: bool) -> None:
        model = _two_swcs()
        model.variant_rich = variant_rich
        ac = _assembly("A_to_B", _end(_PROTO_A, _PORT_A), _end(_PROTO_B, _PORT_B))
        deleg = DelegationConnectorInfo(
            path=f"{_COMP}/A_to_B_deleg", name="A_to_B", inner=_end(_PROTO_A, _PORT_A), outer_port_ref=f"{_COMP}/pOuter"
        )
        _comp1(model, ports=[_boundary_port("pOuter")], assembly=[ac], delegations=[deleg])
        entry = _composition_skip(model, deleg.path)
        _assert_duplicate(
            entry, "duplicate connector SHORT-NAME 'A_to_B' inside composition 'Comp1'", variant_rich=variant_rich
        )


def _assert_duplicate(entry: SkipEntry, text: str, *, variant_rich: bool) -> None:
    TestADuplicateNameUnderOneParent._assert_duplicate(entry, text, variant_rich=variant_rich)


class TestSwcraftsOwnNameSpacesStaySectionTwo:
    """A duplicate only in SWCraft's flatter name spaces is SWCraft's."""

    def test_one_connector_name_in_two_compositions(self) -> None:
        model = _two_swcs()
        inner = "/Pkg/Compositions/Comp2"
        model.compositions.append(
            _composition_info(
                inner,
                "Comp2",
                prototypes=[
                    _prototype(f"{inner}/SwcA_1", "SwcA_1", "/Pkg/Components/SwcA"),
                    _prototype(f"{inner}/SwcB_1", "SwcB_1", "/Pkg/Components/SwcB"),
                ],
                assembly=[
                    _assembly("A_to_B", _end(f"{inner}/SwcA_1", _PORT_A), _end(f"{inner}/SwcB_1", _PORT_B), comp=inner)
                ],
            )
        )
        _comp1(
            model,
            prototypes=[
                _prototype(_PROTO_A, "SwcA_1", "/Pkg/Components/SwcA"),
                _prototype(_PROTO_B, "SwcB_1", "/Pkg/Components/SwcB"),
                _prototype(f"{_COMP}/Comp2_1", "Comp2_1", inner, COMPOSITION_DEST),
            ],
            assembly=[_assembly("A_to_B", _end(_PROTO_A, _PORT_A), _end(_PROTO_B, _PORT_B))],
        )
        entry = _composition_skip(model, f"{_COMP}/A_to_B")
        assert (entry.category, entry.rule) == (_NS, None)
        assert entry.reason.startswith("duplicate connector name 'A_to_B' (already produced by ")

    def test_a_composition_named_like_a_component(self) -> None:
        entry = _composition_skip(_a_composition_named_like_a_component(), "/Pkg/Compositions/SwcA")
        assert (entry.category, entry.rule) == (_NS, None)
        assert entry.reason.startswith("duplicate component-type name 'SwcA'")


class TestAnUnresolvedEndIsSplitByWhatTheFileDeclares:
    """On a connector or a delegation: the chain half and the outside-the-file half."""

    def test_a_prototype_declared_nowhere_is_outside_the_file(self) -> None:
        model = _two_swcs()
        ac = _assembly("A_to_B", _end(_PROTO_A, _PORT_A), _end(f"{_COMP}/Ghost", _PORT_B))
        _comp1(model, assembly=[ac])
        entry = _composition_skip(model, ac.path)
        assert (entry.category, entry.rule) == (_OF, None)
        assert entry.reason == (
            f"requester end: unresolved reference {_COMP}/Ghost (the file declares no component prototype of "
            "composition 'Comp1' at that path)"
        )

    def test_a_declared_prototype_pass_one_skipped_is_the_chain_half(self) -> None:
        model = _two_swcs()
        typeless = _prototype(f"{_COMP}/Typeless", "Typeless", "")
        model.path_index[typeless.path] = raw_element(typeless.path)
        ac = _assembly("A_to_B", _end(_PROTO_A, _PORT_A), _end(typeless.path, _PORT_B))
        _comp1(model, prototypes=[_prototype(_PROTO_A, "SwcA_1", "/Pkg/Components/SwcA"), typeless], assembly=[ac])
        entry = _composition_skip(model, ac.path)
        assert (entry.category, entry.rule) == (_NS, None)
        assert entry.reason == f"requester end: unresolved reference {typeless.path} (component prototype was skipped)"

    def test_a_port_declared_nowhere_is_outside_the_file(self) -> None:
        model = _two_swcs()
        ac = _assembly("A_to_B", _end(_PROTO_A, _PORT_A), _end(_PROTO_B, "/Pkg/Components/SwcB/pGhost"))
        _comp1(model, assembly=[ac])
        entry = _composition_skip(model, ac.path)
        assert (entry.category, entry.rule) == (_OF, None)
        assert entry.reason == (
            "requester end: unresolved reference /Pkg/Components/SwcB/pGhost (the file declares no port of type "
            "'SwcB' at that path)"
        )

    def test_a_declared_port_the_mapper_skipped_is_the_chain_half(self) -> None:
        """The chain half inherits the port's own entry: the port's interface is declared nowhere."""
        model = _two_swcs(interface_b="/Pkg/Interfaces/Missing")
        model.path_index[_PORT_B] = raw_element(_PORT_B)
        ac = _assembly("A_to_B", _end(_PROTO_A, _PORT_A), _end(_PROTO_B, _PORT_B))
        _comp1(model, assembly=[ac])
        entry = _composition_skip(model, ac.path)
        assert (entry.category, entry.rule) == (_OF, None)
        assert entry.reason == f"requester end: unresolved reference {_PORT_B} (port was skipped)"

    def test_a_boundary_port_declared_nowhere_is_outside_the_file(self) -> None:
        model = _two_swcs()
        deleg = _delegation("D1", f"{_COMP}/pGhost", _end(_PROTO_A, _PORT_A))
        _comp1(model, delegations=[deleg])
        entry = _composition_skip(model, deleg.path)
        assert (entry.category, entry.rule) == (_OF, None)
        assert entry.reason == (
            f"unresolved reference {_COMP}/pGhost (the file declares no port of composition 'Comp1' at that path)"
        )

    def test_a_declared_boundary_port_the_mapper_skipped_is_the_chain_half(self) -> None:
        model = _two_swcs()
        pr_port = _boundary_port("pBoth", "PR-PORT-PROTOTYPE")
        model.path_index[pr_port.path] = raw_element(pr_port.path)
        deleg = _delegation("D1", pr_port.path, _end(_PROTO_A, _PORT_A))
        _comp1(model, ports=[pr_port], delegations=[deleg])
        entry = _composition_skip(model, deleg.path)
        assert (entry.category, entry.rule) == (_NS, None)
        assert entry.reason == f"unresolved reference {pr_port.path} (boundary port was skipped)"

    def test_a_declared_connector_named_as_the_outer_port_inherits_nothing(self) -> None:
        """Declared under the composition but no boundary port: the connector's own citation is not the delegation's."""
        model = _two_swcs()
        stray = _assembly("Stray", _end(_PROTO_A, _PORT_A), _end("/Pkg/Compositions/Other/SwcB_1", _PORT_B))
        model.path_index[stray.path] = raw_element(stray.path)
        deleg = _delegation("D1", stray.path, _end(_PROTO_A, _PORT_A))
        _comp1(model, assembly=[stray], delegations=[deleg])
        _mapped, skips, _registry, _swcs = _map_full(model)
        root = skips.first_at(stray.path)
        assert root is not None and (root.category, root.rule) == (_ED, StandardRule.INSTANCE_REF_CONTEXT)
        entry = skips.first_at(deleg.path)
        assert entry is not None and (entry.category, entry.rule) == (_NS, None)
        assert entry.reason == f"unresolved reference {stray.path} (names no port of composition 'Comp1')"

    def test_a_declared_non_port_of_the_members_type_named_as_its_port_inherits_nothing(self) -> None:
        """Declared under the member's type but no port: the element's own citation is not the end's."""
        model = _two_swcs()
        behavior = "/Pkg/Components/SwcB/Behavior"
        model.path_index[behavior] = raw_element(behavior)
        model.skip_log.record(
            behavior,
            "SWC-INTERNAL-BEHAVIOR",
            "a cited refusal",
            category=_ED,
            rule=StandardRule.INSTANCE_REF_CONTEXT,
            effect="abort",
        )
        ac = _assembly("A_to_B", _end(_PROTO_A, _PORT_A), _end(_PROTO_B, behavior))
        _comp1(model, assembly=[ac])
        entry = _composition_skip(model, ac.path)
        assert (entry.category, entry.rule) == (_NS, None)
        assert entry.reason == f"requester end: unresolved reference {behavior} (names no port of type 'SwcB')"

    def test_a_delegation_with_no_outer_port_reference_is_swcrafts(self) -> None:
        model = _two_swcs()
        deleg = _delegation("D1", None, _end(_PROTO_A, _PORT_A))
        _comp1(model, delegations=[deleg])
        entry = _composition_skip(model, deleg.path)
        assert (entry.category, entry.rule) == (_NS, None)
        assert entry.reason == "delegation has no outer port reference"

    def test_a_stub_port_stated_with_contradicting_directions_is_outside_the_file(self) -> None:
        """Which ``DEST`` is wrong is judged only against a declaration no file SWCraft has carries."""
        model = _two_swcs()
        into_b = _assembly(
            "Ext_to_B", _end(_STUB_PROTO, f"{_STUB_TYPE}/pX", "P-PORT-PROTOTYPE", "provide"), _end(_PROTO_B, _PORT_B)
        )
        from_a = _assembly(
            "A_to_Ext", _end(_PROTO_A, _PORT_A), _end(_STUB_PROTO, f"{_STUB_TYPE}/pX", "R-PORT-PROTOTYPE", "require")
        )
        _comp1(
            model,
            prototypes=[
                _prototype(_PROTO_A, "SwcA_1", "/Pkg/Components/SwcA"),
                _prototype(_PROTO_B, "SwcB_1", "/Pkg/Components/SwcB"),
                _prototype(_STUB_PROTO, "Ext", _STUB_TYPE),
            ],
            assembly=[into_b, from_a],
        )
        _mapped, skips, _registry, _swcs = _map_full(model)
        entries = [entry for entry in skips if entry.path in {into_b.path, from_a.path}]
        assert len(entries) == 2
        for entry in entries:
            assert (entry.category, entry.rule) == (_OF, None)
            assert f"the export states contradicting directions for {_STUB_TYPE}/pX" in entry.reason


def _assert_the_count_points_at_section_one(mapped, skips: SkipLog) -> None:
    """Every path the report's stub count line counts is a section-1 ``[TPS_GST_00162]`` line."""
    counted = {path for paths in mapped.unowned_port_paths.values() for path in paths}
    assert counted
    assert counted <= {entry.path for entry in skips if entry.rule is StandardRule.INSTANCE_REF_PATH}


class TestADelegationsInnerEndBreaksTheInstanceReferenceRules:
    """The three end families of :class:`TestAConnectorEndBreaksTheInstanceReferenceRules`, on a delegation.

    The delegation records its inner end at two sites of its own, so each family
    is met there too: the section, the rule and the ``inner end:`` text.
    """

    def test_a_stub_inner_end_whose_target_its_type_does_not_own(self) -> None:
        model = _two_swcs()
        deleg = _delegation(
            "D1", f"{_COMP}/pOuter", _end(_STUB_PROTO, "/Vendor/Other/ExtSwc/pOut", "P-PORT-PROTOTYPE", "provide")
        )
        _comp1(
            model,
            ports=[_boundary_port("pOuter")],
            prototypes=[
                _prototype(_PROTO_A, "SwcA_1", "/Pkg/Components/SwcA"),
                _prototype(_STUB_PROTO, "Ext", _STUB_TYPE),
            ],
            delegations=[deleg],
        )
        mapped, skips, _registry, _swcs = _map_full(model)
        entry = skips.first_at(deleg.path)
        assert entry is not None and (entry.category, entry.rule) == (_ED, StandardRule.INSTANCE_REF_PATH)
        assert entry.reason == (
            "inner end: unresolved reference /Vendor/Other/ExtSwc/pOut (target port path is not owned by the "
            f"context component's type: member 'Ext' is the external stub 'ExtSwc_ext0' typed {_STUB_TYPE}; no port is "
            "minted from it)"
        )
        _assert_the_count_points_at_section_one(mapped, skips)

    def test_a_mapped_members_inner_end_naming_another_types_port(self) -> None:
        model = _two_swcs()
        deleg = _delegation("D1", f"{_COMP}/pOuter", _end(_PROTO_A, "/Pkg/Components/SwcB/pA"))
        _comp1(model, ports=[_boundary_port("pOuter")], delegations=[deleg])
        entry = _composition_skip(model, deleg.path)
        assert (entry.category, entry.rule) == (_ED, StandardRule.INSTANCE_REF_PATH)
        assert entry.reason == (
            "inner end: unresolved reference /Pkg/Components/SwcB/pA (not a port of member 'SwcA_1's type 'SwcA')"
        )

    def test_an_inner_context_component_that_is_not_a_member(self) -> None:
        model = _two_swcs()
        deleg = _delegation("D1", f"{_COMP}/pOuter", _end("/Pkg/Compositions/Other/SwcA_1", _PORT_A))
        _comp1(model, ports=[_boundary_port("pOuter")], delegations=[deleg])
        entry = _composition_skip(model, deleg.path)
        assert (entry.category, entry.rule) == (_ED, StandardRule.INSTANCE_REF_CONTEXT)
        assert (
            entry.reason == "inner end: unresolved reference /Pkg/Compositions/Other/SwcA_1 (not a member of 'Comp1')"
        )


class TestTheStubCountLinePointsAtSectionOne:
    def test_an_assemblys_unowned_stub_end_is_counted_and_cited(self) -> None:
        model = _two_swcs()
        ac = _assembly(
            "Ext_to_B",
            _end(_STUB_PROTO, "/Vendor/Other/ExtSwc/pOut", "P-PORT-PROTOTYPE", "provide"),
            _end(_PROTO_B, _PORT_B),
        )
        _comp1(
            model,
            prototypes=[
                _prototype(_PROTO_B, "SwcB_1", "/Pkg/Components/SwcB"),
                _prototype(_STUB_PROTO, "Ext", _STUB_TYPE),
            ],
            assembly=[ac],
        )
        mapped, skips, _registry, _swcs = _map_full(model)
        _assert_the_count_points_at_section_one(mapped, skips)


class TestAContextNamingSomethingElseOfTheComposition:
    def test_a_boundary_port_named_as_the_context_component_is_swcrafts(self) -> None:
        """Declared under the composition, but no prototype: no quoted rule, and no prototype was skipped."""
        model = _two_swcs()
        outer = _boundary_port("pOuter")
        model.path_index[outer.path] = raw_element(outer.path)
        ac = _assembly("A_to_B", _end(_PROTO_A, _PORT_A), _end(outer.path, _PORT_B))
        _comp1(model, ports=[outer], assembly=[ac])
        entry = _composition_skip(model, ac.path)
        assert (entry.category, entry.rule) == (_NS, None)
        assert entry.reason == (
            f"requester end: unresolved reference {outer.path} (names no component prototype of composition 'Comp1')"
        )


_COMP2 = "/Pkg/Compositions/Comp2"


class TestAnOuterPortOutsideItsComposition:
    """``[constr_1088]`` only when the outer port's owner is another composition of the file."""

    def _entry(self, outer_port_ref: str, *, declare: str | None = None) -> SkipEntry:
        model = _two_swcs()
        model.compositions.append(
            _composition_info(_COMP2, "Comp2", ports=[SwcPortInfo(
                path=f"{_COMP2}/pOuter", name="pOuter", kind="P-PORT-PROTOTYPE",
                interface_ref="/Pkg/Interfaces/ISig", description="",
            )])
        )  # fmt: skip
        if declare is not None:
            model.path_index[declare] = raw_element(declare)
        deleg = _delegation("D1", outer_port_ref, _end(_PROTO_A, _PORT_A))
        _comp1(
            model,
            prototypes=[
                _prototype(_PROTO_A, "SwcA_1", "/Pkg/Components/SwcA"),
                _prototype(_PROTO_B, "SwcB_1", "/Pkg/Components/SwcB"),
                _prototype(f"{_COMP}/Comp2_1", "Comp2_1", _COMP2, COMPOSITION_DEST),
            ],
            delegations=[deleg],
        )
        return _composition_skip(model, deleg.path)

    def test_a_port_of_another_composition_breaks_the_rule(self) -> None:
        entry = self._entry(f"{_COMP2}/pOuter")
        assert (entry.category, entry.rule) == (_ED, StandardRule.DELEGATION_INSIDE_COMPOSITION)
        assert entry.reason == (
            f"outer port {_COMP2}/pOuter is not a port of composition 'Comp1' ({_COMP}), which owns the delegation"
        )

    def test_an_owner_the_file_declares_nowhere_is_outside_the_file(self) -> None:
        entry = self._entry("/Pkg/Compositions/Ghost/pOuter")
        assert (entry.category, entry.rule) == (_OF, None)
        assert entry.reason.endswith("and the file declares nothing at /Pkg/Compositions/Ghost")

    def test_a_port_of_an_atomic_component_is_swcrafts(self) -> None:
        entry = self._entry(_PORT_A, declare="/Pkg/Components/SwcA")
        assert (entry.category, entry.rule) == (_NS, None)
        assert entry.reason.endswith("and names no port of a composition")

    def test_a_reference_with_no_owner_is_swcrafts(self) -> None:
        entry = self._entry("pOuter")
        assert (entry.category, entry.rule) == (_NS, None)


class TestADuplicateConnectorNameIsJudgedBeforeItsEnds:
    def test_an_assembly_whose_end_fails_is_still_the_duplicate(self) -> None:
        """The delegation's order: the name is asked first, so the defect is not hidden behind an end's reason."""
        model = _two_swcs()
        first = _assembly("A_to_B", _end(_PROTO_A, _PORT_A), _end(_PROTO_B, _PORT_B))
        second = AssemblyConnectorInfo(
            path=f"{_COMP}/A_to_B_twin",
            name="A_to_B",
            provider=first.provider,
            requester=_end(f"{_COMP}/Ghost", _PORT_B),
        )
        _comp1(model, assembly=[first, second])
        entry = _composition_skip(model, second.path)
        _assert_duplicate(
            entry,
            f"duplicate connector SHORT-NAME 'A_to_B' inside composition 'Comp1' (already declared by {first.path})",
            variant_rich=False,
        )

    def test_a_conflicting_groups_name_is_claimed_inside_its_composition(self) -> None:
        """A group that imports nothing still names this composition's declarations.

        A delegation of that name is a duplicate."""
        model = _two_swcs()
        first = _assembly("Wire", _end(_PROTO_A, _PORT_A), _end(_PROTO_B, _PORT_B))
        other = AssemblyConnectorInfo(
            path="/Pkg/Compositions/Comp2/Wire", name="Wire", provider=_end("/Pkg/Compositions/Comp2/SwcB_1", _PORT_B),
            requester=_end("/Pkg/Compositions/Comp2/SwcA_1", _PORT_A),
        )  # fmt: skip
        model.compositions.append(
            _composition_info(
                _COMP2,
                "Comp2",
                prototypes=[
                    _prototype(f"{_COMP2}/SwcA_1", "SwcA_1", "/Pkg/Components/SwcA"),
                    _prototype(f"{_COMP2}/SwcB_1", "SwcB_1", "/Pkg/Components/SwcB"),
                ],
                assembly=[other],
            )
        )
        deleg = _delegation("Wire_deleg", f"{_COMP}/pOuter", _end(_PROTO_A, _PORT_A))
        deleg = DelegationConnectorInfo(
            path=deleg.path, name="Wire", inner=deleg.inner, outer_port_ref=deleg.outer_port_ref
        )
        _comp1(
            model,
            ports=[_boundary_port("pOuter")],
            prototypes=[
                _prototype(_PROTO_A, "SwcA_1", "/Pkg/Components/SwcA"),
                _prototype(_PROTO_B, "SwcB_1", "/Pkg/Components/SwcB"),
                _prototype(f"{_COMP}/Comp2_1", "Comp2_1", _COMP2, COMPOSITION_DEST),
            ],
            assembly=[first],
            delegations=[deleg],
        )
        for path in (first.path, other.path):
            model.path_index[path] = raw_element(path)
        decisions = decide_merges(model)
        group = decisions.group_of(first.path)
        assert group is not None and group.verdict == CONFLICTING
        _mapped, skips, _registry, _swcs = _map_full_with_policy(model, decisions)
        entry = skips.first_at(deleg.path)
        assert entry is not None
        _assert_duplicate(entry, "duplicate connector SHORT-NAME 'Wire' inside composition 'Comp1'", variant_rich=False)


class TestAnUnresolvedTypeWithoutAPolicy:
    """``import component``'s case: no policy keeps a reference the file declares nowhere."""

    def test_a_value_types_base_declared_nowhere_is_outside_the_file(self) -> None:
        model = ImportModel(source_path="inline-test")
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path=_COUNTER, name="Counter", category="VALUE", base_type_ref="/Vendor/Ghost", impl_type_ref=None,
                description="",
            )
        )  # fmt: skip
        _data_types, _registry, skips = map_data_types(model, elements=[])
        entry = skips.first_at(_COUNTER)
        assert entry is not None and (entry.category, entry.rule) == (_OF, None)
        assert entry.reason == "unresolved reference /Vendor/Ghost (the file declares no base type at that path)"

    def test_a_value_types_declared_base_that_is_no_base_type_is_chained(self) -> None:
        model = ImportModel(source_path="inline-test")
        root = _dup_reader_entry(model, "/Acme/Types/Twice", "SW-BASE-TYPE")
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path=_COUNTER, name="Counter", category="VALUE", base_type_ref="/Acme/Types/Twice", impl_type_ref=None,
                description="",
            )
        )  # fmt: skip
        _data_types, _registry, skips = map_data_types(model, elements=[])
        entry = skips.first_at(_COUNTER)
        _assert_inherits(entry, root)
        assert entry.reason == "unresolved reference /Acme/Types/Twice (base type)"

    def test_a_referenced_implementation_type_declared_nowhere_is_outside_the_file(self) -> None:
        model = ImportModel(source_path="inline-test")
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/Acme/Types/Ref", name="Ref", category="TYPE_REFERENCE", base_type_ref=None,
                impl_type_ref="/Vendor/Ghost", description="",
            )
        )  # fmt: skip
        _data_types, _registry, skips = map_data_types(model, elements=[])
        target = skips.first_at("/Vendor/Ghost")
        assert target is not None and (target.category, target.rule) == (_OF, None)
        assert target.reason == (
            "unresolved reference /Vendor/Ghost (the file declares no implementation data type at that path)"
        )
        _assert_inherits(skips.first_at("/Acme/Types/Ref"), target)

    def test_a_data_elements_type_declared_nowhere_puts_its_interface_outside_the_file(self) -> None:
        model = ImportModel(source_path="inline-test")
        model.sr_interfaces.append(
            SRInterfaceInfo(
                path="/Acme/Interfaces/ISig", name="ISig", description="",
                data_elements=[SRDataElementInfo(name="value", type_ref="/Vendor/Ghost")],
            )
        )  # fmt: skip
        forms: list = []
        data_types, registry, _type_skips = map_data_types(model, elements=forms)
        _interfaces, skips = map_interfaces(model, registry, data_types, elements=forms)
        entry = skips.first_at("/Acme/Interfaces/ISig")
        assert entry is not None and (entry.category, entry.rule) == (_OF, None)
        assert entry.reason == (
            "data element 'value': unresolved reference /Vendor/Ghost (the file declares no data type at that path)"
        )

    def test_a_data_elements_declared_type_swcraft_skipped_stays_swcrafts(self) -> None:
        """``map_interfaces`` receives no type mapper's log, so it cannot search a root: the line stays SWCraft's."""
        model = ImportModel(source_path="inline-test")
        model.path_index["/Acme/Types/Odd"] = raw_element("/Acme/Types/Odd")
        model.sr_interfaces.append(
            SRInterfaceInfo(
                path="/Acme/Interfaces/ISig", name="ISig", description="",
                data_elements=[SRDataElementInfo(name="value", type_ref="/Acme/Types/Odd")],
            )
        )  # fmt: skip
        forms: list = []
        data_types, registry, _type_skips = map_data_types(model, elements=forms)
        _interfaces, skips = map_interfaces(model, registry, data_types, elements=forms)
        entry = skips.first_at("/Acme/Interfaces/ISig")
        assert entry is not None and (entry.category, entry.rule) == (_NS, None)
        assert entry.reason == "data element 'value': unresolved reference /Acme/Types/Odd"


_MERGED_TYPES_ARXML = """<?xml version="1.0" encoding="UTF-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>Left</SHORT-NAME>
          <ELEMENTS>{counter}</ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>Right</SHORT-NAME>
          <ELEMENTS>{counter}</ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>Use</SHORT-NAME>
          <ELEMENTS>
            <IMPLEMENTATION-DATA-TYPE>
              <SHORT-NAME>Ref</SHORT-NAME>
              <CATEGORY>TYPE_REFERENCE</CATEGORY>
              <SW-DATA-DEF-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE"
                      >/Acme/Right/Counter</IMPLEMENTATION-DATA-TYPE-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </SW-DATA-DEF-PROPS>
            </IMPLEMENTATION-DATA-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""

_COUNTER_OVER_A_GHOST_BASE = """
            <IMPLEMENTATION-DATA-TYPE>
              <SHORT-NAME>Counter</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
              <SW-DATA-DEF-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Vendor/GhostBase</BASE-TYPE-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </SW-DATA-DEF-PROPS>
            </IMPLEMENTATION-DATA-TYPE>"""


class TestATypeBehindAMergedGroupsMemberInheritsThePrimarysRoot:
    def test_the_root_is_searched_at_the_primarys_path(self, tmp_path: Path) -> None:
        """``cause_path``: the group's skipped primary holds the entry, not the member the reference names."""
        source = tmp_path / "merged.arxml"
        source.write_text(
            _MERGED_TYPES_ARXML.format(counter=_COUNTER_OVER_A_GHOST_BASE), encoding="utf-8", newline="\n"
        )
        model = read_arxml(str(source))
        decisions = decide_merges(model)
        assert decisions.primary_path("/Acme/Right/Counter") == "/Acme/Left/Counter"
        _data_types, _registry, skips = map_data_types(model, merges=decisions, elements=[])
        root = skips.first_at("/Acme/Left/Counter")
        assert root is not None and (root.category, root.rule) == (_OF, None)
        assert skips.first_at("/Acme/Right/Counter") is None
        entry = skips.first_at("/Acme/Use/Ref")
        _assert_inherits(entry, root)
        assert entry.reason == "unresolved reference /Acme/Right/Counter (base implementation type)"


# ---------------------------------------------------------------------------
# Chain inheritance: a chained entry carries its root's category and rule, a
# section-1 root included
# ---------------------------------------------------------------------------

_UNDECLARED_INTERFACE = "/Vendor/ISig"


def _dup_reader_entry(model: ImportModel, path: str, tag: str) -> SkipEntry:
    """The reader's duplicate full path at *path*: a section-1 root nothing else maps."""
    reason = duplicate_name_reason(f"duplicate full AUTOSAR path (already indexed as '{path}')", variant_rich=False)
    return model.skip_log.record(path, tag, reason.text, category=reason.category, rule=reason.rule, effect="abort")


def _assert_inherits(entry: SkipEntry | None, root: SkipEntry | None) -> None:
    assert entry is not None and root is not None
    assert (entry.category, entry.rule) == (root.category, root.rule)


class TestAnAccessOrEventBehindASkippedPortInheritsThePorts:
    """``PortOutcome.skip`` is the port's entry, and the access and event chains copy it."""

    @pytest.mark.parametrize("subject", ["access", "event"])
    def test_the_chained_line_carries_the_ports_category(self, subject: str) -> None:
        port = SwcPortInfo(
            path=f"{_SWC_PATH}/rSig",
            name="rSig",
            kind="R-PORT-PROTOTYPE",
            interface_ref=_UNDECLARED_INTERFACE,
            description="",
        )
        if subject == "access":
            access = _write_access(port.path)
            model, path = _swc_model(ports=[port], runnables=[_runnable(accesses=[access])]), access.path
        else:
            event = _operation_event(port.path)
            model, path = _swc_model(ports=[port], runnables=[_runnable()], events=[event]), event.path
        _swcs, skips = map_swcs(model, interfaces=[], port_inits={}, variable_types={})
        root, entry = skips.first_at(port.path), skips.first_at(path)
        _assert_inherits(entry, root)
        assert (root.category, root.rule) == (_OF, None)
        assert entry.reason == f"chain-skipped: port 'rSig' was skipped ({root.reason})"

    def test_a_section_one_port_root_is_inherited_with_its_rule(self) -> None:
        """A duplicate port the hand-built model gives a path of its own: its access lands in section 1."""
        first = SwcPortInfo(
            path=f"{_SWC_PATH}/pSig", name="pSig", kind="P-PORT-PROTOTYPE", interface_ref=None, description=""
        )
        twin = SwcPortInfo(
            path=f"{_SWC_PATH}/pSig_twin", name="pSig", kind="P-PORT-PROTOTYPE", interface_ref=None, description=""
        )
        access = _write_access(twin.path)
        model = _swc_model(ports=[first, twin], runnables=[_runnable(accesses=[access])])
        _swcs, skips = map_swcs(model, interfaces=[], port_inits={}, variable_types={})
        entry = skips.first_at(access.path)
        assert entry is not None
        assert (entry.category, entry.rule) == (_ED, StandardRule.SHORT_NAME_UNIQUE)
        assert entry.reason == "chain-skipped: port 'pSig' was skipped (duplicate port name 'pSig' on SWC 'Ctrl')"


class TestTheAccessesOfASkippedRunnableInheritTheRunnables:
    """The runnable's own entry is kept, and each of its accesses is chained to it."""

    @pytest.mark.parametrize("variant_rich", [False, True])
    def test_a_duplicate_runnables_accesses(self, variant_rich: bool) -> None:
        access = _write_access(f"{_SWC_PATH}/pSig")
        model = _swc_model(runnables=[_runnable(), _runnable(accesses=[access])], variant_rich=variant_rich)
        _swcs, skips = map_swcs(model, interfaces=[], port_inits={}, variable_types={})
        (root,) = [entry for entry in skips if entry.tag == "RUNNABLE-ENTITY"]
        (entry,) = [entry for entry in skips if entry.path == access.path]
        _assert_inherits(entry, root)
        expected = (_NS, None) if variant_rich else (_ED, StandardRule.SHORT_NAME_UNIQUE)
        assert (entry.category, entry.rule) == expected
        assert entry.reason == f"chain-skipped: runnable 'Run' was skipped ({root.reason})"


_RECORD = "/Acme/AppTypes/Rec"
_ARRAY = "/Acme/AppTypes/Arr"


def _type_skip(model: ImportModel, path: str) -> SkipEntry:
    _data_types, _registry, skips = map_data_types(model, elements=[])
    entry = skips.first_at(path)
    assert entry is not None
    return entry


def _composite_on(model: ImportModel, composite: str, type_ref: str) -> str:
    """An application record (one field) or array typed by *type_ref*; its path."""
    if composite == "record":
        model.app_record_data_types.append(
            AppRecordDataTypeInfo(
                path=_RECORD, name="Rec", elements=[RecordElementInfo(name="f", type_ref=type_ref)], description=""
            )
        )
        return _RECORD
    model.app_array_data_types.append(
        AppArrayDataTypeInfo(
            path=_ARRAY, name="Arr", element_type_ref=type_ref, max_number_of_elements=2, description=""
        )
    )
    return _ARRAY


class TestATypeBehindASkippedTypeInheritsItsRoot:
    """The type chains call ``record_chained_to`` with the path they name."""

    @pytest.mark.parametrize("composite", ["record", "array"])
    def test_a_type_behind_a_section_one_root(self, composite: str) -> None:
        model = ImportModel(source_path="inline-test")
        target = "/Acme/AppTypes/Twice"
        root = _dup_reader_entry(model, target, "APPLICATION-PRIMITIVE-DATA-TYPE")
        entry = _type_skip(model, _composite_on(model, composite, target))
        _assert_inherits(entry, root)
        assert (entry.category, entry.rule) == (_ED, StandardRule.SHORT_NAME_UNIQUE)
        cause = "field 'f' type was skipped" if composite == "record" else "element type was skipped"
        assert entry.reason == f"unresolved reference {target} ({cause})"

    @pytest.mark.parametrize("composite", ["record", "array"])
    def test_a_type_behind_a_type_the_file_declares_nowhere_is_outside_the_file(self, composite: str) -> None:
        """``import component``'s case: no policy keeps the path, so the chain takes the fallback."""
        model = ImportModel(source_path="inline-test")
        entry = _type_skip(model, _composite_on(model, composite, "/Vendor/Ghost"))
        assert (entry.category, entry.rule) == (_OF, None)

    def test_a_declared_type_dropped_without_an_entry_is_swcrafts(self) -> None:
        model = ImportModel(source_path="inline-test")
        model.path_index["/Acme/AppTypes/Quiet"] = raw_element("/Acme/AppTypes/Quiet")
        entry = _type_skip(model, _composite_on(model, "record", "/Acme/AppTypes/Quiet"))
        assert (entry.category, entry.rule) == (_NS, None)

    def test_a_value_type_behind_its_skipped_base_type(self) -> None:
        model = ImportModel(source_path="inline-test")
        model.sw_base_types.append(
            SwBaseTypeInfo(
                path=_BASE, name="uint8", size=0, encoding="NONE", native_declaration="uint8", description=""
            )
        )
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path=_COUNTER, name="Counter", category="VALUE", base_type_ref=_BASE, impl_type_ref=None, description=""
            )
        )
        _data_types, _registry, skips = map_data_types(model, elements=[])
        entry = skips.first_at(_COUNTER)
        _assert_inherits(entry, skips.first_at(_BASE))
        assert entry.reason == f"unresolved reference {_BASE} (base type was skipped)"

    def test_a_reference_type_behind_its_skipped_target(self) -> None:
        model = ImportModel(source_path="inline-test")
        target = "/Acme/Types/Odd"
        model.impl_data_types.extend(
            [
                ImplDataTypeInfo(
                    path=target, name="Odd", category="UNION", base_type_ref=None, impl_type_ref=None, description=""
                ),
                ImplDataTypeInfo(
                    path="/Acme/Types/Ref",
                    name="Ref",
                    category="TYPE_REFERENCE",
                    base_type_ref=None,
                    impl_type_ref=target,
                    description="",
                ),
            ]
        )
        _data_types, _registry, skips = map_data_types(model, elements=[])
        entry = skips.first_at("/Acme/Types/Ref")
        _assert_inherits(entry, skips.first_at(target))
        assert entry.reason == f"unresolved reference {target} (base implementation type)"


class TestAPortBehindASkippedInterfaceInheritsTheInterfaces:
    """``map_ports``' chain half searches ``map_interfaces``' log, then the reader's."""

    _INTERFACE = "/Acme/Interfaces/IDup"

    def _model(self) -> ImportModel:
        port = SwcPortInfo(
            path=f"{_SWC_PATH}/rSig",
            name="rSig",
            kind="R-PORT-PROTOTYPE",
            interface_ref=self._INTERFACE,
            description="",
        )
        return _swc_model(ports=[port])

    def test_a_section_one_root_in_the_readers_log(self) -> None:
        model = self._model()
        root = _dup_reader_entry(model, self._INTERFACE, "SENDER-RECEIVER-INTERFACE")
        entry = _one_swc_skip(model, f"{_SWC_PATH}/rSig")
        _assert_inherits(entry, root)
        assert entry.reason == f"unresolved reference {self._INTERFACE} (interface was skipped)"

    def test_the_interface_mappers_log_is_asked_before_the_readers(self) -> None:
        model = self._model()
        _dup_reader_entry(model, self._INTERFACE, "SENDER-RECEIVER-INTERFACE")
        iface_skips = SkipLog()
        root = iface_skips.record(
            self._INTERFACE, "SENDER-RECEIVER-INTERFACE", "no data elements", category=_NS, effect="abort"
        )
        _swcs, skips = map_swcs(model, interfaces=[], iface_skip_log=iface_skips, port_inits={}, variable_types={})
        _assert_inherits(skips.first_at(f"{_SWC_PATH}/rSig"), root)


class TestAConnectorEndBehindASkippedRootInheritsIt:
    """The connector and delegation chains search the composition pass's log, ``map_swcs``' and the reader's."""

    def test_an_end_behind_a_prototype_skipped_as_a_duplicate_member(self) -> None:
        model = _two_swcs()
        twin = _prototype(f"{_COMP}/SwcA_1_twin", "SwcA_1", "/Pkg/Components/SwcB")
        model.path_index[twin.path] = raw_element(twin.path)
        ac = _assembly("A_to_B", _end(_PROTO_A, _PORT_A), _end(twin.path, _PORT_B))
        _comp1(model, prototypes=[_prototype(_PROTO_A, "SwcA_1", "/Pkg/Components/SwcA"), twin], assembly=[ac])
        entry = _composition_skip(model, ac.path)
        assert (entry.category, entry.rule) == (_ED, StandardRule.SHORT_NAME_UNIQUE)
        assert entry.reason == f"requester end: unresolved reference {twin.path} (component prototype was skipped)"

    def test_an_end_behind_a_members_port_map_swcs_skipped(self) -> None:
        """The root is in ``map_swcs``' log, which ``map_compositions`` receives as ``swc_skip_log``."""
        model = _two_swcs(interface_b="/Pkg/Interfaces/Missing")
        model.path_index[_PORT_B] = raw_element(_PORT_B)
        ac = _assembly("A_to_B", _end(_PROTO_A, _PORT_A), _end(_PROTO_B, _PORT_B))
        _comp1(model, assembly=[ac])
        forms: list = []
        data_types, registry, _ = map_data_types(model, elements=forms)
        interfaces, iface_skips = map_interfaces(model, registry, data_types, elements=forms)
        swcs, swc_skips = map_swcs(model, interfaces, iface_skip_log=iface_skips, port_inits={}, variable_types={})
        root = swc_skips.first_at(_PORT_B)
        assert root is not None and (root.category, root.rule) == (_OF, None)
        _mapped, with_log = map_compositions(model, swcs, interfaces, iface_skips, registry, swc_skip_log=swc_skips)
        _assert_inherits(with_log.first_at(ac.path), root)

    def test_a_delegation_behind_a_boundary_port_whose_interface_is_declared_nowhere(self) -> None:
        model = _two_swcs()
        outer = SwcPortInfo(
            path=f"{_COMP}/pOuter",
            name="pOuter",
            kind="P-PORT-PROTOTYPE",
            interface_ref=_UNDECLARED_INTERFACE,
            description="",
        )
        model.path_index[outer.path] = raw_element(outer.path)
        deleg = _delegation("D1", outer.path, _end(_PROTO_A, _PORT_A))
        _comp1(model, ports=[outer], delegations=[deleg])
        _mapped, skips, _registry, _swcs = _map_full(model)
        root, entry = skips.first_at(outer.path), skips.first_at(deleg.path)
        _assert_inherits(entry, root)
        assert (root.category, root.rule) == (_OF, None)
        assert entry.reason == f"unresolved reference {outer.path} (boundary port was skipped)"


class TestChainSkipDecidesByTheCause:
    """``chain_skip``'s four answers: absent, conflict, selection, and a declared target SWCraft skipped."""

    _ACCESS = "/Acme/X/access"

    def _chain(self, model: ImportModel, decisions, target: str) -> tuple[UnresolvedReferences, SkipLog]:
        unresolved = UnresolvedReferences(model, decisions)
        skip_log = SkipLog()
        unresolved.chain_skip(
            skip_log,
            self._ACCESS,
            "VARIABLE-ACCESS",
            "chained",
            target=target,
            layer=LAYER_INTERFACES,
            once_stubbed=lambda stub: f"through the stub {stub}",
            effect="abort",
        )
        return unresolved, skip_log

    def _entry(self, model: ImportModel, decisions, target: str) -> SkipEntry:
        _unresolved, skip_log = self._chain(model, decisions, target)
        (entry,) = skip_log.entries
        return entry

    def test_an_absent_target_is_outside_the_file(self) -> None:
        entry = self._entry(ImportModel(source_path="inline-test"), None, _UNDECLARED_INTERFACE)
        assert (entry.category, entry.rule) == (_OF, None)

    @staticmethod
    def _two_trees() -> ImportModel:
        return _com_spec_model(
            trees={"TreeA": "alpha", "TreeB": "beta"}, port_interface="/Acme/TreeB/ISig", element_ref="/x"
        )

    def test_a_conflicting_target_is_swcrafts(self) -> None:
        model = self._two_trees()
        entry = self._entry(model, decide_merges(model), "/Acme/TreeB/ISig")
        assert (entry.category, entry.rule) == (_NS, None)

    def test_a_target_the_selection_left_out_is_swcrafts(self) -> None:
        model = self._two_trees()
        decisions = narrow_to_selection(
            decide_merges(model), selected=frozenset({"/Acme/TreeA/ISig", _SWC_PATH}), reached=frozenset()
        )
        entry = self._entry(model, decisions, "/Acme/TreeB/ISig")
        assert (entry.category, entry.rule) == (_NS, None)

    def test_a_declared_target_swcraft_skipped_is_chained_to_its_entry(self) -> None:
        model = ImportModel(source_path="inline-test")
        root = _dup_reader_entry(model, "/Acme/Interfaces/IDup", "SENDER-RECEIVER-INTERFACE")
        entry = self._entry(model, None, "/Acme/Interfaces/IDup")
        _assert_inherits(entry, root)
        assert (entry.category, entry.rule) == (_ED, StandardRule.SHORT_NAME_UNIQUE)

    def test_a_declared_target_with_no_entry_is_swcrafts(self) -> None:
        model = ImportModel(source_path="inline-test")
        model.path_index["/Acme/Interfaces/IQuiet"] = raw_element("/Acme/Interfaces/IQuiet")
        entry = self._entry(model, None, "/Acme/Interfaces/IQuiet")
        assert (entry.category, entry.rule) == (_NS, None)

    def test_name_written_stubs_rewords_the_text_and_keeps_both_fields(self) -> None:
        """The stub is planted by hand here; ``test_external_stubs_import.py`` meets it through ``mint_stubs``."""
        unresolved, skip_log = self._chain(ImportModel(source_path="inline-test"), None, _UNDECLARED_INTERFACE)
        unresolved._stubbed[_UNDECLARED_INTERFACE] = ExternalStub(
            layer=LAYER_INTERFACES,
            name="ISig",
            target=_UNDECLARED_INTERFACE,
            dest="SENDER-RECEIVER-INTERFACE",
            referrers=(),
        )
        unresolved.name_written_stubs(interface_names={"ISig"}, type_names=set())
        (entry,) = skip_log.entries
        assert entry.reason == "through the stub ISig"
        assert (entry.category, entry.rule) == (_OF, None)


class TestChainedReason:
    """``merge.chained_reason``: the chain half for a declared cause, returned for a wrapping caller.

    Every caller asks whether the file declares the cause first and words the
    outside-the-file half itself, so the answer is the root's, or SWCraft's.
    """

    def test_the_first_log_holding_an_entry_wins(self) -> None:
        near, far = SkipLog(), SkipLog()
        root = near.record("/p", "T", "near", category=_OF, effect="abort")
        far.record("/p", "T", "far", category=_ED, rule=StandardRule.SHORT_NAME_UNIQUE, effect="abort")
        reason = chained_reason("text", cause_path="/p", chain_logs=(near, far))
        assert (reason.text, reason.category, reason.rule) == ("text", root.category, root.rule)

    def test_a_cause_with_no_entry_is_swcrafts(self) -> None:
        reason = chained_reason("text", cause_path="/p", chain_logs=(SkipLog(),))
        assert (reason.category, reason.rule) == (_NS, None)

    def test_record_chained_to_asks_its_upstream_after_its_own_log(self) -> None:
        own, upstream = SkipLog(), SkipLog()
        root = upstream.record("/p", "T", "root", category=_ED, rule=StandardRule.SHORT_NAME_UNIQUE, effect="abort")
        entry = record_chained_to(own, frozenset({"/p"}), "/q", "T", "chained", cause_path="/p", upstream=(upstream,))
        _assert_inherits(entry, root)


# ---------------------------------------------------------------------------
# The gate: every record site chooses a category
# ---------------------------------------------------------------------------

#: The calls the gate matches, by the name the call spells.
_NAMED_CALLS = frozenset(
    {"SkipEntry", "record_chained", "record_chained_to", "record_reason", "_skip_port", "chain_skip"}
)

#: Calls that pass no ``category``: the policy, the root or the reason decides it.
_CATEGORY_DECIDED_ELSEWHERE = frozenset(
    {"chain_skip", "record_chained", "record_chained_to", "record_reason", "_skip_port"}
)

#: Every matched call in ``packages/tools/src/swcraft_tools/``, by kind. A record site added or
#: removed moves its kind's figure on purpose. A category and rule decided where the cause is found
#: travel in a value, so the literal-defect check does not reach them; their own tests hold them:
#:
#: - an assembly connector's duplicate name inside **one** composition (``[constr_2508]``), apart
#:   from the same name across two (SWCraft's flat ``connectors/``);
#: - ``report.case_only_duplicate_skip``, from ``duplicate_name_reason`` (``[constr_2508]``, or
#:   section 2 in a variant-rich file; ``test_package_placement_import.py``);
#: - ``resolve_impl_type``'s blank sub-element name (``[TPS_GST_00057]``), ``_map_record``'s blank
#:   field name (``[TPS_GST_00057]``) and base-typed field (``[TPS_GST_00020]``), ``_map_array``'s
#:   element typed by an implementation type (``[TPS_GST_00020]``), and ``map_interfaces``' enum
#:   init matching no literal (``[constr_1221]``);
#: - the missing or blank ``SHORT-NAME`` sites, through the reader's ``blank_short_name``
#:   (``[TPS_GST_00057]``; :class:`TestTheReasonFactories`);
#: - ``swc_mapping.report_refused_combination`` (``[TPS_SWCT_01219]``, ``[constr_1174]``,
#:   ``[constr_1277]``; ``test_import_refused_combinations.py``);
#: - ``settle_runnable_symbols``' stated symbol two runnables share (``[constr_2025]``), and the
#:   reader's ``_report_linear_scale_short_label`` (``[constr_1375]``).
EXPECTED_SITES = {
    "skip_log.record": 168,
    "SkipEntry": 4,
    "chain_skip": 9,
    "_skip_port": 5,
    "record_chained": 17,
    "record_chained_to": 7,
    "record_reason": 49,
}


def _call_kind(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "record":
        receiver = func.value
        if (isinstance(receiver, ast.Name) and receiver.id == "skip_log") or (
            isinstance(receiver, ast.Attribute) and receiver.attr == "skip_log"
        ):
            return "skip_log.record"
        return None
    name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
    return name if name in _NAMED_CALLS else None


@functools.cache
def _sites() -> tuple[tuple[str, str, ast.Call], ...]:
    sites = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py"), key=lambda p: p.as_posix()):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (kind := _call_kind(node)) is not None:
                where = f"{path.relative_to(PACKAGE_ROOT).as_posix()}:{node.lineno}"
                sites.append((kind, where, node))
    return tuple(sites)


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _is_member(node: ast.expr | None, enum: str, member: str | None = None) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == enum
        and (member is None or node.attr == member)
    )


class TestEveryRecordSiteChoosesACategory:
    """An AST scan over ``packages/tools/src/swcraft_tools/``: every skip is recorded with a category.

    **What it matches:** a call of ``record`` on the name ``skip_log`` or on an
    attribute ending in ``.skip_log``; ``SkipEntry(``; ``record_chained(``;
    ``record_chained_to(``; ``record_reason(``; ``_skip_port(``; ``chain_skip(``.

    **What it asserts:** every matched call except ``chain_skip``,
    ``record_chained``, ``record_chained_to``, ``record_reason`` and
    ``_skip_port`` (which takes a ``SkipReason``) passes ``category=``;
    ``chain_skip`` and ``record_chained_to`` pass none (the policy or the cause
    decides it); a literal
    ``SkipCategory.EXPORT_DEFECT`` comes with a ``rule=`` that is a
    ``StandardRule.<member>`` attribute; ``record_chained`` passes ``root=``; and
    the number of matched calls per kind is :data:`EXPECTED_SITES`.

    **What it does not reach:** a category carried by a value -- a
    ``SkipReason`` handed to ``record_reason`` or ``_skip_port``,
    ``category=category`` inside a wrapper -- or chosen by
    an expression (a site
    that chooses by ``variant_rich`` or by a pair of interface kinds) -- the
    helpers' own returns and those choices are held by the per-family tests,
    not by this scan; a call through an alias of ``skip_log`` under another
    name, which the receiver rule does not know; the local ``record`` in
    ``connectors._census_stub_ports``, excluded by the receiver rule because it
    records no skip; and **whether a category is right** -- only the per-family
    tests and the closed real-export suites test that.
    """

    def test_the_site_count_is_the_counted_one(self) -> None:
        assert dict(Counter(kind for kind, _where, _call in _sites())) == EXPECTED_SITES

    def test_every_site_passes_a_category_or_is_decided_elsewhere(self) -> None:
        missing = [
            where
            for kind, where, call in _sites()
            if kind not in _CATEGORY_DECIDED_ELSEWHERE and _keyword(call, "category") is None
        ]
        assert missing == []

    def test_chain_skip_is_never_handed_a_category(self) -> None:
        handed = [
            where
            for kind, where, call in _sites()
            if kind in {"chain_skip", "record_chained_to"} and _keyword(call, "category") is not None
        ]
        assert handed == []

    def test_a_literal_defect_names_its_rule(self) -> None:
        unruled = [
            where
            for _kind, where, call in _sites()
            if _is_member(_keyword(call, "category"), "SkipCategory", "EXPORT_DEFECT")
            and not _is_member(_keyword(call, "rule"), "StandardRule")
        ]
        assert unruled == []

    def test_record_chained_passes_its_root(self) -> None:
        rootless = [
            where
            for kind, where, call in _sites()
            if kind == "record_chained" and (_keyword(call, "root") is None or _keyword(call, "category") is not None)
        ]
        assert rootless == []


class TestABlankTypeReferenceIsSwcraftsNotOutsideTheFile:
    """A data element, argument, record field or array element with no TYPE-TREF references nothing.

    The reader stores the absent reference as ``""``, which no path set holds,
    so an unguarded ``not in declared`` split filed it under *outside the
    file* -- "another file may declare it" said of nothing. Each
    is SWCraft's refusal, worded like ``map_ports``' "port has no interface
    reference", under ``import model``'s policy and without one alike.
    """

    @staticmethod
    def _interface_skip(model: ImportModel, path: str, *, policy: bool) -> SkipEntry:
        unresolved = UnresolvedReferences(model, decide_merges(model)) if policy else None
        forms: list = []
        data_types, registry, _type_skips = map_data_types(model, unresolved=unresolved, elements=forms)
        _interfaces, skips = map_interfaces(model, registry, data_types, unresolved=unresolved, elements=forms)
        entry = skips.first_at(path)
        assert entry is not None
        return entry

    @pytest.mark.parametrize("policy", [False, True])
    def test_a_data_element_without_a_type_reference(self, policy: bool) -> None:
        model = ImportModel(source_path="inline-test")
        model.sr_interfaces.append(
            SRInterfaceInfo(
                path="/Acme/Interfaces/ISig", name="ISig", description="",
                data_elements=[SRDataElementInfo(name="d", type_ref="")],
            )
        )  # fmt: skip
        entry = self._interface_skip(model, "/Acme/Interfaces/ISig", policy=policy)
        assert (entry.category, entry.rule) == (_NS, None)
        assert entry.reason == "data element 'd': has no TYPE-TREF"

    @pytest.mark.parametrize("policy", [False, True])
    def test_an_argument_without_a_type_reference(self, policy: bool) -> None:
        model = ImportModel(source_path="inline-test")
        argument = CSArgumentInfo(name="a", type_ref="", direction="IN")
        model.cs_interfaces.append(
            CSInterfaceInfo(
                path="/Acme/Interfaces/ICall", name="ICall", description="",
                operations=[CSOperationInfo(name="Op", arguments=[argument])],
            )
        )  # fmt: skip
        entry = self._interface_skip(model, "/Acme/Interfaces/ICall", policy=policy)
        assert (entry.category, entry.rule) == (_NS, None)
        assert entry.reason == "operation 'Op' argument 'a': has no TYPE-TREF"

    @pytest.mark.parametrize("policy", [False, True])
    @pytest.mark.parametrize("composite", ["record", "array"])
    def test_a_composite_member_without_a_type_reference(self, composite: str, policy: bool) -> None:
        model = ImportModel(source_path="inline-test")
        path = _composite_on(model, composite, "")
        unresolved = UnresolvedReferences(model, decide_merges(model)) if policy else None
        _data_types, _registry, skips = map_data_types(model, unresolved=unresolved, elements=[])
        entry = skips.first_at(path)
        assert entry is not None
        assert (entry.category, entry.rule) == (_NS, None)
        expected = "field 'f' has no TYPE-TREF" if composite == "record" else "array element has no TYPE-TREF"
        assert entry.reason == expected

    def test_record_chained_to_keeps_a_blank_cause_swcrafts(self) -> None:
        """The backstop: a blank *cause_path* is never *outside the file*, whatever the caller."""
        skip_log = SkipLog()
        entry = record_chained_to(skip_log, frozenset(), "/Acme/X", "TAG", "reason", cause_path="")
        assert (entry.category, entry.rule) == (_NS, None)


class TestTheReasonFactories:
    """The reader's two reason factories decide the category (and the rule) their call sites no longer spell."""

    def test_a_blank_short_name_is_the_files_defect_citing_tps_gst_00057(self) -> None:
        for reason in (blank_short_name(), blank_short_name("port has missing or blank SHORT-NAME")):
            assert reason.category is SkipCategory.EXPORT_DEFECT
            assert reason.rule is StandardRule.SHORT_NAME_REQUIRED
        assert blank_short_name().text == "missing or blank SHORT-NAME"
        assert blank_short_name("port has missing or blank SHORT-NAME").text == "port has missing or blank SHORT-NAME"

    def test_not_supported_is_swcrafts_own_refusal_with_no_rule(self) -> None:
        reason = not_supported("has no TYPE-TREF")
        assert (reason.text, reason.category, reason.rule) == ("has no TYPE-TREF", SkipCategory.NOT_SUPPORTED, None)


# ---------------------------------------------------------------------------
# Every record site states its effect
# ---------------------------------------------------------------------------

#: The kinds that inherit their cause's effect instead of stating one.
_EFFECT_INHERITED = frozenset({"record_chained", "record_chained_to"})

#: Every site whose effect is not a literal ``"abort"`` / ``ABORT``, by module and
#: enclosing function, with what it states: ``report`` for a descriptive family
#: (F1, F9, F10, F12, F13, F14, F15, F19, F20, F22), ``passed on`` for
#: a wrapper handing its caller's keyword on, ``inherited`` for the chain helper
#: whose root decides. A site not named here states ``abort``.
EFFECT_EXCEPTIONS = {
    ("arxml_reader.py", "_report_text_form"): ["report"],
    ("arxml_reader.py", "_report_uncarried_calibration_access"): ["report"],
    ("arxml_reader.py", "_report_unsupported_calibration_access"): ["report"],
    ("arxml_reader.py", "_report_constr_level"): ["report"],
    ("arxml_reader.py", "_report_scale_short_label"): ["report"],
    ("arxml_reader.py", "_report_linear_scale_short_label"): ["report"],
    ("arxml_reader.py", "_report_documentation_group"): ["report"],
    ("arxml_reader.py", "_report_package_content"): ["report"],
    ("arxml_import/type_mapping.py", "report_value_label"): ["report"],
    ("arxml_import/type_mapping.py", "report_value_line"): ["report"],
    ("arxml_import/swc_mapping.py", "_report_port_value_line"): ["report"],
    ("arxml_import/swc_mapping.py", "_report_port_init_labels"): ["report"],
    # A carried variable value's descriptive lines, and a stated policy on a variable.
    ("arxml_import/swc_mapping.py", "map_variables"): ["report"],
    ("arxml_reader.py", "_read_swc_variables"): ["<REPORT if policy == 'STANDARD' else ABORT>"],
    ("arxml_import/type_mappers.py", "_map_application_primitive"): ["report"],
    ("arxml_import/type_mappers.py", "_map_application_type"): ["report"],
    # The second: the self-map line -- `report` for the map row that caused the importer's own twin
    # collapse, folded into it, `abort` for every other self-map.
    ("arxml_import/type_mapping.py", "_map_set_row"): ["report", "<REPORT if folded else ABORT>"],
    # F23: the line's own `lost` decides -- an
    # own-side infinite limit carried as the absent bound is descriptive, a limit not carried is not.
    ("arxml_import/type_mapping.py", "report_linear_limits"): ["<ABORT if line.lost else REPORT>"],
    # The third: an execution-time budget's SWC-TIMING placed or split otherwise than the writer
    # writes it.
    ("arxml_import/report.py", "generated_name_losses"): ["report", "report", "report"],
    # A port API option with both features off requests nothing ([TPS_SWCT_01355], [TPS_SWCT_01356]), so its
    # line is descriptive; every other option's line is behavioural -- decided by the reason's absence, never
    # by its text.
    ("arxml_import/swc_mapping.py", "report_port_api_options"): ["report"],
    # Every com-spec value loss on a descriptive port -- F4 and its stub arm, F5, a port's own init value, the unread
    # and the unbindable port init, and the two stub passes over a port's own value -- is descriptive.
    ("arxml_import/swc_mapping.py", "_report_com_spec_loss"): ["report"],
    ("arxml_import/swc_mapping.py", "_report_stub_com_spec"): ["report"],
    ("arxml_import/swc_mapping.py", "_port_init_lines"): ["report", "report"],
    ("arxml_import/type_mapping.py", "port_com_spec_inits"): ["report"],
    ("arxml_import/type_mapping.py", "report_unread_port_com_spec_inits"): ["report", "report"],
    ("arxml_import/type_mapping.py", "record"): ["report", "report"],
    ("arxml_import/orchestrator.py", "without_port_inits_through_a_stub"): ["report"],
    ("arxml_import/orchestrator.py", "name_equivalent_port_inits_through_a_stub"): ["report"],
    ("arxml_import/unresolved.py", "chain_skip"): ["passed on", "passed on"],
    ("arxml_import/swc_mapping.py", "_skip_port"): ["passed on"],
    ("arxml_import/merge.py", "record_chained_to"): ["inherited"],
    ("arxml_reader.py", "record"): ["passed on"],
    # What an SWC-TIMING holds that no budget is, a carried budget's minimum and traceable children and
    # a guarantee are descriptive; the index's ADMIN-DATA line is descriptive on a budget's two elements
    # alone and behavioural everywhere else.
    ("arxml_reader.py", "_report_timing_loss"): ["report"],
    ("arxml_reader.py", "_index_named_element"): [
        "<REPORT if local_tag(el.tag) in _DESCRIPTIVE_ADMIN_DATA_OWNERS else ABORT>"
    ],
    ("arxml_import/swc_mapping.py", "record_budgets"): ["report", "report"],
}


def _effect_of(node: ast.expr | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant) and node.value in ("abort", "report"):
        return node.value
    if isinstance(node, ast.Name) and node.id in ("ABORT", "REPORT"):
        return node.id.lower()
    if isinstance(node, ast.Name) and node.id == "effect":
        return "passed on"
    if isinstance(node, ast.IfExp) and isinstance(node.body, ast.Attribute) and node.body.attr == "effect":
        return "inherited"
    return f"<{ast.unparse(node)}>"


@functools.cache
def _effect_sites() -> tuple[tuple[str, str, str, str | None], ...]:
    """``(kind, module, enclosing function, effect)`` for every matched call, in source order."""
    sites = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py"), key=lambda p: p.as_posix()):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = path.relative_to(PACKAGE_ROOT).as_posix()

        def visit(node: ast.AST, function: str, module: str = module) -> None:
            for child in ast.iter_child_nodes(node):
                name = child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else function
                if isinstance(child, ast.Call) and (kind := _call_kind(child)) is not None:
                    sites.append((kind, module, function, _effect_of(_keyword(child, "effect"))))
                visit(child, name)

        visit(tree, "<module>")
    return tuple(sites)


class TestEverySiteStatesItsEffect:
    """The twin of :class:`TestEveryRecordSiteChoosesACategory`, for ``effect``.

    **What it asserts:** every matched call of :data:`EXPECTED_SITES`' seven kinds except the
    two that inherit (``record_chained``, ``record_chained_to``) passes ``effect=``; a site's
    value is ``"abort"`` unless :data:`EFFECT_EXCEPTIONS` names its module and function; and each
    exception holds exactly the values listed -- so a descriptive family moved to ``abort``, or a
    behavioural one to ``report``, fails here by name.

    **What it does not reach:** whether a family's class is right -- decided per family and
    held by ``test_uncarried_losses.py`` behaviourally; and a site whose
    value is an expression other than the three shapes :func:`_effect_of` names, which it
    reports as ``<expression>`` and so fails.
    """

    def test_every_site_but_the_inheriting_two_passes_effect(self) -> None:
        missing = [
            (module, function)
            for kind, module, function, effect in _effect_sites()
            if kind not in _EFFECT_INHERITED and effect is None
        ]
        assert missing == []

    def test_the_inheriting_two_pass_none(self) -> None:
        handed = [
            (module, function)
            for kind, module, function, effect in _effect_sites()
            if kind in _EFFECT_INHERITED and effect is not None
        ]
        assert handed == []

    def test_every_site_states_abort_but_the_named_exceptions(self) -> None:
        exceptions: dict[tuple[str, str], list[str]] = {}
        for kind, module, function, effect in _effect_sites():
            if kind in _EFFECT_INHERITED:
                continue
            if effect != "abort":
                exceptions.setdefault((module, function), []).append(effect)
        assert exceptions == EFFECT_EXCEPTIONS

    def test_a_chained_entry_inherits_its_roots_effect(self) -> None:
        log = SkipLog()
        root = log.record("/P/X", "TAG", "root", category=SkipCategory.NOT_SUPPORTED, effect="report")
        chained = log.record_chained("/P/Y", "TAG", "chained", root=root)
        assert chained.effect == "report"

    def test_a_descriptive_entry_is_never_a_root(self) -> None:
        """A ``report`` entry documents an imported element, so it decides no fate (``SkipLog.first_at``)."""
        log = SkipLog()
        log.record("/P/X", "UUID", "documentation", category=SkipCategory.NOT_SUPPORTED, effect="report")
        assert log.first_at("/P/X") is None
        entry = log.record("/P/X", "TAG", "skipped", category=SkipCategory.NOT_SUPPORTED, effect="abort")
        assert log.first_at("/P/X") is entry

    def test_the_effect_has_no_default_and_is_one_of_two(self) -> None:
        with pytest.raises(TypeError):
            SkipLog().record("/P/X", "TAG", "reason", category=SkipCategory.NOT_SUPPORTED)  # type: ignore[call-arg]
        with pytest.raises(ValueError, match="effect must be one of"):
            SkipLog().record("/P/X", "TAG", "reason", category=SkipCategory.NOT_SUPPORTED, effect="warn")  # type: ignore[arg-type]
