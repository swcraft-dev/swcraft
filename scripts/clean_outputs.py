"""Move a named ARXML aside and remove the other named generated files, inside the repository only.

    python scripts/clean_outputs.py PATH [PATH ...]

``scripts/dev.py build``'s first step, before ``check-model``: after any failing build
the generated artifacts are absent under their own names, never stale-but-present.

A named path with the suffix ``.arxml`` (any case) is moved to ``<stem>_old<suffix>``
beside it (an existing ``_old`` replaced), so one previous ARXML survives under a name
no build writes. :func:`previous_arxml_path` re-implements the writer's
``swcraft_tools.writer.arxml_xml.previous_arxml_path`` (this script imports nothing of
the product); ``scripts/tests/test_dev_build.py`` holds the two equal.

A separate script because ``dev.py`` plans are lists of subprocess invocations only,
which is what lets them be tested with an injected runner.

It moves or removes named files only; no directory is walked or emptied, so
``audit.jsonl`` beside the ARXML is untouched. Stdlib only, no shell.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def resolve_target(name: str, repo_root: Path = REPO_ROOT) -> Path:
    """Resolve *name* under *repo_root*, refusing to escape it.

    The check is made on the resolved path, so neither an absolute path nor ``..`` escapes.
    """
    target = (repo_root / name).resolve()
    if repo_root not in target.parents:
        raise ValueError(f"refusing to touch a path outside the repository: {name}")
    return target


def previous_arxml_path(target: Path) -> Path:
    """``dist/model.arxml`` -> ``dist/model_old.arxml``: where the previous ARXML is kept."""
    return target.with_name(f"{target.stem}_old{target.suffix}")


def move_aside(target: Path) -> Path | None:
    """Move the ARXML *target* to :func:`previous_arxml_path`; the new path, or ``None`` when absent.

    An existing ``_old`` file is replaced; a directory is refused.
    """
    if target.is_dir():
        raise ValueError(f"refusing to move a directory: {target}")
    if not target.exists():
        return None
    previous = previous_arxml_path(target)
    target.replace(previous)
    return previous


def remove_file(target: Path) -> bool:
    """Delete *target* if it is there; ``True`` when something was removed.

    A missing file is not an error; a directory is refused rather than recursed into.
    """
    if target.is_dir():
        raise ValueError(f"refusing to remove a directory: {target}")
    if not target.exists():
        return False
    target.unlink()
    return True


def main(argv: Sequence[str] | None = None, repo_root: Path = REPO_ROOT) -> int:
    names = list(sys.argv[1:] if argv is None else argv)
    if not names:
        print("Usage: python scripts/clean_outputs.py PATH [PATH ...]", file=sys.stderr)
        return 2

    for name in names:
        try:
            target = resolve_target(name, repo_root)
            if target.suffix.lower() == ".arxml":
                kept = move_aside(target)
                if kept is not None:
                    print(f"Previous ARXML kept: {kept}")
                continue
            removed = remove_file(target)
        except ValueError as error:
            print(f"Error: {error}", file=sys.stderr)
            return 2
        except OSError as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1
        if removed:
            print(f"Removed {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
