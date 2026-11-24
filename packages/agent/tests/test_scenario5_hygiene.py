"""Guards for scenario 5's build override and the worktree fixture.

Scenario 5 runs against a second checkout. With a repo-local ``.venv`` per checkout this
file pins:

* the build override is the **direct** ``swcraft_tools.cli validate`` command, not a repo
  build wrapper (the worktree has no environment of its own), run by an **absolute** interpreter;
* no Moon reference survives in the fixture or the scenario, while the
  ``GIT_TERMINAL_PROMPT`` / ``GCM_INTERACTIVE`` / ``GIT_CONFIG_*`` guards (for ``git worktree``) stay;
* ``worktree_project`` repairs nothing on teardown, and the parent checkout's ``.venv``
  still resolves ``swcraft_tools`` to the parent's own ``src/`` **after** the teardown.

The last test is ``@pytest.mark.slow``: it really creates and removes a git worktree.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import code_string_constants, module_imports
from tests.test_scenario_replay import build_override_command

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TESTS_DIR = _REPO_ROOT / "packages" / "agent" / "tests"
_CONFTEST = _TESTS_DIR / "conftest.py"
_REPLAY = _TESTS_DIR / "test_scenario_replay.py"

#: Env guards scenario 5 must keep: they keep ``git worktree`` from hanging on the Windows Git Credential Manager.
_GIT_GUARDS = (
    "GIT_TERMINAL_PROMPT",
    "GCM_INTERACTIVE",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_VALUE_0",
)


def parent_venv_python() -> Path:
    """The parent checkout's ``.venv`` interpreter (``scripts/dev.py``'s layout)."""
    if os.name == "nt":
        return _REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    return _REPO_ROOT / ".venv" / "bin" / "python"


def _module_file_in_parent_venv(module: str) -> str:
    """Ask the parent checkout's ``.venv`` interpreter where ``module`` resolves, by its own import machinery."""
    interpreter = parent_venv_python()
    assert interpreter.is_file(), (
        f"the parent checkout's virtualenv interpreter is missing at {interpreter} — "
        f"run `python scripts/dev.py install` first"
    )
    completed = subprocess.run(
        [
            str(interpreter),
            "-c",
            f"import {module}, sys; sys.stdout.write({module}.__file__ or '')",
        ],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
    )
    assert completed.returncode == 0, f"{interpreter} could not import {module}: {completed.stderr}"
    return completed.stdout.strip()


# ---------------------------------------------------------------------------
# The build override
# ---------------------------------------------------------------------------


class TestBuildOverrideCommand:
    """The override stays the direct validate command, run by an absolute interpreter."""

    def test_override_is_the_direct_cli_validate_command(self, tmp_path):
        manifest = tmp_path / "swcraft.yaml"
        command = build_override_command(manifest)
        assert "-m swcraft_tools.cli validate --project" in command
        assert command.endswith(manifest.as_posix())

    def test_override_is_not_a_build_wrapper(self, tmp_path):
        command = build_override_command(tmp_path / "swcraft.yaml").lower()
        for wrapper in ("dev.py", "moon"):
            assert wrapper not in command, (
                f"scenario 5's build override must stay the direct validate command, not {wrapper!r}: "
                f"the worktree is a bare checkout with no environment of its own"
            )

    def test_interpreter_is_the_running_suite_interpreter_by_absolute_path(self, tmp_path):
        command = build_override_command(tmp_path / "swcraft.yaml")
        interpreter = command.split(" ", 1)[0]
        assert interpreter == Path(sys.executable).as_posix()
        assert Path(interpreter).is_absolute()
        assert Path(interpreter).exists()
        assert interpreter not in ("python", "python3"), (
            "a bare `python` resolves to whatever comes first on PATH — under "
            "`dev.py test-slow` that is not the checkout's .venv, which is not on PATH at all"
        )

    def test_a_non_absolute_interpreter_is_rejected(self, tmp_path):
        with pytest.raises(ValueError) as exc:
            build_override_command(tmp_path / "swcraft.yaml", interpreter="python")
        assert "absolute" in str(exc.value).lower()

    def test_scenario5_builds_the_override_through_the_helper(self):
        src = _REPLAY.read_text(encoding="utf-8")
        assert 'monkeypatch.setenv("SWCRAFT_MCP_BUILD_COMMAND", build_override_command(' in src, (
            "scenario 5 must set SWCRAFT_MCP_BUILD_COMMAND from build_override_command(), "
            "so the guards in this file constrain the command the scenario actually uses"
        )


# ---------------------------------------------------------------------------
# No Moon residue, but the git guards stay
# ---------------------------------------------------------------------------


class TestNoMoonResidue:
    """The fixture and the scenario no longer mention Moon in any form."""

    @pytest.mark.parametrize("path", [_CONFTEST, _REPLAY], ids=lambda p: p.name)
    def test_no_moon_reference_remains(self, path: Path):
        offenders = [
            f"{path.name}:{number}: {line.strip()}"
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            if "moon" in line.lower()
        ]
        assert not offenders, (
            "scenario 5 has never used Moon (its build override bypasses it); no Moon "
            "reference may remain in the fixture or the scenario:\n" + "\n".join(offenders)
        )

    def test_no_env_scrubbing_of_the_interpreter_environment(self):
        src = _REPLAY.read_text(encoding="utf-8")
        for scrubbed in ("MOON_", "VIRTUAL_ENV", "CONDA_PREFIX"):
            assert scrubbed not in src, (
                f"the {scrubbed!r} scrub existed to stop an inherited environment from choosing "
                f"the build's interpreter; build_override_command names it absolutely instead"
            )

    def test_git_credential_guards_are_kept(self):
        src = _REPLAY.read_text(encoding="utf-8")
        for guard in _GIT_GUARDS:
            assert guard in src, f"{guard} protects `git worktree` on Windows and must be kept"


# ---------------------------------------------------------------------------
# The fixture repairs nothing
# ---------------------------------------------------------------------------


class TestWorktreeFixtureRestoresNothing:
    """``worktree_project`` creates and removes a worktree — and does nothing else."""

    def _subprocess_argv_programs(self) -> list[str]:
        """The first element of every ``subprocess.run([...])`` argv in conftest.py."""
        tree = ast.parse(_CONFTEST.read_text(encoding="utf-8"))
        programs: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "run"):
                continue
            if not (isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
                continue
            assert node.args, "subprocess.run must be called with an explicit argv list"
            argv = node.args[0]
            assert isinstance(argv, ast.List) and argv.elts, "subprocess.run needs a list argv (no shell)"
            first = argv.elts[0]
            programs.append(first.value if isinstance(first, ast.Constant) else "<dynamic>")
        return programs

    def test_fixture_only_shells_out_to_git(self):
        assert self._subprocess_argv_programs() == ["git", "git"], (
            "the fixture must only run `git worktree add` and `git worktree remove` — "
            "any further subprocess is the editable-install restoration coming back"
        )

    def test_the_fixture_module_does_not_import_the_import_machinery(self):
        """A worktree cannot repoint the parent's imports, so the fixture needs no import machinery."""
        imported = module_imports(_CONFTEST)
        offenders = [name for name in imported if name.split(".")[0] in {"importlib", "sysconfig", "site"}]
        assert offenders == [], (
            f"packages/agent/tests/conftest.py imports the import machinery ({offenders}) -- the fixture "
            f"creates and removes a worktree and does nothing else"
        )

    def test_the_fixture_module_computes_with_no_pip_invocation(self):
        """Asserted over computed string literals, never raw text: prose is not an editable-install restoration."""
        literals = code_string_constants(_CONFTEST)
        offenders = [literal for literal in literals if "pip" in literal or literal == "-e"]
        assert offenders == [], (
            f"packages/agent/tests/conftest.py computes with {offenders} -- the editable-install restoration "
            f"is back; the fixture creates and removes a worktree and does nothing else"
        )


# ---------------------------------------------------------------------------
# The parent .venv survives the fixture's teardown
# ---------------------------------------------------------------------------


@pytest.fixture()
def parent_venv_probe():
    """Assert the parent ``.venv`` is intact *after* ``worktree_project`` tore down.

    Requested **before** ``worktree_project``, so it is torn down last; it asserts the worktree
    is already gone, so a flipped order fails instead of weakening the check.
    """
    holder: dict[str, Path] = {}
    yield holder

    worktree = holder.get("worktree")
    assert worktree is not None, "the test must record the worktree path in the probe holder"
    assert not worktree.exists(), (
        f"parent_venv_probe must tear down after worktree_project (request it first in the "
        f"test signature); the worktree is still present at {worktree}"
    )
    resolved = _module_file_in_parent_venv("swcraft_tools")
    expected_root = _REPO_ROOT / "packages" / "tools" / "src"
    assert str(expected_root) in resolved, (
        f"after the worktree fixture tore down, the parent checkout's .venv must still resolve "
        f"swcraft_tools to the parent's own src/ — no restoration step runs any more. "
        f"Resolved to {resolved!r}, expected under {expected_root}"
    )


@pytest.mark.slow
def test_parent_venv_still_resolves_swcraft_tools_after_teardown(parent_venv_probe, worktree_project):
    """The worktree cannot repoint the parent checkout's imports — before or after teardown."""
    parent_venv_probe["worktree"] = worktree_project.parents[2]

    assert worktree_project.is_file(), f"the worktree checkout is missing its manifest: {worktree_project}"
    # Nothing is installed into the worktree, so this holds while it exists too.
    resolved = _module_file_in_parent_venv("swcraft_tools")
    assert str(_REPO_ROOT / "packages" / "tools" / "src") in resolved, resolved
