"""Run ruff check + ruff format --check for one Python project, and mypy where it declares it.

Runs each tool directly, without a shell: Windows PowerShell 5.x has no ``&&``. Exits non-zero if
any check fails. Usage: ``run_python_lint.py [--mypy-config FILE] [PATH ...]``.

mypy runs when the working directory's ``pyproject.toml`` has a ``[tool.mypy]`` table, or with the
configuration file ``--mypy-config`` names (``scripts/``, which has no project), with a fresh
temporary ``--cache-dir`` so the check never depends on an earlier tree state; the ruff arguments do
not reach it. Not reached: a coded ``# type: ignore[code]`` on a line, the modules a configuration
exempts by name, and the tests of every project but the specimen generator.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


def declares_mypy(directory: Path) -> bool:
    """Whether *directory*'s ``pyproject.toml`` has a ``[tool.mypy]`` table."""
    pyproject = directory / "pyproject.toml"
    if not pyproject.is_file():
        return False
    document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return "mypy" in document.get("tool", {})


def split_mypy_config(args: list[str]) -> tuple[str | None, list[str]]:
    """The configuration file ``--mypy-config FILE`` names, anywhere in *args*, and the remaining arguments.

    A ``--mypy-config`` with no value exits with a usage error.
    """
    parser = argparse.ArgumentParser(prog="run_python_lint.py", add_help=False, allow_abbrev=False)
    parser.add_argument("--mypy-config")
    known, rest = parser.parse_known_args(args)
    return known.mypy_config, rest or ["."]


def main(args: list[str]) -> int:
    config, args = split_mypy_config(args)
    check_result = subprocess.run([sys.executable, "-m", "ruff", "check", *args])
    format_result = subprocess.run([sys.executable, "-m", "ruff", "format", "--check", *args])
    mypy_returncode = 0
    if config is not None or declares_mypy(Path.cwd()):
        configured = ["--config-file", config] if config is not None else []
        with tempfile.TemporaryDirectory(prefix="run-python-lint-mypy-", ignore_cleanup_errors=True) as cache:
            mypy_returncode = subprocess.run(
                [sys.executable, "-m", "mypy", *configured, "--cache-dir", cache]
            ).returncode
    for returncode in (check_result.returncode, format_result.returncode, mypy_returncode):
        if returncode != 0:
            return returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
