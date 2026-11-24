"""Every path a helper in ``tests/support/`` computes from its own ``__file__`` names what it means.

The helpers live one directory below the test root, so a wrong depth reads a sibling that does
not exist, or the wrong repository level. Most such paths are read only when a test needs the
data, and the real-export suites read theirs
only where the exports exist: a wrong depth would then be found by nothing that runs everywhere.
These cases hold each computed path to an existence check and the repository root to
``conftest.REPO_ROOT``. Two more properties of the folder are held here: no helper's module name is
one the interpreter would otherwise import (``support/`` sits on a declared ``pythonpath``, ahead of
the standard library and site-packages), and ``tools_conftest_bridge`` loads this suite's conftest
and re-exports its non-hook names. The closed tests' helpers are held by the closed half of this check.

What this does not reach: a path a helper builds inside a function body rather than as a module
constant, and a helper that computes no path from ``__file__`` (``xsd_support``,
``reference_oracle``, ``arxml_export_oracle`` and ``report_family_support`` compute none).
"""

from __future__ import annotations

import importlib.machinery
import sys
from pathlib import Path
from types import ModuleType

import arxml_import_shapes_support
import behaviour_losses_support
import data_event_support
import exclusive_area_support
import external_stubs_support
import guide_fixtures
import inter_runnable_variable_support
import io_oracle_xsd_map
import model_contract_fixtures
import operation_event_support
import passport_support
import pytest
import symbol_citations
import tools_conftest_bridge
from conftest import CONFTEST_PATH, REPO_ROOT

#: ``packages/tools/tests/support/``, the folder every helper module lives in.
SUPPORT_DIR = Path(tools_conftest_bridge.__file__).resolve().parent

#: Each directory a helper computes, by ``module.CONSTANT``.
COMPUTED_DIRECTORIES: dict[str, Path] = {
    "arxml_import_shapes_support.SHAPES_DIR": arxml_import_shapes_support.SHAPES_DIR,
    "data_event_support.DATA_EVENT_MODEL_ROOT": data_event_support.DATA_EVENT_MODEL_ROOT,
    "exclusive_area_support.AREA_MODEL_ROOT": exclusive_area_support.AREA_MODEL_ROOT,
    "external_stubs_support.COMPOSITIONS_MODEL_ROOT": external_stubs_support.COMPOSITIONS_MODEL_ROOT,
    "inter_runnable_variable_support.VARIABLE_MODEL_ROOT": inter_runnable_variable_support.VARIABLE_MODEL_ROOT,
    "model_contract_fixtures.SYNTHETIC_ROOT": model_contract_fixtures.SYNTHETIC_ROOT,
    "operation_event_support.MIXED_EVENT_MODEL_ROOT": operation_event_support.MIXED_EVENT_MODEL_ROOT,
    "passport_support.PASSPORT_VECTORS_DIR": passport_support.PASSPORT_VECTORS_DIR,
}

#: Each file a helper computes, by ``module.CONSTANT``.
COMPUTED_FILES: dict[str, Path] = {
    "behaviour_losses_support.MINIMAL_SWC": behaviour_losses_support.MINIMAL_SWC,
    "io_oracle_xsd_map.DATA_MODULE": io_oracle_xsd_map.DATA_MODULE,
}

#: Each repository root a helper computes, by ``module.CONSTANT``.
COMPUTED_ROOTS: dict[str, Path] = {
    "guide_fixtures.REPO_ROOT": guide_fixtures.REPO_ROOT,
}


@pytest.mark.parametrize("name", sorted(COMPUTED_DIRECTORIES))
def test_a_computed_directory_exists(name: str) -> None:
    assert COMPUTED_DIRECTORIES[name].is_dir(), f"{name} names no directory"


@pytest.mark.parametrize("name", sorted(COMPUTED_FILES))
def test_a_computed_file_exists(name: str) -> None:
    assert COMPUTED_FILES[name].is_file(), f"{name} names no file"


@pytest.mark.parametrize("name", sorted(COMPUTED_ROOTS))
def test_a_computed_root_is_the_repository_root(name: str) -> None:
    assert COMPUTED_ROOTS[name] == REPO_ROOT, f"{name} is not the repository root"


def test_the_package_root_is_the_tools_package() -> None:
    """``symbol_citations.PACKAGE_ROOT`` is ``swcraft_tools`` itself, not a directory of that name."""
    assert (symbol_citations.PACKAGE_ROOT / "__init__.py").is_file()


def test_no_helper_shadows_an_importable_module() -> None:
    """No module of ``support/`` has a name the interpreter finds without the test paths.

    The test trees' ``pythonpath`` puts ``support/`` ahead of the standard library and
    site-packages, so a helper named like one of them would replace it in every test process.
    """
    test_paths = {SUPPORT_DIR, SUPPORT_DIR.parent}
    other_paths = [entry for entry in sys.path if entry and Path(entry).resolve() not in test_paths]
    names = sorted(path.stem for path in SUPPORT_DIR.glob("*.py"))
    shadowing = [
        name
        for name in names
        if name in sys.stdlib_module_names
        or name in sys.builtin_module_names
        or importlib.machinery.PathFinder.find_spec(name, other_paths) is not None
    ]
    assert names, "support/ holds no module"
    assert shadowing == [], f"helpers named like an importable module: {shadowing}"


def test_the_bridge_loads_this_suites_conftest() -> None:
    assert tools_conftest_bridge.TOOLS_CONFTEST == CONFTEST_PATH


def test_the_bridge_returns_a_module_already_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded = ModuleType(tools_conftest_bridge.MODULE_NAME)
    monkeypatch.setitem(sys.modules, tools_conftest_bridge.MODULE_NAME, loaded)
    assert tools_conftest_bridge.load_tools_conftest() is loaded


def test_the_bridge_re_exports_every_name_but_the_hooks_and_the_dunders() -> None:
    module = ModuleType("a_conftest")
    vars(module).update(REPO_ROOT=REPO_ROOT, CONFTEST_PATH=CONFTEST_PATH, pytest_addoption=print)
    exported = tools_conftest_bridge.exported_names(module)
    assert set(exported) == {"REPO_ROOT", "CONFTEST_PATH"}
