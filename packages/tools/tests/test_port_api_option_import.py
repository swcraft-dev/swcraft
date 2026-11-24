"""No ``PORT-API-OPTION`` is written, and a source option is judged against none.

**The writer** writes no ``PORT-API-OPTIONS``: the model states no port API option,
``SwcInternalBehavior.portAPIOption`` is ``0..*`` (SWCT R4.2.2 ``:23196``, R4.3.0 ``:25726``), the
container is ``minOccurs="0"`` in both licensed schemas, and the RTE acts on a flag set to true only
(``[SWS_Rte_02613]``, ``[SWS_Rte_03799]``, ``[SWS_Rte_07100]``, SWS RTE R22-11). Every committed tree
is generated at both versions and holds none, and each behaviour's children keep the schema's order.

**The importer** reads each source option's raw facts (``arxml_reader.PortApiOptionInfo``) and the
mapper judges them once the component's ports are decided (``swc_mapping.report_port_api_options``).
Every option is one line keyed at its ``PORT-REF``'s text as written, or at the behaviour's shared key
when it has no ``PORT-REF``, an option with both features off too: no text of the standard gives an
absent option a default, so the import may not drop one without a line. That line is descriptive,
since such an option requests nothing (``[TPS_SWCT_01355]``, ``[TPS_SWCT_01356]``); every other
option's line is behavioural. An option naming a port the import does not keep is chained to that
port's own entry.

**The cases are shared.** ``fixtures/port_api_option/port-api-option-cases.json`` is read here and by
a closed comparator test, which asserts every case's option a finding unless its own line covers it.
Its names are invented.

What this module does **not** reach: real tool exports (the closed suites hold them), and an option of
a component the mapper skips whole, whose own line covers it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from behaviour_losses_support import MINIMAL_SWC, NS, SWC_PATH, add_xml, snippet
from conftest import ACCESS_POINTS_MODEL_ROOT, REPO_ROOT
from fidelity_strip_support import parse, restore_port_api_options, serialize
from lxml import etree
from project_support import init_project
from swcraft_tools.arxml_import import map_data_types, map_interfaces, map_swcs
from swcraft_tools.arxml_reader import PortApiOptionInfo, SkipCategory, SkipEntry, SkipLog, StandardRule, read_arxml
from swcraft_tools.component_exchange import LossyComponentImportError, export_component, import_component
from swcraft_tools.writer import generate_arxml
from test_writer_fidelity_refreeze import CASES

CASE_FILE = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "port_api_option" / "port-api-option-cases.json"
CASE_TABLE: list[dict] = json.loads(CASE_FILE.read_text(encoding="utf-8"))["cases"]

BEHAVIOUR_PATH = f"{SWC_PATH}/WheelSpeedSensorSwc_InternalBehavior"
SHARED_KEY = f"{BEHAVIOUR_PATH}/PORT-API-OPTIONS"
INTERFACE = "/Acme/Chassis/Interfaces/IWheelSpeed"
TAG = "PORT-API-OPTION"
NO_OPTION = "SWCraft carries no port API option"

#: The fates whose port the import does not keep: each has its own entry at ``(path, kind)``.
UNKEPT_FATES = frozenset({"skipped", "no-interface", "duplicate"})

#: Each verdict of the case table, as the first words its line's reason may start with.
REASON_PREFIX: dict[str, tuple[str, ...]] = {
    "attribute": ("PORT-API-OPTION states the attribute ",),
    "no-reference": ("PORT-API-OPTION has no PORT-REF, or an empty one",),
    "names-no-port": ("PORT-API-OPTION's PORT-REF names no port of this component",),
    "wrong-dest": ("PORT-API-OPTION's PORT-REF DEST ",),
    "unkept": ("PORT-API-OPTION of port ",),
    "second": ("a second PORT-API-OPTION for one port",),
    "child": ("PORT-API-OPTION carries ", "PORT-API-OPTION states PORT-REF more than once"),
    "flag": ("PORT-API-OPTION's ENABLE-TAKE-ADDRESS or INDIRECT-API is absent, stated twice or not false",),
    "off": ("PORT-API-OPTION states both of its features off: it requests nothing",),
}

#: The children a behaviour may hold, in ``group SWC-INTERNAL-BEHAVIOR``'s order as far as the writer
#: emits them (``INTERNAL-BEHAVIOR``'s ``DATA-TYPE-MAPPING-REFS`` first, then ``EVENTS``,
#: ``HANDLE-TERMINATION-AND-RESTART``, ``RUNNABLES`` and ``SUPPORTS-MULTIPLE-INSTANTIATION``).
BEHAVIOUR_ORDER = (
    "SHORT-NAME",
    "ADMIN-DATA",
    "DATA-TYPE-MAPPING-REFS",
    "EVENTS",
    "HANDLE-TERMINATION-AND-RESTART",
    "RUNNABLES",
    "SUPPORTS-MULTIPLE-INSTANTIATION",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _port_xml(port: dict) -> str:
    kind, name, fate = port["kind"], port["name"], port["fate"]
    reference = "REQUIRED-INTERFACE-TREF" if kind == "R-PORT-PROTOTYPE" else "PROVIDED-INTERFACE-TREF"
    interface = (
        "" if fate == "no-interface" else f'<{reference} DEST="SENDER-RECEIVER-INTERFACE">{INTERFACE}</{reference}>'
    )
    desc = '<DESC><L-2 L="FOR-ALL">one</L-2><L-2 L="DE">zwei</L-2></DESC>' if fate == "kept-with-desc-line" else ""
    return f"<{kind}><SHORT-NAME>{name}</SHORT-NAME>{desc}{interface}</{kind}>"


def _source(tmp_path: Path, case: dict) -> Path:
    """``minimal_swc.arxml`` with the case's ports and options in place of its own."""
    tree = etree.parse(str(MINIMAL_SWC))
    component = next(tree.getroot().iter(f"{NS}APPLICATION-SW-COMPONENT-TYPE"))
    ports = component.find(f"{NS}PORTS")
    for child in list(ports):
        ports.remove(child)
    add_xml(ports, "".join(_port_xml(port) for port in case["ports"]))
    options = next(component.iter(f"{NS}PORT-API-OPTIONS"))
    for child in list(options):
        options.remove(child)
    add_xml(options, "".join(option.replace("{component}", SWC_PATH) for option in case["options"]))
    (termination,) = snippet("<HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>")
    options.addprevious(termination)
    path = tmp_path / f"{case['id']}.arxml"
    path.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    return path


def _map(path: Path) -> tuple[list[SkipEntry], list[SkipEntry]]:
    """Read and map *path*: the reader's entries and the mapper's."""
    model = read_arxml(str(path))
    forms: list = []
    data_types, registry, _type_log = map_data_types(model, elements=forms)
    interfaces, iface_log = map_interfaces(model, registry, data_types, elements=forms)
    _swcs, swc_log = map_swcs(model, interfaces, iface_skip_log=iface_log, port_inits={}, variable_types={})
    return list(model.skip_log), list(swc_log)


def _expected_key(case: dict, index: int) -> str:
    verdict = case["verdicts"][index]
    if verdict["key"] == "shared":
        return SHARED_KEY
    option = etree.fromstring(f'<W xmlns="http://autosar.org/schema/r4.0">{case["options"][index]}</W>'.encode())
    return option[0].find(f"{NS}PORT-REF").text.replace("{component}", SWC_PATH)


def _case_id(case: dict) -> str:
    return case["id"]


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("tree", "version"), CASES, ids=[f"{t}:{v}" for t, v in CASES])
def test_no_output_holds_a_port_api_option(tree: str, version: str, tmp_path: Path) -> None:
    """Every committed tree, at both versions: no container, and each behaviour in the schema's order."""
    output = tmp_path / "model.arxml"
    generate_arxml(str(REPO_ROOT / tree), str(output), version=version)
    root = etree.parse(str(output)).getroot()

    assert list(root.iter(f"{NS}PORT-API-OPTIONS")) == []
    for behaviour in root.iter(f"{NS}SWC-INTERNAL-BEHAVIOR"):
        children = [etree.QName(child).localname for child in behaviour]
        assert set(children) <= set(BEHAVIOUR_ORDER), children
        assert children == sorted(children, key=BEHAVIOUR_ORDER.index), children


def test_the_shapes_the_trees_hold() -> None:
    """The three behaviour shapes the writer test above reaches, each with a component port or not."""
    shapes = set()
    for tree in sorted({tree for tree, _version in CASES}):
        for path in (REPO_ROOT / tree).glob("swc/*/*.yaml"):
            text = path.read_text(encoding="utf-8")
            shapes.add(("ports:" in text and "ports: []" not in text, "runnables:" in text))
    assert {(True, True), (True, False)} <= shapes


def test_the_component_export_holds_none(tmp_path: Path) -> None:
    arxml = tmp_path / "AccessSwc.arxml"
    export_component(str(ACCESS_POINTS_MODEL_ROOT), "AccessSwc", str(arxml))
    assert list(etree.parse(str(arxml)).iter(f"{NS}PORT-API-OPTIONS")) == []


# ---------------------------------------------------------------------------
# The reader reads, and judges nothing
# ---------------------------------------------------------------------------


class TestTheReader:
    def test_each_option_is_read_as_raw_facts(self, tmp_path: Path) -> None:
        case = {
            "id": "facts",
            "ports": [
                {"name": "pAlpha", "kind": "P-PORT-PROTOTYPE", "fate": "kept"},
                {"name": "pAlpha", "kind": "R-PORT-PROTOTYPE", "fate": "duplicate"},
            ],
            "options": [
                '<PORT-API-OPTION S="x"><ENABLE-TAKE-ADDRESS> false</ENABLE-TAKE-ADDRESS>'
                '<PORT-REF DEST="P-PORT-PROTOTYPE" BASE="b">{component}/pAlpha</PORT-REF>'
                "<INDIRECT-API/><INDIRECT-API>0</INDIRECT-API><PORT-ARG-VALUES/></PORT-API-OPTION>",
                "<PORT-API-OPTION/>",
            ],
        }
        (swc,) = read_arxml(str(_source(tmp_path, case))).swcs

        assert swc.port_api_options == [
            PortApiOptionInfo(
                container_path=SHARED_KEY,
                port_ref=f"{SWC_PATH}/pAlpha",
                port_ref_dest="P-PORT-PROTOTYPE",
                referenced_kinds=("P-PORT-PROTOTYPE", "R-PORT-PROTOTYPE"),
                child_tags=("ENABLE-TAKE-ADDRESS", "PORT-REF", "INDIRECT-API", "INDIRECT-API", "PORT-ARG-VALUES"),
                stated_attributes=(("PORT-API-OPTION", "S"), ("PORT-REF", "BASE")),
                enable_take_address=(" false",),
                indirect_api=("", "0"),
            ),
            PortApiOptionInfo(
                container_path=SHARED_KEY,
                port_ref=None,
                port_ref_dest=None,
                referenced_kinds=(),
                child_tags=(),
                stated_attributes=(),
                enable_take_address=(),
                indirect_api=(),
            ),
        ]

    @pytest.mark.parametrize("case", CASE_TABLE, ids=_case_id)
    def test_the_reader_records_no_option_line(self, case: dict, tmp_path: Path) -> None:
        reader, _mapper = _map(_source(tmp_path, case))
        assert [entry for entry in reader if entry.tag == TAG] == []


# ---------------------------------------------------------------------------
# The mapper judges each option: the shared case table
# ---------------------------------------------------------------------------


def test_the_case_table_covers_every_verdict_and_fate() -> None:
    verdicts = {verdict["verdict"] for case in CASE_TABLE for verdict in case["verdicts"]}
    fates = {port["fate"] for case in CASE_TABLE for port in case["ports"]}
    assert verdicts == set(REASON_PREFIX)
    assert fates == {"kept", "kept-with-desc-line", *UNKEPT_FATES}
    for case in CASE_TABLE:
        assert len(case["options"]) == len(case["verdicts"]), case["id"]


@pytest.mark.parametrize("case", CASE_TABLE, ids=_case_id)
def test_each_port_s_fate_is_what_the_case_says(case: dict, tmp_path: Path) -> None:
    """The precondition the table rests on: an unkept port has its entry at ``(path, kind)``, a kept one none."""
    reader, mapper = _map(_source(tmp_path, case))
    keys = {(entry.path, entry.tag) for entry in [*reader, *mapper]}
    for port in case["ports"]:
        path = f"{SWC_PATH}/{port['name']}"
        if port["fate"] in UNKEPT_FATES:
            assert (path, port["kind"]) in keys, port
            continue
        assert (path, port["kind"]) not in keys, port
        assert ((path, "DESC") in keys) is (port["fate"] == "kept-with-desc-line"), port


@pytest.mark.parametrize("case", CASE_TABLE, ids=_case_id)
def test_the_mapper_decides_each_option_as_the_table_says(case: dict, tmp_path: Path) -> None:
    """One line per option, in option order, at its key, with its reason.

    The reason is behavioural, except the both-features-off option's descriptive one."""
    _reader, mapper = _map(_source(tmp_path, case))
    lines = [entry for entry in mapper if entry.tag == TAG]
    expected = [(_expected_key(case, index), verdict["verdict"]) for index, verdict in enumerate(case["verdicts"])]

    assert [entry.path for entry in lines] == [key for key, _verdict in expected]
    for entry, (_key, verdict) in zip(lines, expected, strict=True):
        assert entry.reason.startswith(REASON_PREFIX[verdict]), (verdict, entry.reason)
        assert NO_OPTION in entry.reason, entry.reason
        # An option with both features off requests nothing ([TPS_SWCT_01355], [TPS_SWCT_01356]), so
        # its line is descriptive; every other option aborts.
        assert entry.effect == ("report" if verdict == "off" else "abort"), (verdict, entry.effect)
        if verdict != "unkept":
            assert (entry.category, entry.rule) == (SkipCategory.NOT_SUPPORTED, None)


@pytest.mark.parametrize(
    "case",
    [case for case in CASE_TABLE if any(verdict["verdict"] == "unkept" for verdict in case["verdicts"])],
    ids=_case_id,
)
def test_an_option_on_an_unkept_port_is_chained_to_the_port_s_own_entry(case: dict, tmp_path: Path) -> None:
    reader, mapper = _map(_source(tmp_path, case))
    (line,) = [entry for entry in mapper if entry.tag == TAG and entry.reason.startswith(REASON_PREFIX["unkept"])]
    # The root is the first entry at the port's key, the mapper's before the reader's: a duplicate
    # name has one in each (the path index's and the mapper's).
    root = next(entry for entry in [*mapper, *reader] if (entry.path, entry.tag) == (line.path, _dest(line, case)))

    assert (line.category, line.rule, line.effect) == (root.category, root.rule, root.effect)
    assert f"({root.reason})" in line.reason


def _dest(line: SkipEntry, case: dict) -> str:
    (port,) = [port for port in case["ports"] if port["fate"] in UNKEPT_FATES]
    assert line.path == f"{SWC_PATH}/{port['name']}"
    return port["kind"]


def test_a_duplicate_port_s_option_inherits_the_section_one_rule(tmp_path: Path) -> None:
    """Chained, the line takes its root's category and rule: a duplicate name breaks ``[constr_2508]``."""
    (case,) = [case for case in CASE_TABLE if case["id"] == "same-named-ports-of-two-kinds"]
    _reader, mapper = _map(_source(tmp_path, case))
    lines = [entry for entry in mapper if entry.tag == TAG]

    # Every option line of the case: the kept port's descriptive one, then the unkept port's chained one.
    assert [(entry.effect, entry.reason.startswith(REASON_PREFIX["unkept"])) for entry in lines] == [
        ("report", False),
        ("abort", True),
    ]
    assert (lines[1].category, lines[1].rule) == (SkipCategory.EXPORT_DEFECT, StandardRule.SHORT_NAME_UNIQUE)


# ---------------------------------------------------------------------------
# import component
# ---------------------------------------------------------------------------


def _pre_change_export(tmp_path: Path) -> Path:
    """``AccessSwc``'s export with the false/false options an earlier writer wrote, one per kept port."""
    arxml = tmp_path / "AccessSwc.arxml"
    export_component(str(ACCESS_POINTS_MODEL_ROOT), "AccessSwc", str(arxml))
    root = parse(arxml.read_bytes())
    assert restore_port_api_options(root) == 1
    arxml.write_bytes(serialize(root))
    return arxml


def test_a_pre_change_component_file_imports_with_one_descriptive_line_per_option(tmp_path: Path) -> None:
    """Each both-features-off option is a descriptive line and the component is imported whole.

    Such an option requests nothing (``[TPS_SWCT_01355]``, ``[TPS_SWCT_01356]``: each feature acts when
    ``true``)."""
    arxml = _pre_change_export(tmp_path)
    options = len(list(etree.parse(str(arxml)).iter(f"{NS}PORT-API-OPTION")))
    assert options >= 1
    project = init_project(tmp_path / "proj")

    report = import_component(str(arxml), str(project / "model"))

    assert report.swc_action == "created"
    assert not [line for line in report.skip_lines if TAG in line]
    lines = [line for line in report.not_carried_lines if TAG in line]
    assert len(lines) == options
    assert all(REASON_PREFIX["off"][0] in line and NO_OPTION in line for line in lines)


def test_an_option_with_port_arg_values_still_aborts(tmp_path: Path) -> None:
    arxml = _pre_change_export(tmp_path)
    tree = etree.parse(str(arxml))
    add_xml(
        next(tree.iter(f"{NS}PORT-API-OPTION")), "<PORT-ARG-VALUES><PORT-DEFINED-ARGUMENT-VALUE/></PORT-ARG-VALUES>"
    )
    arxml.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    project = init_project(tmp_path / "proj")

    with pytest.raises(LossyComponentImportError) as raised:
        import_component(str(arxml), str(project / "model"))

    # Every PORT-API-OPTION line the refusal names: the one option with content alone -- the other,
    # both-features-off options' lines are descriptive and stop nothing.
    assert [line for line in raised.value.skip_lines if line.startswith("PORT-API-OPTION ")] == [
        "PORT-API-OPTION /ComponentTypes/AccessSwc/pSignals: PORT-API-OPTION carries PORT-ARG-VALUES, which "
        "SWCraft does not read -- SWCraft carries no port API option"
    ]


def test_the_case_table_names_nothing_of_a_reference_export() -> None:
    """The names are invented: the fixture component's and the table's own."""
    names = {port["name"] for case in CASE_TABLE for port in case["ports"]}
    assert names == {"pAlpha", "rBeta", "prGamma", "pDelta", "pDup"}


@pytest.fixture(autouse=True)
def _no_writes_to_the_committed_fixture() -> None:
    before = MINIMAL_SWC.read_bytes()
    yield
    assert MINIMAL_SWC.read_bytes() == before


def test_a_port_s_root_is_one_indexed_lookup_of_either_effect() -> None:
    """``SkipLog.first_tagged`` returns the first entry at ``(path, tag)`` of either effect, one lookup per option."""
    log = SkipLog()
    assert log.first_tagged("/C/pAlpha", "P-PORT-PROTOTYPE") is None
    report = log.record("/C/pAlpha", "P-PORT-PROTOTYPE", "a", category=SkipCategory.NOT_SUPPORTED, effect="report")
    log.record("/C/pAlpha", "P-PORT-PROTOTYPE", "b", category=SkipCategory.NOT_SUPPORTED, effect="abort")
    log.record("/C/pAlpha", "DESC", "c", category=SkipCategory.NOT_SUPPORTED, effect="abort")
    assert log.first_tagged("/C/pAlpha", "P-PORT-PROTOTYPE") is report
    assert log.first_tagged("/C/pAlpha", "R-PORT-PROTOTYPE") is None


# ---------------------------------------------------------------------------
# A child outside the AUTOSAR namespace is never a PORT-REF or a flag
# ---------------------------------------------------------------------------


class TestAForeignChildIsNoOptionChild:
    """The foreign-namespace cases the shared table (``foreign-namespace-*``) cannot state."""

    def test_import_component_aborts_on_a_foreign_flag(self, tmp_path: Path) -> None:
        arxml = _pre_change_export(tmp_path)
        tree = etree.parse(str(arxml))
        option = next(tree.iter(f"{NS}PORT-API-OPTION"))
        flag = option.find(f"{NS}ENABLE-TAKE-ADDRESS")
        flag.tag = "{urn:invented-vendor}ENABLE-TAKE-ADDRESS"
        arxml.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
        project = init_project(tmp_path / "proj")

        with pytest.raises(LossyComponentImportError) as raised:
            import_component(str(arxml), str(project / "model"))

        assert any(
            "PORT-API-OPTION carries {urn:invented-vendor}ENABLE-TAKE-ADDRESS" in line
            for line in raised.value.skip_lines
        )
