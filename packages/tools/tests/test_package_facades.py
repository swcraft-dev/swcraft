"""The package facades: each name they serve is the owning module's own object, and no test patches them.

``swcraft_tools.query`` and ``swcraft_tools.cli`` are packages whose ``__init__`` re-exports, by
``__all__``, the names their callers read. Three claims hold each facade:

* every name in ``__all__`` is the object its owner defines (an explicit owner map, because a
  constant such as ``JOIN_MAX_ENTRIES`` carries no ``__module__``), and ``__all__`` is exactly the
  map's names;
* no test patches the facade: a function looks a global up in the module that binds it, so a patch on
  the facade misses every function that looks the name up elsewhere, and an assertion that the stand-in
  was not called passes vacuously. The scan refuses every ``setattr``/``delattr``/``patch.object`` on
  the facade and every dotted string ``swcraft_tools.query.X``, unless ``X`` is a submodule; the
  ``__init__`` holds only imports and ``__all__``. Planted files prove both checks fire;
* the production code that reads a name off the facade object itself, the one place a facade patch
  would reach, is exactly ``FACADE_READERS``. Everywhere else a test patches a name on each module that
  looks it up.

The scan reads the open test trees; the closed half of this check runs it over the closed tests.

What this does not reach: a patch spelled through ``importlib``, ``vars()`` or ``sys.modules``; a
``patch.multiple`` and a patch whose object or attribute is passed by keyword (``target=``, ``name=``,
``attribute=``); a dotted target assembled at run time; an attribute named by a variable, which the
scan cannot resolve and passes; a facade name rebound to another module in another scope (bindings are
read without scopes, so such a file is refused, never passed); the readers of ``scripts/``; and any
facade not in ``FACADES``.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The open test trees the patch scan reads.
TEST_TREES = (
    "packages/tools/tests",
    "packages/mcp/tests",
    "packages/agent/tests",
    "packages/specimens/tests",
    "scripts/tests",
)

#: Each facade, and the owning submodule of every name its ``__all__`` serves.
FACADES: Mapping[str, Mapping[str, str]] = {
    "swcraft_tools.query": {
        **dict.fromkeys(
            (
                "AdjacencyMap",
                "build_graphs",
                "DEFAULT_DIRECTION",
                "dependencies",
                "Dependent",
                "dependents",
                "DIRECTIONS",
                "EdgeContext",
                "EdgeDetail",
                "ElementRef",
                "Graphs",
                "integral",
                "related",
            ),
            "edges",
        ),
        **dict.fromkeys(
            (
                "ABSENT",
                "Clause",
                "holds_no_value",
                "JOIN_ALIAS_PATTERN",
                "join_entries_refusal",
                "JOIN_MAX_ENTRIES",
                "JoinedQuery",
                "LAYER_NAME_PATTERN",
                "MEMBERS_PATH_PATTERN",
                "ordered_elements",
                "parse_request",
                "QUERY_FORMS",
                "QUERY_ROWS_SCHEMA_VERSION",
                "QUERY_SCHEMA_VERSION",
                "QueryError",
                "QueryJoin",
                "QueryPage",
                "QueryRequest",
                "RESERVED_JOIN_ALIASES",
                "Slice",
                "values_equal",
            ),
            "request",
        ),
        **dict.fromkeys(("full_element", "full_value"), "projection"),
        **dict.fromkeys(("row_cursor_refusal", "slice_layer"), "slicing"),
        **dict.fromkeys(
            (
                "answer_rows",
                "BARE_ROW_SHAPE",
                "collapse_rows",
                "computed_rows",
                "Derive",
                "DERIVED_CLAUSE_PREFIX",
                "derived_refusal",
                "derived_where_refusal",
                "DerivedRefusal",
                "identity_rows",
                "is_derived_clause",
                "join_refusal_prefix",
                "join_rows",
                "join_shape",
                "JoinBound",
                "JoinPathOutside",
                "JoinProduct",
                "page_rows",
                "row_shape",
                "RowCursorNotFound",
                "RowJoin",
                "rows_matching",
                "RowStep",
                "SET_VALUED_ROWS",
                "split_where",
            ),
            "rows",
        ),
        **dict.fromkeys(("answer_request",), "answer"),
        **dict.fromkeys(
            ("format_query", "format_query_json", "format_query_scope", "format_related", "format_related_json"),
            "formatting",
        ),
    },
    "swcraft_tools.cli": {
        **dict.fromkeys(("model_read_failure_sentence", "read_failure_sentence"), "messages"),
        **dict.fromkeys(("is_markdown_report",), "report_files"),
        **dict.fromkeys(("cmd_init", "project_name_problem"), "init_command"),
        **dict.fromkeys(
            (
                "CHECK_MODEL_SCHEMA_VERSION",
                "cmd_check_model",
                "cmd_clean_outputs",
                "cmd_generate",
                "cmd_validate",
                "MANIFEST_UNKNOWN_KEY_RULE",
            ),
            "build_commands",
        ),
        **dict.fromkeys(
            ("cmd_change_report", "cmd_diff", "cmd_impact", "cmd_list", "cmd_metrics", "cmd_resource_report"),
            "read_commands",
        ),
        **dict.fromkeys(("cmd_query",), "query_command"),
        **dict.fromkeys(("cmd_agent", "cmd_mcp_serve"), "handover"),
        **dict.fromkeys(
            ("cmd_import", "cmd_import_component", "cmd_import_requirements", "UPDATE_RESOLVES_EXIT"),
            "import_commands",
        ),
        **dict.fromkeys(
            ("cmd_docs", "DOCS_BIND_HOST", "DOCS_BROWSER_JOIN_SECONDS", "DOCS_CONTENT_TYPES", "GuideRequestHandler"),
            "docs_command",
        ),
        **dict.fromkeys(("cmd_experiment",), "experiment_command"),
        **dict.fromkeys(("cmd_guard",), "guard_command"),
        **dict.fromkeys(("cmd_passport",), "passport_command"),
        **dict.fromkeys(("main",), "dispatch"),
        **dict.fromkeys(("toolchain_pin_problem",), "swcraft_tools.toolchain_pin"),
    },
}

#: The production trees whose reads of a name off a facade (at import or at a call) are held to ``FACADE_READERS``.
SOURCE_TREES = ("packages/tools/src", "packages/mcp/src", "packages/agent/src")

#: Each facade, and the production modules that read a name off the facade object, with those names.
FACADE_READERS: Mapping[str, Mapping[str, frozenset[str]]] = {
    "swcraft_tools.query": {
        "packages/tools/src/swcraft_tools/editing/protocol.py": frozenset({"integral"}),
        "packages/tools/src/swcraft_tools/editing/read.py": frozenset(
            {"BARE_ROW_SHAPE", "Clause", "derived_where_refusal", "split_where"}
        ),
    },
    "swcraft_tools.cli": {
        "packages/tools/src/swcraft_tools/entry.py": frozenset({"main"}),
    },
}

#: The call names whose first argument is the object patched and whose second is the attribute.
_OBJECT_PATCHERS = frozenset({"setattr", "delattr", "object"})


def submodules(facade: str) -> frozenset[str]:
    """The names of *facade*'s submodules, read off the package directory."""
    package = importlib.import_module(facade)
    return frozenset(info.name for info in pkgutil.iter_modules(package.__path__))


def lookups_of(source: str) -> frozenset[str]:
    """The global names the top-level statements of *source* look up, its imports and ``__all__`` aside."""
    names: set[str] = set()
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.Import | ast.ImportFrom):
            continue
        if isinstance(statement, ast.Assign) and [getattr(target, "id", None) for target in statement.targets] == [
            "__all__"
        ]:
            continue
        names |= {node.id for node in ast.walk(statement) if isinstance(node, ast.Name)}
    return frozenset(names)


def owning_module(facade: str, owner: str) -> str:
    """The module an owner-map entry names: a submodule, or a dotted module."""
    return owner if "." in owner else f"{facade}.{owner}"


def _facade_names(tree: ast.AST, facade: str) -> set[str]:
    """The local names a file binds to *facade* itself, in any scope."""
    parent, _, leaf = facade.rpartition(".")
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == parent and node.level == 0:
            names |= {alias.asname or alias.name for alias in node.names if alias.name == leaf}
        elif isinstance(node, ast.Import):
            names |= {alias.asname for alias in node.names if alias.name == facade and alias.asname}
    return names


def _dotted(node: ast.expr) -> str | None:
    """``a.b.c`` for a chain of names and attributes, else ``None``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return None if inner is None else f"{inner}.{node.attr}"
    return None


def facade_patches(source: str, facade: str, modules: Iterable[str]) -> list[tuple[int, str]]:
    """Every patch of *facade* in *source*: ``(line, what)`` for each, in source order.

    A patch is a ``setattr``/``delattr``/``patch.object`` call on the facade (a bound name or the dotted
    module) whose attribute is a string not in *modules*, or a dotted string target into the facade
    whose last part is not in *modules* (the submodules).
    """
    allowed = frozenset(modules)
    tree = ast.parse(source)
    objects = _facade_names(tree, facade) | {facade}
    target = re.compile(rf"{re.escape(facade)}\.([A-Za-z_]\w*)(?:\.[A-Za-z_]\w*)*")
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and len(node.args) >= 2:
            callee = node.func
            name = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", None)
            attribute = node.args[1]
            if (
                name in _OBJECT_PATCHERS
                and _dotted(node.args[0]) in objects
                and isinstance(attribute, ast.Constant)
                and isinstance(attribute.value, str)
                and attribute.value not in allowed
            ):
                found.append((node.lineno, f"{name}({_dotted(node.args[0])}, {attribute.value!r})"))
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            match = target.fullmatch(node.value)
            if match and match.group(1) not in allowed:
                found.append((node.lineno, repr(node.value)))
    return sorted(found)


def facade_reads(source: str, facade: str, modules: Iterable[str]) -> frozenset[str]:
    """The names *source* reads off the *facade* object.

    An attribute of a name bound to the facade (or of the dotted facade), or a name a function imports
    from the facade in its body; a submodule in *modules* is not a read. A top-level
    ``from facade import X`` is not counted: it binds ``X`` in *source*, where a test patches it.
    """
    allowed = frozenset(modules)
    tree = ast.parse(source)
    objects = _facade_names(tree, facade) | {facade}
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _dotted(node.value) in objects and node.attr not in allowed:
            names.add(node.attr)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names |= {
                alias.name
                for inner in ast.walk(node)
                if isinstance(inner, ast.ImportFrom) and inner.module == facade and inner.level == 0
                for alias in inner.names
                if alias.name not in allowed
            }
    return frozenset(names)


def scan_trees(root: Path, trees: Iterable[str], facade: str) -> list[str]:
    """``path:line: what`` for every patch of *facade* in the ``*.py`` files under *root*'s *trees*."""
    modules = submodules(facade)
    hits: list[str] = []
    for tree in trees:
        for path in sorted((root / tree).rglob("*.py"), key=lambda path: path.as_posix()):
            source = path.read_text(encoding="utf-8")
            hits += [
                f"{path.relative_to(root).as_posix()}:{line}: {what}"
                for line, what in facade_patches(source, facade, modules)
            ]
    return hits


def _top_level_definitions(module_name: str) -> set[str]:
    """The names a module's own top-level statements define (not the names it imports)."""
    module = importlib.import_module(module_name)
    tree = ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {target.id for target in node.targets if isinstance(target, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


@pytest.mark.parametrize("facade", sorted(FACADES))
def test_the_facade_serves_exactly_the_owner_maps_names(facade: str) -> None:
    assert sorted(importlib.import_module(facade).__all__) == sorted(FACADES[facade])


@pytest.mark.parametrize(
    ("facade", "name", "owner"),
    [(facade, name, owner) for facade, owners in sorted(FACADES.items()) for name, owner in sorted(owners.items())],
)
def test_each_served_name_is_the_owning_modules_own_object(facade: str, name: str, owner: str) -> None:
    owning = owning_module(facade, owner)
    assert name in _top_level_definitions(owning), f"{owning} does not define {name}"
    assert getattr(importlib.import_module(facade), name) is getattr(importlib.import_module(owning), name)


@pytest.mark.parametrize("facade", sorted(FACADES))
def test_no_open_test_patches_the_facade(facade: str) -> None:
    hits = scan_trees(REPO_ROOT, TEST_TREES, facade)
    assert hits == [], (
        f"a patch on {facade} misses every function that looks the name up elsewhere; patch each module "
        "that looks it up:\n" + "\n".join(hits)
    )


def test_the_scan_reaches_the_test_trees() -> None:
    """Every tree exists and holds Python files, so an empty scan is not a missing directory."""
    for tree in TEST_TREES:
        assert any((REPO_ROOT / tree).rglob("test_*.py")), tree


_PLANTED = """
import swcraft_tools.query
import swcraft_tools.query as query_module
from unittest import mock
from swcraft_tools import query
from swcraft_tools import query as kernel


def test_planted(monkeypatch):
    monkeypatch.setattr(query, "JOIN_MAX_ENTRIES", 2)
    monkeypatch.setattr(query_module, "integral", None)
    monkeypatch.delattr(kernel, "slice_layer")
    monkeypatch.setattr(swcraft_tools.query, "related", None)
    monkeypatch.setattr("swcraft_tools.query.parse_request", None)
    with mock.patch.object(query, "answer_request"):
        pass
    with mock.patch("swcraft_tools.query.format_query"):
        pass


def test_allowed(monkeypatch):
    monkeypatch.setattr(query.request, "JOIN_MAX_ENTRIES", 2)
    monkeypatch.setattr("swcraft_tools.query.request.JOIN_MAX_ENTRIES", 2)
    monkeypatch.setattr(query, "request", None)
"""


def test_the_scan_fires_on_a_planted_file(tmp_path: Path) -> None:
    planted = tmp_path / "tests" / "test_planted.py"
    planted.parent.mkdir()
    planted.write_text(_PLANTED, encoding="utf-8", newline="\n")
    hits = scan_trees(tmp_path, ("tests",), "swcraft_tools.query")
    assert [hit.split(": ", 1)[1] for hit in hits] == [
        "setattr(query, 'JOIN_MAX_ENTRIES')",
        "setattr(query_module, 'integral')",
        "delattr(kernel, 'slice_layer')",
        "setattr(swcraft_tools.query, 'related')",
        "'swcraft_tools.query.parse_request'",
        "object(query, 'answer_request')",
        "'swcraft_tools.query.format_query'",
    ]


_PLANTED_INIT = """
from swcraft_tools.cli.messages import error_text, read_failure_sentence

__all__ = ["read_failure_sentence", "main"]


def main():
    return error_text(None)
"""

_PLANTED_CLI = """
from unittest import mock
from swcraft_tools import cli


def test_planted(monkeypatch):
    monkeypatch.setattr(cli, "read_failure_sentence", None)
    with mock.patch("swcraft_tools.cli.read_failure_sentence"):
        pass
    monkeypatch.setattr("swcraft_tools.cli.main", None)
    monkeypatch.setattr(cli, "_serve_forever", None)


def test_allowed(monkeypatch):
    monkeypatch.setattr(cli.docs_command, "_serve_forever", None)
    monkeypatch.setattr("swcraft_tools.cli.docs_command._serve_forever", None)
    monkeypatch.setattr(cli, "messages", None)
"""


@pytest.mark.parametrize("facade", sorted(FACADES))
def test_the_facade_init_looks_no_name_up(facade: str) -> None:
    """The ``__init__`` is imports and ``__all__`` alone, so a patch on the facade reaches no function."""
    package = importlib.import_module(facade)
    assert lookups_of(Path(package.__file__ or "").read_text(encoding="utf-8")) == frozenset()


def test_an_init_that_holds_code_is_seen_and_every_facade_patch_is_refused() -> None:
    """A planted ``__init__`` with a function looks names up; a facade patch is refused, a submodule's passes."""
    assert lookups_of(_PLANTED_INIT) == {"error_text"}
    modules = submodules("swcraft_tools.cli")
    assert [what for _line, what in facade_patches(_PLANTED_CLI, "swcraft_tools.cli", modules)] == [
        "setattr(cli, 'read_failure_sentence')",
        "'swcraft_tools.cli.read_failure_sentence'",
        "'swcraft_tools.cli.main'",
        "setattr(cli, '_serve_forever')",
    ]


@pytest.mark.parametrize("facade", sorted(FACADES))
def test_the_production_code_that_reads_the_facade_is_the_listed_code(facade: str) -> None:
    """Every read of a name off the facade in the production trees is in ``FACADE_READERS``.

    A read at import or at a call counts, and every entry is read."""
    modules = submodules(facade)
    package = Path(importlib.import_module(facade).__file__ or "").parent
    reads: dict[str, frozenset[str]] = {}
    for tree in SOURCE_TREES:
        for path in sorted((REPO_ROOT / tree).rglob("*.py"), key=lambda path: path.as_posix()):
            if path.parent == package:
                continue
            if names := facade_reads(path.read_text(encoding="utf-8"), facade, modules):
                reads[path.relative_to(REPO_ROOT).as_posix()] = names
    assert reads == FACADE_READERS[facade]


_PLANTED_READER = """
import swcraft_tools.query
from swcraft_tools import query
from swcraft_tools.query import integral


def run():
    from swcraft_tools.query import answer_request

    return query.split_where, query.request.JOIN_MAX_ENTRIES, swcraft_tools.query.related, integral
"""


def test_the_reader_scan_fires_on_a_planted_module() -> None:
    """The reader scan sees each read form and no non-read.

    A read through a bound name, through the dotted facade and by a function's own import is seen; a submodule's name
    and a top-level import are not reads of the facade."""
    modules = submodules("swcraft_tools.query")
    assert facade_reads(_PLANTED_READER, "swcraft_tools.query", modules) == {"answer_request", "related", "split_where"}
