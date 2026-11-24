"""A scratch git repository a test makes starts no background maintenance.

``commit``, ``merge``, ``fetch``, ``rebase``, ``am`` and a push's receiving side end with ``git maintenance run
--auto`` (or ``gc --auto``), which on Windows runs detached and can pack, prune or move loose objects after the
command has returned. A test that reads, damages or counts objects right after its own commit then races a process
it never started.

:func:`quiet_maintenance` writes the two keys into the repository's own configuration, right after ``git init``,
``git init --bare`` or ``git clone`` and before the first commit. The repository's file is read by every git process
in that repository: the commands the test spells, and the ones product code under test runs on its own, including a
git that switches off the global and system files. ``-c`` arguments would reach only the former, and
``GIT_CONFIG_COUNT`` in the environment would change what the tests that set or strip those variables prove.

:data:`NO_AUTO_MAINTENANCE` is for a repository that is not the test's, where writing its configuration would change
someone else's checkout: the commands the test runs carry the arguments instead.

This module serves the ``tools``, ``mcp``, ``scripts`` and closed test trees;
``packages/agent/tests/support/scratch_git.py`` is the copy for the ``agent`` tree. What it does not reach: a
repository product code creates and commits in one call, with no moment between the two for a test to quiet it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

#: The keys :func:`quiet_maintenance` writes, with their values.
_QUIET = (("maintenance.auto", "false"), ("gc.auto", "0"))

#: The same keys as ``-c`` arguments, for a git command run in a repository the test must not reconfigure.
NO_AUTO_MAINTENANCE = tuple(word for key, value in _QUIET for word in ("-c", f"{key}={value}"))


def quiet_maintenance(repo: Path) -> None:
    """Switch automatic maintenance off in *repo*'s own configuration (its git directory, wherever that is)."""
    for key, value in _QUIET:
        result = subprocess.run(
            ["git", "-C", str(repo), "config", "--local", key, value],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, f"git config {key} in {repo} failed: {result.stderr}"


#: A test identity for the commands :func:`git` runs: a CI runner has no git identity of its own.
_IDENTITY = ("-c", "user.name=Test", "-c", "user.email=test@example.com")


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run git in *cwd* under the test identity; an ``init`` there also quiets the new repository's maintenance.

    A ``commit`` otherwise starts a detached ``git maintenance run --auto``, which can pack or move loose objects
    while a test still reads or damages them. The quieting reaches *cwd*'s repository, a separate git directory
    included, so a caller inits in *cwd* itself.
    """
    result = subprocess.run(["git", *_IDENTITY, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    if args[:1] == ("init",):
        quiet_maintenance(cwd)
    return result
