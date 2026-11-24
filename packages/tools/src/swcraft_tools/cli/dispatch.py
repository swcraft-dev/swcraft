"""The ``swcraft`` root parser, ``main`` and the dispatch from a subcommand to its handler."""

from __future__ import annotations

import argparse
import contextlib
import sys

from swcraft_tools import model_state
from swcraft_tools._version import __version__
from swcraft_tools.cli.build_commands import (
    add_check_model_parser,
    add_clean_outputs_parser,
    add_generate_parser,
    add_validate_parser,
    cmd_check_model,
    cmd_clean_outputs,
    cmd_generate,
    cmd_validate,
)
from swcraft_tools.cli.docs_command import add_docs_parser, cmd_docs
from swcraft_tools.cli.experiment_command import add_experiment_parser, cmd_experiment
from swcraft_tools.cli.guard_command import add_guard_parser, cmd_guard
from swcraft_tools.cli.handover import add_agent_parser, add_mcp_serve_parser, cmd_agent, cmd_mcp_serve
from swcraft_tools.cli.import_commands import add_import_parser, cmd_import
from swcraft_tools.cli.init_command import add_init_parser, cmd_init
from swcraft_tools.cli.passport_command import add_passport_parser, cmd_passport
from swcraft_tools.cli.project_args import load_env_file_unless_passport
from swcraft_tools.cli.query_command import add_query_parser, cmd_query
from swcraft_tools.cli.read_commands import (
    add_change_report_parser,
    add_diff_parser,
    add_impact_parser,
    add_list_parser,
    add_metrics_parser,
    add_resource_report_parser,
    cmd_change_report,
    cmd_diff,
    cmd_impact,
    cmd_list,
    cmd_metrics,
    cmd_resource_report,
)
from swcraft_tools.env import force_utf8_streams
from swcraft_tools.yaml_load import clear_parse_cache


def build_parser() -> argparse.ArgumentParser:
    """The ``swcraft`` parser: the root and ``--version``, then each subcommand's adder in ``--help`` order."""
    parser = argparse.ArgumentParser(
        prog="swcraft",
        description="SWCraft toolchain CLI",
    )
    # One lockstep version for the whole toolchain: the string a project pins.
    parser.add_argument(
        "--version",
        action="version",
        version=f"swcraft {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")
    add_init_parser(subparsers)
    add_generate_parser(subparsers)
    add_validate_parser(subparsers)
    add_check_model_parser(subparsers)
    add_diff_parser(subparsers)
    add_impact_parser(subparsers)
    add_list_parser(subparsers)
    add_query_parser(subparsers)
    add_resource_report_parser(subparsers)
    add_mcp_serve_parser(subparsers)
    add_agent_parser(subparsers)
    add_clean_outputs_parser(subparsers)
    add_import_parser(subparsers)
    add_docs_parser(subparsers)
    add_experiment_parser(subparsers)
    add_guard_parser(subparsers)
    add_change_report_parser(subparsers)
    add_passport_parser(subparsers)
    add_metrics_parser(subparsers)

    return parser


def main() -> None:
    """Entry point for the ``swcraft`` CLI command."""
    # Before anything prints: `parse_args()` writes `--help` and usage errors, and the help holds non-ASCII.
    force_utf8_streams()
    load_env_file_unless_passport(sys.argv[1:])
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "init" and args.client and args.integration is None:
        # A usage error, exit 2, before anything is written: a plain init writes no client file.
        parser.error("init: --client needs --integration moon (a plain init writes no chat client's files)")
    clear_parse_cache()

    with _single_read_for(args):
        if args.command == "init":
            cmd_init(args)
        elif args.command == "generate":
            cmd_generate(args)
        elif args.command == "validate":
            cmd_validate(args)
        elif args.command == "check-model":
            cmd_check_model(args)
        elif args.command == "diff":
            cmd_diff(args)
        elif args.command == "impact":
            cmd_impact(args)
        elif args.command == "list":
            cmd_list(args)
        elif args.command == "query":
            cmd_query(args)
        elif args.command == "resource-report":
            cmd_resource_report(args)
        elif args.command == "mcp-serve":
            cmd_mcp_serve(args)
        elif args.command == "agent":
            cmd_agent(args)
        elif args.command == "clean-outputs":
            cmd_clean_outputs(args)
        elif args.command == "import":
            cmd_import(args)
        elif args.command == "docs":
            cmd_docs(args)
        elif args.command == "experiment":
            cmd_experiment(args)
        elif args.command == "guard":
            cmd_guard(args)
        elif args.command == "change-report":
            cmd_change_report(args)
        elif args.command == "passport":
            cmd_passport(args)
        elif args.command == "metrics":
            cmd_metrics(args)
        else:
            parser.print_help()
            sys.exit(1)


def _single_read_for(args: argparse.Namespace) -> contextlib.AbstractContextManager[None]:
    """The block :func:`main` runs a command in: ``single_read()``, or nothing.

    Only commands that read layers repeatedly and write nothing under the model get the block
    (their outputs cannot overlap ``model_root``). ``generate --component``/``--all-components`` write
    and read a temporary model, so they stay out. No block is the default: a missing command costs
    time, never truth. Not reached: a read outside the layer loader, and a CLI run in a subprocess.
    """

    if args.command in {"check-model", "validate", "resource-report"}:
        return model_state.single_read()
    if args.command == "generate" and not (args.component or args.all_components):
        return model_state.single_read()
    return contextlib.nullcontext()
