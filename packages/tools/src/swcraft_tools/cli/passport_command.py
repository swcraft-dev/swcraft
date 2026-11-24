"""``swcraft passport``: its parser, its steps and its handler."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from swcraft_tools import passport
from swcraft_tools.cli.messages import (
    is_bare_os_error,
    model_read_failure_sentence,
    os_failure,
    passport_project_failure,
)
from swcraft_tools.cli.project_args import check_toolchain_pin, guard_project_flag
from swcraft_tools.env import load_env_file
from swcraft_tools.project import require_project


def add_passport_parser(subparsers: argparse._SubParsersAction) -> None:
    """The ``swcraft passport`` parser: a project and an optional output file."""
    passport_parser = subparsers.add_parser(
        "passport",
        help="Write the model's passport -- counts, distributions and lengths, with no name, no value and no "
        "text of the model -- as JSON to stdout or to --output, to send instead of a model that cannot be "
        "shared (writes nothing into the model)",
    )
    passport_parser.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="Path to swcraft.yaml manifest (required)",
    )
    passport_parser.add_argument(
        "--output",
        default=None,
        metavar="FILE",
        help="Write the passport to this file instead of stdout",
    )


def cmd_passport(args: argparse.Namespace) -> None:
    """``swcraft passport``: the names-free passport of the project's model, as JSON.

    Only the ``passport`` is printed (indent 2, trailing newline), never the locator, which names
    elements. A model ``check-model`` refuses still gets one.

    Exit 0 when written. Exit 1, one ``Error:`` line: a missing ``--project``, an unreadable project or
    model, a pin mismatch (the pinned value is withheld), an unwritable ``--output``; any other exception
    is its class alone, never a traceback. Not reached: argparse's usage error (exit 2) and a failure
    before :func:`main` runs.
    """
    try:
        _passport_steps(args)
    except Exception as exc:  # noqa: BLE001 -- entry: an unexpected exception is its class alone
        print(f"Error: the passport could not be made: {type(exc).__name__}, its text withheld", file=sys.stderr)
        sys.exit(1)


def _passport_steps(args: argparse.Namespace) -> None:
    """Every step of ``swcraft passport`` after argument parsing, in order."""

    try:
        load_env_file()
    except Exception as exc:  # noqa: BLE001 -- entry: a .env refusal: an OS error's reason, anything else its class
        reason = os_failure(exc) if is_bare_os_error(exc) else f"{type(exc).__name__}, its text withheld"
        print(f"Error: the .env file could not be read: {reason}", file=sys.stderr)
        sys.exit(1)
    guard_project_flag(args, "passport")
    try:
        project = require_project(args.project)
    except Exception as exc:  # noqa: BLE001 -- entry: every refusal spelled names-free, the unexpected by its class
        print(f"Error: {passport_project_failure(exc)}", file=sys.stderr)
        sys.exit(1)
    check_toolchain_pin(project, args.command, withhold_pin=True)
    try:
        result = passport.compute_passport(project.resolved_model_root)
    except Exception as exc:  # noqa: BLE001 -- entry: every refusal spelled names-free, the unexpected by its class
        print(f"Error: the model could not be read: {model_read_failure_sentence(exc)}", file=sys.stderr)
        sys.exit(1)

    text = json.dumps(result.passport, indent=2) + "\n"
    if args.output is None:
        sys.stdout.write(text)
        return
    try:
        Path(args.output).write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        reason = exc.strerror or "the file could not be written"
        print(f"Error: the passport could not be written to --output: {reason}", file=sys.stderr)
        sys.exit(1)
