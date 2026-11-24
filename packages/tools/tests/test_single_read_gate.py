"""The opener gate: rule 2 of ``model_state.single_read``, the closed list of its callers.

An ``ast`` scan of the three source trees: which function calls ``single_read``, whether an
``async def`` stands round it, any use of the name that is no call, and any touch of the memo
itself. The scan's own cases close the file.

**What this does not reach.** The scan sees a name, so a call by ``getattr`` or ``importlib``
is outside it, and it counts the two calls in ``cli.dispatch._single_read_for`` and not their reach:
that is ``test_single_read.py``'s, by subcommand. ``scripts/`` and the test trees stay out too.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest
from conftest import REPO_ROOT

#: The closed list, ``(module, enclosing qualname)``, spelled as ``__qualname__`` spells it. A new
#: opener is a decision on rule 2 of ``single_read``'s docstring, never a row added to pass this.
OPENERS = frozenset(
    {
        ("swcraft_tools.cli.dispatch", "_single_read_for"),
        ("swcraft_tools.metrics", "compute_metrics"),
        ("swcraft_tools.editing.passes", "pass_d_findings"),
        ("swcraft_tools.editing.trace_candidates", "candidates_for"),
        ("swcraft_mcp.server", "build.<locals>._build_direct"),
        ("swcraft_mcp.server", "validate.<locals>._validate_direct"),
        ("swcraft_mcp.server", "resource_report"),
    }
)

#: Every source tree of an installed module. ``scripts/`` and the test trees open blocks of
#: their own (the measurement, the tripwire's cases) and are no product path.
SOURCE_ROOTS = tuple(
    REPO_ROOT / "packages" / project / "src" / package
    for project, package in (("tools", "swcraft_tools"), ("mcp", "swcraft_mcp"), ("agent", "swcraft_agent"))
)

_THE_MODULE = "swcraft_tools.model_state"
_THE_FUNCTION = "single_read"


@dataclasses.dataclass
class _Scan:
    """What one module does with ``single_read``."""

    #: Enclosing qualname of each call, ``<module>`` at the top level.
    calls: set[str] = dataclasses.field(default_factory=set)
    #: The calls with an ``async def`` anywhere round them.
    inside_async: set[str] = dataclasses.field(default_factory=set)
    #: Uses that are no call: a value handed on, an alias, a star import. Each could open a block unseen.
    other_uses: list[str] = dataclasses.field(default_factory=list)


def _terminal(node: ast.expr) -> str | None:
    """The last name of *node*: ``f`` of ``f`` and of ``a.b.f``, ``None`` for anything else."""
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else None


class _Walker(ast.NodeVisitor):
    """Each use of the name, with the qualname that encloses it.

    By the name alone and whatever it is bound to, so another object's method of that name is a
    hit: a failure, never a miss. The ``def`` in the kernel is no use of the name.
    """

    def __init__(self, scan: _Scan) -> None:
        self._scan = scan
        self._path: list[str] = []
        self._async_depth = 0

    def _inside(self, name: str, node: ast.AST, *, opens_locals: bool, is_async: bool = False) -> None:
        self._path.append(name)
        if opens_locals:
            self._path.append("<locals>")
        self._async_depth += is_async
        for child in ast.iter_child_nodes(node):
            self.visit(child)
        self._async_depth -= is_async
        del self._path[-(2 if opens_locals else 1) :]

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._inside(node.name, node, opens_locals=True)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._inside(node.name, node, opens_locals=True, is_async=True)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._inside("<lambda>", node, opens_locals=True)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._inside(node.name, node, opens_locals=False)

    def _enclosing(self) -> str:
        path = self._path[:-1] if self._path and self._path[-1] == "<locals>" else self._path
        return ".".join(path) or "<module>"

    def visit_Call(self, node: ast.Call) -> None:
        if _terminal(node.func) != _THE_FUNCTION:
            self.generic_visit(node)
            return
        self._scan.calls.add(self._enclosing())
        if self._async_depth:
            self._scan.inside_async.add(self._enclosing())
        if isinstance(node.func, ast.Attribute):
            self.visit(node.func.value)
        for child in (*node.args, *node.keywords):
            self.visit(child)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == _THE_FUNCTION:
            self._scan.other_uses.append(f"line {node.lineno}: used as a value")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == _THE_FUNCTION:
            self._scan.other_uses.append(f"line {node.lineno}: used as a value")
        self.visit(node.value)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == _THE_FUNCTION and alias.asname not in (None, _THE_FUNCTION):
                self._scan.other_uses.append(f"line {node.lineno}: imported as {alias.asname}")
            elif alias.name == "*" and (node.module or "").endswith("model_state"):
                self._scan.other_uses.append(f"line {node.lineno}: a star import of the kernel")


def _scan(source: str) -> _Scan:
    """What a module holding *source* does with ``single_read``."""
    scan = _Scan()
    _Walker(scan).visit(ast.parse(source))
    return scan


def _source_modules() -> dict[str, Path]:
    """Every module of the source trees: its dotted name and its file."""
    found: dict[str, Path] = {}
    for root in SOURCE_ROOTS:
        for file in sorted(root.rglob("*.py"), key=lambda path: path.as_posix()):
            parts = file.relative_to(root.parent).with_suffix("").parts
            found[".".join(parts[:-1] if parts[-1] == "__init__" else parts)] = file
    return found


@pytest.fixture(scope="module")
def opener_scans() -> dict[str, _Scan]:
    """Every source module scanned, once for the module's tests."""
    return {module: _scan(file.read_text(encoding="utf-8")) for module, file in _source_modules().items()}


def test_the_openers_are_exactly_the_closed_list(opener_scans: dict[str, _Scan]) -> None:
    """Rule 2: the openers are exactly the closed list.

    **What this does not reach:** a call by ``getattr`` or ``importlib``, a name imported in one module and handed to
    another as an argument of a function that merely calls it, and any write added later beneath an opener, which only
    the tripwire's fresh load can see."""
    assert {(module, qualname) for module, scan in opener_scans.items() for qualname in scan.calls} == OPENERS


def test_no_opener_sits_inside_an_async_def(opener_scans: dict[str, _Scan]) -> None:
    """Rule 4: a nested ``def`` of an ``async def`` counts, since its caller may hold the request."""
    assert {module: scan.inside_async for module, scan in opener_scans.items() if scan.inside_async} == {}


def test_the_source_trees_use_single_read_only_by_calling_it(opener_scans: dict[str, _Scan]) -> None:
    assert {module: scan.other_uses for module, scan in opener_scans.items() if scan.other_uses} == {}


def test_nothing_but_the_kernel_touches_the_memo_itself() -> None:
    """The gate counts calls of ``single_read``; setting the variable directly would pass under it."""
    touching = [
        module
        for module, file in _source_modules().items()
        if module != _THE_MODULE
        and any(
            isinstance(node, ast.Name | ast.Attribute)
            and (node.id if isinstance(node, ast.Name) else node.attr) == "_SINGLE_READ"
            for node in ast.walk(ast.parse(file.read_text(encoding="utf-8")))
        )
    ]

    assert touching == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("def f():\n    with single_read(): pass\n", {"f"}, id="the-plain-name"),
        pytest.param("def f():\n    ms.single_read()\n", {"f"}, id="through-a-module"),
        pytest.param("def f():\n    a.b.single_read()\n", {"f"}, id="the-dotted-name"),
        pytest.param(
            "def outer():\n    def inner():\n        single_read()\n", {"outer.<locals>.inner"}, id="a-nested-function"
        ),
        pytest.param("class C:\n    def m(self):\n        single_read()\n", {"C.m"}, id="a-method"),
        pytest.param("single_read()\n", {"<module>"}, id="the-top-level"),
        pytest.param("def single_read():\n    pass\n", set(), id="the-definition-is-no-call"),
    ],
)
def test_the_gate_finds_a_call(source: str, expected: set[str]) -> None:
    assert _scan(source).calls == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("async def f():\n    single_read()\n", {"f"}, id="directly"),
        pytest.param(
            "async def f():\n    def run():\n        single_read()\n", {"f.<locals>.run"}, id="in-a-nested-def"
        ),
        pytest.param("def f():\n    single_read()\n", set(), id="not-in-a-plain-def"),
        pytest.param("async def f():\n    pass\n\ndef g():\n    single_read()\n", set(), id="not-after-one"),
    ],
)
def test_the_gate_sees_an_enclosing_async_def(source: str, expected: set[str]) -> None:
    assert _scan(source).inside_async == expected


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("from swcraft_tools.model_state import single_read as once\n", id="an-alias"),
        pytest.param("from swcraft_tools.model_state import *\n", id="a-star-import"),
        pytest.param("from .model_state import *\n", id="a-relative-star-import"),
        pytest.param("open_it = single_read\n", id="a-rebinding"),
        pytest.param("open_it = model_state.single_read\n", id="an-attribute"),
        pytest.param("run(single_read)\n", id="an-argument"),
    ],
)
def test_the_gate_refuses_a_use_that_is_no_call(source: str) -> None:
    assert _scan(source).other_uses
