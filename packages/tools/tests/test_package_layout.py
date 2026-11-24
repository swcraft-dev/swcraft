"""The source trees hold no module that pytest would collect.

A ``test_*.py`` or ``conftest.py`` under ``packages/*/src/`` would be shipped in
a wheel, matched by pytest's default collection pattern, and taken for a pytest
node id by every check that tells evidence citations from test names. The scan
walks the directories under each project's ``src/`` on disk; it does not reach a
module a build step generates elsewhere, nor a name outside pytest's default
patterns.
"""

from __future__ import annotations

from pathlib import Path

from conftest import REPO_ROOT


def _source_roots() -> list[Path]:
    return sorted((REPO_ROOT / "packages").glob("*/src"), key=lambda path: path.as_posix())


def _named_like_a_test(path: Path) -> bool:
    return path.name == "conftest.py" or (path.name.startswith("test_") and path.suffix == ".py")


def test_the_source_roots_are_found() -> None:
    names = {root.parent.name for root in _source_roots()}
    assert {"agent", "mcp", "specimens", "tools"} <= names


def test_no_source_module_is_named_like_a_test() -> None:
    offenders = sorted(
        (
            path.relative_to(REPO_ROOT).as_posix()
            for root in _source_roots()
            for path in root.rglob("*.py")
            if _named_like_a_test(path)
        ),
    )
    assert offenders == [], f"modules under packages/*/src named like a pytest module: {offenders}"
