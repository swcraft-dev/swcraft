"""The real-export tier's gate (``real_models.py``): the three states, the labels, and no name anywhere.

Held in the fast gate on temporary directories, never on a real export:

- **The three states**: unset is one skip on the placeholder case; a missing directory, a file, a
  symlink loop, a directory with no export or any exception during discovery is a names-free failure
  in the test body; ``--require-real-models`` with the variable unset fails.
- **The cases**: which files are exports, their labels in sorted POSIX-name order, an unreadable
  export kept in its place, and absolute paths from a relative value.
- **No name** reaches a test id, a ``repr`` or a message (the ``zqsen`` sentinel prefix).
- **The report hook** (``real_models.withhold_exception_text``).
- **End to end**: ``test_real_models.py`` under ``dev.py``'s own ``REAL_MODELS_PYTEST_ARGS``.

Does not reach the procedure an export runs through, nor the stage's output on a failure inside it,
which ``test_real_models_stage.py`` holds on a sentinel export.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import measure_forms_module

from tests.functional import real_models
from tests.functional.conftest import REPO_ROOT, dev_assignment, diagnosed_pytest, run_diagnosed
from tests.functional.real_models import ENV_VAR, FLAG, PLACEHOLDER_ID, RealModel
from tests.functional.test_real_models import RECORD_VAR

MCP_SERVER = REPO_ROOT / "packages" / "mcp"

#: The prefix every planted name carries; a lowered or truncated derivative still carries it.
SENTINEL = "zqsen"


def _write(path: Path, text: str = "<AUTOSAR/>\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


@pytest.fixture
def exports(tmp_path: Path) -> Path:
    """A directory named with the sentinel: four exports and every kind of non-export beside them."""
    directory = tmp_path / f"{SENTINEL}Directory"
    _write(directory / f"{SENTINEL}b.arxml")
    _write(directory / f"{SENTINEL}C.arxml")
    _write(directory / f"{SENTINEL}a.arxml")
    _write(directory / f"{SENTINEL}d.ARXML")
    _write(directory / f"._{SENTINEL}a.arxml", "AppleDouble metadata\n")
    _write(directory / f"{SENTINEL}a.arxml.sha256", "0" * 64 + "\n")
    _write(directory / f"{SENTINEL}notes.txt")
    _write(directory / "derived" / f"{SENTINEL}inner.arxml")
    (directory / f"{SENTINEL}folder.arxml").mkdir()
    return directory


@contextlib.contextmanager
def _unreadable(path: Path) -> Any:
    """*path* with no permission bits while inside; skipped where that still leaves it readable (root)."""
    mode = path.stat().st_mode
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("this host reads a file without permission bits")
        yield path
    finally:
        path.chmod(mode)


def _skip_reason(case: RealModel | None) -> str:
    with pytest.raises(pytest.skip.Exception) as excinfo:
        real_models.require_real_model(case, required=False)
    return str(excinfo.value)


def _failure(case: RealModel | None, *, required: bool = False) -> str:
    with pytest.raises(pytest.fail.Exception) as excinfo:
        real_models.require_real_model(case, required=required)
    assert excinfo.value.pytrace is False, "a traceback would print the arguments it was raised with"
    return str(excinfo.value)


class TestTheThreeStates:
    def test_unset_is_one_placeholder_case_skipped_naming_the_variable(self, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        (case,) = real_models.cases()
        assert case.id == PLACEHOLDER_ID and case.values == (None,)
        assert ENV_VAR in _skip_reason(None)

    def test_an_empty_value_is_unset(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "")
        assert [case.id for case in real_models.cases()] == [PLACEHOLDER_ID]
        assert ENV_VAR in _skip_reason(None)

    def test_the_flag_turns_the_unset_skip_into_a_failure(self, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        message = _failure(None, required=True)
        assert FLAG in message and ENV_VAR in message

    @pytest.mark.parametrize("flagged", [False, True], ids=["plain", "flagged"])
    def test_a_missing_directory_is_a_failure(self, monkeypatch, tmp_path, flagged):
        monkeypatch.setenv(ENV_VAR, str(tmp_path / f"{SENTINEL}Missing"))
        assert [case.id for case in real_models.cases()] == [PLACEHOLDER_ID]
        message = _failure(None, required=flagged)
        assert "no readable directory" in message and SENTINEL not in message.lower()

    def test_a_file_in_place_of_the_directory_is_a_failure(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ENV_VAR, str(_write(tmp_path / f"{SENTINEL}One.arxml")))
        assert [case.id for case in real_models.cases()] == [PLACEHOLDER_ID]
        message = _failure(None)
        assert "no readable directory" in message and SENTINEL not in message.lower()

    def test_a_directory_without_an_export_is_a_failure(self, monkeypatch, tmp_path):
        directory = tmp_path / f"{SENTINEL}Empty"
        _write(directory / f"{SENTINEL}a.arxml.sha256")
        _write(directory / "derived" / f"{SENTINEL}inner.arxml")
        monkeypatch.setenv(ENV_VAR, str(directory))
        assert [case.id for case in real_models.cases()] == [PLACEHOLDER_ID]
        message = _failure(None)
        assert "*.arxml" in message and SENTINEL not in message.lower()

    def test_a_symlink_loop_is_a_names_free_failure_and_no_collection_error(self, monkeypatch, tmp_path):
        """Python 3.11's ``Path.resolve`` raises ``RuntimeError`` naming the path on a loop."""
        loop = tmp_path / f"{SENTINEL}Loop"
        try:
            loop.symlink_to(loop)
        except OSError:
            pytest.skip("this host cannot create a symlink")
        monkeypatch.setenv(ENV_VAR, str(loop))
        assert [case.id for case in real_models.cases()] == [PLACEHOLDER_ID]
        message = _failure(None)
        assert "no readable directory" in message and SENTINEL not in message.lower()

    @pytest.mark.parametrize("where", ["discovery", "labelling"])
    def test_any_exception_before_a_test_runs_is_a_names_free_failure(self, monkeypatch, exports, where):
        def raises(*_args: Any, **_kwargs: Any) -> Any:
            raise ValueError(f"{SENTINEL} {exports}")

        monkeypatch.setenv(ENV_VAR, str(exports))
        if where == "discovery":
            monkeypatch.setattr(Path, "iterdir", raises)
        else:
            monkeypatch.setattr(measure_forms_module(), "label_letters", raises)
        assert [case.id for case in real_models.cases()] == [PLACEHOLDER_ID]
        message = _failure(None, required=True)
        assert "discovering its exports raised" in message and SENTINEL not in message.lower()

    @pytest.mark.parametrize("flagged", [False, True], ids=["plain", "flagged"])
    def test_an_export_case_is_returned_as_it_is(self, monkeypatch, exports, flagged):
        monkeypatch.setenv(ENV_VAR, str(exports))
        model = real_models.cases()[0].values[0]
        assert real_models.require_real_model(model, required=flagged) is model


class TestTheCases:
    def test_one_case_per_export_labelled_in_sorted_posix_order(self, monkeypatch, exports):
        monkeypatch.setenv(ENV_VAR, str(exports))
        cases = real_models.cases()
        assert [case.id for case in cases] == ["real-model-A", "real-model-B", "real-model-C", "real-model-D"]
        models = [case.values[0] for case in cases]
        assert [model.label for model in models] == ["real model A", "real model B", "real model C", "real model D"]
        # POSIX byte order: the upper-case name sorts before the lower-case ones (distinct names,
        # since a case-insensitive file system would fold a pair differing in case alone). The suffix
        # matches in any case; the AppleDouble dotfile beside an export is not one.
        assert [model.path.name for model in models] == [
            f"{SENTINEL}C.arxml",
            f"{SENTINEL}a.arxml",
            f"{SENTINEL}b.arxml",
            f"{SENTINEL}d.ARXML",
        ]

    def test_a_symlinked_export_is_followed(self, monkeypatch, tmp_path):
        target = _write(tmp_path / "elsewhere" / "target.xml")
        directory = tmp_path / "exports"
        directory.mkdir()
        try:
            (directory / "linked.arxml").symlink_to(target)
        except OSError:
            pytest.skip("this host cannot create a symlink")
        monkeypatch.setenv(ENV_VAR, str(directory))
        assert [case.id for case in real_models.cases()] == ["real-model-A"]

    def test_an_unreadable_export_is_a_case_in_its_sorted_place(self, monkeypatch, exports):
        monkeypatch.setenv(ENV_VAR, str(exports))
        with _unreadable(exports / f"{SENTINEL}b.arxml"):
            models = [case.values[0] for case in real_models.cases()]
            assert [model.label for model in models] == [
                "real model A",
                "real model B",
                "real model C",
                "real model D",
            ]
            assert models[2].path.name == f"{SENTINEL}b.arxml"

    def test_a_relative_value_gives_absolute_paths(self, monkeypatch, exports):
        monkeypatch.chdir(exports.parent)
        monkeypatch.setenv(ENV_VAR, exports.name)
        models = [case.values[0] for case in real_models.cases()]
        assert len(models) == 4
        assert all(model.path.is_absolute() for model in models)
        assert all(model.path.parent == exports.resolve() for model in models)

    def test_the_labels_do_not_run_out(self):
        letters = measure_forms_module().label_letters
        assert [letters(index) for index in (0, 25, 26, 27, 51, 52)] == ["A", "Z", "AA", "AB", "AZ", "BA"]

    def test_no_name_reaches_an_id_or_a_repr(self, monkeypatch, exports):
        monkeypatch.setenv(ENV_VAR, str(exports))
        for case in real_models.cases():
            model = case.values[0]
            for rendering in (case.id, repr(model), str(model), repr(case.values)):
                assert SENTINEL not in rendering.lower(), rendering


class TestWithholdExceptionText:
    """The functional conftest's report hook: a ``real_models`` test's exception text is withheld."""

    @staticmethod
    def _call(exception: BaseException) -> pytest.CallInfo[None]:
        def raises() -> None:
            raise exception

        return pytest.CallInfo.from_call(raises, when="call")

    @staticmethod
    def _item(*, marked: bool) -> Any:
        return SimpleNamespace(get_closest_marker=lambda name: object() if marked and name == "real_models" else None)

    def test_an_exception_of_a_real_models_test_is_its_class_alone(self):
        call = self._call(ValueError(f"{SENTINEL} text"))
        real_models.withhold_exception_text(self._item(marked=True), call)
        assert call.excinfo is not None and call.excinfo.errisinstance(pytest.fail.Exception)
        assert str(call.excinfo.value) == "raised ValueError in call, its text withheld"
        assert call.excinfo.value.pytrace is False

    def test_another_test_s_exception_is_left_whole(self):
        call = self._call(ValueError(f"{SENTINEL} text"))
        real_models.withhold_exception_text(self._item(marked=False), call)
        assert call.excinfo is not None and call.excinfo.errisinstance(ValueError)
        assert SENTINEL in str(call.excinfo.value)

    @pytest.mark.parametrize("outcome", [pytest.skip.Exception, pytest.fail.Exception, pytest.xfail.Exception])
    def test_a_skip_a_failure_and_an_xfail_keep_their_own_message(self, outcome):
        call = self._call(outcome("this tier's own message"))
        real_models.withhold_exception_text(self._item(marked=True), call)
        assert call.excinfo is not None and type(call.excinfo.value) is outcome
        assert "this tier's own message" in str(call.excinfo.value)

    def test_a_passing_call_is_left_alone(self):
        call = pytest.CallInfo.from_call(lambda: None, when="call")
        real_models.withhold_exception_text(self._item(marked=True), call)
        assert call.excinfo is None


class TestTheStageEndToEnd:
    """``test_real_models.py`` under ``dev.py``'s own argv, in a subprocess of the ``mcp_server`` project."""

    @staticmethod
    def _run(tmp_path: Path, *extra: str, value: str | None = None) -> subprocess.CompletedProcess[str]:
        argv = list(dev_assignment("REAL_MODELS_PYTEST_ARGS"))
        environment = {
            name: setting
            for name, setting in os.environ.items()
            if name not in {ENV_VAR, RECORD_VAR, "PYTEST_ADDOPTS"}
            and not name.startswith(("PYTEST_XDIST_", "PYTEST_CURRENT_TEST"))
        }
        # A run that reaches an export writes the record: here, never the repository's dist/.
        environment[RECORD_VAR] = str(tmp_path / "record.md")
        if value is not None:
            environment[ENV_VAR] = value
        # No COLUMNS: the argv's own -vv keeps a message whole at the default width, which is the
        # width the person running the stage reads it at.
        environment.pop("COLUMNS", None)
        # The hang probe's stack dump, because the stage's own --tb=no leaves no trace of where a
        # nested run hangs.
        return run_diagnosed(
            diagnosed_pytest(["tests/functional/test_real_models.py", *argv, *extra], tmp_path / "child"),
            cwd=MCP_SERVER,
            env=environment,
            timeout=120,
        )

    def test_the_argv_is_the_decided_one(self):
        assert dev_assignment("REAL_MODELS_PYTEST_ARGS") == tuple(
            "-m real_models -vv -rfEs -p no:cacheprovider -p no:warnings --capture=fd --show-capture=no --tb=no "
            "-o tmp_path_retention_policy=none --timeout=0".split()
        )

    def test_unset_is_exactly_one_skip_and_the_flag_makes_it_one_failure(self, tmp_path):
        plain = self._run(tmp_path)
        assert plain.returncode == 0, plain.stdout[-2000:]
        assert "1 skipped" in plain.stdout and "passed" not in plain.stdout and "failed" not in plain.stdout
        assert f"test_real_model[{PLACEHOLDER_ID}]" in plain.stdout and ENV_VAR in plain.stdout

        flagged = self._run(tmp_path, FLAG)
        assert flagged.returncode == 1, flagged.stdout[-2000:]
        assert "1 failed" in flagged.stdout and FLAG in flagged.stdout

    def test_a_directory_without_an_export_fails_naming_nothing(self, tmp_path):
        directory = tmp_path / f"{SENTINEL}Empty"
        _write(directory / f"{SENTINEL}a.arxml.sha256")
        completed = self._run(tmp_path, value=str(directory))
        assert completed.returncode == 1, completed.stdout[-2000:]
        assert "1 failed" in completed.stdout
        assert SENTINEL not in (completed.stdout + completed.stderr).lower()

    def test_an_unreadable_export_fails_at_read_naming_nothing(self, tmp_path):
        directory = tmp_path / f"{SENTINEL}Exports"
        export = _write(directory / f"{SENTINEL}a.arxml")
        with _unreadable(export):
            completed = self._run(tmp_path, value=str(directory))
        assert completed.returncode == 1, completed.stdout[-2000:]
        assert "1 failed" in completed.stdout
        assert "real model A: red at read: raised PermissionError" in completed.stdout
        record = (tmp_path / "record.md").read_text(encoding="utf-8")
        assert "- real model A: red at read: raised PermissionError" in record
        assert SENTINEL not in (completed.stdout + completed.stderr + record).lower()

    def test_the_option_is_registered_by_this_projects_initial_conftest(self):
        conftest = (MCP_SERVER / "tests" / "conftest.py").read_text(encoding="utf-8")
        assert "def pytest_addoption" in conftest and "real_models.FLAG" in conftest
        assert FLAG == "--require-real-models"
