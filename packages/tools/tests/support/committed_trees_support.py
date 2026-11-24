"""The model trees this repository holds, for the gates that judge every one of them.

A *tree* is a directory with a layer directory (``swc/``, ``interfaces/``, ...) directly below it.
The list is asked of **git** (``tracked_files_support.listed_files``), never of the filesystem:
the files git tracks plus the ones it would track (untracked and not ignored), so a fixture just
added is judged before it is staged and a generated payload a build left in a source tree --
gitignored package data, a ``dist/`` -- is not; a filesystem walk would make the parameter set
depend on what the checkout had built.

What this does not reach: a tree outside the four bases, a directory that merely holds a
subdirectory named like a layer (a fixtures container holding ``sequences/``) -- it is listed, and
the gate that judges it says what it does with a tree that does not load or is refused -- and the
partial trees under :data:`PARTIAL_TREE_BASES`, which are not models.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from conftest import REPO_ROOT, layer_directories
from tracked_files_support import listed_files

#: The bases searched: the example projects and the three Python projects' fixtures.
BASES = ("examples", "packages/tools", "packages/mcp", "packages/agent")

#: Directory names no tree is looked for below.
SKIPPED = frozenset(
    {".venv", "node_modules", ".git", "dist", "out", "__pycache__", ".pytest_cache", ".claude", "views"}
)


#: The bases whose trees are partial by design, never a whole model: the agent task benchmark's
#: expected states, each the files a right run leaves, laid over a copy of its bench by the checker
#: (``packages/tools/tests/fixtures/agent-bench/README.md``). No tree under them is listed.
PARTIAL_TREE_BASES = (
    "packages/tools/tests/fixtures/agent-bench/expected/",
    "packages/tools/tests/fixtures/agent-bench/large/expected/",
)


@cache
def _committed_model_roots() -> tuple[str, ...]:
    layers = frozenset(layer_directories())
    roots: set[str] = set()
    for name in listed_files(*BASES):
        parts = name.split("/")
        if SKIPPED.intersection(parts) or name.startswith(PARTIAL_TREE_BASES):
            continue
        # The last part is the file; a layer directory needs a parent to be the tree.
        roots.update("/".join(parts[:index]) for index in range(1, len(parts) - 1) if parts[index] in layers)
    return tuple(sorted(roots))


def committed_model_roots() -> list[Path]:
    """Every tree, as an absolute path, in the order of its repository-relative POSIX spelling."""
    return [REPO_ROOT / root for root in _committed_model_roots()]


def tree_id(root: Path) -> str:
    """*root* as its repository-relative POSIX spelling: the name a gate lists a tree by."""
    return root.relative_to(REPO_ROOT).as_posix()
