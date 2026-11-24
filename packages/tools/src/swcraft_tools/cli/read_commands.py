"""The read-only commands diff, impact, list, resource-report, metrics and change-report: parsers and handlers."""

from __future__ import annotations

import argparse
import sys
from fractions import Fraction
from typing import TYPE_CHECKING

from swcraft_tools import project as project_module
from swcraft_tools.cli.messages import error_text
from swcraft_tools.cli.project_args import guard_project_flag, non_negative_int, positive_int, resolve_project
from swcraft_tools.cli.report_files import print_generated_report, report_file_step, resource_report_step
from swcraft_tools.differ import diff_snapshots, format_diff, format_diff_json
from swcraft_tools.impact import format_impact, format_impact_scope, impact_of
from swcraft_tools.listing import format_listing, format_listing_json, list_model
from swcraft_tools.model_registry import current_registry
from swcraft_tools.project import SwcraftProject
from swcraft_tools.query import formatting as query_formatting
from swcraft_tools.resource_report import (
    KIND_CORE,
    KIND_MEMORY,
    FitQuestion,
    format_report_json,
    parse_utilization,
    resource_report,
)
from swcraft_tools.snapshot import load_snapshot
from swcraft_tools.validator import check_hardware_topology

if TYPE_CHECKING:
    from swcraft_tools.metrics import MetricsReport


def cmd_diff(args: argparse.Namespace) -> None:
    """Semantic diff between two model revisions.

    Argument semantics mirror ``git diff``:
        zero args   -> HEAD vs working tree
        one arg     -> REV vs working tree
        two args    -> REV_A vs REV_B (both git revisions)

    Informational, not a gate: a successful diff always exits 0, in both formats, changes or not. Only
    an unreadable project or revision exits non-zero. Rendering sits inside the ``ValueError`` guard so
    a JSON encoding failure is an ``Error:`` line, not a traceback.
    """
    guard_project_flag(args, "diff")
    project = resolve_project(args)
    model_root = project.resolved_model_root

    rev_a: str | None = args.rev_a
    rev_b: str | None = args.rev_b

    try:
        if rev_a is None:
            old = load_snapshot(model_root, rev="HEAD")
            new = load_snapshot(model_root, rev=None)
        elif rev_b is None:
            old = load_snapshot(model_root, rev=rev_a)
            new = load_snapshot(model_root, rev=None)
        else:
            old = load_snapshot(model_root, rev=rev_a)
            new = load_snapshot(model_root, rev=rev_b)
        diff = diff_snapshots(old, new)
        rendered = format_diff_json(diff) if args.format == "json" else format_diff(diff) + "\n"
    except ValueError as exc:
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)

    # Both forms are newline-terminated by here.
    sys.stdout.write(rendered)


def cmd_impact(args: argparse.Namespace) -> None:
    """Dependents of one named element in the working tree.

    Exits 0 on a run that read the project, matched or not; 1 when it could not read one; 2 on a usage
    error, a ``--depth`` below 1 included (so, unlike ``query --depth 0``'s exit 1, it stays an
    always-exit-0 command).

    The default output is :func:`format_impact`'s report; ``--depth N`` appends the footer exactly as
    the MCP ``impact_of`` tool's ``full`` form does; ``--format json`` prints the walk document
    ``query --dependents NAME --format json`` prints.
    """
    guard_project_flag(args, "impact")
    project = resolve_project(args)
    model_root = project.resolved_model_root

    try:
        snapshot = load_snapshot(model_root, rev=None)
        result = impact_of(snapshot, args.name, depth=args.depth)
    except ValueError as exc:
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)

    if args.format == "json":
        # Newline-terminated by contract.
        sys.stdout.write(
            query_formatting.format_related_json(args.name, result, direction="dependents", depth=args.depth)
        )
        return
    text = format_impact(args.name, result)
    scope = format_impact_scope(depth=args.depth)
    if scope:
        text = f"{text}\n\n{scope}"
    print(text)


def cmd_list(args: argparse.Namespace) -> None:
    """``swcraft list``: every declared layer's elements, by identity.

    Read-only and always exits 0 on a successful run (an empty model is an answer). A project it
    cannot read exits non-zero, a single unparseable element file included: every file is validated,
    so there is no partial answer.
    """
    guard_project_flag(args, "list")
    project = resolve_project(args)
    model_root = project.resolved_model_root

    try:
        listings = list_model(model_root, args.layer)
        rendered = format_listing_json(listings) if args.format == "json" else format_listing(listings)
    except (OSError, ValueError) as exc:
        # `OSError`: an absent model root.
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)

    # Both forms are newline-terminated (text is empty for an empty model); `print` would add a line.
    sys.stdout.write(rendered)


def cmd_resource_report(args: argparse.Namespace) -> None:
    """``swcraft resource-report``: the resource utilization report for the working tree.

    Read-only unless ``--report`` is given, and always exits 0 on a successful run, even over budget:
    it reports, ``swcraft validate`` is the gate. Only an unreadable project or model exits non-zero.

    The hardware topology (``check_hardware_topology``) is computed here and handed to the renderer,
    as the MCP ``resource_report`` tool does, so both surfaces carry it; it does not make the command
    a gate. It is evaluated first and independently of the budget analysis, so a model with no core or
    memory still gets its topology findings, and a malformed model exits 1 with a message.

    Which unparseable file exits 1: ``swc/`` always (the ARRIVALS and IMPLICIT ACCESS sections read it
    on every run); ``tasks/`` when ``hardware/`` exists, or when a data-received event or an implicit
    access exists. Some layers are parsed more than once per run, accepted to keep the renderer's input
    a model root.

    ``--format json`` makes stdout one document or nothing. ``--fit-bytes``/``--fit-utilization`` name
    no resource, so they cannot add a refusal; an unreadable amount is argparse's exit 2. ``--report``
    writes from the same computed report after stdout and never moves the exit code.
    """
    guard_project_flag(args, "resource-report")
    project = resolve_project(args)
    model_root = project.resolved_model_root
    fit = _fit_question(args)

    try:
        # Passed in: the renderer importing `validator` would be a cycle.
        report = resource_report(model_root, check_hardware_topology(model_root).records)
        rendered = (
            format_report_json(report, fit)
            if args.format == "json"
            else report.render(subject=project.name, fit=fit) + "\n"
        )
    except ValueError as exc:
        # No `OverflowError`: the figures are exact `Fraction`s, never floats.
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)

    sys.stdout.write(rendered)
    resource_report_step(args, project, report, fit)


def add_diff_parser(subparsers: argparse._SubParsersAction) -> None:
    """The ``swcraft diff`` parser: two optional revisions, the project and the output format."""
    diff_parser = subparsers.add_parser(
        "diff",
        help="Semantic diff between two model revisions (git-diff-like arg semantics)",
    )
    diff_parser.add_argument(
        "rev_a",
        nargs="?",
        default=None,
        metavar="REV_A",
        help="Older revision (default: HEAD). If REV_B is omitted, compared against the working tree.",
    )
    diff_parser.add_argument(
        "rev_b",
        nargs="?",
        default=None,
        metavar="REV_B",
        help="Newer revision. If omitted, the working tree is used.",
    )
    diff_parser.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="Path to swcraft.yaml manifest (required)",
    )
    diff_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format: 'text' (default, human-readable) or 'json' (versioned, machine-readable)",
    )


def add_impact_parser(subparsers: argparse._SubParsersAction) -> None:
    """The ``swcraft impact`` parser: the element's name, the project, a depth bound and the output format."""
    impact_parser = subparsers.add_parser(
        "impact",
        help="Transitive dependents of one named element (always the working tree)",
    )
    impact_parser.add_argument(
        "name",
        metavar="NAME",
        help="Bare name to look up across all model layers (and Requirement.id)",
    )
    impact_parser.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="Path to swcraft.yaml manifest (required)",
    )
    impact_parser.add_argument(
        "--depth",
        type=positive_int,
        default=None,
        metavar="N",
        help="Bound the walk to N levels, echoed as a '(depth: N)' footer (default: the whole transitive walk)",
    )
    impact_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help=(
            "Output format: 'text' (default, the grouped report) or 'json' "
            "(the walk document 'query --dependents' prints)"
        ),
    )


def add_list_parser(subparsers: argparse._SubParsersAction) -> None:
    """The ``swcraft list`` parser: an optional layer from the contract's registry, the project, the output format."""
    # Layer names come from the registry per run, never a literal; the metavar keeps the usage line short.
    list_parser = subparsers.add_parser(
        "list",
        help="List every declared layer's elements by identity (read-only; always exits 0 on a successful run)",
    )
    list_parser.add_argument(
        "layer",
        nargs="?",
        default=None,
        metavar="LAYER",
        choices=current_registry().display_order,
        help="One layer to list (default: every layer, in the order the layers are listed)",
    )
    list_parser.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="Path to swcraft.yaml manifest (required)",
    )
    list_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format: 'text' (default, one 'layer: element' line) or 'json' (versioned, machine-readable)",
    )


def add_resource_report_parser(subparsers: argparse._SubParsersAction) -> None:
    """The ``swcraft resource-report`` parser: the project, the output format, a fit question and the report file."""
    resource_parser = subparsers.add_parser(
        "resource-report",
        help=(
            "Print the resource utilization report (read-only without --report; always exits 0 — "
            "'swcraft validate' is the gate)"
        ),
    )
    resource_parser.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="Path to swcraft.yaml manifest (required)",
    )
    resource_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format: 'text' (default) or 'json' (versioned, machine-readable)",
    )
    resource_fit = resource_parser.add_mutually_exclusive_group()
    resource_fit.add_argument(
        "--fit-bytes",
        type=non_negative_int,
        default=None,
        metavar="N",
        help="Ask whether N more bytes fit each memory, within its threshold and within its size",
    )
    resource_fit.add_argument(
        "--fit-utilization",
        type=_utilization,
        default=None,
        metavar="U",
        help="Ask whether a share U of a core (0.05 or 1/20) fits each core, within its threshold and the whole core",
    )
    resource_parser.add_argument(
        "--report",
        action="store_true",
        help="Also write the report as Markdown to <dist_root>/resource-report/resource-report.md",
    )


def add_change_report_parser(subparsers: argparse._SubParsersAction) -> None:
    """The ``swcraft change-report`` parser: ``diff``'s two revisions, a format, a threshold, no pictures."""
    report_parser = subparsers.add_parser(
        "change-report",
        help="Report what a change does to the architecture -- what changed, requirement links, the findings "
        "it introduced, resources, interface consumers, ARXML elements and the overviews before and after -- "
        "into <dist_root>/change-report/ (git-diff-like revision arguments; writes nothing into the model)",
    )
    report_parser.add_argument(
        "rev_a",
        nargs="?",
        default=None,
        metavar="REV_A",
        help="Older revision (default: HEAD). If REV_B is omitted, compared against the working tree.",
    )
    report_parser.add_argument(
        "rev_b",
        nargs="?",
        default=None,
        metavar="REV_B",
        help="Newer revision. If omitted, the working tree is used.",
    )
    report_parser.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="Path to swcraft.yaml manifest (required)",
    )
    report_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format: 'text' (default, the path of the report written) or 'json' (the report's JSON "
        "document on stdout); the files are written either way",
    )
    report_parser.add_argument(
        "--fail-on",
        choices=["never", "warning", "error"],
        default="never",
        help="Exit 1 after writing the report when the change introduces a finding of this level or above "
        "('never', the default: the run succeeds whatever the change introduces)",
    )
    report_parser.add_argument(
        "--no-diagrams",
        action="store_true",
        help="Leave the overview pictures out (drawing them needs Node.js)",
    )


def cmd_change_report(args: argparse.Namespace) -> None:
    """``swcraft change-report``: what a change does to the architecture, written as a report.

    The revisions are ``diff``'s. The report is computed in memory, then written into
    ``<dist_root>/change-report/``; an earlier run's other files are removed only after the write, so a
    failed write leaves the earlier set whole. Nothing is written into the model.

    Exit 0 whatever the change introduced. Exit 1, one ``Error:`` line and nothing written, on an
    unreadable project or revision, a pin mismatch (it writes, so the pin refuses), a refused report
    directory, or a name declared twice. ``--fail-on`` exits 1 after the report is written.
    Argparse's usage error exits 2.
    """
    from swcraft_tools import change_report, change_report_render  # noqa: PLC0415 -- start-up: the change report's modules load for `change-report` alone
    from swcraft_tools.experiment_frame import write_report_files  # noqa: PLC0415 -- start-up: the report writer stays off the ARXML path

    guard_project_flag(args, "change-report")
    project = resolve_project(args)
    try:
        directory = project_module.experiment_default_directory(project, change_report_render.REPORT_DIRECTORY)
        old_revision, new_revision = change_report.revisions_from_arguments(args.rev_a, args.rev_b)
        report = change_report.compute_change_report(project, old_revision, new_revision, diagrams=not args.no_diagrams)
        files = change_report_render.report_files(report)
    except ValueError as exc:
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)

    try:
        write_report_files(directory, [(directory / name, text) for name, text in files])
    except OSError as exc:
        print(f"Error: the report could not be written: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)
    change_report_render.remove_earlier_report_files(directory, [name for name, _text in files])

    if report.layout_refusal is not None:
        print(f"Warning: diagrams not drawn: {report.layout_refusal}", file=sys.stderr)
    if args.format == "json":
        sys.stdout.write(dict(files)[change_report_render.JSON_FILE])
    else:
        print_generated_report(directory / change_report_render.MARKDOWN_FILE)
    if report.fails_on(args.fail_on):
        print(
            f"Error: the change introduces {report.introduced_errors} error(s) and "
            f"{report.introduced_warnings} warning(s) (--fail-on {args.fail_on}).",
            file=sys.stderr,
        )
        sys.exit(1)


def _utilization(raw: str) -> Fraction:
    """argparse's ``type=`` for a share of a core (``--fit-utilization``), read as the MCP tool reads it."""
    try:
        amount = parse_utilization(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a share of a core such as 0.05 or 1/20, got {raw!r}") from exc
    if amount < 0:
        raise argparse.ArgumentTypeError(f"expected a share of a core, 0 or more, got {raw!r}")
    return amount


def _fit_question(args: argparse.Namespace) -> FitQuestion | None:
    """``resource-report``'s fit question, or ``None``: the parser makes its two flags mutually exclusive."""
    if args.fit_bytes is not None:
        return FitQuestion(KIND_MEMORY, Fraction(args.fit_bytes))
    if args.fit_utilization is not None:
        return FitQuestion(KIND_CORE, args.fit_utilization)
    return None


def add_metrics_parser(subparsers: argparse._SubParsersAction) -> None:
    """The ``swcraft metrics`` parser: a project, the revisions to compare against, a format, a report file.

    ``--since`` with ``--since-head``, or the ambiguous abbreviation ``--sin``, is argparse's exit 2.
    """
    metrics_parser = subparsers.add_parser(
        "metrics",
        help="Print the architecture metrics -- size, coupling, cohesion, coverage and integrity, each explained, "
        "and how they moved since a revision (read-only without --report; always exits 0 -- "
        "'swcraft validate' is the gate)",
    )
    metrics_parser.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="Path to swcraft.yaml manifest (required)",
    )
    base = metrics_parser.add_mutually_exclusive_group()
    base.add_argument(
        "--since",
        action="append",
        default=None,
        metavar="REV",
        help="A revision to compare the working tree against, read as 'swcraft diff' reads one; repeat it for a "
        "series, the first named being the base. A revision that cannot be read fails the run",
    )
    base.add_argument(
        "--since-head",
        action="store_true",
        help="Compare against HEAD when it can be read; when it cannot, the report says why and the run still succeeds",
    )
    metrics_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format: 'text' (default, the report with its explanations) or 'json' (versioned, "
        "machine-readable)",
    )
    metrics_parser.add_argument(
        "--report",
        action="store_true",
        help="Also write the report as Markdown to <dist_root>/metrics/metrics.md",
    )


def cmd_metrics(args: argparse.Namespace) -> None:
    """``swcraft metrics``: the architecture's figures, what each means here, and how they moved.

    Read-only unless ``--report`` is given, and always exits 0 on a successful run: it reports,
    ``swcraft validate`` is the gate. ``compute_metrics`` opens its own ``single_read`` per side.

    Exit 1, one ``Error:`` line and nothing on stdout, on an unreadable project or an unreadable
    ``--since`` revision (a series with a missing column would mislead). An unreadable HEAD under
    ``--since-head`` is one sentence in the report, exit 0. A pin mismatch warns, answers and writes
    no file. Argparse's usage error exits 2.
    """
    from swcraft_tools import metrics, metrics_render  # noqa: PLC0415 -- start-up: the metrics modules load for `metrics` alone

    guard_project_flag(args, "metrics")
    project = resolve_project(args)
    try:
        report = metrics.compute_metrics(project, args.since or (), since_head=args.since_head)
        rendered = metrics_render.render_json(report) if args.format == "json" else metrics_render.render_text(report)
    except ValueError as exc:
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)

    sys.stdout.write(rendered)
    _metrics_report_step(args, project, report)


def _metrics_report_step(args: argparse.Namespace, project: SwcraftProject, report: MetricsReport) -> None:
    """``metrics --report``'s one step: :func:`report_file_step` with this command's file."""

    if not args.report:
        return
    from swcraft_tools import metrics_render  # noqa: PLC0415 -- start-up: the metrics modules load for `metrics` alone

    report_file_step(
        args,
        project,
        "the metrics report",
        metrics_render.REPORT_DIRECTORY,
        metrics_render.MARKDOWN_FILE,
        lambda: metrics_render.render_markdown(report),
    )
