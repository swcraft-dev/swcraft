"""The user's SWCraft home and the review hand-off directory below it: a stdlib-only leaf.

The home rule has **three** copies, which must not drift: this module,
``scripts/workstation/install.py``'s ``default_home()``, which writes the home,
and ``vscode_extension/src/toolchain.ts``'s ``toolchainHome()``, which reads it.
It imports nothing of ``swcraft_tools``, so the guard's hook pays nothing for it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

__all__ = ["REVIEWS_DIRNAME", "reviews_location", "swcraft_home"]

_POSIX_HOME_DIRNAME = ".swcraft"
_WINDOWS_HOME_DIRNAME = "SWCraft"

#: The review hand-off directory's name inside the home.
REVIEWS_DIRNAME = "reviews"


def swcraft_home(env: Mapping[str, str], platform: str, home: PurePath) -> PurePath:
    """The user's SWCraft home: ``~/.swcraft``, or ``%LOCALAPPDATA%\\SWCraft`` on Windows.

    *platform* is :data:`os.name` (``"nt"`` or ``"posix"``) and *home* the user's home directory,
    passed in so the other platform's answer is computable. It never refuses: with neither variable
    set it answers from *home*, and a blank value counts as unset.
    """
    if platform != "nt":
        return PurePosixPath(home) / _POSIX_HOME_DIRNAME
    local = env.get("LOCALAPPDATA", "")
    if local.strip():
        return PureWindowsPath(local) / _WINDOWS_HOME_DIRNAME
    profile = env.get("USERPROFILE", "")
    base = profile if profile.strip() else home
    return PureWindowsPath(base) / "AppData" / "Local" / _WINDOWS_HOME_DIRNAME


def reviews_location() -> Path:
    """``<SWCraft home>/reviews`` for this process's environment, as a path, without creating it.

    Raises:
        RuntimeError: :meth:`Path.home` cannot determine the home directory.
    """
    return Path(swcraft_home(os.environ, os.name, Path.home())) / REVIEWS_DIRNAME
