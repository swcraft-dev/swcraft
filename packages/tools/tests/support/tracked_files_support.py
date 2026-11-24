"""The one listing of the repository's files that the gates reading the checkout ask for.

Four callers ask git which files to read -- ``test_agent_guide.py``, ``test_language.py``,
``committed_trees_support.py`` and ``test_build_artifacts_guide.py``'s guide-page check -- through
one helper, so they agree on what a file of the repository is: every file git tracks plus every
untracked file it does not ignore (``git ls-files --cached --others --exclude-standard``). A
file written but not yet committed is read too, and the ignored trees (``.venv``, ``dist/``,
the extension's ``.vscode-test/``) never are.

What it does not reach: a file of a submodule, and a file deleted in the worktree but still in
the index, which the listing names and the caller skips (``Path.is_file``).
"""

from __future__ import annotations

import subprocess

from conftest import REPO_ROOT


def listed_files(*scopes: str, suffixes: tuple[str, ...] | None = None) -> list[str]:
    """The POSIX names, sorted, of the listed files under *scopes* whose name ends in one of *suffixes*.

    No *scopes* means the whole tree; *suffixes* ``None`` means every listed file."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *scopes],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    return sorted(name for name in listed.split("\0") if name and (suffixes is None or name.endswith(suffixes)))
