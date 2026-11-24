"""``swcraft guard``: its parser and the handler that runs the Copilot hook."""

from __future__ import annotations

import argparse
import sys

from swcraft_tools import copilot_guard


def add_guard_parser(subparsers: argparse._SubParsersAction) -> None:
    """Declare ``swcraft guard``: one option, ``--host``, and a hook payload on stdin."""
    parser = subparsers.add_parser(
        "guard",
        help="Answer an agent host's PreToolUse hook: read the tool call's JSON payload on stdin and answer in "
        "the host's form -- for Copilot, Codex and Claude Code the decision printed on stdout, exit 0; for Kiro "
        "a denial is exit 2 with one line on stderr and an allow exit 0 with nothing printed. For Copilot (the "
        "default), deny a known file tool's read or search that reaches the model directory or cannot be judged; "
        "for Codex (--host codex), also deny a patch or a shell command that reaches it; for Claude Code (--host "
        "claude), also deny an edit, a write or a shell command that reaches it, and every PowerShell call; for "
        "Kiro (--host kiro), judge Kiro's read, search, edit and shell tools alike and deny every tool it does "
        "not know; allow the rest (changes nothing)",
    )
    parser.add_argument(
        "--host",
        choices=copilot_guard.HOSTS,
        default=copilot_guard.HOSTS[0],
        help="the host whose hook calls this: copilot (the default), codex, claude or kiro; codex, claude and kiro "
        "change the answer's shape and add the edit and shell rules; kiro alone denies with exit 2 and one stderr "
        "line instead of an answer on stdout",
    )


def cmd_guard(args: argparse.Namespace) -> None:
    """Answer one host's ``PreToolUse`` hook call for ``swcraft guard`` (:func:`swcraft_tools.copilot_guard.run_hook`).

    Reads the payload from stdin and answers in ``--host``'s dialect. Copilot, codex and claude: the
    decision on stdout (``{}`` allows), exit 0 once it runs, a denial included. Kiro: a denial is exit 2
    with one stderr line, an allow exit 0 with nothing printed. A non-object payload names no tool and
    is allowed with a note (under kiro denied in a guarded project).

    A pin mismatch is a ``Warning:`` line and the decision stands. Argparse's usage error exits 2.
    """

    if code := copilot_guard.run_hook(args.host):
        sys.exit(code)
