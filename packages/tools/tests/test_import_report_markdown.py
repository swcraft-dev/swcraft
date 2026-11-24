"""The import reports' Markdown form against their text form: the same facts, nothing added.

``import component|requirements --report PATH.md`` writes the Markdown form
(``ComponentImportReport.render_markdown()``, ``requirements_import.render_report_markdown()``);
stdout and any other ``--report`` PATH keep the text form. Both forms are spelled from one list
of facts, and these tests hold that from the outside: each form is reduced to its facts by a
reader of its own -- the text by its indentation, the Markdown by its block markers with every
backslash escape and entity resolved -- and the two sequences must be equal, section labels,
counts and names included.

What it does not reach: a Markdown renderer. The reader here resolves the escapes the writer
uses (a backslash before ASCII punctuation, ``&lt;``/``&amp;``); that markdown-it renders
them as the same literal text is the CommonMark rule, not run here. The extension's tab renders
the file with VS Code's own renderer.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from swcraft_tools.component_exchange.report import ComponentImportReport
from swcraft_tools.requirements_import import (
    Changed,
    DeliveryVersion,
    ImportPlan,
    Removed,
    Stub,
    render_report,
    render_report_markdown,
)

# The two readers

_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")


def _literal(markdown: str) -> str:
    """A Markdown inline text as the literal it stands for: escapes and the two entities resolved."""
    return _ESCAPE.sub(r"\1", markdown).replace("&lt;", "<").replace("&amp;", "&")


def _text_facts(text: str) -> list[tuple[str, str]]:
    """The text form's facts: ``("line", ...)``, ``("item", ...)``, ``("detail", ...)``, ``("verbatim", ...)``.

    A heading's trailing colon is the text form's punctuation and is dropped; blank lines carry
    nothing. Under the component's ``Update diff preview:`` every indented line is a verbatim
    line of the preview, whatever it starts with.
    """
    facts: list[tuple[str, str]] = []
    preview = False
    for line in text.splitlines():
        if line == "":
            continue
        if not line.startswith("  "):
            preview = line == "Update diff preview:"
        if preview and line.startswith("  "):
            facts.append(("verbatim", line[2:]))
        elif line.startswith("    "):
            facts.append(("detail", line[4:]))
        elif line.startswith("  - "):
            facts.append(("item", line[4:]))
        elif line.startswith("  "):
            # the component's diff preview, or a requirements entry (two blanks, no dash)
            facts.append(("verbatim", line[2:]))
        else:
            facts.append(("line", line.removesuffix(":")))
    return facts


def _markdown_facts(markdown: str, *, entries: str = "item") -> list[tuple[str, str]]:
    """The Markdown form's facts, by its block markers, every text made literal again.

    *entries* names what a top-level list item is in the text form's terms: ``item`` for the
    component report's ``  - `` lines, ``verbatim`` for the requirements report's two-blank
    entries.
    """
    facts: list[tuple[str, str]] = []
    fence: str | None = None
    for line in markdown.splitlines():
        if fence is not None:
            if line == fence:
                fence = None
            else:
                facts.append(("verbatim", line))
            continue
        if line == "":
            continue
        if re.fullmatch(r"`{3,}", line):
            fence = line
        elif line.startswith("## ") or line.startswith("# "):
            facts.append(("line", _literal(line.split(" ", 1)[1])))
        elif line.startswith("  - "):
            facts.append(("detail", _literal(line[4:])))
        elif line.startswith("- "):
            facts.append((entries, _literal(line[2:])))
        elif line.startswith("**") and line.endswith("**"):
            facts.append(("line", _literal(line[2:-2])))
        else:
            facts.append(("line", _literal(line)))
    return _source_under_the_title(facts)


def _source_under_the_title(facts: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The component title as the text form spells it, from the Markdown's heading and its ``Source:`` line.

    The heading names the source by its file name and the paragraph under it carries the path as
    given: the two are one fact of the text form's title, taken together only when the heading's
    name is that path's file name -- split at ``/`` and ``\\`` independently of the writer -- so a
    heading naming anything else leaves both lines, and the facts differ.
    """
    if len(facts) < 2 or facts[0][0] != "line" or facts[1][0] != "line":
        return facts
    title, under = facts[0][1], facts[1][1]
    prefix = "Component import report: "
    if not (title.startswith(prefix) and under.startswith("Source: ")):
        return facts
    path = under.removeprefix("Source: ")
    if re.split(r"[\\/]", path)[-1] != title.removeprefix(prefix):
        return facts
    return [("line", prefix + path), *facts[2:]]


# The component report's fixtures


def _created() -> ComponentImportReport:
    return ComponentImportReport(
        source="/exports/SpeedSensor.arxml",
        dry_run=True,
        swc="SpeedSensor",
        swc_action="created",
        created_interfaces=["SpeedIf", "StubIf"],
        stub_interfaces=["StubIf"],
        created_data_types=["Speed_T", "uint16"],
        created_elements={"units": ["KmPerH"]},
        created_mapping_sets=["SpeedMap"],
    )


def _reused() -> ComponentImportReport:
    return ComponentImportReport(
        source="SpeedSensor.arxml",
        dry_run=False,
        swc="SpeedSensor",
        swc_action="unchanged",
        reused_interfaces=["SpeedIf"],
        reused_data_types=["Speed_T"],
        reused_elements={"compu_methods": ["SpeedScale"], "data_constraints": ["SpeedRange"]},
        reused_mapping_sets=["SpeedMap"],
        written_paths=[],
    )


def _update() -> ComponentImportReport:
    return ComponentImportReport(
        source="SpeedSensor.arxml",
        dry_run=True,
        swc="SpeedSensor",
        swc_action="updated",
        swc_diff_preview="~ swc/SpeedSensor\n  ports: + Out\n  description: 'a' -> 'b ``x``'",
        preserved_memory={"ram": 128, "rom": 512},
        preserved_asil="B",
        preserved_handle_termination_and_restart="CAN-BE-TERMINATED",
        preserved_descriptions=["port 'In'"],
        preserved_costs={"Run": "50"},
        preserved_measures={"In": {"value": "kept"}},
        dropped_measures=["Old.value"],
        preserved_links={"component": ["SWR-1", "SWR-2"]},
        dropped_links=["port 'Gone': SWR-3"],
        access_list_regime="carried",
        preserved_access_lists={"Run": {"reads": 2, "writes": 1}},
        operation_event_regime="carried",
        preserved_operation_events={"Run": {"events": 1, "activation": True}},
        variable_regime="carried",
        preserved_variables=["V1", "V2"],
        preserved_variable_accesses={"Run": {"reads": 1}},
        preserved_variable_mappings=["VarMap"],
        exclusive_area_regime="carried",
        preserved_exclusive_areas=["EA1"],
        preserved_exclusive_area_references={"Run": {"can_enter": 1}},
        written_exclusive_areas=(1, 2),
        data_event_regime="carried",
        preserved_data_events={"Run": 2},
        extended_mapping_sets={"SpeedMap": ["Speed_T"]},
        extended_mapping_set_components={"SpeedMap": ["Other"]},
        reused_interfaces=["SpeedIf"],
        written_paths=["swc/body/SpeedSensor.yaml"],
    )


def _skipped_and_losses() -> ComponentImportReport:
    return ComponentImportReport(
        source="SpeedSensor.arxml",
        dry_run=True,
        swc="SpeedSensor",
        swc_action="created",
        missing_references=["swc SpeedSensor: runnable Run reads port In element value: no such interface"],
        access_marker_value="v9",
        access_collapse_lines=["Run reads In.value twice"],
        file_suffix_lines=["interfaces/speedif~1.yaml for SpeedIf"],
        package_placement_lines=["SpeedIf placed in /Pkg/Sub"],
        native_declaration_missing_lines=["uint16: no NATIVE-DECLARATION"],
        unread_physics_lines=["Speed_T: unit KmPerH"],
        represented_lines=["Speed_T as a value type"],
        queue_length_lines=["SpeedSensor.Srv: QUEUE-LENGTH 4"],
        descriptions_not_taken_lines=["SpeedIf: description"],
        not_carried_lines=["SpeedSensor: UUID"],
        skip_lines=["/Pkg/Timing: TIMING-EXTENSION (out of scope)"],
    )


_COMPONENT_FIXTURES = {
    "created": _created,
    "reused": _reused,
    "update with a diff preview": _update,
    "skipped elements and losses": _skipped_and_losses,
}


class TestTheComponentReportsTwoFormsCarryTheSameFacts:
    @pytest.mark.parametrize("name", sorted(_COMPONENT_FIXTURES))
    def test_the_facts_are_equal(self, name: str) -> None:
        report = _COMPONENT_FIXTURES[name]()
        text, markdown = report.render(), report.render_markdown()
        assert _markdown_facts(markdown) == _text_facts(text)

    def test_every_count_of_the_text_is_in_the_markdown(self) -> None:
        report = _update()
        counts = re.findall(r"\b\d+\b", report.render())
        assert re.findall(r"\b\d+\b", _literal(report.render_markdown())) == counts


# The component report's Markdown, section by section


class TestTheComponentReportsMarkdown:
    def test_a_created_dry_run(self) -> None:
        assert _created().render_markdown() == (
            "# Component import report\\: SpeedSensor\\.arxml\n"
            "\n"
            "Source\\: \\/exports\\/SpeedSensor\\.arxml\n"
            "\n"
            "(DRY RUN -- no files were written)\n"
            "\n"
            "**SWC SpeedSensor\\: created**\n"
            "\n"
            "## CREATED 2 interface(s)\n"
            "\n"
            "- SpeedIf\n"
            "- StubIf (external interface stub)\n"
            "\n"
            "## REUSED 0 interface(s)\n"
            "\n"
            "## CREATED 2 data type(s)\n"
            "\n"
            "- Speed\\_T\n"
            "- uint16\n"
            "\n"
            "## REUSED 0 data type(s)\n"
            "\n"
            "## CREATED 1 unit(s)\n"
            "\n"
            "- KmPerH\n"
            "\n"
            "## CREATED 1 mapping set(s)\n"
            "\n"
            "- SpeedMap\n"
        )

    def test_the_component_line_is_bold_and_the_dry_run_line_a_paragraph(self) -> None:
        lines = _created().render_markdown().splitlines()
        assert "**SWC SpeedSensor\\: created**" in lines
        assert "(DRY RUN -- no files were written)" in lines
        assert not any(line.startswith("**(DRY") for line in lines)

    def test_an_applied_run_has_no_dry_run_line_and_lists_what_it_wrote(self) -> None:
        markdown = _update().render_markdown()
        applied = ComponentImportReport(
            source="S.arxml", dry_run=False, swc="S", swc_action="created", written_paths=["swc/body/S.yaml"]
        ).render_markdown()
        assert "DRY RUN" not in applied
        assert "## WROTE 1 file(s)\n\n- swc\\/body\\/S\\.yaml\n" in applied
        assert "DRY RUN" in markdown

    def test_the_diff_preview_is_a_fenced_block_no_backtick_of_it_closes(self) -> None:
        markdown = _update().render_markdown()
        assert "## Update diff preview\n\n```\n~ swc/SpeedSensor\n" in markdown
        preview = markdown.split("## Update diff preview\n\n", 1)[1]
        assert preview.startswith("```\n"), "the preview holds a two-backtick run, so three close nothing"
        assert "\n```\n" in preview

    def test_a_longer_backtick_run_gets_a_longer_fence(self) -> None:
        report = ComponentImportReport(
            source="S.arxml", dry_run=True, swc="S", swc_action="updated", swc_diff_preview="a ```` b"
        )
        assert "\n`````\na ```` b\n`````\n" in report.render_markdown()

    def test_each_list_is_a_second_level_heading_without_its_colon(self) -> None:
        markdown = _skipped_and_losses().render_markdown()
        assert "## Skipped out-of-scope elements\n\n- \\/Pkg\\/Timing\\: TIMING-EXTENSION (out of scope)\n" in markdown
        assert "## Collapsed duplicate access points\n" in markdown
        assert not re.search(r"^## .*:$", markdown, re.MULTILINE)
        assert markdown.count("\n# ") == 0 and markdown.startswith("# ")

    def test_the_losses_keep_their_counts_and_sentences(self) -> None:
        markdown = _literal(_skipped_and_losses().render_markdown())
        assert "## References the project does not have (1; the component's runnables" in markdown
        assert "## Not carried from the file (1; descriptive values SWCraft does not carry" in markdown
        assert "## Descriptions not taken from the file (1; not supported by SWCraft" in markdown
        markdown = _literal(_update().render_markdown())
        assert "## MEASURES NOT carried from the target project\n\n- Old.value" in markdown
        assert "## LINKS NOT carried from the target project\n\n- port 'Gone': SWR-3" in markdown


class TestTheComponentReportsMarkdownEdges:
    def test_a_name_with_markdown_metacharacters_is_escaped(self) -> None:
        report = ComponentImportReport(
            source="a_[b](c).arxml#",
            dry_run=True,
            swc="*Bold*_x",
            swc_action="created",
            created_interfaces=["<img src=x>", "# not a heading", "- not a list", "1. not a list", "a|b `c`"],
        )
        markdown = report.render_markdown()
        assert markdown.startswith("# Component import report\\: a\\_\\[b\\](c)\\.arxml\\#\n")
        assert "**SWC \\*Bold\\*\\_x\\: created**" in markdown
        items = markdown.split("## CREATED 5 interface(s)\n\n", 1)[1].split("\n\n", 1)[0].splitlines()
        assert items == [
            "- &lt;img src=x>",
            "- \\# not a heading",
            "- \\- not a list",
            "- 1\\. not a list",
            "- a\\|b \\`c\\`",
        ]
        assert "<img" not in markdown
        assert _markdown_facts(markdown) == _text_facts(report.render())

    def test_an_empty_section_is_its_heading_alone(self) -> None:
        markdown = ComponentImportReport(
            source="S.arxml", dry_run=True, swc="S", swc_action="created"
        ).render_markdown()
        assert "## CREATED 0 interface(s)\n\n## REUSED 0 interface(s)\n" in markdown
        assert "mapping set" not in markdown, "a layer the exchange did not touch stays absent, as in the text"

    def test_the_text_form_is_unchanged(self) -> None:
        """``render()`` byte for byte: the created fixture's text, pinned."""
        assert _created().render() == (
            "Component import report: /exports/SpeedSensor.arxml\n"
            "(DRY RUN -- no files were written)\n"
            "SWC SpeedSensor: created\n"
            "CREATED 2 interface(s)\n"
            "  - SpeedIf\n"
            "  - StubIf (external interface stub)\n"
            "REUSED 0 interface(s)\n"
            "CREATED 2 data type(s)\n"
            "  - Speed_T\n"
            "  - uint16\n"
            "REUSED 0 data type(s)\n"
            "CREATED 1 unit(s)\n"
            "  - KmPerH\n"
            "CREATED 1 mapping set(s)\n"
            "  - SpeedMap"
        )


class TestTheComponentReportsHeadingNamesTheSourceFile:
    """The heading names the source by its file name; the path as given is the paragraph under it."""

    @staticmethod
    def _report(source: str) -> ComponentImportReport:
        return ComponentImportReport(source=source, dry_run=True, swc="S", swc_action="created")

    def test_the_heading_names_the_file_and_the_next_paragraph_the_path(self) -> None:
        report = self._report("/kit/plan-b-walk/BrakeTemperatureSensor.arxml")
        lines = report.render_markdown().splitlines()
        assert lines[:4] == [
            "# Component import report\\: BrakeTemperatureSensor\\.arxml",
            "",
            "Source\\: \\/kit\\/plan-b-walk\\/BrakeTemperatureSensor\\.arxml",
            "",
        ]
        assert (
            report.render().splitlines()[0] == "Component import report: /kit/plan-b-walk/BrakeTemperatureSensor.arxml"
        )
        assert _markdown_facts(report.render_markdown()) == _text_facts(report.render())

    def test_a_relative_path_keeps_its_spelling_on_the_path_line(self) -> None:
        lines = self._report("../exports/S.arxml").render_markdown().splitlines()
        assert lines[0] == "# Component import report\\: S\\.arxml"
        assert lines[2] == "Source\\: \\.\\.\\/exports\\/S\\.arxml"

    def test_markdown_metacharacters_in_the_directory_are_escaped_on_the_path_line(self) -> None:
        report = self._report("/a_[b](c)/*x*/#d/<e>|f/S.arxml")
        lines = report.render_markdown().splitlines()
        assert lines[0] == "# Component import report\\: S\\.arxml"
        assert lines[2] == "Source\\: \\/a\\_\\[b\\](c)\\/\\*x\\*\\/#d\\/&lt;e>\\|f\\/S\\.arxml"
        assert "<e>" not in report.render_markdown()
        assert _markdown_facts(report.render_markdown()) == _text_facts(report.render())

    def test_a_bare_file_name_stands_in_the_heading_alone(self) -> None:
        report = self._report("S.arxml")
        markdown = report.render_markdown()
        assert markdown.startswith("# Component import report\\: S\\.arxml\n\n(DRY RUN")
        assert "Source" not in markdown
        assert _markdown_facts(markdown) == _text_facts(report.render())

    def test_a_windows_path_names_its_file_on_any_host(self) -> None:
        report = self._report("C:\\kit\\plan-b-walk\\BrakeTemperatureSensor.arxml")
        lines = report.render_markdown().splitlines()
        assert lines[0] == "# Component import report\\: BrakeTemperatureSensor\\.arxml"
        assert lines[2] == "Source\\: C\\:\\\\kit\\\\plan-b-walk\\\\BrakeTemperatureSensor\\.arxml"
        assert _markdown_facts(report.render_markdown()) == _text_facts(report.render())

    def test_a_mixed_separator_path_names_its_file(self) -> None:
        assert self._report("C:\\kit/sub\\S.arxml").source_name == "S.arxml"

    def test_a_heading_naming_another_file_is_not_read_as_the_title(self) -> None:
        """The fact reader joins the two lines only when the name is the path's file name."""
        markdown = "# Component import report\\: T\\.arxml\n\nSource\\: \\/x\\/S\\.arxml\n"
        assert _markdown_facts(markdown) == [
            ("line", "Component import report: T.arxml"),
            ("line", "Source: /x/S.arxml"),
        ]


# The requirements report


def _plan(**fields: object) -> ImportPlan:
    empty: dict[str, object] = {
        "model_root": Path("model"),
        "baseline": DeliveryVersion("SRS-1", given=False),
        "ignored_types": (),
        "writes": (),
        "removals": (),
        "added": (),
        "stubs": (),
        "changed": (),
        "removed": (),
        "removed_by_status_not_in_model": (),
        "source_updated": (),
        "parent_versions_updated": (),
        "links_needing_confirmation": (),
        "links_confirmed": (),
        "links_dangling": (),
    }
    return ImportPlan(**{**empty, **fields})  # type: ignore[arg-type]


def _every_section() -> ImportPlan:
    return _plan(
        writes=(),
        removals=("requirements/SWR-9.yaml",),
        added=(("SWR-1", 1), ("SWR-2", 1)),
        stubs=(Stub("SG-1", 1, ("SWR-1", "SWR-2")),),
        changed=(Changed("SWR-3", 2, 3, ("text", "ASIL"), ("text: 10 ms -> 20 ms", "ASIL: A -> B")),),
        removed=(Removed("SWR-9", 4, "status 'rejected'"),),
        removed_by_status_not_in_model=(("SWR-10", "rejected"),),
        source_updated=(("SWR-4", ("revision",)),),
        parent_versions_updated=(("SWR-5", ("SG-1@2",)),),
        links_needing_confirmation=("swc Speed: satisfies SWR-3",),
        links_confirmed=("swc Brake: satisfies SWR-4",),
        links_dangling=("swc Gone: satisfies SWR-9",),
    )


def _mapped_delivery() -> ImportPlan:
    """A delivery read through a mapping that ignored needs, its version given, a returning id and a stub."""
    return _plan(
        baseline=DeliveryVersion("R-2", given=True),
        ignored_types=("story", "epic", "story"),
        writes=(),
        removals=("requirements/SWR-7.yaml",),
        added=(("SWR-1", 1), ("SWR-6", 3)),
        stubs=(Stub("SG-2", 2, ("SWR-1",)),),
        removed=(Removed("SWR-7", 2, "type 'story' not imported"), Removed("SG-3", 1, "stub no longer named")),
        links_dangling=("swc Gone: satisfies SWR-7",),
    )


_REQUIREMENTS_FIXTURES = {
    "every section, applied": (_every_section, False, None),
    "every section, dry run": (_every_section, True, None),
    "no change": (lambda: _plan(), True, None),
    "a mapping, ignored needs, a returning id": (_mapped_delivery, False, "../mappings/needs-mapping.yaml"),
}


class TestTheRequirementsReportsTwoFormsCarryTheSameFacts:
    @pytest.mark.parametrize("name", sorted(_REQUIREMENTS_FIXTURES))
    def test_the_facts_are_equal(self, name: str) -> None:
        make, dry_run, mapping = _REQUIREMENTS_FIXTURES[name]
        plan = make()
        text = render_report(plan, dry_run=dry_run, mapping=mapping)
        markdown = render_report_markdown(plan, dry_run=dry_run, mapping=mapping)
        assert _markdown_facts(markdown, entries="verbatim") == _text_facts(text)


class TestTheRequirementsReportsMarkdown:
    def test_the_summary_is_bold_and_each_section_a_heading_over_its_list(self) -> None:
        markdown = render_report_markdown(_every_section(), dry_run=True)
        assert markdown.startswith(
            "# Requirements import\\: baseline SRS-1 (from the file)\n"
            "\n"
            "Mapping\\: none (SWCraft's own names)\n"
            "\n"
            "Dry run\\: nothing was written\\.\n"
            "\n"
            "**Summary\\: 2 added, 1 changed, 1 removed, 1 stubs created, 1 source updates**\n"
            "\n"
            "## Added\n"
            "\n"
            "- SWR-1 (v1)\n"
            "- SWR-2 (v1)\n"
        )
        assert (
            "## Changed\n\n- SWR-3 v2 → v3 (text, ASIL)\n  - text\\: 10 ms -> 20 ms\n  - ASIL\\: A -> B\n" in markdown
        )
        assert "## Links now naming no requirement\n\n- swc Gone\\: satisfies SWR-9\n" in markdown

    def test_no_change_is_a_paragraph_and_no_section_is_printed(self) -> None:
        markdown = render_report_markdown(_plan(), dry_run=False)
        assert markdown == (
            "# Requirements import\\: baseline SRS-1 (from the file)\n"
            "\n"
            "Mapping\\: none (SWCraft's own names)\n"
            "\n"
            "**Summary\\: 0 added, 0 changed, 0 removed, 0 stubs created, 0 source updates**\n"
            "\n"
            "No change\\.\n"
        )

    def test_the_heading_names_the_baseline_and_no_source_line_follows(self) -> None:
        """The requirements title names no source path, so its Markdown heading is the title whole."""
        markdown = render_report_markdown(_every_section(), dry_run=False)
        assert markdown.splitlines()[:5] == [
            "# Requirements import\\: baseline SRS-1 (from the file)",
            "",
            "Mapping\\: none (SWCraft's own names)",
            "",
            "**Summary\\: 2 added, 1 changed, 1 removed, 1 stubs created, 1 source updates**",
        ]
        assert "Source\\:" not in markdown

    def test_an_id_with_metacharacters_and_a_control_character_is_escaped(self) -> None:
        plan = _plan(baseline=DeliveryVersion("*v1*\n# x", given=False), added=(("[SWR](http://e.com)", 1), ("<b>", 1)))
        markdown = render_report_markdown(plan, dry_run=False)
        assert markdown.startswith("# Requirements import\\: baseline \\*v1\\*\\\\n\\# x (from the file)\n")
        assert "- \\[SWR\\](http\\:\\/\\/e\\.com) (v1)\n- &lt;b> (v1)\n" in markdown
        assert _markdown_facts(markdown, entries="verbatim") == _text_facts(render_report(plan, dry_run=False))

    def test_the_mapping_and_the_needs_not_imported_are_paragraphs_under_the_heading(self) -> None:
        markdown = render_report_markdown(_mapped_delivery(), dry_run=False, mapping="../mappings/needs-mapping.yaml")
        assert markdown.splitlines()[:7] == [
            "# Requirements import\\: baseline R-2",
            "",
            "Mapping\\: \\.\\.\\/mappings\\/needs-mapping\\.yaml",
            "",
            "Not imported\\: 3 needs of types the mapping ignores (epic, story)",
            "",
            "**Summary\\: 2 added, 0 changed, 2 removed, 1 stubs created, 0 source updates**",
        ]
        assert "- SWR-6 (v3; links from before its removal stay unconfirmed)\n" in markdown
