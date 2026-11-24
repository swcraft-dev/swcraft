"""The validator package's facade: what ``swcraft_tools.validator`` serves by its ``__all__``.

The facade serves every public name a module of the package defines, except the edge's own
(``model_view.py``'s, which its callers import from that module), and no private name: no production
module outside the package reads one from the facade. Each entry resolves on the facade.

What it does not reach: a production reader that names a private symbol by a string
(``getattr(validator, "_x")``), that binds the facade by a relative import (``from . import
validator``) or reaches it through another name it was assigned to, and a public name bound in a
module by an import rather than a definition, which belongs to the module that defines it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import validator_package
from conftest import REPO_ROOT
from swcraft_tools import validator

#: The module whose public names the facade does not serve.
EDGE = "model_view.py"


def _defined_public_names() -> set[str]:
    names: set[str] = set()
    for path in validator_package.paths():
        if path.name in {"__init__.py", EDGE}:
            continue
        names |= {name for name in validator_package.top_level_names(path) if not name.startswith("_")}
    return names


def _facade_names(tree: ast.Module) -> set[str]:
    """The dotted names under which *tree* binds the facade module by an absolute import."""
    package, _, leaf = validator.__name__.rpartition(".")
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bound |= {alias.asname or alias.name for alias in node.names if alias.name == validator.__name__}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == package:
            bound |= {alias.asname or alias.name for alias in node.names if alias.name == leaf}
    return bound


def privates_read(source: str) -> set[str]:
    """Every private name *source* imports from the facade or reads as an attribute of it."""
    tree = ast.parse(source)
    facade = _facade_names(tree)
    read: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == validator.__name__:
            read |= {alias.name for alias in node.names if alias.name.startswith("_")}
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_") and ast.unparse(node.value) in facade:
            read.add(node.attr)
    return read


def _privates_read_by_production() -> set[str]:
    """Every private name a production module outside the package imports from or reads on the facade."""
    read: set[str] = set()
    package = Path(validator.__file__ or "").parent
    for source in sorted(REPO_ROOT.glob("packages/*/src/**/*.py"), key=lambda path: path.as_posix()):
        if not source.is_relative_to(package):
            read |= privates_read(source.read_text(encoding="utf-8"))
    return read


def test_every_entry_of_all_resolves_on_the_facade() -> None:
    assert len(set(validator.__all__)) == len(validator.__all__)
    assert [name for name in validator.__all__ if not hasattr(validator, name)] == []


def test_the_facade_serves_every_public_name_the_package_defines_outside_the_edge() -> None:
    public = {name for name in validator.__all__ if not name.startswith("_")}

    assert public == _defined_public_names()


def test_the_facade_serves_no_private_name() -> None:
    assert [name for name in validator.__all__ if name.startswith("_")] == []
    assert _privates_read_by_production() == set()


@pytest.mark.parametrize(
    "source",
    [
        "from swcraft_tools import validator\nvalidator._x",
        "from swcraft_tools import validator as val\nval._x",
        "import swcraft_tools.validator as v\nv._x",
        "import swcraft_tools.validator\nswcraft_tools.validator._x",
        "from swcraft_tools.validator import _x",
    ],
)
def test_a_private_read_is_seen_under_every_absolute_binding_of_the_facade(source: str) -> None:
    assert privates_read(source) == {"_x"}


def test_a_private_attribute_of_another_module_is_not_a_facade_read() -> None:
    assert privates_read("import validator_package as validator\nvalidator._x\nother._y") == set()
