"""The build report's facts and text (``build_report.py``).

Every sentence is asserted through the module's own constant, never a copy of
it, so a reworded sentence moves one place. The ARXML texts are written by
hand, never by the writer, so the comparison is not checked against the code it
reads.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from build_report_support import PASSED, report_section
from conftest import _core, _memory, _task, _write_manifest
from conftest import _swc as _component
from swcraft_tools import build_report as br
from swcraft_tools import exclusive_create
from swcraft_tools.arxml_elements import ArxmlElementChange
from swcraft_tools.arxml_validation import MODE_STRUCTURAL, MODE_XSD, ValidationOutcome, check_arxml
from swcraft_tools.project import require_project
from swcraft_tools.resource_report import byte_figure
from swcraft_tools.validator import ConsistencyIssue, ConsistencyResult
from swcraft_tools.writer.arxml_xml import previous_arxml_path as writer_previous_arxml_path
from swcraft_tools.writer.diagrams.page import INDEX_PAGE
from symlink_support import symlink_or_skip

_HEAD = '<?xml version="1.0" encoding="UTF-8"?>\n'
_NS = "http://autosar.org/schema/r4.0"
_XSI = "http://www.w3.org/2001/XMLSchema-instance"


def _root(location: str | None) -> str:
    if location is None:
        return f'<AUTOSAR xmlns="{_NS}">'
    return f'<AUTOSAR xmlns="{_NS}" xmlns:xsi="{_XSI}" xsi:schemaLocation="{location}">'


def _arxml(elements: str, location: str | None = f"{_NS} AUTOSAR_4-2-2.xsd") -> str:
    return (
        f"{_HEAD}{_root(location)}<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>Components</SHORT-NAME>"
        f"<ELEMENTS>{elements}</ELEMENTS></AR-PACKAGE></AR-PACKAGES></AUTOSAR>"
    )


def _swc(name: str, extra: str = "") -> str:
    return f"<APPLICATION-SW-COMPONENT-TYPE><SHORT-NAME>{name}</SHORT-NAME>{extra}</APPLICATION-SW-COMPONENT-TYPE>"


def _project(tmp_path: Path, data: dict | None = None):
    manifest = _write_manifest(tmp_path / "project", data)
    (manifest.parent / "model").mkdir(exist_ok=True)
    return require_project(str(manifest))


def _write(path: Path, text: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8", newline="\n")


def _result(*warnings: tuple[str, str]) -> ConsistencyResult:
    return ConsistencyResult(
        consistent=True, warnings=tuple(ConsistencyIssue(rule, message) for rule, message in warnings)
    )


def _built(tmp_path: Path, outcome: ValidationOutcome = PASSED, result: ConsistencyResult | None = None, data=None):
    project = _project(tmp_path, data)
    _write(Path(project.resolved_arxml_path), _arxml(_swc("ExampleSWC")))
    return project, br.compute_build_report(project, result or _result(), outcome)


# The verdict


class TestVerdict:
    def test_xsd_passed_names_the_schema_by_its_file_name_alone(self, tmp_path):
        _, report = _built(tmp_path)
        assert report.verdict == br.VERDICT_XSD_PASSED.format(schema="`AUTOSAR_4-2-2.xsd`")
        assert "/opt/licensed" not in br.render_markdown(report)

    def test_structural_passed_names_the_configured_schema_and_is_not_the_xsd_sentence(self, tmp_path):
        _, report = _built(tmp_path, ValidationOutcome(True, MODE_STRUCTURAL))
        assert report.verdict == br.VERDICT_STRUCTURAL_PASSED.format(schema="`AUTOSAR_4-2-2.xsd`")
        assert report.verdict != br.VERDICT_XSD_PASSED.format(schema="`AUTOSAR_4-2-2.xsd`")
        assert "structural check only" in report.verdict

    def test_structural_passed_says_what_the_run_knows_in_validate_s_own_words(self, tmp_path):
        # A structural pass follows from this run's schema configuration (an unset, missing or
        # mismatched SWCRAFT_AUTOSAR_XSD_PATH), not from the licensed file being absent
        # everywhere: the sentence reuses `swcraft validate`'s own note and claims no absence.
        _, report = _built(tmp_path, ValidationOutcome(True, MODE_STRUCTURAL))
        assert "XSD validation is skipped (requires the licensed `AUTOSAR_4-2-2.xsd` schema)" in report.verdict
        assert "absent" not in report.verdict

    def test_structural_passed_follows_the_project_version(self, tmp_path):
        data = {"name": "p", "arxml_version": "4.3.0"}
        _, report = _built(tmp_path, ValidationOutcome(True, MODE_STRUCTURAL), data=data)
        assert report.verdict == br.VERDICT_STRUCTURAL_PASSED.format(schema="`AUTOSAR_4-3-0.xsd`")
        assert report.arxml_version == "4.3.0"

    @pytest.mark.parametrize("mode", [MODE_XSD, MODE_STRUCTURAL])
    def test_invalid_names_the_mode_and_the_arxml_left_on_disk(self, tmp_path, mode):
        _, report = _built(tmp_path, ValidationOutcome(False, mode, messages=("/abs/dist/model.arxml:1: bad",)))
        assert report.verdict == br.VERDICT_FAILED.format(mode=mode, arxml="`dist/model.arxml`")
        assert "/abs/" not in br.render_markdown(report)

    def test_unusable_schema_is_a_toolchain_problem(self, tmp_path):
        outcome = ValidationOutcome(False, MODE_XSD, schema_path="/opt/x/AUTOSAR_4-2-2.xsd", schema_error=True)
        _, report = _built(tmp_path, outcome)
        assert report.verdict == br.VERDICT_NOT_VALIDATED.format(mode=MODE_XSD, schema="`AUTOSAR_4-2-2.xsd`")
        # The mode is named here too, as in every other verdict.
        assert f"the {MODE_XSD} check" in report.verdict

    def test_the_warning_count_follows_the_sentence(self, tmp_path):
        for warnings, tail in (
            ((), br.NO_WARNING),
            ((("r.one", "m"),), br.WARNING_COUNT.format(count=1)),
            ((("r.one", "m"), ("r.two", "n")), br.WARNING_COUNT.format(count=2)),
        ):
            _, report = _built(tmp_path, result=_result(*warnings))
            assert report_section(br.render_markdown(report), br.HEADING_VERDICT) == [f"{report.verdict} {tail}"]
        assert br.WARNING_COUNT.format(count=2) == "2 warning(s)."
        assert br.NO_WARNING == "No warning."


# What was built


class TestOutputs:
    def test_absent_diagrams_and_needs(self, tmp_path):
        _, report = _built(tmp_path)
        assert report.outputs == (
            br.BuiltOutput(br.OUTPUT_ARXML, "dist/model.arxml", br.STATE_WRITTEN.format(version="4.2.2")),
            br.BuiltOutput(br.OUTPUT_DIAGRAMS, "views/index.html", br.STATE_ABSENT),
            br.BuiltOutput(br.OUTPUT_NEEDS, "dist/needs.json", br.STATE_ABSENT),
        )

    def test_present_diagrams_and_needs_json(self, tmp_path):
        project = _project(tmp_path)
        root = Path(project.resolved_project_root)
        _write(root / "views" / "index.html", "<html></html>")
        _write(root / "dist" / "needs.json", "{}\n")
        _write(Path(project.resolved_arxml_path), _arxml(_swc("A")))
        report = br.compute_build_report(project, _result(), PASSED)
        assert [output.state for output in report.outputs[1:]] == [br.STATE_ON_DISK, br.STATE_ON_DISK]

    def test_a_directory_where_the_needs_json_is_named_is_absent(self, tmp_path):
        """On disk means the file: a directory of that name is no ``needs.json`` (the retired export was a folder)."""
        project = _project(tmp_path)
        (Path(project.resolved_needs_path) / "x").mkdir(parents=True)
        _write(Path(project.resolved_arxml_path), _arxml(_swc("A")))
        report = br.compute_build_report(project, _result(), PASSED)
        assert report.outputs[2].state == br.STATE_ABSENT

    def test_a_file_outside_the_project_is_named_by_its_file_name(self, tmp_path):
        outside = tmp_path / "elsewhere" / "needs.json"
        project = _project(tmp_path)
        # The loader refuses an escaping path; a project whose path resolves outside another way (a
        # symlink, a hand-built object) is named by its file alone.
        project.outputs.needs = str(outside)
        _write(Path(project.resolved_arxml_path), _arxml(_swc("A")))
        report = br.compute_build_report(project, _result(), PASSED)
        assert report.outputs[2].path == "needs.json"
        assert str(tmp_path) not in br.render_markdown(report)

    def test_a_project_reached_through_a_symbolic_link_is_still_named_inside_it(self, tmp_path):
        project = _project(tmp_path)
        link = tmp_path / "linked"
        symlink_or_skip(link, Path(project.resolved_project_root))
        # The output as stored, the root as reached through the link: the two spellings differ.
        output = Path(project.resolved_project_root).resolve() / "dist" / "needs.json"
        assert br._project_relative(output, str(link)) == "dist/needs.json"

    def test_the_table_and_the_note(self, tmp_path):
        _, report = _built(tmp_path)
        assert report_section(br.render_markdown(report), br.HEADING_BUILT) == [
            "| Output | File | State |",
            "| --- | --- | --- |",
            "| ARXML | `dist/model.arxml` | written by this run, AUTOSAR 4.2.2 |",
            "| Diagrams | `views/index.html` | absent |",
            "| Sphinx-Needs | `dist/needs.json` | absent |",
            br.BUILT_NOTE,
        ]


class TestPreviousName:
    def test_derived_from_the_manifest_arxml(self, tmp_path):
        data = {"name": "p", "outputs": {"arxml": "dist/sub/generated.arxml", "needs": "dist/needs.json"}}
        project, report = _built(tmp_path, data=data)
        assert report.arxml.sentence == br.ARXML_NO_PREVIOUS.format(old="`dist/sub/generated_old.arxml`")

    def test_the_one_line_equals_the_writer_s(self, tmp_path):
        for name in ("model.arxml", "generated.arxml", "a.b.arxml", "noext"):
            assert br.previous_arxml_path(tmp_path / name) == writer_previous_arxml_path(tmp_path / name)


class TestTheGeneratorsNames:
    """The re-spelled page name equals the generator's own, as :func:`previous_arxml_path` does the writer's."""

    def test_the_diagrams_page_is_the_one_generate_diagrams_writes(self):
        assert br.DIAGRAMS_PAGE == INDEX_PAGE


# Findings and resources


class TestFindings:
    def test_the_table_in_the_run_s_order_with_a_message_made_safe(self, tmp_path):
        result = _result(("rule.b", "first | one\nline <b>"), ("rule.a", "second"))
        _, report = _built(tmp_path, result=result)
        assert report.findings == (("rule.b", "first | one\nline <b>"), ("rule.a", "second"))
        assert report_section(br.render_markdown(report), br.HEADING_FINDINGS) == [
            "| Rule | Finding |",
            "| --- | --- |",
            "| `rule.b` | first \\| one line &lt;b> |",
            "| `rule.a` | second |",
        ]

    def test_a_message_s_inline_markdown_and_linkable_names_are_escaped(self, tmp_path):
        result = _result(("rule.a", "Requirement '**X**' names example.com and `a_b` [x](y) a@b.c"))
        _, report = _built(tmp_path, result=result)
        assert report_section(br.render_markdown(report), br.HEADING_FINDINGS)[2] == (
            "| `rule.a` | Requirement '\\*\\*X\\*\\*' names example\\.com and \\`a\\_b\\` \\[x\\](y) a\\@b\\.c |"
        )

    def test_no_warning_is_one_sentence(self, tmp_path):
        _, report = _built(tmp_path)
        assert report_section(br.render_markdown(report), br.HEADING_FINDINGS) == [br.NO_FINDINGS]


class TestResources:
    def test_a_core_and_a_memory_with_a_load_every_figure_in_its_column(self, tmp_path):
        # A load on both, so no two figures of a row are equal and a swapped column fails.
        project = _project(tmp_path)
        model = project.resolved_model_root
        _core(model, "CORE0", threshold=0.7)
        _task(model, "Task_10ms", period=0.01, wcet="1200us")
        _memory(model, "RAM", size=24576, threshold=0.75, accessible=["CORE0"])
        _component(model, "Brake", memory={"RAM": 14336})
        _write(Path(project.resolved_arxml_path), _arxml(_swc("A")))
        report = br.compute_build_report(project, _result(), PASSED)
        assert report.resources == (
            br.ResourceRow("CORE0", "core", "12.00%", "70.00%", "58.00%", "88.00%", "within budget"),
            br.ResourceRow("RAM", "memory", "14336 bytes", "18432 bytes", "4096 bytes", "10240 bytes", "within budget"),
        )
        assert report_section(br.render_markdown(report), br.HEADING_RESOURCES) == [
            "| Resource | Kind | Used | Limit | Headroom | Physical headroom | Status |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            "| `CORE0` | core | 12.00% | 70.00% | 58.00% | 88.00% | within budget |",
            "| `RAM` | memory | 14336 bytes | 18432 bytes | 4096 bytes | 10240 bytes | within budget |",
        ]

    def test_a_fractional_budget_reads_whole_and_the_status_is_the_exact_comparison_s(self, tmp_path):
        project = _project(tmp_path)
        model = project.resolved_model_root
        _core(model, "CORE0", threshold=0.7)
        _memory(model, "RAM", size=1000, threshold=0.7005, accessible=["CORE0"])
        _component(model, "Brake", memory={"RAM": 700})
        _write(Path(project.resolved_arxml_path), _arxml(_swc("A")))
        (_, memory) = br.compute_build_report(project, _result(), PASSED).resources
        assert memory == br.ResourceRow(
            "RAM", "memory", "700 bytes", "700 bytes", "0 bytes", "300 bytes", "within budget"
        )

    def test_one_byte_is_singular_as_the_resource_report_spells_it(self, tmp_path):
        project = _project(tmp_path)
        model = project.resolved_model_root
        _core(model, "CORE0", threshold=0.7)
        _memory(model, "RAM", size=10, threshold=0.5, accessible=["CORE0"])
        _component(model, "Brake", memory={"RAM": 4})
        _write(Path(project.resolved_arxml_path), _arxml(_swc("A")))
        (_, memory) = br.compute_build_report(project, _result(), PASSED).resources
        assert (memory.used, memory.limit, memory.headroom, memory.physical_headroom) == (
            "4 bytes",
            "5 bytes",
            "1 byte",
            "6 bytes",
        )

    def test_the_unit_is_the_resource_report_s_own_spelling(self):
        assert [byte_figure(figure) for figure in ("0", "1", "2", "1024")] == [
            "0 bytes",
            "1 byte",
            "2 bytes",
            "1024 bytes",
        ]

    def test_no_core_and_no_memory_is_one_sentence(self, tmp_path):
        _, report = _built(tmp_path)
        assert report.resources == ()
        assert report_section(br.render_markdown(report), br.HEADING_RESOURCES) == [br.NO_RESOURCES]


# The ARXML changes


def _with_previous(tmp_path: Path, previous: str | bytes, current: str):
    project = _project(tmp_path)
    _write(Path(project.resolved_arxml_path), current)
    _write(br.previous_arxml_path(project.resolved_arxml_path), previous)
    return br.compute_build_report(project, _result(), PASSED)


class TestArxmlChanges:
    def test_no_previous_arxml(self, tmp_path):
        _, report = _built(tmp_path)
        assert report.arxml == br.ArxmlSection(br.ARXML_NO_PREVIOUS.format(old="`dist/model_old.arxml`"), None)

    def test_a_previous_file_that_is_no_xml_document(self, tmp_path):
        report = _with_previous(tmp_path, "not xml at all", _arxml(_swc("A")))
        assert report.arxml == br.ArxmlSection(br.ARXML_NOT_XML.format(old="`dist/model_old.arxml`"), None)

    def test_a_previous_file_that_cannot_be_read_is_not_compared_and_the_report_is_written(self, tmp_path, monkeypatch):
        project = _project(tmp_path)
        _write(Path(project.resolved_arxml_path), _arxml(_swc("A")))
        previous = br.previous_arxml_path(project.resolved_arxml_path)
        _write(previous, _arxml(_swc("A")))
        read_bytes = Path.read_bytes

        def refuse(path: Path) -> bytes:
            if path == previous:
                raise PermissionError(13, "Permission denied")
            return read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", refuse)
        report = br.compute_build_report(project, _result(), PASSED)
        assert report.arxml == br.ArxmlSection(br.ARXML_NOT_READ.format(old="`dist/model_old.arxml`"), None)

    def test_a_previous_file_whose_stat_is_refused_is_not_compared_and_the_report_is_written(
        self, tmp_path, monkeypatch
    ):
        # An inaccessible link target or directory on the way: Python 3.11's is_file() raised here.
        project = _project(tmp_path)
        _write(Path(project.resolved_arxml_path), _arxml(_swc("A")))
        previous = br.previous_arxml_path(project.resolved_arxml_path)
        stat = Path.stat

        def refuse(path: Path, *args, **kwargs):
            if path == previous:
                raise PermissionError(13, "Permission denied")
            return stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", refuse)
        report = br.compute_build_report(project, _result(), PASSED)
        assert report.arxml == br.ArxmlSection(br.ARXML_NOT_READ.format(old="`dist/model_old.arxml`"), None)

    def test_a_dangling_link_at_the_previous_name_is_no_previous_arxml(self, tmp_path):
        project = _project(tmp_path)
        _write(Path(project.resolved_arxml_path), _arxml(_swc("A")))
        symlink_or_skip(br.previous_arxml_path(project.resolved_arxml_path), tmp_path / "nowhere.arxml")
        report = br.compute_build_report(project, _result(), PASSED)
        assert report.arxml == br.ArxmlSection(br.ARXML_NO_PREVIOUS.format(old="`dist/model_old.arxml`"), None)

    def test_a_previous_file_in_another_encoding_than_it_declares_is_not_compared(self, tmp_path):
        latin1 = _arxml(_swc("A", "<DESC>café</DESC>")).encode("latin-1")
        report = _with_previous(tmp_path, latin1, _arxml(_swc("A")))
        assert report.arxml == br.ArxmlSection(br.ARXML_NOT_XML.format(old="`dist/model_old.arxml`"), None)

    def test_a_previous_file_lxml_cannot_canonicalize_is_not_compared(self, tmp_path):
        # A relative namespace URI parses but fails C14N, on the previous file's side alone.
        odd = _arxml(_swc("A")).replace(
            "<APPLICATION-SW-COMPONENT-TYPE>", '<APPLICATION-SW-COMPONENT-TYPE xmlns:a="rel">'
        )
        report = _with_previous(tmp_path, odd, _arxml(_swc("A")))
        assert report.arxml == br.ArxmlSection(br.ARXML_NOT_CANONICAL.format(old="`dist/model_old.arxml`"), None)

    def test_a_generated_file_that_is_no_xml_document_is_not_compared_and_the_report_is_written(self, tmp_path):
        # check_arxml answers a malformed file with an invalid outcome, never an exception, so the
        # report of that failed run must still be computed, its verdict carrying the failure.
        project = _project(tmp_path)
        current = Path(project.resolved_arxml_path)
        _write(current, "<AUTOSAR><unclosed>")
        _write(br.previous_arxml_path(current), _arxml(_swc("A")))
        outcome = check_arxml(str(current))
        assert (outcome.valid, outcome.mode) == (False, MODE_STRUCTURAL)
        report = br.compute_build_report(project, _result(), outcome)
        assert report.verdict == br.VERDICT_FAILED.format(mode=MODE_STRUCTURAL, arxml="`dist/model.arxml`")
        assert report.arxml == br.ArxmlSection(br.ARXML_NOT_XML.format(old="`dist/model.arxml`"), None)
        assert report_section(br.render_markdown(report), br.HEADING_ARXML) == [report.arxml.sentence]

    def test_no_element_changed_with_other_blank_text(self, tmp_path):
        report = _with_previous(tmp_path, _arxml(_swc("A")).replace("<ELEMENTS>", "<ELEMENTS>\n  "), _arxml(_swc("A")))
        assert report.arxml == br.ArxmlSection(br.ARXML_UNCHANGED.format(old="`dist/model_old.arxml`"), None)
        assert report_section(br.render_markdown(report), br.HEADING_ARXML) == [report.arxml.sentence]

    def test_the_counts_and_the_table(self, tmp_path):
        previous = _arxml(_swc("Kept") + _swc("Edited") + _swc("Gone"))
        current = _arxml(_swc("New") + _swc("Kept") + _swc("Edited", "<CATEGORY>X</CATEGORY>"))
        report = _with_previous(tmp_path, previous, current)
        assert report.arxml.changes == (
            ArxmlElementChange("Components/New", "APPLICATION-SW-COMPONENT-TYPE", "added"),
            ArxmlElementChange("Components/Edited", "APPLICATION-SW-COMPONENT-TYPE", "changed"),
            ArxmlElementChange("Components/Gone", "APPLICATION-SW-COMPONENT-TYPE", "removed"),
        )
        sentence = br.ARXML_CHANGED.format(count=3, old="`dist/model_old.arxml`", added=1, removed=1, changed=1)
        assert report_section(br.render_markdown(report), br.HEADING_ARXML) == [
            sentence,
            "| Element | Type | Change |",
            "| --- | --- | --- |",
            "| `Components/New` | APPLICATION-SW-COMPONENT-TYPE | added |",
            "| `Components/Edited` | APPLICATION-SW-COMPONENT-TYPE | changed |",
            "| `Components/Gone` | APPLICATION-SW-COMPONENT-TYPE | removed |",
        ]

    def test_another_schema_is_compared_and_named(self, tmp_path):
        old_location, new_location = f"{_NS} AUTOSAR_4-3-0.xsd", f"{_NS} AUTOSAR_4-2-2.xsd"
        report = _with_previous(tmp_path, _arxml(_swc("A"), old_location), _arxml(_swc("A"), new_location))
        assert report.arxml.changes == ()
        assert report.arxml.schema_sentence == br.ARXML_SCHEMAS_DIFFER.format(
            old_location=f"`{old_location}`", new_location=f"`{new_location}`"
        )
        assert report_section(br.render_markdown(report), br.HEADING_ARXML) == [
            br.ARXML_UNCHANGED.format(old="`dist/model_old.arxml`"),
            report.arxml.schema_sentence,
        ]

    def test_a_schema_location_names_no_directory(self, tmp_path):
        stored = f"{_NS} file:///home/someone/schemas/AUTOSAR_4-3-0.xsd"
        local = f"{_NS} C:\\schemas\\AUTOSAR_4-2-2.xsd"
        report = _with_previous(tmp_path, _arxml(_swc("A"), stored), _arxml(_swc("A"), local))
        assert report.arxml.schema_sentence == br.ARXML_SCHEMAS_DIFFER.format(
            old_location=f"`{_NS} AUTOSAR_4-3-0.xsd`", new_location=f"`{_NS} AUTOSAR_4-2-2.xsd`"
        )
        assert "someone" not in br.render_markdown(report)

    def test_a_file_uri_in_a_namespace_place_and_a_lone_token_name_no_directory(self, tmp_path):
        stored = f"file:///home/someone/ns {_NS}/AUTOSAR_4-3-0.xsd"
        lone = "/home/someone/schemas/AUTOSAR_4-2-2.xsd"
        report = _with_previous(tmp_path, _arxml(_swc("A"), stored), _arxml(_swc("A"), lone))
        assert report.arxml.schema_sentence == br.ARXML_SCHEMAS_DIFFER.format(
            old_location="`ns AUTOSAR_4-3-0.xsd`", new_location="`AUTOSAR_4-2-2.xsd`"
        )
        assert "someone" not in br.render_markdown(report)

    def test_an_http_or_urn_namespace_is_kept_as_written(self, tmp_path):
        old_location, new_location = f"{_NS} AUTOSAR_4-3-0.xsd", "urn:example:ns AUTOSAR_4-2-2.xsd"
        report = _with_previous(tmp_path, _arxml(_swc("A"), old_location), _arxml(_swc("A"), new_location))
        assert report.arxml.schema_sentence == br.ARXML_SCHEMAS_DIFFER.format(
            old_location=f"`{old_location}`", new_location=f"`{new_location}`"
        )

    def test_schema_locations_differing_in_a_directory_alone_are_not_named(self, tmp_path):
        old_location, new_location = f"{_NS} old/AUTOSAR_4-2-2.xsd", f"{_NS} AUTOSAR_4-2-2.xsd"
        report = _with_previous(tmp_path, _arxml(_swc("A"), old_location), _arxml(_swc("A"), new_location))
        assert report.arxml.schema_sentence is None

    def test_a_root_without_a_schema_location(self, tmp_path):
        report = _with_previous(tmp_path, _arxml(_swc("A"), None), _arxml(_swc("A")))
        assert report.arxml.schema_sentence == br.ARXML_SCHEMAS_DIFFER.format(
            old_location=br.NO_SCHEMA_LOCATION, new_location=f"`{_NS} AUTOSAR_4-2-2.xsd`"
        )


# The whole document


class TestDocument:
    def test_the_title_the_five_sections_in_order_and_the_footer(self, tmp_path):
        _, report = _built(tmp_path)
        lines = br.render_markdown(report).splitlines()
        assert lines[:3] == ["# Build Report", "", "test-project · AUTOSAR 4.2.2"]
        headings = [line for line in lines if line.startswith("## ")]
        assert headings == ["## Verdict", "## What was built", "## Findings", "## Resources", "## ARXML changes"]
        assert lines[-3:] == ["---", "", br.FOOTER]
        assert [br.HEADING_VERDICT, br.HEADING_BUILT, br.HEADING_FINDINGS, br.HEADING_RESOURCES, br.HEADING_ARXML] == [
            heading[3:] for heading in headings
        ]

    def test_deterministic_and_names_no_location(self, tmp_path):
        project, report = _built(tmp_path, ValidationOutcome(False, MODE_XSD))
        first, second = br.render_markdown(report), br.render_markdown(report)
        assert first == second
        assert first == br.render_markdown(
            br.compute_build_report(project, _result(), ValidationOutcome(False, MODE_XSD))
        )
        assert str(tmp_path) not in first
        assert tmp_path.name not in first
        assert project.resolved_project_root not in first

    def test_the_project_name_is_kept_on_one_line(self, tmp_path):
        _, report = _built(tmp_path, data={"name": "Two\nlines <x>"})
        assert br.render_markdown(report).splitlines()[2] == "Two lines &lt;x> · AUTOSAR 4.2.2"

    @pytest.mark.parametrize(
        ("name", "subtitle"),
        [
            ("# X", "\\# X"),
            ("- X", "\\- X"),
            ("> X", "\\> X"),
            ("1. X", "1\\. X"),
            ("12) X", "12\\) X"),
            ("    X", "X"),
            ("A-1. B", "A-1\\. B"),
            ("* X", "\\* X"),
            ("[docs](https://example.com)", "\\[docs\\](https\\:\\/\\/example\\.com)"),
            # A bare URL, domain, protocol-relative host or address: markdown-it's linkify finds none of them.
            ("https://example.com/x", "https\\:\\/\\/example\\.com\\/x"),
            ("Brake example.com", "Brake example\\.com"),
            ("//host", "\\/\\/host"),
            ("a@b.com", "a\\@b\\.com"),
            ("a_b *c* `d` ~e~", "a\\_b \\*c\\* \\`d\\` \\~e\\~"),
        ],
    )
    def test_a_project_name_opening_a_block_stays_the_subtitle_s_paragraph(self, tmp_path, name, subtitle):
        _, report = _built(tmp_path, data={"name": name})
        assert br.render_markdown(report).splitlines()[2] == f"{subtitle} · AUTOSAR 4.2.2"

    def test_a_pipe_in_a_sentence_s_code_span_is_not_escaped(self, tmp_path):
        # Outside a table GFM keeps a backslash before a pipe in a code span, so a sentence's span keeps the pipe.
        # No file is written: Windows refuses `|` in a file name. The ARXML section only stats the previous
        # file, and that stat answers "absent" on every host: Windows' ERROR_INVALID_NAME is mapped by CPython
        # to ENOENT (a mkdir probe of the same name answers "refused", so no filesystem_support branch fits).
        data = {"name": "p", "outputs": {"arxml": "dist/a|b.arxml", "needs": "dist/needs.json"}}
        project = _project(tmp_path, data)
        report = br.compute_build_report(project, _result(), ValidationOutcome(False, MODE_XSD))
        assert report.verdict == br.VERDICT_FAILED.format(mode=MODE_XSD, arxml="`dist/a|b.arxml`")
        assert report.arxml.sentence == br.ARXML_NO_PREVIOUS.format(old="`dist/a|b_old.arxml`")
        assert "| ARXML | `dist/a\\|b.arxml` |" in br.render_markdown(report)
        assert not (tmp_path / "project" / "dist").exists(), "nothing was written under dist_root"


# Writing


class TestWrite:
    def test_written_whole_into_dist_root_and_no_temporary_file_left(self, tmp_path):
        project, report = _built(tmp_path)
        target = Path(project.resolved_dist_root) / br.BUILD_REPORT_FILE
        _write(target, "an older, longer report\n" * 50)
        written = br.write_build_report(project, report)
        assert written == target
        assert target.read_text(encoding="utf-8") == br.render_markdown(report)
        assert sorted(path.name for path in target.parent.iterdir()) == [br.BUILD_REPORT_FILE, "model.arxml"]

    def test_an_arxml_output_at_the_report_s_path_is_refused_and_left_as_it_is(self, tmp_path):
        data = {"name": "p", "outputs": {"arxml": f"dist/{br.BUILD_REPORT_FILE}", "needs": "dist/needs.json"}}
        project, report = _built(tmp_path, data=data)
        arxml = Path(project.resolved_arxml_path)
        before = arxml.read_bytes()
        with pytest.raises(OSError) as refused:
            br.write_build_report(project, report)
        assert refused.value.strerror == "the manifest names the ARXML output at the report's path"
        assert str(tmp_path) not in refused.value.strerror
        assert arxml.read_bytes() == before
        assert sorted(path.name for path in arxml.parent.iterdir()) == [br.BUILD_REPORT_FILE]

    def test_a_failed_replace_removes_the_temporary_file_and_leaves_the_report(self, tmp_path, monkeypatch):
        project, report = _built(tmp_path)
        target = Path(project.resolved_dist_root) / br.BUILD_REPORT_FILE
        _write(target, "the earlier report\n")

        def refuse(source, destination):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(os, "replace", refuse)
        with pytest.raises(PermissionError):
            br.write_build_report(project, report)
        assert sorted(path.name for path in target.parent.iterdir()) == [br.BUILD_REPORT_FILE, "model.arxml"]
        assert target.read_text(encoding="utf-8") == "the earlier report\n"

    def test_a_failed_write_removes_the_temporary_file_it_began_and_leaves_the_report(self, tmp_path, monkeypatch):
        project, report = _built(tmp_path)
        target = Path(project.resolved_dist_root) / br.BUILD_REPORT_FILE
        _write(target, "the earlier report\n")
        begun: list[Path] = []

        class HalfWritten:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                self.handle.close()

            def close(self) -> None:
                self.handle.close()

            def write(self, text: str) -> int:
                self.handle.write(text[:11])
                raise OSError(28, "No space left on device")

        # The temporary file is created and opened by the shared helper (`exclusive_create.open_new`).
        real_create_new = exclusive_create.create_new

        def creating(path, *args, **kwargs):
            begun.append(Path(path))
            return real_create_new(path, *args, **kwargs)

        def opening(descriptor, *args, **kwargs):
            return HalfWritten(open(descriptor, *args, **kwargs))

        monkeypatch.setattr(exclusive_create, "create_new", creating)
        monkeypatch.setattr(exclusive_create, "open", opening, raising=False)
        with pytest.raises(OSError, match="No space left"):
            br.write_build_report(project, report)
        assert [path.parent for path in begun] == [target.parent]
        assert begun[0].name.startswith(f"{br.BUILD_REPORT_FILE}.") and begun[0].suffix == ".tmp"
        assert sorted(path.name for path in target.parent.iterdir()) == [br.BUILD_REPORT_FILE, "model.arxml"]
        assert target.read_text(encoding="utf-8") == "the earlier report\n"

    def test_a_file_or_link_planted_at_the_temporary_name_is_never_written_through(self, tmp_path, monkeypatch):
        project, report = _built(tmp_path)
        target = Path(project.resolved_dist_root) / br.BUILD_REPORT_FILE
        outside = tmp_path / "outside.txt"
        _write(outside, "not the report's\n")
        first, second = b"\x01" * 8, b"\x02" * 8
        names = iter([first, second, b"\x03" * 8])
        monkeypatch.setattr(br.os, "urandom", lambda size: next(names))
        planted_file = target.with_name(f"{target.name}.{first.hex()}.tmp")
        _write(planted_file, "planted\n")
        planted_link = target.with_name(f"{target.name}.{second.hex()}.tmp")
        symlink_or_skip(planted_link, outside)
        assert br.write_build_report(project, report) == target
        assert target.read_text(encoding="utf-8") == br.render_markdown(report)
        assert outside.read_text(encoding="utf-8") == "not the report's\n"
        assert planted_file.read_text(encoding="utf-8") == "planted\n"
        assert planted_link.is_symlink()

    def test_every_name_taken_lets_the_last_collision_raise(self, tmp_path, monkeypatch):
        project, report = _built(tmp_path)
        target = Path(project.resolved_dist_root) / br.BUILD_REPORT_FILE
        taken = b"\x01" * 8
        monkeypatch.setattr(br.os, "urandom", lambda size: taken)
        _write(target.with_name(f"{target.name}.{taken.hex()}.tmp"), "planted\n")
        with pytest.raises(FileExistsError):
            br.write_build_report(project, report)
        assert not target.exists()

    def test_a_failing_cleanup_does_not_mask_the_error_that_caused_it(self, tmp_path, monkeypatch):
        project, report = _built(tmp_path)

        def refuse(source, destination):
            raise PermissionError(13, "Permission denied")

        def cannot_remove(path: Path, *args, **kwargs):
            raise OSError(16, "Device or resource busy")

        monkeypatch.setattr(os, "replace", refuse)
        monkeypatch.setattr(Path, "unlink", cannot_remove)
        with pytest.raises(PermissionError, match="Permission denied"):
            br.write_build_report(project, report)
