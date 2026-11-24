"""The one way an integration test starts the specimen generator: `generator_command`.

`SWCRAFT_SPECIMENS_BIN`, when set, names the `swcraft-specimens` executable to start, so SWCraft's
archive test runs this suite against the installed wheel, never the editable source. Unset, the
command is the caller's fallback, else this interpreter's `python -m swcraft_specimens`.

When `SWCRAFT_SPECIMENS_START_LOG` names a file, every start is logged there as one JSON line (the
test's id and the command); the archive test reads it to show no case started `-m swcraft_specimens`.

What this does not reach: a start that does not call it -- the fast tests that start the module on
purpose, and the in-process `generate_project` calls, which start no process.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence

BIN_VARIABLE = "SWCRAFT_SPECIMENS_BIN"

START_LOG_VARIABLE = "SWCRAFT_SPECIMENS_START_LOG"

MODULE_COMMAND = (sys.executable, "-m", "swcraft_specimens")


def generator_command(fallback: Sequence[str] | None = None) -> list[str]:
    """The generator to start, logged: `[$SWCRAFT_SPECIMENS_BIN]`, else *fallback*, else the module."""
    configured = os.environ.get(BIN_VARIABLE)
    if configured:
        command = [configured]
    elif fallback is not None:
        command = list(fallback)
    else:
        command = list(MODULE_COMMAND)
    log = os.environ.get(START_LOG_VARIABLE)
    if log:
        line = json.dumps({"test": os.environ.get("PYTEST_CURRENT_TEST", ""), "command": command})
        with open(log, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")
    return command
