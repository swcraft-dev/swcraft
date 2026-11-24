"""Deny a read or a write through the mode bits, or skip when they do not bite.

Lives beside ``conftest.py`` because ``test_reproducibility`` and ``test_template_acceptance``
fail on any skip in ``conftest.py``.

The guard is a real attempt, not a ``sys.platform`` or ``os.geteuid`` check: a process running
as root reads a mode-``000`` file, and Windows maps ``chmod`` onto a read-only attribute that
denies no read. A ``skipif`` on ``os.geteuid`` would also fail collection on Windows, which has
no ``os.geteuid``. Asking the filesystem covers both hosts, and asks it late.

What it does not reach:

- ``os.access`` answers for the process's **real** uid/gid, not its effective
  one. Nothing here runs setuid, and a suite that did would be told about a user
  other than the one about to do the read.
- It probes the **mode bits** only. A path denied by an ACL, by SELinux or by an
  immutable flag while the mode stays open is skipped here although the test it
  guards could have run. Nothing widens the probe, because a test that wants
  those denials wants to name them.
- It cannot say **why** the platform did not deny: root and Windows produce one
  message between them, and a reader who needs to tell them apart reads the
  host's configuration, not this skip.
- It restores the mode it found, unconditionally. A body that re-``chmod``s
  inside the block does not keep that change.
"""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest


@contextmanager
def read_denied(path: Path) -> Iterator[None]:
    """Make *path* unreadable for the block (mode restored on exit), or skip: not every host obeys."""
    original = stat.S_IMODE(path.stat().st_mode)
    path.chmod(0o000)
    try:
        if os.access(path, os.R_OK):
            pytest.skip(f"the mode bits deny no read here: {path.name} stays readable at mode 000")
        yield
    finally:
        path.chmod(original)


def require_executable_bit(scratch_dir: Path) -> None:
    """Skip unless the executable bit means something on *scratch_dir*'s filesystem.

    Windows has no such bit: ``os.access(path, os.X_OK)`` answers ``True`` for every existing
    file there. The probe writes a mode-``644`` file and asks whether it is still executable.

    What it does not reach: a POSIX filesystem mounted ``noexec``, where the
    bit is stored but not honoured; the probe reads the stored bit, which is
    what the unpack case asserts too.
    """
    probe = scratch_dir / "mode-bit-probe"
    probe.write_bytes(b"")
    probe.chmod(0o644)
    if os.access(probe, os.X_OK):
        pytest.skip(f"the executable bit does not bite here: {probe.name} at mode 644 is still executable")


@contextmanager
def write_denied(path: Path) -> Iterator[None]:
    """Make *path* unwritable for the block, or skip: not every host obeys.

    Written for a directory, kept ``r-x``: a refusal to create in it is the condition under
    test, an inability to list is a different one.
    """
    original = stat.S_IMODE(path.stat().st_mode)
    path.chmod(0o500)
    try:
        if os.access(path, os.W_OK):
            pytest.skip(f"the mode bits deny no write here: {path.name} stays writable at mode 500")
        yield
    finally:
        path.chmod(original)


def posix_mode_kept(scratch_dir: Path) -> bool:
    """Return whether :func:`os.chmod` keeps a POSIX mode on *scratch_dir*'s filesystem.

    Windows maps ``chmod`` onto the read-only attribute and reports another mode. The probe sets
    ``0o700`` on a directory and reads it back. It does not skip, so a test whose other assertions
    hold everywhere asks it for its mode half alone.

    What it does not reach: an ACL that grants what the mode denies; the probe reads the stored
    mode, which is what the runner's tests assert too.
    """
    probe = Path(tempfile.mkdtemp(prefix="posix-mode-probe-", dir=scratch_dir))
    try:
        probe.chmod(0o700)
        return stat.S_IMODE(probe.stat().st_mode) == 0o700
    finally:
        probe.rmdir()


def require_posix_mode(scratch_dir: Path) -> None:
    """Skip unless :func:`posix_mode_kept` answers yes for *scratch_dir*."""
    if not posix_mode_kept(scratch_dir):
        pytest.skip("os.chmod keeps no POSIX mode here: a directory set to 700 reads back otherwise")
