"""The wire unit is a leaf (:mod:`swcraft_tools.wire_unit`), and ``editing/read.py`` re-exports it.

Two claims, each pinned here:

* **the leaf** -- the module imports the standard library alone, so a module outside
  ``editing/`` (which may not import ``editing/``) can measure in the server's unit
  without a cycle and without a second spelling of the formula;
* **one spelling** -- ``read.compact_json``, ``read.escaped_bytes`` and ``read.measure``
  are the leaf's own functions, not copies, so every caller that kept its
  ``read`` import measures with the same code.

What the formula answers is ``packages/mcp/tests/test_response_budget.py``'s, which reads
the one block off a live in-memory client result and holds ``read.measure`` to it; this
file does not restate it. What this file does not reach: a module that imports the
formula's building blocks (``json``) and spells the formula again by hand -- no scan
looks for that.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest
from swcraft_tools import wire_unit
from swcraft_tools.editing import read

MODULE_PATH = Path(wire_unit.__file__)


def _imported_modules() -> set[str]:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


class TestTheModuleIsALeaf:
    def test_it_imports_the_standard_library_alone(self) -> None:
        roots = {module.split(".")[0] for module in _imported_modules()}
        foreign = sorted(root for root in roots if root not in sys.stdlib_module_names and root != "__future__")
        assert foreign == [], f"wire_unit.py must import the standard library alone; found {foreign}"

    def test_it_imports_nothing_from_swcraft_tools(self) -> None:
        offenders = sorted(module for module in _imported_modules() if module.split(".")[0] == "swcraft_tools")
        assert offenders == [], f"wire_unit.py must stay a leaf module; found {offenders}"

    def test_it_uses_no_relative_imports_either(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        relative = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level > 0]
        assert relative == [], "wire_unit.py must stay a leaf module; found a relative import"

    def test_the_scan_sees_a_planted_third_party_import(self) -> None:
        # The guard bites: `pydantic` is not a standard-library root.
        tree = ast.parse("from pydantic import BaseModel\nimport json\n")
        roots = {
            alias.name.split(".")[0] if isinstance(node, ast.Import) else node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        assert sorted(root for root in roots if root not in sys.stdlib_module_names) == ["pydantic"]


class TestReadReExportsTheLeaf:
    @pytest.mark.parametrize("name", ["compact_json", "escaped_bytes", "measure"])
    def test_the_read_name_is_the_leaf_function(self, name: str) -> None:
        assert getattr(read, name) is getattr(wire_unit, name)

    @pytest.mark.parametrize("name", ["compact_json", "escaped_bytes", "measure"])
    def test_read_still_exports_the_name(self, name: str) -> None:
        assert name in read.__all__

    def test_the_leaf_holds_no_budget(self) -> None:
        # The response budget stays in `editing/read.py`; the leaf is the unit only.
        assert not hasattr(wire_unit, "RESPONSE_BUDGET_WIRE_BYTES")
        assert read.RESPONSE_BUDGET_WIRE_BYTES == 80_000


class TestTheUnit:
    def test_measure_is_the_escaped_compact_document(self) -> None:
        payload = {"text": 'a "quoted"\nline', "n": 1, "name": "Größe"}
        assert wire_unit.compact_json(payload) == '{"text":"a \\"quoted\\"\\nline","n":1,"name":"Größe"}'
        assert wire_unit.measure(payload) == wire_unit.escaped_bytes(wire_unit.compact_json(payload))

    def test_escaped_bytes_counts_utf8_after_the_json_escape(self) -> None:
        # Two quotes, each escaped once (+2), and one two-byte character.
        assert wire_unit.escaped_bytes('"ä"') == len('"\\"ä\\""'.encode())
