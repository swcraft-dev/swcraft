"""The four leaves that broke the package's import cycles, and the import-graph gate that keeps them broken.

Each leaf took definitions from a former owner, which re-exports the names its importers take
(``import X as X``). A re-export is the same object at both paths, so code that imports the old
place and code that imports the leaf agree on identity (an ``except`` clause, an ``isinstance``,
a ``dict`` key). Then the import-linter contracts ``packages/tools/pyproject.toml`` declares, run on a
copy of the package in ``tmp_path``: silent on the copy as it is, and failing on each planted edge.

Each package-shape contract (``guide_reference/``, ``query/``, ``cli/``) names its submodules by hand, so a test
holds that list to the package's files: a new submodule cannot escape the contract by being left off it.

**What these do not reach:** an importer of a moved name that no test exercises, and the
contracts' own blind spots (``packages/tools/pyproject.toml`` names them: an import spelled through
``importlib`` or ``__import__``, and a cycle between modules that are not siblings).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from conftest import dev_module
from swcraft_tools import cli, guide_reference, toolchain_pin, trace_address, trace_search
from swcraft_tools.component_exchange import capabilities, report, report_lines
from swcraft_tools.guide_reference import catalogue

TOOLS_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = TOOLS_ROOT / "src" / "swcraft_tools"

#: (former owner, leaf, the names the owner re-exports).
RE_EXPORTS = (
    (cli, toolchain_pin, ("toolchain_pin_problem",)),
    (
        trace_search,
        trace_address,
        (
            "TargetAddress",
            "TraceError",
            "TraceExperimentError",
            "GroupKey",
            "address_order_key",
            "id_order_key",
            "exact_decimal",
        ),
    ),
    (capabilities, report_lines, ("NONE",)),
    (
        guide_reference,
        catalogue,
        (
            "all_artifacts",
            "nav_entries",
            "index_page",
            "INDEX_PATH",
            "NAV_SECTION_TITLE",
            "FOREIGN_PAGES",
            "UntitledPage",
            "UnsummarizedPage",
        ),
    ),
)


@pytest.mark.parametrize(
    ("owner", "leaf", "name"),
    [(owner, leaf, name) for owner, leaf, names in RE_EXPORTS for name in names],
    ids=lambda value: getattr(value, "__name__", value),
)
def test_each_re_exported_name_is_the_leafs_own_object(owner, leaf, name) -> None:
    assert getattr(owner, name) is getattr(leaf, name)


def test_the_report_names_its_regime_word_from_the_leaf_too() -> None:
    assert report.NONE is report_lines.NONE is capabilities.NONE


# The import-graph contracts, on a copy of the package


def _package_copy(root: Path) -> Path:
    """``swcraft_tools``' Python sources under *root*/src, the real ``packages/tools/pyproject.toml`` beside them."""
    shutil.copytree(
        PACKAGE,
        root / "src" / "swcraft_tools",
        ignore=shutil.ignore_patterns("__pycache__", "templates", "guide_site", "*.json", "*.yaml", "*.md"),
    )
    shutil.copyfile(TOOLS_ROOT / "pyproject.toml", root / "pyproject.toml")
    return root / "src" / "swcraft_tools"


def _lint_imports(root: Path) -> subprocess.CompletedProcess[str]:
    """``lint-imports`` as ``dev.py lint`` runs it, in *root*, with the copy first on the path."""
    dev = dev_module()
    return subprocess.run(
        [dev.import_lint_executable(sys.executable), *dev.IMPORT_LINT_ARGS],
        cwd=root,
        env={**_environment(), "PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}


def _plant(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"\n{line}\n")


@pytest.fixture
def package_copy(tmp_path: Path) -> Path:
    return _package_copy(tmp_path)


def _contracts() -> list[dict]:
    """The import-linter contracts ``packages/tools/pyproject.toml`` declares."""
    configuration = tomllib.loads((TOOLS_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return configuration["tool"]["importlinter"]["contracts"]


def test_every_contract_is_kept_on_the_package_as_it_is(package_copy: Path, tmp_path: Path) -> None:
    run = _lint_imports(tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert f"Contracts: {len(_contracts())} kept, 0 broken." in run.stdout


def test_the_acyclic_contract_breaks_on_a_cycle_hidden_in_a_function_body(package_copy: Path, tmp_path: Path) -> None:
    """The edge the trace leaf removed, planted back inside a function: grimp counts it all the same."""
    _plant(package_copy / "trace_records.py", "def _planted():\n    from swcraft_tools import trace_search")
    run = _lint_imports(tmp_path)
    assert run.returncode == 1
    assert "No import cycle between siblings BROKEN" in run.stdout
    broken = run.stdout.split("Broken contracts", 1)[1]
    assert ".trace_search" in broken
    assert ".trace_records" in broken


#: (package, a submodule to plant the forbidden import in, a name the package's ``__init__`` binds).
SHAPED_PACKAGES = (
    ("cli", "guard_command", "main"),
    ("guide_reference", "cli", "INDEX_PATH"),
    ("query", "slicing", "integral"),
)


def _shape_contract(package: str) -> dict:
    """The one ``forbidden`` contract whose forbidden module is *package*."""
    (shape,) = [
        contract
        for contract in _contracts()
        if contract["type"] == "forbidden" and contract["forbidden_modules"] == [f"swcraft_tools.{package}"]
    ]
    return shape


@pytest.mark.parametrize(("package", "submodule", "name"), SHAPED_PACKAGES)
def test_the_package_shape_contract_breaks_on_a_submodule_taking_a_name_from_the_package(
    package_copy: Path, tmp_path: Path, package: str, submodule: str, name: str
) -> None:
    _plant(package_copy / package / f"{submodule}.py", f"from swcraft_tools.{package} import {name}")
    run = _lint_imports(tmp_path)
    assert run.returncode == 1
    assert f"{_shape_contract(package)['name']} BROKEN" in run.stdout
    assert f"swcraft_tools.{package}.{submodule} -> swcraft_tools.{package}" in run.stdout


@pytest.mark.parametrize("package", [package for package, _, _ in SHAPED_PACKAGES])
def test_the_package_shape_contract_names_every_submodule(package: str) -> None:
    """``source_modules`` is exactly the package's submodules: a new one cannot be left off the contract."""
    directory = PACKAGE / package
    on_disk = {f"swcraft_tools.{package}.{path.stem}" for path in directory.glob("*.py") if path.stem != "__init__"} | {
        f"swcraft_tools.{package}.{path.name}" for path in directory.iterdir() if (path / "__init__.py").is_file()
    }
    assert set(_shape_contract(package)["source_modules"]) == on_disk


def test_every_package_with_a_shape_contract_is_held_by_the_tests_above() -> None:
    """A new package-shape contract is added to ``SHAPED_PACKAGES``, so its witness and its list are held."""
    forbidden = {
        module.removeprefix("swcraft_tools.")
        for contract in _contracts()
        if contract["type"] == "forbidden"
        for module in contract["forbidden_modules"]
    }
    assert forbidden == {package for package, _, _ in SHAPED_PACKAGES}
