"""Which classes of a test module take a fixture -- the check behind an ``xdist_group`` pin.

A module- or class-scoped fixture is built once per worker that runs one of its
consumers. Under ``--dist loadgroup`` an ``xdist_group`` mark holds one class on
one worker, so the fixture is built once only while every consumer sits inside
that class; a consumer elsewhere would build it again on whichever worker xdist
sends it to. Used by ``test_editing_validator_messages.py`` and ``test_editing_vectors.py``.

**What it does not reach.** It reads the module's syntax tree, so it sees a
fixture taken as a parameter by name and nothing else: a ``usefixtures`` mark,
a ``request.getfixturevalue`` call and an ``autouse`` fixture are not consumers
to it.
"""

from __future__ import annotations

import ast
from pathlib import Path


def classes_consuming(module_path: Path, fixture_name: str) -> set[str | None]:
    """The class (``None`` at module level) of every function in *module_path* taking *fixture_name*.

    The fixture's own definition is not a consumer.
    """
    module = ast.parse(module_path.read_text(encoding="utf-8"))
    owners: set[str | None] = set()

    def visit(node: ast.AST, owner: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, child.name)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                arguments = [*child.args.posonlyargs, *child.args.args, *child.args.kwonlyargs]
                if child.name != fixture_name and any(argument.arg == fixture_name for argument in arguments):
                    owners.add(owner)
                visit(child, owner)

    visit(module, None)
    return owners
