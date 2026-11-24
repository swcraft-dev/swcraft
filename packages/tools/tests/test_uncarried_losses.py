"""Every other loss is a report line, and only a real loss is.

A source value that equals what SWCraft's output states for the element is carried by **equivalence**
and gets no line; every other value the importer does not carry is exactly one entry of one family, keyed
by one ``(path, tag)`` pair, and each family is **behavioural** (``import component`` aborts on
it) or **descriptive** (printed in the preview and under ``--dry-run``, in
*Not carried from the file*, and imported past).

The families are held here on hand-built files, one family at a time: each test starts from
SWCraft's own whole-model export of the ``component_exchange`` tree, adds exactly one source
construct, imports the result (``import model``, dry run) and asserts the entries the construct
adds -- their key, their category, their effect -- beside the baseline's. What the baseline itself
reports is a SWCraft-written file's report, held by :class:`TestASwcraftWrittenFileProducesNoLine`.

What this does not reach: the counts on real tool exports, which are the closed suites'; whether a
family's behavioural/descriptive class is right, which is a design judgement, never an RTE's.
"""

from __future__ import annotations

import re
import shutil
from collections import Counter
from pathlib import Path

import pytest
import yaml
from behaviour_losses_support import add_xml, snippet
from committed_trees_support import committed_model_roots, tree_id
from conftest import model_element_path
from lxml import etree
from project_support import init_project
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.type_mapping import _SW_CALIBRATION_ACCESS_VALUES, _SW_IMPL_POLICY_VALUES
from swcraft_tools.arxml_reader import (
    CARRIED_SW_CALIBRATION_ACCESSES,
    CARRIED_SW_IMPL_POLICIES,
    SkipCategory,
    SkipEntry,
    StandardRule,
)
from swcraft_tools.arxml_version import NAMESPACE, ArxmlVersion
from swcraft_tools.component_exchange import LossyComponentImportError, export_component, import_component
from swcraft_tools.component_exchange.report import NOT_CARRIED_HEADING
from swcraft_tools.model_state import ElementParseError
from swcraft_tools.writer import generate_arxml

NS = f"{{{NAMESPACE}}}"
EXCHANGE_MODEL_ROOT = Path(__file__).parent / "fixtures" / "component_exchange" / "model"
INIT_VALUES_MODEL_ROOT = Path(__file__).parent / "fixtures" / "init_values" / "model"

BEHAVIOURAL = "abort"
DESCRIPTIVE = "report"
NOT_SUPPORTED = SkipCategory.NOT_SUPPORTED


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _whole_model(model_root: Path, out: Path, version: ArxmlVersion | None = None) -> Path:
    generate_arxml(str(model_root), str(out), **({"version": version} if version is not None else {}))
    return out


@pytest.fixture(scope="module")
def exchange_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """SWCraft's own whole-model export of the ``component_exchange`` tree."""
    return _whole_model(EXCHANGE_MODEL_ROOT, tmp_path_factory.mktemp("exchange") / "model.arxml")


def _element(root: etree._Element, path: str) -> etree._Element:
    """The element at the AUTOSAR *path*, found by ``SHORT-NAME`` segment by segment."""
    node = root
    for segment in path.strip("/").split("/"):
        node = next(
            candidate
            for candidate in node.iter()
            if isinstance(candidate.tag, str)
            and candidate is not node
            and candidate.findtext(f"{NS}SHORT-NAME") == segment
            and _nearest_named_ancestor(candidate) is node
        )
    return node


def _nearest_named_ancestor(element: etree._Element) -> etree._Element:
    parent = element.getparent()
    while parent is not None and parent.find(f"{NS}SHORT-NAME") is None and parent.getparent() is not None:
        parent = parent.getparent()
    return parent


def _conditional(element: etree._Element) -> etree._Element:
    return element.find(f"{NS}SW-DATA-DEF-PROPS/{NS}SW-DATA-DEF-PROPS-VARIANTS/{NS}SW-DATA-DEF-PROPS-CONDITIONAL")


def _ensure_conditional(element: etree._Element) -> etree._Element:
    conditional = _conditional(element)
    if conditional is None:
        props = snippet(
            "<SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL/>"
            "</SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>"
        )[0]
        element.find(f"{NS}SHORT-NAME").addnext(props)
        conditional = _conditional(element)
    return conditional


def _insert_after_short_name(element: etree._Element, xml: str) -> None:
    anchor = element.find(f"{NS}SHORT-NAME")
    for node in reversed(snippet(xml)):
        anchor.addnext(node)


def _save(tree: etree._ElementTree, path: Path) -> Path:
    path.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    return path


def _import(source: Path, tmp_path: Path, mutate=None) -> list[SkipEntry]:
    """``import model`` (dry run) of *source* after *mutate(root)*; the report's entries."""
    tree = etree.parse(str(source))
    if mutate is not None:
        mutate(tree.getroot())
    tmp_path.mkdir(parents=True, exist_ok=True)
    patched = _save(tree, tmp_path / "source.arxml")
    project = init_project(tmp_path / "project")
    return list(run_import(str(patched), str(project / "swcraft.yaml"), dry_run=True).skip_entries)


def _added(source: Path, tmp_path: Path, mutate) -> list[tuple[str, str, str, SkipCategory]]:
    """The entries *mutate* adds to *source*'s own report, as ``(path, tag, effect, category)``."""
    baseline = Counter(_key(entry) for entry in _import(source, tmp_path / "baseline"))
    after = Counter(_key(entry) for entry in _import(source, tmp_path / "after", mutate))
    return sorted(((path, tag, effect, category) for path, tag, effect, category, _reason in after - baseline))


def _added_entries(source: Path, tmp_path: Path, mutate) -> list[SkipEntry]:
    baseline = Counter(_key(entry) for entry in _import(source, tmp_path / "baseline"))
    added: list[SkipEntry] = []
    for entry in _import(source, tmp_path / "after", mutate):
        if baseline[_key(entry)]:
            baseline[_key(entry)] -= 1
        else:
            added.append(entry)
    return added


def _key(entry: SkipEntry) -> tuple[str, str, str, SkipCategory, str]:
    return (entry.path, entry.tag, entry.effect, entry.category, entry.reason)


# ---------------------------------------------------------------------------
# The families, one construct at a time
# ---------------------------------------------------------------------------

ICAL = "/PortInterfaces/ICalibration"
SET_OFFSET = f"{ICAL}/SetOffset"
ACTUATOR = "/ComponentTypes/ActuatorSwc"
BEHAVIOUR = f"{ACTUATOR}/ActuatorSwc_InternalBehavior"


class TestTheDataDefinitionFamilies:
    """F1, F2, F3, F17, F21 and F22: a ``SW-DATA-DEF-PROPS`` child the model does not carry."""

    def test_f1_a_calibration_access_on_an_argument_other_than_not_accessible(self, exchange_source, tmp_path) -> None:
        def mutate(root):
            _conditional(_element(root, f"{SET_OFFSET}/raw")).find(f"{NS}SW-CALIBRATION-ACCESS").text = "READ-WRITE"

        assert _added(exchange_source, tmp_path, mutate) == [
            (f"{SET_OFFSET}/raw/SW-DATA-DEF-PROPS", "SW-CALIBRATION-ACCESS", DESCRIPTIVE, NOT_SUPPORTED)
        ]

    def test_f1_the_writers_own_not_accessible_is_carried_by_equivalence(self, exchange_source, tmp_path) -> None:
        assert _added(exchange_source, tmp_path, lambda root: None) == []
        assert [e for e in _import(exchange_source, tmp_path) if e.tag == "SW-CALIBRATION-ACCESS"] == []

    def test_f2_an_implementation_types_compu_method_naming_a_table_is_carried(self, exchange_source, tmp_path) -> None:
        """The reference is carried, so family F2 gives no line.

        A second referrer makes ``Gear_T_CompuMethod`` no private form of ``Gear_T``: it is a
        ``texttable`` element, and the value type names it.
        """

        def mutate(root):
            add_xml(
                _conditional(_element(root, "/DataTypes/Implementation/Speed_T")),
                '<COMPU-METHOD-REF DEST="COMPU-METHOD">/DataTypes/Application/Gear_T_CompuMethod</COMPU-METHOD-REF>',
            )

        assert _added(exchange_source, tmp_path, mutate) == []

    def test_f2_an_implementation_types_compu_method_naming_no_table_keeps_its_line(
        self, exchange_source, tmp_path
    ) -> None:
        """A ``LINEAR`` method is no table an implementation type may state (``[constr_1158]``): one line, re-worded."""

        def mutate(root):
            add_xml(
                _conditional(_element(root, "/DataTypes/Implementation/Speed_T")),
                '<COMPU-METHOD-REF DEST="COMPU-METHOD">/DataTypes/Application/Velocity_T_Compu</COMPU-METHOD-REF>',
            )

        (entry,) = _added_entries(exchange_source, tmp_path, mutate)
        assert (entry.path, entry.tag, entry.effect, entry.category) == (
            "/DataTypes/Implementation/Speed_T/SW-DATA-DEF-PROPS",
            "COMPU-METHOD-REF",
            BEHAVIOURAL,
            NOT_SUPPORTED,
        )
        assert "is not imported as a texttable element" in entry.reason

    def test_f3_an_unread_child_a_further_conditional_and_a_policy_on_an_argument(
        self, exchange_source, tmp_path
    ) -> None:
        def mutate(root):
            add_xml(
                _ensure_conditional(_element(root, "/PortInterfaces/IVehicleSpeed/speed")),
                '<SW-ADDR-METHOD-REF DEST="SW-ADDR-METHOD">/X/CODE</SW-ADDR-METHOD-REF>',
            )
            variants = _element(root, "/DataTypes/Implementation/Status_T").find(
                f"{NS}SW-DATA-DEF-PROPS/{NS}SW-DATA-DEF-PROPS-VARIANTS"
            )
            add_xml(
                variants,
                "<SW-DATA-DEF-PROPS-CONDITIONAL><SW-INTENDED-RESOLUTION>1</SW-INTENDED-RESOLUTION></SW-DATA-DEF-PROPS-CONDITIONAL>",
            )
            add_xml(_conditional(_element(root, f"{SET_OFFSET}/raw")), "<SW-IMPL-POLICY>STANDARD</SW-IMPL-POLICY>")

        assert _added(exchange_source, tmp_path, mutate) == sorted(
            [
                (
                    "/PortInterfaces/IVehicleSpeed/speed/SW-DATA-DEF-PROPS",
                    "SW-ADDR-METHOD-REF",
                    BEHAVIOURAL,
                    NOT_SUPPORTED,
                ),
                (
                    "/DataTypes/Implementation/Status_T/SW-DATA-DEF-PROPS",
                    "SW-DATA-DEF-PROPS-CONDITIONAL",
                    BEHAVIOURAL,
                    NOT_SUPPORTED,
                ),
                (f"{SET_OFFSET}/raw/SW-DATA-DEF-PROPS", "SW-IMPL-POLICY", BEHAVIOURAL, NOT_SUPPORTED),
            ]
        )

    def test_f17_an_implementation_types_constraint(self, exchange_source, tmp_path) -> None:
        def mutate(root):
            add_xml(
                _conditional(_element(root, "/DataTypes/Implementation/Speed_T")),
                '<DATA-CONSTR-REF DEST="DATA-CONSTR">/DataTypes/Application/Velocity_T_Range</DATA-CONSTR-REF>',
            )

        assert _added(exchange_source, tmp_path, mutate) == [
            ("/DataTypes/Implementation/Speed_T/SW-DATA-DEF-PROPS", "DATA-CONSTR-REF", BEHAVIOURAL, NOT_SUPPORTED)
        ]

    def test_f21_a_measurement_point_policy(self, exchange_source, tmp_path) -> None:
        def mutate(root):
            add_xml(
                _ensure_conditional(_element(root, "/PortInterfaces/IVehicleSpeed/speed")),
                "<SW-IMPL-POLICY>MEASUREMENT-POINT</SW-IMPL-POLICY>",
            )

        assert _added(exchange_source, tmp_path, mutate) == [
            ("/PortInterfaces/IVehicleSpeed/speed/SW-DATA-DEF-PROPS", "SW-IMPL-POLICY", BEHAVIOURAL, NOT_SUPPORTED)
        ]

    def test_f22_a_calibration_access_constr_1017_refuses_beside_queued(self, exchange_source, tmp_path) -> None:
        def mutate(root):
            add_xml(
                _ensure_conditional(_element(root, "/PortInterfaces/IActuatorStatus/status")),
                "<SW-CALIBRATION-ACCESS>READ-ONLY</SW-CALIBRATION-ACCESS><SW-IMPL-POLICY>QUEUED</SW-IMPL-POLICY>",
            )

        added = [e for e in _added_entries(exchange_source, tmp_path, mutate) if e.tag == "SW-CALIBRATION-ACCESS"]
        assert [(e.path, e.effect, e.category, e.rule) for e in added] == [
            (
                "/PortInterfaces/IActuatorStatus/status/SW-DATA-DEF-PROPS",
                DESCRIPTIVE,
                SkipCategory.EXPORT_DEFECT,
                StandardRule.CALIBRATION_ACCESS_SUPPORTED,
            )
        ]

    def test_the_readers_carried_literals_are_the_mappers(self) -> None:
        """The reader restates the two literal tables so it need not import the mapper."""
        assert set(_SW_IMPL_POLICY_VALUES) == CARRIED_SW_IMPL_POLICIES
        assert set(_SW_CALIBRATION_ACCESS_VALUES) == CARRIED_SW_CALIBRATION_ACCESSES


class TestTheComSpecFamilies:
    """F4 on a kept port, F5 on a port whose interface is an external interface stub and names no listed member.

    Each a descriptive line: a com-spec value on a descriptive port is never a refusal.
    """

    def _receiver(self, root):
        return _element(root, f"{ACTUATOR}/rSpeed").find(f"{NS}REQUIRED-COM-SPECS/{NS}NONQUEUED-RECEIVER-COM-SPEC")

    def test_f4_a_child_neither_carried_nor_silent(self, exchange_source, tmp_path) -> None:
        def mutate(root):
            add_xml(self._receiver(root), "<HANDLE-DATA-STATUS>true</HANDLE-DATA-STATUS>")

        assert _added(exchange_source, tmp_path, mutate) == [
            (f"{ACTUATOR}/rSpeed/REQUIRED-COM-SPECS", "HANDLE-DATA-STATUS", DESCRIPTIVE, NOT_SUPPORTED)
        ]

    def test_f4_the_one_child_only_4_3_0_declares(self, tmp_path) -> None:
        """``TIMEOUT-SUBSTITUTION-VALUE`` exists at 4.3.0 alone, so the case reads a 4.3.0 file."""
        source = _whole_model(EXCHANGE_MODEL_ROOT, tmp_path / "model-4.3.0.arxml", ArxmlVersion.V4_3_0)

        def mutate(root):
            add_xml(
                self._receiver(root),
                "<TIMEOUT-SUBSTITUTION-VALUE><NUMERICAL-VALUE-SPECIFICATION><VALUE>0</VALUE>"
                "</NUMERICAL-VALUE-SPECIFICATION></TIMEOUT-SUBSTITUTION-VALUE>",
            )

        assert _added(source, tmp_path, mutate) == [
            (f"{ACTUATOR}/rSpeed/REQUIRED-COM-SPECS", "TIMEOUT-SUBSTITUTION-VALUE", DESCRIPTIVE, NOT_SUPPORTED)
        ]

    def test_f4_a_filter_other_than_always(self, exchange_source, tmp_path) -> None:
        def mutate(root):
            # The writer writes no FILTER ([constr_1044]); the
            # source's filter goes at its XSD position, after ENABLE-UPDATE.
            data_filter = etree.Element(f"{NS}FILTER")
            etree.SubElement(data_filter, f"{NS}DATA-FILTER-TYPE").text = "NEVER"
            self._receiver(root).find(f"{NS}ENABLE-UPDATE").addnext(data_filter)

        assert _added(exchange_source, tmp_path, mutate) == [
            (f"{ACTUATOR}/rSpeed/REQUIRED-COM-SPECS", "FILTER", DESCRIPTIVE, NOT_SUPPORTED)
        ]

    def test_f4_a_second_com_spec_for_one_element(self, exchange_source, tmp_path) -> None:
        def mutate(root):
            receiver = self._receiver(root)
            receiver.addnext(etree.fromstring(etree.tostring(receiver)))

        assert _added(exchange_source, tmp_path, mutate) == [
            (f"{ACTUATOR}/rSpeed/REQUIRED-COM-SPECS", "NONQUEUED-RECEIVER-COM-SPEC", DESCRIPTIVE, NOT_SUPPORTED)
        ]

    def test_a_carried_value_is_no_line(self, exchange_source, tmp_path) -> None:
        def mutate(root):
            self._receiver(root).find(f"{NS}ENABLE-UPDATE").text = "true"

        assert _added(exchange_source, tmp_path, mutate) == []

    def test_f5_every_com_spec_of_a_port_on_an_external_interface_stub_naming_no_listed_member(
        self, exchange_source, tmp_path
    ) -> None:
        """The com-spec the stub's member names is carried with no line; the others are F5.

        The stub lists the member the first receiver com-spec names; a com-spec naming a path two segments under the
        stub, and a mode-switch com-spec, are F5."""

        def mutate(root):
            port = _element(root, f"{ACTUATOR}/rSpeed")
            port.find(f"{NS}REQUIRED-INTERFACE-TREF").text = "/Foreign/IForeignSpeed"
            spec = self._receiver(root)
            spec.find(f"{NS}DATA-ELEMENT-REF").text = "/Foreign/IForeignSpeed/speed"
            deeper = etree.fromstring(etree.tostring(spec))
            deeper.find(f"{NS}DATA-ELEMENT-REF").text = "/Foreign/IForeignSpeed/Nested/speed"
            spec.addnext(deeper)
            add_xml(
                port.find(f"{NS}REQUIRED-COM-SPECS"),
                "<MODE-SWITCH-RECEIVER-COM-SPEC><ENHANCED-MODE-API>false</ENHANCED-MODE-API>"
                "</MODE-SWITCH-RECEIVER-COM-SPEC>",
            )

        added = [e for e in _added_entries(exchange_source, tmp_path, mutate) if e.path.endswith("-COM-SPECS")]
        assert sorted((e.path, e.tag, e.effect) for e in added) == [
            (f"{ACTUATOR}/rSpeed/REQUIRED-COM-SPECS", "MODE-SWITCH-RECEIVER-COM-SPEC", DESCRIPTIVE),
            (f"{ACTUATOR}/rSpeed/REQUIRED-COM-SPECS", "NONQUEUED-RECEIVER-COM-SPEC", DESCRIPTIVE),
        ]
        assert all("port on an external interface stub not carried" in e.reason for e in added)


#: Texts ``xsd:double`` does not spell as a finite number (XML Schema Part 2, 3.2.5.1;
#: ``TIME-VALUE--SIMPLE`` restricts it with no facet of its own, XSD R4.3.0:88606-88608;
#: R4.2.2:76972-76974): Python's ``float()`` reads the first three.
_UNREADABLE_TIME_VALUES = ("1_000", " 0.5 ", "１", "INF")


class TestAnUnreadableTimeValueIsNeverZero:
    """A ``TIME-VALUE`` text that is no finite ``xsd:double`` is never read as 0.

    Its field stays absent and the report names it, through each field's own ARXML shape:
    ``MINIMUM-START-INTERVAL`` and ``PERIOD``, which the model holds as a number, skip the runnable
    and the timing event that hold them; an ``ALIVE-TIMEOUT``, which the model may leave absent,
    is F4's value line."""

    RUNNABLE = f"{BEHAVIOUR}/ActuatorMain"

    @staticmethod
    def _reason(tag: str, text: str, holder: str) -> str:
        return (
            f"{tag} {text!r} not carried: the text could not be read as a number (a TIME-VALUE is a finite "
            f"xsd:double), and SWCraft never reads it as 0 -- the {holder} is not imported"
        )

    @pytest.mark.parametrize("text", _UNREADABLE_TIME_VALUES)
    def test_a_minimum_start_interval_skips_its_runnable_with_a_line(self, exchange_source, tmp_path, text) -> None:
        def mutate(root):
            _element(root, self.RUNNABLE).find(f"{NS}MINIMUM-START-INTERVAL").text = text

        own = [(e.tag, e.effect, e.category, e.reason) for e in _added_entries(exchange_source, tmp_path, mutate)]
        assert (
            "RUNNABLE-ENTITY",
            BEHAVIOURAL,
            NOT_SUPPORTED,
            self._reason("MINIMUM-START-INTERVAL", text, "runnable"),
        ) in own

    @pytest.mark.parametrize("text", _UNREADABLE_TIME_VALUES)
    def test_a_period_skips_its_timing_event_with_a_line_saying_so(self, exchange_source, tmp_path, text) -> None:
        paths: list[str] = []

        def mutate(root):
            (event,) = _element(root, BEHAVIOUR).iter(f"{NS}TIMING-EVENT")
            event.find(f"{NS}PERIOD").text = text
            paths.append(f"{BEHAVIOUR}/{event.findtext(f'{NS}SHORT-NAME')}")

        added = _added_entries(exchange_source, tmp_path, mutate)
        assert [(e.path, e.tag, e.effect, e.category, e.reason) for e in added] == [
            (paths[0], "TIMING-EVENT", BEHAVIOURAL, NOT_SUPPORTED, self._reason("PERIOD", text, "timing event"))
        ]

    @pytest.mark.parametrize("text", _UNREADABLE_TIME_VALUES)
    def test_an_alive_timeout_is_f4s_value_line(self, exchange_source, tmp_path, text) -> None:
        """An alive timeout is F4's value line.

        The model's ``alive_timeout`` stays ``None``; the writer writes the silent value it writes for every com-spec
        attribute the model does not hold, at the line's key."""

        def mutate(root):
            receiver = _element(root, f"{ACTUATOR}/rSpeed").find(
                f"{NS}REQUIRED-COM-SPECS/{NS}NONQUEUED-RECEIVER-COM-SPEC"
            )
            receiver.find(f"{NS}ALIVE-TIMEOUT").text = text

        added = _added_entries(exchange_source, tmp_path, mutate)
        assert [(e.path, e.tag, e.effect, e.category, e.reason) for e in added] == [
            (
                f"{ACTUATOR}/rSpeed/REQUIRED-COM-SPECS",
                "ALIVE-TIMEOUT",
                DESCRIPTIVE,
                NOT_SUPPORTED,
                f"ALIVE-TIMEOUT value '{text}' of a com-spec not carried: no model value spells it",
            )
        ]


#: XML's own whitespace (space, tab, carriage return, line feed): what the reader strips from a
#: number, boolean or enumeration leaf, as a stated tolerance.
_XML_PADDING = " \t\r\n"
#: A no-break space, which no grammar of these fields admits and XML does not call white space.
_NO_BREAK_SPACE = " "
COMPU = "/DataTypes/Application"


def _linear_scale(root: etree._Element) -> etree._Element:
    return _element(root, f"{COMPU}/Velocity_T_Compu").find(
        f"{NS}COMPU-INTERNAL-TO-PHYS/{NS}COMPU-SCALES/{NS}COMPU-SCALE"
    )


def _first_texttable_scale(root: etree._Element) -> etree._Element:
    return _element(root, f"{COMPU}/Gear_T_CompuMethod").find(
        f"{NS}COMPU-INTERNAL-TO-PHYS/{NS}COMPU-SCALES/{NS}COMPU-SCALE"
    )


def _pad_text(element: etree._Element, pad: str) -> None:
    element.text = f"{pad}{element.text}{pad}"


def _pad_linear_lower_limit(root: etree._Element, pad: str) -> None:
    coefficients = _linear_scale(root).find(f"{NS}COMPU-RATIONAL-COEFFS")
    for node in snippet(
        f'<LOWER-LIMIT INTERVAL-TYPE="CLOSED">{pad}0{pad}</LOWER-LIMIT>'
        '<UPPER-LIMIT INTERVAL-TYPE="CLOSED">400</UPPER-LIMIT>'
    ):
        coefficients.addprevious(node)


def _pad_compu_numerator_v(root: etree._Element, pad: str) -> None:
    _pad_text(_linear_scale(root).find(f"{NS}COMPU-RATIONAL-COEFFS/{NS}COMPU-NUMERATOR/{NS}V"), pad)


def _pad_texttable_upper_limit(root: etree._Element, pad: str) -> None:
    _pad_text(_first_texttable_scale(root).find(f"{NS}UPPER-LIMIT"), pad)


def _pad_texttable_interval_type(root: etree._Element, pad: str) -> None:
    limit = _first_texttable_scale(root).find(f"{NS}LOWER-LIMIT")
    limit.set("INTERVAL-TYPE", f"{pad}CLOSED{pad}")


def _pad_texttable_short_label(root: etree._Element, pad: str) -> None:
    _pad_text(_first_texttable_scale(root).find(f"{NS}SHORT-LABEL"), pad)


def _pad_phys_upper_infinity(root: etree._Element, pad: str) -> None:
    upper = _element(root, f"{COMPU}/Velocity_T_Range").find(
        f"{NS}DATA-CONSTR-RULES/{NS}DATA-CONSTR-RULE/{NS}PHYS-CONSTRS/{NS}UPPER-LIMIT"
    )
    upper.text = f"{pad}INF{pad}"


def _receiver(root: etree._Element) -> etree._Element:
    return _element(root, f"{ACTUATOR}/rSpeed").find(f"{NS}REQUIRED-COM-SPECS/{NS}NONQUEUED-RECEIVER-COM-SPEC")


def _pad_com_spec_boolean(root: etree._Element, pad: str) -> None:
    _pad_text(_receiver(root).find(f"{NS}ENABLE-UPDATE"), pad)


def _pad_data_filter_type(root: etree._Element, pad: str) -> None:
    _receiver(root).append(snippet(f"<FILTER><DATA-FILTER-TYPE>{pad}ALWAYS{pad}</DATA-FILTER-TYPE></FILTER>")[0])


def _pad_concurrency(root: etree._Element, pad: str) -> None:
    _pad_text(_element(root, f"{BEHAVIOUR}/ActuatorMain").find(f"{NS}CAN-BE-INVOKED-CONCURRENTLY"), pad)


def _pad_termination(root: etree._Element, pad: str) -> None:
    _pad_text(_element(root, BEHAVIOUR).find(f"{NS}HANDLE-TERMINATION-AND-RESTART"), pad)


def _pad_instantiation(root: etree._Element, pad: str) -> None:
    _pad_text(_element(root, BEHAVIOUR).find(f"{NS}SUPPORTS-MULTIPLE-INSTANTIATION"), pad)


def _pad_argument_calibration_access(root: etree._Element, pad: str) -> None:
    _pad_text(_conditional(_element(root, f"{SET_OFFSET}/samples")).find(f"{NS}SW-CALIBRATION-ACCESS"), pad)


def _pad_server_argument_policy(root: etree._Element, pad: str) -> None:
    _pad_text(_element(root, f"{SET_OFFSET}/raw").find(f"{NS}SERVER-ARGUMENT-IMPL-POLICY"), pad)


def _pad_implementation_category(root: etree._Element, pad: str) -> None:
    _pad_text(_element(root, "/DataTypes/Implementation/Speed_T").find(f"{NS}CATEGORY"), pad)


class TestANoBreakSpaceIsNoXmlWhitespace:
    """A number, boolean or enumeration leaf is stripped of XML whitespace only.

    XML whitespace is space, tab, carriage return and line feed, never Python's wider Unicode set,
    so a text padded with a no-break space is no value of its field's grammar and gets that field's
    existing report line rather than being read as the padded token. XML-whitespace padding is
    still read, the stated tolerance (``arxml_reader.stripped_spelling``). One rule across the leaves; each
    case is the field's own ARXML shape in SWCraft's own export.

    What this does not reach: ``IS-SERVICE``, whose unreadable text drops its interface
    (:class:`TestAnUnreadableIsServiceDropsItsInterface`), the fields held elsewhere
    (``INTERNAL-CONSTRS`` in ``test_value_type_range.py``, the server ``QUEUE-LENGTH`` in
    ``test_operation_event_import.py``, the application value's ``CATEGORY`` in
    ``test_arxml_reader.py``, the merge comparison's texts in ``test_arxml_import_merges.py``).
    """

    CASES = [
        pytest.param(
            _pad_linear_lower_limit,
            [
                f"{COMPU}/Velocity_T_Compu/COMPU-INTERNAL-TO-PHYS | LOWER-LIMIT | LOWER-LIMIT of a LINEAR COMPU-SCALE "
                "not carried: it is not a finite number; the scale's limits are imported together or not at all, "
                "so neither is",
                f"{COMPU}/Velocity_T_Compu/COMPU-INTERNAL-TO-PHYS | UPPER-LIMIT | UPPER-LIMIT of a LINEAR COMPU-SCALE "
                "not carried: the scale's LOWER-LIMIT is not readable, and the scale's limits are imported "
                "together or not at all",
            ],
            id="linear-lower-limit",
        ),
        pytest.param(
            _pad_compu_numerator_v,
            [
                f"{COMPU}/Velocity_T: LINEAR compu method {COMPU}/Velocity_T_Compu has a COMPU-NUMERATOR "
                "coefficient that is not a finite number; the type is imported without a conversion",
                f"{COMPU}/Velocity_T_Compu | COMPU-METHOD | non-TEXTTABLE compu-method (category 'LINEAR') was not "
                "read as an application type's conversion and has no SWCraft representation",
            ],
            id="compu-numerator-v",
        ),
        pytest.param(
            _pad_texttable_upper_limit,
            [
                f"{COMPU}/Gear_T_CompuMethod/COMPU-INTERNAL-TO-PHYS | UPPER-LIMIT | UPPER-LIMIT of a TEXTTABLE "
                "COMPU-SCALE not carried: it is not proven equal to what SWCraft writes (the literal's single "
                "value, CLOSED)"
            ],
            id="texttable-upper-limit",
        ),
        pytest.param(
            _pad_texttable_interval_type,
            [
                f"{COMPU}/Gear_T_CompuMethod/COMPU-INTERNAL-TO-PHYS | LOWER-LIMIT | LOWER-LIMIT of a TEXTTABLE "
                "COMPU-SCALE not carried: it is not proven equal to what SWCraft writes (the literal's single "
                "value, CLOSED)"
            ],
            id="texttable-interval-type",
        ),
        pytest.param(
            _pad_texttable_short_label,
            [
                f"{COMPU}/Gear_T_CompuMethod/COMPU-INTERNAL-TO-PHYS | SHORT-LABEL | SHORT-LABEL of a TEXTTABLE "
                "COMPU-SCALE not carried: SWCraft writes the literal as the label"
            ],
            id="texttable-short-label",
        ),
        pytest.param(
            _pad_phys_upper_infinity,
            [
                f"{COMPU}/Velocity_T: DATA-CONSTR {COMPU}/Velocity_T_Range declares a PHYS-CONSTRS UPPER-LIMIT "
                "this release cannot read as a number; that bound is not imported"
            ],
            id="phys-limit-infinity",
        ),
        pytest.param(
            _pad_com_spec_boolean,
            [
                f"{ACTUATOR}/rSpeed/REQUIRED-COM-SPECS | ENABLE-UPDATE | ENABLE-UPDATE value "
                f"'{_NO_BREAK_SPACE}false{_NO_BREAK_SPACE}' of a com-spec not carried: no model value spells it"
            ],
            id="com-spec-boolean",
        ),
        pytest.param(
            _pad_data_filter_type,
            [
                f"{ACTUATOR}/rSpeed/REQUIRED-COM-SPECS | FILTER | FILTER other than DATA-FILTER-TYPE ALWAYS alone "
                "not carried"
            ],
            id="data-filter-type",
        ),
        pytest.param(
            _pad_concurrency,
            [
                f"{BEHAVIOUR}/ActuatorMain | CAN-BE-INVOKED-CONCURRENTLY | unreadable value "
                f"{_NO_BREAK_SPACE + 'false' + _NO_BREAK_SPACE!r} (not an xsd:boolean) -- runnable 'ActuatorMain' "
                "is written with concurrent: false"
            ],
            id="can-be-invoked-concurrently",
        ),
        pytest.param(
            _pad_termination,
            [
                f"{BEHAVIOUR} | HANDLE-TERMINATION-AND-RESTART | unrecognized HANDLE-TERMINATION-AND-RESTART value "
                f"'{_NO_BREAK_SPACE}NO-SUPPORT{_NO_BREAK_SPACE}' (expected one of NO-SUPPORT, CAN-BE-TERMINATED, "
                "CAN-BE-TERMINATED-AND-RESTARTED)"
            ],
            id="handle-termination-and-restart",
        ),
        pytest.param(
            _pad_instantiation,
            [
                f"{BEHAVIOUR} | SUPPORTS-MULTIPLE-INSTANTIATION | SUPPORTS-MULTIPLE-INSTANTIATION is not false -- "
                "the writer emits false"
            ],
            id="supports-multiple-instantiation",
        ),
        pytest.param(
            _pad_argument_calibration_access,
            [
                f"{SET_OFFSET}/samples/SW-DATA-DEF-PROPS | SW-CALIBRATION-ACCESS | SW-CALIBRATION-ACCESS "
                f"{_NO_BREAK_SPACE}NOT-ACCESSIBLE{_NO_BREAK_SPACE} on a ARGUMENT-DATA-PROTOTYPE not carried: "
                "SWCraft writes NOT-ACCESSIBLE there"
            ],
            id="sw-calibration-access",
        ),
        pytest.param(
            _pad_server_argument_policy,
            [
                f"{SET_OFFSET}/raw | SERVER-ARGUMENT-IMPL-POLICY | SERVER-ARGUMENT-IMPL-POLICY "
                f"{_NO_BREAK_SPACE}USE-ARGUMENT-TYPE{_NO_BREAK_SPACE} on a non-array argument not carried: "
                "SWCraft's output means USE-ARGUMENT-TYPE there"
            ],
            id="server-argument-impl-policy",
        ),
        pytest.param(
            _pad_implementation_category,
            [
                f"{ACTUATOR}/rSpeed | R-PORT-PROTOTYPE | unresolved reference /PortInterfaces/IVehicleSpeed "
                "(interface was skipped)",
                "/ComponentTypes/Root/speedLink | ASSEMBLY-SW-CONNECTOR | provider end: unresolved reference "
                "/ComponentTypes/SensorSwc/pSpeed (port was skipped)",
                "/ComponentTypes/SensorSwc/pSpeed | P-PORT-PROTOTYPE | unresolved reference "
                "/PortInterfaces/IVehicleSpeed (interface was skipped)",
                "/DataTypes/Implementation/Speed_T | IMPLEMENTATION-DATA-TYPE | unsupported category "
                f"'{_NO_BREAK_SPACE}VALUE{_NO_BREAK_SPACE}'",
                "/DataTypes/MappingSets/PhysicalTypes/DATA-TYPE-MAPS | DATA-TYPE-MAP | implementation side "
                "/DataTypes/Implementation/Speed_T is an IMPLEMENTATION-DATA-TYPE of CATEGORY "
                f"'{_NO_BREAK_SPACE}VALUE{_NO_BREAK_SPACE}', which this release does not import as a data type",
                "/PortInterfaces/IVehicleSpeed | SENDER-RECEIVER-INTERFACE | data element 'speed': unresolved "
                "reference /DataTypes/Implementation/Speed_T",
                "/PortInterfaces/IVehicleSpeed/speed | VARIABLE-DATA-PROTOTYPE | chain-skipped: interface "
                "'IVehicleSpeed' was skipped (data element 'speed': unresolved reference "
                "/DataTypes/Implementation/Speed_T)",
            ],
            id="implementation-category",
        ),
    ]

    @staticmethod
    def _lines(source: Path, tmp_path: Path, mutate) -> Counter[str]:
        tree = etree.parse(str(source))
        mutate(tree.getroot())
        tmp_path.mkdir(parents=True, exist_ok=True)
        patched = _save(tree, tmp_path / "source.arxml")
        project = init_project(tmp_path / "project")
        report = run_import(str(patched), str(project / "swcraft.yaml"), dry_run=True)
        return Counter(
            [f"{entry.path} | {entry.tag} | {entry.reason}" for entry in report.skip_entries]
            + list(report.unread_physics_lines)
        )

    @pytest.mark.parametrize(("pad", "expected"), CASES)
    def test_xml_whitespace_padding_is_still_read(self, exchange_source, tmp_path, pad, expected) -> None:
        del expected
        assert self._lines(exchange_source, tmp_path / "xml", lambda root: pad(root, _XML_PADDING)) == self._lines(
            exchange_source, tmp_path / "bare", lambda root: pad(root, "")
        )

    @pytest.mark.parametrize(("pad", "expected"), CASES)
    def test_a_no_break_space_is_the_fields_line(self, exchange_source, tmp_path, pad, expected) -> None:
        added = self._lines(exchange_source, tmp_path / "nbsp", lambda root: pad(root, _NO_BREAK_SPACE)) - self._lines(
            exchange_source, tmp_path / "bare", lambda root: pad(root, "")
        )
        assert sorted(added.elements()) == sorted(expected)


#: Texts an ``IS-SERVICE`` does not spell (``BOOLEAN--SIMPLE`` restricts ``xsd:string`` with the
#: pattern ``0|1|true|false``, XSD R4.3.0:79700-79704; R4.2.2:69226-69230): a no-break-space-padded
#: ``true`` and a word the grammar does not name.
_UNREADABLE_IS_SERVICE = (f"{_NO_BREAK_SPACE}true{_NO_BREAK_SPACE}", "yes")
#: Every spelling the grammar admits, and one padded with XML's own whitespace (the stated tolerance).
_READABLE_IS_SERVICE = ("true", "false", "1", "0", f"{_XML_PADDING}true{_XML_PADDING}")
IVEHICLE_SPEED = "/PortInterfaces/IVehicleSpeed"


class TestAnUnreadableIsServiceDropsItsInterface:
    """An ``IS-SERVICE`` text that is no ``xsd:boolean`` after XML's own whitespace is unreadable.

    The model's ``is_service`` is a required boolean, so *unset* is no model state and a
    silent ``false`` is a value the file never stated: the interface is not carried, with one
    behavioural line naming the field, and what depends on it gets the chained or
    unresolved-reference line a dropped interface's dependants already get, as an unreadable time
    value skips its runnable. Each case is the interface's own ARXML shape in SWCraft's
    own export, for both interface kinds.

    The interface's members (a data element, an operation, an argument) are chained to its line at
    their own keys, as a skipped runnable's accesses are; its ports and connector ends get the
    unresolved-reference lines every skipped interface's do. What this does not reach: a dependant
    the ``component_exchange`` tree does not have (a runnable's access through the dropped port, a
    provided client-server port) -- each is the dropped port's or interface's existing chain, held
    by the suites of that chain.
    """

    @staticmethod
    def _reason(text: str) -> str:
        return (
            f"IS-SERVICE {text!r} not carried: the text could not be read as a boolean (an IS-SERVICE is an "
            "xsd:boolean -- true, false, 1 or 0), and SWCraft never reads it as false -- the interface is not "
            "imported"
        )

    @staticmethod
    def _set(path: str, text: str):
        def mutate(root):
            _element(root, path).find(f"{NS}IS-SERVICE").text = text

        return mutate

    @classmethod
    def _chained(cls, name: str, text: str) -> str:
        return f"chain-skipped: interface '{name}' was skipped ({cls._reason(text)})"

    @pytest.mark.parametrize("text", _UNREADABLE_IS_SERVICE)
    def test_a_sender_receiver_interface_is_not_carried_with_its_dependants_lines(
        self, exchange_source, tmp_path, text
    ) -> None:
        added = _added_entries(exchange_source, tmp_path, self._set(IVEHICLE_SPEED, text))
        assert sorted((e.path, e.tag, e.effect, e.category, e.reason) for e in added) == sorted(
            [
                (IVEHICLE_SPEED, "SENDER-RECEIVER-INTERFACE", BEHAVIOURAL, NOT_SUPPORTED, self._reason(text)),
                (
                    f"{IVEHICLE_SPEED}/speed",
                    "VARIABLE-DATA-PROTOTYPE",
                    BEHAVIOURAL,
                    NOT_SUPPORTED,
                    self._chained("IVehicleSpeed", text),
                ),
                (
                    f"{ACTUATOR}/rSpeed",
                    "R-PORT-PROTOTYPE",
                    BEHAVIOURAL,
                    NOT_SUPPORTED,
                    f"unresolved reference {IVEHICLE_SPEED} (interface was skipped)",
                ),
                (
                    "/ComponentTypes/SensorSwc/pSpeed",
                    "P-PORT-PROTOTYPE",
                    BEHAVIOURAL,
                    NOT_SUPPORTED,
                    f"unresolved reference {IVEHICLE_SPEED} (interface was skipped)",
                ),
                (
                    "/ComponentTypes/Root/speedLink",
                    "ASSEMBLY-SW-CONNECTOR",
                    BEHAVIOURAL,
                    NOT_SUPPORTED,
                    "provider end: unresolved reference /ComponentTypes/SensorSwc/pSpeed (port was skipped)",
                ),
            ]
        )

    @pytest.mark.parametrize("text", _UNREADABLE_IS_SERVICE)
    def test_a_client_server_interface_is_not_carried_with_its_dependants_lines(
        self, exchange_source, tmp_path, text
    ) -> None:
        added = _added_entries(exchange_source, tmp_path, self._set(ICAL, text))
        chained = self._chained("ICalibration", text)
        assert sorted((e.path, e.tag, e.effect, e.category, e.reason) for e in added) == sorted(
            [
                (ICAL, "CLIENT-SERVER-INTERFACE", BEHAVIOURAL, NOT_SUPPORTED, self._reason(text)),
                (SET_OFFSET, "CLIENT-SERVER-OPERATION", BEHAVIOURAL, NOT_SUPPORTED, chained),
                (f"{SET_OFFSET}/raw", "ARGUMENT-DATA-PROTOTYPE", BEHAVIOURAL, NOT_SUPPORTED, chained),
                (f"{SET_OFFSET}/samples", "ARGUMENT-DATA-PROTOTYPE", BEHAVIOURAL, NOT_SUPPORTED, chained),
                (
                    "/ComponentTypes/SensorSwc/rCalib",
                    "R-PORT-PROTOTYPE",
                    BEHAVIOURAL,
                    NOT_SUPPORTED,
                    f"unresolved reference {ICAL} (interface was skipped)",
                ),
            ]
        )

    @pytest.mark.parametrize("text", _UNREADABLE_IS_SERVICE)
    def test_no_interface_file_is_written(self, exchange_source, tmp_path, text) -> None:
        tree = etree.parse(str(exchange_source))
        self._set(IVEHICLE_SPEED, text)(tree.getroot())
        patched = _save(tree, tmp_path / "source.arxml")
        project = init_project(tmp_path / "project")
        run_import(str(patched), str(project / "swcraft.yaml"), force=True)
        names = {
            yaml.safe_load(path.read_text(encoding="utf-8")).get("name")
            for path in (project / "model" / "interfaces").rglob("*.yaml")
        }
        assert "IVehicleSpeed" not in names

    @pytest.mark.parametrize("text", _READABLE_IS_SERVICE)
    def test_a_readable_text_drops_nothing(self, exchange_source, tmp_path, text) -> None:
        assert _added_entries(exchange_source, tmp_path, self._set(IVEHICLE_SPEED, text)) == []


IPOSE = "/PortInterfaces/IPose"
SENSOR = "/ComponentTypes/SensorSwc"
BASE_TYPE = "/DataTypes/Base/uint8_base"


def _set_short_name(root: etree._Element, path: str, name: str) -> None:
    _element(root, path).find(f"{NS}SHORT-NAME").text = name


def _remove(root: etree._Element, path: str) -> None:
    node = _element(root, path)
    node.getparent().remove(node)


def _retype(node: etree._Element, path: str, dest: str) -> None:
    ref = node.find(f"{NS}TYPE-TREF")
    ref.text = path
    ref.set("DEST", dest)


def _second_pose_element(root: etree._Element, name: str, base_typed: bool) -> None:
    element = _element(root, f"{IPOSE}/pose")
    twin = etree.fromstring(etree.tostring(element))
    twin.find(f"{NS}SHORT-NAME").text = name
    if base_typed:
        _retype(twin, BASE_TYPE, "SW-BASE-TYPE")
    element.addnext(twin)


def _blank_interface_name(root: etree._Element) -> None:
    # The port is removed with it: its reference names the old path, which would become an
    # external interface stub, a construct of its own.
    _set_short_name(root, IPOSE, " ")
    _remove(root, f"{SENSOR}/pPose")


def _name_held_by_the_other_kind(root: etree._Element) -> None:
    """``IPose`` moved into a package of its own and renamed ``ICalibration``.

    The sender-receiver loop maps it first, so the client-server ``ICalibration`` finds its bare name held."""
    interface = _element(root, IPOSE)
    package = interface.getparent().getparent()
    twin = etree.fromstring(etree.tostring(package))
    twin.find(f"{NS}SHORT-NAME").text = "PortInterfacesTwin"
    elements = twin.find(f"{NS}ELEMENTS")
    for child in list(elements):
        if child.findtext(f"{NS}SHORT-NAME") != "IPose":
            elements.remove(child)
    elements[0].find(f"{NS}SHORT-NAME").text = "ICalibration"
    package.addnext(twin)
    interface.getparent().remove(interface)
    _remove(root, f"{SENSOR}/pPose")


def _no_data_elements(root: etree._Element) -> None:
    interface = _element(root, IPOSE)
    interface.remove(interface.find(f"{NS}DATA-ELEMENTS"))


_CS_MEMBERS = [
    (SET_OFFSET, "CLIENT-SERVER-OPERATION"),
    (f"{SET_OFFSET}/samples", "ARGUMENT-DATA-PROTOTYPE"),
    (f"{SET_OFFSET}/raw", "ARGUMENT-DATA-PROTOTYPE"),
]


class TestEveryInterfaceDropChainsItsMembers:
    """Every interface ``map_interfaces`` drops chains each member it declares to the interface's line.

    Not only for an unreadable ``IS-SERVICE``: each member (a data element, an operation, an
    argument) gets a line at its own key, through the one site ``type_mapping.drop_interface``.
    Without the chained lines the input/output oracle reports each member as an unallowed
    difference; that oracle case is the closed half's.

    The drop paths, every one of ``map_interfaces``' (both kinds share each): a blank interface
    ``SHORT-NAME``; a bare name already held; an unreadable ``IS-SERVICE``
    (:class:`TestAnUnreadableIsServiceDropsItsInterface`); a member that abandons the interface (a
    blank member name, a member type no data prototype may have, an unsupported argument
    ``DIRECTION``); no member left (no data elements; no operations, each operation refused on its
    own -- whose own line is not repeated). What this does not reach: the three merge verdicts that
    leave a member unmapped without a line in ``map_interfaces`` (a conflicting group's member, a merged
    group's alias, a member ``--packages`` excluded), each covered by its group's record.
    """

    CASES = [
        pytest.param(
            lambda root: _second_pose_element(root, "pose2", base_typed=True),
            IPOSE,
            "SENDER-RECEIVER-INTERFACE",
            [(f"{IPOSE}/pose", "VARIABLE-DATA-PROTOTYPE"), (f"{IPOSE}/pose2", "VARIABLE-DATA-PROTOTYPE")],
            id="sr-a-member-type-abandons-it",
        ),
        pytest.param(
            lambda root: _second_pose_element(root, " ", base_typed=False),
            IPOSE,
            "SENDER-RECEIVER-INTERFACE",
            [(f"{IPOSE}/pose", "VARIABLE-DATA-PROTOTYPE"), (f"{IPOSE}/ ", "VARIABLE-DATA-PROTOTYPE")],
            id="sr-a-blank-member-name-abandons-it",
        ),
        pytest.param(_no_data_elements, IPOSE, "SENDER-RECEIVER-INTERFACE", [], id="sr-no-data-elements"),
        pytest.param(
            _blank_interface_name,
            "/PortInterfaces/ ",
            "SENDER-RECEIVER-INTERFACE",
            [("/PortInterfaces/ /pose", "VARIABLE-DATA-PROTOTYPE")],
            id="sr-blank-interface-name",
        ),
        pytest.param(
            lambda root: _element(root, f"{SET_OFFSET}/raw").find(f"{NS}DIRECTION").__setattr__("text", "FOO"),
            ICAL,
            "CLIENT-SERVER-INTERFACE",
            _CS_MEMBERS,
            id="cs-an-unsupported-direction-abandons-it",
        ),
        pytest.param(
            lambda root: _retype(_element(root, f"{SET_OFFSET}/raw"), BASE_TYPE, "SW-BASE-TYPE"),
            ICAL,
            "CLIENT-SERVER-INTERFACE",
            _CS_MEMBERS,
            id="cs-an-argument-type-abandons-it",
        ),
        pytest.param(
            lambda root: _set_short_name(root, f"{SET_OFFSET}/raw", " "),
            ICAL,
            "CLIENT-SERVER-INTERFACE",
            [(f"{SET_OFFSET}/samples", "ARGUMENT-DATA-PROTOTYPE"), (f"{SET_OFFSET}/ ", "ARGUMENT-DATA-PROTOTYPE")],
            id="cs-no-operations-after-a-blank-argument-name",
        ),
        pytest.param(
            lambda root: _set_short_name(root, SET_OFFSET, " "),
            ICAL,
            "CLIENT-SERVER-INTERFACE",
            [
                (f"{ICAL}/ ", "CLIENT-SERVER-OPERATION"),
                (f"{ICAL}/ /samples", "ARGUMENT-DATA-PROTOTYPE"),
                (f"{ICAL}/ /raw", "ARGUMENT-DATA-PROTOTYPE"),
            ],
            id="cs-no-operations-after-a-blank-operation-name",
        ),
        pytest.param(
            _name_held_by_the_other_kind, ICAL, "CLIENT-SERVER-INTERFACE", _CS_MEMBERS, id="cs-name-already-held"
        ),
    ]

    @staticmethod
    def imported(source: Path, tmp_path: Path, mutate):
        """``(report, patched source, project, generated output)`` for one mutated copy of *source*.

        The closed half of this module judges the same run with the input-vs-output oracle.
        """
        tree = etree.parse(str(source))
        mutate(tree.getroot())
        tmp_path.mkdir(parents=True, exist_ok=True)
        patched = _save(tree, tmp_path / "source.arxml")
        project = init_project(tmp_path / "project")
        report = run_import(str(patched), str(project / "swcraft.yaml"), force=True)
        output = tmp_path / "output.arxml"
        generate_arxml(str(project / "model"), str(output))
        return report, patched, project, output

    @pytest.mark.parametrize(("mutate", "path", "tag", "members"), CASES)
    def test_each_member_is_chained_to_the_interface_line(
        self, exchange_source, tmp_path, mutate, path, tag, members
    ) -> None:
        report, _patched, _project, _output = self.imported(exchange_source, tmp_path, mutate)
        entries = list(report.skip_entries)
        (root,) = [e for e in entries if (e.path, e.tag) == (path, tag)]
        assert root.effect == BEHAVIOURAL
        name = path.rsplit("/", 1)[1]
        chained = sorted(
            (e.path, e.tag)
            for e in entries
            if e.reason == f"chain-skipped: interface '{name}' was skipped ({root.reason})"
        )
        assert chained == sorted(members)
        for entry in entries:
            if (entry.path, entry.tag) in members and entry.reason.startswith("chain-skipped: interface"):
                assert (entry.category, entry.rule, entry.effect) == (root.category, root.rule, root.effect)


class TestTheInterfaceFamilies:
    """F6, F7 and F8: an argument's implementation policy and the application errors."""

    def test_f6_a_policy_meaning_other_than_the_writers_output(self, exchange_source, tmp_path) -> None:
        def mutate(root):
            _element(root, f"{SET_OFFSET}/raw").find(f"{NS}SERVER-ARGUMENT-IMPL-POLICY").text = "USE-VOID"

        assert _added(exchange_source, tmp_path, mutate) == [
            (f"{SET_OFFSET}/raw", "SERVER-ARGUMENT-IMPL-POLICY", BEHAVIOURAL, NOT_SUPPORTED)
        ]

    def test_f6_use_array_base_type_on_an_array_argument_and_absent_on_any_are_carried(
        self, exchange_source, tmp_path
    ) -> None:
        """The equivalence, both halves.

        The writer omits the policy on the array argument, so the source states it here, and removes it from the
        non-array one."""

        def mutate(root):
            samples = _element(root, f"{SET_OFFSET}/samples")
            assert samples.find(f"{NS}SERVER-ARGUMENT-IMPL-POLICY") is None
            policy = etree.Element(f"{NS}SERVER-ARGUMENT-IMPL-POLICY")
            policy.text = "USE-ARRAY-BASE-TYPE"
            samples.find(f"{NS}DIRECTION").addnext(policy)
            raw = _element(root, f"{SET_OFFSET}/raw")
            raw.remove(raw.find(f"{NS}SERVER-ARGUMENT-IMPL-POLICY"))

        after = [e for e in _import(exchange_source, tmp_path, mutate) if e.tag == "SERVER-ARGUMENT-IMPL-POLICY"]
        assert after == []

    def test_f6_any_policy_on_an_argument_typed_outside_the_file(self, exchange_source, tmp_path) -> None:
        """Any policy on an argument typed outside the file is not carried.

        The writer states no policy on an argument typed by an external type stub, so a source that states one -- even
        ``USE-ARGUMENT-TYPE`` -- is not carried, and an absent one is."""

        def stated(root):
            _element(root, f"{SET_OFFSET}/raw").find(f"{NS}TYPE-TREF").text = "/Foreign/Raw_T"

        def absent(root):
            stated(root)
            raw = _element(root, f"{SET_OFFSET}/raw")
            raw.remove(raw.find(f"{NS}SERVER-ARGUMENT-IMPL-POLICY"))

        lines = [
            e for e in _import(exchange_source, tmp_path / "stated", stated) if e.tag == "SERVER-ARGUMENT-IMPL-POLICY"
        ]
        assert [(e.path, e.effect) for e in lines] == [(f"{SET_OFFSET}/raw", BEHAVIOURAL)]
        assert "declared outside the file" in lines[0].reason
        after = [
            e for e in _import(exchange_source, tmp_path / "absent", absent) if e.tag == "SERVER-ARGUMENT-IMPL-POLICY"
        ]
        assert after == []

    def test_f7_and_f8_an_application_error_and_the_references_to_it(self, exchange_source, tmp_path) -> None:
        def mutate(root):
            interface = _element(root, ICAL)
            add_xml(
                interface,
                "<POSSIBLE-ERRORS><APPLICATION-ERROR><SHORT-NAME>E_RANGE</SHORT-NAME>"
                "<ERROR-CODE>1</ERROR-CODE></APPLICATION-ERROR></POSSIBLE-ERRORS>",
            )
            add_xml(
                _element(root, SET_OFFSET),
                f'<POSSIBLE-ERROR-REFS><POSSIBLE-ERROR-REF DEST="APPLICATION-ERROR">{ICAL}/E_RANGE'
                "</POSSIBLE-ERROR-REF></POSSIBLE-ERROR-REFS>",
            )

        assert _added(exchange_source, tmp_path, mutate) == [
            (f"{ICAL}/E_RANGE", "APPLICATION-ERROR", BEHAVIOURAL, NOT_SUPPORTED),
            (SET_OFFSET, "POSSIBLE-ERROR-REFS", BEHAVIOURAL, NOT_SUPPORTED),
        ]


class TestTheCompuAndConstraintFamilies:
    """F9, F10, F23 (a ``LINEAR`` scale's limits) and the enumeration's scales."""

    COMPU = "/DataTypes/Application/Velocity_T_Compu"

    def _scale(self, root):
        return _element(root, self.COMPU).find(f"{NS}COMPU-INTERNAL-TO-PHYS/{NS}COMPU-SCALES/{NS}COMPU-SCALE")

    @pytest.mark.parametrize(("level", "carried"), [("0", True), ("x", False)], ids=["integer", "no-integer"])
    def test_f9_a_constraint_level(self, exchange_source, tmp_path, level: str, carried: bool) -> None:
        """A level is the ``data_constr`` element's, so F9 gives no line for it.

        A level makes ``Velocity_T_Range`` no private form (the inline ``range`` cannot state
        it), so it is an element; a level no integer states is not carried, and keeps its line.
        """

        def mutate(root):
            rule = _element(root, "/DataTypes/Application/Velocity_T_Range").find(
                f"{NS}DATA-CONSTR-RULES/{NS}DATA-CONSTR-RULE"
            )
            rule.insert(0, snippet(f"<CONSTR-LEVEL>{level}</CONSTR-LEVEL>")[0])

        expected = [
            ("/DataTypes/Application/Velocity_T_Range/DATA-CONSTR-RULES", "CONSTR-LEVEL", DESCRIPTIVE, NOT_SUPPORTED)
        ]
        assert _added(exchange_source, tmp_path, mutate) == ([] if carried else expected)

    def test_f10_a_linear_scales_short_label(self, exchange_source, tmp_path) -> None:
        """The line cites ``[constr_1375]`` and is descriptive at F10's key.

        ``CompuScale.shortLabel`` is ``N/A`` for ``LINEAR``."""

        def mutate(root):
            self._scale(root).insert(0, snippet("<SHORT-LABEL>Speed</SHORT-LABEL>")[0])

        assert _added(exchange_source, tmp_path, mutate) == [
            (f"{self.COMPU}/COMPU-INTERNAL-TO-PHYS", "SHORT-LABEL", DESCRIPTIVE, SkipCategory.EXPORT_DEFECT)
        ]

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    @pytest.mark.parametrize(
        ("lower", "upper", "lines", "carried"),
        [
            # closed, both sides: carried, no line
            (
                '<LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT>',
                '<UPPER-LIMIT INTERVAL-TYPE="CLOSED">4000</UPPER-LIMIT>',
                [],
                {"lower": {"value": 0, "interval": "closed"}, "upper": {"value": 4000, "interval": "closed"}},
            ),
            # an absent interval type is CLOSED, and a hexadecimal limit is its value
            (
                "<LOWER-LIMIT>0</LOWER-LIMIT>",
                "<UPPER-LIMIT>0xFA0</UPPER-LIMIT>",
                [],
                {"lower": {"value": 0, "interval": "closed"}, "upper": {"value": 4000, "interval": "closed"}},
            ),
            # an OPEN limit is carried open
            (
                '<LOWER-LIMIT INTERVAL-TYPE="OPEN">1</LOWER-LIMIT>',
                "<UPPER-LIMIT>4000</UPPER-LIMIT>",
                [],
                {"lower": {"value": 1, "interval": "open"}, "upper": {"value": 4000, "interval": "closed"}},
            ),
            # one side only
            (None, "<UPPER-LIMIT>4000</UPPER-LIMIT>", [], {"upper": {"value": 4000, "interval": "closed"}}),
            # a large integer is kept exact
            (
                None,
                "<UPPER-LIMIT>18446744073709551615</UPPER-LIMIT>",
                [],
                {"upper": {"value": 18446744073709551615, "interval": "closed"}},
            ),
            # equal and both closed: a point, coherent
            (
                "<LOWER-LIMIT>5</LOWER-LIMIT>",
                "<UPPER-LIMIT>5</UPPER-LIMIT>",
                [],
                {"lower": {"value": 5, "interval": "closed"}, "upper": {"value": 5, "interval": "closed"}},
            ),
            # infinite on its own side: an absent bound, with a descriptive line each
            (
                '<LOWER-LIMIT INTERVAL-TYPE="INFINITE">0</LOWER-LIMIT>',
                "<UPPER-LIMIT>INF</UPPER-LIMIT>",
                [("LOWER-LIMIT", DESCRIPTIVE), ("UPPER-LIMIT", DESCRIPTIVE)],
                None,
            ),
            # the other side's infinity: unreadable, and the pair is not carried
            (
                "<LOWER-LIMIT>INF</LOWER-LIMIT>",
                "<UPPER-LIMIT>4000</UPPER-LIMIT>",
                [("LOWER-LIMIT", BEHAVIOURAL), ("UPPER-LIMIT", BEHAVIOURAL)],
                None,
            ),
            # no finite number
            (
                "<LOWER-LIMIT>NaN</LOWER-LIMIT>",
                "<UPPER-LIMIT>4000</UPPER-LIMIT>",
                [("LOWER-LIMIT", BEHAVIOURAL), ("UPPER-LIMIT", BEHAVIOURAL)],
                None,
            ),
            # an inexact decimal: no model value holds it exactly
            (
                "<LOWER-LIMIT>0.1000000000000000000001</LOWER-LIMIT>",
                "<UPPER-LIMIT>4000</UPPER-LIMIT>",
                [("LOWER-LIMIT", BEHAVIOURAL), ("UPPER-LIMIT", BEHAVIOURAL)],
                None,
            ),
            # reversed
            (
                "<LOWER-LIMIT>4000</LOWER-LIMIT>",
                "<UPPER-LIMIT>0</UPPER-LIMIT>",
                [("LOWER-LIMIT", BEHAVIOURAL), ("UPPER-LIMIT", BEHAVIOURAL)],
                None,
            ),
            # equal with one side open: an empty interval
            (
                '<LOWER-LIMIT INTERVAL-TYPE="OPEN">5</LOWER-LIMIT>',
                "<UPPER-LIMIT>5</UPPER-LIMIT>",
                [("LOWER-LIMIT", BEHAVIOURAL), ("UPPER-LIMIT", BEHAVIOURAL)],
                None,
            ),
        ],
        ids=[
            "closed",
            "closed-by-default-and-hex",
            "open",
            "one-sided",
            "large-integer",
            "a-point",
            "own-side-infinite",
            "other-side-infinite",
            "nan",
            "inexact",
            "reversed",
            "equal-and-open",
        ],
    )
    def test_f23_a_linear_scales_limits_carried_or_reported(
        self, tmp_path, version, lower, upper, lines, carried
    ) -> None:
        """Family F23: a ``LINEAR`` scale's limits.

        ``[TPS_SWCT_01548]``: a scale's limits are the conversion's scope, so the model carries
        them -- on ``Velocity_T``'s inline conversion here, its private ``_Compu`` read back
        inline while the limits are carried whole -- and the report names what is not: an own
        side's infinity is an absent bound (descriptive), an unreadable or incoherent pair is not
        carried (behavioural, one line per limit).
        """
        source = _whole_model(EXCHANGE_MODEL_ROOT, tmp_path / "model.arxml", version)

        def mutate(root):
            scale = self._scale(root)
            for xml in (upper, lower):
                if xml is not None:
                    scale.insert(0, snippet(xml)[0])

        added = _added(source, tmp_path / "added", mutate)
        assert [(tag, effect) for path, tag, effect, _category in added if path.startswith(self.COMPU)] == lines

        project = init_project(tmp_path / "project")
        tree = etree.parse(str(source))
        mutate(tree.getroot())
        run_import(str(_save(tree, tmp_path / "mutated.arxml")), str(project / "swcraft.yaml"))
        held = yaml.safe_load(
            (model_element_path(project / "model", "Velocity_T", kind="application_type")).read_text(encoding="utf-8")
        )
        if lines and any(effect == BEHAVIOURAL for _tag, effect in lines):
            # Not carried: the form is no private form any more, so the type names its own
            # `linear` element -- and that element holds no limits either.
            assert "conversion" not in held
            linear = yaml.safe_load(
                model_element_path(project / "model", held["compu_method_ref"], kind="linear").read_text(
                    encoding="utf-8"
                )
            )
            assert (linear["kind"], "limits" in linear) == ("linear", False)
            return
        assert held["conversion"].get("limits") == carried

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    @pytest.mark.parametrize("denominator", [None, "1/1", "1.0", "1"], ids=["absent", "ratio", "decimal", "integer"])
    def test_f23_every_denominator_of_one_carries_the_same_limits(self, tmp_path, version, denominator) -> None:
        """The limits are carried whatever spelling of the denominator one the conversion was read on."""
        source = _whole_model(EXCHANGE_MODEL_ROOT, tmp_path / "model.arxml", version)

        def mutate(root):
            scale = self._scale(root)
            stated = scale.find(f"{NS}COMPU-RATIONAL-COEFFS/{NS}COMPU-DENOMINATOR")
            if denominator is None:
                stated.getparent().remove(stated)
            else:
                stated.find(f"{NS}V").text = denominator
            for tag, text in (("UPPER-LIMIT", "4000"), ("LOWER-LIMIT", "0")):
                scale.insert(0, snippet(f"<{tag}>{text}</{tag}>")[0])

        assert _added(source, tmp_path / "carried", mutate) == []

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    @pytest.mark.parametrize(
        ("lower", "upper"),
        [("-inf", "inf"), ("-Inf", "+inf"), (" -INF ", " INF ")],
        ids=["lower-case", "mixed-case", "padded"],
    )
    def test_f23_an_infinite_spelling_is_one_to_the_importer_and_to_the_reader(
        self, tmp_path, version, lower, upper
    ) -> None:
        """A spelling the reader carries as no bound is an own-side infinity to F23 too: descriptive lines."""
        source = _whole_model(EXCHANGE_MODEL_ROOT, tmp_path / "model.arxml", version)

        def mutate(root):
            scale = self._scale(root)
            for tag, text in (("UPPER-LIMIT", upper), ("LOWER-LIMIT", lower)):
                scale.insert(0, snippet(f"<{tag}>{text}</{tag}>")[0])

        assert _added(source, tmp_path, mutate) == [
            (f"{self.COMPU}/COMPU-INTERNAL-TO-PHYS", tag, DESCRIPTIVE, NOT_SUPPORTED)
            for tag in ("LOWER-LIMIT", "UPPER-LIMIT")
        ]

    def test_the_enumerations_scales_are_carried_label_and_limits_written_back(self, exchange_source, tmp_path) -> None:
        """The ``TEXTTABLE`` scale's ``SHORT-LABEL`` and ``UPPER-LIMIT`` are CARRIED.

        The source's literal name and value survive the import, and the writer writes the same
        ``SHORT-LABEL`` and limits back: SWCraft's own export, imported and exported again, holds
        the same scales -- label, both limits and their interval types, the literal -- as the
        source. No allowed difference of the oracle is involved.
        """
        entries = _import(exchange_source, tmp_path / "import")
        assert [e for e in entries if e.path.startswith("/DataTypes/Application/Gear_T_CompuMethod")] == []

        project = init_project(tmp_path / "reexport")
        run_import(str(exchange_source), str(project / "swcraft.yaml"))
        output = _whole_model(project / "model", tmp_path / "reexported.arxml")

        def scales(path: Path) -> list[tuple[str | None, ...]]:
            root = etree.parse(str(path)).getroot()
            method = next(
                m for m in root.iter(f"{NS}COMPU-METHOD") if m.findtext(f"{NS}SHORT-NAME") == "Gear_T_CompuMethod"
            )
            return [
                (
                    scale.findtext(f"{NS}SHORT-LABEL"),
                    scale.findtext(f"{NS}LOWER-LIMIT"),
                    scale.find(f"{NS}LOWER-LIMIT").get("INTERVAL-TYPE"),
                    scale.findtext(f"{NS}UPPER-LIMIT"),
                    scale.find(f"{NS}UPPER-LIMIT").get("INTERVAL-TYPE"),
                    scale.findtext(f"{NS}COMPU-CONST/{NS}VT"),
                )
                for scale in method.iter(f"{NS}COMPU-SCALE")
            ]

        assert scales(output) == scales(exchange_source)
        assert len(scales(output)) == 2

    def test_an_enumerations_label_or_upper_limit_saying_otherwise_is_f10_and_f11(
        self, exchange_source, tmp_path
    ) -> None:
        def mutate(root):
            scale = next(_element(root, "/DataTypes/Application/Gear_T_CompuMethod").iter(f"{NS}COMPU-SCALE"))
            scale.find(f"{NS}SHORT-LABEL").text = "Parked"
            scale.find(f"{NS}UPPER-LIMIT").text = "3"

        assert _added(exchange_source, tmp_path, mutate) == [
            (
                "/DataTypes/Application/Gear_T_CompuMethod/COMPU-INTERNAL-TO-PHYS",
                "SHORT-LABEL",
                DESCRIPTIVE,
                NOT_SUPPORTED,
            ),
            (
                "/DataTypes/Application/Gear_T_CompuMethod/COMPU-INTERNAL-TO-PHYS",
                "UPPER-LIMIT",
                BEHAVIOURAL,
                NOT_SUPPORTED,
            ),
        ]


class TestTheValueSpecificationLabels:
    """F12: a value specification's ``SHORT-LABEL`` the writer does not write back."""

    def test_the_writers_own_record_labels_are_carried_by_equivalence(self, tmp_path) -> None:
        source = _whole_model(INIT_VALUES_MODEL_ROOT, tmp_path / "init.arxml")
        assert [e for e in _import(source, tmp_path) if e.path.endswith("/VALUE-SPEC")] == []

    def test_a_label_the_writer_does_not_write_is_one_line_per_label_once_per_constant(self, tmp_path) -> None:
        source = _whole_model(INIT_VALUES_MODEL_ROOT, tmp_path / "init.arxml")

        def mutate(root):
            constant = next(root.iter(f"{NS}CONSTANT-SPECIFICATION"))
            value = constant.find(f"{NS}VALUE-SPEC")[0]
            label = value.find(f"{NS}SHORT-LABEL")
            if label is None:
                value.insert(0, snippet("<SHORT-LABEL>Relabelled</SHORT-LABEL>")[0])
            else:
                label.text = "Relabelled"

        added = _added(source, tmp_path, mutate)
        assert len(added) == 1
        ((path, tag, effect, category),) = added
        assert path.endswith("/VALUE-SPEC") and (tag, effect, category) == ("SHORT-LABEL", DESCRIPTIVE, NOT_SUPPORTED)


class TestTheDocumentationFamilies:
    """F13, F14 and F15."""

    def test_f13_an_elements_documentation_and_uuid(self, exchange_source, tmp_path) -> None:
        def mutate(root):
            interface = _element(root, "/PortInterfaces/IVehicleSpeed")
            interface.set("UUID", "1234")
            _insert_after_short_name(interface, '<INTRODUCTION><P><L-1 L="EN">Why.</L-1></P></INTRODUCTION>')
            _insert_after_short_name(_element(root, ACTUATOR), '<LONG-NAME><L-4 L="FOR-ALL"/></LONG-NAME>')

        assert _added(exchange_source, tmp_path, mutate) == [
            ("/PortInterfaces/IVehicleSpeed", "INTRODUCTION", DESCRIPTIVE, NOT_SUPPORTED),
            ("/PortInterfaces/IVehicleSpeed", "UUID", DESCRIPTIVE, NOT_SUPPORTED),
        ]

    def test_f14_a_desc_not_carried_exactly_is_one_line_however_often_it_is_read(
        self, exchange_source, tmp_path
    ) -> None:
        def mutate(root):
            _insert_after_short_name(
                _element(root, "/PortInterfaces/IVehicleSpeed/speed"),
                '<DESC><L-2 L="FOR-ALL">Speed.</L-2><L-2 L="DE">Geschwindigkeit.</L-2></DESC>',
            )
            _insert_after_short_name(
                _element(root, "/PortInterfaces/IActuatorStatus/status"), '<DESC><L-2 L="FOR-ALL">A<BR/>B</L-2></DESC>'
            )

        assert _added(exchange_source, tmp_path, mutate) == [
            ("/PortInterfaces/IActuatorStatus/status", "DESC", DESCRIPTIVE, NOT_SUPPORTED),
            ("/PortInterfaces/IVehicleSpeed/speed", "DESC", DESCRIPTIVE, NOT_SUPPORTED),
        ]

    def test_f14_a_literals_desc_and_a_linear_scales_desc(self, exchange_source, tmp_path) -> None:
        def mutate(root):
            literal = next(_element(root, "/DataTypes/Application/Gear_T_CompuMethod").iter(f"{NS}COMPU-SCALE"))
            literal.insert(0, snippet('<DESC><L-2 L="FOR-ALL">Park.</L-2><L-2 L="DE">Parken.</L-2></DESC>')[0])
            linear = next(_element(root, "/DataTypes/Application/Velocity_T_Compu").iter(f"{NS}COMPU-SCALE"))
            linear.insert(0, snippet('<DESC><L-2 L="FOR-ALL">Scaled.</L-2></DESC>')[0])

        assert _added(exchange_source, tmp_path, mutate) == [
            ("/DataTypes/Application/Gear_T_CompuMethod/COMPU-INTERNAL-TO-PHYS", "DESC", DESCRIPTIVE, NOT_SUPPORTED),
            ("/DataTypes/Application/Velocity_T_Compu/COMPU-INTERNAL-TO-PHYS", "DESC", DESCRIPTIVE, NOT_SUPPORTED),
        ]

    def test_f14_a_compositions_long_name_and_f13_leaves_it_alone(self, exchange_source, tmp_path) -> None:
        def mutate(root):
            _insert_after_short_name(
                _element(root, "/ComponentTypes/Root"),
                '<LONG-NAME><L-4 L="FOR-ALL">Root</L-4><L-4 L="DE">Wurzel</L-4></LONG-NAME>',
            )

        assert _added(exchange_source, tmp_path, mutate) == [
            ("/ComponentTypes/Root", "LONG-NAME", DESCRIPTIVE, NOT_SUPPORTED)
        ]

    def test_f15_a_packages_own_content(self, exchange_source, tmp_path) -> None:
        def mutate(root):
            package = _element(root, "/PortInterfaces")
            package.set("UUID", "abcd")
            _insert_after_short_name(package, '<DESC><L-2 L="FOR-ALL">Interfaces.</L-2></DESC><CATEGORY>X</CATEGORY>')

        assert _added(exchange_source, tmp_path, mutate) == [
            ("/PortInterfaces", "CATEGORY", DESCRIPTIVE, NOT_SUPPORTED),
            ("/PortInterfaces", "DESC", DESCRIPTIVE, NOT_SUPPORTED),
            ("/PortInterfaces", "UUID", DESCRIPTIVE, NOT_SUPPORTED),
        ]

    def test_an_empty_desc_is_no_desc(self, exchange_source, tmp_path) -> None:
        def mutate(root):
            _insert_after_short_name(_element(root, "/PortInterfaces"), '<DESC><L-2 L="FOR-ALL"/></DESC>')
            _insert_after_short_name(
                _element(root, "/PortInterfaces/IVehicleSpeed/speed"), '<DESC><L-2 L="FOR-ALL"/></DESC>'
            )

        assert _added(exchange_source, tmp_path, mutate) == []


class TestEveryDescriptionReadIsJudgedOnce:
    """F14 is recorded at one site for every ``DESC`` the reader reads, and once per element.

    The callers of ``_extract_description``: the index judges every indexed element,
    and a kind's own reader reads the same ``DESC`` again, so one element's variant is one line
    however often it is read. The ``COMPU-SCALE``'s, which the index never sees, has a test of
    its own (:class:`TestTheDocumentationFamilies`).
    """

    @pytest.mark.parametrize(
        "path",
        [
            "/DataTypes/Application/Pose_T/x",
            "/DataTypes/Application/Samples_T/Element",
            f"{SET_OFFSET}/raw",
            SET_OFFSET,
            f"{ACTUATOR}/rSpeed",
            f"{BEHAVIOUR}/ActuatorMain",
            f"{BEHAVIOUR}/TMT_ActuatorMain",
            "/ComponentTypes/Root/ActuatorSwc",
            "/ComponentTypes/Root/speedLink",
            "/DataTypes/Application/Velocity_T",
            "/DataTypes/MappingSets/PhysicalTypes",
        ],
        ids=[
            "record-element",
            "array-element",
            "argument",
            "operation",
            "port",
            "runnable",
            "event",
            "component-prototype",
            "assembly-connector",
            "application-type",
            "mapping-set",
        ],
    )
    def test_a_second_l2_is_one_descriptive_line_at_the_element(self, exchange_source, tmp_path, path) -> None:
        def mutate(root):
            _insert_after_short_name(
                _element(root, path), '<DESC><L-2 L="FOR-ALL">One.</L-2><L-2 L="DE">Eins.</L-2></DESC>'
            )

        added = [e for e in _added_entries(exchange_source, tmp_path, mutate) if e.effect == DESCRIPTIVE]
        assert [(e.path, e.tag) for e in added] == [(path, "DESC")]
        assert "a second L-2" in added[0].reason


_LANGUAGE_OWNERS = {
    "record-element": "/DataTypes/Application/Pose_T/x",
    "array-element": "/DataTypes/Application/Samples_T/Element",
    "argument": f"{SET_OFFSET}/raw",
    "operation": SET_OFFSET,
    "data-element": "/PortInterfaces/IVehicleSpeed/speed",
    "port": f"{ACTUATOR}/rSpeed",
    "runnable": f"{BEHAVIOUR}/ActuatorMain",
    "event": f"{BEHAVIOUR}/TMT_ActuatorMain",
    "component-prototype": "/ComponentTypes/Root/ActuatorSwc",
    "assembly-connector": "/ComponentTypes/Root/speedLink",
    "application-type": "/DataTypes/Application/Velocity_T",
    "mapping-set": "/DataTypes/MappingSets/PhysicalTypes",
}


@pytest.fixture(scope="module", params=list(ArxmlVersion), ids=lambda version: version.value)
def versioned_exchange_source(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The ``component_exchange`` tree's whole-model export, once per supported version."""
    return _whole_model(EXCHANGE_MODEL_ROOT, tmp_path_factory.mktemp("exchange") / "model.arxml", request.param)


def _set_long_name(root: etree._Element, path: str, xml: str) -> None:
    """Replace the ``LONG-NAME`` of the element at *path* (the fixture's composition has a title) by *xml*."""
    element = _element(root, path)
    for existing in element.findall(f"{NS}LONG-NAME"):
        element.remove(existing)
    _insert_after_short_name(element, xml)


class TestALanguageTheWriterRestatesIsALine:
    """F14: a paragraph given in one language comes back as ``FOR-ALL``, so it is one descriptive line.

    The model's text names no language and the writer states the language-neutral ``FOR-ALL``. A
    single ``L-2`` / ``L-4`` with another ``L`` is therefore a value the round trip changes,
    reported at the same site and key as every other form the reader does not carry exactly. What
    it does not reach: a ``DESC`` with no text, which is no ``DESC`` whatever its ``L`` and is
    written nowhere.
    """

    @pytest.mark.parametrize("path", list(_LANGUAGE_OWNERS.values()), ids=list(_LANGUAGE_OWNERS))
    def test_a_desc_in_one_language_is_one_line_at_the_element(self, versioned_exchange_source, tmp_path, path) -> None:
        def mutate(root):
            _insert_after_short_name(_element(root, path), '<DESC><L-2 L="EN">One.</L-2></DESC>')

        added = [e for e in _added_entries(versioned_exchange_source, tmp_path, mutate) if e.effect == DESCRIPTIVE]
        assert [(e.path, e.tag, e.category) for e in added] == [(path, "DESC", NOT_SUPPORTED)]
        assert added[0].reason == (
            "DESC not carried exactly: it holds a language other than FOR-ALL on L-2 -- "
            "SWCraft carries the first L-2's text alone"
        )

    def test_a_literals_desc_in_one_language(self, versioned_exchange_source, tmp_path) -> None:
        def mutate(root):
            literal = next(_element(root, "/DataTypes/Application/Gear_T_CompuMethod").iter(f"{NS}COMPU-SCALE"))
            literal.insert(0, snippet('<DESC><L-2 L="DE">Parken.</L-2></DESC>')[0])

        added = _added_entries(versioned_exchange_source, tmp_path, mutate)
        assert [(e.path, e.tag, e.effect) for e in added] == [
            ("/DataTypes/Application/Gear_T_CompuMethod/COMPU-INTERNAL-TO-PHYS", "DESC", DESCRIPTIVE)
        ]
        assert "a language other than FOR-ALL on L-2" in added[0].reason

    @pytest.mark.parametrize("text", ["Root", "Wurzel", "  ", ""], ids=["name", "other", "blank", "empty"])
    def test_a_compositions_long_name_in_one_language(self, versioned_exchange_source, tmp_path, text) -> None:
        """A composition's title is written back blank or not, so its ``L`` is restated either way."""

        def mutate(root):
            _set_long_name(root, "/ComponentTypes/Root", f'<LONG-NAME><L-4 L="EN">{text}</L-4></LONG-NAME>')

        added = _added_entries(versioned_exchange_source, tmp_path, mutate)
        assert [(e.path, e.tag, e.effect, e.category) for e in added] == [
            ("/ComponentTypes/Root", "LONG-NAME", DESCRIPTIVE, NOT_SUPPORTED)
        ]
        assert added[0].reason == (
            "LONG-NAME not carried exactly: it holds a language other than FOR-ALL on L-4 -- "
            "SWCraft carries the first L-4's text alone"
        )

    @pytest.mark.parametrize(
        "xml",
        [
            '<DESC><L-2 L="FOR-ALL">One.</L-2></DESC>',
            '<DESC><L-2 L="EN"/></DESC>',
            '<DESC><L-2 L="EN"></L-2></DESC>',
        ],
        ids=["for-all", "empty-in-one-language", "no-text-in-one-language"],
    )
    def test_the_neutral_language_and_an_empty_desc_are_no_line(self, versioned_exchange_source, tmp_path, xml) -> None:
        def mutate(root):
            _insert_after_short_name(_element(root, f"{ACTUATOR}/rSpeed"), xml)

        assert _added(versioned_exchange_source, tmp_path, mutate) == []

    def test_a_long_name_in_the_neutral_language_is_no_line(self, versioned_exchange_source, tmp_path) -> None:
        def mutate(root):
            _set_long_name(root, "/ComponentTypes/Root", '<LONG-NAME><L-4 L="FOR-ALL">Wurzel</L-4></LONG-NAME>')

        added = _added_entries(versioned_exchange_source, tmp_path, mutate)
        assert [e for e in added if e.tag == "LONG-NAME"] == []

    def test_a_second_paragraph_is_named_before_the_language(self, versioned_exchange_source, tmp_path) -> None:
        """One phrase per shape: the closed order names the second paragraph, never both."""

        def mutate(root):
            _insert_after_short_name(
                _element(root, f"{ACTUATOR}/rSpeed"), '<DESC><L-2 L="EN">One.</L-2><L-2 L="DE">Eins.</L-2></DESC>'
            )

        added = _added_entries(versioned_exchange_source, tmp_path, mutate)
        assert [e.reason.split(" -- ")[0] for e in added] == ["DESC not carried exactly: it holds a second L-2"]


#: A paragraph holding white space and nothing else: a text node, so a value under the exact rule.
WHITESPACE_PARAGRAPHS = [pytest.param(" ", id="space"), pytest.param("\n      ", id="line")]


class TestAWhitespaceOnlyParagraphIsText:
    """A description is read exactly, so a paragraph of white space is text and never *no description*.

    Empty is a paragraph with no text node (``<L-2 L="FOR-ALL"/>``). Where the model
    holds the text it is carried as held; where the owner has no place for it -- a
    package, an element's ``LONG-NAME``, a behaviour's own ``DESC`` -- it gets the
    line a worded one gets. What it does not reach: the white space **around** a
    paragraph, which is the file's layout and no value.
    """

    @pytest.mark.parametrize("text", WHITESPACE_PARAGRAPHS)
    def test_a_packages_desc_is_its_line(self, versioned_exchange_source, tmp_path, text) -> None:
        def mutate(root):
            _insert_after_short_name(_element(root, "/PortInterfaces"), f'<DESC><L-2 L="FOR-ALL">{text}</L-2></DESC>')

        assert _added(versioned_exchange_source, tmp_path, mutate) == [
            ("/PortInterfaces", "DESC", DESCRIPTIVE, NOT_SUPPORTED)
        ]

    @pytest.mark.parametrize("text", WHITESPACE_PARAGRAPHS)
    def test_an_elements_long_name_is_its_line(self, versioned_exchange_source, tmp_path, text) -> None:
        def mutate(root):
            _insert_after_short_name(
                _element(root, "/PortInterfaces/IVehicleSpeed"), f'<LONG-NAME><L-4 L="FOR-ALL">{text}</L-4></LONG-NAME>'
            )

        assert _added(versioned_exchange_source, tmp_path, mutate) == [
            ("/PortInterfaces/IVehicleSpeed", "LONG-NAME", DESCRIPTIVE, NOT_SUPPORTED)
        ]

    @pytest.mark.parametrize("text", WHITESPACE_PARAGRAPHS)
    def test_a_behaviours_desc_is_its_line(self, versioned_exchange_source, tmp_path, text) -> None:
        def mutate(root):
            _insert_after_short_name(_element(root, BEHAVIOUR), f'<DESC><L-2 L="FOR-ALL">{text}</L-2></DESC>')

        added = _added_entries(versioned_exchange_source, tmp_path, mutate)
        assert [(e.path, e.tag, e.effect, e.reason) for e in added] == [
            (
                BEHAVIOUR,
                "DESC",
                BEHAVIOURAL,
                "DESC of a SWC-INTERNAL-BEHAVIOR is documentation SWCraft does not carry -- not imported or preserved",
            )
        ]

    @pytest.mark.parametrize("path", ["/PortInterfaces", BEHAVIOUR, "/PortInterfaces/IVehicleSpeed"])
    def test_the_layout_around_an_empty_paragraph_is_no_text(self, versioned_exchange_source, tmp_path, path) -> None:
        def mutate(root):
            xml = (
                '<DESC>\n      <L-2 L="FOR-ALL"/>\n    </DESC>'
                '<LONG-NAME>\n      <L-4 L="FOR-ALL"></L-4>\n    </LONG-NAME>'
            )
            _insert_after_short_name(_element(root, path), xml)

        assert _added(versioned_exchange_source, tmp_path, mutate) == []

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    @pytest.mark.parametrize("text", WHITESPACE_PARAGRAPHS)
    def test_a_carried_description_is_carried_and_written_back_as_held(self, tmp_path, version, text) -> None:
        tree = etree.parse(str(_whole_model(EXCHANGE_MODEL_ROOT, tmp_path / "model.arxml", version)))
        interface = _element(tree.getroot(), "/PortInterfaces/IVehicleSpeed")
        for existing in interface.findall(f"{NS}DESC"):
            interface.remove(existing)
        _insert_after_short_name(interface, f'<DESC><L-2 L="FOR-ALL">{text}</L-2></DESC>')
        project = init_project(tmp_path / "project")
        report = run_import(str(_save(tree, tmp_path / "source.arxml")), str(project / "swcraft.yaml"), force=True)

        held = yaml.safe_load((project / "model" / "interfaces" / "IVehicleSpeed.yaml").read_text(encoding="utf-8"))
        assert held["description"] == text
        assert [e for e in report.skip_entries if e.path == "/PortInterfaces/IVehicleSpeed" and e.tag == "DESC"] == []
        out = _whole_model(project / "model", tmp_path / "out.arxml", version)
        written = _element(etree.parse(str(out)).getroot(), "/PortInterfaces/IVehicleSpeed")
        assert written.findtext(f"{NS}DESC/{NS}L-2") == text


class TestTheEarlierSitesRecordTheirKey:
    """The earlier report sites, each against the one ``(path, tag)`` key."""

    def test_an_application_types_unread_property(self, exchange_source, tmp_path) -> None:
        def mutate(root):
            add_xml(
                _conditional(_element(root, "/DataTypes/Application/Velocity_T")),
                "<DISPLAY-FORMAT>%3.1f</DISPLAY-FORMAT>",
            )

        assert _added(exchange_source, tmp_path, mutate) == [
            ("/DataTypes/Application/Velocity_T/SW-DATA-DEF-PROPS", "DISPLAY-FORMAT", BEHAVIOURAL, NOT_SUPPORTED)
        ]

    def test_a_compu_scale_not_read_keeps_its_index_in_the_reason(self, exchange_source, tmp_path) -> None:
        def mutate(root):
            scale = list(_element(root, "/DataTypes/Application/Gear_T_CompuMethod").iter(f"{NS}COMPU-SCALE"))[1]
            scale.find(f"{NS}COMPU-CONST").getparent().remove(scale.find(f"{NS}COMPU-CONST"))

        added = [e for e in _added_entries(exchange_source, tmp_path, mutate) if e.tag == "COMPU-SCALE"]
        assert [(e.path, e.tag) for e in added] == [
            ("/DataTypes/Application/Gear_T_CompuMethod/COMPU-INTERNAL-TO-PHYS", "COMPU-SCALE")
        ]
        assert "COMPU-SCALE [1]" in added[0].reason

    def test_a_data_type_map_the_reader_and_the_mapper_drop(self, exchange_source, tmp_path) -> None:
        def mutate(root):
            maps = _element(root, "/DataTypes/MappingSets/PhysicalTypes").find(f"{NS}DATA-TYPE-MAPS")
            first, second = list(maps)
            first.remove(first.find(f"{NS}IMPLEMENTATION-DATA-TYPE-REF"))
            second.find(f"{NS}APPLICATION-DATA-TYPE-REF").text = "/DataTypes/Implementation/Status_T"

        added = [e for e in _added_entries(exchange_source, tmp_path, mutate) if e.tag == "DATA-TYPE-MAP"]
        assert (
            sorted((e.path, e.tag) for e in added)
            == [("/DataTypes/MappingSets/PhysicalTypes/DATA-TYPE-MAPS", "DATA-TYPE-MAP")] * 2
        )
        assert any("DATA-TYPE-MAP [0]" in e.reason for e in added)

    def test_vendor_admin_data_is_keyed_at_its_element(self, exchange_source, tmp_path) -> None:
        def mutate(root):
            _insert_after_short_name(
                _element(root, "/PortInterfaces/IVehicleSpeed"),
                '<ADMIN-DATA><SDGS><SDG GID="VENDOR"><SD GID="k">v</SD></SDG></SDGS></ADMIN-DATA>',
            )

        assert _added(exchange_source, tmp_path, mutate) == [
            ("/PortInterfaces/IVehicleSpeed", "ADMIN-DATA", BEHAVIOURAL, NOT_SUPPORTED)
        ]

    def test_a_port_api_option_is_keyed_at_its_port_ref(self, exchange_source, tmp_path) -> None:
        """The writer writes no option, so one added to the source is one line.

        The line is the whole option's, keyed at its anchor, the ``PORT-REF``'s text, so it cannot
        be spent on another port's option.
        """

        def mutate(root):
            behaviour = _element(root, BEHAVIOUR)
            behaviour.find(f"{NS}RUNNABLES").addprevious(etree.Element(f"{NS}PORT-API-OPTIONS"))
            add_xml(
                behaviour.find(f"{NS}PORT-API-OPTIONS"),
                "<PORT-API-OPTION><ENABLE-TAKE-ADDRESS>false</ENABLE-TAKE-ADDRESS><INDIRECT-API>false</INDIRECT-API>"
                f'<PORT-REF DEST="R-PORT-PROTOTYPE">{ACTUATOR}/rSpeed</PORT-REF>'
                "<PORT-ARG-VALUES><PORT-DEFINED-ARGUMENT-VALUE/></PORT-ARG-VALUES></PORT-API-OPTION>",
            )

        assert _added(exchange_source, tmp_path, mutate) == [
            (f"{ACTUATOR}/rSpeed", "PORT-API-OPTION", BEHAVIOURAL, NOT_SUPPORTED)
        ]

    def test_an_init_value_not_carried_is_keyed_at_its_data_element(self, exchange_source, tmp_path) -> None:
        def mutate(root):
            add_xml(
                _element(root, "/PortInterfaces/IVehicleSpeed/speed"),
                '<INIT-VALUE><CONSTANT-REFERENCE><CONSTANT-REF DEST="CONSTANT-SPECIFICATION">/Nowhere/C'
                "</CONSTANT-REF></CONSTANT-REFERENCE></INIT-VALUE>",
            )

        assert _added(exchange_source, tmp_path, mutate) == [
            ("/PortInterfaces/IVehicleSpeed/speed", "INIT-VALUE", BEHAVIOURAL, NOT_SUPPORTED)
        ]

    def test_a_server_queue_length_other_than_one_has_its_structured_twin(self, exchange_source, tmp_path) -> None:
        tree = etree.parse(str(exchange_source))
        ports = _element(tree.getroot(), "/ComponentTypes/SensorSwc").find(f"{NS}PORTS")
        ports.insert(
            0,
            snippet(
                "<P-PORT-PROTOTYPE><SHORT-NAME>pCalibServer</SHORT-NAME><PROVIDED-COM-SPECS><SERVER-COM-SPEC>"
                f'<OPERATION-REF DEST="CLIENT-SERVER-OPERATION">{SET_OFFSET}</OPERATION-REF>'
                "<QUEUE-LENGTH>3</QUEUE-LENGTH></SERVER-COM-SPEC></PROVIDED-COM-SPECS>"
                f'<PROVIDED-INTERFACE-TREF DEST="CLIENT-SERVER-INTERFACE">{ICAL}</PROVIDED-INTERFACE-TREF>'
                "</P-PORT-PROTOTYPE>"
            )[0],
        )
        project = init_project(tmp_path / "project")
        report = run_import(str(_save(tree, tmp_path / "source.arxml")), str(project / "swcraft.yaml"), dry_run=True)

        assert report.queue_length_keys == [
            ("/ComponentTypes/SensorSwc/pCalibServer/PROVIDED-COM-SPECS", "QUEUE-LENGTH")
        ]
        assert len(report.queue_length_lines) == 1


class TestTheDisabledModes:
    """F16's own half; the chained half is ``test_behavioural_import.py``'s."""

    def test_a_disabled_mode_on_an_init_event_and_one_through_no_port_of_the_component(
        self, exchange_source, tmp_path
    ) -> None:
        iref = (
            "<DISABLED-MODE-IREFS><DISABLED-MODE-IREF>"
            '<CONTEXT-PORT-REF DEST="R-PORT-PROTOTYPE">{port}</CONTEXT-PORT-REF>'
            '<CONTEXT-MODE-DECLARATION-GROUP-PROTOTYPE-REF DEST="MODE-DECLARATION-GROUP-PROTOTYPE">/M/I/g'
            "</CONTEXT-MODE-DECLARATION-GROUP-PROTOTYPE-REF>"
            '<TARGET-MODE-DECLARATION-REF DEST="MODE-DECLARATION">/M/G/OFF</TARGET-MODE-DECLARATION-REF>'
            "</DISABLED-MODE-IREF></DISABLED-MODE-IREFS>"
        )

        def mutate(root):
            sensor = "/ComponentTypes/SensorSwc/SensorSwc_InternalBehavior"
            _insert_after_short_name(
                _element(root, f"{sensor}/IT_SensorMain"), iref.format(port="/ComponentTypes/SensorSwc/rCalib")
            )
            _insert_after_short_name(
                _element(root, f"{BEHAVIOUR}/TMT_ActuatorMain"), iref.format(port="/Elsewhere/rMode")
            )

        assert _added(exchange_source, tmp_path, mutate) == [
            (f"{BEHAVIOUR}/TMT_ActuatorMain", "DISABLED-MODE-IREFS", BEHAVIOURAL, NOT_SUPPORTED),
            (
                "/ComponentTypes/SensorSwc/SensorSwc_InternalBehavior/IT_SensorMain",
                "DISABLED-MODE-IREFS",
                BEHAVIOURAL,
                NOT_SUPPORTED,
            ),
        ]


class TestTheGeneratedNames:
    """F19 and F20: the names SWCraft generates itself."""

    def test_swcrafts_own_names_are_no_line(self, exchange_source, tmp_path) -> None:
        assert [e for e in _import(exchange_source, tmp_path) if e.tag == "SHORT-NAME"] == []

    def test_a_behaviour_an_event_and_a_compu_method_named_otherwise(self, exchange_source, tmp_path) -> None:
        """A differing event name is stated, so the line is left to one the importer declines.

        Here a name equal to its runnable's ignoring case ([constr_2508])."""

        def mutate(root):
            _element(root, f"{BEHAVIOUR}/TMT_ActuatorMain").find(f"{NS}SHORT-NAME").text = "ACTUATORMAIN"
            method = _element(root, "/DataTypes/Application/Gear_T_CompuMethod")
            method.find(f"{NS}SHORT-NAME").text = "GearTable"
            for ref in root.iter(f"{NS}COMPU-METHOD-REF"):
                if ref.text == "/DataTypes/Application/Gear_T_CompuMethod":
                    ref.text = "/DataTypes/Application/GearTable"

        # A method not named for its type is no private form: it is a `texttable` element under its
        # own name, so only the event's is lost.
        assert _added(exchange_source, tmp_path, mutate) == [
            (f"{BEHAVIOUR}/ACTUATORMAIN", "SHORT-NAME", DESCRIPTIVE, NOT_SUPPORTED),
        ]

    def test_f20_a_unit_two_application_types_share(self, exchange_source, tmp_path) -> None:
        """A shared form is one element, so F20 is left to a form no element holds.

        Here a unit stating an SI relation, which both types state inline, and which is named once more for what it
        states -- and the compu method naming it, which a ``linear`` element cannot hold while its unit is no element.
        The constraint is an element."""

        def mutate(root):
            twin = etree.fromstring(etree.tostring(_element(root, "/DataTypes/Application/Velocity_T")))
            twin.find(f"{NS}SHORT-NAME").text = "Velocity2_T"
            _element(root, "/DataTypes/Application/Velocity_T").addnext(twin)
            add_xml(
                _element(root, "/DataTypes/Application/Velocity_T_Unit"), "<FACTOR-SI-TO-UNIT>1</FACTOR-SI-TO-UNIT>"
            )

        added = _added(exchange_source, tmp_path, mutate)
        shared = [row for row in added if row[1] != "SHORT-NAME"]
        assert shared == [
            ("/DataTypes/Application/Velocity_T_Compu", "COMPU-METHOD", DESCRIPTIVE, NOT_SUPPORTED),
            ("/DataTypes/Application/Velocity_T_Unit", "UNIT", BEHAVIOURAL, NOT_SUPPORTED),
            ("/DataTypes/Application/Velocity_T_Unit", "UNIT", DESCRIPTIVE, NOT_SUPPORTED),
        ]
        assert "shared by 2 data types" in next(
            e.reason
            for e in _added_entries(exchange_source, tmp_path / "again", mutate)
            if e.tag == "UNIT" and e.effect == DESCRIPTIVE
        )


class TestTheClosedTablesReportWhatNoFamilyReads:
    """A child or an attribute a closed table names *unread* is one behavioural line."""

    def test_a_category_on_an_interface_and_an_attribute_on_a_data_element(self, exchange_source, tmp_path) -> None:
        def mutate(root):
            _insert_after_short_name(_element(root, "/PortInterfaces/IVehicleSpeed"), "<CATEGORY>VENDOR</CATEGORY>")
            _element(root, "/PortInterfaces/IVehicleSpeed/speed").set("S", "checksum")

        assert _added(exchange_source, tmp_path, mutate) == [
            ("/PortInterfaces/IVehicleSpeed", "CATEGORY", BEHAVIOURAL, NOT_SUPPORTED),
            ("/PortInterfaces/IVehicleSpeed/speed", "S", BEHAVIOURAL, NOT_SUPPORTED),
        ]


# ---------------------------------------------------------------------------
# import component: behavioural aborts, descriptive is printed and imported past
# ---------------------------------------------------------------------------


@pytest.fixture
def actuator_export(tmp_path: Path) -> Path:
    """``ActuatorSwc``'s component export, whose closure holds no array-typed argument."""
    out = tmp_path / "ActuatorSwc.arxml"
    export_component(str(EXCHANGE_MODEL_ROOT), "ActuatorSwc", str(out))
    return out


def _mutated(source: Path, mutate) -> Path:
    tree = etree.parse(str(source))
    mutate(tree.getroot())
    return _save(tree, source.with_name("mutated.arxml"))


class TestImportComponentSplitsTheEffect:
    def test_the_clean_export_imports_with_no_block(self, actuator_export, tmp_path) -> None:
        project = init_project(tmp_path / "project")
        report = import_component(str(actuator_export), str(project / "model"), dry_run=True)
        assert report.not_carried_lines == []
        assert NOT_CARRIED_HEADING not in report.render()

    def test_a_behavioural_line_aborts(self, actuator_export, tmp_path) -> None:
        """A runnable's unreadable ``MINIMUM-START-INTERVAL`` drops the runnable: behavioural, the import refused."""

        def mutate(root):
            _element(root, f"{BEHAVIOUR}/ActuatorMain").find(f"{NS}MINIMUM-START-INTERVAL").text = "INF"

        project = init_project(tmp_path / "project")
        with pytest.raises(LossyComponentImportError) as caught:
            import_component(str(_mutated(actuator_export, mutate)), str(project / "model"))
        assert any(line.startswith(f"RUNNABLE-ENTITY {BEHAVIOUR}/ActuatorMain:") for line in caught.value.skip_lines)

    def test_a_com_spec_line_is_descriptive_and_the_port_written(self, actuator_export, tmp_path) -> None:
        """A com-spec child SWCraft does not carry is one line under *Not carried*."""

        def mutate(root):
            spec = _element(root, f"{ACTUATOR}/rSpeed").find(f"{NS}REQUIRED-COM-SPECS/{NS}NONQUEUED-RECEIVER-COM-SPEC")
            add_xml(spec, "<HANDLE-DATA-STATUS>true</HANDLE-DATA-STATUS>")

        project = init_project(tmp_path / "project")
        report = import_component(str(_mutated(actuator_export, mutate)), str(project / "model"))

        assert report.swc_action == "created"
        assert [line.split(" ", 2)[:2] for line in report.not_carried_lines] == [
            ["HANDLE-DATA-STATUS", f"{ACTUATOR}/rSpeed/REQUIRED-COM-SPECS:"]
        ]
        assert report.missing_references == []

    @pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "write"])
    def test_a_descriptive_line_is_printed_and_imported_past(self, actuator_export, tmp_path, dry_run) -> None:
        def mutate(root):
            _element(root, "/PortInterfaces/IVehicleSpeed").set("UUID", "1234")
            # Declined, not stated: equal to its runnable's name ignoring case.
            _element(root, f"{BEHAVIOUR}/TMT_ActuatorMain").find(f"{NS}SHORT-NAME").text = "ACTUATORMAIN"

        project = init_project(tmp_path / "project")
        report = import_component(str(_mutated(actuator_export, mutate)), str(project / "model"), dry_run=dry_run)

        assert report.swc_action == "created"
        assert sorted(line.split(" ", 2)[:2] for line in report.not_carried_lines) == [
            ["SHORT-NAME", f"{BEHAVIOUR}/ACTUATORMAIN:"],
            ["UUID", "/PortInterfaces/IVehicleSpeed:"],
        ]
        rendered = report.render()
        assert f"{NOT_CARRIED_HEADING} (2; " in rendered
        assert all(f"  - {line}" in rendered for line in report.not_carried_lines)
        assert bool(report.written_paths) is not dry_run


# ---------------------------------------------------------------------------
# A SWCraft-written file produces no line of a new family
# ---------------------------------------------------------------------------


#: The reasons' marks of the families, by the site that writes each (the reader's,
#: ``swc_mapping``'s, ``type_mapping``'s and ``report.generated_name_losses``' texts).
_FAMILY_MARKS = (
    "not carried: SWCraft writes NOT-ACCESSIBLE there",  # F1
    "COMPU-METHOD-REF of an IMPLEMENTATION-DATA-TYPE",  # F2
    "SW-DATA-DEF-PROPS",  # F3 (and the conditional's attribute)
    "SWCraft writes one com-spec per data element",  # F4, whole
    "of a com-spec not carried",  # F4, a value
    "FILTER other than",  # F4, a filter
    "COM-SPEC is not read by SWCraft",  # F4, a child
    "port on an external interface stub not carried",  # F5
    "SERVER-ARGUMENT-IMPL-POLICY",  # F6
    "SWCraft models no application error",  # F7, F8
    "CONSTR-LEVEL of a DATA-CONSTR-RULE",  # F9
    "COMPU-SCALE not carried",  # F10, F11, F14 (a LINEAR scale's DESC)
    "SHORT-LABEL of a value specification",  # F12
    "not carried: SWCraft carries no ",  # F13
    "is package content SWCraft does not carry",  # F15
    "not carried exactly",  # F14
    "disabled mode",  # F16
    "DATA-CONSTR-REF of an IMPLEMENTATION-DATA-TYPE",  # F17
    "SWCraft names it itself",  # F19
    "data types not carried as one",  # F20
    "MEASUREMENT-POINT",  # F21
    "beside SW-IMPL-POLICY QUEUED",  # F22
    "is not read by SWCraft -- not imported or preserved",  # the closed tables' unread attribute
)


def _is_a_new_family(entry: SkipEntry) -> bool:
    return any(mark in entry.reason for mark in _FAMILY_MARKS)


#: The trees whose ARXML the writer refuses, each with the words of its refusal: a fixture of a
#: model `generate arxml` must not write. Any other exception out of the writer fails the gate,
#: and so does a tree listed here that the writer now writes.
_REFUSED_TREES = {
    "packages/tools/tests/fixtures/editor_schema/accepted_but_invalid": (ElementParseError, "duplicate reads entry"),
    # The tree's own refusal: an undeclared implementation type.
    "packages/tools/tests/fixtures/editor_schema/valid": (ValueError, "is not declared in data_types/implementation/"),
    "packages/tools/tests/fixtures/handle_termination_and_restart/model": (
        ValueError,
        "rule:swc.undeclared-termination-and-restart",
    ),
    # Its variable's unmapped type, `check-model`'s finding on the tree.
    "packages/tools/tests/fixtures/recorded_oracles/impact_model": (ValueError, "'SpeedControl' speaks 'SpeedPhys'"),
}


#: The trees this gate does not judge, each with its reason. The one tree listed is no model, the way
#: the byte-identity suites exempt it.
_NOT_JUDGED: dict[str, str] = {
    "packages/tools/tests/fixtures/unknown_key_oracle/model": (
        "test_unknown_key_oracle.py's per-file corpus: "
        "one element of each kind no other corpus reaches, each loaded on its own through its "
        "layer's adapter. Its references name elements it does not hold, so it is no consistent "
        "model; nothing runs check-model or generates from it, and its build is no refusal to record."
    ),
}


class TestASwcraftWrittenFileProducesNoLine:
    @pytest.mark.parametrize("version", list(ArxmlVersion), ids=str)
    @pytest.mark.parametrize(
        "model_root",
        [pytest.param(root, id=tree_id(root)) for root in committed_model_roots() if tree_id(root) not in _NOT_JUDGED],
    )
    def test_no_committed_tree_reports_a_line_of_a_new_family(self, model_root: Path, version, tmp_path) -> None:
        copy = tmp_path / "model"
        shutil.copytree(model_root, copy)
        refusal = _REFUSED_TREES.get(tree_id(model_root))
        if refusal is not None:
            # A recorded refusal, held to its words: a writer crash of another
            # kind is a failure here, never a skip.
            error, words = refusal
            with pytest.raises(error, match=re.escape(words)):
                _whole_model(copy, tmp_path / "model.arxml", version=version)
            return
        source = _whole_model(copy, tmp_path / "model.arxml", version=version)
        lines = [f"{e.tag} {e.path}: {e.reason}" for e in _import(source, tmp_path) if _is_a_new_family(e)]
        assert lines == []

    def test_the_gate_judges_the_trees_it_is_meant_to(self) -> None:
        """Counted as judged, not as found: every listed tree but the recorded refusals is imported."""
        trees = {tree_id(root) for root in committed_model_roots()}
        assert set(_REFUSED_TREES) <= trees
        assert set(_NOT_JUDGED) <= trees
        assert len(trees - set(_REFUSED_TREES) - set(_NOT_JUDGED)) >= 15


# -- what another module reads from this one --------------------------------------------------

#: The names of this module that the work repository's closed half (run by ``dev.py test-internal``,
#: not in the open copy) reads: the fixture by name, and the class's ``imported`` and ``CASES``. Rename
#: or remove one and the open gate stays green while ``test-internal`` breaks, so the test below fails
#: first, in the open gate, and the rename is carried to the closed half in the same commit.
CLOSED_HALF_READS = ("exchange_source", "TestEveryInterfaceDropChainsItsMembers")


def test_every_name_the_closed_half_reads_is_here() -> None:
    missing = [name for name in CLOSED_HALF_READS if name not in globals()]
    assert missing == [], f"renamed or removed, and read by the closed half under test-internal: {missing}"
    owner = TestEveryInterfaceDropChainsItsMembers
    assert hasattr(owner, "imported") and hasattr(owner, "CASES"), "the closed half reads both"
