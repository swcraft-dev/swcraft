"""Load ``packages/tools/tests/conftest.py`` for the tests under ``scripts/tests/``.

These tests use the ``tools`` conftest's script loaders, fixtures, hooks and options, so it is
registered as a plugin and its non-hook names are re-exported (``from conftest import REPO_ROOT``
works as in ``packages/tools/tests``).
"""

from __future__ import annotations

from pathlib import Path

import tools_conftest_bridge

_tools_conftest = tools_conftest_bridge.load_tools_conftest()
globals().update(tools_conftest_bridge.exported_names(_tools_conftest))

pytest_plugins = [tools_conftest_bridge.MODULE_NAME]

#: The modules that load the ``tools`` conftest; the never-skips guards scan them too.
LOADER_MODULES = (Path(__file__), Path(tools_conftest_bridge.__file__))
