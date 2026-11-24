"""The project, the toolchain pin and the argument types every command shares."""

from __future__ import annotations

import argparse
import sys

from swcraft_tools import project as project_module
from swcraft_tools.cli.messages import error_text
from swcraft_tools.env import load_env_file
from swcraft_tools.project import SwcraftProject, require_project
from swcraft_tools.resource_report import FIT_TEXT_MAX
from swcraft_tools.toolchain_pin import toolchain_pin_problem as toolchain_pin_problem

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def guard_project_flag(args: argparse.Namespace, cmd: str) -> None:
    """The guard that prints an error and exits 1 if ``args.project`` is not set."""
    if not args.project:
        print(
            f"Error: --project is required.  Run: swcraft {cmd} --project PATH",
            file=sys.stderr,
        )
        sys.exit(1)


#: The read-only commands, where a pin mismatch is reported and the answer still produced: they
#: promise success on any run that could read the project, and a mismatch is no read failure. Every
#: other command writes or gates and is refused. Mirrors ``guide_reference.cli.ALWAYS_EXITS_ZERO +
#: REFUSES_AN_UNANSWERABLE_REQUEST`` (which imports this module); a test holds the copies equal.
_PIN_ADVISORY_COMMANDS = frozenset({"diff", "impact", "list", "metrics", "query", "resource-report"})


def check_toolchain_pin(project: SwcraftProject, command: str, *, withhold_pin: bool = False) -> None:
    """The pin check: a mismatch reported, and the run refused unless *command* only reads.

    A command that writes or gates stops here (``Error:``, exit 1) before writing anything; a
    read-only command prints the same sentence as ``Warning:`` and answers. *withhold_pin* hides the
    pinned value (``swcraft passport``).
    """
    problem = toolchain_pin_problem(project, withhold_pin=withhold_pin)
    if problem is None:
        return
    if command in _PIN_ADVISORY_COMMANDS:
        print(f"Warning: {problem}", file=sys.stderr)
        return
    print(f"Error: {problem}", file=sys.stderr)
    sys.exit(1)


def resolve_project(args: argparse.Namespace) -> SwcraftProject:
    """The project ``args.project`` names, its pin checked against ``args.command`` so no command can skip the pin.

    Not covered: commands that hand the manifest to another program use :func:`check_toolchain_pin_of`.
    """
    try:
        project = require_project(args.project)
    except Exception as exc:  # noqa: BLE001 -- entry: a project that cannot be read is an Error: line and exit 1
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)
    check_toolchain_pin(project, args.command)
    return project


def as_given_or_in_project(project: SwcraftProject, path: str) -> str:
    """*path* named relative to the project inside it and as given outside it (the MCP server's rule)."""
    return project_module.project_relative_name(project, path) or path


def check_toolchain_pin_of(path: str | None, command: str) -> None:
    """The pin check for a command that hands the manifest to another program.

    Silent on a manifest that cannot be read: the receiving program reports that in its own words.
    """
    if not path:
        return
    try:
        project = require_project(path)
    except Exception:  # noqa: BLE001 -- deferred: the command's own read reports a project it cannot load
        return
    check_toolchain_pin(project, command)


def remainder_project(remainder: list[str]) -> str | None:
    """The ``--project`` of ``swcraft agent``'s passed-through command line, or ``None``.

    Reads ``--project P`` and ``--project=P`` and stops at a bare ``--`` (past it, the tokens are the
    assistant's own). A missing one is the assistant's to report.
    """
    for index, token in enumerate(remainder):
        if token == "--":
            return None
        if token == "--project":
            return remainder[index + 1] if index + 1 < len(remainder) else None
        if token.startswith("--project="):
            return token.split("=", 1)[1]
    return None


def positive_int(raw: str) -> int:
    """argparse's ``type=`` for a count that is at least 1 (``impact --depth``): exit 2 otherwise.

    The ASCII-digit spelling is checked first, because ``int()`` also accepts ``+3``, ``1_0`` and
    non-ASCII digits. ``query`` refuses a value below 1 in its body instead (exit 1).
    """
    spelled_as_digits = raw.isascii() and raw.isdigit()
    if not spelled_as_digits or int(raw) < 1:
        raise argparse.ArgumentTypeError(f"expected a whole number of at least 1, got {raw!r}")
    return int(raw)


def load_env_file_unless_passport(argv: list[str]) -> None:
    """:func:`load_env_file`, except for ``swcraft passport``, which loads it inside its names-free boundary.

    The root parser has no option taking a value, so the command is the first argument not starting
    with ``-``.
    """
    command = next((arg for arg in argv if not arg.startswith("-")), None)
    if command != "passport":
        load_env_file()


def non_negative_int(raw: str) -> int:
    """argparse's ``type=`` for an amount that is at least 0 (``resource-report --fit-bytes``).

    :func:`positive_int`'s spelling rule with 0 allowed, at most ``FIT_TEXT_MAX`` digits (past
    Python's ``int()`` digit limit the conversion would fail without a useful message).
    """

    if not (raw.isascii() and raw.isdigit()):
        raise argparse.ArgumentTypeError(f"expected a whole number of bytes, 0 or more, got {raw!r}")
    if len(raw) > FIT_TEXT_MAX:
        raise argparse.ArgumentTypeError(
            f"expected a whole number of bytes of at most {FIT_TEXT_MAX} digits, got {len(raw)} digits"
        )
    return int(raw)
