"""Run the ``swcraft`` entry point in process and capture what it answered.

:func:`run_swcraft` patches ``sys.argv`` through the caller's ``monkeypatch``, calls
``swcraft_tools.cli.main`` and returns the exit code with the captured stdout and stderr; a
``SystemExit`` (argparse's usage error included) is read as its code, a return as ``0``.
"""

from __future__ import annotations

from typing import Any

from swcraft_tools.cli import main


def run_swcraft(argv: list[str], monkeypatch: Any, capsys: Any) -> tuple[int, str, str]:
    """``swcraft *argv*``: its exit code, stdout and stderr."""
    monkeypatch.setattr("sys.argv", ["swcraft", *argv])
    capsys.readouterr()
    code = 0
    try:
        main()
    except SystemExit as exited:
        code = int(exited.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err
