"""The trace link search's import boundaries, pinned rather than described.

``test_allocation_boundaries.py``'s pattern: each rule is a pure function over a parsed module, checked
against the real modules and shown to **fire** against a synthetic offender -- a guard never seen to fail
proves nothing.

**The rule.** Neither ``trace_search.py`` nor ``experiment_frame.py`` imports ``writer``, ``editing``,
``allocation`` or ``scipy``: a search writes nothing into the model, the second experiment kind never
depends on the first (the kind-independent pieces live in ``experiment_frame.py``), and the search needs no
solver. The last test witnesses that ``generate arxml`` and ``validate`` over ``examples/basic``, each in a
child interpreter, load neither module.

**What these do not reach:** an import spelled through ``importlib`` or ``__import__`` with a computed name
(the raw-text check refuses the two names outright in both modules), and a module the two import that
itself imports one of the four -- their own imports are the boundary stated, not their closure.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from swcraft_tools import experiment_frame, trace_search

MODULES = {"trace_search": Path(trace_search.__file__), "experiment_frame": Path(experiment_frame.__file__)}
REPO_ROOT = Path(__file__).resolve().parents[3]
FORBIDDEN = ("swcraft_tools.writer", "swcraft_tools.editing", "swcraft_tools.allocation", "scipy")


def _imported_modules(tree: ast.Module) -> set[str]:
    """Every module name *tree* imports, absolute and relative (a relative one spelled from the package)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = f"swcraft_tools.{base}" if base else "swcraft_tools"
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def forbidden_imports(tree: ast.Module) -> list[str]:
    return sorted(
        name for name in _imported_modules(tree) for root in FORBIDDEN if name == root or name.startswith(f"{root}.")
    )


@pytest.mark.parametrize("module", sorted(MODULES))
def test_neither_module_imports_a_forbidden_one(module: str) -> None:
    assert forbidden_imports(ast.parse(MODULES[module].read_text(encoding="utf-8"))) == []


@pytest.mark.parametrize("module", sorted(MODULES))
def test_no_dynamic_import_is_spelled(module: str) -> None:
    text = MODULES[module].read_text(encoding="utf-8")
    assert "importlib" not in text and "__import__" not in text


def test_the_check_fires_on_an_offender() -> None:
    offender = ast.parse(
        "from swcraft_tools import allocation\n"
        "from .writer.mutations import swc_to_dict\n"
        "import scipy.optimize\n"
        "from swcraft_tools.editing import engine\n"
        "import os\n"
    )
    assert forbidden_imports(offender) == [
        "scipy.optimize",
        "swcraft_tools.allocation",
        "swcraft_tools.editing",
        "swcraft_tools.editing.engine",
        "swcraft_tools.writer.mutations",
        "swcraft_tools.writer.mutations.swc_to_dict",
    ]


def test_the_arxml_path_never_loads_either_module(tmp_path: Path) -> None:
    project = tmp_path / "basic"
    shutil.copytree(REPO_ROOT / "examples" / "basic", project, ignore=shutil.ignore_patterns("dist", "views"))
    manifest = str(project / "swcraft.yaml")
    probe = (
        "import json, sys\n"
        "from swcraft_tools.cli import main\n"
        "sys.argv = ['swcraft', *sys.argv[1:]]\n"
        "code = 0\n"
        "try:\n"
        "    main()\n"
        "except SystemExit as exc:\n"
        "    code = exc.code or 0\n"
        "names = ('swcraft_tools.trace_search', 'swcraft_tools.experiment_frame')\n"
        "print('EXIT-CODE ' + str(code))\n"
        "print('LOADED ' + json.dumps(sorted(name for name in names if name in sys.modules)))\n"
    )
    for argv in (["generate", "arxml", "--project", manifest], ["validate", "--project", manifest]):
        completed = subprocess.run(
            [sys.executable, "-c", probe, *argv], capture_output=True, text=True, encoding="utf-8", timeout=300
        )
        lines = completed.stdout.splitlines()
        assert "EXIT-CODE 0" in lines, completed.stderr
        assert "LOADED []" in lines, completed.stdout
