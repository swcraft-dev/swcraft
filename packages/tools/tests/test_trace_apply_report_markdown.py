"""Trace Apply's report in two forms, and ``experiment trace --apply FILE --report PATH``.

``trace_apply.render_report`` (stdout, a ``--report`` PATH not ending in ``.md``) and
``trace_apply.render_report_markdown`` (a PATH ending in ``.md``: the report document with tables) are
spelled from one list of facts, ``_report_parts``. These tests hold that from the outside: every section
the text form prints has its table in the Markdown, one row per printed entry, naming the same
requirement; the counts of the Markdown's *Result* table are the ``Summary:`` line's. The text form keeps
its bytes. The CLI half: the file written after the run, the refusals before the pin, and a write that
fails after the apply.

What it does not reach: a Markdown renderer (the tests read the escaped text back; the
extension's tab renders the file with VS Code's own renderer), and the panel, whose parsers read
stdout, never this file.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT
from swcraft_tools import cli, trace_apply
from swcraft_tools.trace_apply import render_report, render_report_markdown, run_apply, run_time_text
from test_import_report_markdown import _literal
from trace_models import OUT, STEP, TOP, ApplyProject, apply_model, confirming, tree

#: The run's time in these tests: a fixed local time one hour east of UTC.
NOW = datetime(2026, 11, 9, 13, 6, 41, tzinfo=timezone(timedelta(hours=1)))
WHEN = "2026-11-09 13:06 (UTC+01:00)"

#: The text form's section title and the Markdown form's, for a dry run and for an applied run.
SECTIONS = {
    "Links to write": ("Links to write", "Links written"),
    "Links to confirm": ("Links to confirm", "Links confirmed"),
    "Rejections to record": ("Rejections to record", "Rejections recorded"),
    "Already done": ("Already done", "Already done"),
    "Stale": ("Stale (skipped)", "Stale (skipped)"),
    "Allocated levels raised": ("Allocated levels raised", "Allocated levels raised"),
    "Links not written (edit by hand)": ("Not written (edit by hand)", "Not written (edit by hand)"),
}


class _FixedClock(datetime):
    """``datetime.now()`` answering *NOW*: what the default clock converts, in :class:`TestTheRunsClock`."""

    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        return NOW


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run's clock, fixed at the seam.

    The CLI's run reads it, and the tests compare its file with a run's own. The seam returns *NOW* as it is, so no case
    depends on the host's time zone."""
    monkeypatch.setattr(trace_apply, "_clock", lambda: NOW)


@pytest.fixture
def project(tmp_path: Path) -> ApplyProject:
    """A project whose proposals file accepts one port link, rejects one and confirms one."""
    project = ApplyProject(tmp_path, apply_model())
    project.write(confirming(project, {"SR-2": [(OUT, "accept"), (TOP, "reject")]}))
    return project


def _stale(project: ApplyProject) -> ApplyProject:
    project.write(project.propose({"SR-2": [(TOP, "accept")]}))
    (project.root / "requirements" / "SR-2.yaml").unlink()
    return project


def _done(project: ApplyProject) -> ApplyProject:
    run_apply(project.project, project.proposals, dry_run=False)
    return project


def _rich(tmp_path: Path) -> ApplyProject:
    """Every section at once.

    A write, a confirm, a reject, one already done, one stale, a raised level, one not written."""
    model = apply_model()
    model.get("requirements/SR-2.yaml")["asil"] = "C"
    project = ApplyProject(tmp_path, model)
    document = confirming(
        project,
        {"SR-2": [(OUT, "accept"), (TOP, "accept"), (STEP, "reject")], "SR-3": [(OUT, "accept"), (TOP, "reject")]},
    )
    project.write(document)
    # After the search: SR-3 changes (both its items stale), and Top's list becomes a flow list over several
    # lines, a form Apply does not edit (SR-2 on Top not written).
    project.edit("requirements/SR-3.yaml", lambda d: d.update(description="The lamp state shall be logged"))
    (project.root / "compositions" / "Top.yaml").write_text(
        "title: Top\nname: Top\ndescription: the top composition\nsatisfies: [\n  SR-9@1]\n",
        encoding="utf-8",
        newline="\n",
    )
    return project


def _text_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        if line.endswith(":") and not line.startswith(" ") and line[:-1] in SECTIONS:
            current = sections.setdefault(line[:-1], [])
        elif line.startswith("  ") and current is not None:
            current.append(line[2:])
        elif line == "":
            current = None
    return sections


def _cells(line: str) -> list[str]:
    return [_literal(cell.strip()) for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def _markdown_tables(markdown: str) -> dict[str, list[list[str]]]:
    """Each ``##`` section's table: its header row first, then its rows, every cell made literal again."""
    tables: dict[str, list[list[str]]] = {}
    current: list[list[str]] | None = None
    for line in markdown.splitlines():
        if line.startswith("## "):
            current = tables.setdefault(line[3:], [])
        elif line.startswith("| ") and current is not None and not line.startswith("| ---"):
            current.append(_cells(line))
    return tables


# ---------------------------------------------------------------------------
# The two forms
# ---------------------------------------------------------------------------


class TestTheTwoFormsCarryTheSameFacts:
    @pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "applied"])
    @pytest.mark.parametrize("shape", ["every-kind", "stale", "nothing-to-do", "rich"])
    def test_every_printed_entry_has_its_row(
        self, project: ApplyProject, tmp_path: Path, shape: str, dry_run: bool
    ) -> None:
        if shape == "stale":
            _stale(project)
        elif shape == "nothing-to-do":
            _done(project)
        elif shape == "rich":
            project = _rich(tmp_path / "rich")
        plan = project.plan()
        text_sections = _text_sections(render_report(plan, dry_run=dry_run))
        tables = _markdown_tables(render_report_markdown(plan, dry_run=dry_run, now=NOW))
        shown = {SECTIONS[title][0 if dry_run else 1] for title in text_sections}
        assert set(tables) == {"Result"} | shown
        for title, entries in text_sections.items():
            rows = tables[SECTIONS[title][0 if dry_run else 1]][1:]
            assert len(rows) == len(entries), title
            for entry, row in zip(entries, rows, strict=True):
                # the requirement (or, for a raised level, the component) the printed entry names
                assert row[1 if title != "Allocated levels raised" else 0] in entry, (title, entry, row)

    @pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "applied"])
    def test_the_result_table_holds_the_summary_lines_counts(self, tmp_path: Path, dry_run: bool) -> None:
        plan = _rich(tmp_path).plan()
        summary = re.search(r"^Summary: (.*)$", render_report(plan, dry_run=dry_run), re.M)
        assert summary is not None
        counts = [int(each) for each in re.findall(r"\d+", summary.group(1))]
        result = _markdown_tables(render_report_markdown(plan, dry_run=dry_run, now=NOW))["Result"]
        assert [int(row[1]) for row in result[1:]] == counts

    def test_the_run_hands_back_both_forms_of_one_plan(self, project: ApplyProject) -> None:
        run = run_apply(project.project, project.proposals, dry_run=True, markdown=True)
        assert run.report == render_report(run.plan, dry_run=True)
        assert run.markdown == render_report_markdown(run.plan, dry_run=True, now=NOW)

    def test_the_run_reads_its_own_clock_unless_given_a_time(self, project: ApplyProject) -> None:
        later = NOW + timedelta(days=1)
        run = run_apply(project.project, project.proposals, dry_run=True, now=later, markdown=True)
        assert run.markdown is not None
        assert "Preview made 2026-11-10 13:06 (UTC+01:00) — nothing was written." in run.markdown

    def test_without_the_markdown_form_neither_the_clock_nor_the_renderer_is_reached(
        self, project: ApplyProject, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("reached without the Markdown form asked for")

        monkeypatch.setattr(trace_apply, "_clock", refuse)
        monkeypatch.setattr(trace_apply, "render_report_markdown", refuse)
        run = run_apply(project.project, project.proposals, dry_run=False)
        assert run.markdown is None
        assert run.written


def test_the_extensions_heading_constant_is_the_reports_own_title_prefix(project: ApplyProject) -> None:
    """The extension's heading constant is the report's own title prefix.

    The tab cuts ``TRACE_APPLY_MARKDOWN_HEADING`` off the CLI's file (``tabReportMarkdown``) and tells a Markdown report
    by it (``isMarkdownImportReport``); a title changed on this side alone would make the tab fall back to the fenced
    text with both suites green. The constant is read from ``commandCore.ts`` and held to the rendered title."""
    source = (REPO_ROOT / "vscode_extension" / "src" / "commandCore.ts").read_text(encoding="utf-8")
    declared = re.search(r'^export const TRACE_APPLY_MARKDOWN_HEADING = "([^"]*)";$', source, re.M)
    assert declared is not None, "commandCore.ts declares no TRACE_APPLY_MARKDOWN_HEADING"
    for dry_run in (True, False):
        markdown = render_report_markdown(project.plan(), dry_run=dry_run, now=NOW)
        assert markdown.startswith(declared.group(1)), markdown.splitlines()[0]


class TestTheRunsClock:
    """The default clock, the one thing the autouse seam replaces: the local time with its offset."""

    def test_the_default_clock_converts_to_the_local_offset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.undo()  # the real `_clock`, over a `datetime.now()` fixed at NOW
        monkeypatch.setattr(trace_apply, "datetime", _FixedClock)
        local = trace_apply._clock()
        assert local == NOW  # the same instant
        assert local.utcoffset() == NOW.astimezone().utcoffset()  # in the host's zone, whatever it is
        assert run_time_text(local) == run_time_text(NOW.astimezone())


class TestTheMarkdownForm:
    def test_the_preview(self, project: ApplyProject) -> None:
        plan = project.plan()
        markdown = render_report_markdown(plan, dry_run=True, now=NOW)
        lines = markdown.splitlines()
        assert lines[:5] == [
            "# Trace Apply Report — search 'trace'",
            "",
            f"Preview made {WHEN} — nothing was written.",
            "",
            # the fixture's proposals carry a placeholder model digest
            "The model has changed since the search.",
        ]
        assert markdown.count("Trace Apply Report") == 1
        assert [line for line in lines if line.startswith("## ")] == [
            "## Result",
            "## Links to write",
            "## Links to confirm",
            "## Rejections to record",
        ]
        tables = _markdown_tables(markdown)
        assert tables["Result"] == [
            ["Result", "Count"],
            ["Links to write", "1"],
            ["Links to confirm", "1"],
            ["Rejections to record", "1"],
            ["Already done", "0"],
            ["Stale (skipped)", "0"],
            ["Not written (edit by hand)", "0"],
            ["Still undecided", "0"],
            ["Files to change", "1"],
        ]
        assert tables["Links to write"] == [
            ["Element (kind)", "Requirement", "Version"],
            ["pOut (port of SWC Ctl)", "SR-2", "1"],
        ]
        assert tables["Links to confirm"] == [
            ["Element (kind)", "Requirement", "Version"],
            ["Ctl (SWC)", "SR-1", "1 → 2"],
        ]
        assert tables["Rejections to record"] == [["Element (kind)", "Requirement"], ["Top (Composition)", "SR-2"]]
        assert lines[-1] == f"<small>Plan digest, for --expect-plan: {plan.digest}</small>"
        assert "Summary:" not in markdown and "No change." not in markdown
        assert markdown.endswith("\n") and not markdown.endswith("\n\n")

    def test_the_applied_run_in_its_own_words(self, project: ApplyProject) -> None:
        markdown = render_report_markdown(project.plan(), dry_run=False, now=NOW)
        assert f"\nApplied {WHEN}.\n" in markdown and "Preview" not in markdown
        assert [line for line in markdown.splitlines() if line.startswith("## ")] == [
            "## Result",
            "## Links written",
            "## Links confirmed",
            "## Rejections recorded",
        ]
        labels = [row[0] for row in _markdown_tables(markdown)["Result"][1:]]
        assert labels == [
            "Links written",
            "Links confirmed",
            "Rejections recorded",
            "Already done",
            "Stale (skipped)",
            "Not written (edit by hand)",
            "Still undecided",
            "Files changed",
        ]

    def test_each_reasoned_section_and_the_raised_levels(self, tmp_path: Path) -> None:
        tables = _markdown_tables(render_report_markdown(_rich(tmp_path).plan(), dry_run=True, now=NOW))
        assert tables["Stale (skipped)"] == [
            ["Element (kind)", "Requirement", "Reason"],
            ["pOut (port of SWC Ctl)", "SR-3", "SR-3 changed since the search (same version); search again"],
            ["Top (Composition)", "SR-3", "SR-3 changed since the search (same version); search again"],
        ]
        assert tables["Not written (edit by hand)"] == [
            ["Element (kind)", "Requirement", "Reason"],
            [
                "Top (Composition)",
                "SR-2",
                "its satisfies list is a flow list over several lines, a form Apply does not edit; add SR-2@1 by hand",
            ],
        ]
        assert tables["Allocated levels raised"] == [
            ["Component", "Allocated level before", "After", "Declared"],
            ["Ctl", "QM", "C", "—"],
        ]

    def test_already_done_and_no_change(self, project: ApplyProject) -> None:
        markdown = render_report_markdown(_done(project).plan(), dry_run=False, now=NOW)
        assert "\nNo change.\n" in markdown
        assert [line for line in markdown.splitlines() if line.startswith("## ")] == ["## Result", "## Already done"]
        assert _markdown_tables(markdown)["Already done"][0] == ["Element (kind)", "Requirement", "Reason"]

    def test_a_declared_level_is_named(self, tmp_path: Path) -> None:
        model = apply_model()
        model.get("requirements/SR-1.yaml")["asil"] = "B"
        model.get("requirements/SR-2.yaml")["asil"] = "D"
        model.get("swc/app/Ctl.yaml")["asil"] = "C"
        project = ApplyProject(tmp_path, model)
        plan = project.plan(project.propose({"SR-2": [(OUT, "accept")]}))
        rows = _markdown_tables(render_report_markdown(plan, dry_run=True, now=NOW))["Allocated levels raised"]
        assert rows[1:] == [["Ctl", "B", "D", "C"]]

    def test_a_value_is_literal_markdown(self, tmp_path: Path) -> None:
        """A value taken from the model is literal Markdown, a table's ``|`` included.

        The odd identifier is put on the plan, not in the project: Windows refuses ``*`` and ``|`` in a file
        name, and a requirement's storage name is its identifier.
        """
        model = apply_model()
        model.requirement("R-ODD", "Odd", "An odd identifier")
        project = ApplyProject(tmp_path, model)
        plan = project.plan(project.propose({"R-ODD": [(TOP, "accept")]}))
        replace = trace_apply.dataclasses.replace
        plan = replace(plan, items=tuple(replace(item, requirement="*R|1*") for item in plan.items))
        markdown = render_report_markdown(plan, dry_run=True, now=NOW)
        assert "| Top (Composition) | \\*R\\|1\\* | 1 |" in markdown
        assert _markdown_tables(markdown)["Links to write"][1] == ["Top (Composition)", "*R|1*", "1"]

    def test_the_search_name_is_escaped_in_the_title(self, project: ApplyProject) -> None:
        plan = project.plan()
        odd = trace_apply.dataclasses.replace(plan, name="a_b #1")
        first = render_report_markdown(odd, dry_run=True, now=NOW).splitlines()[0]
        assert first == "# Trace Apply Report — search 'a\\_b \\#1'"

    @pytest.mark.parametrize(
        ("now", "text"),
        [
            (NOW, WHEN),
            (datetime(2026, 1, 2, 3, 4, tzinfo=UTC), "2026-01-02 03:04 (UTC+00:00)"),
            (
                datetime(2026, 1, 2, 3, 4, tzinfo=timezone(-timedelta(hours=5, minutes=30))),
                "2026-01-02 03:04 (UTC-05:30)",
            ),
        ],
        ids=["east", "utc", "west"],
    )
    def test_the_time_is_local_with_its_offset(self, now: datetime, text: str) -> None:
        assert run_time_text(now) == text

    @pytest.mark.parametrize(
        ("member", "text"),
        [("REQ@2", "2"), ("REQ", "none"), ("REQ@x", "x"), (None, "none")],
        ids=["versioned", "bare", "malformed", "absent"],
    )
    def test_a_confirms_version_cell_names_the_suffix_never_the_identifier(self, member: str | None, text: str):
        # A confirm can replace a bare or a malformed member the element still holds; the text form prints it as
        # it stands, and the Version cell must not print the identifier as a version.
        assert trace_apply._version_text(member) == text

    def test_a_time_without_an_offset_is_refused(self) -> None:
        with pytest.raises(ValueError, match="UTC offset"):
            run_time_text(datetime(2026, 1, 2, 3, 4))  # noqa: DTZ001 - the naive time is the point

    def test_the_text_form_keeps_its_bytes(self, project: ApplyProject) -> None:
        # The text form, spelled out: the shared facts structure moves no byte of it.
        plan = project.plan()
        text = render_report(plan, dry_run=True)
        assert text.splitlines()[:4] == [
            "Trace apply: proposals of the search 'trace' (the model has changed since the search)",
            "Dry run: nothing was written.",
            "Summary: 1 written, 1 confirmed, 1 rejected, 0 already done, 0 stale, 0 not written, 0 undecided; 1 files",
            f"Plan: {plan.digest}",
        ]
        assert "\n\nLinks to write:\n  " in text


# ---------------------------------------------------------------------------
# The CLI: `--report PATH` on `--apply`
# ---------------------------------------------------------------------------


def _run(argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    monkeypatch.setattr("sys.argv", ["swcraft", "experiment", *argv])
    try:
        cli.main()
        code = 0
    except SystemExit as exc:
        code = int(exc.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _argv(project: ApplyProject, *extra: str) -> list[str]:
    return ["trace", "--apply", str(project.proposals), "--project", str(project.manifest), *extra]


def _pin(manifest: Path) -> None:
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["toolchain_version"] = "9.9.9"
    manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline="\n")


class TestTheReportFile:
    @pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "applied"])
    def test_a_md_path_gets_the_markdown_form(
        self, project: ApplyProject, tmp_path: Path, monkeypatch, capsys, dry_run: bool
    ) -> None:
        expected = run_apply(project.project, project.proposals, dry_run=True, markdown=True)
        report = tmp_path / "apply-report.MD"
        flags = ["--dry-run"] if dry_run else []
        code, out, err = _run(_argv(project, *flags, "--report", str(report)), monkeypatch, capsys)
        assert code == 0, err
        written = report.read_text(encoding="utf-8")
        assert out == (expected.report if dry_run else expected.report.replace("Dry run: nothing was written.\n", ""))
        assert written == render_report_markdown(expected.plan, dry_run=dry_run, now=NOW)
        assert f"Preview made {WHEN}" in written if dry_run else f"Applied {WHEN}." in written
        assert report.read_bytes().count(b"\r") == 0
        assert (project.dir / "trace-decisions.yaml").is_file() is not dry_run

    def test_any_other_path_gets_the_printed_text(self, project: ApplyProject, tmp_path: Path, monkeypatch, capsys):
        report = tmp_path / "apply-report.txt"
        code, out, err = _run(_argv(project, "--dry-run", "--report", str(report)), monkeypatch, capsys)
        assert code == 0, err
        assert report.read_text(encoding="utf-8") == out

    def test_format_json_writes_the_printed_document_to_any_other_path(
        self, project: ApplyProject, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        report = tmp_path / "apply-report.json"
        argv = _argv(project, "--dry-run", "--format", "json", "--report", str(report))
        code, out, err = _run(argv, monkeypatch, capsys)
        assert code == 0, err
        assert out.startswith("{")
        assert report.read_text(encoding="utf-8") == out

    def test_format_json_prints_the_document_and_writes_the_report(
        self, project: ApplyProject, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        expected = run_apply(project.project, project.proposals, dry_run=True, markdown=True)
        report = tmp_path / "apply-report.md"
        argv = _argv(project, "--dry-run", "--format", "json", "--report", str(report))
        code, out, err = _run(argv, monkeypatch, capsys)
        assert code == 0, err
        assert out.startswith("{")
        assert report.read_text(encoding="utf-8") == expected.markdown

    @pytest.mark.parametrize(
        "argv",
        [
            ["trace", "--report", "r.md"],
            ["trace", "--documents", "--report", "r.md"],
            ["allocation", "--report", "r.md"],
        ],
        ids=["search", "documents", "allocation"],
    )
    def test_report_without_apply_is_refused_by_name_before_the_pin(
        self, project: ApplyProject, monkeypatch, capsys, argv: list[str]
    ) -> None:
        _pin(project.manifest)
        before = tree(project.dir)
        code, out, err = _run([*argv, "--project", str(project.manifest)], monkeypatch, capsys)
        assert (code, out, err) == (1, "", "Error: --report applies only to --apply.\n")
        assert tree(project.dir) == before

    def test_the_apply_only_refusals_come_first(self, project: ApplyProject, monkeypatch, capsys) -> None:
        argv = ["trace", "--dry-run", "--report", "r.md", "--project", str(project.manifest)]
        assert _run(argv, monkeypatch, capsys)[2] == "Error: --dry-run applies only to --apply.\n"

    def test_a_missing_directory_is_refused_before_the_pin_and_the_run(
        self, project: ApplyProject, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        _pin(project.manifest)
        before = tree(project.dir)
        report = tmp_path / "absent" / "r.md"
        code, out, err = _run(_argv(project, "--report", str(report)), monkeypatch, capsys)
        assert (code, out) == (1, "")
        assert err == f"Error: --report {report}: the directory {report.parent} does not exist.\n"
        assert tree(project.dir) == before

    @pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "applied"])
    def test_a_write_that_fails_after_the_run_says_the_run_was_done(
        self, project: ApplyProject, tmp_path: Path, monkeypatch, capsys, dry_run: bool
    ) -> None:
        report = tmp_path / "r.md"
        real_write_text = Path.write_text

        def write_text(self: Path, *args: Any, **kwargs: Any) -> int:
            if self == report:
                raise OSError(28, "No space left on device")
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", write_text)
        flags = ["--dry-run"] if dry_run else []
        code, out, err = _run(_argv(project, *flags, "--report", str(report)), monkeypatch, capsys)
        monkeypatch.undo()
        assert code == 1
        assert out.startswith("Trace apply: proposals of the search 'trace'")
        done = "the dry run completed" if dry_run else "the decisions were applied"
        assert err == (
            f"Error: {done} and its report printed, but --report {report} could not be written "
            "(No space left on device).\n"
        )
        assert (project.dir / "trace-decisions.yaml").is_file() is not dry_run
        assert not report.exists()

    def test_a_refused_apply_writes_no_report(self, project: ApplyProject, tmp_path: Path, monkeypatch, capsys):
        report = tmp_path / "r.md"
        code, _out, err = _run(
            _argv(project, "--expect-plan", "sha256:0", "--report", str(report)), monkeypatch, capsys
        )
        assert code == 1 and err.startswith("Error: the plan changed")
        assert not report.exists()
