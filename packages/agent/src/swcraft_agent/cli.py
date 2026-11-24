"""swcraft-agent CLI entry point.

Subcommands:
    swcraft-agent chat --project PATH              Interactive REPL
    swcraft-agent chat --project PATH --script FILE  Scripted mode

``--scoped`` (off by default) has the LLM propose a :class:`~swcraft_agent.scope_proposal.ScopeProposal` each
turn; once the user approves it, the turn's own MCP server carries it as ``SWCRAFT_APPROVED_SCOPE`` and the
editing engine refuses every operation outside it, before anything is staged.

With or without the flag, every server runs in a reviewing ``SWCRAFT_REVIEW`` mode: a change set is applied only
after the user approves the staged delta; the scope never substitutes for that review. A REPL run uses ``panel``
(answered in the SWCraft review tab of a VS Code window open on the project; Ctrl-C ends the agent and withdraws
the question; a scope proposal is still answered in the terminal); a ``--script`` run uses ``elicit`` and answers
from each turn's ``approvals`` list, so a replay never blocks on stdin and approves only what the scenario file
wrote down.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from swcraft_tools import __version__
from swcraft_tools.env import force_utf8_streams, load_env_file
from swcraft_tools.yaml_load import load_yaml_document

from swcraft_agent.approval import Approver, ScriptedApprover, StdinApprover

#: An unknown key is rejected: a mistyped ``aprovals`` would silently make a turn that approves nothing.
SCRIPT_TURN_KEYS = frozenset({"user", "approvals"})


@dataclass(frozen=True)
class ScriptTurn:
    """One turn of a scenario script.

    ``approvals`` are consumed in order by that turn's prompts (in a scoped mutation turn: the scope, then each
    change set's exact delta, then the safety question where the delta weakens a check) and never carry into
    the next turn.
    """

    user: str
    approvals: tuple[str, ...] = ()


def load_script(script_path: str) -> list[ScriptTurn]:
    """Parse a YAML scenario script into its turns.

    Two turn forms are accepted:

    * a plain string, which approves nothing;
    * a ``{user, approvals}`` mapping, where ``approvals`` is an optional list
      of answer strings.

    Raises:
        FileNotFoundError: If ``script_path`` does not exist.
        ValueError: If the document is not a list, or a mapping turn carries an
            unknown key or no ``user``.
        TypeError: If a turn is neither form, ``user`` is not a string, or
            ``approvals`` is not a list of strings (a bare YAML ``yes`` is a bool, not a denial).
    """
    path = Path(script_path)
    if not path.is_file():
        raise FileNotFoundError(f"Script file not found: {script_path}")

    document = load_yaml_document(path.read_text(encoding="utf-8"))

    if not isinstance(document, list):
        raise ValueError(f"Script file must contain a YAML list of turns, got {type(document).__name__}: {script_path}")
    return [_parse_turn(index, raw) for index, raw in enumerate(document)]


def _parse_turn(index: int, raw: object) -> ScriptTurn:
    """One scenario turn, in either accepted form."""
    if isinstance(raw, str):
        return ScriptTurn(user=raw)
    if not isinstance(raw, Mapping):
        raise TypeError(
            f"Script turn {index} must be a string or a {{user, approvals}} mapping, got {type(raw).__name__}: {raw!r}"
        )

    unknown = sorted(set(raw) - SCRIPT_TURN_KEYS)
    if unknown:
        raise ValueError(
            f"Script turn {index} has unknown key(s) {', '.join(unknown)}: allowed keys are user, approvals"
        )
    if "user" not in raw:
        raise ValueError(f"Script turn {index} must have a 'user' key: {raw!r}")

    user = raw["user"]
    if not isinstance(user, str):
        raise TypeError(f"Script turn {index}: 'user' must be a string, got {type(user).__name__}: {user!r}")

    approvals = raw.get("approvals", ())
    if isinstance(approvals, str) or not isinstance(approvals, Sequence):
        raise TypeError(
            f"Script turn {index}: 'approvals' must be a list of strings, got {type(approvals).__name__}: {approvals!r}"
        )
    for position, answer in enumerate(approvals):
        if not isinstance(answer, str):
            raise TypeError(
                f"Script turn {index}: approval {position} must be a string, "
                f"got {type(answer).__name__}: {answer!r} (a bare YAML 'yes' is a bool - quote it)"
            )
    return ScriptTurn(user=user, approvals=tuple(approvals))


def run_script(agent, script_path: str) -> list[tuple[str, str]]:
    """Load a YAML scenario script and feed each turn to the agent.

    Each turn gets a fresh :class:`~swcraft_agent.approval.ScriptedApprover` holding its ``approvals``, so a
    replay never reads stdin and a turn approves only what its own entry wrote down.

    Returns:
        The ``(user_turn, agent_reply)`` pairs in order.

    Raises:
        FileNotFoundError: If ``script_path`` does not exist.
        ValueError: If the file is not a valid scenario script.
        RuntimeError: If a turn aborts (e.g. the tool-use round backstop fires); the run stops at that turn,
            naming its index and text.
    """
    turns = load_script(script_path)

    transcript: list[tuple[str, str]] = []
    for i, turn in enumerate(turns):
        agent.approver = ScriptedApprover(turn.approvals)
        try:
            reply = agent.chat(turn.user)
        except RuntimeError as exc:
            raise RuntimeError(f"Script aborted at turn {i} ({turn.user!r}): {exc}") from exc
        transcript.append((turn.user, reply))
    return transcript


PANEL_REVIEW_HINT = (
    "Change sets are reviewed in the SWCraft review tab in VS Code: keep a VS Code window open on this "
    "project with the SWCraft extension; each one waits until you answer it there; Ctrl-C ends the agent "
    "and withdraws it."
)


def review_mode(script: str | None) -> str:
    """The ``SWCRAFT_REVIEW`` value this run's servers carry: ``elicit`` for a ``--script`` replay, else ``panel``."""
    from swcraft_agent.core import REVIEW_ELICIT, REVIEW_PANEL

    return REVIEW_ELICIT if script else REVIEW_PANEL


def make_approver(script: str | None) -> Approver:
    """The approval channel for this run.

    A ``--script`` run's approver starts empty (``run_script`` installs each turn's answers), so anything asked
    before the first turn is denied; a REPL run asks the terminal user.
    """
    return ScriptedApprover() if script else StdinApprover()


def _cmd_chat(args: argparse.Namespace) -> None:
    """Run the agent in REPL or scripted mode.

    Unscoped, one long-lived client serves the session (``review_server_env()``). Scoped, each turn spawns its
    own server, because a scope approved mid-conversation cannot reach the environment of a process spawned
    before it. The approver is shared by the client (review elicitations) and the agent (scope proposals).
    """
    from swcraft_agent.core import REVIEW_PANEL, SwcraftAgent, review_server_env
    from swcraft_agent.mcp_client import SwcraftMcpClient

    approver = make_approver(args.script)
    review = review_mode(args.script)

    def run(agent: SwcraftAgent) -> None:
        if args.script:
            transcript = run_script(agent, args.script)
            for user_turn, reply in transcript:
                print(f"User: {user_turn}")
                print(f"Agent: {reply}")
                print()
            return
        print("SWCraft Agent ready. Type your request or Ctrl-D to exit.")
        if review == REVIEW_PANEL:
            # A change set waits silently in the review tab: say where the question is asked.
            print(PANEL_REVIEW_HINT)
        try:
            for line in sys.stdin:
                line = line.rstrip("\n")
                if line.lower() in ("exit", "quit"):
                    break
                if not line.strip():
                    continue
                try:
                    reply = agent.chat(line)
                except RuntimeError as exc:
                    # e.g. the MAX_TOOL_ROUNDS backstop: keep the session and its history alive.
                    print(f"Error: {exc}", file=sys.stderr)
                    print()
                    continue
                print(f"Agent: {reply}")
                print()
        except EOFError:
            pass

    if args.scoped:
        run(
            SwcraftAgent(
                project_path=args.project,
                scoped=True,
                approver=approver,
                review=review,
            )
        )
        return

    with SwcraftMcpClient(args.project, env=review_server_env(review=review), approver=approver) as mcp:
        run(
            SwcraftAgent(
                project_path=args.project,
                mcp=mcp,
                scoped=False,
                approver=approver,
                review=review,
            )
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swcraft-agent",
        description="SWCraft LLM architecture agent",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"swcraft-agent {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")

    chat_parser = subparsers.add_parser("chat", help="Chat with the SWCraft agent")
    chat_parser.add_argument(
        "--project",
        required=True,
        metavar="PATH",
        help="Path to swcraft.yaml manifest",
    )
    chat_parser.add_argument(
        "--script",
        default=None,
        metavar="FILE",
        help="Path to a YAML scenario script (user turns, optionally with approvals)",
    )
    chat_parser.add_argument(
        "--scoped",
        action="store_true",
        help=(
            "Require an approved task scope for every mutation: the turn proposes a scope, "
            "you approve it, changes are made in an isolated copy, and the resulting diff is "
            "checked against that scope and shown before anything is committed"
        ),
    )

    return parser


def main() -> None:
    """Entry point for the ``swcraft-agent`` CLI command."""
    # First, before anything prints: the model's replies may hold any character, and a redirected transcript
    # on a non-UTF-8 host would fail on one.
    force_utf8_streams()
    load_env_file()
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "chat":
        _cmd_chat(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
