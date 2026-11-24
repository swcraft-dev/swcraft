"""The import boundaries of the trace link search's Apply and of the modules around it.

Each rule is a pure function over a parsed module, checked against the real modules and shown to **fire**
against a synthetic offender -- a guard never seen to fail proves nothing.

**The rules:**

- ``trace_terms.py`` imports nothing from ``swcraft_tools``: the leaf the search and the requirements
  import's report share;
- ``trace_records.py`` imports neither ``writer``, ``editing``, ``allocation`` nor ``scipy``;
- ``trace_search.py`` imports none of ``writer``, ``editing``, ``allocation``, ``scipy`` or ``trace_apply``;
- ``trace_apply.py`` imports ``writer.elements`` and neither ``editing``, ``allocation`` nor ``scipy``;
- ``requirements_import.py`` imports ``trace_terms`` and not ``trace_search``.

**The edges.** ``trace_search`` imports ``trace_records`` at its top; both take the target address from the
leaf ``trace_address``, so the two never import each other. ``trace_apply`` imports two private names of
``requirements_import`` (``create_staging``, ``visible``). The last test witnesses that ``generate arxml``
and ``validate`` over ``examples/basic`` load neither the search, its records nor Apply.

**What these do not reach:** an import spelled through ``importlib`` or ``__import__`` with a computed name
(the raw-text check refuses the two names outright in the four modules), and a module these import that
itself imports a forbidden one -- their own imports are the boundary stated, not their closure.

**The change report:** ``change_report.py`` reads exactly one private name of another module,
``writer.arxml_generation._build_document`` (by a ``from`` import or off an imported module), and imports
``writer.diagrams.change``; ``change_report_render.py`` imports no ``writer`` module. The CLI imports both
inside ``change-report``'s command, which keeps them off the ARXML path. What this does not reach: an import's closure.
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from swcraft_tools import (
    change_report,
    change_report_render,
    requirements_import,
    trace_address,
    trace_apply,
    trace_records,
    trace_search,
    trace_terms,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The four trace modules, by the name the rules use.
TRACE_MODULES = {
    "trace_apply": Path(trace_apply.__file__),
    "trace_records": Path(trace_records.__file__),
    "trace_search": Path(trace_search.__file__),
    "trace_terms": Path(trace_terms.__file__),
}
REQUIREMENTS_IMPORT = Path(requirements_import.__file__)

#: The change report's two modules.
CHANGE_REPORT_MODULES = {
    "change_report": Path(change_report.__file__),
    "change_report_render": Path(change_report_render.__file__),
}
ARXML_GENERATION = "swcraft_tools.writer.arxml_generation"
DIAGRAMS_CHANGE = "swcraft_tools.writer.diagrams.change"
BUILD_REPORT = "swcraft_tools.build_report"

WRITER = "swcraft_tools.writer"
WRITER_ELEMENTS = "swcraft_tools.writer.elements"
EDITING = "swcraft_tools.editing"
ALLOCATION = "swcraft_tools.allocation"
SCIPY = "scipy"
TRACE_ADDRESS = "swcraft_tools.trace_address"
TRACE_APPLY = "swcraft_tools.trace_apply"
TRACE_RECORDS = "swcraft_tools.trace_records"
TRACE_SEARCH = "swcraft_tools.trace_search"
TRACE_TERMS = "swcraft_tools.trace_terms"
REQUIREMENTS_IMPORT_MODULE = "swcraft_tools.requirements_import"

#: Each module's forbidden roots: a name equal to a root or beneath it is refused.
FORBIDDEN: dict[str, tuple[str, ...]] = {
    "trace_terms": ("swcraft_tools",),
    "trace_records": (WRITER, EDITING, ALLOCATION, SCIPY),
    "trace_search": (WRITER, EDITING, ALLOCATION, SCIPY, TRACE_APPLY),
    "trace_apply": (EDITING, ALLOCATION, SCIPY),
}


def _module_names(node: ast.Import | ast.ImportFrom) -> set[str]:
    """The module names one import statement names, a relative one spelled from the package."""
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names}
    base = node.module or ""
    if node.level:
        base = f"swcraft_tools.{base}" if base else "swcraft_tools"
    return {base, *(f"{base}.{alias.name}" for alias in node.names)}


def imported_modules(tree: ast.Module) -> set[str]:
    """Every module name *tree* imports, anywhere in it."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            names |= _module_names(node)
    return names


def top_level_imports(tree: ast.Module) -> set[str]:
    """The module names *tree* imports at module level (the imports that run when it is loaded)."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            names |= _module_names(node)
    return names


def _within(name: str, root: str) -> bool:
    return name == root or name.startswith(f"{root}.")


def forbidden_imports(tree: ast.Module, roots: tuple[str, ...]) -> list[str]:
    """The imported names *roots* forbid, sorted."""
    return sorted(name for name in imported_modules(tree) if any(_within(name, root) for root in roots))


def names_imported_from(tree: ast.Module, module: str) -> set[str]:
    """The names a ``from <module> import ...`` statement of *tree* takes, anywhere in it."""
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == module and not node.level
        for alias in node.names
    }


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# ---------------------------------------------------------------------------
# The rules on the real modules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", sorted(FORBIDDEN))
def test_no_trace_module_imports_a_forbidden_one(module: str) -> None:
    assert forbidden_imports(_tree(TRACE_MODULES[module]), FORBIDDEN[module]) == []


@pytest.mark.parametrize("module", sorted(TRACE_MODULES))
def test_no_dynamic_import_is_spelled(module: str) -> None:
    text = TRACE_MODULES[module].read_text(encoding="utf-8")
    assert "importlib" not in text and "__import__" not in text


def test_apply_edits_the_text_and_imports_no_element_writer() -> None:
    """Apply inserts the satisfies lines into the file's text: no writer serializes a file."""
    imports = top_level_imports(_tree(TRACE_MODULES["trace_apply"]))
    assert WRITER_ELEMENTS not in imports and "swcraft_tools.trace_satisfies_text" in imports


def test_apply_takes_the_staging_directory_from_the_requirements_import() -> None:
    """The one edge from Apply into the import: two private names, the staging directory and its visibility rule."""
    assert names_imported_from(_tree(TRACE_MODULES["trace_apply"]), REQUIREMENTS_IMPORT_MODULE) == {
        "create_staging",
        "visible",
    }


def test_the_requirements_import_reaches_the_terms_and_no_other_trace_module() -> None:
    names = imported_modules(_tree(REQUIREMENTS_IMPORT))
    assert TRACE_TERMS in names
    assert [
        name for name in sorted(names) if any(_within(name, m) for m in (TRACE_SEARCH, TRACE_RECORDS, TRACE_APPLY))
    ] == []


def test_the_search_imports_the_records_and_both_take_the_address_from_the_leaf() -> None:
    """No cycle: the search imports the records at its top, the records import nothing of the search anywhere."""
    records = _tree(TRACE_MODULES["trace_records"])
    search = _tree(TRACE_MODULES["trace_search"])
    address = _tree(Path(trace_address.__file__))
    assert any(_within(name, TRACE_RECORDS) for name in top_level_imports(search))
    assert not any(_within(name, TRACE_SEARCH) for name in imported_modules(records))
    assert TRACE_ADDRESS in top_level_imports(records)
    assert TRACE_ADDRESS in top_level_imports(search)
    assert forbidden_imports(address, ("swcraft_tools",)) == []


# ---------------------------------------------------------------------------
# Each rule fired on a synthetic offender
# ---------------------------------------------------------------------------

OFFENDER = ast.parse(
    "from swcraft_tools import allocation\n"
    "from .writer.mutations import swc_to_dict\n"
    "import scipy.optimize\n"
    "from swcraft_tools.editing import engine\n"
    "import os\n"
    "def later():\n"
    "    from swcraft_tools import trace_apply\n"
)


def test_the_leaf_rule_fires_on_any_package_import() -> None:
    offender = ast.parse("import re\nfrom swcraft_tools._version import __version__\n")
    assert forbidden_imports(offender, FORBIDDEN["trace_terms"]) == [
        "swcraft_tools._version",
        "swcraft_tools._version.__version__",
    ]


def test_the_records_rule_fires_on_an_offender() -> None:
    assert forbidden_imports(OFFENDER, FORBIDDEN["trace_records"]) == [
        "scipy.optimize",
        "swcraft_tools.allocation",
        "swcraft_tools.editing",
        "swcraft_tools.editing.engine",
        "swcraft_tools.writer.mutations",
        "swcraft_tools.writer.mutations.swc_to_dict",
    ]


def test_the_search_rule_fires_on_apply_even_inside_a_function() -> None:
    assert "swcraft_tools.trace_apply" in forbidden_imports(OFFENDER, FORBIDDEN["trace_search"])


def test_the_apply_rule_fires_on_an_offender_and_lets_the_writer_through() -> None:
    offender = ast.parse(
        "from swcraft_tools.writer.elements import element_to_dict\n"
        "from swcraft_tools.editing import engine\n"
        "import scipy\n"
        "from swcraft_tools import allocation\n"
    )
    assert forbidden_imports(offender, FORBIDDEN["trace_apply"]) == [
        "scipy",
        "swcraft_tools.allocation",
        "swcraft_tools.editing",
        "swcraft_tools.editing.engine",
    ]
    assert WRITER_ELEMENTS in top_level_imports(offender)
    assert WRITER_ELEMENTS not in top_level_imports(
        ast.parse("def later():\n    from swcraft_tools.writer import elements\n")
    )


def test_the_staging_edge_check_fires_on_a_wider_import() -> None:
    offender = ast.parse(
        "from swcraft_tools.requirements_import import create_staging, visible, plan_requirements_import\n"
    )
    assert names_imported_from(offender, REQUIREMENTS_IMPORT_MODULE) == {
        "create_staging",
        "visible",
        "plan_requirements_import",
    }


def test_the_direction_check_fires_on_the_inverted_edge_and_on_a_deferred_import() -> None:
    inverted = ast.parse("def later():\n    from swcraft_tools.trace_search import TargetAddress\n")
    assert any(_within(name, TRACE_SEARCH) for name in imported_modules(inverted))
    deferred = ast.parse("def run():\n    from swcraft_tools import trace_records\n")
    assert not any(_within(name, TRACE_RECORDS) for name in top_level_imports(deferred))
    top = ast.parse("from swcraft_tools import trace_records\n")
    assert any(_within(name, TRACE_RECORDS) for name in top_level_imports(top))
    assert forbidden_imports(ast.parse("from swcraft_tools.trace import parse_link\n"), ("swcraft_tools",)) == [
        "swcraft_tools.trace",
        "swcraft_tools.trace.parse_link",
    ]


# ---------------------------------------------------------------------------
# The change report's one private name
# ---------------------------------------------------------------------------


def private_names_taken(tree: ast.Module) -> set[tuple[str, str]]:
    """``(module, name)`` for every private name a ``from swcraft_tools... import _name`` of *tree* takes."""
    return {
        (node.module, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and not node.level
        and node.module is not None
        and _within(node.module, "swcraft_tools")
        for alias in node.names
        if alias.name.startswith("_") and not alias.name.startswith("__")
    }


def _dotted(node: ast.expr) -> str | None:
    """``a.b.c`` for a chain of attribute reads on a name, else ``None``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base is not None else None
    return None


def private_names_read(tree: ast.Module) -> set[tuple[str, str]]:
    """:func:`private_names_taken`, plus every ``(module, name)`` *tree* reads off a ``swcraft_tools`` module.

    A ``module._name`` read counts through a name an import binds (``from``, or ``import ... as``), or spelled out in
    full (``swcraft_tools.x.y._name`` after a plain ``import swcraft_tools.x.y``)."""
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and not node.level
            and node.module
            and _within(node.module, "swcraft_tools")
        ):
            bound.update({alias.asname or alias.name: f"{node.module}.{alias.name}" for alias in node.names})
        elif isinstance(node, ast.Import):
            bound.update(
                {
                    alias.asname: alias.name
                    for alias in node.names
                    if alias.asname and _within(alias.name, "swcraft_tools")
                }
            )
    read: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr.startswith("_") and not node.attr.startswith("__")):
            continue
        if isinstance(node.value, ast.Name) and node.value.id in bound:
            read.add((bound[node.value.id], node.attr))
        elif (owner := _dotted(node.value)) is not None and _within(owner, "swcraft_tools"):
            read.add((owner, node.attr))
    return private_names_taken(tree) | read


def test_the_change_report_reads_one_private_name_of_another_module() -> None:
    report = _tree(CHANGE_REPORT_MODULES["change_report"])
    assert private_names_read(report) == {(ARXML_GENERATION, "_build_document")}
    assert private_names_read(_tree(CHANGE_REPORT_MODULES["change_report_render"])) == set()
    assert {ARXML_GENERATION, DIAGRAMS_CHANGE} <= imported_modules(report)


def test_the_change_report_renderer_imports_no_writer() -> None:
    assert forbidden_imports(_tree(CHANGE_REPORT_MODULES["change_report_render"]), (WRITER,)) == []


@pytest.mark.parametrize("module", sorted(CHANGE_REPORT_MODULES))
def test_no_dynamic_import_is_spelled_in_the_change_report(module: str) -> None:
    text = CHANGE_REPORT_MODULES[module].read_text(encoding="utf-8")
    assert "importlib" not in text and "__import__" not in text


def test_the_private_name_check_fires_on_a_second_name_and_a_top_level_import() -> None:
    offender = ast.parse(
        "from swcraft_tools.writer.arxml_generation import _build_document\n"
        "from swcraft_tools.differ import _render_key, __all__\n"
        "from lxml import _private\n"
    )
    assert private_names_taken(offender) == {
        (ARXML_GENERATION, "_build_document"),
        ("swcraft_tools.differ", "_render_key"),
    }
    assert ARXML_GENERATION in top_level_imports(offender)


def test_the_private_name_check_fires_on_a_read_off_an_imported_module() -> None:
    offender = ast.parse(
        "from swcraft_tools.writer import arxml_generation\n"
        "import swcraft_tools.differ as differ\n"
        "import swcraft_tools.impact\n"
        "import numpy as np\n"
        "arxml_generation._build_document(root)\n"
        "differ._render_key(key)\n"
        "swcraft_tools.impact._walk(model)\n"
        "np._private\n"
        "local._private\n"
    )
    assert private_names_read(offender) == {
        (ARXML_GENERATION, "_build_document"),
        ("swcraft_tools.differ", "_render_key"),
        ("swcraft_tools.impact", "_walk"),
    }


# ---------------------------------------------------------------------------
# The consequence on the ARXML path
# ---------------------------------------------------------------------------


def test_the_arxml_path_loads_none_of_the_three_trace_modules_that_search_or_apply(tmp_path: Path) -> None:
    """``generate arxml`` and ``validate`` load none of the trace modules.

    They load neither the search, its records, Apply, the change report's two modules nor the diagram module it calls.

    ``trace_terms`` is loaded (``cli/import_commands.py`` imports ``requirements_import`` at module level) and
    brings nothing else along. Only ``validate --report`` loads the build report.
    """
    project = tmp_path / "basic"
    shutil.copytree(REPO_ROOT / "examples" / "basic", project, ignore=shutil.ignore_patterns("dist", "views"))
    manifest = str(project / "swcraft.yaml")
    names = sorted(
        [
            *(f"swcraft_tools.{name}" for name in TRACE_MODULES if name != "trace_terms"),
            *(f"swcraft_tools.{name}" for name in CHANGE_REPORT_MODULES),
            DIAGRAMS_CHANGE,
            BUILD_REPORT,
        ]
    )
    probe = (
        "import json, sys\n"
        "from swcraft_tools.cli import main\n"
        "sys.argv = ['swcraft', *sys.argv[1:]]\n"
        "code = 0\n"
        "try:\n"
        "    main()\n"
        "except SystemExit as exc:\n"
        "    code = exc.code or 0\n"
        f"names = {names!r}\n"
        "print('EXIT-CODE ' + str(code))\n"
        "print('LOADED ' + json.dumps(sorted(name for name in names if name in sys.modules)))\n"
    )
    runs = (
        (["generate", "arxml", "--project", manifest], []),
        (["validate", "--project", manifest], []),
        (["validate", "--project", manifest, "--report"], [BUILD_REPORT]),
    )
    for argv, loaded in runs:
        completed = subprocess.run(
            [sys.executable, "-c", probe, *argv], capture_output=True, text=True, encoding="utf-8", timeout=300
        )
        lines = completed.stdout.splitlines()
        assert "EXIT-CODE 0" in lines, completed.stderr
        assert f"LOADED {json.dumps(loaded)}" in lines, completed.stdout
