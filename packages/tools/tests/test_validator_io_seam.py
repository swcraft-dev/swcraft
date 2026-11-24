"""No module of the validator package reads storage except ``model_view.py``.

Every check reads the model through a ``ModelView``. This scan parses every module of the
package and fails on a call, outside ``model_view.py``, to a layer reader, to one of the named
storage and root-taking helpers, to ``open``, or to a path or ``os`` function that reads, probes or
lists the disk; and on an import of any of those names, so an alias cannot hide a call. A call of a
``ModelView`` method on any receiver is the view's own and passes, unless the receiver is a module the
source imports (``model_state.declared_identity(...)``).

**What it does not reach.** The lists are closed by name: I/O behind another module's function that is
not on them is not seen, a path method is recognised by its name alone, and a name reached by a string
(``getattr(reader, "read_tasks")``) or a module bound by a plain ``import`` and called by an
attribute not on the lists is not seen. Nor does it reach the laziness itself: a check still reads the
disk *through* the view on its first access to a layer, and no test runs a check without a disk.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import validator_package
from swcraft_tools import reader
from swcraft_tools.validator.model_view import ModelView

#: The module that is the edge.
EDGE = "model_view.py"

#: Every layer reader ``reader.py`` defines.
READER_FUNCTIONS: frozenset[str] = frozenset(
    node.name
    for node in ast.parse(Path(reader.__file__ or "").read_text(encoding="utf-8")).body
    if isinstance(node, ast.FunctionDef) and node.name.startswith(("read_", "find_"))
)

#: The storage and root-taking helpers outside the package.
STORAGE_HELPERS: frozenset[str] = frozenset(
    {
        "scan_layer",
        "declared_identity",
        "load_model_state",
        "read_data_description",
        "analyze_resource_budgets",
        "identities_for",
        "satisfying_elements",
        "open",
    }
)

#: Path methods and ``os``/``os.path`` functions that read, probe or list the disk, recognised by name
#: on any receiver.
PATH_METHODS: frozenset[str] = frozenset(
    {
        "read_text",
        "read_bytes",
        "open",
        "exists",
        "glob",
        "rglob",
        "iterdir",
        "stat",
        "lstat",
        "is_file",
        "is_dir",
        "isfile",
        "isdir",
        "listdir",
        "scandir",
        "walk",
    }
)

#: The modules whose disk functions an import may bind by name.
OS_MODULES: frozenset[str] = frozenset({"os", "os.path"})

FORBIDDEN: frozenset[str] = READER_FUNCTIONS | STORAGE_HELPERS

#: The view's own public methods and properties: a call of one on a view is the edge, not a read.
VIEW_MEMBERS: frozenset[str] = frozenset(name for name in vars(ModelView) if not name.startswith("_"))


def storage_reads(source: str, *, label: str) -> list[str]:
    """Every forbidden call or import in *source*, as ``label:line name``."""
    tree = ast.parse(source)
    imported_modules = {
        alias.asname or alias.name.partition(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None and not node.module.startswith("typing")
        for alias in node.names
    }
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found.extend(
                f"{label}:{node.lineno} imports {alias.name}"
                for alias in node.names
                if alias.name in FORBIDDEN or (node.module in OS_MODULES and alias.name in PATH_METHODS)
            )
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN:
                found.append(f"{label}:{node.lineno} calls {func.id}")
            elif isinstance(func, ast.Attribute) and _reads(func, imported_modules):
                found.append(f"{label}:{node.lineno} calls .{func.attr}")
    return found


def _reads(func: ast.Attribute, imported_modules: set[str]) -> bool:
    """Whether the attribute call *func* reads: a disk name, or a forbidden name not the view's own."""
    if func.attr in PATH_METHODS:
        return True
    if func.attr not in FORBIDDEN:
        return False
    on_a_module = isinstance(func.value, ast.Name) and func.value.id in imported_modules
    return on_a_module or func.attr not in VIEW_MEMBERS


def test_the_lists_are_populated() -> None:
    assert {"read_swc_model", "read_interfaces", "read_hardware", "read_sequences"} <= READER_FUNCTIONS


def test_the_edge_is_one_of_the_packages_modules_and_it_reads() -> None:
    (edge,) = [path for path in validator_package.paths() if path.name == EDGE]

    assert storage_reads(edge.read_text(encoding="utf-8"), label=EDGE)


def test_no_module_of_the_package_but_the_edge_reads_storage() -> None:
    found = [
        read
        for path in validator_package.paths()
        if path.name != EDGE
        for read in storage_reads(path.read_text(encoding="utf-8"), label=path.name)
    ]

    assert found == [], "storage read outside the view:\n" + "\n".join(found)


@pytest.mark.parametrize(
    "planted",
    [
        "from swcraft_tools.reader import read_swc_model\n\ndef f(root):\n    return read_swc_model(root)\n",
        "from swcraft_tools import reader\n\ndef f(root):\n    return reader.read_tasks(root)\n",
        "from swcraft_tools.reader import read_hardware as hw\n",
        "from swcraft_tools.model_state import scan_layer\n",
        "def f(root):\n    return identities_for(root)\n",
        "def f(root):\n    return satisfying_elements(root)\n",
        "def f(root):\n    return analyze_resource_budgets(root)\n",
        "def f(path):\n    return open(path).read()\n",
        "def f(path):\n    return path.read_text(encoding='utf-8')\n",
        "def f(path):\n    return list(path.glob('*.yaml'))\n",
        "def f(path):\n    return path.exists()\n",
        "def f(path):\n    return path.is_file()\n",
        "def f(path):\n    return path.is_dir()\n",
        "def f(path):\n    return path.lstat()\n",
        "import os\n\ndef f(path):\n    return os.listdir(path)\n",
        "import os\n\ndef f(path):\n    return list(os.scandir(path))\n",
        "import os\n\ndef f(path):\n    return list(os.walk(path))\n",
        "import os\n\ndef f(path):\n    return os.path.isfile(path)\n",
        "from os import listdir\n",
        "from swcraft_tools import model_state\n\ndef f(path):\n    return model_state.declared_identity(path, 'id')\n",
    ],
)
def test_a_planted_module_that_reads_is_refused(planted: str) -> None:
    assert storage_reads(planted, label="planted.py")


def test_a_module_reading_through_the_view_passes() -> None:
    source = (
        "def f(model_root):\n"
        "    view = as_view(model_root)\n"
        "    return view.swcs, view.stored_text('swc', 'swc/a/A.yaml')\n"
        "\n"
        "def g(view, path):\n"
        "    return view.declared_identity('swc', path), view.layer_present('hardware'), view.stored_paths('swc')\n"
    )

    assert storage_reads(source, label="clean.py") == []
