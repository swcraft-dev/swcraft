"""``mcp-serve`` and ``agent``: their parsers and the hand-over to the server or the agent process."""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys

from swcraft_tools.cli.project_args import check_toolchain_pin_of, guard_project_flag, remainder_project


def _exec_replaces_this_process() -> bool:
    """Return whether ``os.execvp`` really replaces this process: not on Windows, where it spawns and exits."""
    return os.name != "nt"


def _exit_status_of(code: int) -> int:
    """Return *code* as an exit status this process can hand on, folded out of DWORD form.

    Windows reports a status unsigned (``0xC000013A`` as ``3221225786``), and ``sys.exit`` converts it to
    a 32-bit ``long`` there, so anything above ``0x7FFFFFFF`` would exit ``-1``. Only that top half is
    folded to its signed value; every other code passes through.
    """
    if code > 0x7FFF_FFFF:
        return code - 0x1_0000_0000
    return code


def _hand_over(command: list[str]) -> None:
    """Give the rest of this run to *command*, in this same installation; does not return.

    On POSIX it is ``os.execvp``; on Windows the child is waited for and its exit status handed on.
    Not reached: no signal is forwarded; a child that never exits is waited for forever; a child
    outliving a killed parent is reaped by nothing here.
    """
    if not _exec_replaces_this_process():
        # Inherited stdin on purpose: the child is the stdio MCP server and needs the JSON-RPC pipe. The
        # one spawn in the package allowed to inherit stdin.
        process = subprocess.Popen(command, stdin=None)
        while True:
            try:
                sys.exit(_exit_status_of(process.wait()))
            except KeyboardInterrupt:
                # Never `kill()`: the child handles its own interrupt.
                continue
    # Last statement: a patched `exec` in tests must not fall through.
    os.execvp(command[0], command)


def cmd_mcp_serve(args: argparse.Namespace) -> None:
    """Start the MCP server for the given project."""
    guard_project_flag(args, "mcp-serve")
    check_toolchain_pin_of(args.project, args.command)

    spec = importlib.util.find_spec("swcraft_mcp")
    if spec is None:
        print(
            "swcraft-mcp package required: pip install swcraft-mcp",
            file=sys.stderr,
        )
        sys.exit(1)

    # Both an `exec` and the Windows child inherit `os.environ`.
    os.environ["SWCRAFT_MANIFEST"] = args.project
    _hand_over([sys.executable, "-m", "swcraft_mcp.server"])


def cmd_agent(args: argparse.Namespace) -> None:
    """Hand the rest of the command line to the assistant, unchanged.

    A process handover with this interpreter (``swcraft_tools`` may not import ``swcraft_agent``).
    Exits 1 with the install line when ``swcraft_agent`` is absent. The tokens are passed on as given;
    only ``--project`` is read, for the pin.
    """
    check_toolchain_pin_of(remainder_project(args.agent_args), args.command)

    spec = importlib.util.find_spec("swcraft_agent")
    if spec is None:
        print(
            "swcraft-agent package required: pip install swcraft-agent",
            file=sys.stderr,
        )
        sys.exit(1)

    _hand_over([sys.executable, "-m", "swcraft_agent.cli", *args.agent_args])


def add_mcp_serve_parser(subparsers: argparse._SubParsersAction) -> None:
    """Declare ``swcraft mcp-serve``: the project the server answers about."""
    mcp_parser = subparsers.add_parser("mcp-serve", help="Launch the swcraft MCP server")
    mcp_parser.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="Path to swcraft.yaml manifest (required)",
    )


def add_agent_parser(subparsers: argparse._SubParsersAction) -> None:
    """Declare ``swcraft agent``: everything after it, passed on as the assistant's own command line."""
    # The passthrough is the whole surface: restating the assistant's options would be a second one.

    agent_parser = subparsers.add_parser(
        "agent",
        help="Talk to the assistant about this project (everything written after 'agent' is its own command line)",
    )
    agent_parser.add_argument(
        "agent_args",
        nargs=argparse.REMAINDER,
        metavar="ARGS",
        help="The assistant's own command line, for example 'chat --project swcraft.yaml'",
    )
