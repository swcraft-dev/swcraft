"""Two helpers the component-exchange suites share.

:func:`init_project` scaffolds a fresh project through the real ``swcraft init``
entry point, with ``sys.argv`` patched for the call only; :func:`files_of` is a
tree's bytes by POSIX-relative path, the "nothing written" oracle.

What they do **not** reach: the older suites that define their own copy of either
helper keep it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from swcraft_tools.cli import main


def init_project(target: Path) -> Path:
    """Scaffold a fresh project at *target* through ``swcraft init``; returns *target*."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("sys.argv", ["swcraft", "init", str(target)])
        main()
    return target


def files_of(root: Path) -> dict[str, bytes]:
    """Every file under *root*, by its POSIX path relative to *root*, in a stable order."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }
