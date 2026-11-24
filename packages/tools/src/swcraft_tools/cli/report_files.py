"""Report files and the generated-report steps of the build and read commands."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from swcraft_tools import project as project_module
from swcraft_tools.arxml_validation import ValidationOutcome
from swcraft_tools.cli.messages import CONTROL_CHARACTERS, path_free_reason
from swcraft_tools.project import SwcraftProject
from swcraft_tools.requirements_import import visible
from swcraft_tools.resource_report import MARKDOWN_FILE, REPORT_DIRECTORY, FitQuestion, ResourceReport, report_markdown
from swcraft_tools.toolchain_pin import toolchain_pin_problem as toolchain_pin_problem
from swcraft_tools.validator import ConsistencyResult


def is_markdown_report(path: str) -> bool:
    """Whether a ``--report PATH`` asks for the report's Markdown form: it ends in ``.md``, any case.

    Any other PATH gets what the run printed. ``import model`` always writes its text form.
    """
    return path.lower().endswith(".md")


def refuse_missing_report_directory(args: argparse.Namespace) -> None:
    """The refusal, exit 1, of an import or an Apply whose ``--report`` file's directory does not exist.

    Checked before anything is written, so an applied run never ends in exit 1 over its report.
    """
    if args.report and not Path(args.report).parent.is_dir():
        print(
            f"Error: --report {args.report}: the directory {Path(args.report).parent} does not exist.", file=sys.stderr
        )
        sys.exit(1)


def wants_markdown_report(args: argparse.Namespace) -> bool:
    """Whether the run's ``--report`` PATH is given and asks for the Markdown form (:func:`is_markdown_report`)."""
    return bool(args.report) and is_markdown_report(args.report)


def wanted_markdown_report(args: argparse.Namespace, render: Callable[[], str]) -> str | None:
    """The Markdown form of a run's report, rendered by *render*, when :func:`wants_markdown_report`."""
    return render() if wants_markdown_report(args) else None


def write_report_file(args: argparse.Namespace, text: str, markdown: str | None, done: str) -> None:
    """A run's report written to its ``--report`` file, after the run and its printed report.

    *markdown* when the PATH asked for it, else *text*. A failed write exits 1 with a sentence saying
    the run itself completed (*done*).
    """
    if not args.report:
        return
    try:
        if markdown is not None:
            # A Markdown document, so `\n` on every platform.
            Path(args.report).write_text(markdown, encoding="utf-8", newline="\n")
        else:
            # A transcript of what was printed, so the platform's line ending.
            Path(args.report).write_text(text, encoding="utf-8", newline=None)
    except OSError as exc:
        print(
            f"Error: {done} and its report printed, but --report {args.report} could not be "
            f"written ({exc.strerror or exc}).",
            file=sys.stderr,
        )
        sys.exit(1)


def print_generated_report(path: Path | str, stream: TextIO | None = None) -> None:
    """The ``Generated report: <path>`` line the extension previews, or a warning for a control character.

    The extension opens the path on the last such line; *stream* defaults to stdout. A path holding a
    control character (configured roots are free text) could forge a second line, so it is not
    printed: one path-free ``Warning:`` instead, the exit code unchanged.
    """
    text = str(path)
    if not CONTROL_CHARACTERS.isdisjoint(text):
        print(
            "Warning: the report was written, but its path holds a control character, so it is not printed",
            file=sys.stderr,
        )
        return
    print(f"Generated report: {text}", file=stream)


def build_report_step(
    args: argparse.Namespace,
    project: SwcraftProject,
    result: ConsistencyResult,
    outcome: ValidationOutcome,
) -> None:
    """``validate --report``'s one step: write the build report, print where, and never move the exit code.

    Called right after ``check_arxml`` returns, so the report is written whatever the outcome, and its
    line precedes the run's final line. Any failure is one path-free ``Warning:`` line.
    """
    if not args.report:
        return
    try:
        from swcraft_tools.build_report import compute_build_report, write_build_report  # noqa: PLC0415 -- start-up: the build report loads for `validate --report` alone

        path = write_build_report(project, compute_build_report(project, result, outcome))
    except Exception as exc:  # noqa: BLE001 -- secondary: an unwritten build report is a warning, never the build's failure
        print(f"Warning: the build report was not written: {path_free_reason(exc)}", file=sys.stderr)
        return
    print_generated_report(path)


def resource_report_step(
    args: argparse.Namespace, project: SwcraftProject, report: ResourceReport, fit: FitQuestion | None
) -> None:
    """``resource-report --report``'s one step: :func:`report_file_step` with this command's file."""
    if not args.report:
        return

    report_file_step(
        args,
        project,
        "the resource report",
        REPORT_DIRECTORY,
        MARKDOWN_FILE,
        lambda: report_markdown(report, project.name, fit),
    )


def report_file_step(
    args: argparse.Namespace,
    project: SwcraftProject,
    subject: str,
    relative: str,
    file_name: str,
    markdown: Callable[[], str],
) -> None:
    """A read-only command's ``--report`` step: write ``<dist_root>/<relative>/<file_name>``, print where.

    Runs after stdout is written, never moves the exit code and reads nothing of the model. The
    ``Generated report:`` line goes to stderr under ``--format json``.

    Every failure is one ``Warning: <subject> was not written: <reason>`` line naming no absolute path
    (a directory refusal is escaped with ``visible``: a line break must not forge a report line). A
    pin mismatch writes nothing, since every writing command refuses under one. The resolved
    directory must stay below the resolved ``dist_root`` (a symlink could take it elsewhere). Not
    reached: a symlink created between the check and the write.
    """

    not_written = f"Warning: {subject} was not written:"
    if toolchain_pin_problem(project) is not None:
        print(f"{not_written} this project is pinned to another SWCraft release", file=sys.stderr)
        return
    try:
        from swcraft_tools.experiment_frame import write_report_files  # noqa: PLC0415 -- start-up: the report writer stays off the ARXML path

        try:
            directory = project_module.experiment_default_directory(project, relative)
            if not directory.is_relative_to(project.resolved_dist_root):
                raise ValueError(f"the report directory '<dist_root>/{relative}' resolves outside 'dist_root'")
        except ValueError as exc:
            print(f"{not_written} {visible(str(exc))}", file=sys.stderr)
            return
        path = directory / file_name
        write_report_files(directory, [(path, markdown())])
    except Exception as exc:  # noqa: BLE001 -- secondary: an unwritten report file is a warning, never the command's failure
        print(f"{not_written} {path_free_reason(exc)}", file=sys.stderr)
        return
    print_generated_report(path, sys.stderr if args.format == "json" else None)
