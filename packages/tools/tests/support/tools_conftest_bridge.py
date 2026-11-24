"""``packages/tools/tests/conftest.py`` for a pytest root outside ``packages/tools/``.

Two roots run tests written against the ``tools`` suite's conftest: the closed
record-consistency tests' root and ``scripts/`` (the tests of the repository's scripts). Each one's
conftest calls this module and then does the two things only a conftest can do for itself:

* it fills **its own** namespace with :func:`exported_names`, because pytest imports *that* file as
  ``conftest`` and a test's ``from conftest import REPO_ROOT`` has to keep meaning what it means in
  ``packages/tools/tests``;
* it declares ``pytest_plugins = [MODULE_NAME]``, so the fixtures **and the hooks** of the
  module :func:`load_tools_conftest` registered under that name exist in the run -- its
  ``pytest_addoption`` among them, which is why the calling conftest must be an initial conftest.

What it does not reach: pytest's assertion rewriting. The module is loaded by path before pytest
could rewrite it, so a helper's failing ``assert`` reports as a plain ``AssertionError``; each
root's ``pytest.ini`` filters the warning that says so.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

#: The module name the ``tools`` conftest is loaded and registered under.
MODULE_NAME = "tools_conftest"

#: ``packages/tools/tests/conftest.py``, one level above this module's ``support/``.
TOOLS_CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"


def load_tools_conftest() -> ModuleType:
    """:data:`TOOLS_CONFTEST` as the module :data:`MODULE_NAME`, once per interpreter.

    A module already registered under :data:`MODULE_NAME` is returned as it is.
    """
    cached = sys.modules.get(MODULE_NAME)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(MODULE_NAME, TOOLS_CONFTEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def exported_names(module: ModuleType) -> dict[str, object]:
    """The names of *module* a calling conftest re-exports: every one but the hooks and the dunders.

    A re-exported ``pytest_addoption`` would register the same options a second time (the plugin
    registers them already), and ``--require-autosar-xsd`` would fail as "already added".
    """
    return {name: value for name, value in vars(module).items() if not name.startswith(("pytest_", "__"))}
