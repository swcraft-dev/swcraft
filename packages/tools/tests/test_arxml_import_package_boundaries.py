"""The `arxml_import/` package's dependency graph, pinned rather than described.

* Every module of the package but ``__init__`` is a stage of :data:`STAGES`, and every stage names a module.
* In each such module, every absolute import of ``swcraft_tools.arxml_import.X`` read off the AST imports a
  module standing to the **left** of the importing one.

Does not reach: ``__init__.py``; the chain each module docstring states (its agreement with :data:`STAGES` is
kept by review); edges, since the chain is an ordering (`merge` before `registry` would pass, as neither imports
a module of this package); imports of `swcraft_tools` modules outside the package; a relative import
(``from .x import``); an import through `importlib`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[3] / "packages" / "tools" / "src" / "swcraft_tools" / "arxml_import"

#: The chain, module by module. `registry` and
#: `merge` share the first stage: neither imports anything of this package, and
#: the chain writes them with a comma for that reason.
STAGES: tuple[tuple[str, ...], ...] = (
    ("registry", "merge"),
    ("merge_verdict",),
    ("unresolved",),
    ("impl_type_resolution",),
    ("type_mapping",),
    ("type_mappers",),
    ("swc_mapping",),
    ("runnable_accesses",),
    ("swc_mapper",),
    ("connectors",),
    ("reconcile",),
    ("report",),
    ("orchestrator",),
    ("pipeline",),
)

#: The chain flattened for the arrow refusal: stage index by module name.
STAGE_OF = {module: index for index, stage in enumerate(STAGES) for module in stage}

#: The chain as a string, for the arrow refusal's message. Derived from
#: :data:`STAGES` rather than written out, so the two cannot disagree.
CHAIN = " -> ".join(", ".join(stage) for stage in STAGES)


def _modules() -> list[Path]:
    return sorted(
        (path for path in PACKAGE_ROOT.glob("*.py")),
        key=lambda path: path.as_posix(),
    )


def _submodules() -> list[Path]:
    """The modules the chain orders; `__init__.py` only re-exports them, so it is left out."""
    return [path for path in _modules() if path.stem != "__init__"]


def test_every_module_of_the_package_is_a_stage_of_the_chain() -> None:
    """Every module but `__init__` is a stage and every stage a module, so none of them escapes the arrow rule."""
    on_disk = {path.stem for path in _submodules()}
    assert on_disk == set(STAGE_OF), (on_disk - set(STAGE_OF), set(STAGE_OF) - on_disk)


@pytest.mark.parametrize("module", _submodules(), ids=lambda path: path.name)
def test_no_module_imports_one_that_stands_to_its_right(module: Path) -> None:
    """No module imports one that stands to its right; this also fails on any import cycle."""
    importer = STAGE_OF[module.stem]
    for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
        imported: set[str] = set()
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("swcraft_tools.arxml_import."):
            imported.add((node.module or "").split(".")[-1])
        elif isinstance(node, ast.Import):
            imported.update(
                alias.name.split(".")[-1]
                for alias in node.names
                if alias.name.startswith("swcraft_tools.arxml_import.")
            )
        for name in imported:
            assert name in STAGE_OF, f"{module.name} imports `{name}`, which the chain does not name"
            assert STAGE_OF[name] < importer, (
                f"{module.name} imports `{name}`, which the chain puts at or to its right — "
                f"either the import is a cycle or the chain is stale ({CHAIN})"
            )
