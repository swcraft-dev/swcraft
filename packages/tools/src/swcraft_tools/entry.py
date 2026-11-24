"""The ``swcraft`` console script: ``swcraft guard`` without the ``cli`` package's imports, every other run through it.

The clients' tool-call hooks spawn ``swcraft guard [--host <host>]`` before every tool call, so its
start-up cost is paid per call, while the ``cli`` package imports every command's modules eagerly.
An argument list that is exactly ``guard`` or ``guard --host <host>`` for a host of
:data:`swcraft_tools.copilot_guard.HOSTS` goes to :func:`swcraft_tools.copilot_guard.run_hook` and
exits with its code when that is not 0 (only a Kiro denial, exit 2). Every other argument list,
``swcraft guard --help`` and an unknown host included, is :func:`swcraft_tools.cli.main`'s, so a usage
error is argparse's.

What this does not reach: ``python -m swcraft_tools.cli guard`` and the ``--host=codex`` spelling,
which pay the ``cli`` package's imports and answer the same way.
"""

from __future__ import annotations

import sys

from swcraft_tools.env import force_utf8_streams


def main() -> None:
    """Entry point for the ``swcraft`` console script."""
    # First, before anything can print.
    force_utf8_streams()
    arguments = tuple(sys.argv[1:])
    if arguments[:1] == ("guard",):
        from swcraft_tools.copilot_guard import HOSTS, run_hook  # noqa: PLC0415 -- start-up: the entry point keeps the guard hook and the CLI off each other's imports

        if arguments == ("guard",):
            if code := run_hook(HOSTS[0]):
                sys.exit(code)
            return
        if len(arguments) == 3 and arguments[1] == "--host" and arguments[2] in HOSTS:
            if code := run_hook(arguments[2]):
                sys.exit(code)
            return
    from swcraft_tools.cli import main as cli_main  # noqa: PLC0415 -- start-up: the entry point keeps the guard hook and the CLI off each other's imports

    cli_main()


if __name__ == "__main__":
    main()
