"""The modules of the ``swcraft_tools.validator`` package, for the tests that read or patch the validator.

A scan of "the validator" is the union over these modules, and a patch of a name the validator looks
up reaches every module that binds it: a module looks a global up in its own namespace, so a patch on
the facade or on one module alone is inert in the others.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PACKAGE = "swcraft_tools.validator"


def modules() -> tuple[ModuleType, ...]:
    """Every module of the package, the facade excluded, in name order."""
    package = importlib.import_module(PACKAGE)
    return tuple(importlib.import_module(f"{PACKAGE}.{info.name}") for info in pkgutil.iter_modules(package.__path__))


def paths() -> tuple[Path, ...]:
    """Every source file of the package, the facade included, in POSIX path order."""
    root = Path(importlib.import_module(PACKAGE).__file__ or "").parent
    return tuple(sorted(root.glob("*.py"), key=lambda path: path.as_posix()))


def top_level_names(path: Path) -> set[str]:
    """Every name the source at *path* defines at module level: a ``def``, a ``class`` or an assignment.

    What it does not reach: a name bound by an import, by tuple unpacking (``a, b = ...``), by a
    ``type X = ...`` alias, or inside a module-level ``if``, ``try`` or ``with`` block; such a name
    reads as not defined.
    """
    names: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {target.id for target in node.targets if isinstance(target, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def defining_module(name: str) -> ModuleType:
    """The one module of the package whose source defines *name* at top level."""
    found = [module for module in modules() if name in top_level_names(Path(module.__file__ or ""))]
    assert len(found) == 1, f"{name} is defined in {[module.__name__ for module in found]}, not in one module"
    return found[0]


def defines(name: str) -> bool:
    """Whether any module of the package defines *name* at top level."""
    return any(name in top_level_names(path) for path in paths())


def binding_modules(name: str) -> tuple[ModuleType, ...]:
    """Every module of the package whose namespace binds *name*, the facade included: where a lookup happens."""
    package = importlib.import_module(PACKAGE)
    return tuple(module for module in (package, *modules()) if name in vars(module))


def patch_everywhere(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> tuple[ModuleType, ...]:
    """Set *name* to *value* on every module of the package that binds it, and return those modules.

    Fails when no module binds *name*, so a patch that would reach nothing is an error and never a
    silent no-op.
    """
    bound = binding_modules(name)
    assert bound, f"no module of {PACKAGE} binds {name}"
    for module in bound:
        monkeypatch.setattr(module, name, value)
    return bound


def module_functions(module: ModuleType) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every module-level ``def`` and ``async def`` of *module*'s source, by name, read off its syntax tree."""
    tree = ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))
    return {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def reached_functions(module: ModuleType, entry: str) -> set[str]:
    """*entry* and every module-level function of *module* it reaches by a call by bare name, transitively.

    Read off the module's syntax tree: a phase a check calls, and a step a phase calls, are reached.
    What it does not reach: a function called as an attribute (``module.f()``), passed as a value
    rather than called, defined in another module, or defined inside a function or a class. What it
    over-reaches: a call is followed wherever it is written in a reached function's body, including
    a nested ``def``, a ``lambda`` or a branch that never runs, so a function it names may never be
    called at run time. Fails when *entry* is not a module-level function of *module*.
    """
    defined = module_functions(module)
    assert entry in defined, f"{entry} is not a module-level function of {module.__name__}"
    reached = [entry]
    for name in reached:
        for node in ast.walk(defined[name]):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in defined
                and node.func.id not in reached
            ):
                reached.append(node.func.id)
    return set(reached)


COMPOSER = "check_cross_layer_consistency"


def tuple_loop_names(module: ModuleType, entry: str) -> tuple[str, ...]:
    """The names of the one tuple literal a ``for`` statement walks in *entry* or a function it reaches.

    The ``ast.Name`` elements of that tuple, in the order it lists them, among *entry* and the
    functions of *module* it reaches (:func:`reached_functions`). Fails unless there is exactly one
    such loop and its tuple holds a name, so a second tuple loop in a reached function is an error
    and never a silent pick.
    """
    defined = module_functions(module)
    loops = [
        (name, node)
        for name in sorted(reached_functions(module, entry))
        for node in ast.walk(defined[name])
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple)
    ]
    assert len(loops) == 1, f"{entry} reaches {len(loops)} loops over a tuple, not one: {[n for n, _ in loops]}"
    (_name, loop) = loops[0]
    assert isinstance(loop.iter, ast.Tuple)
    names = tuple(element.id for element in loop.iter.elts if isinstance(element, ast.Name))
    assert names, f"{entry}'s tuple loop names no check"
    return names


def composed_checks() -> tuple[str, ...]:
    """The names of the checks :data:`COMPOSER` runs, in the order its tuple lists them.

    The composer iterates a tuple of function objects: :func:`tuple_loop_names` over the composer's
    module.
    """
    return tuple_loop_names(defining_module(COMPOSER), COMPOSER)
