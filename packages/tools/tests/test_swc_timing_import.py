"""The importer reads a runnable's execution-time budget back (contract 10.0.0).

An ``EXECUTION-TIME-CONSTRAINT`` under an ``SWC-TIMING``'s ``TIMING-REQUIREMENTS`` becomes the
runnable's ``wcet`` and ``wcet_type`` only when every carrying condition holds; every other shape
is a skip with its category, its effect and its reason. One hand-built file per shape (one class
per shape: the carried ones, the skipped ones, then the carried ones with what is not): each test starts from
SWCraft's own whole-model export of the ``component_exchange`` tree (with a second, eventless
runnable on ``ActuatorSwc``), adds ``SWC-TIMING`` content and imports the result, and asserts the
entries the content adds and the budget the written component carries. The writer -> importer
round trip is ``test_swc_timing_writer.py``'s; the ``X_Timing`` rename is at the end of this module.

What this does not reach: real tool exports; whether a row's behavioural/descriptive class is
right is a design judgement, never an RTE's.
"""

from __future__ import annotations

import shutil
from collections import Counter
from pathlib import Path

import pytest
import yaml
from lxml import etree
from project_support import init_project
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_reader import (
    MultidimensionalTimeInfo,
    SkipCategory,
    SkipEntry,
    _read_cse_code,
    read_arxml,
)
from swcraft_tools.arxml_version import NAMESPACE, ArxmlVersion
from swcraft_tools.component_exchange import LossyComponentImportError, export_component, import_component
from swcraft_tools.validator import check_identifier_conformance
from swcraft_tools.writer import generate_arxml

NS = f"{{{NAMESPACE}}}"
EXCHANGE_MODEL_ROOT = Path(__file__).parent / "fixtures" / "component_exchange" / "model"

ABORT = "abort"
REPORT = "report"
NOT_SUPPORTED = SkipCategory.NOT_SUPPORTED
OUTSIDE_FILE = SkipCategory.OUTSIDE_FILE

ACTUATOR = "/ComponentTypes/ActuatorSwc"
BEHAVIOUR = f"{ACTUATOR}/ActuatorSwc_InternalBehavior"
MAIN = f"{BEHAVIOUR}/ActuatorMain"
AUX = f"{BEHAVIOUR}/ActuatorAux"
SENSOR_MAIN = "/ComponentTypes/SensorSwc/SensorSwc_InternalBehavior/SensorMain"
TIMING = "/ComponentTypes/ActuatorSwc_Timing"


# ---------------------------------------------------------------------------
# The hand-built files
# ---------------------------------------------------------------------------


def _with_aux_runnable(target: Path, *, packages: bool = False) -> Path:
    """A copy of the exchange tree whose ``ActuatorSwc`` has a second runnable, ``ActuatorAux`` (no event)."""
    shutil.copytree(EXCHANGE_MODEL_ROOT, target)
    actuator = target / "swc" / "actuation" / "ActuatorSwc.yaml"
    text = actuator.read_text(encoding="utf-8").replace(
        "handle_termination_and_restart: no_support\n",
        "- name: ActuatorAux\nhandle_termination_and_restart: no_support\n",
    )
    if packages:
        text += "package: Actuation\n"
        sensor = target / "swc" / "sensing" / "SensorSwc.yaml"
        sensor.write_text(sensor.read_text(encoding="utf-8") + "package: Sensing\n", encoding="utf-8", newline="\n")
    actuator.write_text(text, encoding="utf-8", newline="\n")
    return target


@pytest.fixture(scope="module")
def model_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _with_aux_runnable(tmp_path_factory.mktemp("timing") / "model")


@pytest.fixture(scope="module")
def source(model_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """SWCraft's own 4.2.2 whole-model export of the tree: no timing content at all."""
    out = tmp_path_factory.mktemp("source") / "model.arxml"
    generate_arxml(str(model_root), str(out))
    return out


@pytest.fixture(scope="module")
def source_4_3_0(model_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("source430") / "model.arxml"
    generate_arxml(str(model_root), str(out), version=ArxmlVersion.V4_3_0)
    return out


def constraint(
    name: str = "ActuatorMain_Budget",
    *,
    executable: str = MAIN,
    dest: str = "RUNNABLE-ENTITY",
    type_: str | None = "GROSS",
    code: str | None = "0",
    factor: str | None = "400",
    kind: str = "EXECUTION-TIME-CONSTRAINT",
    before: str = "",
    after: str = "",
    maximum: bool = True,
) -> str:
    """One constraint's XML: *before* goes after the ``SHORT-NAME``, *after* after the ``MAXIMUM``."""
    if kind != "EXECUTION-TIME-CONSTRAINT":
        return f"<{kind}><SHORT-NAME>{name}</SHORT-NAME>{before}</{kind}>"
    members = "".join(
        (
            f'<EXECUTABLE-REF DEST="{dest}">{executable}</EXECUTABLE-REF>' if executable is not None else "",
            f"<EXECUTION-TIME-TYPE>{type_}</EXECUTION-TIME-TYPE>" if type_ is not None else "",
        )
    )
    max_xml = ""
    if maximum:
        max_xml = "<MAXIMUM>"
        max_xml += f"<CSE-CODE>{code}</CSE-CODE>" if code is not None else ""
        max_xml += f"<CSE-CODE-FACTOR>{factor}</CSE-CODE-FACTOR>" if factor is not None else ""
        max_xml += "</MAXIMUM>"
    return f"<{kind}><SHORT-NAME>{name}</SHORT-NAME>{before}{members}{max_xml}{after}</{kind}>"


def swc_timing(
    *requirements: str,
    name: str = "ActuatorSwc_Timing",
    guarantees: tuple[str, ...] = (),
    behavior: str | None = BEHAVIOUR,
    component: str | None = None,
    head: str = "",
    tail: str = "",
) -> str:
    """One ``SWC-TIMING``: *head* after the ``SHORT-NAME``, *tail* last."""
    parts = [f"<SWC-TIMING><SHORT-NAME>{name}</SHORT-NAME>{head}"]
    if guarantees:
        parts.append(f"<TIMING-GUARANTEES>{''.join(guarantees)}</TIMING-GUARANTEES>")
    if requirements:
        parts.append(f"<TIMING-REQUIREMENTS>{''.join(requirements)}</TIMING-REQUIREMENTS>")
    if behavior is not None:
        parts.append(f'<BEHAVIOR-REF DEST="SWC-INTERNAL-BEHAVIOR">{behavior}</BEHAVIOR-REF>')
    if component is not None:
        parts.append(f'<COMPONENT-REF DEST="APPLICATION-SW-COMPONENT-TYPE">{component}</COMPONENT-REF>')
    parts.append(f"{tail}</SWC-TIMING>")
    return "".join(parts)


def _parse(xml: str) -> etree._Element:
    return etree.fromstring(f'<W xmlns="{NAMESPACE}">{xml}</W>')[0]


def _package(root: etree._Element, name: str) -> etree._Element:
    """The top-level ``AR-PACKAGE`` *name*, created (with an ``ELEMENTS``) when the file has none."""
    packages = root.find(f"{NS}AR-PACKAGES")
    for package in packages.findall(f"{NS}AR-PACKAGE"):
        if package.findtext(f"{NS}SHORT-NAME") == name:
            return package
    package = _parse(f"<AR-PACKAGE><SHORT-NAME>{name}</SHORT-NAME><ELEMENTS/></AR-PACKAGE>")
    packages.append(package)
    return package


def with_timings(base: Path, out: Path, *timings: str, package: str = "ComponentTypes", mutate=None) -> Path:
    """*base* with each of *timings* appended to *package*'s ``ELEMENTS``, written to *out*."""
    tree = etree.parse(str(base))
    root = tree.getroot()
    elements = _package(root, package).find(f"{NS}ELEMENTS")
    for timing in timings:
        elements.append(_parse(timing))
    if mutate is not None:
        mutate(root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    return out


class Imported:
    """One ``import model`` run: the report's entries and the runnables of the written ``ActuatorSwc``."""

    def __init__(self, source: Path, tmp_path: Path, packages: list[str] | None = None) -> None:
        project = init_project(tmp_path / "project")
        self.report = run_import(str(source), str(project / "swcraft.yaml"), packages=packages)
        self.entries: list[SkipEntry] = list(self.report.skip_entries)
        self.swc_root = project / "model" / "swc"

    def runnables(self, swc: str = "ActuatorSwc") -> dict[str, dict]:
        (path,) = self.swc_root.rglob(f"{swc}.yaml")
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        return {runnable["name"]: runnable for runnable in document["runnables"]}

    def budget(self, runnable: str = "ActuatorMain", swc: str = "ActuatorSwc") -> tuple[str | None, str | None]:
        entry = self.runnables(swc)[runnable]
        return entry.get("wcet"), entry.get("wcet_type")

    def at(self, path: str, tag: str | None = None) -> list[SkipEntry]:
        return [entry for entry in self.entries if entry.path == path and (tag is None or entry.tag == tag)]

    def timing_entries(self) -> list[SkipEntry]:
        """Every entry about timing content: at an ``SWC-TIMING`` or below one."""
        return [entry for entry in self.entries if "Timing" in entry.path or entry.path.startswith("/TimingExt")]


def _import(base: Path, tmp_path: Path, *timings: str, **kwargs) -> Imported:
    packages = kwargs.pop("packages", None)
    patched = with_timings(base, tmp_path / "source.arxml", *timings, **kwargs)
    return Imported(patched, tmp_path, packages=packages)


def _one(entries: list[SkipEntry]) -> SkipEntry:
    assert len(entries) == 1, entries
    return entries[0]


def _shape(entry: SkipEntry) -> tuple[str, str, SkipCategory, str]:
    return (entry.path, entry.tag, entry.category, entry.effect)


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


class TestTheReader:
    def test_the_base_file_reads_no_timing(self, source) -> None:
        assert read_arxml(str(source)).swc_timings == []

    def test_an_swc_timing_is_indexed_and_read_whole(self, source, tmp_path) -> None:
        timing = swc_timing(
            constraint(
                before='<DESC><L-2 L="FOR-ALL">why</L-2></DESC>',
                after="<MINIMUM><CSE-CODE>0</CSE-CODE><CSE-CODE-FACTOR>1</CSE-CODE-FACTOR></MINIMUM>",
            ),
            guarantees=(constraint("G", kind="LATENCY-TIMING-CONSTRAINT"),),
            component=ACTUATOR,
        )
        model = read_arxml(str(with_timings(source, tmp_path / "s.arxml", timing)))
        assert TIMING in model.path_index
        (read,) = model.swc_timings
        assert (read.path, read.name, read.behavior_ref, read.component_ref, read.variation_point) == (
            TIMING, "ActuatorSwc_Timing", BEHAVIOUR, ACTUATOR, False,
        )  # fmt: skip
        guarantee, requirement = read.constraints
        assert (guarantee.kind, guarantee.role, guarantee.path) == (
            "LATENCY-TIMING-CONSTRAINT",
            "guarantee",
            f"{TIMING}/G",
        )
        assert requirement.role == "requirement"
        assert (requirement.executable_ref, requirement.executable_dest, requirement.execution_time_type) == (
            MAIN, "RUNNABLE-ENTITY", "GROSS",
        )  # fmt: skip
        assert requirement.maximum == MultidimensionalTimeInfo(0, "0", 400, "400")
        assert requirement.minimum == MultidimensionalTimeInfo(0, "0", 1, "1")
        assert requirement.scoping == ()
        assert [tag for tag, _reason in requirement.uncarried] == ["DESC"]
        assert [entry for entry in model.skip_log if "Timing" in entry.path] == []

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("0", 0), ("-0", 0), ("-000", 0), ("+026", 26), ("026", 26), (" 3\n", 3), ("4294967295", 4294967295)],
    )
    def test_a_cse_code_is_read_by_unsigned_int(self, text, expected) -> None:
        assert _read_cse_code(text) == expected

    @pytest.mark.parametrize("text", ["-1", "4294967296", "0x1A", "1.0", "", "2 6", "9" * 5000, "00000000004294967296"])
    def test_what_is_no_unsigned_int_is_none(self, text) -> None:
        assert _read_cse_code(text) is None

    def test_an_over_long_factor_is_marked_too_long_never_raised(self, source, tmp_path) -> None:
        timing = swc_timing(constraint(factor="9" * 5000))
        (read,) = read_arxml(str(with_timings(source, tmp_path / "s.arxml", timing))).swc_timings
        maximum = read.constraints[0].maximum
        assert (maximum.cse_factor, maximum.factor_overflow) == (None, "positive")

    def test_an_over_long_negative_factor_is_marked_negative(self, source, tmp_path) -> None:
        timing = swc_timing(constraint(factor="-" + "9" * 5000))
        (read,) = read_arxml(str(with_timings(source, tmp_path / "s.arxml", timing))).swc_timings
        maximum = read.constraints[0].maximum
        assert (maximum.cse_factor, maximum.factor_overflow) == (None, "negative")

    @pytest.mark.parametrize(("text", "radix_source"), [("0x352", "0x352"), (" 0620\n", "0620"), ("850", None)])
    def test_a_factor_s_radix_spelling_is_kept(self, source, tmp_path, text, radix_source) -> None:
        timing = swc_timing(constraint(factor=text))
        (read,) = read_arxml(str(with_timings(source, tmp_path / "s.arxml", timing))).swc_timings
        assert read.constraints[0].maximum.factor_radix_source == radix_source

    def test_an_unreadable_factor_is_not_too_long(self, source, tmp_path) -> None:
        timing = swc_timing(constraint(factor="1.5"))
        (read,) = read_arxml(str(with_timings(source, tmp_path / "s.arxml", timing))).swc_timings
        maximum = read.constraints[0].maximum
        assert (maximum.cse_factor, maximum.factor_overflow) == (None, None)

    def test_an_unread_attribute_of_the_closed_tables_is_reported(self, source, tmp_path) -> None:
        def mutate(root):
            for element in root.iter(f"{NS}SWC-TIMING", f"{NS}EXECUTION-TIME-CONSTRAINT"):
                element.set("S", "checksum")

        model = read_arxml(str(with_timings(source, tmp_path / "s.arxml", swc_timing(constraint()), mutate=mutate)))
        assert sorted((entry.path, entry.tag, entry.effect) for entry in model.skip_log if "Timing" in entry.path) == [
            (TIMING, "S", ABORT),
            (f"{TIMING}/ActuatorMain_Budget", "S", ABORT),
        ]


# ---------------------------------------------------------------------------
# Carried
# ---------------------------------------------------------------------------


class TestTheWriterShapeIsCarried:
    def test_the_writer_shaped_file_is_carried_with_no_line(self, source, tmp_path) -> None:
        imported = _import(source, tmp_path, swc_timing(constraint()))
        assert imported.budget() == ("400us", "gross")
        assert imported.timing_entries() == []
        assert imported.report.represented_lines == []

    @pytest.mark.parametrize(("code", "unit"), [("26", "ns"), ("0", "us"), ("3", "ms"), ("6", "s")])
    def test_each_unit_code_is_carried_unchanged(self, source, tmp_path, code, unit) -> None:
        imported = _import(source, tmp_path, swc_timing(constraint(code=code, factor="850", type_="NET")))
        assert imported.budget() == (f"850{unit}", "net")
        assert imported.report.represented_lines == []

    @pytest.mark.parametrize(
        ("code", "factor", "wcet"),
        [("27", "85", "850ns"), ("8", "2", "120s"), ("25", "20", "2ns")],
        ids=["10ns", "1min", "100ps"],
    )
    def test_another_time_code_is_normalised_with_its_line(self, source, tmp_path, code, factor, wcet) -> None:
        imported = _import(source, tmp_path, swc_timing(constraint(code=code, factor=factor)))
        assert imported.budget() == (wcet, "gross")
        assert imported.report.represented_lines == [
            f"{TIMING}/ActuatorMain_Budget: MAXIMUM CSE-CODE {code} with CSE-CODE-FACTOR {factor} represented as "
            f"wcet {wcet}"
        ]
        assert imported.timing_entries() == []
        assert imported.report.represented_lines[0] in imported.report.render()

    @pytest.mark.parametrize(
        ("code", "factor", "wcet"),
        [("26", "0x352", "850ns"), ("0", "0620", "400us"), ("3", "0b11", "3ms"), ("27", "0x55", "850ns")],
        ids=["hex-ns", "octal-us", "binary-ms", "hex-10ns"],
    )
    def test_a_factor_spelled_in_another_radix_is_said(self, source, tmp_path, code, factor, wcet) -> None:
        """A radix dropped is a line, never silent -- whatever the code."""
        imported = _import(source, tmp_path, swc_timing(constraint(code=code, factor=factor)))
        assert imported.budget() == (wcet, "gross")
        assert imported.report.represented_lines == [
            f"{TIMING}/ActuatorMain_Budget: MAXIMUM CSE-CODE {code} with CSE-CODE-FACTOR {factor} represented as "
            f"wcet {wcet}"
        ]

    @pytest.mark.parametrize(
        ("text", "wcet"), [("-0", "400us"), ("-000", "400us"), ("+026", "400ns"), ("026", "400ns")]
    )
    def test_a_cse_code_is_spelled_by_its_lexical_space(self, source, tmp_path, text, wcet) -> None:
        imported = _import(source, tmp_path, swc_timing(constraint(code=text)))
        assert imported.budget() == (wcet, "gross")
        assert imported.timing_entries() == []

    def test_a_matching_component_ref_is_carried(self, source, tmp_path) -> None:
        imported = _import(source, tmp_path, swc_timing(constraint(), component=ACTUATOR))
        assert imported.budget() == ("400us", "gross")
        assert imported.timing_entries() == []


# ---------------------------------------------------------------------------
# The skip rows
# ---------------------------------------------------------------------------

BUDGET = f"{TIMING}/ActuatorMain_Budget"


def _skipped(imported: Imported, path: str = BUDGET, tag: str = "EXECUTION-TIME-CONSTRAINT") -> SkipEntry:
    assert imported.budget() == (None, None)
    return _one(imported.at(path, tag))


class TestNoUnitHoldsIt:
    @pytest.mark.parametrize(
        ("code", "factor"),
        [("23", "1500"), ("100", "1"), ("997", "1"), ("50", "1")],
        ids=["1ps-inexact", "angle", "other", "outside-the-table"],
    )
    def test_the_code_is_a_skip_never_a_rounded_value(self, source, tmp_path, code, factor) -> None:
        entry = _skipped(_import(source, tmp_path, swc_timing(constraint(code=code, factor=factor))))
        assert (entry.category, entry.effect) == (NOT_SUPPORTED, ABORT)
        assert entry.reason.startswith(f"the maximum is written in CSE code {code}, which no budget unit holds exactly")


class TestAGuaranteeIsReported:
    def test_an_execution_time_guarantee_is_reported_and_the_import_goes_on(self, source, tmp_path) -> None:
        imported = _import(source, tmp_path, swc_timing(guarantees=(constraint(),)))
        entry = _skipped(imported)
        assert (entry.category, entry.effect) == (NOT_SUPPORTED, REPORT)
        assert "a timing guarantee is the supplier's statement" in entry.reason

    def test_another_guarantee_kind_at_4_3_0(self, source_4_3_0, tmp_path) -> None:
        imported = _import(
            source_4_3_0, tmp_path, swc_timing(guarantees=(constraint("Lat", kind="LATENCY-TIMING-CONSTRAINT"),))
        )
        entry = _one(imported.at(f"{TIMING}/Lat"))
        assert (entry.tag, entry.category, entry.effect) == ("LATENCY-TIMING-CONSTRAINT", NOT_SUPPORTED, REPORT)

    def test_a_guarantee_never_counts_as_a_second_requirement(self, source, tmp_path) -> None:
        imported = _import(
            source, tmp_path, swc_timing(constraint(), guarantees=(constraint("Guaranteed", factor="300"),))
        )
        assert imported.budget() == ("400us", "gross")
        assert [_shape(entry) for entry in imported.timing_entries()] == [
            (f"{TIMING}/Guaranteed", "EXECUTION-TIME-CONSTRAINT", NOT_SUPPORTED, REPORT)
        ]


class TestAScopedMaximum:
    @pytest.mark.parametrize(
        "member",
        [
            '<COMPONENT-IREF><TARGET-COMPONENT-REF DEST="SW-COMPONENT-PROTOTYPE">/ComponentTypes/Root/x'
            "</TARGET-COMPONENT-REF></COMPONENT-IREF>",
            "<VARIATION-POINT><SHORT-LABEL>v</SHORT-LABEL></VARIATION-POINT>",
        ],
        ids=["COMPONENT-IREF", "VARIATION-POINT"],
    )
    def test_a_scoping_member_skips_the_constraint(self, source, tmp_path, member) -> None:
        tag = member[1 : member.index(">")]
        entry = _skipped(_import(source, tmp_path, swc_timing(constraint(before=member))))
        assert (entry.category, entry.effect) == (NOT_SUPPORTED, ABORT)
        assert entry.reason == (
            f"the execution-time constraint carries {tag}: keeping the maximum without it would change what the "
            "budget means"
        )

    def test_a_timing_condition_ref_at_4_3_0(self, source_4_3_0, tmp_path) -> None:
        member = (
            '<TIMING-CONDITION-REF DEST="TIMING-CONDITION">/ComponentTypes/ActuatorSwc_Timing/C</TIMING-CONDITION-REF>'
        )
        entry = _skipped(_import(source_4_3_0, tmp_path, swc_timing(constraint(before=member))))
        assert (entry.category, entry.effect) == (NOT_SUPPORTED, ABORT)
        assert "carries TIMING-CONDITION-REF" in entry.reason


class TestAnIncompleteConstraint:
    @pytest.mark.parametrize(
        ("kwargs", "reason"),
        [
            ({"type_": None}, "the execution-time constraint has no EXECUTION-TIME-TYPE"),
            ({"type_": "BOTH"}, "EXECUTION-TIME-TYPE 'BOTH' is neither GROSS nor NET"),
            ({"maximum": False}, "the execution-time constraint has no MAXIMUM, and a budget is a maximum"),
            ({"code": None}, "the MAXIMUM has no CSE-CODE"),
            ({"factor": None}, "the MAXIMUM has no CSE-CODE-FACTOR"),
            ({"factor": "1.5"}, "the MAXIMUM's CSE-CODE-FACTOR '1.5' is not an AR:INTEGER"),
            ({"factor": "0"}, "the MAXIMUM's CSE-CODE-FACTOR is not positive, and a budget is a positive duration"),
            ({"factor": "-3"}, "the MAXIMUM's CSE-CODE-FACTOR is not positive, and a budget is a positive duration"),
            ({"code": "-1"}, "the MAXIMUM's CSE-CODE '-1' is not an xsd:unsignedInt"),
            ({"code": "4294967296"}, "the MAXIMUM's CSE-CODE '4294967296' is not an xsd:unsignedInt"),
            ({"executable": None}, "the execution-time constraint has no EXECUTABLE-REF, so it times no runnable"),
        ],
        ids=["no-type", "type", "no-maximum", "no-code", "no-factor", "decimal", "zero", "negative", "code-negative",
             "code-overflow", "no-executable"],
    )  # fmt: skip
    def test_the_member_is_named(self, source, tmp_path, kwargs, reason) -> None:
        entry = _skipped(_import(source, tmp_path, swc_timing(constraint(**kwargs))))
        assert (entry.category, entry.effect, entry.reason) == (NOT_SUPPORTED, ABORT, reason)


class TestABudgetOverTheCap:
    def test_a_factor_of_nineteen_digits(self, source, tmp_path) -> None:
        entry = _skipped(_import(source, tmp_path, swc_timing(constraint(factor="1" + "0" * 18))))
        assert (entry.category, entry.effect) == (NOT_SUPPORTED, ABORT)
        assert entry.reason.startswith("the maximum is larger than a budget holds (18 digits of its unit)")

    def test_a_factor_beyond_what_the_interpreter_converts(self, source, tmp_path) -> None:
        entry = _skipped(_import(source, tmp_path, swc_timing(constraint(factor="9" * 5000))))
        assert (entry.category, entry.effect) == (NOT_SUPPORTED, ABORT)
        assert entry.reason.endswith("the maximum is larger than a budget holds (18 digits of its unit)")

    def test_a_negative_factor_beyond_what_the_interpreter_converts_is_not_positive(self, source, tmp_path) -> None:
        entry = _skipped(_import(source, tmp_path, swc_timing(constraint(factor="-" + "9" * 5000))))
        assert (entry.category, entry.effect, entry.reason) == (
            NOT_SUPPORTED,
            ABORT,
            "the MAXIMUM's CSE-CODE-FACTOR is not positive, and a budget is a positive duration",
        )

    def test_an_eighteen_digit_factor_that_normalises_past_the_cap(self, source, tmp_path) -> None:
        # Code 7 is 10 s: 10**17 of it is 10**18 s, nineteen digits.
        entry = _skipped(_import(source, tmp_path, swc_timing(constraint(code="7", factor="1" + "0" * 17))))
        assert (entry.category, entry.effect) == (NOT_SUPPORTED, ABORT)
        assert entry.reason.startswith("the maximum is larger than a budget holds (18 digits of its unit): ")


class TestTheExecutable:
    def test_an_undeclared_target_is_outside_the_file(self, source, tmp_path) -> None:
        entry = _skipped(_import(source, tmp_path, swc_timing(constraint(executable=f"{BEHAVIOUR}/Elsewhere"))))
        assert (entry.category, entry.effect) == (OUTSIDE_FILE, ABORT)

    def test_another_behaviours_runnable_is_swcrafts(self, source, tmp_path) -> None:
        entry = _skipped(_import(source, tmp_path, swc_timing(constraint(executable=SENSOR_MAIN))))
        assert (entry.category, entry.effect) == (NOT_SUPPORTED, ABORT)
        assert "names no runnable this import carries" in entry.reason

    def test_a_wrong_dest_is_swcrafts(self, source, tmp_path) -> None:
        entry = _skipped(_import(source, tmp_path, swc_timing(constraint(dest="BSW-MODULE-ENTITY"))))
        assert (entry.category, entry.effect) == (NOT_SUPPORTED, ABORT)
        assert "(DEST BSW-MODULE-ENTITY)" in entry.reason


class TestTheWholeTimingIsSkipped:
    @pytest.mark.parametrize(
        ("kwargs", "category", "reason"),
        [
            ({"behavior": None}, NOT_SUPPORTED, "the SWC-TIMING has no BEHAVIOR-REF"),
            ({"behavior": f"{ACTUATOR}/Gone"}, OUTSIDE_FILE, "names a behaviour the file does not declare"),
            ({"behavior": ACTUATOR}, NOT_SUPPORTED, "names no behaviour of a component this import creates"),
            ({"component": "/ComponentTypes/SensorSwc"}, NOT_SUPPORTED, "names another component"),
            ({"component": "/ComponentTypes/Gone"}, OUTSIDE_FILE, "names another component"),
        ],
        ids=["no-behavior-ref", "undeclared", "not-a-behaviour", "other-component", "undeclared-component"],
    )
    def test_once_with_its_constraints_chained(self, source, tmp_path, kwargs, category, reason) -> None:
        imported = _import(source, tmp_path, swc_timing(constraint(), constraint("Aux", executable=AUX), **kwargs))
        assert imported.budget() == (None, None)
        root = _one(imported.at(TIMING, "SWC-TIMING"))
        assert (root.category, root.effect) == (category, ABORT)
        assert reason in root.reason
        chained = [entry for entry in imported.entries if entry.reason.startswith("chain-skipped: SWC-TIMING")]
        assert [(entry.path, entry.category, entry.effect) for entry in chained] == [
            (BUDGET, category, ABORT),
            (f"{TIMING}/Aux", category, ABORT),
        ]

    def test_a_variation_point_on_the_timing(self, source, tmp_path) -> None:
        imported = _import(
            source,
            tmp_path,
            swc_timing(constraint(), tail="<VARIATION-POINT><SHORT-LABEL>v</SHORT-LABEL></VARIATION-POINT>"),
        )
        assert imported.budget() == (None, None)
        root = _one(imported.at(TIMING, "SWC-TIMING"))
        assert (root.category, root.effect) == (NOT_SUPPORTED, ABORT)
        assert "carries a VARIATION-POINT" in root.reason
        assert _one(imported.at(BUDGET)).reason.startswith("chain-skipped: SWC-TIMING 'ActuatorSwc_Timing'")


class TestTwoRequirementsOnOneRunnable:
    def _assert_both_skipped(self, imported: Imported, first: str, second: str) -> None:
        assert imported.budget() == (None, None)
        one, two = _one(imported.at(first)), _one(imported.at(second))
        for entry, other in ((one, second), (two, first)):
            assert (entry.category, entry.effect) == (NOT_SUPPORTED, ABORT)
            assert f"second execution-time requirement ({other})" in entry.reason
        assert imported.entries.index(one) < imported.entries.index(two)

    def test_in_one_timing(self, source, tmp_path) -> None:
        imported = _import(source, tmp_path, swc_timing(constraint("A"), constraint("B", factor="500")))
        self._assert_both_skipped(imported, f"{TIMING}/A", f"{TIMING}/B")

    def test_across_two_timings(self, source, tmp_path) -> None:
        imported = _import(
            source, tmp_path, swc_timing(constraint("A")), swc_timing(constraint("B"), name="Second_Timing")
        )
        self._assert_both_skipped(imported, f"{TIMING}/A", "/ComponentTypes/Second_Timing/B")

    @pytest.mark.parametrize(
        ("ineligible", "reason"),
        [
            ({"type_": None}, "the execution-time constraint has no EXECUTION-TIME-TYPE"),
            ({"code": "23", "factor": "1500"}, "the maximum is written in CSE code 23, which no budget unit holds"),
            (
                {"before": '<TIMING-CONDITION-REF DEST="TIMING-CONDITION">/T/C</TIMING-CONDITION-REF>'},
                "the execution-time constraint carries TIMING-CONDITION-REF",
            ),
        ],
        ids=["no-type", "1500ps", "condition"],
    )
    def test_an_ineligible_requirement_beside_an_eligible_one_is_no_second(
        self, source, tmp_path, ineligible, reason
    ) -> None:
        """Only eligible requirements count: the eligible one is carried, the other skipped with its own reason."""
        imported = _import(source, tmp_path, swc_timing(constraint(), constraint("B", **ineligible)))
        assert imported.budget() == ("400us", "gross")
        assert imported.at(BUDGET) == []
        entry = _one(imported.at(f"{TIMING}/B"))
        assert (entry.category, entry.effect) == (NOT_SUPPORTED, ABORT)
        assert entry.reason.startswith(reason), entry.reason
        assert not [e for e in imported.entries if "second execution-time requirement" in e.reason]


class TestAnotherRequirementKind:
    def test_a_latency_requirement_is_named(self, source, tmp_path) -> None:
        imported = _import(source, tmp_path, swc_timing(constraint("Lat", kind="LATENCY-TIMING-CONSTRAINT")))
        entry = _one(imported.at(f"{TIMING}/Lat"))
        assert (entry.tag, entry.category, entry.effect) == ("LATENCY-TIMING-CONSTRAINT", NOT_SUPPORTED, ABORT)
        assert entry.reason.startswith("a LATENCY-TIMING-CONSTRAINT under TIMING-REQUIREMENTS")


# ---------------------------------------------------------------------------
# Carried, with what is not
# ---------------------------------------------------------------------------


class TestAMinimum:
    def test_the_maximum_is_carried_and_the_minimum_reported(self, source, tmp_path) -> None:
        minimum = "<MINIMUM><CSE-CODE>0</CSE-CODE><CSE-CODE-FACTOR>100</CSE-CODE-FACTOR></MINIMUM>"
        imported = _import(source, tmp_path, swc_timing(constraint(after=minimum)))
        assert imported.budget() == ("400us", "gross")
        assert [(_shape(entry), entry.reason) for entry in imported.timing_entries()] == [
            (
                (BUDGET, "MINIMUM", NOT_SUPPORTED, REPORT),
                "MINIMUM not carried: the model holds a budget's maximum alone",
            )
        ]


class TestDescriptionsConditionsResource:
    def test_a_timing_description_entry(self, source, tmp_path) -> None:
        description = (
            "<TIMING-DESCRIPTIONS><TD-EVENT-SWC-INTERNAL-BEHAVIOR><SHORT-NAME>Start</SHORT-NAME>"
            "</TD-EVENT-SWC-INTERNAL-BEHAVIOR></TIMING-DESCRIPTIONS>"
        )
        imported = _import(source, tmp_path, swc_timing(constraint(), head=description))
        assert imported.budget() == ("400us", "gross")
        assert [_shape(entry) for entry in imported.timing_entries()] == [
            (f"{TIMING}/Start", "TD-EVENT-SWC-INTERNAL-BEHAVIOR", NOT_SUPPORTED, REPORT)
        ]

    def test_a_blank_named_entry_is_keyed_at_the_timing(self, source, tmp_path) -> None:
        """A blank ``SHORT-NAME`` is no name: the line sits at the ``SWC-TIMING``'s path, as a blank constraint's."""
        description = (
            "<TIMING-DESCRIPTIONS><TD-EVENT-SWC-INTERNAL-BEHAVIOR><SHORT-NAME> </SHORT-NAME>"
            "</TD-EVENT-SWC-INTERNAL-BEHAVIOR></TIMING-DESCRIPTIONS>"
        )
        imported = _import(source, tmp_path, swc_timing(constraint(), head=description))
        assert imported.budget() == ("400us", "gross")
        assert [_shape(entry) for entry in imported.timing_entries()] == [
            (TIMING, "TD-EVENT-SWC-INTERNAL-BEHAVIOR", NOT_SUPPORTED, REPORT)
        ]

    def test_a_condition_and_a_resource_at_4_3_0(self, source_4_3_0, tmp_path) -> None:
        head = (
            "<TIMING-CONDITIONS><TIMING-CONDITION><SHORT-NAME>Cond</SHORT-NAME></TIMING-CONDITION></TIMING-CONDITIONS>"
        )
        tail = "<TIMING-RESOURCE><SHORT-NAME>Res</SHORT-NAME></TIMING-RESOURCE>"
        imported = _import(source_4_3_0, tmp_path, swc_timing(constraint(), head=head, tail=tail))
        assert imported.budget() == ("400us", "gross")
        assert sorted(_shape(entry) for entry in imported.timing_entries()) == [
            (f"{TIMING}/Cond", "TIMING-CONDITION", NOT_SUPPORTED, REPORT),
            (f"{TIMING}/Res", "TIMING-RESOURCE", NOT_SUPPORTED, REPORT),
        ]


class TestTraceableChildren:
    def test_desc_admin_data_and_category_on_the_constraint(self, source, tmp_path) -> None:
        before = (
            '<DESC><L-2 L="FOR-ALL">supplier note</L-2></DESC><CATEGORY>VENDOR</CATEGORY>'
            '<ADMIN-DATA><SDGS><SDG GID="X"><SD GID="Y">1</SD></SDG></SDGS></ADMIN-DATA>'
        )
        imported = _import(source, tmp_path, swc_timing(constraint(before=before)))
        assert imported.budget() == ("400us", "gross")
        assert sorted(_shape(entry) for entry in imported.timing_entries()) == [
            (BUDGET, "ADMIN-DATA", NOT_SUPPORTED, REPORT),
            (BUDGET, "CATEGORY", NOT_SUPPORTED, REPORT),
            (BUDGET, "DESC", NOT_SUPPORTED, REPORT),
        ]

    def test_desc_and_category_on_the_timing(self, source, tmp_path) -> None:
        head = '<DESC><L-2 L="FOR-ALL">timing note</L-2></DESC><CATEGORY>VENDOR</CATEGORY>'
        imported = _import(source, tmp_path, swc_timing(constraint(), head=head))
        assert imported.budget() == ("400us", "gross")
        assert sorted(_shape(entry) for entry in imported.timing_entries()) == [
            (TIMING, "CATEGORY", NOT_SUPPORTED, REPORT),
            (TIMING, "DESC", NOT_SUPPORTED, REPORT),
        ]

    def test_a_skipped_constraints_children_are_not_reported(self, source, tmp_path) -> None:
        before = '<DESC><L-2 L="FOR-ALL">note</L-2></DESC>'
        imported = _import(source, tmp_path, swc_timing(constraint(before=before, type_=None)))
        assert [entry.tag for entry in imported.timing_entries()] == ["EXECUTION-TIME-CONSTRAINT"]


class TestALongRunnableName:
    def test_a_budget_on_a_125_character_runnable_is_carried_with_no_line(self, source, tmp_path) -> None:
        long_name = "R" + "x" * 124

        def mutate(root):
            for runnable in root.iter(f"{NS}RUNNABLE-ENTITY"):
                if runnable.findtext(f"{NS}SHORT-NAME") == "ActuatorAux":
                    runnable.find(f"{NS}SHORT-NAME").text = long_name
                    runnable.find(f"{NS}SYMBOL").text = long_name

        imported = _import(
            source,
            tmp_path,
            swc_timing(constraint(f"{long_name}_Budget"[:128], executable=f"{BEHAVIOUR}/{long_name}")),
            mutate=mutate,
        )
        assert imported.budget(long_name) == ("400us", "gross")
        assert [entry for entry in imported.timing_entries() if entry.tag != "SHORT-NAME"] == []

    def test_check_model_then_reports_the_generated_name(self, source, tmp_path) -> None:
        """The import leaves the overflow to ``check-model``."""
        long_name = "R" + "x" * 124

        def mutate(root):
            for runnable in root.iter(f"{NS}RUNNABLE-ENTITY"):
                if runnable.findtext(f"{NS}SHORT-NAME") == "ActuatorAux":
                    runnable.find(f"{NS}SHORT-NAME").text = long_name
                    runnable.find(f"{NS}SYMBOL").text = long_name

        imported = _import(
            source,
            tmp_path,
            swc_timing(constraint(f"{long_name}_Budget"[:128], executable=f"{BEHAVIOUR}/{long_name}")),
            mutate=mutate,
        )
        model_root = imported.swc_root.parent
        records = check_identifier_conformance(str(model_root)).records

        assert [r.rule for r in records if f"{long_name}_Budget" in r.message] == ["identifier.generated"]


class TestWhatTheWriterDerives:
    def test_source_names_are_not_carried(self, source, tmp_path) -> None:
        imported = _import(source, tmp_path, swc_timing(constraint("Main_Req"), name="Foo_TimingReq"))
        assert imported.budget() == ("400us", "gross")
        assert sorted((entry.path, entry.tag, entry.effect, entry.reason) for entry in imported.timing_entries()) == [
            (
                "/ComponentTypes/Foo_TimingReq",
                "SHORT-NAME",
                REPORT,
                "SHORT-NAME of a component's timing (SWC-TIMING) not carried: SWCraft names it itself",
            ),
            (
                "/ComponentTypes/Foo_TimingReq/Main_Req",
                "SHORT-NAME",
                REPORT,
                "SHORT-NAME of an execution-time budget (EXECUTION-TIME-CONSTRAINT) not carried: SWCraft names it "
                "itself",
            ),
        ]

    def test_a_timing_in_another_package(self, source, tmp_path) -> None:
        imported = _import(source, tmp_path, swc_timing(constraint()), package="TimingExtensions")
        assert imported.budget() == ("400us", "gross")
        (entry,) = imported.timing_entries()
        assert _shape(entry) == ("/TimingExtensions/ActuatorSwc_Timing", "SWC-TIMING", NOT_SUPPORTED, REPORT)
        assert entry.reason.startswith("package of the SWC-TIMING not carried")

    def test_two_timings_of_one_component(self, source, tmp_path) -> None:
        imported = _import(
            source,
            tmp_path,
            swc_timing(constraint()),
            swc_timing(
                constraint("ActuatorAux_Budget", executable=AUX, type_="NET", code="3", factor="2"), name="More_Timing"
            ),
        )
        assert imported.budget() == ("400us", "gross")
        assert imported.budget("ActuatorAux") == ("2ms", "net")
        assert sorted(
            (_shape(entry), entry.reason) for entry in imported.timing_entries() if entry.tag == "SWC-TIMING"
        ) == [
            (
                ("/ComponentTypes/ActuatorSwc_Timing", "SWC-TIMING", NOT_SUPPORTED, REPORT),
                "split of a component's budgets over 2 SWC-TIMINGs not carried: SWCraft writes them as one "
                "ActuatorSwc_Timing",
            ),
            (
                ("/ComponentTypes/More_Timing", "SWC-TIMING", NOT_SUPPORTED, REPORT),
                "split of a component's budgets over 2 SWC-TIMINGs not carried: SWCraft writes them as one "
                "ActuatorSwc_Timing",
            ),
        ]


class TestAPackageSelection:
    def test_a_selection_that_leaves_the_budgeted_component_out_records_no_timing_line(
        self, tmp_path_factory, tmp_path
    ) -> None:
        root = _with_aux_runnable(tmp_path_factory.mktemp("packaged") / "model", packages=True)
        base = tmp_path / "base.arxml"
        generate_arxml(str(root), str(base))
        behaviour = "/ComponentTypes/Actuation/ActuatorSwc/ActuatorSwc_InternalBehavior"
        timing = swc_timing(
            constraint("Main_Req", executable=f"{behaviour}/ActuatorMain", code="27", factor="85"),
            name="Foo_TimingReq",
            behavior=behaviour,
        )
        whole = _import(base, tmp_path / "whole", timing)
        assert whole.budget() == ("850ns", "gross")
        assert len(whole.report.represented_lines) == 1
        assert {entry.tag for entry in whole.timing_entries()} == {"SHORT-NAME", "SWC-TIMING"}

        narrowed = _import(base, tmp_path / "narrowed", timing, packages=["Sensing"])
        assert not list(narrowed.swc_root.rglob("ActuatorSwc.yaml"))
        assert narrowed.report.represented_lines == []
        assert narrowed.timing_entries() == []


# ---------------------------------------------------------------------------
# import component
# ---------------------------------------------------------------------------


@pytest.fixture
def actuator_export(model_root: Path, tmp_path: Path) -> Path:
    out = tmp_path / "ActuatorSwc.arxml"
    export_component(str(model_root), "ActuatorSwc", str(out))
    return out


def _component_runnables(project: Path) -> dict[str, dict]:
    (path,) = (project / "model" / "swc").rglob("ActuatorSwc.yaml")
    return {runnable["name"]: runnable for runnable in yaml.safe_load(path.read_text(encoding="utf-8"))["runnables"]}


class TestImportComponent:
    def test_a_carried_budget_is_written_with_its_normalisation(self, actuator_export, tmp_path) -> None:
        patched = with_timings(actuator_export, tmp_path / "in.arxml", swc_timing(constraint(code="27", factor="85")))
        project = init_project(tmp_path / "project")
        report = import_component(str(patched), str(project / "model"))
        assert report.swc_action == "created"
        assert (
            _component_runnables(project)["ActuatorMain"].get("wcet"),
            _component_runnables(project)["ActuatorMain"].get("wcet_type"),
        ) == ("850ns", "gross")
        assert report.represented_lines == [
            f"{BUDGET}: MAXIMUM CSE-CODE 27 with CSE-CODE-FACTOR 85 represented as wcet 850ns"
        ]
        assert report.not_carried_lines == []

    def test_a_guarantee_is_printed_and_the_component_written(self, actuator_export, tmp_path) -> None:
        patched = with_timings(actuator_export, tmp_path / "in.arxml", swc_timing(guarantees=(constraint(),)))
        project = init_project(tmp_path / "project")
        report = import_component(str(patched), str(project / "model"))
        assert report.swc_action == "created"
        assert "wcet" not in _component_runnables(project)["ActuatorMain"]
        assert [line.split(" ", 2)[:2] for line in report.not_carried_lines] == [
            ["EXECUTION-TIME-CONSTRAINT", f"{BUDGET}:"]
        ]

    def test_a_requirement_it_cannot_carry_aborts(self, actuator_export, tmp_path) -> None:
        patched = with_timings(actuator_export, tmp_path / "in.arxml", swc_timing(constraint(type_=None)))
        project = init_project(tmp_path / "project")
        with pytest.raises(LossyComponentImportError) as caught:
            import_component(str(patched), str(project / "model"))
        assert any(line.startswith(f"EXECUTION-TIME-CONSTRAINT {BUDGET}:") for line in caught.value.skip_lines)
        assert not list((project / "model" / "swc").rglob("ActuatorSwc.yaml"))


class TestNothingElseMoves:
    def test_a_file_without_timing_reports_what_it_did(self, source, tmp_path) -> None:
        """The base file's report is unchanged by the reader knowing the tag: no entry names a timing."""
        imported = Imported(source, tmp_path)
        assert imported.timing_entries() == []
        assert Counter(entry.tag for entry in imported.entries)["SWC-TIMING"] == 0


class TestTheTimingNameIsRenamedOnImport:
    """``X_Timing`` beside a component ``X`` the import gives a budget is renamed (phase B).

    ``reconcile.reserved_swc_names``'s ``timing_bases`` -- the components with at least one budgeted
    runnable -- reserve it as ``implementation_bases`` reserve ``_Implementation``; the rename is
    listed in the report's rename lines and the written project builds.
    """

    @staticmethod
    def _source(tmp_path: Path, *, budget: bool) -> Path:
        model_root = _with_aux_runnable(tmp_path / "renamed" / "model")
        extra = model_root / "swc" / "extra" / "ActuatorSwc_Timing.yaml"
        extra.parent.mkdir(parents=True)
        extra.write_text(
            "name: ActuatorSwc_Timing\ntype: application\nhandle_termination_and_restart: no_support\n",
            encoding="utf-8",
            newline="\n",
        )
        base = tmp_path / "base.arxml"
        generate_arxml(str(model_root), str(base))
        if not budget:
            return base
        # The SWC-TIMING in a package of its own, so its SHORT-NAME claims nothing in ComponentTypes.
        return with_timings(base, tmp_path / "source.arxml", swc_timing(constraint(), name="T"), package="TimingExt")

    def test_the_colliding_component_is_renamed_and_listed(self, tmp_path) -> None:
        imported = Imported(self._source(tmp_path, budget=True), tmp_path)

        assert imported.budget() == ("400us", "gross")
        names = {path.stem for path in imported.swc_root.rglob("*.yaml")}
        assert "ActuatorSwc_Timing" not in names and "ActuatorSwc_Timing_app" in names
        assert any("ActuatorSwc_Timing" in line and "SWC-TIMING" in line for line in imported.report.rename_lines)
        generate_arxml(str(imported.swc_root.parent), str(tmp_path / "rebuilt.arxml"))

    def test_without_a_budget_the_name_is_kept(self, tmp_path) -> None:
        imported = Imported(self._source(tmp_path, budget=False), tmp_path)

        assert "ActuatorSwc_Timing" in {path.stem for path in imported.swc_root.rglob("*.yaml")}
        assert not any("SWC-TIMING" in line for line in imported.report.rename_lines)
