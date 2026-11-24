"""The diagram layout bridge: graphs in, laid-out graphs out, through one Node process.

Eclipse ELK (``elkjs``, package data under ``elkjs/``) computes every layout. This is the only
Python module that knows Node exists: it hands one JSON array of ELK graphs to ``elk-layout.mjs``
on stdin and reads the laid-out graphs back in the same order. One ``node --version`` first refuses
a Node below :data:`NODE_FLOOR` by name; an empty list spawns nothing.

Which Node: the installation's own (beside ``sys.executable``) first, then ``PATH``'s. Every float in the answer is
rounded to two decimals, so platform differences never reach a rendered byte. The version is the last stdout line
of the form ``vX.Y.Z`` and the answer follows the line :data:`ANSWER_MARKER`, so a version manager's shim may print
lines of its own; output other than the answer line never causes a refusal by its encoding. Both runs are bounded
by a timeout.

A refusal's shared sentence names no location and carries only the script's own stderr lines
(``graph <index>: ...``, ``the layout input ...``). Not reached: the layout's quality, whether
``elkjs`` holds the pinned bytes (only one file's presence is checked), a shim writing into the
answer line or printing the marker itself; on Windows a ``.cmd`` wrapper that starts Node as its own process keeps
the pipes open after the kill, so the refusal comes only when that Node exits; on POSIX such a Node is left running.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

#: The Node script, resolved from ``__file__`` because Node needs a filesystem path.
LAYOUT_SCRIPT = Path(__file__).with_name("elk-layout.mjs")

#: The ``elkjs`` file the script imports, relative to the script's directory.
LAYOUT_ENGINE_ENTRY = ("elkjs", "lib", "main.js")

#: How long ``node --version`` may take.
VERSION_TIMEOUT_SECONDS = 60

#: How long the one layout run may take: a base, and a share per graph it lays out.
LAYOUT_TIMEOUT_BASE_SECONDS = 120
LAYOUT_TIMEOUT_PER_GRAPH_SECONDS = 5

#: The stdout line the script writes immediately before its one-line answer.
ANSWER_MARKER = "swcraft-diagram-layout-answer"

#: The lowest Node.js release the bridge runs on: the script's requirement (``elkjs`` declares no
#: ``engines``). The script awaits at the top level of an ES module, which Node runs unflagged
#: from 14.8.0; 14.7.0 fails on it. Releases between the tested ones are not measured.
NODE_FLOOR: tuple[int, int, int] = (14, 8, 0)

_VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)\s*$", re.MULTILINE)

#: The stderr lines ``elk-layout.mjs`` writes itself, the only ones the shared sentence carries.
_SCRIPT_MESSAGE_RE = re.compile(r"^(?:graph \d+: |the layout input ).*$", re.MULTILINE)


class DiagramLayoutError(Exception):
    """The layout could not be computed; the message says why.

    ``str()`` names no location, because an MCP reply carries it; ``render(full_path=True)`` is a
    local terminal's sentence (*located*) and names the Node found.
    """

    def __init__(self, message: str, *, located: str | None = None) -> None:
        super().__init__(message)
        self.located = message if located is None else located

    def render(self, *, full_path: bool = False) -> str:
        """The refusal naming no location, or naming what was found for a local terminal."""
        return self.located if full_path else str(self)


def _reason(error: OSError) -> str:
    """An ``OSError``'s errno and reason without the file it names (``rendering.path_free``'s shape)."""
    if error.strerror is None:
        return type(error).__name__
    return error.strerror if error.errno is None else f"[Errno {error.errno}] {error.strerror}"


def _version_text(parts: Sequence[int]) -> str:
    return ".".join(str(part) for part in parts)


def _start_refusal(node: str, error: OSError) -> DiagramLayoutError:
    """The refusal for a Node that could not be started: the reason shared, the path the terminal's."""
    return DiagramLayoutError(
        f"Node.js could not be started: {_reason(error)}",
        located=f"Node.js at {node} could not be started: {error}",
    )


def installation_node(executable: str | None = None, *, platform: str = os.name) -> Path:
    """Where the workstation installer places ``node`` for the interpreter *executable*.

    *executable* is not resolved: a venv's ``python`` is a link out of the directory, and the
    directory is the answer.
    """
    interpreter = Path(sys.executable if executable is None else executable)
    return interpreter.parent / ("node.exe" if platform == "nt" else "node")


def resolve_node(
    *,
    executable: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    platform: str = os.name,
) -> str:
    """The Node the layout runs: the installation's own, then ``PATH``'s, or a refusal."""
    installed = installation_node(executable, platform=platform)
    if installed.is_file():
        return str(installed)
    found = which("node")
    if found:
        return found
    advice = (
        f"Diagrams are laid out by Eclipse ELK, which runs in Node.js {_version_text(NODE_FLOOR)} or later; "
        "install the SWCraft workstation archive, which carries one, or put node on PATH."
    )
    raise DiagramLayoutError(
        f"Node.js was not found beside the SWCraft installation's interpreter or on PATH. {advice}",
        located=(
            f"Node.js was not found: there is no {installed.name} beside the interpreter ({installed.parent}) "
            f"and no node on PATH. {advice}"
        ),
    )


def node_version(node: str) -> tuple[int, int, int]:
    """``node --version`` as three integers, or a refusal naming what came back."""
    try:
        completed = subprocess.run(
            [node, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=VERSION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        answer = f"did not answer `node --version` within {VERSION_TIMEOUT_SECONDS} s."
        raise DiagramLayoutError(f"Node.js {answer}", located=f"Node.js at {node} {answer}") from error
    except OSError as error:
        raise _start_refusal(node, error) from error
    # Decoded with replacement: a shim's notice in a Windows code page is read past, never refused.
    printed = (completed.stdout or b"").decode("utf-8", errors="replace")
    matches = list(_VERSION_RE.finditer(printed))
    match = matches[-1] if matches else None
    if completed.returncode != 0 or match is None:
        # What came back is the terminal's alone: a shim can print a path, and the shared sentence
        # is an MCP reply's.
        answer = f"did not report a version `node --version` can be read from (exit {completed.returncode}"
        raise DiagramLayoutError(
            f"Node.js {answer}).",
            located=f"Node.js at {node} {answer}, output {printed!r}).",
        )
    major, minor, patch = (int(group) for group in match.groups())
    return major, minor, patch


def _refuse_a_missing_engine() -> None:
    """The refusal, by name, when the script or the ``elkjs`` file it imports is not installed."""
    engine = LAYOUT_SCRIPT.parent.joinpath(*LAYOUT_ENGINE_ENTRY)
    if LAYOUT_SCRIPT.is_file() and engine.is_file():
        return
    missing = LAYOUT_SCRIPT if not LAYOUT_SCRIPT.is_file() else engine
    advice = (
        "the diagram layout engine (elkjs) is not installed beside the SWCraft tools. An installed "
        "toolchain carries it; a source checkout stages it with `python scripts/dev.py install`."
    )
    raise DiagramLayoutError(
        f"No diagram was laid out: {advice}", located=f"No diagram was laid out: {missing} is absent; {advice}"
    )


def round_coordinates(value: Any) -> Any:
    """*value* with every ``float`` rounded to two decimals, recursively."""
    if isinstance(value, float):
        rounded = round(value, 2)
        return 0.0 if rounded == 0 else rounded
    if isinstance(value, dict):
        return {key: round_coordinates(item) for key, item in value.items()}
    if isinstance(value, list):
        return [round_coordinates(item) for item in value]
    return value


def layout(graphs: Sequence[Mapping[str, Any]], *, node: str | None = None) -> list[dict[str, Any]]:
    """*graphs* laid out with ELK in one Node process; the answers in the same order.

    *node* overrides :func:`resolve_node`. An empty *graphs* starts no process.
    """
    if not graphs:
        return []
    # Encoded before any process: text UTF-8 cannot hold is refused while no process exists.
    document = json.dumps(list(graphs), ensure_ascii=False).encode("utf-8")
    _refuse_a_missing_engine()
    executable = resolve_node() if node is None else node
    found = node_version(executable)
    if found < NODE_FLOOR:
        verdict = (
            f"is {_version_text(found)}, below {_version_text(NODE_FLOOR)}, the lowest release the diagram "
            "layout runs on. Install the SWCraft workstation archive, which carries its own, or put Node.js "
            f"{_version_text(NODE_FLOOR)} or later on PATH."
        )
        raise DiagramLayoutError(f"Node.js {verdict}", located=f"Node.js at {executable} {verdict}")
    timeout = LAYOUT_TIMEOUT_BASE_SECONDS + LAYOUT_TIMEOUT_PER_GRAPH_SECONDS * len(graphs)
    try:
        completed = subprocess.run(
            [executable, str(LAYOUT_SCRIPT)],
            input=document,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise DiagramLayoutError(
            f"the diagram layout did not finish within {timeout} s ({len(graphs)} graphs); nothing was written."
        ) from error
    except OSError as error:
        raise _start_refusal(executable, error) from error
    if completed.returncode != 0:
        stderr = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        own = _SCRIPT_MESSAGE_RE.search(stderr)
        shared = own.group(0) if own else "the layout engine stopped without a message of its own"
        raise DiagramLayoutError(
            f"the diagram layout failed (exit {completed.returncode}): {shared}",
            located=f"the diagram layout failed (exit {completed.returncode}): {stderr or 'no message'}",
        )
    lines = (completed.stdout or b"").split(b"\n")
    marker = ANSWER_MARKER.encode("ascii")
    markers = [index for index, line in enumerate(lines) if line.strip() == marker]
    if not markers or markers[-1] + 1 >= len(lines):
        raise DiagramLayoutError("the diagram layout finished without writing its answer.")
    try:
        text = lines[markers[-1] + 1].decode("utf-8")
    except UnicodeDecodeError as error:
        raise DiagramLayoutError("the diagram layout answered with text that is not UTF-8") from error
    try:
        answer = json.loads(text)
    except json.JSONDecodeError as error:
        raise DiagramLayoutError(f"the diagram layout answered with something other than JSON: {error}") from error
    if not isinstance(answer, list) or len(answer) != len(graphs) or not all(isinstance(g, dict) for g in answer):
        raise DiagramLayoutError(
            f"the diagram layout answered with other than one laid-out graph per graph ({len(graphs)} given)."
        )
    return [round_coordinates(graph) for graph in answer]
