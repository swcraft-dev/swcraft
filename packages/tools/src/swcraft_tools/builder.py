"""Run an architecture project's build command and parse its validator output.

Public API: ``run_build(project_root, command) -> BuildResult`` and
``parse_moon_errors(output) -> list[BuildError]``.

The MCP server reaches this module through the opt-in ``SWCRAFT_MCP_BUILD_COMMAND`` /
``SWCRAFT_MCP_VALIDATE_COMMAND`` environment variables, for a model that lives in a standalone
architecture project. The ``"moon run :build"`` default names that project's build (``swcraft init
--integration moon`` scaffolds it), not SWCraft's; any command that surfaces the validator's
output works the same way. The command string is split with ``shlex``.

The validator reports a broken ARXML file as ``INVALID (structural mode): <path>`` or
``INVALID (XSD mode): <path>``; a task runner may prefix the line with ``<task_name> |``, and
both forms are matched.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass, field


@dataclass
class BuildError:
    """A single validation or build error extracted from a build command's output."""

    message: str
    path: str = ""


@dataclass
class BuildResult:
    """Result of a build-command invocation."""

    success: bool
    errors: list[BuildError] = field(default_factory=list)
    output: str = ""


# Matches both the raw and the task-runner-prefixed form (``validate-arxml | INVALID ...``).
_INVALID_PATTERN = re.compile(r"(?:\S+\s+\|\s+)?INVALID\s+\((structural|XSD)\s+mode\):\s+(.+)")


def parse_moon_errors(output: str) -> list[BuildError]:
    """Parse a build command's output and return a list of BuildErrors.

    Returns:
        One BuildError per matched INVALID line; empty when none matched.
    """
    errors: list[BuildError] = []
    for line in output.splitlines():
        m = _INVALID_PATTERN.search(line)
        if m:
            mode = m.group(1)
            path = m.group(2).strip()
            message = f"INVALID ({mode} mode): {path}"
            errors.append(BuildError(message=message, path=path))
    return errors


def run_build(
    project_root: str,
    command: str = "moon run :build",
) -> BuildResult:
    """Invoke an architecture project's build command and return a BuildResult.

    Args:
        project_root: The architecture project's root, not SWCraft's.
        command: Shell-style command string, split with ``shlex``.

    Returns:
        BuildResult with ``success=True`` when the exit code is 0.
    """
    args = shlex.split(command)
    # `text=True` alone decodes with the locale's codec (cp1252 on Windows), which corrupts
    # or raises on the build errors an agent quotes back to the user.
    completed = subprocess.run(
        args,
        cwd=project_root,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    combined = completed.stdout + completed.stderr
    errors = parse_moon_errors(combined)
    return BuildResult(
        success=completed.returncode == 0,
        errors=errors,
        output=combined,
    )
