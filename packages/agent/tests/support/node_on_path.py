"""The real Node.js binary, found once per session under the real environment.

A module of its own because ``conftest.py`` is held to running ``git`` alone; ``conftest.py`` imports
``REAL_NODE_DIRECTORY``, so the probe runs at its import, before any fixture.
"""

import os
import shutil
import subprocess
from pathlib import Path

#: How long the one ``node -e`` probe of a session may take.
NODE_PROBE_TIMEOUT_SECONDS = 30


def node_directory_from_answer(returncode: int, answer: str) -> str | None:
    """The directory of the binary ``node -e "process.stdout.write(process.execPath)"`` named.

    ``None`` unless the probe exited 0 and answered the absolute path of an existing file:
    an empty or whitespace answer, a relative one, a directory or a missing path changes nothing.
    """
    if returncode != 0:
        return None
    candidate = Path(answer.strip())
    if not answer.strip() or not candidate.is_absolute() or not candidate.is_file():
        return None
    return os.fspath(candidate.parent)


def real_node_directory(which=shutil.which) -> str | None:
    """The directory of the Node.js binary ``PATH``'s ``node`` runs, or ``None``.

    A version manager's shim finds the real binary through the user's home, so it fails once a test's home is
    redirected; asked once under the real environment, Node names itself (``process.execPath``). ``None`` when
    there is no ``node`` or the probe cannot start, times out, fails or names no existing file.

    Not reached: a real binary that itself reads the home, and a server that uses the installation's own Node
    (``resolve_node`` prefers it). Every other program in that directory moves ahead on ``PATH`` with it.
    """
    node = which("node")
    if node is None:
        return None
    try:
        completed = subprocess.run(
            [node, "-e", "process.stdout.write(process.execPath)"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=NODE_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return node_directory_from_answer(completed.returncode, completed.stdout or "")


#: Computed at import, before any fixture redirects the home.
REAL_NODE_DIRECTORY = real_node_directory()
