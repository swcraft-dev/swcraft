"""Process-environment helpers for SWCraft CLI entry points.

:func:`load_env_file` reads the nearest ``.env``; :func:`force_utf8_streams` decides how this
process encodes what it prints; :func:`checkout_root` answers whether this code runs from a SWCraft
source checkout. A leaf that imports nothing from ``swcraft_tools``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

#: Keys this process took from a ``.env`` file, accumulated across every load.
#:
#: A ``.env`` is file input from a project directory that can arrive from anywhere, and it
#: becomes indistinguishable from the shell's exports once loaded; that matters wherever a value
#: decides which program gets launched (``BROWSER`` is a command line ``webbrowser`` runs).
#: Recorded rather than filtered, because the shipped ``.env.example`` documents keys outside
#: the product's own namespace (``AZURE_OPENAI_*``).
_ENV_FILE_KEYS: set[str] = set()


def env_file_keys() -> frozenset[str]:
    """The environment keys a ``.env`` file introduced, not the ones the shell had.

    A caller that must not take a value from an unread file drops these for the duration of the call.

    What this does **not** reach: a value the shell exported is trusted, because whoever exported it
    already runs this process; and a key the file set to the value the environment already had is
    not recorded.
    """
    return frozenset(_ENV_FILE_KEYS)


#: The project manifest's file name, the bound of the ``.env`` search; not imported from
#: ``project`` so this module stays a leaf.
_MANIFEST_FILENAME = "swcraft.yaml"


def checkout_root(module_file: str = __file__) -> Path | None:
    """The root of the SWCraft source checkout this module runs from, or ``None``.

    The root is four levels above ``packages/tools/src/swcraft_tools/env.py`` and holds both
    ``scripts/dev.py`` and ``packages/tools/pyproject.toml``; an installed package answers ``None``.
    *module_file* exists for tests; callers pass nothing.

    What this does not reach: nothing checks that a directory with the two marker files is the
    checkout this module came from.
    """
    module_path = Path(module_file).resolve()
    if module_path.parent.parts[-4:] != ("packages", "tools", "src", "swcraft_tools"):
        return None
    root = module_path.parents[4]
    if (root / "scripts" / "dev.py").is_file() and (root / "packages" / "tools" / "pyproject.toml").is_file():
        return root
    return None


def _candidate_dirs() -> list[Path]:
    """The directories :func:`load_env_file` looks in, nearest first."""
    cwd = Path.cwd().resolve()
    lineage = [cwd, *cwd.parents]
    project_root = next((directory for directory in lineage if (directory / _MANIFEST_FILENAME).is_file()), None)
    dirs = lineage[: lineage.index(project_root) + 1] if project_root is not None else [cwd]
    root = checkout_root()
    if root is not None and root not in dirs:
        dirs.append(root)
    return dirs


def load_env_file(filename: str = ".env") -> Path | None:
    """Load the nearest SWCraft env file, if there is one, and return its path.

    The search runs from the working directory up to and including the project root (the nearest
    directory holding ``swcraft.yaml``), or covers the working directory alone when there is none;
    then, in a source checkout, the checkout root. The first file found wins. Existing process
    environment variables take priority over file values; the keys the file introduced are recorded
    in :func:`env_file_keys`.

    What this does not reach: the search is keyed on the working directory, never on ``--project``
    (the file can itself set ``SWCRAFT_MANIFEST``); and a manifest under another name does not bound
    the walk.
    """
    for directory in _candidate_dirs():
        env_path = directory / filename
        if env_path.is_file():
            before = set(os.environ)
            load_dotenv(dotenv_path=env_path, override=False)
            _ENV_FILE_KEYS.update(set(os.environ) - before)
            return env_path
    return None


def force_utf8_streams() -> None:
    """Encode this process's ``stdout`` and ``stderr`` as UTF-8, whatever the locale is.

    On Windows a stream redirected into a file or a pipe has no console behind it and falls back to
    the locale codec (``cp1252``), which mangles the product's non-ASCII punctuation; naming the
    encoding on the stream fixes it. ``errors="replace"`` because a CLI that raises while printing an
    error message is worse than one that prints an approximation.

    **What this does not reach.**

    - A stream with no ``reconfigure`` (anything that is not a ``TextIOWrapper``) is left as it was.
    - ``sys.stdin``: ``swcraft guard`` reads bytes from ``sys.stdin.buffer`` and decodes them itself.
    - The MCP server, which does not call this: its ``stdout`` is the JSON-RPC transport.
    - Any child process, which resolves its own streams from its own environment.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):  # pragma: no cover - platform-dependent
            continue
