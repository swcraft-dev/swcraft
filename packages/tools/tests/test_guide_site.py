"""Locating the installed user guide: ``guide_site.py``, the directory ``swcraft docs`` serves.

Three properties get their own coverage, each a way the answer can be wrong:

* **The search order is a precedence, not a set.** All three steps can hold at once on a developer
  machine, so every pair is pinned as well as each step alone.
* **A directory is a site only if it holds ``index.html``.** An aborted copy exists and serves
  nothing; treating it as present would serve a stale or empty guide with no failure signal.
* **The refusal when nothing is found** names the package-data directory and the archive to fall
  back to, and not the interpreter-relative sibling, which in a virtualenv means nothing to the reader.

The bundle sibling (step 3) is exercised against a synthetic layout, including the trap that
``sys.executable`` must not be resolved through its symlink.

What these tests do **not** reach: they build no wheel, so they witness the search order and the
refusal, not that a released distribution carries a payload at step 2 -- that is the packaging's
``artifact``-marked gate.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest
from conftest import build_artifacts_module, make_guide_site, neutralise_guide_site_search
from swcraft_tools import guide_site as guide_site_module
from swcraft_tools._version import __version__
from swcraft_tools.env import load_env_file
from swcraft_tools.guide_site import (
    ARCHIVE_PREFIX,
    GUIDE_SITE_ENV,
    PACKAGE_SITE_DIRNAME,
    SITE_INDEX_NAME,
    GuideSiteMissing,
    archive_name,
    bundle_site_dir,
    env_site_dir,
    is_site,
    site_root,
)

MODULE_PATH = Path(guide_site_module.__file__)


@pytest.fixture
def isolated(monkeypatch, tmp_path: Path) -> Path:
    """Neutralise all three search steps, so each test enables only the one it is about."""
    return neutralise_guide_site_search(monkeypatch, tmp_path)


def _env_site(tmp_path: Path, monkeypatch) -> Path:
    site = make_guide_site(tmp_path / "explicit")
    monkeypatch.setenv(GUIDE_SITE_ENV, str(site))
    return site


def _package_site(tmp_path: Path) -> Path:
    return make_guide_site(tmp_path / "site-packages" / "swcraft_tools" / PACKAGE_SITE_DIRNAME)


def _bundle_site(tmp_path: Path) -> Path:
    return make_guide_site(tmp_path / "venv" / "docs" / "site")


class TestEachSearchStepAlone:
    """One step satisfied at a time — the three ways a guide can be present."""

    def test_the_environment_override_is_found(self, isolated: Path, monkeypatch, tmp_path: Path) -> None:
        site = _env_site(tmp_path, monkeypatch)
        assert site_root() == site

    def test_the_package_data_directory_is_found(self, isolated: Path, tmp_path: Path) -> None:
        site = _package_site(tmp_path)
        assert site_root() == site

    def test_the_bundle_sibling_is_found(self, isolated: Path, tmp_path: Path) -> None:
        site = _bundle_site(tmp_path)
        assert site_root() == site

    def test_the_environment_value_is_user_expanded(self, isolated: Path, monkeypatch, tmp_path: Path) -> None:
        """``~`` is what a support instruction is actually typed with."""
        home = tmp_path / "home"
        site = make_guide_site(home / "guide")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv(GUIDE_SITE_ENV, "~/guide")
        assert site_root() == site


class TestPrecedence:
    """Every pair, and all three at once: the order is a precedence, not a set."""

    def test_the_environment_override_beats_the_package_data(self, isolated: Path, monkeypatch, tmp_path: Path) -> None:
        site = _env_site(tmp_path, monkeypatch)
        _package_site(tmp_path)
        assert site_root() == site

    def test_the_environment_override_beats_the_bundle_sibling(
        self, isolated: Path, monkeypatch, tmp_path: Path
    ) -> None:
        site = _env_site(tmp_path, monkeypatch)
        _bundle_site(tmp_path)
        assert site_root() == site

    def test_the_package_data_beats_the_bundle_sibling(self, isolated: Path, tmp_path: Path) -> None:
        site = _package_site(tmp_path)
        _bundle_site(tmp_path)
        assert site_root() == site

    def test_all_three_present_resolves_to_the_environment_override(
        self, isolated: Path, monkeypatch, tmp_path: Path
    ) -> None:
        site = _env_site(tmp_path, monkeypatch)
        _package_site(tmp_path)
        _bundle_site(tmp_path)
        assert site_root() == site


class TestADirectoryWithoutAnIndexIsAbsent:
    """The half-copied tree: it exists, it serves nothing, and it must not win."""

    def test_an_empty_environment_directory_falls_through_to_the_package_data(
        self, isolated: Path, monkeypatch, tmp_path: Path
    ) -> None:
        empty = tmp_path / "half-copied"
        empty.mkdir()
        monkeypatch.setenv(GUIDE_SITE_ENV, str(empty))
        site = _package_site(tmp_path)
        assert site_root() == site

    def test_an_empty_package_directory_falls_through_to_the_bundle_sibling(
        self, isolated: Path, tmp_path: Path
    ) -> None:
        (tmp_path / "site-packages" / "swcraft_tools" / PACKAGE_SITE_DIRNAME).mkdir()
        site = _bundle_site(tmp_path)
        assert site_root() == site

    def test_an_empty_bundle_directory_leaves_nothing_found(self, isolated: Path, tmp_path: Path) -> None:
        (tmp_path / "venv" / "docs" / "site").mkdir(parents=True)
        with pytest.raises(GuideSiteMissing):
            site_root()

    def test_a_directory_holding_only_other_pages_is_not_a_site(self, isolated: Path, tmp_path: Path) -> None:
        """``index.html`` is the guard, and it is the *only* guard — so it is the one checked."""
        directory = tmp_path / "site-packages" / "swcraft_tools" / PACKAGE_SITE_DIRNAME
        (directory / "search").mkdir(parents=True)
        (directory / "search" / "search_index.json").write_text("{}", encoding="utf-8")
        with pytest.raises(GuideSiteMissing):
            site_root()

    def test_a_file_at_the_environment_path_is_not_a_site(self, isolated: Path, monkeypatch, tmp_path: Path) -> None:
        """A path to the *archive* rather than to the unpacked site is the plausible mistake."""
        archive = tmp_path / archive_name()
        archive.write_text("not a directory", encoding="utf-8")
        monkeypatch.setenv(GUIDE_SITE_ENV, str(archive))
        with pytest.raises(GuideSiteMissing):
            site_root()

    def test_a_blank_environment_value_is_ignored_rather_than_read_as_the_cwd(
        self, isolated: Path, monkeypatch, tmp_path: Path
    ) -> None:
        """``SWCRAFT_GUIDE_SITE=`` would otherwise become ``Path("")``, which is ``.``."""
        monkeypatch.setenv(GUIDE_SITE_ENV, "   ")
        site = _package_site(tmp_path)
        assert site_root() == site


class TestTheBundleSiblingIsRelativeToTheRunningInterpreter:
    """Step 3's one mechanical trap, pinned before the bundle that fills it exists."""

    def test_it_is_two_levels_above_the_interpreter(self, isolated: Path, tmp_path: Path) -> None:
        assert bundle_site_dir() == tmp_path / "venv" / "docs" / "site"

    def test_the_interpreter_symlink_is_not_resolved(self, isolated: Path, monkeypatch, tmp_path: Path) -> None:
        """A virtualenv's ``bin/python`` symlink is not resolved: the payload sits beside the bundle's ``python``."""
        real = tmp_path / "system" / "bin" / "python3"
        real.parent.mkdir(parents=True)
        real.write_text("", encoding="utf-8")
        link = tmp_path / "bundle" / "runtime" / "python"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):  # pragma: no cover - a host without unprivileged symlinks
            pytest.skip("this platform does not create symlinks unprivileged")
        monkeypatch.setattr("sys.executable", str(link))

        site = make_guide_site(tmp_path / "bundle" / "docs" / "site")
        make_guide_site(tmp_path / "system" / "docs" / "site")

        assert site_root() == site


class TestAnEmptyInterpreterPathIsNotACwdSearch:
    """``sys.executable`` is ``''`` in an embedded interpreter; step 3 must not become the relative ``docs/site``."""

    def test_bundle_site_dir_is_none(self, isolated: Path, monkeypatch) -> None:
        monkeypatch.setattr("sys.executable", "")
        assert bundle_site_dir() is None

    def test_the_working_directory_is_not_searched(self, isolated: Path, monkeypatch, tmp_path: Path) -> None:
        cwd = tmp_path / "somewhere"
        make_guide_site(cwd / "docs" / "site")
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("sys.executable", "")
        with pytest.raises(GuideSiteMissing):
            site_root()


class TestTheDotEnvRoute:
    """A project's ``.env`` reaches the override although ``guide_site.py`` imports nothing from ``env.py``.

    ``cli.main()`` calls ``load_env_file()`` before it dispatches, which writes into ``os.environ``, which
    :func:`env_site_dir` reads. Pinned in both directions, so the claim cannot be stated the wrong way round.
    """

    def test_load_env_file_puts_the_override_into_the_process_environment(
        self, isolated: Path, monkeypatch, tmp_path: Path
    ) -> None:
        site = make_guide_site(tmp_path / "from-dotenv")
        project = tmp_path / "project"
        project.mkdir()
        (project / ".env").write_text(f"{GUIDE_SITE_ENV}={site}\n", encoding="utf-8")
        monkeypatch.chdir(project)

        load_env_file()

        assert env_site_dir() == site
        assert site_root() == site

    def test_a_real_environment_value_still_wins_over_the_file(
        self, isolated: Path, monkeypatch, tmp_path: Path
    ) -> None:
        """``load_dotenv(..., override=False)`` — the file fills gaps, it does not overrule."""
        chosen = make_guide_site(tmp_path / "chosen")
        make_guide_site(tmp_path / "from-dotenv")
        project = tmp_path / "project"
        project.mkdir()
        (project / ".env").write_text(f"{GUIDE_SITE_ENV}={tmp_path / 'from-dotenv'}\n", encoding="utf-8")
        monkeypatch.chdir(project)
        monkeypatch.setenv(GUIDE_SITE_ENV, str(chosen))

        load_env_file()

        assert site_root() == chosen


class TestTheIsolationHelperItself:
    """``neutralise_guide_site_search`` must restore the override, set or unset.

    ``TestTheDotEnvRoute`` writes into ``os.environ`` outside monkeypatch's bookkeeping; a ``delenv`` of an
    absent key records no undo, so that write would leak an override into every later test. The inner
    ``MonkeyPatch.context()`` is what lets the restoration be observed inside a test.

    Not reached: anything but ``GUIDE_SITE_ENV`` (the helper's other patches are ``setattr`` calls, which
    always record an undo), and a fourth search step reading another variable.
    """

    def test_an_absent_override_is_still_absent_afterwards(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv(GUIDE_SITE_ENV, "")
        monkeypatch.delenv(GUIDE_SITE_ENV)

        with pytest.MonkeyPatch.context() as inner:
            neutralise_guide_site_search(inner, tmp_path)
            os.environ[GUIDE_SITE_ENV] = str(tmp_path)  # what `load_env_file()` does

        assert GUIDE_SITE_ENV not in os.environ

    def test_a_pre_existing_override_comes_back_unchanged(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv(GUIDE_SITE_ENV, "/pre-existing")

        with pytest.MonkeyPatch.context() as inner:
            neutralise_guide_site_search(inner, tmp_path)
            os.environ[GUIDE_SITE_ENV] = str(tmp_path)

        assert os.environ[GUIDE_SITE_ENV] == "/pre-existing"


class TestTheBuilderAndTheLocatorAgree:
    """Three names spelled twice, in two files that cannot import each other.

    ``scripts/build_artifacts.py`` is stdlib-only and off the package path. Renaming either side alone
    leaves every other test green while the wheel ships a payload the command never looks for.
    """

    @pytest.fixture
    def builder(self):
        return build_artifacts_module()

    def test_the_payload_directory_has_one_name(self, builder) -> None:
        assert PACKAGE_SITE_DIRNAME == builder.PACKAGE_GUIDE_SITE_DIRNAME

    def test_the_index_file_has_one_name(self, builder) -> None:
        assert SITE_INDEX_NAME == builder.SITE_INDEX_FILENAME

    def test_the_archive_the_refusal_advises_is_the_one_the_build_produces(self, builder) -> None:
        assert ARCHIVE_PREFIX == builder.DOCS_ARCHIVE_PREFIX
        assert archive_name() == builder.docs_archive_filename(__version__)

    def test_neither_sidecar_the_build_leaves_beside_the_payload_is_found(self, builder, isolated: Path) -> None:
        """A killed rewrite's ``guide_site.incoming/`` or ``guide_site.outgoing/`` is not what step 2 resolves."""
        package_root = isolated / "site-packages" / "swcraft_tools"
        for dirname in (builder.PACKAGE_GUIDE_SITE_STAGING_DIRNAME, builder.PACKAGE_GUIDE_SITE_SUPERSEDED_DIRNAME):
            make_guide_site(package_root / dirname)

        with pytest.raises(GuideSiteMissing):
            site_root()


class TestTheRefusal:
    """Nothing found at all: the message is what the reader gets, so it has an oracle."""

    @pytest.fixture
    def refusal(self, isolated: Path) -> str:
        with pytest.raises(GuideSiteMissing) as raised:
            site_root()
        return str(raised.value)

    def test_it_names_the_package_data_directory_it_looked_for(self, refusal: str, tmp_path: Path) -> None:
        assert str(tmp_path / "site-packages" / "swcraft_tools" / PACKAGE_SITE_DIRNAME) in refusal

    def test_it_names_the_archive_with_this_version(self, refusal: str) -> None:
        assert f"swcraft-guide-{__version__}.zip" in refusal

    def test_it_says_which_install_would_carry_the_guide(self, refusal: str) -> None:
        assert "swcraft-tools" in refusal

    def test_it_names_the_environment_variable_as_the_way_to_point_at_a_site(self, refusal: str) -> None:
        assert GUIDE_SITE_ENV in refusal

    def test_it_does_not_name_the_interpreter_relative_sibling(self, refusal: str, tmp_path: Path) -> None:
        """``.venv/docs/site`` is a path the reader has no reason to have heard of."""
        assert str(tmp_path / "venv" / "docs" / "site") not in refusal

    def test_it_names_a_rejected_environment_directory_as_well(
        self, isolated: Path, monkeypatch, tmp_path: Path
    ) -> None:
        """Set-but-wrong is the support case the base message alone would not explain."""
        empty = tmp_path / "half-copied"
        empty.mkdir()
        monkeypatch.setenv(GUIDE_SITE_ENV, str(empty))
        with pytest.raises(GuideSiteMissing) as raised:
            site_root()
        message = str(raised.value)
        assert str(empty) in message
        assert SITE_INDEX_NAME in message

    def test_it_names_the_directory_the_search_looked_in_and_not_a_second_resolution(
        self, isolated: Path, monkeypatch, tmp_path: Path
    ) -> None:
        """``site_root`` resolves the package directory once and hands it to the message.

        A second resolution could let the two answers differ; the oracle is ``resource_files`` asked once.
        """
        package_root = tmp_path / "site-packages" / "swcraft_tools"
        asked: list[str] = []

        def counted(package: str):
            asked.append(package)
            return package_root

        monkeypatch.setattr(guide_site_module, "resource_files", counted)
        with pytest.raises(GuideSiteMissing) as raised:
            site_root()

        assert asked == ["swcraft_tools"]
        assert str(package_root / PACKAGE_SITE_DIRNAME) in str(raised.value)


class TestIsSite:
    """The one predicate every step goes through, asked directly."""

    def test_a_missing_directory_is_not_a_site(self, tmp_path: Path) -> None:
        assert is_site(tmp_path / "absent") is False

    def test_a_directory_with_an_index_is_a_site(self, tmp_path: Path) -> None:
        assert is_site(make_guide_site(tmp_path / "present")) is True

    def test_a_file_is_not_a_site(self, tmp_path: Path) -> None:
        candidate = tmp_path / "not-a-directory"
        candidate.write_text("", encoding="utf-8")
        assert is_site(candidate) is False


class TestLeafModule:
    """Standard library plus ``_version``, and nothing else: a documentation server reads no model."""

    @staticmethod
    def _tree() -> ast.Module:
        return ast.parse(MODULE_PATH.read_text(encoding="utf-8"))

    def _imported_modules(self) -> set[str]:
        modules: set[str] = set()
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules.add(node.module)
        return modules

    def test_the_only_swcraft_import_is_the_version(self) -> None:
        offenders = sorted(
            module
            for module in self._imported_modules()
            if module.split(".")[0] == "swcraft_tools" and module != "swcraft_tools._version"
        )
        assert offenders == [], f"guide_site.py must stay a leaf module; found {offenders}"

    def test_it_uses_no_relative_imports_either(self) -> None:
        """A relative import carries no module prefix, so it is invisible to the check above."""
        relative = [node for node in ast.walk(self._tree()) if isinstance(node, ast.ImportFrom) and node.level > 0]
        assert relative == [], "guide_site.py must stay a leaf module; found a relative import"
