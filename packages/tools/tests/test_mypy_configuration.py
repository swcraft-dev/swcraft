"""No setting hides an error from ``lint``'s mypy, in any of the four Python projects.

``scripts/run_python_lint.py`` runs mypy in each project configured by its ``[tool.mypy]`` table alone,
and the zero it reports means nothing if the configuration hides errors. These tests read each
project's table with ``tomllib``, the configuration files mypy would read instead, and the comment
tokens of the files the table names, and refuse:

- ``ignore_errors``, a non-empty ``disable_error_code``, ``exclude``, or a ``follow_imports`` other
  than ``normal``, in a table or one of its overrides; a boolean key counts as mypy reads it (a string
  ``"true"``, ``"yes"`` or a ``1`` hides as ``true`` does, an unreadable value counts too; the dashed
  ``ignore-errors`` mypy does not honour in ``pyproject.toml``, so it is not read);
- a ``files`` list that does not hold the project's package source tree (a broader list is allowed);
- a ``mypy.ini``, a ``.mypy.ini`` or a ``setup.cfg`` with a ``[mypy`` section in a project directory;
- an inline ``# mypy:`` directive, or a ``# type: ignore`` before a module's first statement (which
  ignores the whole module), in a file the table names.

The tables also keep the integrity flags and the shared platform, and no table or override naming a
``swcraft_`` module sets ``ignore_missing_imports``; an override pattern that reaches one of ours
without spelling the ``swcraft_`` prefix (``*.cli``) is not caught.

What these tests do not reach: a coded ``# type: ignore[code]`` on a line, which is allowed; ``Any``
from the untyped third parties ``ignore_missing_imports`` lets in; a user-level mypy configuration on
the host; the checks only ``--strict`` adds; and the tests of tools, mcp and agent, which no table
names. The ratchet's mypy count (``scripts/quality_ratchet.py``) reads no project table and no
configuration file, so an inline ``# mypy:`` directive and a whole-module ``# type: ignore`` are held
by :func:`test_no_named_file_carries_a_hiding_comment` alone.
"""

from __future__ import annotations

import configparser
import io
import re
import tokenize
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from conftest import REPO_ROOT

PROJECTS_ROOT = REPO_ROOT / "packages"

#: The four Python projects, each with a ``[tool.mypy]`` table.
PROJECTS = ("tools", "mcp", "agent", "specimens")

#: The product projects whose ``[tool.mypy]`` table is plain (not ``strict``).
TYPED_PROJECTS = ("tools", "mcp", "agent")

#: The configuration files mypy reads before ``pyproject.toml`` (``setup.cfg`` only with a ``[mypy`` section).
SHADOWING_FILES = ("mypy.ini", ".mypy.ini")

DIRECTIVE = re.compile(r"#\s*mypy\s*:")
WHOLE_MODULE_IGNORE = re.compile(r"#\s*type\s*:\s*ignore\b")


class Hiding(NamedTuple):
    """One setting that hides errors: the project, the modules of its override (empty for the table), the key."""

    project: str
    modules: tuple[str, ...]
    key: str


def mypy_table(project: Path) -> dict[str, Any]:
    """The project's ``[tool.mypy]`` table."""
    document = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    return document["tool"]["mypy"]


def read_as_true(settings: dict[str, Any], key: str) -> bool:
    """Whether mypy reads the boolean *key* as true: its string spellings too, and an unreadable value counts."""
    return configparser.RawConfigParser.BOOLEAN_STATES.get(str(settings.get(key, False)).lower(), True)


def scopes(project: Path) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    """The project's table, keyed ``()``, and each override, keyed by its modules."""
    table = mypy_table(project)
    found: list[tuple[tuple[str, ...], dict[str, Any]]] = [((), table)]
    for override in table.get("overrides", []):
        module = override.get("module", ())
        found.append(((module,) if isinstance(module, str) else tuple(module), override))
    return found


def hiding_settings(project: Path) -> list[Hiding]:
    """Every setting of the project's table and overrides that hides errors from mypy."""
    found: list[Hiding] = []
    for modules, settings in scopes(project):
        checks = {
            "ignore_errors": read_as_true(settings, "ignore_errors"),
            "disable_error_code": bool(settings.get("disable_error_code")),
            "exclude": "exclude" in settings,
            "follow_imports": settings.get("follow_imports", "normal") != "normal",
        }
        found.extend(Hiding(project.name, modules, key) for key, hides in checks.items() if hides)
    return found


def excused_from_resolving(project: Path) -> list[tuple[str, ...]]:
    """The scopes that set ``ignore_missing_imports`` over modules of ours: the table (``()``), or an override."""
    return [
        modules
        for modules, settings in scopes(project)
        if read_as_true(settings, "ignore_missing_imports")
        and (not modules or any(module.split(".")[0].startswith("swcraft_") for module in modules))
    ]


def package_trees(project: Path) -> list[str]:
    """The project's package source trees, ``src/<package>``."""
    return sorted(
        f"src/{package.name}" for package in (project / "src").iterdir() if (package / "__init__.py").is_file()
    )


def missing_package_trees(project: Path) -> list[str]:
    """The package source trees the table's ``files`` does not name."""
    files = mypy_table(project).get("files", [])
    named = {Path(entry).as_posix().rstrip("/") for entry in ([files] if isinstance(files, str) else files)}
    return [tree for tree in package_trees(project) if tree not in named]


def shadowing_configurations(project: Path) -> list[str]:
    """The configuration files in the project directory that mypy would read instead of ``pyproject.toml``."""
    found = [name for name in SHADOWING_FILES if (project / name).exists()]
    setup_cfg = project / "setup.cfg"
    if setup_cfg.is_file():
        parser = configparser.ConfigParser()
        parser.read_string(setup_cfg.read_text(encoding="utf-8"))
        if any(section == "mypy" or section.startswith("mypy-") for section in parser.sections()):
            found.append("setup.cfg")
    return found


def named_sources(project: Path) -> Iterator[Path]:
    """Every ``.py`` file under the entries of the table's ``files``."""
    files = mypy_table(project).get("files", [])
    for entry in [files] if isinstance(files, str) else files:
        root = project / entry
        yield from [root] if root.is_file() else sorted(root.rglob("*.py"), key=lambda path: path.as_posix())


def hiding_comments(source: str) -> list[tuple[int, str]]:
    """The ``# mypy:`` directives, and a ``# type: ignore`` before the first statement, as (line, comment)."""
    found: list[tuple[int, str]] = []
    before_first_statement = True
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            if DIRECTIVE.match(token.string) or (before_first_statement and WHOLE_MODULE_IGNORE.match(token.string)):
                found.append((token.start[0], token.string))
        elif token.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.ENCODING):
            before_first_statement = False
    return found


def hiding_comments_in(project: Path) -> list[tuple[str, int, str]]:
    """:func:`hiding_comments` over :func:`named_sources`, as (project-relative path, line, comment)."""
    return [
        (path.relative_to(project).as_posix(), line, comment)
        for path in named_sources(project)
        for line, comment in hiding_comments(path.read_text(encoding="utf-8"))
    ]


@pytest.mark.parametrize("project", PROJECTS)
def test_no_setting_hides_errors(project: str) -> None:
    assert hiding_settings(PROJECTS_ROOT / project) == []


@pytest.mark.parametrize("project", PROJECTS)
def test_files_holds_the_package_source_tree(project: str) -> None:
    assert package_trees(PROJECTS_ROOT / project) != []
    assert missing_package_trees(PROJECTS_ROOT / project) == []


@pytest.mark.parametrize("project", PROJECTS)
def test_no_configuration_file_shadows_the_table(project: str) -> None:
    assert shadowing_configurations(PROJECTS_ROOT / project) == []


@pytest.mark.parametrize("project", PROJECTS)
def test_no_named_file_carries_a_hiding_comment(project: str) -> None:
    assert hiding_comments_in(PROJECTS_ROOT / project) == []


@pytest.mark.parametrize("project", PROJECTS)
def test_no_module_of_ours_is_excused_from_resolving(project: str) -> None:
    assert excused_from_resolving(PROJECTS_ROOT / project) == []


@pytest.mark.parametrize("project", TYPED_PROJECTS)
def test_the_table_keeps_the_integrity_flags_and_the_shared_platform(project: str) -> None:
    table = mypy_table(PROJECTS_ROOT / project)
    assert table.get("warn_unused_ignores") is True
    assert table.get("warn_redundant_casts") is True
    assert "ignore-without-code" in table.get("enable_error_code", [])
    assert table.get("platform") == "linux"


# --- planted cases: each hiding channel, written into a project of its own, is refused --------------------


def plant_project(root: Path, mypy: str, name: str = "tools") -> Path:
    """A project directory *name* with ``src/swcraft_planted`` and a ``pyproject.toml`` holding *mypy*."""
    project = root / name
    (project / "src" / "swcraft_planted").mkdir(parents=True)
    (project / "src" / "swcraft_planted" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (project / "pyproject.toml").write_text(mypy, encoding="utf-8", newline="\n")
    return project


CLEAN_TABLE = '[tool.mypy]\nfiles = ["src/swcraft_planted"]\n'


@pytest.mark.parametrize(
    ("setting", "key"),
    [
        ("ignore_errors = true", "ignore_errors"),
        ('ignore_errors = "true"', "ignore_errors"),
        ('ignore_errors = "yes"', "ignore_errors"),
        ("ignore_errors = 1", "ignore_errors"),
        ('disable_error_code = ["union-attr"]', "disable_error_code"),
        ('exclude = ["^src/swcraft_planted/old\\\\.py$"]', "exclude"),
        ('follow_imports = "skip"', "follow_imports"),
        ('follow_imports = "silent"', "follow_imports"),
    ],
)
def test_a_planted_hiding_setting_is_refused_in_the_table_and_in_an_override(
    tmp_path: Path, setting: str, key: str
) -> None:
    in_table = plant_project(tmp_path / "table", CLEAN_TABLE + setting + "\n")
    assert hiding_settings(in_table) == [Hiding("tools", (), key)]
    in_override = plant_project(
        tmp_path / "override",
        CLEAN_TABLE + f'[[tool.mypy.overrides]]\nmodule = ["swcraft_planted.a"]\n{setting}\n',
    )
    assert hiding_settings(in_override) == [Hiding("tools", ("swcraft_planted.a",), key)]


@pytest.mark.parametrize(
    ("planted", "excused"),
    [
        ("ignore_missing_imports = true\n", [()]),
        ('ignore_missing_imports = "true"\n', [()]),
        (
            '[[tool.mypy.overrides]]\nmodule = "swcraft_planted.a"\nignore_missing_imports = true\n',
            [("swcraft_planted.a",)],
        ),
        (
            '[[tool.mypy.overrides]]\nmodule = ["lxml", "swcraft_x.*"]\nignore_missing_imports = 1\n',
            [("lxml", "swcraft_x.*")],
        ),
        ('[[tool.mypy.overrides]]\nmodule = ["lxml", "lxml.*"]\nignore_missing_imports = true\n', []),
        ("ignore_missing_imports = false\n", []),
    ],
)
def test_a_planted_excuse_from_resolving_is_refused_in_the_table_and_in_an_override(
    tmp_path: Path, planted: str, excused: list[tuple[str, ...]]
) -> None:
    assert excused_from_resolving(plant_project(tmp_path, CLEAN_TABLE + planted)) == excused


@pytest.mark.parametrize("module", ['["swcraft_tools.cli"]', '"swcraft_tools.cli"', '["swcraft_tools.cli.*"]'])
def test_an_override_for_one_of_our_modules_is_refused(tmp_path: Path, module: str) -> None:
    override = f"[[tool.mypy.overrides]]\nmodule = {module}\nignore_errors = true\n"
    modules = (module.strip('[]"'),)
    assert hiding_settings(plant_project(tmp_path, CLEAN_TABLE + override)) == [
        Hiding("tools", modules, "ignore_errors")
    ]


@pytest.mark.parametrize("files", ['["src"]', '["src/swcraft_planted/sub"]', '["tests"]', "[]"])
def test_a_files_list_without_the_package_tree_is_refused(tmp_path: Path, files: str) -> None:
    project = plant_project(tmp_path, f"[tool.mypy]\nfiles = {files}\n")
    assert missing_package_trees(project) == ["src/swcraft_planted"]


def test_a_broader_files_list_is_allowed(tmp_path: Path) -> None:
    project = plant_project(tmp_path, '[tool.mypy]\nfiles = ["src/swcraft_planted/", "tests"]\n')
    assert missing_package_trees(project) == []


def test_files_as_one_string_is_read_as_a_list_of_one(tmp_path: Path) -> None:
    project = plant_project(tmp_path, '[tool.mypy]\nfiles = "src/swcraft_planted"\n')
    (project / "src" / "swcraft_planted" / "mod.py").write_text(
        "# mypy: ignore-errors\n", encoding="utf-8", newline="\n"
    )
    assert missing_package_trees(project) == []
    assert hiding_comments_in(project) == [("src/swcraft_planted/mod.py", 1, "# mypy: ignore-errors")]


@pytest.mark.parametrize(
    ("name", "text", "found"),
    [
        ("mypy.ini", "[mypy]\n", ["mypy.ini"]),
        (".mypy.ini", "[mypy]\n", [".mypy.ini"]),
        ("setup.cfg", "[mypy]\nignore_errors = True\n", ["setup.cfg"]),
        ("setup.cfg", "[mypy-swcraft_planted.*]\nignore_errors = True\n", ["setup.cfg"]),
        ("setup.cfg", "[metadata]\nname = planted\n", []),
    ],
)
def test_a_planted_configuration_file_is_reported(tmp_path: Path, name: str, text: str, found: list[str]) -> None:
    project = plant_project(tmp_path, CLEAN_TABLE)
    (project / name).write_text(text, encoding="utf-8", newline="\n")
    assert shadowing_configurations(project) == found


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("# mypy: ignore-errors\nx = 1\n", [(1, "# mypy: ignore-errors")]),
        ('"""Doc."""\n# mypy: disable-error-code="union-attr"\n', [(2, '# mypy: disable-error-code="union-attr"')]),
        ("# type: ignore\nimport os\n", [(1, "# type: ignore")]),
        ("#!/usr/bin/env python\n\n# type: ignore[attr-defined]\n", [(3, "# type: ignore[attr-defined]")]),
        ('"""Doc."""\nx = y  # type: ignore[name-defined]\n', []),
        ('"""Doc."""\n# a comment about mypy: no directive\nx = "# mypy: in a string"\n', []),
    ],
)
def test_a_planted_hiding_comment_is_reported(source: str, found: list[tuple[int, str]]) -> None:
    assert hiding_comments(source) == found


def test_a_hiding_comment_in_a_named_file_is_reported(tmp_path: Path) -> None:
    project = plant_project(tmp_path, CLEAN_TABLE)
    (project / "src" / "swcraft_planted" / "mod.py").write_text(
        "# mypy: ignore-errors\n", encoding="utf-8", newline="\n"
    )
    assert hiding_comments_in(project) == [("src/swcraft_planted/mod.py", 1, "# mypy: ignore-errors")]
