"""A scratch git repository a test makes starts no background maintenance.

Several git commands end with ``git maintenance run --auto``, which on Windows runs detached and can pack or move
loose objects after the command returns, racing a test that reads them.

:func:`quiet_maintenance` writes the keys into the repository's own configuration, which every git process there
reads, product code's included. :data:`NO_AUTO_MAINTENANCE` carries them as ``-c`` arguments for a repository
that is not the test's. A copy of the ``tools`` tree's module, which this tree's ``pythonpath`` does not reach.
Not reached: a repository product code creates and commits in one call.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_QUIET = (("maintenance.auto", "false"), ("gc.auto", "0"))

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


#: A CI runner has no git identity of its own.
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
