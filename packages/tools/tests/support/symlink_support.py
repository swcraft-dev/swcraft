"""Create a symlink, or skip when the platform refuses to make one.

Lives beside ``conftest.py`` because ``test_reproducibility`` and ``test_template_acceptance``
fail on any skip in ``conftest.py``. The guard is a real attempt rather than a ``sys.platform``
check: Windows creates symlinks only with ``SeCreateSymbolicLinkPrivilege`` (administrator or
Developer Mode), and on a machine that has it the test runs and proves what it claims.

``SWCRAFT_REQUIRE_SYMLINKS=1`` turns this module's skip for a link that cannot be made into a
failure. ``quick.yml`` and ``verify.yml`` set it at job level: their runners can make links, so a
link case that skipped there would pass for a proof it never gave. Unset, or anything but ``1``,
the cases skip. No product code reads it. What it does not reach: a case that skips through its
own ``pytest.skip``; the Node tests, which read the variable in their own probe; and
:func:`skip_without_symlink_support`'s last skip (a relative forward-slash link that is made but
does not resolve), which stays a skip because it is a property of the Windows runner, not a link
that cannot be made.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

#: The environment variable that turns this module's skips into failures.
REQUIRE_SYMLINKS_VARIABLE = "SWCRAFT_REQUIRE_SYMLINKS"


def symlinks_required() -> bool:
    """Return whether ``SWCRAFT_REQUIRE_SYMLINKS`` is ``1``: a link that cannot be made then fails."""
    return os.environ.get(REQUIRE_SYMLINKS_VARIABLE) == "1"


def _skip_or_fail(reason: str) -> None:
    """Skip with *reason*, or fail with it under ``SWCRAFT_REQUIRE_SYMLINKS=1``."""
    if symlinks_required():
        pytest.fail(f"{reason} ({REQUIRE_SYMLINKS_VARIABLE}=1 requires them)")
    pytest.skip(reason)


def symlink_or_skip(link: Path, target: Path) -> None:
    """Create *link* -> *target*, or skip: Windows needs a privilege for this."""
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - platform dependent
        _skip_or_fail(f"symlinks are not available here: {exc}")


def skip_without_symlink_support(scratch: Path) -> None:
    """Skip unless this host lets *this* process make a symlink in *scratch*.

    Guards a test whose symlink is made by the code under test (``replace_link`` given the
    ``"posix"`` platform in ``test_workstation_install.py``), where a Windows host without
    ``SeCreateSymbolicLinkPrivilege`` raises ``WinError 1314`` for its configuration, not for the
    installer.

    Two probes, because creating a link is not the whole capability: the installer's POSIX links
    are relative with forward slashes (``../runtime/node/bin/node``), and on the Windows runner
    such a link is created and ``islink`` but does not ``exists``, while the backslash spelling
    resolves. The installer never writes one on Windows, so a host where it does not resolve skips.

    What it does not reach: it answers for *scratch*'s filesystem and this process's privileges
    at the moment of the call; a different directory or a privilege dropped later is not covered.
    """
    target = scratch / "_symlink_probe_target"
    link = scratch / "_symlink_probe_link"
    target.mkdir(exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - platform dependent
        _skip_or_fail(f"symlinks are not available here: {exc}")
    else:
        link.unlink()
    finally:
        target.rmdir()

    inside = scratch / "_symlink_probe_dir"
    inside.mkdir(exist_ok=True)
    file = inside / "_symlink_probe_file"
    file.write_bytes(b"probe")
    relative_link = scratch / "_symlink_probe_relative"
    try:
        os.symlink("_symlink_probe_dir/_symlink_probe_file", relative_link)
        resolves = os.path.exists(relative_link)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - platform dependent
        _skip_or_fail(f"a relative symlink cannot be made here: {exc}")
    finally:
        if os.path.lexists(relative_link):
            os.unlink(relative_link)
        file.unlink()
        inside.rmdir()
    if not resolves:  # pragma: no cover - platform dependent
        # A plain skip under SWCRAFT_REQUIRE_SYMLINKS=1 too: the link was made; not
        # resolving it is a property of the host, not a missing capability.
        pytest.skip("a relative forward-slash symlink is created but does not resolve on this host")
