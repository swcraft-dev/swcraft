"""``swcraft experiment``: its parser and the allocation and trace-link experiments."""

from __future__ import annotations

import argparse
import sys

from swcraft_tools import allocation
from swcraft_tools.cli.messages import error_text
from swcraft_tools.cli.project_args import guard_project_flag, resolve_project
from swcraft_tools.cli.report_files import (
    print_generated_report,
    refuse_missing_report_directory,
    wants_markdown_report,
    write_report_file,
)
from swcraft_tools.project import SwcraftProject


def add_experiment_parser(subparsers: argparse._SubParsersAction) -> None:
    """The ``swcraft experiment`` parser: the kind, its configuration, the project, the format and the Apply flags."""
    experiment_parser = subparsers.add_parser(
        "experiment",
        help="Run an experiment of one kind: 'allocation' (where can a component move, and what does it buy) or "
        "'trace' (which elements may a requirement be linked to); a search writes a report and nothing into the "
        "model, and 'trace --apply' writes the decided links into it",
    )
    experiment_parser.add_argument(
        "kind", choices=["allocation", "trace"], metavar="KIND", help="'allocation' (CARMA) or 'trace' (links)"
    )
    experiment_parser.add_argument(
        "--config",
        default=None,
        metavar="FILE",
        help="Path to the experiment's configuration file (YAML); without it the kind runs with its defaults",
    )
    experiment_parser.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="Path to swcraft.yaml manifest (required)",
    )
    experiment_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format: 'text' (default, the summary and the files written, or Apply's report) or 'json' "
        "(the result document the run writes, or Apply's report document, on stdout)",
    )
    _add_experiment_apply_flags(experiment_parser)


def cmd_experiment(args: argparse.Namespace) -> None:
    """``swcraft experiment KIND [--config FILE]``: answer an experiment of one kind and write its report.

    The command writes, so a pin mismatch refuses. ``allocation`` refuses a model ``check-model``
    refuses (errors counted), an unreadable or refused configuration (``--config`` as given, relative
    to the current directory), an unknown name, a refused ``output``, and a solver failure. ``trace``
    refuses only an unreadable project, an ambiguous identity, a refused configuration, name or
    ``output``; every other finding is reported, and it also writes ``proposals.json``. Each refusal is
    one ``Error:`` line, exit 1, nothing written. Reports go under ``<dist_root>/experiments/<name>/``
    or ``output``, never into the model.

    Exit 0 on a run, "no improvement", "no free component" and "nothing found" included. Argparse's
    usage errors exit 2.
    """
    guard_project_flag(args, "experiment")
    _refuse_experiment_usage(args)
    refuse_missing_report_directory(args)
    project = resolve_project(args)
    if args.apply is not None:
        _run_trace_apply(args, project)
        return
    if args.documents:
        _run_trace_documents(args, project)
        return
    if args.kind == "trace":
        _run_trace_experiment(args, project)
        return
    try:
        run = allocation.run_allocation(project, args.config)
    except allocation.ConfigFileUnreadableError as exc:
        print(f"Error: {exc} ({args.config})", file=sys.stderr)
        sys.exit(1)
    except allocation.AllocationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 -- entry: a project that cannot be read is an Error: line and exit 1
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)
    if args.format == "json":
        print(allocation.result_json(run.document), end="")
        return
    print(run.summary)
    print_generated_report(run.report_path)
    print(f"Generated result: {run.result_path}")


def _run_trace_experiment(args: argparse.Namespace, project: SwcraftProject) -> None:
    """``swcraft experiment trace``: :func:`~swcraft_tools.trace_search.run_trace`, its refusals and its output."""
    from swcraft_tools import trace_search  # noqa: PLC0415 -- start-up: the trace modules load for the trace commands alone

    try:
        run = trace_search.run_trace(
            project, args.config, discard_pending=args.discard_pending, labelling=args.labelling
        )
    except trace_search.TraceConfigUnreadableError as exc:
        print(f"Error: {exc} ({args.config})", file=sys.stderr)
        sys.exit(1)
    except trace_search.TraceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 -- entry: a project that cannot be read is an Error: line and exit 1
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)
    if args.format == "json":
        print(trace_search.result_json(run.document), end="")
        return
    print(run.summary)
    print_generated_report(run.report_path)
    print(f"Generated result: {run.result_path}")
    print(f"Generated proposals: {run.proposals_path}")


def _add_experiment_apply_flags(experiment_parser: argparse.ArgumentParser) -> None:
    """``experiment``'s Apply flags, the search's ``--discard-pending``, ``--documents`` and ``--labelling``."""
    experiment_parser.add_argument(
        "--apply",
        default=None,
        metavar="FILE",
        help="'trace' only: apply the decisions in this proposals file (inside the project) -- write the accepted "
        "and confirmed links into the model, record the decisions in trace-decisions.yaml and mark them done in "
        "the file; all or nothing",
    )
    experiment_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --apply: print what Apply would write, and the plan's digest, and write nothing",
    )
    experiment_parser.add_argument(
        "--expect-plan",
        default=None,
        metavar="DIGEST",
        help="With --apply: refuse, writing nothing, when the plan differs from the one a dry run printed",
    )
    experiment_parser.add_argument(
        "--report",
        default=None,
        metavar="PATH",
        help="With --apply: also write the report to PATH after the run -- its Markdown form when PATH ends in .md, "
        "else what was printed (the JSON document with --format json, the text otherwise)",
    )
    experiment_parser.add_argument(
        "--discard-pending",
        action="store_true",
        help="'trace' search only: overwrite a previous proposals file that holds decisions not yet applied",
    )
    experiment_parser.add_argument(
        "--documents",
        action="store_true",
        help="'trace' only: print each document's requirements and links, the pass the configuration gives it and "
        "the proposals file a search would replace, and write nothing",
    )
    experiment_parser.add_argument(
        "--labelling",
        default=None,
        metavar="JSON",
        help="'trace' search only: the documents labelled in this run, as JSON -- \"all\" or a list of document "
        "values -- in place of the configuration's search.labelling; recorded in the report, never written back",
    )


def _refuse_experiment_usage(args: argparse.Namespace) -> None:
    """``experiment``'s usage refusals, decided before the pin: an ``Error:`` line and exit 1.

    Checked in a fixed order, so a command line with two faults is refused for the first.
    """
    refusal: str | None = None
    if args.apply is not None and args.kind != "trace":
        refusal = "--apply applies only to 'trace'."
    elif args.apply is not None and (args.config is not None or args.discard_pending):
        flag = "--config" if args.config is not None else "--discard-pending"
        refusal = f"{flag} applies only to a search, not to --apply."
    elif args.apply is None and (args.dry_run or args.expect_plan is not None or args.report is not None):
        flag = "--dry-run" if args.dry_run else "--expect-plan" if args.expect_plan is not None else "--report"
        refusal = f"{flag} applies only to --apply."
    elif args.discard_pending and args.kind != "trace":
        refusal = "--discard-pending applies only to 'trace'."
    elif args.documents and args.kind != "trace":
        refusal = "--documents applies only to 'trace'."
    elif args.documents and args.apply is not None:
        refusal = "--documents and --apply cannot be combined."
    elif args.documents and args.labelling is not None:
        refusal = "--labelling applies only to a search, not to --documents."
    elif args.documents and args.discard_pending:
        refusal = "--discard-pending applies only to a search, not to --documents."
    elif args.labelling is not None and args.kind != "trace":
        refusal = "--labelling applies only to 'trace'."
    elif args.labelling is not None and args.apply is not None:
        refusal = "--labelling applies only to a search, not to --apply."
    if refusal is not None:
        print(f"Error: {refusal}", file=sys.stderr)
        sys.exit(1)


def _run_trace_apply(args: argparse.Namespace, project: SwcraftProject) -> None:
    """``swcraft experiment trace --apply FILE``: :func:`~swcraft_tools.trace_apply.run_apply` and its output.

    The report is rendered before the write and printed after it, so a rolled-back run prints none.
    Exit 0 on a dry run, an applied run, nothing to do, or all decisions stale; 1 on every refusal and
    a failed write, whose notes (files a rollback could not restore) follow the ``Error:`` line.
    ``--report`` follows :func:`write_report_file`'s rule.
    """
    from swcraft_tools import trace_apply, trace_search  # noqa: PLC0415 -- start-up: the trace modules load for the trace commands alone

    try:
        run = trace_apply.run_apply(
            project,
            args.apply,
            dry_run=args.dry_run,
            expect_plan=args.expect_plan,
            markdown=wants_markdown_report(args),
        )
    except Exception as exc:  # noqa: BLE001 -- entry: a failed apply is an Error: line, its notes and exit 1
        text = str(exc) if isinstance(exc, trace_search.TraceError) else error_text(exc)
        print(f"Error: {text}", file=sys.stderr)
        for note in getattr(exc, "__notes__", ()):
            print(note, file=sys.stderr)
        sys.exit(1)
    printed = trace_search.result_json(run.document) if args.format == "json" else run.report
    print(printed, end="")
    done = "the dry run completed" if args.dry_run else "the decisions were applied"
    write_report_file(args, printed, run.markdown, done)


def _run_trace_documents(args: argparse.Namespace, project: SwcraftProject) -> None:
    """``swcraft experiment trace --documents``: the search's document table, writing nothing.

    Refuses what a search refuses before it searches, each an ``Error:`` line and exit 1.
    """
    from swcraft_tools import trace_search  # noqa: PLC0415 -- start-up: the trace modules load for the trace commands alone

    try:
        answer = trace_search.run_trace_documents(project, args.config)
    except trace_search.TraceConfigUnreadableError as exc:
        print(f"Error: {exc} ({args.config})", file=sys.stderr)
        sys.exit(1)
    except trace_search.TraceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 -- entry: a project that cannot be read is an Error: line and exit 1
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)
    if args.format == "json":
        print(trace_search.dump_json(answer, indent=2, ensure_ascii=False, sort_keys=True))
        return
    print(trace_search.documents_text(answer), end="")
