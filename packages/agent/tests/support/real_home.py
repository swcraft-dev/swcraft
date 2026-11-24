"""The real user home, captured once at import under the real environment.

The tests redirect ``HOME``, ``LOCALAPPDATA`` and ``USERPROFILE`` to a per-test directory; the worktree
fixture's ``git`` calls get the real values back, because git reads its global configuration from the home
(on the Windows runner, the ``core.longpaths true`` a deep temp checkout needs).
"""

import os

HOME_VARIABLES = ("HOME", "LOCALAPPDATA", "USERPROFILE")

REAL_HOME_ENVIRONMENT = {name: os.environ[name] for name in HOME_VARIABLES if name in os.environ}

#: Unset at import: a caller restoring the real home removes these, because Git for Windows reads an invented
#: ``HOME`` before the real ``USERPROFILE``.
REAL_HOME_UNSET = tuple(name for name in HOME_VARIABLES if name not in os.environ)
