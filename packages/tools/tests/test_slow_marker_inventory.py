"""The inventory of ``slow``-marked suites, derived from the test trees.

:func:`slow_marked_modules` parses each test module for a real ``pytest.mark.slow`` attribute -- a
decorator, a ``pytestmark``, an entry in a list of marks; a mention in a docstring, comment or regex
literal is invisible. The two marker tables that name the ``slow`` suites by file are held to this
inventory by the closed half of this check; this module checks that it reads every project's tests.

What this does **not** reach: a ``slow`` mark applied dynamically -- ``pytest.ini``
markers, a ``conftest`` hook, a decorator built at run time -- because it never
imports or collects anything.
"""

from __future__ import annotations

import ast

from conftest import REPO_ROOT, dev_module

#: The test trees ``dev.py test-slow`` runs, in the order the stage runs them.
_TEST_ROOTS = (
    "packages/tools/tests",
    "packages/mcp/tests",
    "packages/agent/tests",
    "packages/specimens/tests",
    "scripts/tests",
)


def is_slow_mark(node: ast.AST) -> bool:
    """Return whether *node* is the expression ``…mark.slow``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "slow"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "mark"
    )


def slow_marked_modules() -> list[str]:
    """Every test module carrying a real ``pytest.mark.slow``, repo-relative POSIX."""
    found = []
    for root in _TEST_ROOTS:
        directory = REPO_ROOT / root
        for path in sorted(directory.rglob("test_*.py"), key=lambda item: item.as_posix()):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
            if any(is_slow_mark(node) for node in ast.walk(tree)):
                found.append(path.relative_to(REPO_ROOT).as_posix())
    return found


class TestEverySlowSuiteIsInTheOpenMarkerTable:
    """The inventory reads every project's tests.

    The two tables are held to it by the closed half of this check."""

    def test_it_reads_every_python_projects_tests(self) -> None:
        """The inventory reads every Python project's tests and each test-only project that owns the marker.

        ``test-slow`` runs ``-m slow`` in every project of ``dev.py``'s ``PYTHON_PROJECTS`` and in each test-only
        project that owns the marker (``scripts``); a project left out here would hold slow suites no table names."""
        dev = dev_module()
        assert _TEST_ROOTS == tuple(
            (dev.project_test_dir(REPO_ROOT, project) / "tests").relative_to(REPO_ROOT).as_posix()
            for project in dev.TEST_PROJECTS
            if project in dev.PYTHON_PROJECTS or project in dev.MARKER_STAGE_OWNERS["slow"]
        )
