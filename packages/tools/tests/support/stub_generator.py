"""A stub specimen generator, written at run time, for the tests of ``scripts/specimen_generator.py``.

The stub is a Python script run as ``[sys.executable, script, MODE, *ARGS]``; ``run`` appends
``specimen PASSPORT --out OUT``. No shell shim on either platform: a command list needs none, and the
script is written with ``newline="\\n"``. The modes:

- ``copy SOURCE`` -- copy the directory SOURCE's entries into OUT, exit 0;
- ``copy-unless NAME SOURCE`` -- ``reject 1 NAME`` when the passport's file name starts with
  ``NAME.``, else ``copy SOURCE`` (one command over several specimens, one of them rejected);
- ``reject N SENTINEL`` -- write N stderr lines, each carrying SENTINEL, plus one blank line, exit 2;
- ``exit CODE`` -- exit CODE, writing nothing;
- ``empty`` -- exit 0, writing nothing;
- ``sleep SECONDS`` -- sleep, then exit 0.

Every run first appends one JSON line to ``$STUB_RECORD`` (when that variable is set): its argv after
the script and its working directory -- the argv a test asserts on.

:func:`write_sh_wrapper` writes a one-line ``sh`` script that ``exec``s the stub, for the POSIX tests
that need a single executable file (``configured`` returns one path).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

STUB_SOURCE = """\
import json, os, shutil, sys, time
from pathlib import Path

argv = sys.argv[1:]
record = os.environ.get("STUB_RECORD")
if record:
    with open(record, "a", encoding="utf-8", newline="\\n") as stream:
        stream.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\\n")
split = argv.index("specimen")
own, rest = argv[:split], argv[split + 1 :]
out = Path(rest[rest.index("--out") + 1])
mode = own[0]
if mode == "copy-unless":
    if Path(rest[0]).name.startswith(own[1] + "."):
        mode, own = "reject", ["reject", "1", own[1]]
    else:
        mode, own = "copy", ["copy", own[2]]
if mode == "copy":
    out.mkdir(parents=True, exist_ok=True)
    shutil.copytree(own[1], out, dirs_exist_ok=True)
    sys.exit(0)
if mode == "reject":
    for index in range(int(own[1])):
        sys.stderr.write(f"refused: leaf.{index} (later-shape): {own[2]}\\n")
    sys.stderr.write("\\n")
    sys.exit(2)
if mode == "exit":
    sys.exit(int(own[1]))
if mode == "empty":
    sys.exit(0)
if mode == "sleep":
    time.sleep(float(own[1]))
    sys.exit(0)
sys.exit(f"unknown stub mode {mode}")
"""


def write_stub(directory: Path) -> Path:
    """Write the stub script into *directory* and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "stub_generator.py"
    script.write_text(STUB_SOURCE, encoding="utf-8", newline="\n")
    return script


def stub_command(script: Path, *arguments: str) -> list[str]:
    """``[sys.executable, script, *arguments]``: the command list ``run`` is given."""
    return [sys.executable, os.fspath(script), *arguments]


def write_sh_wrapper(path: Path, script: Path, *arguments: str) -> Path:
    """A one-line POSIX ``sh`` script at *path* that runs the stub with *arguments*; made executable."""
    quoted = " ".join(f"'{value}'" for value in (sys.executable, os.fspath(script), *arguments))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\nexec {quoted} "$@"\n', encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return path
