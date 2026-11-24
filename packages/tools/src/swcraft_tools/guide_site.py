"""Where is the installed user guide? — the one question ``swcraft docs`` asks first.

The guide is served over a local HTTP server, never opened over ``file://``, where its search cannot
load its index and every directory URL degrades to a listing. This module answers which directory
holds the site that shipped with *this* copy of SWCraft. A leaf: its only ``swcraft_tools`` import is
:data:`~swcraft_tools._version.__version__`, which the refusal needs to name the release archive.

**The search order, first hit wins.**

1. :data:`GUIDE_SITE_ENV` — an explicit directory, read off the process environment (which includes
   what a nearby ``.env`` file set).
2. The package-data directory :data:`PACKAGE_SITE_DIRNAME` inside the installed package, resolved
   through ``importlib.resources``, never through ``__file__`` or a checkout path.
3. ``<dir of sys.executable>/../docs/site``, the self-contained bundle's payload. ``sys.executable``
   is not resolved through its symlink, or every virtualenv would answer with the system
   interpreter's neighbourhood.

**A directory counts as a site only if it holds** ``index.html``; an empty or half-copied tree is
absent at every step. That is a presence check, not a verification that the site is complete or
current.

**What this module does not reach.** It does not build a site, serve one, compare a site's version
against the running product, or open a browser — the command owns all four. It cannot see inside a
zipimported distribution: a package not extracted on a real filesystem reads as absent at step 2.
"""

from __future__ import annotations

import os
import sys
from importlib.resources import files as resource_files
from pathlib import Path

from swcraft_tools._version import __version__

#: The explicit-directory override, search step 1, read from ``os.environ``. A ``.env`` file
#: reaches it like every other ``SWCRAFT_*`` variable; a real process-environment value wins.
GUIDE_SITE_ENV = "SWCRAFT_GUIDE_SITE"

#: The package-data directory holding the rendered site, relative to the ``swcraft_tools`` package
#: root — search step 2, and the location the refusal names. Must equal the name the artifact build
#: writes (``scripts/build_artifacts.py``).
PACKAGE_SITE_DIRNAME = "guide_site"

#: The one file whose presence makes a directory a site; a half-copied tree is the case that lacks it.
SITE_INDEX_NAME = "index.html"

#: The release archive's filename stem — the fallback the refusal points at. Must equal the prefix
#: the artifact build names the archive with (``scripts/build_artifacts.py``), which this module
#: cannot import.
ARCHIVE_PREFIX = "swcraft-guide-"


class GuideSiteMissing(RuntimeError):
    """No directory the search knows about holds a servable guide.

    Carries the whole refusal as its message: the location looked in, a rejected override if one
    was set, and what to do instead. The caller prints it and exits non-zero.
    """


def archive_name() -> str:
    """``swcraft-guide-<version>.zip`` — the archive this build's release ships."""
    return f"{ARCHIVE_PREFIX}{__version__}.zip"


def is_site(candidate: Path) -> bool:
    """Whether *candidate* is a directory holding :data:`SITE_INDEX_NAME`."""
    return (candidate / SITE_INDEX_NAME).is_file()


def env_site_dir() -> Path | None:
    """The directory :data:`GUIDE_SITE_ENV` names, or ``None`` when it names none.

    A blank value is ``None``: ``Path("")`` would search the working directory.
    """
    raw = os.environ.get(GUIDE_SITE_ENV, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def package_site_dir() -> Path:
    """The package-data site directory — search step 2, always a path.

    A distribution not extracted onto a filesystem yields a path that does not exist, which the
    caller treats as absent.
    """
    return Path(str(resource_files("swcraft_tools") / PACKAGE_SITE_DIRNAME))


def bundle_site_dir() -> Path | None:
    """``<dir of sys.executable>/../docs/site`` — the self-contained bundle's payload.

    Not resolved through a symlink. ``None`` when ``sys.executable`` is empty (an embedded
    interpreter), which would otherwise search the relative path ``docs/site``.
    """
    if not sys.executable:
        return None
    return Path(sys.executable).parent.parent / "docs" / "site"


def site_root() -> Path:
    """The directory holding the installed guide, searched in the documented order.

    Returns:
        The first of the three candidates that holds :data:`SITE_INDEX_NAME`, as resolved — the
        override as the user spelled it, with ``~`` expanded.

    Raises:
        GuideSiteMissing: when none of the three does.
    """
    override = env_site_dir()
    package_dir = package_site_dir()
    for candidate in (override, package_dir, bundle_site_dir()):
        if candidate is not None and is_site(candidate):
            return candidate
    raise GuideSiteMissing(_refusal(override, package_dir))


def _refusal(override: Path | None, package_dir: Path) -> str:
    """The message a reader gets when no guide is installed.

    It names where it looked (the package-data location, never the interpreter-relative sibling),
    what would have put a guide there, what to do instead, and a rejected override.
    """
    lines = [
        "This copy of SWCraft carries no user guide to serve.",
        "",
        f"  looked for: {package_dir}",
    ]
    if override is not None:
        lines.append(f"  {GUIDE_SITE_ENV} is set to {override}, which holds no {SITE_INDEX_NAME}")
    lines.extend(
        [
            "",
            "An installed release of the swcraft-tools package carries the guide at that location.",
            f"Otherwise take {archive_name()} from the release, unpack it, and run the command again",
            f"with {GUIDE_SITE_ENV} set to the unpacked directory.",
        ]
    )
    return "\n".join(lines)
