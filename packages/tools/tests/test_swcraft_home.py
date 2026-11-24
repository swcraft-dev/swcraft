"""``swcraft_tools.swcraft_home`` -- the user's SWCraft home and the review hand-off directory below it.

The same literal cases as ``vscode_extension/src/toolchain.test.ts``'s ``toolchainHome`` tests (the three
copies of the rule must not drift: this leaf, ``install.py``'s ``default_home()``, ``toolchain.ts``), plus the
blank-variable case, ``reviews_location()`` under a redirected environment, and the leaf's boundary: it imports
nothing of ``swcraft_tools``.

What this does not reach: ``install.py``'s and ``toolchain.ts``'s own copies, which their own tests hold to
the same literals.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
from swcraft_tools import swcraft_home
from swcraft_tools.swcraft_home import REVIEWS_DIRNAME, reviews_location


class TestTheHome:
    def test_posix_is_dot_swcraft_and_ignores_localappdata(self) -> None:
        home = swcraft_home.swcraft_home({"LOCALAPPDATA": "/somewhere/else"}, "posix", PurePosixPath("/Users/dev"))
        assert home.as_posix() == "/Users/dev/.swcraft"

    def test_windows_is_localappdata_swcraft(self) -> None:
        home = swcraft_home.swcraft_home(
            {"LOCALAPPDATA": "C:\\Users\\dev\\AppData\\Local"}, "nt", PureWindowsPath("C:\\Users\\dev")
        )
        assert home == PureWindowsPath("C:\\Users\\dev\\AppData\\Local\\SWCraft")

    def test_windows_without_localappdata_falls_back_to_userprofile(self) -> None:
        home = swcraft_home.swcraft_home({"USERPROFILE": "C:\\Users\\dev"}, "nt", PureWindowsPath("C:\\Users\\other"))
        assert home.as_posix() == "C:/Users/dev/AppData/Local/SWCraft"

    def test_windows_with_neither_answers_from_the_home_directory(self) -> None:
        home = swcraft_home.swcraft_home({}, "nt", PureWindowsPath("C:\\Users\\dev"))
        assert home.as_posix() == "C:/Users/dev/AppData/Local/SWCraft"

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_variable_counts_as_unset(self, blank: str) -> None:
        home = swcraft_home.swcraft_home(
            {"LOCALAPPDATA": blank, "USERPROFILE": blank}, "nt", PureWindowsPath("C:\\Users\\dev")
        )
        assert home.as_posix() == "C:/Users/dev/AppData/Local/SWCraft"


class TestTheReviewsLocation:
    def test_it_is_reviews_under_this_processs_home_and_is_not_created(self, tmp_path: Path, monkeypatch) -> None:
        for variable in ("HOME", "LOCALAPPDATA", "USERPROFILE"):
            monkeypatch.setenv(variable, str(tmp_path))
        expected = tmp_path / "SWCraft" / "reviews" if os.name == "nt" else tmp_path / ".swcraft" / "reviews"
        location = reviews_location()
        assert location.as_posix() == expected.as_posix()
        assert location.name == REVIEWS_DIRNAME
        assert not location.exists()


def test_the_leaf_imports_nothing_of_the_package() -> None:
    """A stdlib-only leaf: the guard's hook and ``swcraft_mcp`` import it for one rule and pay for nothing else."""
    tree = ast.parse(Path(swcraft_home.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    } | {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert imported <= {"__future__", "os", "collections", "pathlib"}, imported
