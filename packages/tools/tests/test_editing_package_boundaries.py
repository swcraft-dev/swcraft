"""The `editing/` package's three import boundaries, pinned rather than described.

**Rule 1: `editing/` imports nothing from `swcraft_mcp` or `swcraft_agent`**, and names neither
in its code (`packages/tools/pyproject.toml` depends on neither).

**Rule 2: nothing outside `editing/` in `swcraft_tools` imports `editing/`**; `validator/`
and `resource_analysis.py` place a finding in `diagnostics.Subject`, never in a protocol
address.

**Rule 3: `editing/` imports exactly what the boundary rules list**, asserted both ways
(`TestTheDeclaredImportListIsExact`).

Checked over the AST and the raw text (an `importlib` call, a function-local import);
each check is also shown to fire against a synthetic offender.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from swcraft_tools import editing as editing_package
from swcraft_tools import writer as writer_package

EDITING_DIR = Path(editing_package.__file__).parent
WRITER_DIR = Path(writer_package.__file__).parent
TOOLS_DIR = EDITING_DIR.parent

#: The two packages the editing core must stay independent of: higher layers
#: (`swcraft_mcp` wraps the tools as tool calls, `swcraft_agent` hosts the approval
#: loop), neither a dependency of `packages/tools/`.
FORBIDDEN_ROOTS = ("swcraft_mcp", "swcraft_agent")


def imported_roots(tree: ast.Module) -> set[str]:
    """Every root package name a module imports, absolute imports only.

    A relative import names something inside its own package, so it cannot reach `swcraft_mcp`.
    """
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def imported_modules(tree: ast.Module) -> set[str]:
    """Every fully qualified module name a module imports, absolute imports only."""
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def code_without_docstrings(tree: ast.Module) -> str:
    """The module's source with every docstring blanked, as text.

    Docstrings are dropped because this package's prose names both forbidden packages
    to state that it does not use them.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    node.body = body[1:] or [ast.Pass()]
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def python_files(directory: Path) -> list[Path]:
    """Every module under *directory*, recursively, so a nested subpackage is not missed."""
    return sorted(
        (p for p in directory.rglob("*.py") if "__pycache__" not in p.parts), key=lambda path: path.as_posix()
    )


EDITING_MODULES = python_files(EDITING_DIR)
WRITER_MODULES = python_files(WRITER_DIR)

#: Every production module of `swcraft_tools` outside `editing/`, in one list: rule 2
#: is a property of the package, not of a family inside it.
OUTSIDE_EDITING_MODULES = [path for path in python_files(TOOLS_DIR) if EDITING_DIR not in (path.parent, *path.parents)]


def module_id(path: Path) -> str:
    """The package-relative path, so two files with one name stay two ids."""
    for root in (EDITING_DIR, WRITER_DIR):
        if root in path.parents:
            return path.relative_to(root).as_posix()
    return path.name


def test_the_package_inventories_are_not_empty():
    # A glob that silently matches nothing turns every rule below into a vacuous pass.
    assert len(EDITING_MODULES) >= 15, EDITING_MODULES
    assert len(WRITER_MODULES) >= 5, WRITER_MODULES
    # `review.py` renders the approval question a human reads, the shape that
    # attracts a host import, so the rules must reach it by name.
    assert "review.py" in {path.name for path in EDITING_MODULES}
    outside = {path.relative_to(TOOLS_DIR).as_posix() for path in OUTSIDE_EDITING_MODULES}
    assert {"writer/elements.py", "resource_analysis.py", "diagnostics.py"} <= outside
    validator_modules = {path.relative_to(TOOLS_DIR).as_posix() for path in (TOOLS_DIR / "validator").glob("*.py")}
    assert validator_modules and validator_modules <= outside
    assert not any("editing" in path.parts for path in OUTSIDE_EDITING_MODULES)


def test_the_inventories_reach_into_subpackages(tmp_path: Path):
    # The count cannot notice a missed subpackage, so the recursion is asserted directly.
    (tmp_path / "sub" / "deeper").mkdir(parents=True)
    (tmp_path / "top.py").write_text("", encoding="utf-8")
    (tmp_path / "sub" / "deeper" / "nested.py").write_text("", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "cached.py").write_text("", encoding="utf-8")
    assert [p.name for p in python_files(tmp_path)] == ["nested.py", "top.py"]


class TestEditingIsMcpIndependent:
    """Rule 1: nothing under `editing/` may reach the MCP server or the agent."""

    @pytest.mark.parametrize("path", EDITING_MODULES, ids=module_id)
    def test_no_module_imports_the_mcp_server_or_the_agent(self, path: Path):
        offenders = sorted(imported_roots(parse(path)) & set(FORBIDDEN_ROOTS))
        assert offenders == [], (
            f"{path.name} imports {offenders}. The editing core is the "
            "provider-independent half of the editing protocol and must stay importable with "
            "neither swcraft_mcp nor swcraft_agent installed; the MCP surface "
            "depends on this package, not the other way round."
        )

    @pytest.mark.parametrize("path", EDITING_MODULES, ids=module_id)
    def test_no_module_names_them_in_its_source_at_all(self, path: Path):
        # The half that catches the back doors: `importlib.import_module(...)` and a
        # function-local import inside a `try:` are invisible to an import audit.
        # Docstrings may name the packages, so the check is on code alone.
        source = code_without_docstrings(parse(path))
        offenders = sorted(name for name in FORBIDDEN_ROOTS if name in source)
        assert offenders == [], f"{path.name} names {offenders} outside a docstring"


class TestTheEditingArrowPointsOneWay:
    """Rule 2: the whole package may import `editing/`, and none of it does."""

    def test_no_production_module_outside_the_package_imports_the_editing_package(self):
        # One sweep over every module outside `editing/`: asserting family by
        # family leaves every module nobody thought of unguarded.
        offenders = sorted(
            path.relative_to(TOOLS_DIR).as_posix()
            for path in OUTSIDE_EDITING_MODULES
            if any(m.startswith("swcraft_tools.editing") for m in imported_modules(parse(path)))
        )
        assert offenders == [], (
            f"{offenders} import the editing package. editing/ imports writer/, reader, differ, "
            "session and validator, so an import back would make the protocol a dependency of the "
            "code it is built on; the storage target is composed in editing/registry.py and passed IN."
        )


class TestTheDeclaredImportListIsExact:
    """Rule 3: `editing/` imports exactly the modules the boundary rules list, asserted both ways.

    An import the list does not name is an unrecorded dependency; a listed module nothing
    imports is prose describing a dependency that is gone.
    """

    #: The list the boundary rules publish, as import roots inside `swcraft_tools`
    #: (`editing` itself excluded). `contract` is deliberately absent: only the
    #: kernel `model_registry` reads the normative JSON. `model_state` is a value
    #: type `derive.py` composes, not a second reader; `safety` holds `asil_rank`,
    #: the one ASIL ordering, which the safety guard must not fork; `yaml_load` is
    #: there because `engine.handle_request` clears its parse cache, while every
    #: read goes through the listed modules, never `yaml` itself.
    DECLARED = frozenset(
        {
            "_version",
            "auditor",
            "authorization",
            "diagnostics",
            "differ",
            "identifier",
            "model_registry",
            "model_state",
            "project",
            # `editing/read.py` answers identity rows through `query`'s vocabulary
            # check, row collapse and whole-row cursor, stated once for both
            # surfaces; `query` imports nothing of `editing`, so it is no cycle.
            "query",
            "reader",
            "rendering",
            "safety",
            # `safety_guard._levels_spanned` resolves an `instance` entry through
            # the leaf directly rather than through `validator`, a non-leaf.
            "scheduling_identity",
            "schema",
            "session",
            "snapshot",
            # The guard reads a component's links through `trace`, the one spelling
            # of the link grammar; `trace` imports nothing that imports `editing`.
            "trace",
            # `editing/trace_candidates.py` answers `find` by calling the trace
            # search and reading the rejected decisions as `experiment trace` does:
            # a second ranker or reader would be a second truth. Neither module
            # imports `editing`, so the edges are no cycle.
            "trace_records",
            "trace_search",
            "validator",
            # `editing/diagnosis.py` reuses the event short-name counter from its
            # defining module, a member of the `validator` package already listed.
            "validator.events",
            # `editing/read.py` re-exports the wire unit from this leaf, so the
            # guide's producer and reader (outside `editing/`) measure in the
            # server's unit. The leaf imports the standard library alone.
            "wire_unit",
            "writer.arxml_generation",
            "writer.elements",
            "writer.mutations",
            "yaml_load",
        }
    )

    @staticmethod
    def imported_core_modules() -> set[str]:
        """Every `swcraft_tools.*` module `editing/` imports, `editing.*` excluded."""
        imported: set[str] = set()
        for path in EDITING_MODULES:
            for module in imported_modules(parse(path)):
                if module == "swcraft_tools" or not module.startswith("swcraft_tools."):
                    continue
                name = module[len("swcraft_tools.") :]
                if name == "editing" or name.startswith("editing."):
                    continue
                imported.add(name)
        return imported

    def test_nothing_outside_the_declared_list_is_imported(self):
        undeclared = sorted(self.imported_core_modules() - self.DECLARED)
        assert undeclared == [], (
            f"editing/ imports {undeclared}, which the boundary rules do not name. Add it to the "
            "boundary rules' list and to this one, or route the need through a module already on it."
        )

    def test_every_declared_module_is_actually_imported(self):
        stale = sorted(self.DECLARED - self.imported_core_modules())
        assert stale == [], (
            f"the boundary rules name {stale} among editing/'s imports and nothing imports them any more."
        )


class TestTheGuardsBite:
    """Each rule, shown failing against a synthetic offender."""

    def test_a_top_level_import_is_caught(self):
        tree = ast.parse("import swcraft_mcp.server\n")
        assert imported_roots(tree) & set(FORBIDDEN_ROOTS) == {"swcraft_mcp"}

    def test_a_from_import_is_caught(self):
        tree = ast.parse("from swcraft_agent.core import SwcraftAgent\n")
        assert imported_roots(tree) & set(FORBIDDEN_ROOTS) == {"swcraft_agent"}

    def test_a_function_local_import_is_caught(self):
        # The lazy-import back door: legal Python, invisible to a "top of file"
        # reading.
        tree = ast.parse("def f():\n    import swcraft_mcp\n    return swcraft_mcp\n")
        assert imported_roots(tree) & set(FORBIDDEN_ROOTS) == {"swcraft_mcp"}

    def test_an_importlib_back_door_is_caught_by_the_text_rule_only(self):
        source = 'import importlib\n\n\ndef f():\n    return importlib.import_module("swcraft_mcp.server")\n'
        tree = ast.parse(source)
        # The AST rule cannot see it; that is why the text rule exists.
        assert imported_roots(tree) & set(FORBIDDEN_ROOTS) == set()
        assert "swcraft_mcp" in code_without_docstrings(ast.parse(source))

    def test_prose_naming_a_forbidden_package_is_not_an_offence(self):
        # editing/__init__.py's own docstring names the MCP; a rule that fired on
        # prose would be worked around.
        source = '"""Mentions swcraft_mcp and swcraft_agent in prose only."""\n\nX = 1\n'
        assert "swcraft_mcp" not in code_without_docstrings(ast.parse(source))

    def test_a_writer_importing_editing_is_caught(self):
        tree = ast.parse("from swcraft_tools.editing.registry import build_registry\n")
        offenders = [m for m in imported_modules(tree) if m.startswith("swcraft_tools.editing")]
        assert offenders == ["swcraft_tools.editing.registry"]

    def test_a_relative_import_is_not_read_as_an_escape(self):
        # `from . import registry` can only name a sibling, so counting it would
        # be a false positive on every module.
        tree = ast.parse("from . import registry\nfrom .address import Address\n")
        assert imported_roots(tree) == set()
