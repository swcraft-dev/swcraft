"""The specimen generator's independence, held at the process boundary.

The generator is an *independent* oracle: it writes models that SWCraft's validator, passport and build judge, and
its passport reading restates ``passport.py`` rather than importing it. Both projects are installed editable in the
one ``.venv``, so the scan reads the test trees and the scripts too:

* no module under ``packages/{tools,mcp,agent}/src``, ``packages/{tools,mcp,agent}/tests`` or ``scripts`` imports
  ``swcraft_specimens`` (SWCraft starts the generator, never calls it);
* no module under ``packages/specimens/src`` or ``packages/specimens/tests`` (the scenario runner included) imports
  a SWCraft package;
* no module of SWCraft's trees spells a ``"-m", "swcraft_specimens"`` start outside :data:`MODULE_START_ALLOWED`.

Each exception is an allow-list entry with its reason. The scan reads only the open trees; the closed half of this
check runs the two SWCraft-side checks over the closed tree. An ``import`` statement counts at any depth, and so do
``importlib.import_module`` and ``__import__`` with a constant name.

What it does not reach: a name built at run time; a process either side starts (the boundary itself); the
generator's read of ``swcraft-tools``' distribution version through ``importlib.metadata``; an import inside a
string handed to another interpreter; and a module start spelled through a variable.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path

import pytest
from conftest import REPO_ROOT

#: SWCraft's product trees, which must not import the generator.
SWCRAFT_TREES = ("packages/tools/src", "packages/mcp/src", "packages/agent/src")

#: The generator's product tree, which must not import SWCraft.
GENERATOR_TREE = "packages/specimens/src"

#: The generator's top-level package.
GENERATOR_PACKAGES = frozenset({"swcraft_specimens"})

#: SWCraft's top-level packages.
SWCRAFT_PACKAGES = frozenset({"swcraft_tools", "swcraft_mcp", "swcraft_agent"})

#: SWCraft's open test trees and scripts, which start the generator and never import it (the closed
#: tree is the closed half's).
SWCRAFT_DRIVER_TREES = ("packages/tools/tests", "packages/mcp/tests", "packages/agent/tests", "scripts")

#: The generator's test tree, its scenario runner included, which starts ``swcraft`` and never imports it.
GENERATOR_DRIVER_TREES = ("packages/specimens/tests",)

#: Every open tree the scan reads (the closed half checks that none of them is a closed tree).
ALL_TREES = (*SWCRAFT_TREES, GENERATOR_TREE, *SWCRAFT_DRIVER_TREES, *GENERATOR_DRIVER_TREES)

#: ``(module, reason)``: SWCraft modules allowed to import the generator in-process. None today.
SWCRAFT_IMPORT_ALLOWED: dict[str, str] = {}

#: ``(module, reason)``: generator modules allowed to import SWCraft in-process. None today.
GENERATOR_IMPORT_ALLOWED: dict[str, str] = {}

#: ``(module, reason)``: SWCraft modules allowed to spell ``"-m", "swcraft_specimens"``.
MODULE_START_ALLOWED = {
    "scripts/tests/test_archive_specimen_generator.py": (
        "a start-log entry of a module start, planted as data: the finding the archive test must report"
    ),
    "packages/tools/tests/test_specimen_generator_boundary.py": "this scan's own pattern, the pair it looks for",
}

#: The calls that import a module named by their first argument.
_IMPORT_CALLS = frozenset({"import_module", "__import__"})


def imported_packages(tree: ast.AST) -> list[tuple[int, str]]:
    """``(line, top-level package)`` of every import *tree* states, statement or constant-named call."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name.split(".")[0]) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.lineno, node.module.split(".")[0]))
        elif isinstance(node, ast.Call):
            function = node.func
            name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", None)
            if name in _IMPORT_CALLS and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.append((node.lineno, first.value.split(".")[0]))
    return found


def _modules(tree: str) -> list[Path]:
    root = REPO_ROOT / tree
    return sorted(root.rglob("*.py"), key=lambda path: path.relative_to(REPO_ROOT).as_posix())


def module_starts(tree: ast.AST) -> list[int]:
    """The lines of *tree* holding a sequence literal with ``"-m", "swcraft_specimens"`` side by side."""
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.List | ast.Tuple):
            values = [element.value if isinstance(element, ast.Constant) else None for element in node.elts]
            if any(pair == ("-m", "swcraft_specimens") for pair in zip(values, values[1:], strict=False)):
                lines.append(node.lineno)
    return lines


def import_offenders(
    trees: tuple[str, ...], forbidden: frozenset[str], allowed: Mapping[str, str] | None = None
) -> list[str]:
    """``module:line imports package`` for every import of *forbidden* in *trees*, *allowed* aside."""
    offenders = []
    for tree in trees:
        for module in _modules(tree):
            name = module.relative_to(REPO_ROOT).as_posix()
            if allowed and name in allowed:
                continue
            parsed = ast.parse(module.read_text(encoding="utf-8"), filename=module.as_posix())
            offenders.extend(
                f"{name}:{line} imports {package}"
                for line, package in imported_packages(parsed)
                if package in forbidden
            )
    return offenders


def start_offenders(trees: tuple[str, ...], allowed: Mapping[str, str]) -> list[str]:
    """``module:line`` for every ``"-m", "swcraft_specimens"`` start in *trees*, *allowed* aside."""
    return [
        f"{module.relative_to(REPO_ROOT).as_posix()}:{line}"
        for tree in trees
        for module in _modules(tree)
        if module.relative_to(REPO_ROOT).as_posix() not in allowed
        for line in module_starts(ast.parse(module.read_text(encoding="utf-8"), filename=module.as_posix()))
    ]


@pytest.mark.parametrize("tree", ALL_TREES)
def test_each_scanned_tree_holds_modules(tree: str) -> None:
    """A tree that moved would make the scan pass by reading nothing."""
    assert _modules(tree), f"{tree} holds no module"


def test_no_swcraft_product_module_imports_the_generator() -> None:
    assert import_offenders(SWCRAFT_TREES, GENERATOR_PACKAGES) == []


def test_no_generator_module_imports_swcraft() -> None:
    assert import_offenders((GENERATOR_TREE,), SWCRAFT_PACKAGES) == []


def test_no_swcraft_test_or_script_imports_the_generator() -> None:
    assert import_offenders(SWCRAFT_DRIVER_TREES, GENERATOR_PACKAGES, SWCRAFT_IMPORT_ALLOWED) == []


def test_no_generator_test_or_script_imports_swcraft() -> None:
    assert import_offenders(GENERATOR_DRIVER_TREES, SWCRAFT_PACKAGES, GENERATOR_IMPORT_ALLOWED) == []


def test_no_swcraft_module_starts_the_generators_module_form() -> None:
    """No SWCraft module starts the generator by its module form.

    SWCraft starts the generator's executable through ``scripts/specimen_generator.py``; a literal ``python -m
    swcraft_specimens`` would run whatever ``.venv`` holds, bypassing the configured one."""
    assert start_offenders((*SWCRAFT_TREES, *SWCRAFT_DRIVER_TREES), MODULE_START_ALLOWED) == []


@pytest.mark.parametrize("module", sorted(MODULE_START_ALLOWED))
def test_each_allowed_module_start_is_still_there(module: str) -> None:
    """A stale allow-list entry is removed, so the list stays the set of real exceptions."""
    assert module_starts(ast.parse((REPO_ROOT / module).read_text(encoding="utf-8")))


def test_the_start_scan_sees_a_planted_start() -> None:
    assert module_starts(ast.parse("run([python, '-m', 'swcraft_specimens', 'generate'])\n")) == [1]
    assert module_starts(ast.parse("run([python, '-m', 'swcraft_tools.cli'])\n")) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import swcraft_specimens\n", [(1, "swcraft_specimens")]),
        ("import swcraft_specimens.cli as cli\n", [(1, "swcraft_specimens")]),
        ("from swcraft_tools.passport import read\n", [(1, "swcraft_tools")]),
        ("def f():\n    from swcraft_mcp import server\n", [(2, "swcraft_mcp")]),
        ("import importlib\nimportlib.import_module('swcraft_agent.host')\n", [(1, "importlib"), (2, "swcraft_agent")]),
        ("__import__('swcraft_specimens')\n", [(1, "swcraft_specimens")]),
        ("from . import sibling\n", []),
        ("import importlib.metadata\nimportlib.metadata.version('swcraft-tools')\n", [(1, "importlib")]),
        ("name = 'swcraft_tools'\nimportlib.import_module(name)\n", []),
    ],
)
def test_the_scan_reads_each_form_of_import(source: str, expected: list[tuple[int, str]]) -> None:
    """Planted: statements at any depth and constant-named calls count.

    A relative import, a metadata read and a name built at run time do not (the last is what the scan does not reach).
    """
    assert imported_packages(ast.parse(source)) == expected
