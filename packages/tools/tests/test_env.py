"""Tests for SWCraft environment file loading."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import swcraft_tools.env as env_module
from conftest import REPO_ROOT
from swcraft_tools import project
from swcraft_tools.env import env_file_keys, load_env_file

#: The real :func:`swcraft_tools.env.checkout_root`, kept before any test patches it, so a
#: test can answer the search's checkout question for a fake layout under ``tmp_path``.
_original_checkout_root = env_module.checkout_root


@pytest.fixture
def no_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the search as an installed package would: no checkout root to add.

    Otherwise a developer's ``.env`` in the real checkout would answer a test about the project's tree.
    """
    monkeypatch.setattr(env_module, "checkout_root", lambda module_file=None: None)


def _checkout_at(monkeypatch: pytest.MonkeyPatch, module: Path) -> None:
    """Patch ``env.checkout_root`` to answer the real search's question for *module*, a fake layout's ``env.py``."""
    monkeypatch.setattr(env_module, "checkout_root", lambda module_file=None: _original_checkout_root(str(module)))


def _write_env(directory: Path, value: str) -> Path:
    """Write a ``.env`` setting ``SWCRAFT_TEST_ENV`` to *value* in *directory*; return it."""
    directory.mkdir(parents=True, exist_ok=True)
    env_file = directory / ".env"
    env_file.write_text(f"SWCRAFT_TEST_ENV={value}\n", encoding="utf-8", newline="\n")
    return env_file


def _clear_test_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ``SWCRAFT_TEST_ENV`` so that a ``load_dotenv`` write is undone after the test.

    setenv-then-delenv, never a bare delenv: pytest records an undo only for a key
    that was there, so a bare delenv would let the write outlive the test.
    """
    monkeypatch.setenv("SWCRAFT_TEST_ENV", "")
    monkeypatch.delenv("SWCRAFT_TEST_ENV")


@pytest.mark.usefixtures("no_checkout")
def test_the_projects_own_env_is_read_from_a_subdirectory(tmp_path: Path, monkeypatch) -> None:
    env_file = _write_env(tmp_path, "from-file")
    (tmp_path / "swcraft.yaml").write_text("", encoding="utf-8", newline="\n")
    child = tmp_path / "nested" / "project"
    child.mkdir(parents=True)

    monkeypatch.chdir(child)
    _clear_test_key(monkeypatch)

    loaded = load_env_file()

    assert loaded == env_file
    assert loaded is not None
    assert loaded.is_absolute()
    assert os.environ["SWCRAFT_TEST_ENV"] == "from-file"


def test_load_env_file_preserves_existing_environment(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SWCRAFT_TEST_ENV=from-file\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SWCRAFT_TEST_ENV", "from-process")

    loaded = load_env_file()

    assert loaded == env_file
    assert os.environ["SWCRAFT_TEST_ENV"] == "from-process"


def _fresh_key_record(monkeypatch) -> None:
    """Give the test its own :data:`!_ENV_FILE_KEYS`, since the real one accumulates.

    Rebound rather than cleared: both readers look the name up when they run, and
    monkeypatch restores the process-wide record afterwards.
    """
    monkeypatch.setattr(env_module, "_ENV_FILE_KEYS", set())


def test_env_file_keys_records_what_the_file_introduced(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SWCRAFT_TEST_ENV=from-file\nBROWSER=from-file\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # setenv-then-delenv, never a bare delenv: pytest records an undo only for a key
    # that was there, so a bare delenv would let `load_dotenv`'s write outlive the test.
    for name in ("SWCRAFT_TEST_ENV", "BROWSER"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    _fresh_key_record(monkeypatch)

    load_env_file()

    assert env_file_keys() == frozenset({"SWCRAFT_TEST_ENV", "BROWSER"})


def test_a_value_the_process_already_carried_is_not_recorded(tmp_path: Path, monkeypatch) -> None:
    """``override=False`` means the file changed nothing, so the file owns nothing.

    What a caller must not trust is a value that arrived from the file, not a name that also appears in one.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("SWCRAFT_TEST_ENV=from-file\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SWCRAFT_TEST_ENV", "from-process")
    _fresh_key_record(monkeypatch)

    load_env_file()

    assert os.environ["SWCRAFT_TEST_ENV"] == "from-process"
    assert env_file_keys() == frozenset()


@pytest.mark.usefixtures("no_checkout")
def test_no_file_no_record(tmp_path: Path, monkeypatch) -> None:
    """A working directory with no ``.env`` and no checkout to add records nothing."""
    monkeypatch.chdir(tmp_path)
    _fresh_key_record(monkeypatch)

    assert load_env_file() is None
    assert env_file_keys() == frozenset()


@pytest.mark.usefixtures("no_checkout")
def test_an_env_above_the_project_root_is_not_read(tmp_path: Path, monkeypatch) -> None:
    """The walk stops at the nearest directory holding ``swcraft.yaml``."""
    _write_env(tmp_path, "from-above")
    project_root = tmp_path / "workspace" / "project"
    project_root.mkdir(parents=True)
    (project_root / "swcraft.yaml").write_text("", encoding="utf-8", newline="\n")
    monkeypatch.chdir(project_root)
    _clear_test_key(monkeypatch)

    assert load_env_file() is None
    assert "SWCRAFT_TEST_ENV" not in os.environ


@pytest.mark.usefixtures("no_checkout")
def test_without_a_project_root_only_the_working_directory_is_read(tmp_path: Path, monkeypatch) -> None:
    _write_env(tmp_path, "from-parent")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    _clear_test_key(monkeypatch)

    assert load_env_file() is None
    own = _write_env(work, "from-cwd")
    assert load_env_file() == own
    assert os.environ["SWCRAFT_TEST_ENV"] == "from-cwd"


def _touch(path: Path) -> Path:
    """Create *path* as an empty file, with its parents; return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8", newline="\n")
    return path


def _make_checkout(root: Path) -> Path:
    """Lay out the two checkout markers and the module under *root*; return the module path."""
    _touch(root / "scripts" / "dev.py")
    _touch(root / "packages" / "tools" / "pyproject.toml")
    return _touch(root / "packages" / "tools" / "src" / "swcraft_tools" / "env.py")


def test_this_checkout_is_recognised() -> None:
    """The tests run from a source checkout, found from the module and not from the cwd."""
    assert env_module.checkout_root() == REPO_ROOT


@pytest.mark.parametrize(
    "site_packages",
    [("lib", "python3.11", "site-packages"), ("Lib", "site-packages")],
    ids=["posix", "windows"],
)
def test_an_installed_layout_has_no_checkout_and_reads_nothing_at_or_above_the_venv(
    tmp_path: Path, monkeypatch, site_packages: tuple[str, ...]
) -> None:
    venv = tmp_path / "venv"
    module = _touch(venv.joinpath(*site_packages, "swcraft_tools", "env.py"))
    assert env_module.checkout_root(str(module)) is None

    for directory in (tmp_path, venv, venv / site_packages[0]):
        _write_env(directory, "from-install")
    work = tmp_path / "elsewhere" / "work"
    work.mkdir(parents=True)
    monkeypatch.chdir(work)
    _checkout_at(monkeypatch, module)
    _clear_test_key(monkeypatch)

    assert load_env_file() is None
    assert "SWCRAFT_TEST_ENV" not in os.environ


def test_the_checkout_layout_adds_its_roots_env(tmp_path: Path, monkeypatch) -> None:
    checkout = tmp_path / "checkout"
    module = _make_checkout(checkout)
    assert env_module.checkout_root(str(module)) == checkout

    env_file = _write_env(checkout, "from-checkout")
    _write_env(tmp_path, "from-above-the-checkout")
    work = tmp_path / "elsewhere"
    work.mkdir()
    monkeypatch.chdir(work)
    _checkout_at(monkeypatch, module)
    _clear_test_key(monkeypatch)

    assert load_env_file() == env_file
    assert os.environ["SWCRAFT_TEST_ENV"] == "from-checkout"


def test_the_projects_env_wins_over_the_checkouts(tmp_path: Path, monkeypatch) -> None:
    """Nearest first: a project's own ``.env`` is read and the checkout's is not."""
    module = _make_checkout(tmp_path / "checkout")
    _write_env(tmp_path / "checkout", "from-checkout")
    project_root = tmp_path / "project"
    own = _write_env(project_root, "from-project")
    _touch(project_root / "swcraft.yaml")
    monkeypatch.chdir(project_root)
    _checkout_at(monkeypatch, module)
    _clear_test_key(monkeypatch)

    assert load_env_file() == own
    assert os.environ["SWCRAFT_TEST_ENV"] == "from-project"


def test_a_working_directory_at_the_checkout_root_is_searched_once(tmp_path: Path, monkeypatch) -> None:
    checkout = tmp_path / "checkout"
    module = _make_checkout(checkout)
    monkeypatch.chdir(checkout)
    _checkout_at(monkeypatch, module)

    assert env_module._candidate_dirs() == [checkout.resolve()]


def test_the_manifest_name_is_the_projects() -> None:
    assert env_module._MANIFEST_FILENAME == project.MANIFEST_FILENAME


def test_a_checkout_is_recognised_by_both_markers(tmp_path: Path) -> None:
    """``scripts/dev.py`` alone, without ``packages/tools/pyproject.toml``, is not a checkout."""
    module = _touch(tmp_path / "packages" / "tools" / "src" / "swcraft_tools" / "env.py")
    _touch(tmp_path / "scripts" / "dev.py")
    assert env_module.checkout_root(str(module)) is None
    _touch(tmp_path / "packages" / "tools" / "pyproject.toml")
    assert env_module.checkout_root(str(module)) == tmp_path
    (tmp_path / "scripts" / "dev.py").unlink()
    assert env_module.checkout_root(str(module)) is None, (
        "``packages/tools/pyproject.toml`` alone, without ``scripts/dev.py``, is not a checkout"
    )


def test_a_windows_venv_inside_a_checkout_is_not_the_checkout(tmp_path: Path) -> None:
    """``<checkout>/.venv/Lib/site-packages/swcraft_tools/env.py`` is not the checkout four levels up."""
    _touch(tmp_path / "scripts" / "dev.py")
    _touch(tmp_path / "packages" / "tools" / "pyproject.toml")
    module = _touch(tmp_path / ".venv" / "Lib" / "site-packages" / "swcraft_tools" / "env.py")
    assert env_module.checkout_root(str(module)) is None


def test_a_shallow_target_install_has_no_checkout(tmp_path: Path) -> None:
    """A ``pip install --target`` two levels below the anchor has no ``parents[4]``; the answer is ``None``."""
    anchor = Path(tmp_path.anchor)
    module = anchor / "swcraft-shallow-target" / "lib" / "swcraft_tools" / "env.py"
    assert len(module.parents) == 4
    assert env_module.checkout_root(str(module)) is None
