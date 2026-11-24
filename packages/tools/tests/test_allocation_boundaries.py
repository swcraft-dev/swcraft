"""The allocation leaf's two import boundaries and its lazy solver, pinned rather than described.

Each rule is a pure function over a parsed module, checked against the real ``allocation.py`` and shown
to fire against a synthetic offender -- a guard never seen to fail proves nothing.

**Rule 1 -- no ``writer``, no ``editing``.** An experiment changes nothing in the model, so the model
writers and the ARXML generation stay out of its reach.

**Rule 2 -- ``scipy`` is imported inside ``solve_curve`` and nowhere else**, so a ``swcraft`` without the
solver starts; ``generate arxml`` and ``validate`` are witnessed to finish with no ``scipy`` loaded.

**What these do not reach:** an import spelled through ``importlib`` or
``__import__`` with a computed name (the raw-text check refuses the two names
outright in this module), and a module ``allocation`` imports that itself
imports ``writer`` -- the leaf's own imports are the boundary stated, not their
closure.
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

from swcraft_tools import allocation

ALLOCATION = Path(allocation.__file__)
REPO_ROOT = Path(__file__).resolve().parents[3]
FORBIDDEN = ("swcraft_tools.writer", "swcraft_tools.editing")
SOLVER_FUNCTION = "solve_curve"


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


def _is_solver_import(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name == "scipy" or alias.name.startswith("scipy.") for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        return node.level == 0 and (module == "scipy" or module.startswith("scipy."))
    return False


def misplaced_solver_imports(tree: ast.Module) -> list[int]:
    """Line numbers of every ``scipy`` import outside the body of a top-level ``solve_curve``."""
    allowed: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == SOLVER_FUNCTION:
            allowed.update(id(inner) for inner in ast.walk(node))
    return sorted(node.lineno for node in ast.walk(tree) if _is_solver_import(node) and id(node) not in allowed)


def solver_imports(tree: ast.Module) -> list[int]:
    return sorted(node.lineno for node in ast.walk(tree) if _is_solver_import(node))


def _tree() -> ast.Module:
    return ast.parse(ALLOCATION.read_text(encoding="utf-8"))


class TestTheLeafImportsNoMutationSurface:
    def test_allocation_imports_neither_writer_nor_editing(self) -> None:
        assert forbidden_imports(_tree()) == []

    def test_the_check_fires_on_an_offender(self) -> None:
        offender = ast.parse(
            "from swcraft_tools.writer.mutations import swc_to_dict\nfrom .editing import engine\nimport os\n"
        )
        assert forbidden_imports(offender) == [
            "swcraft_tools.editing",
            "swcraft_tools.editing.engine",
            "swcraft_tools.writer.mutations",
            "swcraft_tools.writer.mutations.swc_to_dict",
        ]

    def test_no_dynamic_import_is_spelled(self) -> None:
        text = ALLOCATION.read_text(encoding="utf-8")
        assert "importlib" not in text
        assert "__import__" not in text


class TestTheSolverIsImportedLazily:
    def test_scipy_is_imported_only_inside_solve_curve(self) -> None:
        tree = _tree()
        assert solver_imports(tree), "the leaf must import the solver somewhere"
        assert misplaced_solver_imports(tree) == []

    def test_the_check_fires_on_a_module_level_and_a_foreign_function_import(self) -> None:
        offender = ast.parse(
            "import scipy\n"
            "def helper():\n"
            "    from scipy.optimize import milp\n"
            "def solve_curve():\n"
            "    import scipy.optimize\n"
        )
        assert misplaced_solver_imports(offender) == [1, 3]

    def test_the_arxml_path_never_loads_the_solver(self, tmp_path: Path) -> None:
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
            "loaded = sorted(n for n in sys.modules if n == 'scipy' or n.startswith('scipy.'))\n"
            "print('EXIT-CODE ' + str(code))\n"
            "print('SOLVER-MODULES ' + json.dumps(loaded))\n"
        )
        for argv in (["generate", "arxml", "--project", manifest], ["validate", "--project", manifest]):
            completed = subprocess.run(
                [sys.executable, "-c", probe, *argv],
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=str(tmp_path),
                check=False,
            )
            lines = [line for line in completed.stdout.splitlines() if line.startswith("SOLVER-MODULES ")]
            assert lines, (argv, completed.stdout, completed.stderr)
            assert json.loads(lines[-1].removeprefix("SOLVER-MODULES ")) == [], argv
            # Each command ran to its end: a validate that failed early would not prove the path solver-free.
            assert "EXIT-CODE 0" in completed.stdout.splitlines(), (argv, completed.stdout, completed.stderr)
            assert (project / "dist" / "model.arxml").is_file(), (argv, completed.stdout, completed.stderr)
