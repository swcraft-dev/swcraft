"""The Moon workspace is gone from SWCraft itself, and kept for the architecture project.

1. **The deletion is complete.** No ``moon.yml`` survives outside the shipped templates, no code
   names the deleted sequential runner, ``.prototools`` carries no ``moon`` pin, and no authored
   document instructs a ``moon run`` task other than the architecture project's own build.
2. **The deletion is scoped.** The scaffolded architecture project still builds with
   ``moon run :build``, and ``python scripts/dev.py test-model-integration`` **fails** (never skips)
   without moon.

Every rule is a pure function over text, so each is also shown to *fire* against a mutated copy.

What this does not reach: a surviving ``.moon/`` directory or file other than a ``moon.yml``; and
whether any document still names moon as a prerequisite of SWCraft development.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from conftest import REPO_ROOT, dev_module

_PROTOTOOLS = REPO_ROOT / ".prototools"
_GITIGNORE = REPO_ROOT / ".gitignore"

#: The *templates* directory: the one place a ``moon.yml`` legitimately survives is the
#: architecture project's build graph, shipped inside the ``swcraft-tools`` wheel.
_TEMPLATES_DIR = REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "templates"

#: Directories a repo-wide sweep must not descend into. A git worktree kept inside the repository
#: is a second full copy of it, and descending into one reports that copy's template `moon.yml`,
#: its `.moon/` directory and its copy of this module as live residue.
_PRUNED = {
    ".git",
    ".claude",
    ".venv",
    "node_modules",
    "dist",
    "out",
    ".moon",
    ".ruff_cache",
    ".pytest_cache",
}


_DEV = dev_module()


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------

#: The only ``moon run`` invocation the docs may still instruct: the scaffolded architecture
#: project's own build. Public: the closed half of this check imports it.
ALLOWED_MOON_COMMAND = "moon run :build"


def runnable_moon_lines(text: str) -> list[str]:
    """Lines that literally instruct running moon: a bare ``moon ...`` line in a code block, not prose."""
    return [
        stripped
        for line in text.splitlines()
        if (stripped := line.strip()).startswith("moon ") and not stripped.startswith("#")
    ]


def forbidden_moon_lines(text: str) -> list[str]:
    """Runnable moon lines that are **not** the architecture project's build."""
    return [line for line in runnable_moon_lines(text) if line != ALLOWED_MOON_COMMAND]


def prototools_pins(text: str) -> dict[str, str]:
    """The ``tool = "version"`` pins declared in ``.prototools``."""
    pins: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        pins[key.strip()] = value.strip().strip('"')
    return pins


#: Spelled without its ``.py`` so the constant reads as a name, not a path this repo still resolves.
_SEQUENCE_RUNNER_STEM = "moon_run_sequence"


def code_string_constants(path: Path) -> list[str]:
    """Every string literal in a module **except** its docstrings (comments never reach the AST)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


def code_modules_outside_docs(repo_root: Path) -> list[Path]:
    """Every ``.py`` under *repo_root*, pruned folders and ``docs/`` left out, in POSIX order.

    Shared with the closed half of this check.
    """
    return [
        path
        for path in sorted(repo_root.rglob("*.py"), key=lambda path: path.as_posix())
        if not any(part in _PRUNED for part in path.relative_to(repo_root).parts)
        and path.relative_to(repo_root).parts[0] != "docs"
    ]


def names_the_sequential_runner(path: Path) -> bool:
    """Whether a string constant of *path* (docstrings aside) names the deleted sequential runner."""
    return any(_SEQUENCE_RUNNER_STEM in constant for constant in code_string_constants(path))


def stray_moon_yml_files(repo_root: Path) -> list[str]:
    """Every ``moon.yml`` in the tree that is not part of the shipped template."""
    stray: list[str] = []
    for path in repo_root.rglob("moon.yml"):
        if any(part in _PRUNED for part in path.relative_to(repo_root).parts):
            continue
        if _TEMPLATES_DIR in path.parents:
            continue
        # `as_posix()`: on Windows `str()` yields `tools\moon.yml`, which failed this guard and made
        # `test_a_template_moon_yml_is_not_caught` pass vacuously.
        stray.append(path.relative_to(repo_root).as_posix())
    return sorted(stray)


# ---------------------------------------------------------------------------
# The deletion is complete
# ---------------------------------------------------------------------------


class TestTheMoonWorkspaceIsGone:
    def test_no_stray_moon_yml_survives_anywhere(self):
        assert stray_moon_yml_files(REPO_ROOT) == []

    def test_nothing_still_loads_the_sequential_runner(self):
        """No code string constant names the runner; a docstring or comment explaining it is prose.

        This file is excluded because it holds the search pattern as a literal.
        """
        offenders = [
            path.relative_to(REPO_ROOT).as_posix()
            for path in code_modules_outside_docs(REPO_ROOT)
            if path != Path(__file__) and names_the_sequential_runner(path)
        ]
        assert offenders == [], f"the deleted sequential runner is still named in code by {offenders}"


class TestPrototoolsKeepsTheLanguagePinsOnly:
    def _pins(self) -> dict[str, str]:
        return prototools_pins(_PROTOTOOLS.read_text(encoding="utf-8"))

    def test_the_moon_pin_is_dropped(self):
        assert "moon" not in self._pins(), (
            ".prototools must no longer pin moon: SWCraft declares no Moon workspace, and the "
            "architecture project carries its own pin in the scaffolded template"
        )

    def test_the_python_and_node_pins_stay(self):
        pins = self._pins()
        assert "python" in pins and "node" in pins, (
            ".prototools is kept for its language pins -- only the moon entry is removed"
        )

    def test_the_file_itself_is_kept(self):
        assert _PROTOTOOLS.is_file()

    def test_the_moon_cache_rule_lives_in_the_template_and_not_here(self):
        """The repository's `.gitignore` drops `.moon/cache/`; the template's own `.gitignore` keeps it."""
        assert ".moon/cache/" not in _GITIGNORE.read_text(encoding="utf-8").splitlines()
        template_ignore = _TEMPLATES_DIR / "vehicle-architecture" / ".gitignore"
        assert ".moon/cache/" in template_ignore.read_text(encoding="utf-8").splitlines()


# ---------------------------------------------------------------------------
# The removal is scoped: the product still uses Moon
# ---------------------------------------------------------------------------


class TestTheArchitectureProjectKeepsMoon:
    def test_the_template_still_ships_a_moon_build_graph(self):
        template = _TEMPLATES_DIR / "vehicle-architecture"
        assert (template / "moon.yml").is_file()
        assert (template / ".moon" / "workspace.yml").is_file()
        assert (template / ".prototools").is_file()

    def test_the_template_pins_moon_for_the_architecture_project(self):
        pins = prototools_pins((_TEMPLATES_DIR / "vehicle-architecture" / ".prototools").read_text(encoding="utf-8"))
        assert "moon" in pins, (
            "the architecture project builds with moon, so its own .prototools must pin it -- "
            "that pin moving out of SWCraft is the whole point of Stage 5"
        )

    def test_the_mandatory_acceptance_still_fails_loudly_without_moon(self, tmp_path):
        """Dropping the pin does not turn the acceptance into a skip."""
        context = _DEV.Context(repo_root=tmp_path, python=str(tmp_path / "python"), has_moon=False)
        with pytest.raises(_DEV.DevError) as excinfo:
            _DEV.plan_test_model_integration(context)
        assert "moon" in str(excinfo.value)
        assert "must not be skipped" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The docs instruct the native commands
# ---------------------------------------------------------------------------

#: The open root document swept; the closed half of this check applies this module's readings to the
#: closed ones.
_DOC_FILES = ("README.md",)

#: Directories of *authored guidance* swept for Moon task references as well: none in the open
#: tree. The closed half of this check sets this tuple to the record trees it sweeps.
_SWEPT_DOC_DIRS: tuple[str, ...] = ()

#: A Moon *task target* reference in prose, backticked or not; only ALLOWED_MOON_COMMAND may survive.
_MOON_TASK_PATTERN = re.compile(r"moon run \S+")


def swept_doc_files() -> list[Path]:
    """Every authored guidance document, plus README.md."""
    found = [REPO_ROOT / name for name in _DOC_FILES]
    for directory in _SWEPT_DOC_DIRS:
        found.extend(sorted((REPO_ROOT / directory).rglob("*.md"), key=lambda path: path.as_posix()))
    return found


#: Punctuation and markup that can trail a command quoted in prose.
_TRAILING_MARKUP = "`.,;:)*\"'"


def named_moon_tasks(text: str) -> list[str]:
    """Moon task targets a document instructs, other than the allowed build."""
    targets = (match.rstrip(_TRAILING_MARKUP) for match in _MOON_TASK_PATTERN.findall(text))
    return [target for target in targets if target != ALLOWED_MOON_COMMAND]


class TestDocsInstructTheNativeCommands:
    def test_no_authored_document_instructs_a_deleted_moon_task(self):
        """The sweep reaches every authored guidance document, not only the roots."""
        offenders = {
            path.relative_to(REPO_ROOT).as_posix(): named
            for path in swept_doc_files()
            if (named := named_moon_tasks(path.read_text(encoding="utf-8")))
        }
        assert offenders == {}, f"documents naming Moon tasks this repository no longer declares: {offenders}"


class TestTheGuardsFire:
    """A guard that has never been seen to fail proves nothing."""

    def test_a_moon_run_command_line_is_caught(self):
        assert forbidden_moon_lines("```sh\nmoon run root:test\n```") == ["moon run root:test"]

    def test_prose_mentioning_moon_is_not_caught(self):
        text = "Build the architecture project with `moon run :build`.\n# moon run tools:test\n"
        assert forbidden_moon_lines(text) == []

    def test_the_architecture_build_is_allowed(self):
        assert forbidden_moon_lines("```sh\nmoon run :build\n```") == []

    def test_a_reintroduced_moon_pin_is_caught(self):
        pins = prototools_pins('python = "3.11.13"\nmoon = "2.3.5"\nnode = "22.13.0"\n')
        assert pins["moon"] == "2.3.5"

    def test_a_reintroduced_moon_yml_is_caught(self, tmp_path):
        (tmp_path / "tools").mkdir()
        (tmp_path / "tools" / "moon.yml").write_text("tasks: {}\n", encoding="utf-8")
        assert stray_moon_yml_files(tmp_path) == ["tools/moon.yml"]

    def test_code_naming_the_deleted_runner_is_caught(self, tmp_path):
        module = tmp_path / "loader.py"
        module.write_text('"""A docstring is not code."""\nRUNNER = "scripts/moon_run_sequence.py"\n', encoding="utf-8")
        assert any(_SEQUENCE_RUNNER_STEM in constant for constant in code_string_constants(module))

    def test_a_docstring_naming_the_deleted_runner_is_not_caught(self, tmp_path):
        module = tmp_path / "prose.py"
        module.write_text(
            '"""This replaced scripts/moon_run_sequence.py."""\n\n\ndef f() -> None:\n    """Ditto."""\n',
            encoding="utf-8",
        )
        assert code_string_constants(module) == []

    def test_a_template_moon_yml_is_not_caught(self):
        """The sweep must not flag the shipped architecture-project build graph."""
        assert "packages/tools/src/swcraft_tools/templates/vehicle-architecture/moon.yml" not in stray_moon_yml_files(
            REPO_ROOT
        )
