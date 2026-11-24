"""The ``pythonpath`` a project declares for pytest, read for an interpreter no pytest configuration reaches.

A test module run as a script (``test_diagram_generation.py --freeze``) has only its own directory on
``sys.path``, so this module lives beside ``conftest.py`` rather than in ``support/``, the directory it
puts on the path. It imports the standard library alone.
"""

from __future__ import annotations

import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path


def declared_pythonpath(project_root: Path) -> list[Path]:
    """The directories *project_root*'s ``pyproject.toml`` declares as pytest's ``pythonpath``, resolved.

    Every project with tests declares the key, so its absence is a ``KeyError``, never an empty list.
    """
    text = (project_root / "pyproject.toml").read_text(encoding="utf-8")
    declared = tomllib.loads(text)["tool"]["pytest"]["ini_options"]["pythonpath"]
    return [(project_root / entry).resolve() for entry in declared]


def absent_from_sys_path(directories: Iterable[Path]) -> list[str]:
    """Each of *directories* that ``sys.path`` lacks, compared resolved, as the string ``sys.path`` holds."""
    present = {Path(entry).resolve() for entry in sys.path if entry}
    return [str(directory) for directory in directories if directory.resolve() not in present]
