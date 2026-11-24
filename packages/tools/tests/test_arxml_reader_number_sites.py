"""The number helpers' call sites, pinned: every call names its grammar, and the grammar is its field's.

The inventory is ``arxml_reader_number_sites.py`` (its docstring says what it does not reach). It is held
against the AST of every ``swcraft_tools`` module, so an unlisted call or a grammar passed other than as a
literal keyword fails, and against the XSD map's number class of every listed field.
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
from arxml_reader_number_sites import GRAMMAR_OF_CLASS, HELPERS, NUMBER_SITES, PASSED_ON
from io_oracle_xsd_map_data import NUMERIC_CONTEXTS

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "swcraft_tools"
READER = "swcraft_tools.arxml_reader"


# Modelled on scripts/quality_ratchet.py::_imported_module; here the path's last segment (``__init__`` included)
# is the one dropped for level 1, so no package flag is needed.
def _imported_module(module: str, node: ast.ImportFrom) -> str:
    """Return the absolute module that *node*, an import in the package file *module*, names."""
    if node.level == 0:
        return node.module or ""
    parts = ["swcraft_tools", *module.removesuffix(".py").split("/")][: -node.level]
    return ".".join([*parts, *([node.module] if node.module else [])])


def _other_bindings(module: str, body: list[ast.stmt]) -> set[str]:
    """The helper names one scope's *body* binds to another function: a definition, or an import from elsewhere.

    Nested function and class bodies are their own scopes and are not read; the reader's own module binds none.
    """
    if module == "arxml_reader.py":
        return set()
    bound: set[str] = set()
    pending: list[ast.AST] = list(body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            continue
        if isinstance(node, ast.ImportFrom) and _imported_module(module, node) != READER:
            bound.update(alias.asname or alias.name for alias in node.names)
        pending.extend(child for child in ast.iter_child_nodes(node) if not isinstance(child, ast.Lambda))
    return bound & HELPERS


def _package_sources() -> Iterator[tuple[str, str]]:
    """``(module path relative to the package, source text)`` of every ``swcraft_tools`` module."""
    for path in sorted(PACKAGE.rglob("*.py"), key=lambda candidate: candidate.as_posix()):
        yield path.relative_to(PACKAGE).as_posix(), path.read_text(encoding="utf-8")


def _calls(sources: Iterable[tuple[str, str]]) -> Counter[tuple[str, str, str, str]]:
    """``(module, enclosing function, helper, grammar)`` of every call of a helper in *sources*, counted."""
    found: Counter[tuple[str, str, str, str]] = Counter()
    for module, text in sources:
        tree = ast.parse(text, filename=module)
        enclosing: list[str] = []
        scopes: list[set[str]] = [_other_bindings(module, tree.body)]

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                enclosing.append(node.name)
                scopes.append(scopes[-1] | _other_bindings(module, node.body))
                self.generic_visit(node)
                scopes.pop()
                enclosing.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node: ast.Call) -> None:
                callee = node.func
                name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
                if name in HELPERS and not (isinstance(callee, ast.Name) and name in scopes[-1]):
                    grammar = [keyword.value for keyword in node.keywords if keyword.arg == "grammar"]
                    if len(grammar) != 1:
                        spelled = "<no grammar keyword>"
                    elif isinstance(grammar[0], ast.Constant) and isinstance(grammar[0].value, str):
                        spelled = grammar[0].value
                    elif isinstance(grammar[0], ast.Name) and grammar[0].id == PASSED_ON:
                        spelled = PASSED_ON
                    else:
                        spelled = f"<computed: {ast.unparse(grammar[0])}>"
                    found[(module, enclosing[-1] if enclosing else "<module>", name, spelled)] += 1
                self.generic_visit(node)

        Visitor().visit(tree)
    return found


def test_every_call_site_is_inventoried_with_its_grammar() -> None:
    expected: Counter[tuple[str, str, str, str]] = Counter()
    for site in NUMBER_SITES:
        expected[(site.module, site.function, site.helper, site.grammar)] += site.count
    assert _calls(_package_sources()) == expected


def test_the_scan_skips_another_function_of_a_helper_name_and_counts_the_readers() -> None:
    """A helper name bound elsewhere is skipped in its scope only; the reader's helper, by any import, is counted."""
    call = "x = exact_decimal(t, grammar='pattern')\n"
    probes = {
        "own.py": "def exact_decimal(v):\n    return v\n" + call,
        "other.py": "from swcraft_tools.trace_address import exact_decimal\n" + call,
        "absolute.py": "from swcraft_tools.arxml_reader import exact_decimal\n" + call,
        "sub/relative.py": "from ..arxml_reader import exact_decimal\n" + call,
        "attribute.py": "from swcraft_tools import arxml_reader as r\ny = r.exact_decimal(t, grammar='pattern')\n",
        "arxml_reader.py": "def exact_decimal(t, *, grammar):\n    return t\n" + call,
        "local.py": (
            "from swcraft_tools.arxml_reader import exact_decimal\n"
            "def own():\n    from swcraft_tools.trace_address import exact_decimal\n    return exact_decimal(t)\n"
            "def reader():\n    return exact_decimal(t)\n"
        ),
    }
    found = _calls(probes.items())
    assert {module for module, *_ in found} == {
        "absolute.py",
        "sub/relative.py",
        "attribute.py",
        "arxml_reader.py",
        "local.py",
    }
    assert [key for key in found if key[0] == "local.py"] == [
        ("local.py", "reader", "exact_decimal", "<no grammar keyword>")
    ]


@pytest.mark.parametrize("site", [site for site in NUMBER_SITES if site.grammar != PASSED_ON], ids=lambda s: s.function)
def test_each_field_is_read_by_the_grammar_of_its_xsd_class(site) -> None:
    assert site.fields, "a site that reads a field names it"
    classes = {field: NUMERIC_CONTEXTS.get(field) for field in site.fields}
    assert None not in classes.values(), f"a field the XSD map has no number class for: {classes}"
    assert {GRAMMAR_OF_CLASS[cls] for cls in classes.values()} == {site.grammar}


def test_a_chaining_call_reads_no_field_of_its_own() -> None:
    assert all(not site.fields for site in NUMBER_SITES if site.grammar == PASSED_ON)
