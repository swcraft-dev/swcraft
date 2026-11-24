"""The workstation installer, ``scripts/workstation/install.py``.

The installer is stdlib-only and shipped by copying: it runs before any wheel is installed, so it may import
nothing the wheels bring. Like ``scripts/dev.py`` it is built as a plan: :func:`plan_install` turns arguments, an
environment, a target platform and a found interpreter into an ordered list of :class:`Step` s without touching the
filesystem, the registry or a subprocess. Most of this file asserts on that list; the rest covers the executing
layer: the pure ``PATH`` and broadcast values, :func:`execute_plan` driven through a :class:`Journal` that records
every seam call in order, and one ``artifact``-marked class that installs the real archive into a ``tmp_path`` home
(:class:`TestTheRealArchiveInstalls`).

Every case plans for a platform this host is not: ``platform`` is a parameter, and paths are built with
``PureWindowsPath`` / ``PurePosixPath`` and spelled with ``str()``, because an argv, a link target or a ``PATH``
entry must carry the target platform's separators.

What these tests do not reach: nothing here runs on Windows; the junction, the ``HKCU\\Environment`` edit, the
broadcast and the ``code.cmd`` call are driven through injected seams, not observed. ``winreg``, ``ctypes`` and
``_winapi`` are imported by no test. The ``artifact`` class is evidence about the platform the suite runs on alone.
:class:`TestParseFloor` states its own reach.
"""

from __future__ import annotations

import ast
import base64
import contextlib
import datetime
import hashlib
import inspect
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tarfile
import tomllib
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
from conftest import (
    REPO_ROOT,
    build_artifacts_module,
    install_workstation_archive,
    installed_executable,
    require_wheelhouse,
    workstation_install_module,
)
from symlink_support import skip_without_symlink_support, symlink_or_skip

_INSTALL = workstation_install_module()
_ARTIFACTS = build_artifacts_module()

_SCRIPT_PATH = REPO_ROOT / "scripts" / "workstation" / "install.py"
_CMD_WRAPPER = REPO_ROOT / "scripts" / "workstation" / "swcraft_install.cmd"
_SH_WRAPPER = REPO_ROOT / "scripts" / "workstation" / "swcraft_install.sh"

#: The ``pip`` step's progress label: what it installs by name.
_PIP_LABEL = f"installing {_INSTALL.REQUIREMENT}, {_INSTALL.GENERATOR_REQUIREMENT} and their dependencies"

#: A Windows archive, home and interpreter, spelled as Windows spells them.
_WIN_ARCHIVE = r"C:\Downloads\swcraft-0.7.0-windows-x64"
_WIN_HOME = r"C:\Users\dev\AppData\Local\SWCraft"
_WIN_PYTHON = ("py", "-3.11")
_WIN_CODE = r"C:\Program Files\Microsoft VS Code\bin\code.cmd"

#: The POSIX equivalents.
_POSIX_ARCHIVE = "/home/dev/swcraft-0.7.0-linux-x64"
_POSIX_HOME = "/home/dev/.swcraft"
_POSIX_PYTHON = ("/usr/bin/python3.11",)
_POSIX_CODE = "/usr/local/bin/code"

_VERSION = "0.7.0"


def _posix_home(path: Path) -> str:
    """*path* spelled so that a ``platform="posix"`` plan calls it absolute -- on this host.

    On Windows a ``tmp_path`` is ``C:\\Users\\...``, which :func:`install.home_is_absolute` refuses under POSIX
    rules. Dropping the drive satisfies both readers: ``/Users/...`` is absolute to :class:`PurePosixPath`, and
    Windows resolves a drive-less rooted path against the current drive. Skips when the temp directory is on another
    drive than the working directory's, where that spelling would name a different place.
    """
    if os.name != "nt":
        return str(path)
    if path.drive.upper() != Path.cwd().drive.upper():
        pytest.skip(
            f"{path} is on {path.drive} and the working directory on {Path.cwd().drive}: "
            "a drive-less spelling would not name it"
        )
    return "/" + path.as_posix().split(":/", 1)[1]


def _without_the_first_spelling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``_winapi.CreateJunction`` unavailable, on every host.

    The fallback-chain cases assume the first spelling fails; on Windows it works and the chain would go
    unexercised. A ``None`` in ``sys.modules`` makes ``import _winapi`` raise ``ImportError``; ``subprocess`` keeps
    the reference it already holds.
    """
    monkeypatch.setitem(sys.modules, "_winapi", None)


def _unstartable(*args, **kwargs):
    """``subprocess.run`` for a program that is not there -- what macOS answers for ``cmd.exe``."""
    raise FileNotFoundError(2, "No such file or directory", "cmd.exe")


def _windows_args(*extra: str):
    return _INSTALL.parse_args(["--home", _WIN_HOME, "--version", _VERSION, *extra], archive_root=_WIN_ARCHIVE)


def _posix_args(*extra: str):
    return _INSTALL.parse_args(["--home", _POSIX_HOME, "--version", _VERSION, *extra], archive_root=_POSIX_ARCHIVE)


#: Answers to "the version directory holds an interpreter that starts", a fact `plan_install` takes rather than
#: reads. ``None`` is the function's own default: a directory name proves nothing.
_NO_VENV = None
_VENV_STARTS = lambda path: True  # noqa: E731 - one expression, named for the reader


def _windows_plan(*extra: str, env=None, installed=(), found_code=_WIN_CODE, venv_usable=_NO_VENV, members=()):
    return _INSTALL.plan_install(
        _windows_args(*extra),
        {"PATH": r"C:\Windows\system32"} if env is None else env,
        "nt",
        _WIN_PYTHON,
        installed=installed,
        found_code=found_code,
        venv_usable=venv_usable,
        members=members,
    )


def _posix_plan(*extra: str, env=None, installed=(), found_code=_POSIX_CODE, venv_usable=_NO_VENV, members=()):
    return _INSTALL.plan_install(
        _posix_args(*extra),
        {"PATH": "/usr/bin:/bin"} if env is None else env,
        "posix",
        _POSIX_PYTHON,
        installed=installed,
        found_code=found_code,
        venv_usable=venv_usable,
        members=members,
    )


#: An archive's member list without ``runtime/`` and with it, once per Node.js spelling the three platforms publish.
_MEMBERS_WITHOUT_RUNTIME = (
    "README.md",
    "install.py",
    "swcraft_install.cmd",
    "swcraft_install.sh",
    f"swcraft-vscode-{_VERSION}.vsix",
    "wheels/PLATFORM.json",
    f"wheels/swcraft_agent-{_VERSION}-py3-none-any.whl",
)
_LINUX_MEMBERS = _MEMBERS_WITHOUT_RUNTIME + ("runtime/RUNTIME.json", "runtime/node.tar.xz")
_MACOS_MEMBERS = _MEMBERS_WITHOUT_RUNTIME + ("runtime/RUNTIME.json", "runtime/node.tar.gz")
_WINDOWS_MEMBERS = _MEMBERS_WITHOUT_RUNTIME + ("runtime/RUNTIME.json", "runtime/node.zip")

#: The member a 0.11.0 archive carried beside Node.js, since removed.
_RETIRED_LIKEC4_MEMBER = "runtime/likec4.tar.xz"

#: The two companion extensions an archive carries under ``extensions/``, in the pack's order -- deliberately not
#: the order they are handed over in, which is sorted.
_COMPANION_MEMBERS = (
    "extensions/redhat.vscode-yaml.vsix",
    "extensions/redhat.vscode-xml.vsix",
)
_MEMBERS_WITH_COMPANIONS = _MEMBERS_WITHOUT_RUNTIME + _COMPANION_MEMBERS + ("THIRD-PARTY-NOTICES.md",)

#: The CPython payload, and a whole archive's member list -- all three payloads -- per platform.
_PYTHON_MEMBER = "runtime/python.tar.gz"
_FULL_LINUX_MEMBERS = _LINUX_MEMBERS + (_PYTHON_MEMBER,)
_FULL_WINDOWS_MEMBERS = _WINDOWS_MEMBERS + (_PYTHON_MEMBER,)


def _small_archive(root: Path, names) -> Path:
    """An unpacked archive at *root* whose ``SHA256SUMS`` lists *names*, each a small real file."""
    lines = []
    for name in names:
        payload = f"the member {name}\n".encode()
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(payload)
        lines.append(f"{hashlib.sha256(payload).hexdigest()}  {name}\n")
    (root / "SHA256SUMS").write_text("".join(lines), encoding="utf-8", newline="\n")
    return root


def _annotation_nodes(tree: ast.AST) -> set[int]:
    """The ids of every node inside an annotation.

    ``from __future__ import annotations`` makes an annotation a string that is never evaluated, so ``str | None``
    there costs nothing on a 3.7 interpreter; only a *runtime* union is banned.
    """
    inside: set[int] = set()
    for node in ast.walk(tree):
        annotations = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        elif isinstance(node, ast.arg):
            annotations.append(node.annotation)
        for annotation in annotations:
            if annotation is None:
                continue
            for child in ast.walk(annotation):
                inside.add(id(child))
    return inside


# ---------------------------------------------------------------------------
# The parse floor
# ---------------------------------------------------------------------------


class TestParseFloor:
    """The refusal has to be printable by the interpreter that found the file.

    ``install.py`` runs only on 3.11, but it must compile under whatever the wrapper found, or "you need Python
    3.11" arrives as a ``SyntaxError`` traceback. The floor is asserted twice:

    * :meth:`test_no_construct_newer_than_the_floor` is a syntactic AST scan; it knows nothing about the standard
      library (a 3.7 interpreter would compile the file and still fail to import a newer stdlib call).
    * :meth:`test_compiles_under_every_interpreter_on_this_host` compiles the file with each interpreter on
      ``PATH``; it reaches the interpreters this host carries and nothing older. It never skips: with no other
      interpreter found it compiles with the running one.

    The floor is 3.7, not 3.6: ``from __future__ import annotations`` is itself a 3.7 feature, so a 3.6 interpreter
    cannot reach the refusal whatever else the file avoids.
    """

    def test_no_construct_newer_than_the_floor(self) -> None:
        tree = ast.parse(_SCRIPT_PATH.read_text(encoding="utf-8"))
        annotated = _annotation_nodes(tree)
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Match):
                offenders.append(f"match statement at line {node.lineno} (3.10)")
            if isinstance(node, ast.TryStar):
                offenders.append(f"except* at line {node.lineno} (3.11)")
            if isinstance(node, ast.NamedExpr):
                offenders.append(f"walrus at line {node.lineno} (3.8)")
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr) and id(node) not in annotated:
                offenders.append(f"runtime X | Y at line {node.lineno} (3.10)")
            if isinstance(node, ast.arguments) and node.posonlyargs:
                offenders.append("positional-only parameters (3.8)")
        assert offenders == [], (
            f"{_SCRIPT_PATH.name} must compile under the interpreter the wrapper found, which is "
            f"not necessarily 3.11: {offenders}"
        )

    def test_the_ban_on_runtime_unions_is_not_vacuous(self) -> None:
        """The scan above exempts annotations, and something has to prove it can still fail."""
        tree = ast.parse("from __future__ import annotations\nx: int | None = None\ny = 1 | 2\n")
        annotated = _annotation_nodes(tree)
        unions = [node for node in ast.walk(tree) if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)]
        assert len(unions) == 2
        assert [id(node) in annotated for node in unions] == [True, False]

    def test_the_refusal_sentence_names_both_versions(self) -> None:
        assert _INSTALL.python_refusal((3, 11, 13)) is None
        message = _INSTALL.python_refusal((3, 9, 6))
        assert "3.11" in message and "3.9" in message and "--python" in message

    def test_the_future_import_is_the_first_statement(self) -> None:
        tree = ast.parse(_SCRIPT_PATH.read_text(encoding="utf-8"))
        statements = [node for node in tree.body if not isinstance(node, ast.Expr)]
        first = statements[0]
        assert isinstance(first, ast.ImportFrom) and first.module == "__future__"
        assert [alias.name for alias in first.names] == ["annotations"]

    def test_compiles_under_every_interpreter_on_this_host(self) -> None:
        source = _SCRIPT_PATH.read_text(encoding="utf-8")
        for interpreter in _older_interpreters():
            result = subprocess.run(
                [interpreter, "-c", "import sys; compile(sys.stdin.read(), 'install.py', 'exec')"],
                input=source,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            assert result.returncode == 0, f"{interpreter} cannot compile install.py: {result.stderr}"

    def test_runs_under_every_interpreter_on_this_host(self) -> None:
        """Each interpreter on this host runs the file, not only compiles it.

        ``--python PATH`` suppresses the version refusal, so an older interpreter executes the imports and argparse;
        ``--help`` is the cheapest whole-file execution. Reaches only the interpreters this host carries.
        """
        for interpreter in _older_interpreters():
            result = subprocess.run(
                [interpreter, str(_SCRIPT_PATH), "--help"],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            assert result.returncode == 0, f"{interpreter} cannot run install.py: {result.stderr}"
            assert "--python" in result.stdout

    def test_the_installer_imports_only_the_standard_library(self) -> None:
        tree = ast.parse(_SCRIPT_PATH.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        assert imported <= set(sys.stdlib_module_names), (
            "the installer runs before any wheel is installed: every import must be stdlib -- "
            f"{sorted(imported - set(sys.stdlib_module_names))}"
        )


# ---------------------------------------------------------------------------
# Arguments and the default home
# ---------------------------------------------------------------------------


class TestArguments:
    def test_source_defaults_to_the_archives_wheels_directory(self) -> None:
        args = _windows_args()
        assert args.source is None
        step = [step for step in _windows_plan() if step.kind == "pip"][0]
        assert str(PureWindowsPath(_WIN_ARCHIVE) / "wheels") in step.argv

    def test_source_with_index_url_is_refused_in_one_sentence(self) -> None:
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.parse_args(["--source", "wheels", "--index-url", "https://example.invalid/simple"])
        message = str(excinfo.value)
        assert "--source" in message and "--index-url" in message
        assert message.count(".") <= 1, "a refusal is one sentence"

    def test_home_is_absolute_reads_the_target_platforms_flavour(self) -> None:
        """``--home`` is read under the flavour that will spell the path: the target platform's.

        Reading both flavours at once accepted ``C:\\SWCraft`` on a POSIX target, which ``PurePosixPath`` then spelt
        as relative, and the ``current`` link dangled behind an exit-0 "is installed in ...". In production the
        flavour is the host's (``platform`` defaults to ``os.name``).
        """
        for accepted, platform in (
            ("/home/dev/.swcraft", "posix"),
            ("//srv/share/swcraft", "posix"),
            (_WIN_HOME, "nt"),
            (r"\\srv\share\swcraft", "nt"),
        ):
            assert _INSTALL.home_is_absolute(accepted, platform) is True, (accepted, platform)
        for refused, platform in (
            ("relhome", "posix"),
            ("./relhome", "posix"),
            ("relhome/versions", "posix"),
            ("~/swcraft", "posix"),
            ("relhome", "nt"),
            ("C:dist", "nt"),
            ("\\dist", "nt"),
            # The two mirrors: each is what the other platform calls absolute.
            (r"C:\SWCraft", "posix"),
            (r"\\srv\share\swc", "posix"),
            ("/dist", "nt"),
        ):
            assert _INSTALL.home_is_absolute(refused, platform) is False, (refused, platform)

    def test_the_gate_agrees_with_the_path_that_is_actually_built(self) -> None:
        """``home_is_absolute`` and ``home_directory`` read one flavour.

        Asserting their agreement, not a list of spellings, makes a future divergence a failure here.
        """
        for spelling in ("/home/dev/.swcraft", r"C:\SWCraft", "relhome", "C:dist", "//srv/x"):
            for platform in ("posix", "nt"):
                args = _INSTALL.parse_args(["--home", spelling, "--version", _VERSION], archive_root=_POSIX_ARCHIVE)
                assert (
                    _INSTALL.home_is_absolute(spelling, platform)
                    == _INSTALL.home_directory(args, {}, platform).is_absolute()
                ), (spelling, platform)

    def test_home_replaces_the_default(self) -> None:
        args = _windows_args()
        assert _INSTALL.home_directory(args, {}, "nt") == PureWindowsPath(_WIN_HOME)

    def test_the_default_windows_home_is_localappdata(self) -> None:
        args = _INSTALL.parse_args(["--version", _VERSION], archive_root=_WIN_ARCHIVE)
        home = _INSTALL.home_directory(args, {"LOCALAPPDATA": r"C:\Users\dev\AppData\Local"}, "nt")
        assert home == PureWindowsPath(r"C:\Users\dev\AppData\Local\SWCraft")

    def test_the_default_posix_home_is_dot_swcraft(self) -> None:
        args = _INSTALL.parse_args(["--version", _VERSION], archive_root=_POSIX_ARCHIVE)
        home = _INSTALL.home_directory(args, {"HOME": "/home/dev"}, "posix")
        assert home == PurePosixPath("/home/dev/.swcraft")

    def test_a_windows_environment_without_a_profile_is_refused(self) -> None:
        args = _INSTALL.parse_args(["--version", _VERSION], archive_root=_WIN_ARCHIVE)
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.home_directory(args, {}, "nt")
        assert "--home" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The fresh install
# ---------------------------------------------------------------------------


class TestFreshInstall:
    def test_the_windows_order(self) -> None:
        steps = _windows_plan()
        assert [step.kind for step in steps] == [
            "check-platform",
            "venv",
            "pip",
            "link",
            "path",
            "vsix",
        ]

        home = PureWindowsPath(_WIN_HOME)
        archive = PureWindowsPath(_WIN_ARCHIVE)
        target = home / "versions" / _VERSION
        assert steps[0].path == str(archive / "wheels" / "PLATFORM.json")
        assert steps[1].argv == ("py", "-3.11", "-m", "venv", str(target))
        assert steps[2].argv == (
            str(target / "Scripts" / "python.exe"),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(archive / "wheels"),
            f"swcraft-agent=={_VERSION}",
            f"swcraft-specimens=={_VERSION}",
        )
        assert (steps[3].path, steps[3].target) == (str(home / "current"), str(target))
        assert steps[4].entry == str(home / "current" / "Scripts")
        assert steps[5].argv == (
            "cmd.exe",
            "/c",
            "call",
            _WIN_CODE,
            "--install-extension",
            str(archive / f"swcraft-vscode-{_VERSION}.vsix"),
            "--force",
        )

    def test_the_vsix_step_forces_so_a_rollback_rolls_the_extension_back_too(self) -> None:
        """`--force`, so a rollback rolls the extension back too.

        `code --install-extension` refuses to replace a newer version with an older one and exits zero ("Use
        '--force' option to downgrade"), so running an older installer rolled the toolchain back, reported success and
        left the extension at the newer version. Asserted on both platform plans.
        """
        for steps in (_windows_plan(), _posix_plan()):
            vsix = [step for step in steps if step.kind == "vsix"]
            assert len(vsix) == 1, "one extension step, whichever platform"
            assert vsix[0].argv[-1] == "--force"
            assert vsix[0].argv[-2] == vsix[0].path, "the flag follows the package it is about"

    def test_the_posix_order_uses_bin(self) -> None:
        steps = _posix_plan()
        assert [step.kind for step in steps] == [
            "check-platform",
            "venv",
            "pip",
            "link",
            "path",
            "vsix",
        ]
        home = PurePosixPath(_POSIX_HOME)
        target = home / "versions" / _VERSION
        assert steps[1].argv == (_POSIX_PYTHON[0], "-m", "venv", str(target))
        assert steps[2].argv[0] == str(target / "bin" / "python")
        assert steps[4].entry == str(home / "current" / "bin")
        assert steps[5].argv[0] == _POSIX_CODE

    def test_a_fresh_install_removes_nothing(self) -> None:
        assert [step for step in _windows_plan() if step.kind in {"remove", "path-remove"}] == []


# ---------------------------------------------------------------------------
# Update, repair, uninstall
# ---------------------------------------------------------------------------


class TestUpdateAndRepair:
    def test_the_removals_come_after_the_vsix(self) -> None:
        steps = _windows_plan(installed=("0.6.0", "0.5.3"))
        assert [step.kind for step in steps] == [
            "check-platform",
            "venv",
            "pip",
            "link",
            "path",
            "vsix",
            "remove",
            "remove",
        ]
        home = PureWindowsPath(_WIN_HOME)
        assert [step.path for step in steps if step.kind == "remove"] == [
            str(home / "versions" / "0.5.3"),
            str(home / "versions" / "0.6.0"),
        ]

    def test_the_same_version_is_a_repair(self) -> None:
        steps = _windows_plan(installed=(_VERSION,), venv_usable=_VENV_STARTS)
        assert [step.kind for step in steps] == ["check-platform", "pip", "link", "path", "vsix"]
        assert "--force-reinstall" in steps[1].argv

    def test_a_repair_beside_an_older_version_still_removes_it(self) -> None:
        steps = _windows_plan(installed=(_VERSION, "0.6.0"), venv_usable=_VENV_STARTS)
        assert [step.kind for step in steps] == [
            "check-platform",
            "pip",
            "link",
            "path",
            "vsix",
            "remove",
        ]

    def test_a_version_directory_whose_interpreter_is_missing_is_rebuilt(self) -> None:
        """A version directory whose interpreter does not start gets its venv rebuilt.

        ``python -m venv`` can create ``versions/<v>/`` and then fail (an interrupt, a full disk, no ``ensurepip``).
        Deciding the repair from the directory name made every later run fail with ``FileNotFoundError``, and the user
        has no checkout and is never told to delete the directory.
        """
        asked: list[str] = []

        def venv_usable(path):
            asked.append(path)
            return False

        steps = _windows_plan(installed=(_VERSION,), venv_usable=venv_usable)
        assert [step.kind for step in steps] == ["check-platform", "venv", "pip", "link", "path", "vsix"]
        assert asked == [str(PureWindowsPath(_WIN_HOME) / "versions" / _VERSION / "Scripts" / "python.exe")]
        assert "--force-reinstall" in [step for step in steps if step.kind == "pip"][0].argv, (
            "it is still a repair: the directory is the one that is already there"
        )

    def test_an_absent_answer_plans_the_venv_step(self) -> None:
        """The default is the safe one: a directory name is not an installation."""
        assert "venv" in [step.kind for step in _windows_plan(installed=(_VERSION,))]

    def test_an_update_does_not_force_reinstall(self) -> None:
        steps = _windows_plan(installed=("0.6.0",))
        pip = [step for step in steps if step.kind == "pip"][0]
        assert "--force-reinstall" not in pip.argv


class TestUninstall:
    def test_the_four_steps_in_order(self) -> None:
        steps = _windows_plan("--uninstall")
        assert [step.kind for step in steps] == ["remove", "path-remove", "vsix-remove", "bootstrap-remove"]
        home = PureWindowsPath(_WIN_HOME)
        assert steps[0].path == str(home)
        assert steps[1].entry == str(home / "current" / "Scripts")
        assert steps[2].argv == ("cmd.exe", "/c", "call", _WIN_CODE, "--uninstall-extension", _INSTALL.EXTENSION_ID)
        assert steps[3].path == str(PureWindowsPath(_WIN_ARCHIVE) / _INSTALL.BOOTSTRAP_DIRNAME)

    def test_no_path_and_no_vsix_apply_to_uninstall_too(self) -> None:
        steps = _windows_plan("--uninstall", "--no-path", "--no-vsix")
        assert [step.kind for step in steps] == ["remove", "bootstrap-remove"]

    def test_the_bootstrap_copy_a_wrapper_made_is_removed_and_a_locked_one_is_a_sentence(self) -> None:
        """Both wrappers unpack ``.bootstrap`` whatever the arguments, so an uninstall removes it too.

        A copy that cannot be removed (on Windows the running interpreter lives in it) is a sentence, never an
        unremoved home that fails the run.
        """
        journal = Journal(locked=_INSTALL.BOOTSTRAP_DIRNAME)
        unremoved = journal.execute(_posix_plan("--uninstall", "--no-path", "--no-vsix"), "posix")
        assert unremoved == ()
        assert journal.kinds == ["remove", "remove", "echo"]
        assert journal.calls[1] == ("remove", str(PurePosixPath(_POSIX_ARCHIVE) / _INSTALL.BOOTSTRAP_DIRNAME))
        assert "delete that directory yourself" in journal.echoed[0]

    def test_on_windows_the_locked_bootstrap_copy_is_left_to_the_wrapper_without_a_word(self) -> None:
        """``python.exe`` runs from inside ``.bootstrap``, so Windows never lets go of it.

        The ``.cmd`` wrapper removes the directory once ``install.py`` has exited
        (:meth:`TestWrappers.test_the_cmd_wrapper_removes_the_bootstrap_copy_after_the_installer_returns`). That is the
        normal path, so the run says nothing: a line about it read as a failure on every install.
        """
        journal = Journal(locked=_INSTALL.BOOTSTRAP_DIRNAME)
        plan = _windows_plan("--uninstall", "--no-path", "--no-vsix")
        bootstrap = [step.path for step in plan if step.kind == "bootstrap-remove"]
        assert len(bootstrap) == 1, plan
        unremoved = journal.execute(plan, "nt")
        assert ("remove", bootstrap[0]) in journal.calls, "the removal is still attempted"
        assert unremoved == ()
        assert [line for line in journal.echoed if _INSTALL.BOOTSTRAP_DIRNAME in line] == [], journal.echoed

    def test_uninstall_checks_no_platform(self) -> None:
        assert [step for step in _windows_plan("--uninstall") if step.kind == "check-platform"] == []


# ---------------------------------------------------------------------------
# The runtime payloads: planned
# ---------------------------------------------------------------------------


class TestTheRuntimeIsPlanned:
    """The ``runtime`` and ``node`` steps, as a plan: where, when, and only when carried.

    Both kinds sit after ``pip`` and before ``link``: install beside the old version, switch ``current`` last.
    Whether anything is unpacked is not reached here; :class:`TestTheRuntimePayloadIsUnpacked` and
    :class:`TestNodeIsPlaced` do that on this host's filesystem.
    """

    def test_the_posix_order_with_the_payloads(self) -> None:
        steps = _posix_plan(members=_LINUX_MEMBERS)
        assert [step.kind for step in steps] == [
            "check-platform",
            "venv",
            "pip",
            "runtime",
            "node",
            "link",
            "path",
            "vsix",
        ]
        archive = PurePosixPath(_POSIX_ARCHIVE)
        target = PurePosixPath(_POSIX_HOME) / "versions" / _VERSION
        assert [(step.path, step.target) for step in steps if step.kind == "runtime"] == [
            (str(archive / "runtime" / "node.tar.xz"), str(target / "runtime" / "node")),
        ]
        node = [step for step in steps if step.kind == "node"][0]
        assert node.path == str(target / "bin" / "node")
        assert node.target == "../runtime/node/bin/node", "a RELATIVE link, so it resolves through `current`"

    def test_the_windows_order_copies_node_exe_into_scripts(self) -> None:
        steps = _windows_plan(members=_WINDOWS_MEMBERS)
        assert [step.kind for step in steps] == [
            "check-platform",
            "venv",
            "pip",
            "runtime",
            "node",
            "link",
            "path",
            "vsix",
        ]
        archive = PureWindowsPath(_WIN_ARCHIVE)
        target = PureWindowsPath(_WIN_HOME) / "versions" / _VERSION
        runtime = [step for step in steps if step.kind == "runtime"]
        assert (runtime[0].path, runtime[0].target) == (
            str(archive / "runtime" / "node.zip"),
            str(target / "runtime" / "node"),
        )
        node = [step for step in steps if step.kind == "node"][0]
        assert (node.path, node.target) == (
            str(target / "Scripts" / "node.exe"),
            str(target / "runtime" / "node" / "node.exe"),
        ), "the planned destination and the absolute source it is copied from"

    def test_the_macos_node_file_is_planned_the_same_way(self) -> None:
        steps = _posix_plan(members=_MACOS_MEMBERS)
        assert [step.path for step in steps if step.kind == "runtime"][0] == str(
            PurePosixPath(_POSIX_ARCHIVE) / "runtime" / "node.tar.gz"
        )
        assert [step.kind for step in steps].count("node") == 1

    def test_an_archive_without_runtime_plans_exactly_what_it_did_before(self) -> None:
        """Every new step is conditional on its payload, so an older archive installs unchanged."""
        assert _posix_plan(members=_MEMBERS_WITHOUT_RUNTIME) == _posix_plan()
        assert _windows_plan(members=_MEMBERS_WITHOUT_RUNTIME) == _windows_plan()
        assert _windows_plan(installed=("0.6.0",), members=_MEMBERS_WITHOUT_RUNTIME) == _windows_plan(
            installed=("0.6.0",)
        )

    def test_a_repair_unpacks_again_before_the_link(self) -> None:
        steps = _windows_plan(installed=(_VERSION,), venv_usable=_VENV_STARTS, members=_WINDOWS_MEMBERS)
        assert [step.kind for step in steps] == [
            "check-platform",
            "pip",
            "runtime",
            "node",
            "link",
            "path",
            "vsix",
        ]

    def test_a_likec4_payload_is_unpacked_by_no_step(self) -> None:
        """The likec4 tree left the archive; a member of that name plans nothing, beside Node.js or alone."""
        assert not [name for name in vars(_INSTALL) if "likec4" in name.lower()]
        assert _posix_plan(members=_MEMBERS_WITHOUT_RUNTIME + (_RETIRED_LIKEC4_MEMBER,)) == _posix_plan()
        for plan, members in ((_posix_plan, _LINUX_MEMBERS), (_windows_plan, _WINDOWS_MEMBERS)):
            steps = plan(members=members + (_RETIRED_LIKEC4_MEMBER,))
            assert steps == plan(members=members), "Node.js is placed as before, and nothing else is unpacked"
            assert not [step for step in steps if "likec4" in (step.path + step.target).lower()]

    def test_each_payload_is_planned_on_its_own(self) -> None:
        node_only = _posix_plan(members=_MEMBERS_WITHOUT_RUNTIME + ("runtime/node.tar.xz",))
        assert [step.kind for step in node_only][3:5] == ["runtime", "node"]

    def test_the_record_alone_plans_nothing(self) -> None:
        assert _posix_plan(members=_MEMBERS_WITHOUT_RUNTIME + ("runtime/RUNTIME.json",)) == _posix_plan()

    def test_two_node_payloads_are_refused_by_name(self) -> None:
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _posix_plan(members=_LINUX_MEMBERS + ("runtime/node.zip",))
        message = str(excinfo.value)
        assert "runtime/node.tar.xz" in message and "runtime/node.zip" in message

    def test_uninstall_plans_no_runtime(self) -> None:
        steps = _windows_plan("--uninstall", members=_WINDOWS_MEMBERS)
        assert [step.kind for step in steps] == ["remove", "path-remove", "vsix-remove", "bootstrap-remove"]

    @pytest.mark.parametrize(
        "extra, installed",
        [
            ((), ()),
            (("--no-path",), ()),
            (("--no-vsix",), ("0.6.0",)),
            (("--no-path", "--no-vsix"), ("0.6.0", "0.5.3")),
        ],
    )
    def test_every_runtime_step_follows_pip_and_precedes_the_link(self, extra, installed) -> None:
        for steps in (
            _posix_plan(*extra, installed=installed, members=_LINUX_MEMBERS),
            _windows_plan(*extra, installed=installed, members=_WINDOWS_MEMBERS),
        ):
            kinds = [step.kind for step in steps]
            runtime = [index for index, kind in enumerate(kinds) if kind in ("runtime", "node")]
            assert len(runtime) == 2
            assert kinds.index("pip") < min(runtime) and max(runtime) < kinds.index("link")

    def test_the_payload_names_are_the_ones_the_archive_builder_writes(self) -> None:
        """The two readers of one vocabulary agree: the builder's names are the installer's."""
        for token in _ARTIFACTS.RUNTIME_PLATFORMS:
            member = f"{_ARTIFACTS.WORKSTATION_RUNTIME_DIRNAME}/{_ARTIFACTS.node_payload_filename(token)}"
            assert member in _INSTALL.NODE_PAYLOAD_MEMBERS, token
        assert (
            f"{_ARTIFACTS.WORKSTATION_RUNTIME_DIRNAME}/{_ARTIFACTS.PYTHON_PAYLOAD_FILENAME}"
            == _INSTALL.PYTHON_PAYLOAD_MEMBER
        )
        for token, row in _ARTIFACTS.RUNTIME_PLATFORMS.items():
            platform = "nt" if token.startswith("windows") else "posix"
            pure = PureWindowsPath if platform == "nt" else PurePosixPath
            assert _INSTALL.installed_python(pure("v"), platform) == pure(
                "v", "runtime", "python", *row["python_executable"].split("/")
            ), "the builder's interpreter path and the installer's name one place in the tree"

    def test_the_python_payload_is_unpacked_before_the_venv_and_builds_it(self) -> None:
        """The venv is built on the installation's own copy, so its unpack precedes ``venv``."""
        steps = _posix_plan(members=_FULL_LINUX_MEMBERS)
        assert [step.kind for step in steps] == [
            "check-platform",
            "runtime",
            "venv",
            "pip",
            "runtime",
            "node",
            "link",
            "path",
            "vsix",
            "bootstrap-remove",
        ]
        archive = PurePosixPath(_POSIX_ARCHIVE)
        target = PurePosixPath(_POSIX_HOME) / "versions" / _VERSION
        assert (steps[1].path, steps[1].target) == (
            str(archive / "runtime" / "python.tar.gz"),
            str(target / "runtime" / "python"),
        )
        assert steps[2].argv == (str(target / "runtime" / "python" / "bin" / "python3"), "-m", "venv", str(target))
        assert steps[0].argv == _POSIX_PYTHON, (
            "check-platform asks the interpreter running the installer -- the wrapper's bootstrap copy of "
            "the same build"
        )
        assert steps[-1] == _INSTALL.Step("bootstrap-remove", path=str(archive / ".bootstrap"))

    def test_the_windows_venv_is_built_on_the_unpacked_python_exe(self) -> None:
        steps = _windows_plan(members=_FULL_WINDOWS_MEMBERS)
        kinds = [step.kind for step in steps]
        target = PureWindowsPath(_WIN_HOME) / "versions" / _VERSION
        venv = steps[kinds.index("venv")]
        assert venv.argv[0] == str(target / "runtime" / "python" / "python.exe")
        assert kinds.index("runtime") < kinds.index("venv")
        assert steps[kinds.index("runtime")].target == str(target / "runtime" / "python")
        assert steps[-1].path == str(PureWindowsPath(_WIN_ARCHIVE) / ".bootstrap")

    def test_python_names_the_interpreter_and_unpacks_no_python_payload(self) -> None:
        """``--python`` means *build on this one, unpack none* -- the plan of an archive without the payload."""
        with_payload = _posix_plan("--python", "/opt/py311/bin/python3.11", members=_FULL_LINUX_MEMBERS)
        assert with_payload == _posix_plan("--python", "/opt/py311/bin/python3.11", members=_LINUX_MEMBERS)
        assert "bootstrap-remove" not in [step.kind for step in with_payload]
        assert [step for step in with_payload if step.kind == "venv"][0].argv[0] == _POSIX_PYTHON[0]

    def test_an_archive_without_the_python_payload_builds_on_the_found_interpreter_as_before(self) -> None:
        steps = _posix_plan(members=_LINUX_MEMBERS)
        assert [step for step in steps if step.kind == "venv"][0].argv[0] == _POSIX_PYTHON[0]
        assert "bootstrap-remove" not in [step.kind for step in steps]

    def test_a_repair_unpacks_the_python_again_and_keeps_the_venv_it_has(self) -> None:
        steps = _posix_plan(installed=(_VERSION,), venv_usable=_VENV_STARTS, members=_FULL_LINUX_MEMBERS)
        assert [step.kind for step in steps] == [
            "check-platform",
            "runtime",
            "pip",
            "runtime",
            "node",
            "link",
            "path",
            "vsix",
            "bootstrap-remove",
        ]

    def test_the_bootstrap_is_removed_after_the_older_versions(self) -> None:
        steps = _windows_plan(installed=("0.6.0",), members=_FULL_WINDOWS_MEMBERS)
        assert steps[-2] == _INSTALL.Step("remove", path=str(PureWindowsPath(_WIN_HOME) / "versions" / "0.6.0"))
        assert steps[-1].kind == "bootstrap-remove"

    def test_an_index_url_still_builds_on_the_carried_python(self) -> None:
        steps = _posix_plan("--index-url", "https://example.invalid/simple", members=_FULL_LINUX_MEMBERS)
        assert [step.kind for step in steps][:3] == ["runtime", "venv", "pip"]

    def test_uninstall_plans_no_python_and_removes_the_bootstrap_copy_last(self) -> None:
        steps = _posix_plan("--uninstall", members=_FULL_LINUX_MEMBERS)
        assert [step.kind for step in steps] == ["remove", "path-remove", "vsix-remove", "bootstrap-remove"]
        # The same step whether or not the archive carries a Python: uninstall reads no member list.
        assert steps == _posix_plan("--uninstall")


# ---------------------------------------------------------------------------
# The companion extensions
# ---------------------------------------------------------------------------


def _extension_markers() -> list[str]:
    """The fragments ``toolchainInstall.ts`` reads as "SWCraft's extension was not installed", from the file itself."""
    source = (REPO_ROOT / "vscode_extension" / "src" / "toolchainInstall.ts").read_text(encoding="utf-8")
    block = source.split("const EXTENSION_NOT_INSTALLED_MARKERS = [", 1)[1].split("];", 1)[0]
    markers = re.findall(r'"([^"]+)"', block)
    assert len(markers) == 2, markers
    return markers


class TestTheCompanionExtensionsAreHandedOverFirst:
    """The pack's members from ``extensions/``, before SWCraft's own ``.vsix`` -- and never a refusal.

    The plan half asserts the argv list; the execution half asserts that a failed companion is one sentence and that
    SWCraft's floor is asked once, before anything is handed over. Not reached: VS Code itself -- whether it installs
    a companion, and which version it keeps once a reachable Marketplace updates one (the archive's copy is a floor,
    not a pin).
    """

    def test_four_handovers_companions_first_swcraft_last_each_forced(self) -> None:
        for plan, archive, pure in (
            (_posix_plan, _POSIX_ARCHIVE, PurePosixPath),
            (_windows_plan, _WIN_ARCHIVE, PureWindowsPath),
        ):
            steps = plan(members=_MEMBERS_WITH_COMPANIONS)
            handovers = [step for step in steps if step.kind in ("vsix-companion", "vsix")]
            assert [step.kind for step in handovers] == ["vsix-companion"] * 2 + ["vsix"]
            assert [step.path for step in handovers] == [
                str(pure(archive, "extensions", "redhat.vscode-xml.vsix")),
                str(pure(archive, "extensions", "redhat.vscode-yaml.vsix")),
                str(pure(archive, f"swcraft-vscode-{_VERSION}.vsix")),
            ]
            for step in handovers:
                assert step.argv[-3:] == ("--install-extension", step.path, "--force"), step
            assert [step.target for step in handovers[:2]] == [
                "redhat.vscode-xml",
                "redhat.vscode-yaml",
            ]
            kinds = [step.kind for step in steps]
            assert kinds.index("link") < kinds.index("vsix-companion"), "the toolchain is current first"

    def test_the_companions_use_the_one_resolved_code(self) -> None:
        steps = _windows_plan(members=_MEMBERS_WITH_COMPANIONS)
        package = [step for step in steps if step.kind == "vsix"][0]
        companions = [step for step in steps if step.kind == "vsix-companion"]
        assert len(companions) == 2, "the loop below must have something to assert on"
        for step in companions:
            assert step.argv[:4] == package.argv[:4] == ("cmd.exe", "/c", "call", _WIN_CODE)

    def test_no_vsix_drops_all_three(self) -> None:
        for plan in (_posix_plan, _windows_plan):
            steps = plan("--no-vsix", members=_MEMBERS_WITH_COMPANIONS)
            assert not [step for step in steps if step.kind in ("vsix", "vsix-companion")]
            assert steps == plan("--no-vsix")

    def test_an_archive_without_extensions_plans_exactly_what_it_did_before(self) -> None:
        with_companions = _posix_plan(members=_MEMBERS_WITH_COMPANIONS)
        assert [step for step in with_companions if step.kind != "vsix-companion"] == _posix_plan()

    def test_uninstall_hands_nothing_over_and_removes_no_companion(self) -> None:
        steps = _windows_plan("--uninstall", members=_MEMBERS_WITH_COMPANIONS)
        assert [step.kind for step in steps] == ["remove", "path-remove", "vsix-remove", "bootstrap-remove"]
        assert steps[2].argv[-2:] == ("--uninstall-extension", _INSTALL.EXTENSION_ID)

    def test_only_a_flat_publisher_name_vsix_under_extensions_is_a_companion(self) -> None:
        names = (
            "extensions/redhat.vscode-yaml.vsix",
            "extensions/nested/example.ext.vsix",
            "extensions/notes.txt",
            "extensions/Not.An.Id.vsix",
            "wheels/example.ext.vsix",
            f"swcraft-vscode-{_VERSION}.vsix",
        )
        assert _INSTALL.companion_packages(names) == (("extensions/redhat.vscode-yaml.vsix", "redhat.vscode-yaml"),)

    def test_the_companion_names_are_the_ones_the_archive_builder_writes(self) -> None:
        """The builder's names for the **committed** pins are exactly what the installer reads back."""
        pins = _ARTIFACTS.read_runtime_pins(REPO_ROOT)
        for token in _ARTIFACTS.RUNTIME_PLATFORMS:
            pairs = [
                (f"{_ARTIFACTS.WORKSTATION_EXTENSIONS_DIRNAME}/{_ARTIFACTS.extension_payload_filename(ident)}", ident)
                for ident, _, _, _ in _ARTIFACTS.extension_pins(pins, token)
            ]
            assert _INSTALL.companion_packages(name for name, _ in pairs) == tuple(sorted(pairs))
        assert _ARTIFACTS.WORKSTATION_EXTENSIONS_DIRNAME == _INSTALL.EXTENSIONS_DIRNAME

    # -- execution ------------------------------------------------------------------

    @staticmethod
    def _installs(journal) -> list[tuple]:
        return [call for call in journal.calls if call[0] == "run" and "--install-extension" in call]

    def test_every_handover_runs_when_code_installs_them_all(self) -> None:
        journal = Journal()
        journal.execute(_windows_plan(members=_MEMBERS_WITH_COMPANIONS, installed=("0.6.0",)), "nt")
        installs = self._installs(journal)
        assert len(installs) == 3
        assert installs[-1][-2] == str(PureWindowsPath(_WIN_ARCHIVE) / f"swcraft-vscode-{_VERSION}.vsix")
        assert journal.kinds[-1] == "remove"
        assert not [line for line in journal.echoed if "was not installed" in line]

    def test_a_companion_code_will_not_install_is_one_sentence_and_the_run_goes_on(self) -> None:
        """By now the toolchain is installed, so a pack member that fails never costs the installation."""
        journal = Journal(codes=lambda argv: 1 if any("redhat.vscode-xml.vsix" in part for part in argv) else 0)
        journal.execute(_windows_plan(members=_MEMBERS_WITH_COMPANIONS, installed=("0.6.0",)), "nt")
        sentences = [line for line in journal.echoed if "was not installed" in line]
        assert len(sentences) == 1
        assert sentences[0].startswith("`redhat.vscode-xml` was not installed (`code` exited with code 1)")
        assert "install it by name from the Extensions view" in sentences[0]
        assert str(PureWindowsPath(_WIN_ARCHIVE) / "extensions" / "redhat.vscode-xml.vsix") in sentences[0]
        assert len(self._installs(journal)) == 3, "the remaining companion and SWCraft's own were still handed over"
        assert journal.kinds[-1] == "remove", "and the run finished"

    def test_a_companion_code_cannot_start_is_one_sentence_too(self) -> None:
        def codes(argv):
            if any("redhat.vscode-yaml.vsix" in part for part in argv):
                raise _INSTALL.InstallError("the program could not be started")
            return 0

        journal = Journal(codes=codes)
        journal.execute(_windows_plan(members=_MEMBERS_WITH_COMPANIONS), "nt")
        sentences = [line for line in journal.echoed if "was not installed" in line]
        assert sentences == [
            "`redhat.vscode-yaml` was not installed (`code` could not be started: the program could not be started) "
            "-- install it by name from the Extensions view, or install "
            f"{PureWindowsPath(_WIN_ARCHIVE) / 'extensions' / 'redhat.vscode-yaml.vsix'} there with Install from VSIX"
        ]

    def test_without_code_each_companion_is_named_and_swcraft_keeps_its_own_sentence(self) -> None:
        journal = Journal()
        journal.execute(_windows_plan(members=_MEMBERS_WITH_COMPANIONS, found_code=None), "nt")
        named = [
            line for line in journal.echoed if "` was not installed (VS Code's `code` command was not found)" in line
        ]
        assert [line.split("`")[1] for line in named] == [
            "redhat.vscode-xml",
            "redhat.vscode-yaml",
        ]
        assert [line for line in journal.echoed if "so the extension was not installed" in line] != [], (
            "SWCraft's own sentence is today's, marker included"
        )
        assert self._installs(journal) == []

    def test_swcraft_failing_after_the_companions_still_stops_the_run(self) -> None:
        """SWCraft's own ``.vsix`` keeps today's outcomes exactly: a ``code`` that fails it is a refusal."""
        journal = Journal(codes=lambda argv: 1 if any("swcraft-vscode-" in part for part in argv) else 0)
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            journal.execute(_windows_plan(members=_MEMBERS_WITH_COMPANIONS, installed=("0.6.0",)), "nt")
        assert "the toolchain is installed and the extension is not" in str(excinfo.value)
        assert len(self._installs(journal)) == 3
        assert journal.kinds[-1] != "remove"

    @staticmethod
    def _execute_with_floor(steps, refusal):
        journal = Journal()
        asked: list = []

        def floor(step):
            asked.append(step)
            return refusal

        _INSTALL.execute_plan(
            steps,
            "nt",
            run=journal.run,
            link=journal.link,
            remove=journal.remove,
            registry=journal,
            echo=journal.echo,
            stamp=journal.stamp,
            current=journal.current,
            floor=floor,
        )
        return journal, asked

    def test_a_floor_refusal_comes_before_anything_is_handed_over(self) -> None:
        steps = _windows_plan(members=_MEMBERS_WITH_COMPANIONS)
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            self._execute_with_floor(steps, "VS Code 1.108.0 is older than ^1.110.0 ...")
        assert str(excinfo.value) == "VS Code 1.108.0 is older than ^1.110.0 ..."

        journal = Journal()
        with pytest.raises(_INSTALL.InstallError):
            _INSTALL.execute_plan(
                steps,
                "nt",
                run=journal.run,
                link=journal.link,
                remove=journal.remove,
                registry=journal,
                echo=journal.echo,
                stamp=journal.stamp,
                current=journal.current,
                floor=lambda step: "too old",
            )
        assert self._installs(journal) == [], "no companion is handed to a VS Code too old for SWCraft"

    def test_the_floor_is_asked_once_and_of_swcrafts_own_package(self) -> None:
        steps = _windows_plan(members=_MEMBERS_WITH_COMPANIONS)
        journal, asked = self._execute_with_floor(steps, None)
        assert asked == [step for step in steps if step.kind == "vsix"], "one question, about SWCraft's package"
        assert len(self._installs(journal)) == 3

    def test_no_companion_sentence_carries_a_marker_the_extension_reads(self) -> None:
        step = _INSTALL.Step("vsix-companion", path="/a/extensions/example.ext.vsix", target="example.ext")
        for why in (
            "VS Code's `code` command was not found",
            "`code` could not be started: gone",
            "`code` exited with code 1",
        ):
            sentence = _INSTALL.companion_not_installed_sentence(step, why)
            for marker in _extension_markers():
                assert marker not in sentence, sentence


# ---------------------------------------------------------------------------
# The flags that drop steps
# ---------------------------------------------------------------------------


class TestFlags:
    def test_no_path_drops_exactly_the_path_step(self) -> None:
        steps = _windows_plan("--no-path")
        assert [step.kind for step in steps] == ["check-platform", "venv", "pip", "link", "vsix"]

    def test_no_vsix_drops_exactly_the_vsix_step(self) -> None:
        steps = _windows_plan("--no-vsix")
        assert [step.kind for step in steps] == ["check-platform", "venv", "pip", "link", "path"]

    def test_the_windows_step_is_planned_whatever_the_process_path_says(self) -> None:
        """On Windows the path step is always planned, and ``_add_path_entry`` decides.

        The process ``PATH`` is not the value that decides there: the durable one is ``HKCU\\Environment``'s, the two
        disagree routinely, and reading the process value planned no step and never wrote the registry. "Added once"
        is asserted in :class:`TestTheDurableWindowsPathIsTheAuthority` and :class:`TestPathValue`.
        """
        entry = str(PureWindowsPath(_WIN_HOME) / "current" / "Scripts")
        for value in (r"C:\Windows;" + entry, r"C:\Windows;" + entry.upper().replace("\\", "/")):
            steps = _windows_plan(env={"PATH": value})
            assert [step.entry for step in steps if step.kind == "path"] == [entry]

    def test_the_posix_path_entry_is_added_only_when_absent(self) -> None:
        """On POSIX the step is advice (nothing durable is edited), so a listed directory plans none."""
        entry = str(PurePosixPath(_POSIX_HOME) / "current" / "bin")
        steps = _posix_plan(env={"PATH": "/usr/bin:" + entry})
        assert [step for step in steps if step.kind == "path"] == []

    def test_the_posix_path_comparison_is_case_sensitive(self) -> None:
        entry = str(PurePosixPath(_POSIX_HOME) / "current" / "bin")
        env = {"PATH": "/usr/bin:" + entry.upper()}
        assert [step.kind for step in _posix_plan(env=env)].count("path") == 1

    def test_source_redirects_both_the_find_links_and_the_platform_stamp(self) -> None:
        """``--source`` moves both the find-links directory and the platform stamp."""
        source = r"C:\build\wheels"
        steps = _windows_plan("--source", source)
        stamp = [step for step in steps if step.kind == "check-platform"][0]
        assert stamp.path == str(PureWindowsPath(source) / _INSTALL.PLATFORM_FILENAME), (
            "the stamp read is the named directory's, not the archive's"
        )
        pip = [step for step in steps if step.kind == "pip"][0].argv
        assert pip[pip.index("--find-links") + 1] == source
        assert "--no-index" in pip

    def test_an_index_url_replaces_the_source_and_skips_the_platform_check(self) -> None:
        steps = _windows_plan("--index-url", "https://artifactory.example/api/pypi/simple")
        assert [step.kind for step in steps] == ["venv", "pip", "link", "path", "vsix"]
        pip = steps[1].argv
        assert "--no-index" not in pip and "--find-links" not in pip
        assert pip[-3:] == (
            "https://artifactory.example/api/pypi/simple",
            f"swcraft-agent=={_VERSION}",
            f"swcraft-specimens=={_VERSION}",
        )
        assert pip[-4] == "--index-url"


# ---------------------------------------------------------------------------
# The specimen generator, installed by name beside the toolchain
# ---------------------------------------------------------------------------


class TestTheSpecimenGeneratorIsInstalledInTheSamePipCall:
    """``swcraft-specimens==<version>`` goes into the one ``pip install`` beside ``swcraft-agent``.

    One call, so pip resolves SWCraft and the generator together against the one wheelhouse; two calls would let the
    second upgrade or downgrade what the first installed. Named, because no SWCraft distribution depends on the
    generator, and at the release's own version. Not reached: whether the wheelhouse carries the generator's wheel --
    ``pip`` refuses a missing one, and the archive composer's ``require_swcraft_wheels`` refuses to zip a wheelhouse
    without it.
    """

    def test_the_one_pip_call_names_both_requirements_at_the_version(self) -> None:
        for plan, python in (
            (_posix_plan, str(PurePosixPath(_POSIX_HOME) / "versions" / _VERSION / "bin" / "python")),
            (_windows_plan, str(PureWindowsPath(_WIN_HOME) / "versions" / _VERSION / "Scripts" / "python.exe")),
        ):
            steps = plan()
            pips = [step for step in steps if step.kind == "pip"]
            assert len(pips) == 1, "one pip call, so the generator is resolved together with SWCraft"
            argv = pips[0].argv
            assert argv[0] == python
            assert "--no-index" in argv
            assert argv[-2:] == (f"{_INSTALL.REQUIREMENT}=={_VERSION}", f"{_INSTALL.GENERATOR_REQUIREMENT}=={_VERSION}")

    def test_the_generator_requirement_is_the_generators_distribution(self) -> None:
        """The name the installer passes is the one ``packages/specimens/pyproject.toml`` declares."""
        declared = tomllib.loads((REPO_ROOT / "packages" / "specimens" / "pyproject.toml").read_text(encoding="utf-8"))
        assert _INSTALL.GENERATOR_REQUIREMENT == declared["project"]["name"]
        assert _INSTALL.REQUIREMENTS == (_INSTALL.REQUIREMENT, _INSTALL.GENERATOR_REQUIREMENT)

    def test_a_repair_reinstalls_the_generator_too(self) -> None:
        steps = _posix_plan(installed=(_VERSION,), venv_usable=_VENV_STARTS)
        argv = [step for step in steps if step.kind == "pip"][0].argv
        assert "--force-reinstall" in argv and argv[-1] == f"{_INSTALL.GENERATOR_REQUIREMENT}=={_VERSION}"

    def test_an_index_url_installs_the_generator_too(self) -> None:
        steps = _posix_plan("--index-url", "https://example.invalid/simple")
        argv = [step for step in steps if step.kind == "pip"][0].argv
        assert argv[-1] == f"{_INSTALL.GENERATOR_REQUIREMENT}=={_VERSION}"

    def test_main_installs_both_from_a_stamp_with_no_bundled_key(self, tmp_path: Path) -> None:
        """The stamp names the platform alone; the requirements are the installer's own."""
        stamp = {"python_version": "3.11", "platform_tag": "linux-x86_64"}
        archive = _stamped_archive(tmp_path / "archive", stamp)
        journal = Journal()
        code = journal.main(
            ["--home", _posix_home(tmp_path / "SWCraft"), "--no-vsix", "--no-path"],
            env={"PATH": "/usr/bin"},
            platform="posix",
            which=_which({}),
            archive_root=_posix_home(archive),
            version_info=sys.version_info,
        )
        assert code == 0
        pips = [call[1:] for call in journal.calls if call[0] == "run" and "pip" in call]
        assert len(pips) == 1
        assert pips[0][-2:] == (f"{_INSTALL.REQUIREMENT}=={_VERSION}", f"{_INSTALL.GENERATOR_REQUIREMENT}=={_VERSION}")
        assert journal.kinds.count("stamp") == 1, "the check-platform step reads the stamp once"

    def test_the_retired_stamp_readers_are_gone(self) -> None:
        """The ``bundled`` rows, their two readers and the plan check went with the private generator."""
        for retired in ("bundled_requirements", "stamp_bundled_requirements", "check_planned_bundled"):
            assert not hasattr(_INSTALL, retired), retired


def _stamped_archive(root: Path, stamp) -> Path:
    """A small archive whose ``wheels/PLATFORM.json`` holds *stamp*, listed in ``SHA256SUMS``."""
    names = (f"wheels/swcraft_agent-{_VERSION}-py3-none-any.whl", "wheels/PLATFORM.json")
    archive = _small_archive(root, names)
    payload = (json.dumps(stamp, indent=2) + "\n").encode()
    (archive / "wheels" / "PLATFORM.json").write_bytes(payload)
    lines = [f"{hashlib.sha256((archive / name).read_bytes()).hexdigest()}  {name}\n" for name in names]
    (archive / "SHA256SUMS").write_text("".join(lines), encoding="utf-8", newline="\n")
    return archive


# ---------------------------------------------------------------------------
# The interpreter lookup and the ``code`` invocation
# ---------------------------------------------------------------------------


class TestInterpreterLookup:
    """Nothing looks a Python up on the machine any more: the archive carries one."""

    def test_no_candidate_list_is_left_to_search(self) -> None:
        assert not hasattr(_INSTALL, "PYTHON_CANDIDATES")
        assert not hasattr(_INSTALL, "python_spellings")
        # `find_python(requested)` had become `return (requested,)`, so `main` spells the override's argv itself.
        assert not hasattr(_INSTALL, "find_python")

    def test_the_override_names_the_interpreter_verbatim_and_nothing_is_looked_up(self, tmp_path: Path) -> None:
        looked_up: list[str] = []

        def which(name):
            looked_up.append(name)
            return None

        argv = ["--home", _posix_home(tmp_path / "SWCraft"), "--version", _VERSION, "--no-vsix"]
        argv += ["--index-url", "https://example.invalid/simple"]
        for extra, expected in (((), []), (("--python", "/opt/py311/bin/python3.11"), ["/opt/py311/bin/python3.11"])):
            journal = Journal()
            code = journal.main(
                [*argv, *extra],
                env={"PATH": "/usr/bin"},
                platform="posix",
                which=which,
                archive_root=_posix_home(tmp_path / "archive"),
                executable="/opt/running/bin/python3",
            )
            assert code == 0
            # The venv's own `run`: the named interpreter verbatim, or the running one.
            assert journal.calls[0][1] == (expected[0] if expected else "/opt/running/bin/python3"), extra
        assert looked_up == [], "no executable was looked up on PATH for an interpreter"


class TestTheSpawnSitesAreEnumerated:
    """``install.py`` hands an argv to the operating system in exactly four places.

    The ``cmd.exe`` guard lives in :func:`run_argv`. Two probes, :func:`interpreter_stamp` and :func:`venv_is_usable`,
    call ``subprocess.run`` directly and reach no guard: each builds its argv from an interpreter path plus a literal
    ``-c`` program, so ``cmd.exe`` can never be ``argv[0]``. The fourth, :func:`code_version_output`, gets its argv
    from :func:`code_argv`, so ``argv[0]`` is ``cmd.exe`` on Windows; it asks :func:`cmd_reparsed_part` the same
    question ``run_argv`` asks but answers a "no" with ``None``, because it runs before the install and every
    unreadable answer must leave the install path as it was.

    The four are enumerated by enclosing function, read from the file's AST: a fifth site fails this case and its
    author has to decide which kind it is. Not reached: the shape of what any of them spawn -- a probe rewritten to
    spawn ``cmd.exe`` would still pass, which is why the enumeration is by name.
    """

    SPAWN_SITES = {
        "run_argv": "the guarded one: every `run` step's argv, and the `cmd.exe` refusal",
        "interpreter_stamp": "an interpreter path plus a literal `-c` program",
        "venv_is_usable": "an interpreter path plus a literal `-c` program",
        "code_version_output": "`code --version`: guarded like `run_argv`, but every refusal is None",
    }

    @staticmethod
    def _enclosing_functions_of_subprocess_run(source: str) -> list[str]:
        tree = ast.parse(source)
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call):
                    continue
                function = inner.func
                if (
                    isinstance(function, ast.Attribute)
                    and function.attr == "run"
                    and isinstance(function.value, ast.Name)
                    and function.value.id == "subprocess"
                ):
                    found.append(node.name)
        return found

    def test_the_four_and_no_fifth(self) -> None:
        source = _SCRIPT_PATH.read_text(encoding="utf-8")
        assert sorted(self._enclosing_functions_of_subprocess_run(source)) == sorted(self.SPAWN_SITES)

    def test_the_runtime_steps_spawn_nothing(self) -> None:
        """The ``runtime`` and ``node`` steps spawn nothing: unpacking is ``tarfile``/``zipfile``.

        Names the unpack functions, so a reader who makes one shell out to ``tar`` meets the reason before the count.
        """
        source = _SCRIPT_PATH.read_text(encoding="utf-8")
        sites = self._enclosing_functions_of_subprocess_run(source)
        for name in ("unpack_payload", "_unpack_tar", "_unpack_zip", "place_node"):
            assert hasattr(_INSTALL, name), name
            assert name not in sites, name
        assert len(sites) == len(self.SPAWN_SITES)

    def test_the_scan_finds_a_spawn_it_is_not_told_about(self) -> None:
        """Non-vacuity: the same reading over a source with one more site names it."""
        source = _SCRIPT_PATH.read_text(encoding="utf-8") + (
            "\n\ndef _another_spawn_site():\n    return subprocess.run(['x'])\n"
        )
        assert "_another_spawn_site" in self._enclosing_functions_of_subprocess_run(source)

    def test_the_code_probe_is_guarded_the_way_run_argv_is(self) -> None:
        """``code_version_output`` refuses what ``run_argv`` refuses, answering ``None`` and starting nothing.

        ``code_argv`` puts ``cmd.exe`` at ``argv[0]`` for a ``code.cmd``, so this probe is the one site besides
        :func:`run_argv` the guard applies to.
        """
        started: list = []

        def record(*args, **kwargs):  # pragma: no cover - it must not be called
            started.append(args)
            raise AssertionError("the guard must refuse before anything is started")

        argv = _INSTALL.code_argv(_WIN_CODE, ("--version",))[:3] + (r"C:\a&b\code.cmd", "--version")
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(_INSTALL.subprocess, "run", record)
            assert _INSTALL.code_version_output(argv) is None
        assert started == []


class TestCodeInvocation:
    def test_a_cmd_shim_is_run_through_cmd_exe(self) -> None:
        assert _INSTALL.code_argv(_WIN_CODE, ("--install-extension", "x.vsix")) == (
            "cmd.exe",
            "/c",
            "call",
            _WIN_CODE,
            "--install-extension",
            "x.vsix",
        )

    def test_a_bat_shim_too(self) -> None:
        assert _INSTALL.code_argv(r"C:\tools\code.BAT", ("--version",))[:4] == (
            "cmd.exe",
            "/c",
            "call",
            r"C:\tools\code.BAT",
        )

    def test_a_posix_executable_is_invoked_directly(self) -> None:
        assert _INSTALL.code_argv(_POSIX_CODE, ("--version",)) == (_POSIX_CODE, "--version")

    def test_no_argv_part_is_a_shell_string(self) -> None:
        for step in _windows_plan() + _posix_plan():
            for part in step.argv:
                assert not any(operator in part for operator in ("&&", "||", "|", ">", "<")), part

    def test_an_unresolved_code_leaves_the_step_with_the_vsix_and_no_argv(self) -> None:
        steps = _windows_plan(found_code=None)
        vsix = [step for step in steps if step.kind == "vsix"][0]
        assert vsix.argv == ()
        assert vsix.path == str(PureWindowsPath(_WIN_ARCHIVE) / f"swcraft-vscode-{_VERSION}.vsix")


class TestTheCommandLineCmdActuallyReads:
    """What ``cmd.exe`` receives is a joined line, and its first token decides.

    ``subprocess`` joins an argv on Windows with :func:`subprocess.list2cmdline`, and ``cmd /?`` documents two rules
    for what follows ``/c``: quotes survive only when the line carries exactly two of them, none of ``&<>()@^|``
    between them, whitespace between them and an executable name between them -- otherwise, if the first character
    is a quote, ``cmd`` strips that quote and the last quote on the line and parses what is left. Both real
    configurations fail rule 1: a ``.vsix`` under ``C:\\Users\\John Smith\\Downloads`` puts four quotes on the line,
    and ``C:\\Program Files (x86)\\Microsoft VS Code\\bin\\code.cmd`` puts ``(`` and ``)`` between the two; rule 2
    then turns the command into ``C:\\Program``.

    So the assertion is on the joined line: the first character after ``/c `` must not be a quote. ``call`` puts an
    ordinary letter there while leaving the batch file's path quoted. Not reached: Windows -- ``cmd``'s parser is
    read from its documentation, not observed.
    """

    @staticmethod
    def _after_slash_c(argv) -> str:
        line = subprocess.list2cmdline(list(argv))
        assert "/c " in line, line
        return line.split("/c ", 1)[1]

    def _assert_rule_two_cannot_fire(self, argv) -> None:
        remainder = self._after_slash_c(argv)
        assert remainder[0] != '"', (
            f"`cmd /c` strips the leading and the last quote when the line does not meet rule 1, "
            f"and this one begins with one: {remainder}"
        )

    def test_a_cmd_shim_with_a_spacey_vsix_path(self) -> None:
        argv = _INSTALL.code_argv(
            _WIN_CODE,
            ("--install-extension", r"C:\Users\John Smith\Downloads\swcraft\swcraft-vscode-0.7.0.vsix"),
        )
        self._assert_rule_two_cannot_fire(argv)

    def test_a_32_bit_vs_code_shim(self) -> None:
        argv = _INSTALL.code_argv(
            r"C:\Program Files (x86)\Microsoft VS Code\bin\code.cmd",
            ("--install-extension", r"C:\d\x.vsix"),
        )
        self._assert_rule_two_cannot_fire(argv)

    @pytest.mark.parametrize("extra", [(), ("--uninstall",)])
    def test_every_cmd_argv_the_plan_builds(self, extra) -> None:
        planned = _windows_plan(*extra) + _posix_plan(*extra)
        cmd_argvs = [step.argv for step in planned if step.argv and _INSTALL.cmd_reparsed_part(step.argv) is None]
        cmd_argvs = [argv for argv in cmd_argvs if os.path.basename(str(argv[0])).lower() == "cmd.exe"]
        assert cmd_argvs, "the Windows plan builds no cmd.exe argv -- this oracle would assert nothing"
        for argv in cmd_argvs:
            self._assert_rule_two_cannot_fire(argv)

    def test_the_junction_argv_too(self) -> None:
        # `_create_windows_junction`'s second spelling, built at run time: its first token is a `cmd` command,
        # so rule 2 cannot fire -- the case catches a rewrite that quotes it.
        self._assert_rule_two_cannot_fire(
            ["cmd.exe", "/c", "mklink", "/J", r"C:\Users\dev\AppData\Local\SWCraft\current", _WIN_HOME]
        )


# ---------------------------------------------------------------------------
# The refusals that read the archive
# ---------------------------------------------------------------------------


class TestPlatformStamp:
    def test_a_matching_stamp_passes(self) -> None:
        stamp = {"python_version": "3.11", "platform_tag": "win-amd64"}
        assert _INSTALL.check_platform_stamp(dict(stamp), dict(stamp)) is None

    def test_a_foreign_platform_names_both_tags(self) -> None:
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.check_platform_stamp(
                {"python_version": "3.11", "platform_tag": "macosx-11.0-arm64"},
                {"python_version": "3.11", "platform_tag": "win-amd64"},
            )
        message = str(excinfo.value)
        assert "macosx-11.0-arm64" in message and "win-amd64" in message

    def test_a_foreign_python_names_both_versions(self) -> None:
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.check_platform_stamp(
                {"python_version": "3.12", "platform_tag": "win-amd64"},
                {"python_version": "3.11", "platform_tag": "win-amd64"},
            )
        message = str(excinfo.value)
        assert "3.12" in message and "3.11" in message

    def test_two_macos_deployment_targets_are_one_platform(self) -> None:
        """macOS stamps that differ only in the deployment target are one platform.

        ``sysconfig.get_platform()``'s middle field on macOS is the interpreter's build-time deployment target, not a
        property of the host: a ``.venv`` saying ``macosx-11.0-arm64`` and a Homebrew 3.11 saying
        ``macosx-26.0-arm64`` install each other's wheels. Compared for identity, an archive refused the machine it was
        built on.
        """
        assert (
            _INSTALL.check_platform_stamp(
                {"python_version": "3.11", "platform_tag": "macosx-11.0-arm64"},
                {"python_version": "3.11", "platform_tag": "macosx-26.0-arm64"},
            )
            is None
        )

    def test_another_architecture_is_still_refused(self) -> None:
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.check_platform_stamp(
                {"python_version": "3.11", "platform_tag": "macosx-11.0-arm64"},
                {"python_version": "3.11", "platform_tag": "macosx-11.0-x86_64"},
            )
        assert "x86_64" in str(excinfo.value)

    def test_an_unknown_tag_is_still_compared_whole(self) -> None:
        """Nothing here decomposes a tag it does not know, so it compares it entire."""
        assert _INSTALL.platform_compatibility("linux-aarch64") == ("linux-aarch64",)
        with pytest.raises(_INSTALL.InstallError):
            _INSTALL.check_platform_stamp(
                {"python_version": "3.11", "platform_tag": "linux-aarch64"},
                {"python_version": "3.11", "platform_tag": "linux-x86_64"},
            )

    def test_a_missing_tag_is_not_a_traceback(self) -> None:
        with pytest.raises(_INSTALL.InstallError):
            _INSTALL.check_platform_stamp({}, {"python_version": "3.11", "platform_tag": "win-amd64"})


class TestChecksums:
    _WHEEL = "wheels/swcraft_agent-0.7.0-py3-none-any.whl"
    _SUMS = f"{'0' * 63}1  install.py\n{'0' * 63}2  {_WHEEL}\n"

    def test_the_coreutils_format_is_parsed(self) -> None:
        assert _INSTALL.parse_sha256sums(self._SUMS) == (
            ("0" * 63 + "1", "install.py"),
            ("0" * 63 + "2", self._WHEEL),
        )

    def test_a_malformed_line_is_refused_by_number(self) -> None:
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.parse_sha256sums("not a checksum line\n")
        assert "line 1" in str(excinfo.value)

    def test_a_member_name_that_leaves_the_archive_is_refused(self) -> None:
        """A ``SHA256SUMS`` name that leaves the archive is refused by name.

        ``member_digest`` only reads, so the impact would be a read beside the archive, not an overwrite; it is refused
        anyway, because "it only reads" is a property of today's callers and not of the format.
        """
        for name in ("../outside.txt", "/etc/passwd", r"..\outside.txt", r"C:\Windows\x"):
            with pytest.raises(_INSTALL.InstallError) as excinfo:
                _INSTALL.parse_sha256sums(f"{'0' * 64}  {name}\n")
            assert name in str(excinfo.value)

    def test_a_missing_member_is_named(self) -> None:
        entries = _INSTALL.parse_sha256sums(self._SUMS)
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.verify_members(entries, lambda name: None if name == "install.py" else "0" * 63 + "2")
        assert "install.py" in str(excinfo.value)

    def test_a_mismatched_member_is_named(self) -> None:
        entries = _INSTALL.parse_sha256sums(self._SUMS)
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.verify_members(entries, lambda name: "f" * 64)
        assert "install.py" in str(excinfo.value)

    def test_an_intact_archive_passes(self) -> None:
        entries = _INSTALL.parse_sha256sums(self._SUMS)
        digests = dict((name, digest) for digest, name in entries)
        assert _INSTALL.verify_members(entries, digests.get) is None


class TestArchiveVersion:
    def test_the_version_comes_from_the_members(self) -> None:
        names = (
            "install.py",
            "swcraft_install.cmd",
            "wheels/swcraft_agent-0.7.0-py3-none-any.whl",
            "wheels/swcraft_tools-0.7.0-py3-none-any.whl",
            "swcraft-vscode-0.7.0.vsix",
        )
        assert _INSTALL.archive_version(names) == "0.7.0"

    def test_a_companion_file_beside_the_vsix_is_not_a_second_version(self) -> None:
        """A checksum or signature file beside the ``.vsix`` does not slice a second version out of its name.

        Guards the conjunction ``startsWith(VSIX_PREFIX) and endswith(VSIX_SUFFIX)``: mutated to ``or``, a file a
        mirror or a signing step adds is read as a second version and a working archive is refused.
        """
        names = ("install.py", "swcraft-vscode-0.7.0.vsix", "swcraft-vscode-0.7.0.vsix.sha256")

        assert _INSTALL.archive_version(names) == "0.7.0"

    def test_the_runtime_members_carry_no_version(self) -> None:
        """The runtime members are neither a ``.whl`` nor a ``swcraft-vscode-*.vsix``, so they are ignored."""
        for members in (_LINUX_MEMBERS, _MACOS_MEMBERS, _WINDOWS_MEMBERS):
            assert _INSTALL.archive_version(members) == _VERSION

    def test_two_versions_are_refused(self) -> None:
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.archive_version(("wheels/swcraft_agent-0.7.0-py3-none-any.whl", "swcraft-vscode-0.6.0.vsix"))
        assert "0.7.0" in str(excinfo.value) and "0.6.0" in str(excinfo.value)

    def test_no_version_at_all_is_refused(self) -> None:
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.archive_version(("install.py", "SHA256SUMS"))
        assert "--version" in str(excinfo.value)

    def test_a_plan_without_a_version_is_refused(self) -> None:
        args = _INSTALL.parse_args([], archive_root=_WIN_ARCHIVE)
        with pytest.raises(_INSTALL.InstallError):
            _INSTALL.plan_install(args, {"PATH": ""}, "nt", _WIN_PYTHON)


# ---------------------------------------------------------------------------
# The two wrappers
# ---------------------------------------------------------------------------


class TestWrappers:
    """Four acts each: check the carried Python, unpack it, run ``install.py`` on it, pass everything through.

    ``swcraft_install.cmd`` is read as text here, run for its refusals by the Windows CI runner's fast gate
    (:meth:`test_the_cmd_wrapper_refuses_in_one_sentence`) and for its success path by the ``artifact`` stage.
    ``swcraft_install.sh`` is read as text and run against a small archive on POSIX hosts. Not reached: the open
    ``preparing the installer ... `` line rendered live in an interactive console, and the ``.cmd`` double-click /
    ``pause`` branch.
    """

    #: The wrappers' own unnumbered line, as it reads once closed.
    _PREPARED = "preparing the installer ... done"
    _NOT_PREPARED = "preparing the installer ... failed"

    #: A version literal -- ``3.11``, ``22.23.2`` -- anywhere in a wrapper.
    _VERSION_LITERAL = re.compile(r"\d+\.\d+")

    @staticmethod
    def _code(wrapper: Path) -> list[str]:
        return [
            line.strip()
            for line in wrapper.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().lower().startswith("rem ") and not line.strip().startswith("#")
        ]

    def test_the_cmd_wrapper_forwards_everything(self) -> None:
        text = _CMD_WRAPPER.read_text(encoding="utf-8")
        assert "install.py" in text
        assert "%*" in text, "the wrapper passes its arguments through unchanged"

    def test_the_cmd_wrapper_performs_the_four_acts_in_order(self) -> None:
        """Hash, find that digest's line in ``SHA256SUMS``, unpack with Windows' own ``tar.exe``, run.

        ``cmd.exe`` has no parser for a sums line without a ``for`` block, so the file is searched for the digest this
        machine computed, followed by the member name.
        """
        code = self._code(_CMD_WRAPPER)
        hashed = next(index for index, line in enumerate(code) if line.startswith("certutil -hashfile"))
        searched = next(index for index, line in enumerate(code) if line.startswith("findstr /b /i /c:"))
        unpacked = next(index for index, line in enumerate(code) if "tar.exe" in line)
        ran = next(index for index, line in enumerate(code) if '"%~dp0install.py" %*' in line)
        assert hashed < searched < unpacked < ran
        assert '"%~dp0SHA256SUMS"' in code[searched] and "runtime/python.tar.gz" in code[searched]
        assert code[unpacked].startswith('if not defined SWCRAFT_REFUSAL "%SystemRoot%\\System32\\tar.exe" -xzf')
        assert code[unpacked].endswith("--strip-components 1")
        refusal = [line for line in code if line.startswith('if errorlevel 1 set "SWCRAFT_REFUSAL=')]
        assert len(refusal) == 1 and "does not match its line in SHA256SUMS" in refusal[0]

    def test_the_cmd_wrapper_removes_the_bootstrap_copy_after_the_installer_returns(self) -> None:
        """``install.py`` cannot remove ``.bootstrap`` on Windows, so the ``.cmd`` wrapper does, after it returns.

        Windows holds ``.bootstrap\\python\\python.exe`` open while it runs, so the removal follows the save of the
        installer's exit code (the code the wrapper still exits with), and only when the wrapper refused nothing. A
        static assertion on the text: whether ``rmdir /s /q`` succeeds is the Windows runner's artifact stage.
        """
        code = self._code(_CMD_WRAPPER)
        run = code.index('if not defined SWCRAFT_REFUSAL "%SWCRAFT_PY%" "%~dp0install.py" %*')
        removal = 'if not defined SWCRAFT_REFUSAL rmdir /s /q "%SWCRAFT_BOOT%" >nul 2>&1'
        assert code.count(removal) == 1
        assert code[run + 1] == 'set "SWCRAFT_EXIT=%errorlevel%"'
        assert code.index(removal) > run + 1, "after the installer returned and its code was saved"
        assert code.index(removal) < code.index("exit /b %SWCRAFT_EXIT%")
        assert [line for line in code if "rmdir /s" in line] == [
            'if not defined SWCRAFT_REFUSAL if exist "%SWCRAFT_BOOT%\\python" rmdir /s /q "%SWCRAFT_BOOT%\\python"',
            removal,
        ], "the only recursive removals: the stale copy before the unpack, and the bootstrap after the run"

    def test_the_cmd_wrapper_uses_no_labels(self) -> None:
        code = [
            line
            for line in _CMD_WRAPPER.read_text(encoding="utf-8").splitlines()
            if not line.strip().lower().startswith("rem ")
        ]
        assert [line for line in code if line.strip().startswith(":")] == [], (
            "the working tree is LF-only (.gitattributes), and a batch label reached by goto is the "
            "documented failure mode of an LF-only .cmd -- this wrapper is written without one"
        )
        assert [line for line in code if "goto" in line.lower()] == []

    def test_the_cmd_wrapper_expands_no_errorlevel_inside_a_block(self) -> None:
        """No ``%errorlevel%`` is expanded inside a parenthesised block.

        ``cmd.exe`` expands it when it parses the block, before the command inside has run, so ``exit /b
        %errorlevel%`` there answered the preceding probe's 0 and every refusal read as success. With no block at all
        the wrapper also needs no delayed expansion, which would mangle a ``!`` in an argument.
        """
        code = [
            line
            for line in _CMD_WRAPPER.read_text(encoding="utf-8").splitlines()
            if not line.strip().lower().startswith("rem ")
        ]
        assert [line for line in code if "(" in line] == [], (
            "a parenthesised block is where %errorlevel% expands too early"
        )
        exits = [line.strip() for line in code if line.strip().lower().startswith("exit /b")]
        assert exits == ["exit /b %SWCRAFT_EXIT%"], (
            "one exit, carrying the installer's own code saved on the line after the run"
        )

    def test_the_cmd_wrapper_saves_the_code_before_anything_else_runs(self) -> None:
        code = [line.strip() for line in _CMD_WRAPPER.read_text(encoding="utf-8").splitlines()]
        run = code.index('if not defined SWCRAFT_REFUSAL "%SWCRAFT_PY%" "%~dp0install.py" %*')
        assert code[run + 1] == 'set "SWCRAFT_EXIT=%errorlevel%"', (
            "every later command overwrites %errorlevel%, the `pause` probe included"
        )

    def test_the_cmd_wrapper_pauses_on_three_conditions_and_not_on_one(self) -> None:
        """The ``.cmd`` wrapper pauses only on a double-click: three conditions, decided on one line.

        A double-clicked console closes the instant the wrapper ends, when a refusal needs to be read. But
        ``%cmdcmdline%`` names this file for any ``cmd.exe /c <wrapper>`` launch -- PowerShell, VS Code's default
        Windows shell and a build agent run a ``.cmd`` that way -- and with an inherited console ``pause`` blocks
        forever. So it pauses when the command line names this file, no argument was passed (an automated caller
        passes flags), and ``SWCRAFT_NO_PAUSE`` is unset (the opt-out for a caller that passes none); one line, so a
        single ``errorlevel`` reading governs them.

        Not reached: an interactive PowerShell run with no arguments is indistinguishable from a double-click, so it
        waits for a key (a key press, not a hang; ``SWCRAFT_NO_PAUSE`` answers it). The CI runner runs the file only
        with ``SWCRAFT_NO_PAUSE`` set, so this branch is unobserved.
        """
        lines = [line.strip() for line in _CMD_WRAPPER.read_text(encoding="utf-8").splitlines()]
        code = [line for line in lines if not line.lower().startswith("rem ")]

        assert code.count("pause") == 0, "`pause` is never a statement of its own here"
        guarded = [line for line in code if line.endswith("pause")]
        assert len(guarded) == 1, "exactly one `pause`, and it is guarded"
        assert guarded[0] == "if defined SWCRAFT_PAUSE pause", (
            "the pause reads a flag set on one line above it, so the three conditions are decided "
            "before %errorlevel% can be overwritten again"
        )

        deciders = [line for line in code if line.endswith('set "SWCRAFT_PAUSE=1"')]
        assert len(deciders) == 1, "one line decides it"
        decider = deciders[0]
        assert "if not errorlevel 1 " in decider, "the `find` result is the first condition"
        assert 'if "%~1"=="" ' in decider, (
            "an argument means an automated caller -- the documented build-agent flow is "
            "`cmd /c swcraft_install.cmd --no-vsix`, and a `pause` there hangs the provisioning step"
        )
        assert "if not defined SWCRAFT_NO_PAUSE " in decider, (
            "the opt-out for a non-interactive caller that passes no flags either"
        )

        probe = code.index('echo %cmdcmdline% | find /i "%~0" >nul')
        assert code[probe + 1] == decider, "nothing runs between the probe and the reading of it"

    def test_the_cmd_wrapper_opens_its_line_where_no_errorlevel_reads_it(self) -> None:
        """``set /p`` with no input sets ``errorlevel`` to 1, so the open line stands before ``certutil``.

        A static assertion: the next ``errorlevel`` read after the open line is the one after ``findstr``, and no
        ``set /p`` of the wrapper's line sits between a command and its reader.
        """
        code = self._code(_CMD_WRAPPER)
        opened = code.index('<nul set /p "=preparing the installer ... "')
        assert code[opened + 1].startswith("certutil -hashfile"), "directly before the hash"
        assert code.count('<nul set /p "=preparing the installer ... "') == 1
        reads = [index for index, line in enumerate(code) if "errorlevel" in line.lower() and index > opened]
        assert reads, "the wrapper reads errorlevel after its line"
        first = reads[0]
        assert code[first].startswith("if errorlevel 1 ")
        assert code[first - 1].startswith("findstr /b /i /c:"), "the first read after the open line is findstr's"
        for index in reads:
            assert "set /p" not in code[index - 1], f"an errorlevel read follows a `set /p`: {code[index]}"

    def test_the_cmd_wrapper_closes_its_line_in_the_six_line_sequence(self) -> None:
        """The tail, by index: the unpack's check, ``failed``, the refusal, ``done``, the run, the save."""
        code = self._code(_CMD_WRAPPER)
        check = next(
            index
            for index, line in enumerate(code)
            if line.startswith('if not defined SWCRAFT_REFUSAL if errorlevel 1 set "SWCRAFT_REFUSAL=')
        )
        assert code[check : check + 6] == [
            code[check],
            "if defined SWCRAFT_REFUSAL echo failed",
            "if defined SWCRAFT_REFUSAL echo %SWCRAFT_REFUSAL% 1>&2",
            "if not defined SWCRAFT_REFUSAL echo done",
            'if not defined SWCRAFT_REFUSAL "%SWCRAFT_PY%" "%~dp0install.py" %*',
            'set "SWCRAFT_EXIT=%errorlevel%"',
        ]
        assert [line for line in code if line.endswith("echo failed")] == ["if defined SWCRAFT_REFUSAL echo failed"]
        assert [line for line in code if line.endswith("echo done")] == ["if not defined SWCRAFT_REFUSAL echo done"]

    @staticmethod
    def _archive_for_the_shell_wrapper(root: Path, *, listed: bool = True, damaged: bool = False) -> Path:
        """An unpacked archive holding the real wrapper and a Python that echoes what it was asked to run."""
        root.mkdir(parents=True)
        shutil.copyfile(_SH_WRAPPER, root / "swcraft_install.sh")
        (root / "install.py").write_text("print('never run by this stand-in interpreter')\n", encoding="utf-8")
        interpreter = b'#!/bin/sh\nprintf "%s\\n" "$@"\n'
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
            for name in ("python", "python/bin"):
                directory = tarfile.TarInfo(name)
                directory.type, directory.mode = tarfile.DIRTYPE, 0o755
                bundle.addfile(directory)
            member = tarfile.TarInfo("python/bin/python3")
            member.size, member.mode = len(interpreter), 0o755
            bundle.addfile(member, io.BytesIO(interpreter))
        payload = buffer.getvalue()
        (root / "runtime").mkdir()
        (root / "runtime" / "python.tar.gz").write_bytes(payload)
        digest = hashlib.sha256(b"not the payload" if damaged else payload).hexdigest()
        lines = [f"{digest}  runtime/python.tar.gz\n"] if listed else []
        lines.append(f"{hashlib.sha256(b'x').hexdigest()}  install.py\n")
        (root / "SHA256SUMS").write_text("".join(lines), encoding="utf-8", newline="\n")
        return root

    @staticmethod
    def _run_the_shell_wrapper(root: Path, *arguments: str) -> subprocess.CompletedProcess:
        if os.name == "nt" or shutil.which("tar") is None or not Path("/bin/sh").is_file():
            pytest.skip("the shell wrapper runs on a POSIX host with /bin/sh and tar")
        return subprocess.run(
            ["/bin/sh", str(root / "swcraft_install.sh"), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_the_sh_wrapper_verifies_unpacks_and_runs_install_py_on_the_carried_python(self, tmp_path: Path) -> None:
        root = self._archive_for_the_shell_wrapper(tmp_path / "archive")

        result = self._run_the_shell_wrapper(root, "--no-vsix", "--home", "/a home/with spaces")

        assert result.returncode == 0, result.stderr
        printed = [line for line in result.stdout.splitlines() if line != self._PREPARED]
        assert Path(printed[0]).resolve() == (root / "install.py").resolve()
        assert printed[1:] == ["--no-vsix", "--home", "/a home/with spaces"], "every argument, unsplit"
        assert (root / ".bootstrap" / "python" / "bin" / "python3").is_file()
        assert not (root / ".bootstrap" / "python" / "python").exists(), "the vendor's leading directory is stripped"

    @pytest.mark.parametrize("shape", ["damaged", "unlisted"])
    def test_the_sh_wrapper_refuses_a_python_it_cannot_verify_in_one_sentence(self, tmp_path: Path, shape: str) -> None:
        root = self._archive_for_the_shell_wrapper(
            tmp_path / "archive", damaged=shape == "damaged", listed=shape != "unlisted"
        )

        result = self._run_the_shell_wrapper(root)

        assert result.returncode == 1
        assert result.stderr.strip().count("\n") == 0, "one sentence"
        assert "does not match its line in SHA256SUMS" in result.stderr
        assert result.stdout == self._NOT_PREPARED + "\n" and not (root / ".bootstrap").exists(), (
            "nothing is unpacked or run"
        )

    def test_the_sh_wrapper_prints_its_own_line_before_install_py_runs(self, tmp_path: Path) -> None:
        """The wrapper's line is closed with ``done`` before the stand-in interpreter prints."""
        root = self._archive_for_the_shell_wrapper(tmp_path / "archive")

        result = self._run_the_shell_wrapper(root, "--no-vsix")

        assert result.returncode == 0, result.stderr
        printed = result.stdout.splitlines()
        assert printed[0] == self._PREPARED
        assert Path(printed[1]).resolve() == (root / "install.py").resolve()
        assert result.stdout.count("preparing the installer") == 1

    def test_the_sh_wrapper_closes_its_line_before_the_refusal_which_stays_last(self, tmp_path: Path) -> None:
        """``failed`` on stdout, then the one sentence on stderr -- read as one stream, in order."""
        root = self._archive_for_the_shell_wrapper(tmp_path / "archive", damaged=True)
        if os.name == "nt" or shutil.which("tar") is None or not Path("/bin/sh").is_file():
            pytest.skip("the shell wrapper runs on a POSIX host with /bin/sh and tar")

        result = subprocess.run(
            ["/bin/sh", str(root / "swcraft_install.sh")],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )

        assert result.returncode == 1
        lines = result.stdout.splitlines()
        assert len(lines) == 2, lines
        assert lines[0] == self._NOT_PREPARED
        assert "does not match its line in SHA256SUMS" in lines[-1], "the refusal is the last line"

    # -- the shell wrapper's other paths --------------------------------------

    @staticmethod
    def _path_holding(directory: Path, *tools: str) -> str:
        """A ``PATH`` of one directory linking exactly *tools*, so a case decides which programs exist."""
        directory.mkdir()
        for tool in tools:
            found = shutil.which(tool)
            if found is None:
                pytest.skip(f"this host has no {tool} to put on the restricted PATH")
            os.symlink(found, directory / tool)
        return str(directory)

    # `gzip` is on the list because GNU tar runs it as a child for `-z`; macOS's bsdtar decompresses in-process.
    _BASE_TOOLS = ("awk", "dirname", "rm", "mkdir", "tar", "gzip")

    def _run_with_path(self, root: Path, path: str, *arguments: str) -> subprocess.CompletedProcess:
        if os.name == "nt" or not Path("/bin/sh").is_file():
            pytest.skip("the shell wrapper runs on a POSIX host with /bin/sh")
        return subprocess.run(
            ["/bin/sh", str(root / "swcraft_install.sh"), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={"PATH": path},
        )

    def test_the_sh_wrapper_falls_back_to_sha256sum(self, tmp_path: Path) -> None:
        root = self._archive_for_the_shell_wrapper(tmp_path / "archive")
        path = self._path_holding(tmp_path / "bin", *self._BASE_TOOLS, "sha256sum")
        result = self._run_with_path(root, path, "--no-vsix")
        assert result.returncode == 0, result.stderr
        assert [line for line in result.stdout.splitlines() if line != self._PREPARED][1:] == ["--no-vsix"]

    def test_the_sh_wrapper_with_no_hashing_tool_says_so_rather_than_blaming_the_download(self, tmp_path: Path) -> None:
        root = self._archive_for_the_shell_wrapper(tmp_path / "archive")
        path = self._path_holding(tmp_path / "bin", *self._BASE_TOOLS)
        result = self._run_with_path(root, path)
        assert result.returncode == 1
        assert result.stderr.strip().count("\n") == 0, "one sentence"
        assert "neither shasum nor sha256sum" in result.stderr
        assert "download the archive again" not in result.stderr
        assert result.stdout == self._NOT_PREPARED + "\n" and not (root / ".bootstrap").exists()

    @staticmethod
    def _quarantine_stub(directory: Path, marked: str | None) -> None:
        """An ``xattr`` that answers ``-p com.apple.quarantine`` for the file named *marked* alone."""
        script = directory / "xattr"
        names = "" if marked is None else marked
        script.write_text(
            "#!/bin/sh\n"
            '[ "$1" = -p ] && [ "$2" = com.apple.quarantine ] || exit 2\n'
            f'case "$3" in */{names}) [ -n "{names}" ] && {{ echo 0081; exit 0; }} ;; esac\n'
            "exit 1\n",
            encoding="utf-8",
            newline="\n",
        )
        script.chmod(0o755)

    @pytest.mark.parametrize("marked", ["swcraft_install.sh", "python.tar.gz"])
    def test_the_sh_wrapper_refuses_a_quarantined_folder_naming_the_command(self, tmp_path: Path, marked: str) -> None:
        """macOS kills the unsigned Python of a quarantined download silently (exit 137): refuse first, by name."""
        root = self._archive_for_the_shell_wrapper(tmp_path / "a folder")
        path = self._path_holding(tmp_path / "bin", *self._BASE_TOOLS, "shasum")
        self._quarantine_stub(tmp_path / "bin", marked)
        result = self._run_with_path(root, path)
        assert result.returncode == 1
        assert result.stderr.strip().count("\n") == 0, "one sentence"
        assert "download quarantine" in result.stderr
        assert f'xattr -dr com.apple.quarantine "{root}"' in result.stderr
        assert result.stdout == self._NOT_PREPARED + "\n" and not (root / ".bootstrap").exists(), "nothing is run"

    def test_the_sh_wrapper_runs_when_xattr_finds_no_quarantine(self, tmp_path: Path) -> None:
        root = self._archive_for_the_shell_wrapper(tmp_path / "archive")
        path = self._path_holding(tmp_path / "bin", *self._BASE_TOOLS, "shasum")
        self._quarantine_stub(tmp_path / "bin", None)
        result = self._run_with_path(root, path, "--no-vsix")
        assert result.returncode == 0, result.stderr
        assert [line for line in result.stdout.splitlines() if line != self._PREPARED][1:] == ["--no-vsix"]

    def test_the_sh_wrapper_refuses_a_real_quarantine_flag(self, tmp_path: Path) -> None:
        """Where the host's own ``xattr`` can set the flag (macOS), the real attribute is refused too."""
        root = self._archive_for_the_shell_wrapper(tmp_path / "archive")
        xattr = shutil.which("xattr")
        if xattr is None:
            pytest.skip("this host has no xattr command")
        flagged = subprocess.run(
            [xattr, "-w", "com.apple.quarantine", "0081;00000000;Browser;", str(root / "runtime" / "python.tar.gz")],
            capture_output=True,
            check=False,
        )
        if flagged.returncode != 0:
            pytest.skip("this host's xattr cannot set com.apple.quarantine")
        result = self._run_the_shell_wrapper(root)
        assert result.returncode == 1
        assert "xattr -dr com.apple.quarantine" in result.stderr

    def test_the_sh_wrapper_refuses_a_verified_python_it_cannot_unpack(self, tmp_path: Path) -> None:
        root = self._archive_for_the_shell_wrapper(tmp_path / "archive")
        payload = b"matches its line and is not a tar"
        (root / "runtime" / "python.tar.gz").write_bytes(payload)
        (root / "SHA256SUMS").write_text(
            f"{hashlib.sha256(payload).hexdigest()}  runtime/python.tar.gz\n", encoding="utf-8", newline="\n"
        )
        result = self._run_the_shell_wrapper(root)
        assert result.returncode == 1
        assert "could not be unpacked into .bootstrap/python" in result.stderr
        assert result.stdout == self._NOT_PREPARED + "\n", "install.py is never run"

    def test_the_sh_wrapper_closes_its_line_when_bootstrap_cannot_be_made(self, tmp_path: Path) -> None:
        """A ``.bootstrap`` that is a file: ``mkdir -p`` fails under ``set -e``, and the line still closes."""
        root = self._archive_for_the_shell_wrapper(tmp_path / "archive")
        (root / ".bootstrap").write_text("a file, not a directory\n", encoding="utf-8", newline="\n")
        result = self._run_the_shell_wrapper(root)
        assert result.returncode == 1
        assert result.stdout == self._NOT_PREPARED + "\n", "install.py is never run"
        assert result.stderr.strip().splitlines()[-1].startswith(".bootstrap/python could not be made")

    def test_the_sh_wrapper_refuses_an_archive_with_no_sums_file(self, tmp_path: Path) -> None:
        root = self._archive_for_the_shell_wrapper(tmp_path / "archive")
        (root / "SHA256SUMS").unlink()
        result = self._run_the_shell_wrapper(root)
        assert result.returncode == 1
        assert "does not match its line in SHA256SUMS" in result.stderr
        assert result.stdout == self._NOT_PREPARED + "\n" and not (root / ".bootstrap").exists()

    # -- the batch wrapper's refusals, run where cmd.exe is ------------------

    @pytest.mark.skipif(os.name != "nt", reason="the batch wrapper runs under cmd.exe, on Windows only")
    @pytest.mark.parametrize("shape", ["damaged", "unlisted", "not-a-tar"])
    def test_the_cmd_wrapper_refuses_in_one_sentence(self, tmp_path: Path, shape: str) -> None:
        """Nothing here needs an interpreter: every refusal comes before one would be started.

        The digest refusals also assert that no ``.bootstrap`` is left beside the wrapper (the digest file it writes is
        removed as soon as it is read).
        """
        root = tmp_path / "archive"
        (root / "runtime").mkdir(parents=True)
        shutil.copyfile(_CMD_WRAPPER, root / "swcraft_install.cmd")
        (root / "install.py").write_text("print('never run')\n", encoding="utf-8", newline="\n")
        payload = b"not a tar at all"
        (root / "runtime" / "python.tar.gz").write_bytes(payload)
        digest = hashlib.sha256(payload if shape == "not-a-tar" else b"another file").hexdigest()
        lines = [] if shape == "unlisted" else [f"{digest}  runtime/python.tar.gz\n"]
        lines.append(f"{hashlib.sha256(b'x').hexdigest()}  install.py\n")
        (root / "SHA256SUMS").write_text("".join(lines), encoding="utf-8", newline="\n")
        result = subprocess.run(
            ["cmd.exe", "/c", str(root / "swcraft_install.cmd"), "--no-vsix"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=dict(os.environ, SWCRAFT_NO_PAUSE="1"),
        )
        assert result.returncode == 1, result.stdout + result.stderr
        if shape == "not-a-tar":
            assert "could not be unpacked into .bootstrap" in result.stderr
        else:
            assert "does not match its line in SHA256SUMS" in result.stderr
            assert not (root / ".bootstrap").exists()
        assert "never run" not in result.stdout
        assert result.stdout.strip() == self._NOT_PREPARED, "the wrapper's own line, closed before the refusal"

    def test_both_wrappers_are_committed_beside_the_installer(self) -> None:
        assert _SCRIPT_PATH.is_file() and _CMD_WRAPPER.is_file() and _SH_WRAPPER.is_file()


# ---------------------------------------------------------------------------
# The two values the executing layer computes, and nothing else
# ---------------------------------------------------------------------------


class TestPathValue:
    """``PATH`` is appended to, never rebuilt.

    The durable Windows value routinely holds ``%SystemRoot%`` and friends; the installer neither expands them nor
    reorders what is there: it adds one separator and one entry and hands the string back with the registry type it
    came with. These two pure functions are the whole of that arithmetic.
    """

    def test_the_entry_is_appended_to_the_value_as_read(self) -> None:
        value = r"%SystemRoot%\system32;C:\tools"
        entry = r"C:\Users\dev\AppData\Local\SWCraft\current\Scripts"
        assert _INSTALL.appended_path(value, entry, "nt") == value + ";" + entry

    def test_an_entry_already_present_appends_nothing(self) -> None:
        entry = r"C:\Users\dev\AppData\Local\SWCraft\current\Scripts"
        assert _INSTALL.appended_path(r"C:\tools;" + entry, entry, "nt") is None

    def test_the_windows_comparison_ignores_case_and_separator(self) -> None:
        entry = r"C:\SWCraft\current\Scripts"
        assert _INSTALL.appended_path("C:/swcraft/CURRENT/scripts", entry, "nt") is None

    def test_an_empty_value_becomes_the_entry_alone(self) -> None:
        assert _INSTALL.appended_path("", r"C:\SWCraft\current\Scripts", "nt") == r"C:\SWCraft\current\Scripts"

    def test_removal_keeps_every_other_part_verbatim(self) -> None:
        entry = r"C:\SWCraft\current\Scripts"
        value = r"%SystemRoot%\system32;" + entry + r";C:\tools"
        assert _INSTALL.removed_path(value, entry, "nt") == r"%SystemRoot%\system32;C:\tools"

    def test_removing_an_absent_entry_changes_nothing(self) -> None:
        assert _INSTALL.removed_path(r"C:\tools", r"C:\SWCraft\current\Scripts", "nt") is None

    def test_the_posix_separator_is_the_colon(self) -> None:
        assert _INSTALL.appended_path("/usr/bin", "/home/dev/.swcraft/current/bin", "posix") == (
            "/usr/bin:/home/dev/.swcraft/current/bin"
        )


class TestBroadcast:
    """``WM_SETTINGCHANGE``, with a timeout and ``SMTO_ABORTIFHUNG``.

    A plain ``SendMessage`` blocks while any top-level window on the desktop is hung, so the installer sends with a
    timeout and the abort flag; the send is a seam, so the argument list is assertable without ``user32``. Not
    reached: whether Windows delivers the message, whether Explorer re-reads the environment, and whether an
    already-open terminal picks it up (it does not -- hence the advice to open a new terminal).
    """

    def test_the_send_receives_the_documented_arguments(self) -> None:
        sent: list[tuple] = []
        _INSTALL.broadcast_environment_change(lambda *arguments: sent.append(arguments))
        assert sent == [
            (
                _INSTALL.HWND_BROADCAST,
                _INSTALL.WM_SETTINGCHANGE,
                0,
                _INSTALL.ENVIRONMENT_SUBKEY,
                _INSTALL.SMTO_ABORTIFHUNG,
                _INSTALL.BROADCAST_TIMEOUT_MS,
            )
        ]

    def test_the_constants_are_the_documented_values(self) -> None:
        assert (_INSTALL.HWND_BROADCAST, _INSTALL.WM_SETTINGCHANGE, _INSTALL.SMTO_ABORTIFHUNG) == (
            0xFFFF,
            0x001A,
            0x0002,
        )
        assert 0 < _INSTALL.BROADCAST_TIMEOUT_MS <= 10_000, "a broadcast that can hang the installer is the bug"

    def test_the_real_sender_is_send_message_timeout_w_and_is_platform_guarded(self) -> None:
        """A source assertion: the default sender names the Unicode timeout form and runs only on ``win32``.

        It cannot be called here (it resolves ``ctypes.windll``); the check keeps the fallback from silently becoming
        ``SendMessage``.
        """
        source = inspect.getsource(_INSTALL.send_message_timeout)
        assert "SendMessageTimeoutW" in source
        assert "sys.platform" in source and "win32" in source


# ---------------------------------------------------------------------------
# The seams, and the order in which they are called
# ---------------------------------------------------------------------------


class Journal:
    """Every seam call in one ordered list, and the registry object as well.

    One recorder rather than five spies: "each step kind maps to exactly one call" is a statement about a sequence,
    which separate call lists cannot express without inventing an ordering. The registry seam is this same object, so
    its read, write and broadcast land in the same list as the runs and the link.
    """

    #: ``winreg.REG_EXPAND_SZ``, named here because ``winreg`` does not import on this platform.
    REG_EXPAND_SZ = 2
    REG_SZ = 1

    def __init__(
        self, *, codes=None, value=None, stamp=None, current=None, locked=None, unpack_fails=None, place_fails=False
    ):
        self._unpack_fails = unpack_fails
        self._place_fails = place_fails
        self.calls: list[tuple] = []
        self._codes = codes if codes is not None else (lambda argv: 0)
        self._value = value if value is not None else (r"%SystemRoot%\system32;C:\tools", self.REG_EXPAND_SZ)
        self._stamp = stamp if stamp is not None else {"python_version": "3.11", "platform_tag": "win-amd64"}
        self._current = current if current is not None else {"python_version": "3.11", "platform_tag": "win-amd64"}
        self._locked = locked
        #: The argv of every interpreter `check-platform` asked, in order.
        self.asked_interpreters: list[tuple] = []

    # -- the four the plan names -------------------------------------------
    def run(self, argv):
        self.calls.append(("run",) + tuple(argv))
        return self._codes(tuple(argv))

    def link(self, path, target):
        self.calls.append(("link", path, target))

    def echo(self, line):
        self.calls.append(("echo", line))

    def read(self):
        self.calls.append(("registry-read",))
        return self._value

    def write(self, value, value_type):
        self.calls.append(("registry-write", value, value_type))

    def broadcast(self):
        self.calls.append(("registry-broadcast",))

    # -- removal and the platform stamp ------------------------------------
    def remove(self, path):
        self.calls.append(("remove", path))
        if self._locked is not None and self._locked in str(path):
            raise PermissionError(13, "the process cannot access the file", str(path))

    def stamp(self, path):
        self.calls.append(("stamp", path))
        return dict(self._stamp)

    def current(self, argv=()):
        self.asked_interpreters.append(tuple(argv))
        return dict(self._current)

    # -- the runtime payloads ----------------------------------------------
    def unpack(self, archive, destination):
        self.calls.append(("unpack", archive, destination))
        if self._unpack_fails is not None and self._unpack_fails in str(archive):
            raise _INSTALL.InstallError(f"{archive} could not be unpacked into {destination}")

    def place(self, path, target):
        self.calls.append(("place", path, target))
        if self._place_fails:
            raise _INSTALL.InstallError(f"node could not be placed at {path}")

    def floor(self, step):
        """The engine-floor seam, answered with "carry on" and recorded nowhere.

        At its default it would read the step's ``.vsix`` on this host and could start ``code --version``.
        :class:`TestTheVsixStepAsksTheFloorFirst` replaces it per journal.
        """
        return None

    @property
    def kinds(self) -> list[str]:
        return [call[0] for call in self.calls]

    @property
    def echoed(self) -> list[str]:
        return [call[1] for call in self.calls if call[0] == "echo"]

    def execute(self, steps, platform, progress=None):
        return _INSTALL.execute_plan(
            steps,
            platform,
            progress=progress,
            run=self.run,
            link=self.link,
            remove=self.remove,
            registry=self,
            echo=self.echo,
            stamp=self.stamp,
            current=self.current,
            unpack=self.unpack,
            place=self.place,
            floor=self.floor,
        )

    def main(self, argv, **rest):
        """:func:`install.main` with all seven seams wired into this journal.

        A case that deliberately leaves a seam at its default calls ``_INSTALL.main`` directly: the interpreter-floor
        and usage-error cases wire ``run`` alone (or nothing) so that ``stamp`` and ``current`` read the real host.
        """
        return _INSTALL.main(
            argv,
            run=self.run,
            link=self.link,
            remove=self.remove,
            registry=self,
            echo=self.echo,
            stamp=self.stamp,
            current=self.current,
            unpack=self.unpack,
            place=self.place,
            floor=self.floor,
            **rest,
        )


def _older_interpreters() -> list[str]:
    """This interpreter, plus every older spelling that happens to be on this host.

    The wrapper falls back to a plain ``python`` when it finds no 3.11, so an older interpreter still reaches
    ``install.py`` and must get a sentence rather than a ``SyntaxError``. Measures what is here; installs nothing.
    """
    interpreters = [sys.executable]
    for candidate in ("/usr/bin/python3", "python3.7", "python3.8", "python3.9", "python3.10"):
        resolved = candidate if Path(candidate).is_file() else shutil.which(candidate)
        if resolved and resolved not in interpreters:
            interpreters.append(resolved)
    return interpreters


def _which(mapping):
    return lambda name: mapping.get(name)


def _cannot_be_started(program: str):
    """A ``run`` seam that answers what :func:`run_argv` answers for a program that will not start.

    ``run_argv`` converts the ``OSError`` into an :class:`InstallError`, so the seam raises the same rather than
    answering a code. Every other argv exits 0.
    """

    def run(argv):
        if argv[0] == program:
            # `run_argv` itself over a path that is not there, so the class and the sentence are the production ones.
            return _INSTALL.run_argv([os.path.join(os.sep, "nonexistent-swcraft-probe", program), *argv[1:]])
        return 0

    return run


class TestExecutionSeams:
    """One step kind, one call -- asserted as a list, not claimed as a property."""

    def test_the_posix_plan_calls_five_seams_in_order(self) -> None:
        journal = Journal()
        journal.execute(_posix_plan(), "posix")
        assert journal.kinds == ["stamp", "run", "run", "link", "echo", "run"]
        home = PurePosixPath(_POSIX_HOME)
        assert journal.calls[3] == ("link", str(home / "current"), str(home / "versions" / _VERSION))
        assert str(home / "current" / "bin") in journal.echoed[0]
        assert "export PATH=" in journal.echoed[0] and "$PATH" in journal.echoed[0]
        assert "Add this to your shell profile" in journal.echoed[0], (
            "the VS Code extension reads this fragment out of the transcript to tell a reader "
            "whether the line was printed at all (vscode_extension/src/toolchainInstall.ts, "
            "PATH_LINE_PRINTED_MARKER). The `path` step is planned only when the entry is absent "
            "from the environment the installer was spawned with, so its absence is news too -- "
            "and a reword that reaches only one side turns that reading into a wrong answer "
            "rather than a missing one"
        )

    def test_the_windows_plan_puts_the_path_step_in_the_registry(self) -> None:
        journal = Journal()
        journal.execute(_windows_plan(), "nt")
        assert journal.kinds == [
            "stamp",
            "run",
            "run",
            "link",
            "registry-read",
            "registry-write",
            "registry-broadcast",
            "run",
        ]
        assert journal.echoed == [], "the Windows PATH edit is silent: the value is the report"

    def test_the_registry_write_keeps_the_type_it_read_and_appends(self) -> None:
        journal = Journal(value=(r"%SystemRoot%\system32", Journal.REG_EXPAND_SZ))
        journal.execute(_windows_plan(), "nt")
        written = [call for call in journal.calls if call[0] == "registry-write"]
        entry = str(PureWindowsPath(_WIN_HOME) / "current" / "Scripts")
        assert written == [("registry-write", r"%SystemRoot%\system32;" + entry, Journal.REG_EXPAND_SZ)]

    def test_a_plain_string_value_is_written_back_as_a_plain_string(self) -> None:
        """The type is *carried*, not chosen: an unexpanded value stays unexpanded."""
        journal = Journal(value=(r"C:\tools", Journal.REG_SZ))
        journal.execute(_windows_plan(), "nt")
        assert [call[2] for call in journal.calls if call[0] == "registry-write"] == [Journal.REG_SZ]

    def test_an_entry_already_in_the_durable_value_is_not_appended_again(self) -> None:
        """The process ``PATH`` and ``HKCU\\Environment`` can disagree; the durable one decides."""
        entry = str(PureWindowsPath(_WIN_HOME) / "current" / "Scripts")
        journal = Journal(value=(r"C:\tools;" + entry, Journal.REG_EXPAND_SZ))
        journal.execute(_windows_plan(), "nt")
        assert "registry-write" not in journal.kinds
        assert "registry-broadcast" not in journal.kinds
        assert journal.kinds.count("registry-read") == 1

    def test_the_check_platform_step_reads_the_stamp_the_plan_names(self) -> None:
        journal = Journal()
        journal.execute(_windows_plan(), "nt")
        assert journal.calls[0] == ("stamp", str(PureWindowsPath(_WIN_ARCHIVE) / "wheels" / "PLATFORM.json"))

    def test_the_stamp_is_taken_from_the_interpreter_the_venv_is_built_with(self) -> None:
        """The stamp is asked of the interpreter the venv is built with, not the one running this file.

        Comparing against the running interpreter let ``python3.11 install.py --python <a 3.9>`` pass the check and
        fail inside pip with the resolution error the check exists to prevent.
        """
        journal = Journal()
        journal.execute(_posix_plan(), "posix")
        assert journal.asked_interpreters == [_POSIX_PYTHON]

    def test_a_foreign_stamp_stops_the_run_before_the_venv(self) -> None:
        journal = Journal(stamp={"python_version": "3.11", "platform_tag": "macosx-15.0-arm64"})
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            journal.execute(_windows_plan(), "nt")
        assert "macosx-15.0-arm64" in str(excinfo.value)
        assert journal.kinds == ["stamp"]

    def test_a_failing_pip_stops_the_run_and_leaves_current_untouched(self) -> None:
        journal = Journal(codes=lambda argv: 1 if "pip" in argv else 0)
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            journal.execute(_windows_plan(), "nt")
        assert "pip" in str(excinfo.value)
        assert journal.kinds == ["stamp", "run", "run"], "the link step is where `current` moves"
        assert "link" not in journal.kinds

    def test_a_failing_venv_stops_before_pip(self) -> None:
        journal = Journal(codes=lambda argv: 1 if "venv" in argv else 0)
        with pytest.raises(_INSTALL.InstallError):
            journal.execute(_windows_plan(), "nt")
        assert journal.kinds == ["stamp", "run"]

    def test_the_removals_run_after_the_link_and_the_vsix(self) -> None:
        journal = Journal()
        journal.execute(_windows_plan(installed=("0.6.0",)), "nt")
        assert journal.kinds.index("link") < journal.kinds.index("remove")
        assert journal.kinds.index("run") < journal.kinds.index("remove")
        assert journal.calls[-1] == ("remove", str(PureWindowsPath(_WIN_HOME) / "versions" / "0.6.0"))

    def test_a_directory_that_cannot_be_removed_is_one_sentence_and_the_run_finishes(self) -> None:
        journal = Journal(locked="0.5.3")
        journal.execute(_windows_plan(installed=("0.6.0", "0.5.3")), "nt")
        locked = [line for line in journal.echoed if "0.5.3" in line]
        assert len(locked) == 1, "the locked directory is reported once"
        assert "remove" in locked[0]
        home = PureWindowsPath(_WIN_HOME)
        assert journal.calls[-1] == ("remove", str(home / "versions" / "0.6.0")), (
            "the run finished: the other version was still removed after the failure"
        )

    def test_the_runtime_seams_run_after_pip_and_before_the_link(self) -> None:
        journal = Journal()
        journal.execute(_posix_plan(members=_LINUX_MEMBERS), "posix")
        assert journal.kinds == ["stamp", "run", "run", "unpack", "place", "echo", "link", "echo", "run"]
        archive = PurePosixPath(_POSIX_ARCHIVE)
        target = PurePosixPath(_POSIX_HOME) / "versions" / _VERSION
        assert journal.calls[3] == (
            "unpack",
            str(archive / "runtime" / "node.tar.xz"),
            str(target / "runtime" / "node"),
        )
        assert journal.calls[4] == ("place", str(target / "bin" / "node"), "../runtime/node/bin/node")
        assert journal.echoed[0] == (
            f"Node.js for the diagram generator is linked into {target / 'bin'}, beside swcraft"
        )
        assert "Add this to your shell profile" in journal.echoed[1], "the PATH line is still the path step's own"

    def test_the_windows_runtime_seams_report_a_copy(self) -> None:
        journal = Journal()
        journal.execute(_windows_plan(members=_WINDOWS_MEMBERS), "nt")
        assert journal.kinds == [
            "stamp",
            "run",
            "run",
            "unpack",
            "place",
            "echo",
            "link",
            "registry-read",
            "registry-write",
            "registry-broadcast",
            "run",
        ]
        scripts = PureWindowsPath(_WIN_HOME) / "versions" / _VERSION / "Scripts"
        assert journal.echoed == [f"Node.js for the diagram generator is copied into {scripts}, beside swcraft"]

    def test_the_node_sentence_matches_none_of_the_markers_the_extension_reads(self) -> None:
        """``toolchainInstall.ts`` reads two fragments out of this transcript; a new line must not carry either."""
        source = (REPO_ROOT / "vscode_extension" / "src" / "toolchainInstall.ts").read_text(encoding="utf-8")
        block = source.split("const EXTENSION_NOT_INSTALLED_MARKERS = [", 1)[1].split("];", 1)[0]
        markers = re.findall(r'"([^"]+)"', block) + re.findall(r'const PATH_LINE_PRINTED_MARKER = "([^"]+)"', source)
        assert len(markers) >= 2, markers
        for platform, plan in (
            ("posix", _posix_plan(members=_LINUX_MEMBERS)),
            ("nt", _windows_plan(members=_WINDOWS_MEMBERS)),
        ):
            sentence = _INSTALL.node_placed_sentence([step for step in plan if step.kind == "node"][0], platform)
            assert [marker for marker in markers if marker in sentence] == [], sentence

    def test_a_payload_that_will_not_unpack_stops_the_run_before_the_link(self) -> None:
        journal = Journal(unpack_fails="node.tar.xz")
        with pytest.raises(_INSTALL.InstallError):
            journal.execute(_posix_plan(members=_LINUX_MEMBERS), "posix")
        assert journal.kinds == ["stamp", "run", "run", "unpack"]
        assert "link" not in journal.kinds and "place" not in journal.kinds

    def test_a_node_that_cannot_be_placed_stops_the_run_before_the_link(self) -> None:
        journal = Journal(place_fails=True)
        with pytest.raises(_INSTALL.InstallError):
            journal.execute(_windows_plan(members=_WINDOWS_MEMBERS), "nt")
        assert journal.kinds == ["stamp", "run", "run", "unpack", "place"]
        assert "link" not in journal.kinds

    def test_a_step_kind_the_installer_does_not_know_is_refused_by_name(self) -> None:
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            Journal().execute([_INSTALL.Step("unheard-of")], "posix")
        assert "unheard-of" in str(excinfo.value)

    def test_the_carried_python_is_unpacked_before_the_venv_and_the_bootstrap_removed_last(self) -> None:
        journal = Journal()
        journal.execute(_posix_plan(members=_FULL_LINUX_MEMBERS), "posix")
        assert journal.kinds == [
            "stamp",
            "unpack",
            "run",
            "run",
            "unpack",
            "place",
            "echo",
            "link",
            "echo",
            "run",
            "remove",
        ]
        archive = PurePosixPath(_POSIX_ARCHIVE)
        target = PurePosixPath(_POSIX_HOME) / "versions" / _VERSION
        assert journal.calls[1] == (
            "unpack",
            str(archive / "runtime" / "python.tar.gz"),
            str(target / "runtime" / "python"),
        )
        assert journal.calls[2][1] == str(target / "runtime" / "python" / "bin" / "python3")
        assert journal.calls[-1] == ("remove", str(archive / ".bootstrap"))
        assert journal.asked_interpreters == [_POSIX_PYTHON]

    def test_a_python_payload_that_will_not_unpack_stops_the_run_before_the_venv(self) -> None:
        journal = Journal(unpack_fails="python.tar.gz")
        with pytest.raises(_INSTALL.InstallError):
            journal.execute(_posix_plan(members=_FULL_LINUX_MEMBERS), "posix")
        assert journal.kinds == ["stamp", "unpack"]

    def test_on_windows_a_bootstrap_that_cannot_be_removed_is_silent_and_the_run_finishes(self) -> None:
        """On Windows the interpreter running the installer lives inside it; the installation is done by then.

        The wrapper removes the directory after ``install.py`` exits, so no echoed line names it.
        """
        journal = Journal(locked=".bootstrap")
        assert journal.execute(_windows_plan(members=_FULL_WINDOWS_MEMBERS), "nt") == (), (
            "not an unremoved version: an uninstall would not report it"
        )
        assert journal.calls[-1][0] == "remove" and journal.calls[-1][1].endswith(".bootstrap"), (
            "the removal is still tried: run on a system Python it can succeed"
        )
        assert [line for line in journal.echoed if ".bootstrap" in line] == [], journal.echoed

    def test_on_posix_a_bootstrap_that_cannot_be_removed_is_still_the_readers_to_delete(self) -> None:
        """No wrapper step follows ``install.py`` on POSIX: ``swcraft_install.sh`` ends in ``exec``."""
        journal = Journal(locked=".bootstrap")
        journal.execute(_posix_plan(members=_FULL_LINUX_MEMBERS), "posix")
        lines = [line for line in journal.echoed if ".bootstrap" in line]
        assert len(lines) == 1 and "delete that directory yourself" in lines[0]

    def test_the_bootstrap_sentence_matches_none_of_the_markers_the_extension_reads(self) -> None:
        source = (REPO_ROOT / "vscode_extension" / "src" / "toolchainInstall.ts").read_text(encoding="utf-8")
        block = source.split("const EXTENSION_NOT_INSTALLED_MARKERS = [", 1)[1].split("];", 1)[0]
        markers = re.findall(r'"([^"]+)"', block) + re.findall(r'const PATH_LINE_PRINTED_MARKER = "([^"]+)"', source)
        sentence = _INSTALL.bootstrap_left_sentence("/tmp/swcraft/.bootstrap", PermissionError(13, "in use"))
        assert len(markers) >= 2 and [marker for marker in markers if marker in sentence] == [], sentence


@pytest.mark.parametrize(
    "tag", ["macosx-11.0-arm64", "macosx-26.0-arm64", "macosx-14.0-x86_64", "win-amd64", "linux-x86_64", "macosx", None]
)
def test_the_builder_and_the_installer_read_a_platform_tag_the_same_way(tag) -> None:
    """``build_artifacts.platform_compatibility`` restates the installer's; this holds the two to one answer."""
    assert _ARTIFACTS.platform_compatibility(tag) == _INSTALL.platform_compatibility(tag)


def test_the_builder_and_the_installer_ask_an_interpreter_the_same_question() -> None:
    """``build_artifacts._INTERPRETER_STAMP_PROGRAM`` restates the installer's ``_STAMP_PROGRAM``.

    They differ on purpose in one field: the builder asks for ``major.minor.micro`` (it compares the carried
    interpreter with the pinned full version), the installer for ``major.minor``. Reaches only the interpreter running
    the suite.
    """

    def ask(program: str) -> dict:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", program],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            encoding="utf-8",
            check=True,
        )
        return json.loads(completed.stdout)

    builder = ask(_ARTIFACTS._INTERPRETER_STAMP_PROGRAM)
    installer = ask(_INSTALL._STAMP_PROGRAM)
    assert sorted(builder) == sorted(installer) == ["platform_tag", "python_version"]
    assert builder["python_version"].split(".")[:2] == installer["python_version"].split(".")
    assert builder["python_version"] == ".".join(str(part) for part in sys.version_info[:3])
    assert builder["platform_tag"] == installer["platform_tag"]


# ---------------------------------------------------------------------------
# The runtime payloads: unpacked and placed, on this host
# ---------------------------------------------------------------------------

_NODE_SCRIPT = b"#!/bin/sh\necho v0.0.0-test\n"


def _write_tar(path: Path, entries, compression: str = "xz") -> Path:
    """A tar payload from ``(kind, name, payload, mode)`` entries, written with :mod:`tarfile`."""
    with tarfile.open(path, "w:" + compression) as bundle:
        for kind, name, payload, mode in entries:
            info = tarfile.TarInfo(name)
            info.mode = mode
            data = None
            if kind == "dir":
                info.type = tarfile.DIRTYPE
            elif kind == "file":
                info.size = len(payload)
                data = io.BytesIO(payload)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = payload
            elif kind == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = payload
            elif kind == "fifo":
                info.type = tarfile.FIFOTYPE
            bundle.addfile(info, data)
    return path


def _node_shaped(leading: str = "node-v0.0.0-test-x64", link: bool = False) -> list[tuple]:
    """Node.js' own tarball in miniature: one leading directory, an executable, a library file."""
    entries = [
        ("dir", leading, b"", 0o755),
        ("dir", f"{leading}/bin", b"", 0o755),
        ("file", f"{leading}/bin/node", _NODE_SCRIPT, 0o755),
        ("dir", f"{leading}/lib", b"", 0o755),
        ("file", f"{leading}/lib/npm-cli.js", b"console.log('npm')\n", 0o644),
    ]
    if link:
        entries.append(("symlink", f"{leading}/bin/npm", "../lib/npm-cli.js", 0o777))
    return entries


def _write_zip(path: Path, entries) -> Path:
    """A zip payload from ``(name, payload, mode)`` entries; a ``None`` mode records none."""
    with zipfile.ZipFile(path, "w") as bundle:
        for name, payload, mode in entries:
            info = zipfile.ZipInfo(name)
            if mode is not None:
                info.external_attr = mode << 16
            bundle.writestr(info, payload)
    return path


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _tree_outside(root: Path, inside: Path) -> set[str]:
    """Every entry under *root* that is not *inside* or below it, links listed and never followed.

    A member that leaves ``<destination>.unpacking`` lands wherever its ``..`` or its links lead, which is rarely the
    one path a test thought to name.
    """
    found = set()
    for directory, subdirectories, files in os.walk(root):
        for name in subdirectories + files:
            path = Path(directory) / name
            if path != inside and inside not in path.parents:
                found.add(path.relative_to(root).as_posix())
    return found


class TestTheRuntimePayloadIsUnpacked:
    """:func:`install.unpack_payload` on real files: the leading directory goes, modes stay, nothing escapes.

    Every refusal is asserted with the **previous** tree still in place and no
    staging directory left behind, because the step runs before ``link`` and a
    half-unpacked tree under a version that may already be ``current`` (a repair)
    is the state the staging swap exists to prevent. Mode assertions are POSIX-only
    by the installer's own decision: it changes no mode on Windows.
    """

    @pytest.mark.parametrize("compression", ["xz", "gz"])
    def test_a_tar_payload_is_unpacked_with_its_leading_directory_stripped(self, tmp_path: Path, compression) -> None:
        archive = _write_tar(tmp_path / f"node.tar.{compression}", _node_shaped(), compression)
        destination = self._fresh_destination(tmp_path)
        _INSTALL.unpack_payload(str(archive), str(destination))
        assert (destination / "bin" / "node").read_bytes() == _NODE_SCRIPT
        assert (destination / "lib" / "npm-cli.js").is_file()
        assert sorted(path.name for path in destination.iterdir()) == ["bin", "lib"], "no version-named directory"
        assert not Path(str(destination) + _INSTALL.UNPACKING_SUFFIX).exists()
        if os.name != "nt":
            assert _mode(destination / "bin" / "node") == 0o755
            assert _mode(destination / "lib" / "npm-cli.js") == 0o644

    def test_a_zip_payload_is_unpacked_the_same_way(self, tmp_path: Path) -> None:
        archive = _write_zip(
            tmp_path / "node.zip",
            [
                ("node-v0.0.0-test-win-x64/", b"", 0o40755),
                ("node-v0.0.0-test-win-x64/node.exe", b"MZ-not-really", 0o100755),
                ("node-v0.0.0-test-win-x64/node_modules/npm/index.js", b"module.exports = 1\n", None),
            ],
        )
        destination = tmp_path / "runtime" / "node"
        _INSTALL.unpack_payload(str(archive), str(destination))
        assert (destination / "node.exe").read_bytes() == b"MZ-not-really"
        assert (destination / "node_modules" / "npm" / "index.js").is_file()
        assert sorted(path.name for path in destination.iterdir()) == ["node.exe", "node_modules"]
        if os.name != "nt":
            assert _mode(destination / "node.exe") == 0o755

    def test_the_vendors_links_are_unpacked_as_links(self, tmp_path: Path) -> None:
        skip_without_symlink_support(tmp_path)
        archive = _write_tar(tmp_path / "node.tar.xz", _node_shaped(link=True))
        destination = tmp_path / "runtime" / "node"
        _INSTALL.unpack_payload(str(archive), str(destination))
        link = destination / "bin" / "npm"
        assert link.is_symlink()
        assert os.readlink(link).replace("\\", "/") == "../lib/npm-cli.js"
        assert link.resolve() == (destination / "lib" / "npm-cli.js").resolve()

    def test_a_hard_link_is_unpacked_as_a_copy_of_its_target(self, tmp_path: Path) -> None:
        entries = _node_shaped() + [
            ("hardlink", "node-v0.0.0-test-x64/bin/node22", "node-v0.0.0-test-x64/bin/node", 0o755)
        ]
        destination = tmp_path / "runtime" / "node"
        _INSTALL.unpack_payload(str(_write_tar(tmp_path / "node.tar.xz", entries)), str(destination))
        assert (destination / "bin" / "node22").read_bytes() == _NODE_SCRIPT

    def test_an_existing_tree_is_replaced_whole(self, tmp_path: Path) -> None:
        destination = tmp_path / "runtime" / "node"
        (destination / "bin").mkdir(parents=True)
        (destination / "stale.txt").write_bytes(b"from the previous run")
        _INSTALL.unpack_payload(str(_write_tar(tmp_path / "node.tar.xz", _node_shaped())), str(destination))
        assert not (destination / "stale.txt").exists()
        assert (destination / "bin" / "node").is_file()

    def test_a_staging_directory_an_interrupted_run_left_is_not_carried_in(self, tmp_path: Path) -> None:
        destination = tmp_path / "runtime" / "node"
        leftover = Path(str(destination) + _INSTALL.UNPACKING_SUFFIX)
        leftover.mkdir(parents=True)
        (leftover / "half.bin").write_bytes(b"interrupted")
        _INSTALL.unpack_payload(str(_write_tar(tmp_path / "node.tar.xz", _node_shaped())), str(destination))
        assert not (destination / "half.bin").exists()
        assert not leftover.exists()

    @pytest.mark.parametrize(
        "entries",
        [
            pytest.param([("file", "../escape.txt", b"x", 0o644)], id="dot-dot"),
            pytest.param(
                [("dir", "lead", b"", 0o755), ("file", "lead/../../escape.txt", b"x", 0o644)], id="dot-dot-inside"
            ),
            pytest.param([("file", "/tmp/swcraft-absolute-escape.txt", b"x", 0o644)], id="absolute"),
            pytest.param([("dir", "lead", b"", 0o755), ("file", "lead/a:b", b"x", 0o644)], id="colon"),
            pytest.param([("dir", "lead", b"", 0o755), ("file", "other/x", b"x", 0o644)], id="two-leading-directories"),
            pytest.param([("file", "loose.txt", b"x", 0o644)], id="a-file-at-the-root"),
            pytest.param([("dir", "lead", b"", 0o755), ("symlink", "lead/out", "../../escape", 0o777)], id="link-out"),
            pytest.param(
                [("dir", "lead", b"", 0o755), ("symlink", "lead/abs", "/etc/passwd", 0o777)], id="absolute-link"
            ),
            pytest.param([("dir", "lead", b"", 0o755), ("fifo", "lead/pipe", b"", 0o644)], id="fifo"),
            pytest.param(
                [("dir", "lead", b"", 0o755), ("symlink", "lead/bs", "\\etc\\passwd", 0o777)], id="backslash-link"
            ),
            pytest.param([("file", "lead\\..\\..\\escape.txt", b"x", 0o644)], id="backslash-dot-dot"),
            pytest.param([("dir", "lead", b"", 0o755)], id="only-the-leading-directory"),
            pytest.param([], id="no-member-at-all"),
            pytest.param(
                [("dir", "lead", b"", 0o755), ("hardlink", "lead/x", "other/y", 0o644)],
                id="hard-link-to-another-leading-directory",
            ),
            pytest.param(
                [("dir", "lead", b"", 0o755), ("hardlink", "lead/x", "lead/../../escape.txt", 0o644)],
                id="hard-link-dot-dot",
            ),
        ],
    )
    def test_a_member_the_rule_cannot_place_is_refused_and_the_previous_tree_stays(
        self, tmp_path: Path, entries
    ) -> None:
        destination = tmp_path / "home" / "runtime" / "node"
        destination.mkdir(parents=True)
        (destination / "keep.txt").write_bytes(b"the previous tree")
        archive = _write_tar(tmp_path / "node.tar.xz", entries)
        before = _tree_outside(tmp_path, destination)
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.unpack_payload(str(archive), str(destination))
        assert str(archive) in str(excinfo.value) and "download the archive again" in str(excinfo.value)
        assert (destination / "keep.txt").read_bytes() == b"the previous tree"
        assert not Path(str(destination) + _INSTALL.UNPACKING_SUFFIX).exists()
        assert _tree_outside(tmp_path, destination) == before, "nothing appeared beside the destination"
        assert not Path("/tmp/swcraft-absolute-escape.txt").exists()

    @pytest.mark.parametrize(
        "entries",
        [
            pytest.param(
                [
                    ("dir", "lead", b"", 0o755),
                    ("dir", "lead/a", b"", 0o755),
                    ("dir", "lead/a/b", b"", 0o755),
                    ("symlink", "lead/a/b/up", "../..", 0o777),
                    ("symlink", "lead/a/b/up/esc", "../../..", 0o777),
                    ("file", "lead/a/b/up/esc/pwned.txt", b"written outside", 0o644),
                ],
                id="a-file-written-through-a-chain",
            ),
            pytest.param(
                [
                    ("dir", "lead", b"", 0o755),
                    ("dir", "lead/a", b"", 0o755),
                    ("dir", "lead/a/b", b"", 0o755),
                    ("symlink", "lead/a/b/up", "../..", 0o777),
                    ("symlink", "lead/a/b/up/esc", "../../..", 0o777),
                ],
                id="a-chain-left-pointing-out",
            ),
            pytest.param(
                [
                    ("dir", "lead", b"", 0o755),
                    ("dir", "lead/deep", b"", 0o755),
                    ("symlink", "lead/deep/up", "..", 0o777),
                    ("symlink", "lead/x", "deep/up/..", 0o777),
                    ("file", "lead/x/evil", b"written outside", 0o644),
                ],
                id="a-link-through-a-link",
            ),
            pytest.param(
                [
                    ("dir", "lead", b"", 0o755),
                    ("dir", "lead/a", b"", 0o755),
                    ("symlink", "lead/a/up", "..", 0o777),
                    ("symlink", "lead/a/up/esc", "../..", 0o777),
                    ("hardlink", "lead/stolen", "lead/a/up/esc/secret.txt", 0o644),
                ],
                id="a-hard-link-copied-through-a-chain",
            ),
        ],
    )
    def test_a_chain_of_links_that_each_stay_inside_on_paper_is_refused(self, tmp_path: Path, entries) -> None:
        """A chain of links that each stay inside on paper is refused, three levels above staging.

        Each link passes :func:`install.link_stays_inside`, which reads every earlier segment as a real directory; on
        disk an earlier link resolves first. The previous tree stays, nothing appears under ``tmp_path`` but the
        destination, and a file beside the installation is never copied in.
        """
        skip_without_symlink_support(tmp_path)
        destination = tmp_path / "deep" / "home" / "runtime" / "node"
        destination.mkdir(parents=True)
        (destination / "keep.txt").write_bytes(b"the previous tree")
        (tmp_path / "deep" / "home" / "secret.txt").write_bytes(b"not the payload's")
        archive = _write_tar(tmp_path / "node.tar.xz", entries)
        before = _tree_outside(tmp_path, destination)
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.unpack_payload(str(archive), str(destination))
        assert "through a link to outside" in str(excinfo.value)
        assert str(excinfo.value).count("\n") == 0, "one sentence"
        assert (destination / "keep.txt").read_bytes() == b"the previous tree"
        assert not (destination / "stolen").exists()
        assert not Path(str(destination) + _INSTALL.UNPACKING_SUFFIX).exists()
        assert _tree_outside(tmp_path, destination) == before, "nothing was written outside the destination"

    def test_a_relative_link_that_stays_inside_through_another_link_is_kept(self, tmp_path: Path) -> None:
        """The physical check refuses only what leaves: a link resolved through an in-tree link is Node's own shape."""
        skip_without_symlink_support(tmp_path)
        entries = _node_shaped(link=True) + [
            ("symlink", "node-v0.0.0-test-x64/lib/current", ".", 0o777),
            ("symlink", "node-v0.0.0-test-x64/bin/npm2", "../lib/current/npm-cli.js", 0o777),
            ("hardlink", "node-v0.0.0-test-x64/bin/npm3", "node-v0.0.0-test-x64/bin/npm", 0o755),
        ]
        destination = tmp_path / "runtime" / "node"
        _INSTALL.unpack_payload(str(_write_tar(tmp_path / "node.tar.xz", entries)), str(destination))
        assert (destination / "bin" / "npm2").read_bytes() == b"console.log('npm')\n"
        assert (destination / "bin" / "npm3").read_bytes() == b"console.log('npm')\n", "a hard link to a link copies"

    @pytest.mark.parametrize(
        "entries",
        [
            pytest.param(
                [
                    ("dir", "lead", b"", 0o755),
                    ("hardlink", "lead/early", "lead/late", 0o644),
                    ("file", "lead/late", b"x", 0o644),
                ],
                id="hard-link-to-a-later-member",
            ),
            pytest.param(
                [("dir", "lead", b"", 0o755), ("dir", "lead/d", b"", 0o755), ("hardlink", "lead/x", "lead/d", 0o644)],
                id="hard-link-to-a-directory",
            ),
        ],
    )
    def test_a_hard_link_with_nothing_to_copy_is_one_sentence(self, tmp_path: Path, entries) -> None:
        """The copy fails, and the failure is the damaged-payload sentence with the previous tree kept."""
        destination = tmp_path / "runtime" / "node"
        destination.mkdir(parents=True)
        (destination / "keep.txt").write_bytes(b"the previous tree")
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.unpack_payload(str(_write_tar(tmp_path / "node.tar.xz", entries)), str(destination))
        assert "could not be unpacked" in str(excinfo.value) and str(excinfo.value).count("\n") == 0
        assert (destination / "keep.txt").read_bytes() == b"the previous tree"
        assert not Path(str(destination) + _INSTALL.UNPACKING_SUFFIX).exists()

    def test_a_read_only_directory_and_file_stay_removable_and_repairable(self, tmp_path: Path) -> None:
        """A ``0o555`` directory gets the owner's ``rwx``; a ``0o444`` file keeps its mode and still goes."""
        entries = [
            ("dir", "lead", b"", 0o755),
            ("dir", "lead/ro", b"", 0o555),
            ("file", "lead/ro/f.txt", b"read only", 0o444),
        ]
        archive = _write_tar(tmp_path / "payload.tar.xz", entries)
        destination = tmp_path / "runtime" / "payload"
        _INSTALL.unpack_payload(str(archive), str(destination))
        if os.name != "nt":
            assert _mode(destination / "ro") == 0o755
            assert _mode(destination / "ro" / "f.txt") == 0o444
        _INSTALL.unpack_payload(str(archive), str(destination))  # a repair, over the tree it left
        assert (destination / "ro" / "f.txt").read_bytes() == b"read only"
        assert sorted(path.name for path in destination.parent.iterdir()) == ["payload"], "nothing left beside it"
        _INSTALL.remove_tree(str(destination))
        assert not destination.exists()

    def test_member_names_spelled_with_backslashes_are_unpacked_by_their_segments(self, tmp_path: Path) -> None:
        entries = [("dir", "lead", b"", 0o755), ("file", "lead\\bin\\node", _NODE_SCRIPT, 0o755)]
        destination = tmp_path / "runtime" / "node"
        _INSTALL.unpack_payload(str(_write_tar(tmp_path / "node.tar.xz", entries)), str(destination))
        assert (destination / "bin" / "node").read_bytes() == _NODE_SCRIPT

    # -- the swap ------------------------------------------------------------

    @staticmethod
    def _fresh_destination(tmp_path: Path) -> Path:
        """Where a version's Node.js tree goes; nothing is there yet (a first install)."""
        return tmp_path / "versions" / _VERSION / "runtime" / "node"

    @classmethod
    def _previous_tree(cls, tmp_path: Path) -> Path:
        destination = cls._fresh_destination(tmp_path)
        (destination / "bin").mkdir(parents=True)
        (destination / "bin" / "node.exe").write_bytes(b"held open by a running viewer")
        (destination / "keep.txt").write_bytes(b"the previous tree")
        return destination

    @staticmethod
    def _pauses(monkeypatch: pytest.MonkeyPatch) -> list[float]:
        """Record the retry's pauses instead of sleeping them: the one seam, the ``time.sleep`` it calls."""
        paused: list[float] = []
        monkeypatch.setattr(_INSTALL.time, "sleep", paused.append)
        return paused

    @classmethod
    def _rename_failing(
        cls, monkeypatch: pytest.MonkeyPatch, *suffixes_or_paths: str
    ) -> tuple[dict[str, int], list[float]]:
        """Refuse every rename of the named sources; the attempts are counted per item, the pauses recorded."""
        real = os.rename
        attempts = dict.fromkeys(suffixes_or_paths, 0)

        def rename(source, target):
            spelled = os.fspath(source)
            for item in suffixes_or_paths:
                if spelled == item or spelled.endswith(item):
                    attempts[item] += 1
                    raise PermissionError(13, "The process cannot access the file", spelled)
            return real(source, target)

        monkeypatch.setattr(_INSTALL.os, "rename", rename)
        return attempts, cls._pauses(monkeypatch)

    @staticmethod
    def _retried(source: object) -> str:
        """The last refusal's own text, unchanged, then the clause a retried refusal carries."""
        refused = PermissionError(13, "The process cannot access the file", os.fspath(source))
        return f"({refused}){_INSTALL.RENAME_RETRY_CLAUSE},"

    def test_a_tree_in_use_is_never_half_deleted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replacing a tree in use never half-deletes it: the live tree is renamed aside, never removed in place.

        Modelled as Windows behaves with a file in use: a rename of the directory fails whole.
        """
        destination = self._previous_tree(tmp_path)
        real_remove = _INSTALL.remove_tree
        removed: list[str] = []

        def remove_tree(path):
            removed.append(path)
            return real_remove(path)

        monkeypatch.setattr(_INSTALL, "remove_tree", remove_tree)
        attempts, paused = self._rename_failing(monkeypatch, str(destination))
        archive = _write_tar(tmp_path / "node.tar.xz", _node_shaped())
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.unpack_payload(str(archive), str(destination))
        message = str(excinfo.value)
        assert "could not be replaced" in message and "unchanged" in message and "close anything running" in message
        assert str(destination) not in removed, "the live tree is never deleted, only renamed aside"
        assert attempts == {str(destination): len(_INSTALL.RENAME_RETRY_PAUSES) + 1}, "moving aside is retried"
        assert paused == list(_INSTALL.RENAME_RETRY_PAUSES), "and it waits"
        assert self._retried(destination) in message and message.count("\n") == 0
        assert "viewer" not in message, "no diagram viewer holds node.exe"
        assert (destination / "keep.txt").read_bytes() == b"the previous tree", "the live tree is whole"
        assert (destination / "bin" / "node.exe").is_file()
        assert not Path(str(destination) + _INSTALL.UNPACKING_SUFFIX).exists()
        assert not Path(str(destination) + _INSTALL.SUPERSEDED_SUFFIX).exists()

    def test_a_staged_tree_that_cannot_take_the_name_puts_the_previous_one_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        destination = self._previous_tree(tmp_path)
        attempts, paused = self._rename_failing(monkeypatch, _INSTALL.UNPACKING_SUFFIX)
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.unpack_payload(str(_write_tar(tmp_path / "node.tar.xz", _node_shaped())), str(destination))
        message = str(excinfo.value)
        assert "unchanged" in message
        assert attempts == {_INSTALL.UNPACKING_SUFFIX: len(_INSTALL.RENAME_RETRY_PAUSES) + 1}
        assert paused == list(_INSTALL.RENAME_RETRY_PAUSES)
        assert self._retried(str(destination) + _INSTALL.UNPACKING_SUFFIX) in message and message.count("\n") == 0
        assert (destination / "keep.txt").read_bytes() == b"the previous tree"
        assert not Path(str(destination) + _INSTALL.UNPACKING_SUFFIX).exists()
        assert not Path(str(destination) + _INSTALL.SUPERSEDED_SUFFIX).exists()

    def test_a_previous_tree_that_cannot_be_put_back_is_named_where_it_is(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        destination = self._previous_tree(tmp_path)
        attempts, paused = self._rename_failing(monkeypatch, _INSTALL.UNPACKING_SUFFIX, _INSTALL.SUPERSEDED_SUFFIX)
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.unpack_payload(str(_write_tar(tmp_path / "node.tar.xz", _node_shaped())), str(destination))
        previous = Path(str(destination) + _INSTALL.SUPERSEDED_SUFFIX)
        assert str(previous) in str(excinfo.value) and str(excinfo.value).count("\n") == 0
        assert (previous / "keep.txt").read_bytes() == b"the previous tree", "moved aside, never deleted"
        every = len(_INSTALL.RENAME_RETRY_PAUSES) + 1
        assert attempts == {_INSTALL.UNPACKING_SUFFIX: every, _INSTALL.SUPERSEDED_SUFFIX: every}, "the rollback too"
        assert paused == list(_INSTALL.RENAME_RETRY_PAUSES) * 2, "each rename waits its own pauses"
        assert self._retried(str(destination) + _INSTALL.UNPACKING_SUFFIX) in str(excinfo.value)

    def test_a_previous_tree_that_cannot_be_cleared_claims_no_wait(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``remove_tree(previous)`` is not retried, so its refusal must not carry the clause."""
        destination = self._previous_tree(tmp_path)
        real_remove = _INSTALL.remove_tree

        def remove_tree(path):
            if path.endswith(_INSTALL.SUPERSEDED_SUFFIX):
                raise PermissionError(13, "The process cannot access the file", path)
            return real_remove(path)

        monkeypatch.setattr(_INSTALL, "remove_tree", remove_tree)
        attempts, paused = self._rename_failing(monkeypatch, str(destination), _INSTALL.UNPACKING_SUFFIX)
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.unpack_payload(str(_write_tar(tmp_path / "node.tar.xz", _node_shaped())), str(destination))
        message = str(excinfo.value)
        assert "could not be replaced" in message and "unchanged" in message
        assert " attempts over " not in message
        assert attempts == {str(destination): 0, _INSTALL.UNPACKING_SUFFIX: 0} and paused == []
        assert (destination / "keep.txt").read_bytes() == b"the previous tree"
        assert not Path(str(destination) + _INSTALL.UNPACKING_SUFFIX).exists()

    # -- an interrupt during a retry's pause (the window is seconds long) --

    @staticmethod
    def _interrupting(monkeypatch: pytest.MonkeyPatch, after: int) -> list[float]:
        """``time.sleep`` recording *after* pauses, then raising ``KeyboardInterrupt`` as Ctrl+C would."""
        paused: list[float] = []

        def sleep(delay):
            if len(paused) == after:
                raise KeyboardInterrupt
            paused.append(delay)

        monkeypatch.setattr(_INSTALL.time, "sleep", sleep)
        return paused

    @staticmethod
    def _renames(
        monkeypatch: pytest.MonkeyPatch,
        refuse: Callable[[str, int], bool],
        error: Callable[[str], OSError] | None = None,
    ) -> list[str]:
        """``os.rename`` refusing whenever ``refuse(source, attempt)`` says so; the attempts are recorded.

        The refusal is ``PermissionError`` -- the scanner's -- unless *error* builds another from the source.
        """
        real = os.rename
        attempts: list[str] = []

        def rename(source, target):
            spelled = os.fspath(source)
            attempts.append(spelled)
            if refuse(spelled, attempts.count(spelled)):
                if error is not None:
                    raise error(spelled)
                raise PermissionError(13, "The process cannot access the file", spelled)
            return real(source, target)

        monkeypatch.setattr(_INSTALL.os, "rename", rename)
        return attempts

    @pytest.mark.parametrize("with_previous", [False, True], ids=["first-install", "repair"])
    def test_an_interrupt_while_the_staged_in_rename_waits_puts_the_previous_tree_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_previous: bool
    ) -> None:
        if with_previous:
            destination = self._previous_tree(tmp_path)
        else:
            destination = self._fresh_destination(tmp_path)
        staging = str(destination) + _INSTALL.UNPACKING_SUFFIX
        previous = str(destination) + _INSTALL.SUPERSEDED_SUFFIX
        attempts = self._renames(monkeypatch, lambda source, _attempt: source == staging)
        paused = self._interrupting(monkeypatch, after=2)
        with pytest.raises(KeyboardInterrupt):
            _INSTALL.unpack_payload(str(_write_tar(tmp_path / "node.tar.xz", _node_shaped())), str(destination))
        assert attempts.count(staging) == 3 and paused == list(_INSTALL.RENAME_RETRY_PAUSES[:2])
        if with_previous:
            assert attempts.count(previous) == 1, "put back with one attempt, never a wait"
            assert (destination / "keep.txt").read_bytes() == b"the previous tree", "the live tree is back"
        else:
            assert previous not in attempts and not destination.exists()
        assert not Path(previous).exists()
        assert not Path(staging).exists(), "the staging tree is discarded"

    def test_an_interrupt_while_the_put_back_waits_still_tries_it_once_more(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        destination = self._previous_tree(tmp_path)
        staging = str(destination) + _INSTALL.UNPACKING_SUFFIX
        previous = str(destination) + _INSTALL.SUPERSEDED_SUFFIX
        attempts = self._renames(
            monkeypatch, lambda source, attempt: source == staging or (source == previous and attempt == 1)
        )
        every = len(_INSTALL.RENAME_RETRY_PAUSES)
        paused = self._interrupting(monkeypatch, after=every)  # the put-back's first pause is interrupted
        with pytest.raises(KeyboardInterrupt):
            _INSTALL.unpack_payload(str(_write_tar(tmp_path / "node.tar.xz", _node_shaped())), str(destination))
        assert attempts.count(staging) == every + 1 and paused == list(_INSTALL.RENAME_RETRY_PAUSES)
        assert attempts.count(previous) == 2, "the retried put-back's first attempt, then the one last attempt"
        assert (destination / "keep.txt").read_bytes() == b"the previous tree"
        assert not Path(previous).exists() and not Path(staging).exists()

    def test_an_interrupt_whose_put_back_is_refused_leaves_the_tree_aside_whole(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What the guarantee does not reach: the interrupt propagates, and the tree is at ``.previous``."""
        destination = self._previous_tree(tmp_path)
        staging = str(destination) + _INSTALL.UNPACKING_SUFFIX
        previous = Path(str(destination) + _INSTALL.SUPERSEDED_SUFFIX)
        attempts = self._renames(monkeypatch, lambda source, _attempt: source in (staging, str(previous)))
        self._interrupting(monkeypatch, after=0)
        with pytest.raises(KeyboardInterrupt):
            _INSTALL.unpack_payload(str(_write_tar(tmp_path / "node.tar.xz", _node_shaped())), str(destination))
        assert attempts.count(str(previous)) == 1
        assert (previous / "keep.txt").read_bytes() == b"the previous tree", "moved aside, never deleted"
        assert not destination.exists() and not Path(staging).exists()

    # -- the retried rename (an on-access scanner holding the files just written) --

    @pytest.mark.parametrize("with_previous", [False, True], ids=["first-install", "repair"])
    def test_a_staged_in_rename_refused_twice_then_allowed_completes_the_swap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_previous: bool
    ) -> None:
        """An on-access scanner lets go after two refusals, and the swap completes through ``unpack_payload``."""
        if with_previous:
            destination = self._previous_tree(tmp_path)
        else:
            destination = self._fresh_destination(tmp_path)
        staging = str(destination) + _INSTALL.UNPACKING_SUFFIX
        attempts = self._renames(monkeypatch, lambda source, attempt: source == staging and attempt <= 2)
        paused = self._pauses(monkeypatch)
        _INSTALL.unpack_payload(str(_write_tar(tmp_path / "node.tar.xz", _node_shaped())), str(destination))
        assert (destination / "bin" / "node").read_bytes() == _NODE_SCRIPT
        assert not (destination / "keep.txt").exists(), "the previous tree is replaced"
        assert attempts.count(staging) == 3, "refused twice, then allowed"
        assert paused == list(_INSTALL.RENAME_RETRY_PAUSES[:2])
        assert not Path(staging).exists()
        assert not Path(str(destination) + _INSTALL.SUPERSEDED_SUFFIX).exists(), "the previous tree is discarded"

    @staticmethod
    def _refusing(monkeypatch: pytest.MonkeyPatch, errors: list[OSError]) -> list[str]:
        """``os.rename`` raising *errors* in order, then renaming for real; the attempts are recorded."""
        real = os.rename
        attempts: list[str] = []

        def rename(source, target):
            attempts.append(os.fspath(source))
            if len(attempts) <= len(errors):
                raise errors[len(attempts) - 1]
            return real(source, target)

        monkeypatch.setattr(_INSTALL.os, "rename", rename)
        return attempts

    @pytest.mark.parametrize("refusals", [1, 2, len(_INSTALL.RENAME_RETRY_PAUSES)])
    def test_a_rename_refused_then_allowed_is_a_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusals: int
    ) -> None:
        destination = self._fresh_destination(tmp_path)
        staging = str(destination) + _INSTALL.UNPACKING_SUFFIX
        attempts = self._refusing(monkeypatch, [PermissionError(13, "Access is denied", staging)] * refusals)
        paused = self._pauses(monkeypatch)
        Path(staging).mkdir(parents=True)
        (Path(staging) / "python.exe").write_bytes(b"just written")
        _INSTALL.rename_tree(staging, str(destination))
        assert (destination / "python.exe").read_bytes() == b"just written"
        assert not Path(staging).exists()
        assert attempts == [staging] * (refusals + 1)
        assert paused == list(_INSTALL.RENAME_RETRY_PAUSES[:refusals])

    def test_a_rename_refused_every_time_raises_the_last_refusal_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source, target = str(tmp_path / "staged"), str(tmp_path / "live")
        every = len(_INSTALL.RENAME_RETRY_PAUSES) + 1
        errors: list[OSError] = [PermissionError(13, f"Access is denied ({n})", source) for n in range(every)]
        attempts = self._refusing(monkeypatch, errors)
        paused = self._pauses(monkeypatch)
        with pytest.raises(PermissionError) as excinfo:
            _INSTALL.rename_tree(source, target)
        assert excinfo.value is errors[-1]
        assert excinfo.value.errno == 13 and excinfo.value.filename == source
        assert attempts == [source] * every
        assert paused == list(_INSTALL.RENAME_RETRY_PAUSES), "no pause after the last attempt"

    def test_a_refusal_that_turns_into_another_error_stops_at_that_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source, target = str(tmp_path / "staged"), str(tmp_path / "live")
        gone = FileNotFoundError(2, "No such file or directory", source)
        attempts = self._refusing(monkeypatch, [PermissionError(13, "Access is denied", source), gone])
        paused = self._pauses(monkeypatch)
        with pytest.raises(FileNotFoundError) as excinfo:
            _INSTALL.rename_tree(source, target)
        assert excinfo.value is gone
        assert attempts == [source] * 2 and paused == [_INSTALL.RENAME_RETRY_PAUSES[0]]

    @pytest.mark.parametrize("failing", ["staged-in", "moved-aside"])
    def test_a_rename_refused_by_an_error_that_waiting_does_not_clear_is_tried_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
    ) -> None:
        destination = self._previous_tree(tmp_path)
        source = str(destination) + _INSTALL.UNPACKING_SUFFIX if failing == "staged-in" else str(destination)
        attempts = self._renames(
            monkeypatch,
            lambda spelled, _attempt: spelled == source,
            lambda spelled: OSError(28, "No space left on device", spelled),
        )
        paused = self._pauses(monkeypatch)
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.unpack_payload(str(_write_tar(tmp_path / "node.tar.xz", _node_shaped())), str(destination))
        message = str(excinfo.value)
        assert attempts.count(source) == 1 and paused == []
        assert "could not be replaced" in message and "unchanged" in message and " attempts over " not in message
        assert (destination / "keep.txt").read_bytes() == b"the previous tree"

    def test_the_retried_clause_is_derived_from_the_pauses(self) -> None:
        pauses = _INSTALL.RENAME_RETRY_PAUSES
        assert len(pauses) + 1 == 8 and round(sum(pauses), 2) == 7.85, "the documents' published figures"
        assert _INSTALL.RENAME_RETRY_CLAUSE == " after 8 attempts over 7.85 s"

    def test_a_previous_tree_an_interrupted_swap_left_is_removed_first(self, tmp_path: Path) -> None:
        destination = self._previous_tree(tmp_path)
        leftover = Path(str(destination) + _INSTALL.SUPERSEDED_SUFFIX)
        leftover.mkdir()
        (leftover / "stale.bin").write_bytes(b"from an interrupted swap")
        _INSTALL.unpack_payload(str(_write_tar(tmp_path / "node.tar.xz", _node_shaped())), str(destination))
        assert (destination / "bin" / "node").read_bytes() == _NODE_SCRIPT
        assert not (destination / "keep.txt").exists()
        assert not leftover.exists()

    # -- the zip container's own refusals --

    @pytest.mark.parametrize(
        ("entries", "said"),
        [
            pytest.param([("lead/", b"", 0o40755)], "holds nothing", id="only-the-leading-directory"),
            pytest.param([("loose.txt", b"x", 0o100644)], "at its root", id="a-file-at-the-root"),
            pytest.param([("lead/../../escape.txt", b"x", 0o100644)], "outside", id="dot-dot"),
            pytest.param(
                [("lead/a.txt", b"x", 0o100644), ("other/b.txt", b"x", 0o100644)],
                "beside",
                id="two-leading-directories",
            ),
            pytest.param(
                [("lead/", b"", 0o40755), ("lead/npm", b"../lib/npm-cli.js", 0o120777)], "symbolic link", id="a-link"
            ),
        ],
    )
    def test_a_zip_the_rule_cannot_place_is_refused_and_the_previous_tree_stays(
        self, tmp_path: Path, entries, said
    ) -> None:
        destination = tmp_path / "home" / "runtime" / "node"
        destination.mkdir(parents=True)
        (destination / "keep.txt").write_bytes(b"the previous tree")
        archive = _write_zip(tmp_path / "node.zip", entries)
        before = _tree_outside(tmp_path, destination)
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.unpack_payload(str(archive), str(destination))
        assert said in str(excinfo.value)
        assert (destination / "keep.txt").read_bytes() == b"the previous tree"
        assert not Path(str(destination) + _INSTALL.UNPACKING_SUFFIX).exists()
        assert _tree_outside(tmp_path, destination) == before

    def test_a_zip_that_is_not_a_zip_is_one_sentence(self, tmp_path: Path) -> None:
        archive = tmp_path / "node.zip"
        archive.write_bytes(b"PK but not really a zip")
        destination = tmp_path / "runtime" / "node"
        destination.mkdir(parents=True)
        (destination / "keep.txt").write_bytes(b"the previous tree")
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.unpack_payload(str(archive), str(destination))
        assert "could not be unpacked" in str(excinfo.value) and str(excinfo.value).count("\n") == 0
        assert (destination / "keep.txt").read_bytes() == b"the previous tree"

    @pytest.mark.parametrize("damage", ["garbage", "truncated"])
    def test_a_damaged_payload_is_one_sentence_and_the_previous_tree_stays(self, tmp_path: Path, damage) -> None:
        archive = tmp_path / "payload.tar.xz"
        if damage == "garbage":
            archive.write_bytes(b"this is not an archive at all")
        else:
            entries = [("dir", "payload", b"", 0o755), ("file", "payload/big.js", os.urandom(200_000), 0o644)]
            whole = _write_tar(tmp_path / "whole.tar.xz", entries).read_bytes()
            archive.write_bytes(whole[: len(whole) // 2])
        destination = tmp_path / "runtime" / "payload"
        destination.mkdir(parents=True)
        (destination / "keep.txt").write_bytes(b"the previous tree")
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.unpack_payload(str(archive), str(destination))
        message = str(excinfo.value)
        assert "could not be unpacked" in message and "`current`" in message
        assert message.count("\n") == 0, "one sentence"
        assert (destination / "keep.txt").read_bytes() == b"the previous tree"
        assert not Path(str(destination) + _INSTALL.UNPACKING_SUFFIX).exists()

    def test_the_uninstall_and_the_removal_take_the_runtime_with_the_version(self, tmp_path: Path) -> None:
        """No new code: the ``remove`` step's ``remove_tree`` already takes the whole version directory."""
        version = tmp_path / "SWCraft" / "versions" / "0.6.0"
        _INSTALL.unpack_payload(
            str(_write_tar(tmp_path / "node.tar.xz", _node_shaped())), str(version / "runtime" / "node")
        )
        (version / "bin").mkdir()
        (version / "bin" / "node.exe").write_bytes(_NODE_SCRIPT)
        _INSTALL.remove_tree(str(version))
        assert not version.exists()
        _INSTALL.remove_tree(str(tmp_path / "SWCraft"))
        assert not (tmp_path / "SWCraft").exists()

    def test_a_link_in_the_version_is_removed_and_never_followed(self, tmp_path: Path) -> None:
        """A link inside a version directory is removed as a link; its target outside stays."""
        skip_without_symlink_support(tmp_path)
        version = tmp_path / "SWCraft" / "versions" / "0.6.0"
        _INSTALL.unpack_payload(
            str(_write_tar(tmp_path / "node.tar.xz", _node_shaped())), str(version / "runtime" / "node")
        )
        (version / "bin").mkdir()
        outside = tmp_path / "outside-node"
        outside.write_bytes(b"not the installation's")
        os.symlink(os.path.join("..", "runtime", "node", "bin", "node"), version / "bin" / "node")
        os.symlink(outside, version / "bin" / "stray")
        _INSTALL.remove_tree(str(version))
        assert not version.exists()
        assert outside.read_bytes() == b"not the installation's", "a link is removed, never followed"


class TestNodeIsPlaced:
    """:func:`install.place_node` on real files -- a link on POSIX and a copy on Windows."""

    @staticmethod
    def _installation(tmp_path: Path) -> Path:
        home = tmp_path / "SWCraft"
        version = home / "versions" / _VERSION
        (version / "runtime" / "node" / "bin").mkdir(parents=True)
        (version / "runtime" / "node" / "bin" / "node").write_bytes(_NODE_SCRIPT)
        (version / "bin").mkdir()
        return home

    def test_the_posix_link_is_relative_and_resolves_through_current(self, tmp_path: Path) -> None:
        skip_without_symlink_support(tmp_path)
        home = self._installation(tmp_path)
        version = home / "versions" / _VERSION
        os.symlink(version, home / "current", target_is_directory=True)
        target = _INSTALL.node_placement_target(PurePosixPath("/unused") / "versions" / _VERSION, "posix")
        _INSTALL.place_node(str(version / "bin" / "node"), target, "posix")
        assert os.readlink(version / "bin" / "node") == "../runtime/node/bin/node"
        through_current = home / "current" / "bin" / "node"
        assert through_current.resolve() == (version / "runtime" / "node" / "bin" / "node").resolve()
        assert through_current.read_bytes() == _NODE_SCRIPT

    def test_a_repair_replaces_the_link_the_previous_run_left(self, tmp_path: Path) -> None:
        skip_without_symlink_support(tmp_path)
        version = self._installation(tmp_path) / "versions" / _VERSION
        os.symlink("../somewhere/else", version / "bin" / "node")
        _INSTALL.place_node(str(version / "bin" / "node"), "../runtime/node/bin/node", "posix")
        assert os.readlink(version / "bin" / "node") == "../runtime/node/bin/node"

    def test_a_payload_without_bin_node_is_refused_and_leaves_no_link(self, tmp_path: Path) -> None:
        skip_without_symlink_support(tmp_path)
        version = self._installation(tmp_path) / "versions" / _VERSION
        (version / "runtime" / "node" / "bin" / "node").unlink()
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.place_node(str(version / "bin" / "node"), "../runtime/node/bin/node", "posix")
        assert "does not carry" in str(excinfo.value)
        assert not os.path.lexists(version / "bin" / "node")

    def test_a_directory_where_node_goes_is_refused(self, tmp_path: Path) -> None:
        version = self._installation(tmp_path) / "versions" / _VERSION
        (version / "bin" / "node").mkdir()
        (version / "bin" / "node" / "precious.txt").write_bytes(b"somebody's")
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.place_node(str(version / "bin" / "node"), "../runtime/node/bin/node", "posix")
        assert "is a directory" in str(excinfo.value)
        assert (version / "bin" / "node" / "precious.txt").read_bytes() == b"somebody's"

    def test_the_windows_copy_carries_the_payloads_bytes(self, tmp_path: Path) -> None:
        """Asserted on this host's filesystem, which is all a copy needs: the bytes, not the image."""
        version = tmp_path / "SWCraft" / "versions" / _VERSION
        (version / "runtime" / "node").mkdir(parents=True)
        (version / "Scripts").mkdir()
        payload = b"MZ" + os.urandom(4096)
        (version / "runtime" / "node" / "node.exe").write_bytes(payload)
        (version / "Scripts" / "node.exe").write_bytes(b"the previous run's copy")
        source = str(version / "runtime" / "node" / "node.exe")
        _INSTALL.place_node(str(version / "Scripts" / "node.exe"), source, "nt")
        assert (version / "Scripts" / "node.exe").read_bytes() == payload
        assert not (version / "Scripts" / "node.exe").is_symlink(), "a copy, never a link"

    def test_a_windows_copy_with_no_source_is_one_sentence(self, tmp_path: Path) -> None:
        (tmp_path / "Scripts").mkdir()
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.place_node(str(tmp_path / "Scripts" / "node.exe"), str(tmp_path / "missing" / "node.exe"), "nt")
        assert "could not be placed" in str(excinfo.value)
        assert str(excinfo.value).count("\n") == 0


class TestTheLinkIsReplacedNeverTheDirectory:
    """``replace_link`` on the platform the suite runs on.

    The Windows first spelling -- ``_winapi.CreateJunction``, with ``mklink /J`` behind it -- is made unavailable on
    every host (:func:`_without_the_first_spelling`), because on the Windows runner it works and would end a case
    before the chain it is about; the archive install's ``artifact`` cases measure it. Asserted here is the rule
    governing both halves: an existing link is removed and remade, and a real directory in the link's place is a
    refusal rather than an ``rmtree``.

    The cases that hand ``replace_link`` the ``"posix"`` platform reach ``os.symlink`` inside the installer, which
    Windows performs only with ``SeCreateSymbolicLinkPrivilege`` (else ``WinError 1314``), so they are guarded by
    :func:`symlink_support.skip_without_symlink_support` rather than by a platform check.
    """

    def test_an_existing_link_is_replaced(self, tmp_path: Path) -> None:
        skip_without_symlink_support(tmp_path)
        first = tmp_path / "versions" / "0.6.0"
        second = tmp_path / "versions" / "0.7.0"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        current = tmp_path / "current"
        _INSTALL.replace_link(str(current), str(first), "posix")
        _INSTALL.replace_link(str(current), str(second), "posix")
        assert current.is_symlink()
        assert current.resolve() == second.resolve()

    def test_a_real_directory_in_the_links_place_is_refused_by_name(self, tmp_path: Path) -> None:
        current = tmp_path / "current"
        current.mkdir()
        (current / "keepme.txt").write_text("not the installer's\n", encoding="utf-8", newline="\n")
        target = tmp_path / "versions" / "0.7.0"
        target.mkdir(parents=True)
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.replace_link(str(current), str(target), "posix")
        assert "current" in str(excinfo.value)
        assert (current / "keepme.txt").is_file(), "a refusal deletes nothing"

    @staticmethod
    def _as_a_junction(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
        """Make *path* -- a real, empty directory -- read the way a Windows junction reads.

        ``os.path.islink`` says no and ``os.lstat`` carries the mount-point reparse tag, stated on a host where no
        junction can be made. Only *path* is affected.
        """
        real_lstat = os.lstat

        class _JunctionStat:
            """The real directory's stat with the junction's tag on top -- and only while it is a directory."""

            st_reparse_tag = _INSTALL._JUNCTION_REPARSE_TAG

            def __init__(self, real) -> None:
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

        def lstat(target, *args, **kwargs):
            result = real_lstat(target, *args, **kwargs)
            if os.fspath(target) == str(path) and stat.S_ISDIR(result.st_mode):
                return _JunctionStat(result)
            return result

        monkeypatch.setattr(_INSTALL.os, "lstat", lstat)

    def test_a_junction_is_a_link_and_a_directory_is_not(self, tmp_path: Path, monkeypatch) -> None:
        junction = tmp_path / "current"
        junction.mkdir()
        plain = tmp_path / "plain"
        plain.mkdir()
        self._as_a_junction(monkeypatch, junction)
        assert _INSTALL.is_link(str(junction))
        assert not _INSTALL.is_link(str(plain))
        assert not _INSTALL.is_link(str(tmp_path / "absent"))

    def test_a_junction_in_the_links_place_is_replaced_not_refused(self, tmp_path: Path, monkeypatch) -> None:
        """A repair on Windows replaces the junction the first install made rather than refusing it.

        ``os.remove`` refuses a directory, so the removal has to reach ``os.rmdir``, the spelling that takes a junction
        on Windows.
        """
        skip_without_symlink_support(tmp_path)
        target = tmp_path / "versions" / _VERSION
        target.mkdir(parents=True)
        current = tmp_path / "current"
        current.mkdir()
        self._as_a_junction(monkeypatch, current)
        _INSTALL.replace_link(str(current), str(target), "posix")
        assert current.is_symlink() and current.resolve() == target.resolve()

    def test_remove_tree_removes_a_junction_and_never_what_it_points_at(self, tmp_path: Path, monkeypatch) -> None:
        current = tmp_path / "current"
        current.mkdir()
        self._as_a_junction(monkeypatch, current)
        removed: list[str] = []
        monkeypatch.setattr(_INSTALL.shutil, "rmtree", lambda path, *a, **k: removed.append(("rmtree", path)))
        _INSTALL.remove_tree(str(current))
        assert removed == [], "a link is removed as a link, never walked"
        assert not current.exists()

    def test_a_cmd_exe_that_cannot_be_started_still_reaches_the_third_spelling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``cmd.exe`` that cannot be started still lets the chain reach ``os.symlink``.

        The middle spelling runs through ``run_argv``, which raises an :class:`InstallError` for a program that will
        not start; ``replace_link``'s ``except InstallError: raise`` once let that escape, so the ``os.symlink``
        fallback was unreachable on any host without a working ``cmd.exe``. Both Windows spellings are made to fail as
        on such a host: ``_winapi`` unimportable and ``subprocess.run`` answering ``FileNotFoundError`` for
        ``cmd.exe``. The third spelling is a real symlink, so a host that refuses one skips.
        """
        _without_the_first_spelling(monkeypatch)
        monkeypatch.setattr(_INSTALL.subprocess, "run", _unstartable)
        target = tmp_path / "versions" / _VERSION
        target.mkdir(parents=True)
        symlink_or_skip(tmp_path / "probe", target)
        link = tmp_path / "current"
        _INSTALL._create_windows_junction(str(link), str(target))
        assert link.is_symlink() and link.resolve() == target.resolve()

    def test_when_no_spelling_works_the_refusal_names_all_three(self, tmp_path: Path) -> None:
        """The fallback chain is only a chain if its report says what each one did."""
        target = tmp_path / "versions" / _VERSION
        target.mkdir(parents=True)
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory\n", encoding="utf-8", newline="\n")
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL._create_windows_junction(str(blocked / "current"), str(target))
        message = str(excinfo.value)
        assert "CreateJunction" in message and "mklink /J" in message and "os.symlink" in message


class TestTheLinkDispatchAndTheMklinkResult:
    """The link dispatch and the ``mklink`` result, both pinned against a flipped operator.

    ``replace_link``'s ``if platform == "nt"`` and ``_create_windows_junction``'s ``if code == 0`` survived operator
    mutation: the class above reaches ``replace_link`` on ``"posix"`` only, and no case gave ``mklink`` an exit code.
    The dispatch is asserted by recording which link maker ran; the ``mklink`` result by stubbing :func:`run_argv`,
    the only thing between this file and a process. Not reached: Windows -- every case starts one spelling down the
    chain, and what a real ``mklink /J`` does is not observed here.
    """

    @staticmethod
    def _record_symlink(monkeypatch: pytest.MonkeyPatch) -> list:
        made: list = []
        monkeypatch.setattr(_INSTALL.os, "symlink", lambda *args, **kwargs: made.append(args))
        return made

    def test_windows_links_through_the_junction_and_posix_does_not(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        junctions: list = []
        monkeypatch.setattr(_INSTALL, "_create_windows_junction", lambda path, target: junctions.append((path, target)))
        made = self._record_symlink(monkeypatch)
        target = tmp_path / "versions" / _VERSION
        target.mkdir(parents=True)
        link = tmp_path / "current"

        _INSTALL.replace_link(str(link), str(target), "nt")

        assert junctions == [(str(link), str(target))]
        assert made == [], "the Windows branch does not also call os.symlink"

    def test_posix_links_through_os_symlink_and_never_the_junction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        junctions: list = []
        monkeypatch.setattr(_INSTALL, "_create_windows_junction", lambda path, target: junctions.append((path, target)))
        made = self._record_symlink(monkeypatch)
        target = tmp_path / "versions" / _VERSION
        target.mkdir(parents=True)
        link = tmp_path / "current"

        _INSTALL.replace_link(str(link), str(target), "posix")

        assert junctions == []
        assert made == [(str(target), str(link))]

    def test_a_mklink_that_exits_zero_is_the_answer(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`mklink /J` succeeded, so the third spelling must not run."""
        monkeypatch.setattr(_INSTALL, "run_argv", lambda argv: 0)
        made = self._record_symlink(monkeypatch)
        target = tmp_path / "versions" / _VERSION
        target.mkdir(parents=True)

        _INSTALL._create_windows_junction(str(tmp_path / "current"), str(target))

        assert made == [], "a successful mklink is the answer; os.symlink is not tried after it"

    def test_a_mklink_that_exits_non_zero_falls_through_and_names_the_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """…and one that ran and failed is an *attempt*, with its code in the report."""
        _without_the_first_spelling(monkeypatch)
        monkeypatch.setattr(_INSTALL, "run_argv", lambda argv: 3)

        def refuse(*args, **kwargs):
            raise OSError(1, "Operation not permitted")

        monkeypatch.setattr(_INSTALL.os, "symlink", refuse)
        target = tmp_path / "versions" / _VERSION
        target.mkdir(parents=True)

        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL._create_windows_junction(str(tmp_path / "current"), str(target))

        message = str(excinfo.value)
        assert "mklink /J (exit code 3)" in message, message
        assert "os.symlink" in message, "the third spelling still ran"


class TestTheExtensionIdIsThePackagesOwn:
    """``EXTENSION_ID`` is a literal, and it must stay the manifest's ``publisher.name``.

    ``install.py`` runs before any wheel is installed and cannot read ``vscode_extension/package.json`` (the file is
    not in the archive). A renamed publisher or extension would make ``--uninstall`` silently leave the extension
    behind, because ``code --uninstall-extension`` on an unknown id is not an error the installer reads as one. Not
    reached: whether ``code`` accepts the id; this is a string comparison.
    """

    def test_it_equals_the_manifests_publisher_and_name(self) -> None:
        manifest = json.loads((REPO_ROOT / "vscode_extension" / "package.json").read_text(encoding="utf-8"))
        assert _INSTALL.EXTENSION_ID == f"{manifest['publisher']}.{manifest['name']}", (
            "`code --uninstall-extension` takes `publisher.name`; a rename here leaves the "
            "extension installed after `--uninstall` and says nothing"
        )


class TestTheInterpreterIsAsked:
    """``interpreter_stamp`` -- the real seam, run against real interpreters.

    This is the one seam in the file that runs a subprocess on the host the suite
    runs on, so what it witnesses is this host's interpreters. It does not reach
    Windows, ``py -3.11`` or an interpreter this host does not carry.
    """

    def test_the_running_interpreter_answers_for_itself(self) -> None:
        assert _INSTALL.interpreter_stamp([sys.executable]) == _INSTALL.current_stamp()

    def test_an_empty_argv_falls_back_to_the_running_interpreter(self) -> None:
        assert _INSTALL.interpreter_stamp(()) == _INSTALL.current_stamp()

    def test_an_interpreter_that_is_not_there_is_one_sentence(self) -> None:
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.interpreter_stamp(["/nonexistent/python3.11"])
        assert "/nonexistent/python3.11" in str(excinfo.value)

    def test_a_program_that_answers_nothing_is_one_sentence(self) -> None:
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.interpreter_stamp([sys.executable, "-c", "print('hello')", "--"])
        assert "--python" in str(excinfo.value)


class TestNeitherProbeCanHang:
    """Neither question this file asks a subprocess can hang.

    ``interpreter_stamp`` and ``venv_is_usable`` start an interpreter to ask it something. ``check-platform`` runs on
    every non-``--index-url`` install, so a ``--python`` that starts and blocks (a wrapper that prompts, a shim
    reading stdin) would hang the installer silently; under the VS Code extension the child's stdin is a pipe nobody
    writes, there is no cancellation, and the in-flight guard then refuses both toolchain commands for the life of
    the window. So both get ``stdin=subprocess.DEVNULL`` and a bound. ``run_argv`` is the run, not a question, so it
    gets no bound; it keeps the inherited stdin only with a log-free run (``--verbose``, ``--uninstall``) and gets
    ``DEVNULL`` when its output goes to ``install.log`` (``TestTheLog``).

    Not reached: a probe that answers slowly but within the bound still costs that time.
    """

    #: Sleeps 30 s, bounded by a shortened `PROBE_TIMEOUT_SECONDS`: a real blocked subprocess, run in under a second.
    _BLOCKS = "import time; time.sleep(30)"

    def test_an_interpreter_that_starts_and_blocks_is_one_sentence(self, monkeypatch) -> None:
        monkeypatch.setattr(_INSTALL, "PROBE_TIMEOUT_SECONDS", 0.5)
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.interpreter_stamp((sys.executable, "-c", self._BLOCKS))
        message = str(excinfo.value)
        assert sys.executable in message and "--python" in message

    def test_a_venv_interpreter_that_blocks_is_not_usable(self, monkeypatch, tmp_path: Path) -> None:
        """`plan_install` reads this as "rebuild it", which is the recoverable answer."""
        monkeypatch.setattr(_INSTALL, "PROBE_TIMEOUT_SECONDS", 0.5)
        blocking = tmp_path / "python"
        blocking.write_text(
            f"#!/bin/sh\nexec {sys.executable} -c 'import time; time.sleep(30)'\n",
            encoding="utf-8",
            newline="\n",
        )
        blocking.chmod(0o755)
        assert _INSTALL.venv_is_usable(str(blocking)) is False

    def test_neither_probe_inherits_stdin(self, monkeypatch) -> None:
        """A probe can never read from the console: a prompting wrapper would sit until the bound expires.

        Under the extension there is no console to answer it at all.
        """
        calls = []
        real = _INSTALL.subprocess.run

        def record(argv, **kwargs):
            calls.append(kwargs)
            return real([sys.executable, "-c", "print()"], **kwargs)

        monkeypatch.setattr(_INSTALL.subprocess, "run", record)
        with contextlib.suppress(_INSTALL.InstallError):
            _INSTALL.interpreter_stamp((sys.executable,))
        _INSTALL.venv_is_usable(sys.executable)

        assert len(calls) == 2, "both probes ran"
        for kwargs in calls:
            assert kwargs["stdin"] is _INSTALL.subprocess.DEVNULL
            assert kwargs["timeout"] == _INSTALL.PROBE_TIMEOUT_SECONDS


class TestTheVenvIsProbed:
    def test_a_directory_with_no_interpreter_is_not_usable(self, tmp_path: Path) -> None:
        assert _INSTALL.venv_is_usable(str(tmp_path / "bin" / "python")) is False

    def test_the_running_interpreter_is_usable(self) -> None:
        assert _INSTALL.venv_is_usable(sys.executable) is True


class TestOsErrorsAreSentences:
    """An ``OSError`` from a spawn or a link is a sentence, not a traceback."""

    def test_a_program_that_cannot_be_started_names_itself(self) -> None:
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.run_argv(["/nonexistent/python3.11", "-m", "venv", "/tmp/x"])
        assert "/nonexistent/python3.11" in str(excinfo.value)

    def test_a_link_that_cannot_be_made_names_both_paths(self, tmp_path: Path) -> None:
        """A link whose parent is a *file* -- `os.symlink` raises `NotADirectoryError`."""
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory\n", encoding="utf-8", newline="\n")
        target = tmp_path / "versions" / _VERSION
        target.mkdir(parents=True)
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.replace_link(str(blocked / "current"), str(target), "posix")
        assert "current" in str(excinfo.value) and str(target) in str(excinfo.value)


class TestTheCommandInterpreterIsHandedNothingItWouldReParse:
    """``cmd.exe /c`` re-parses its command line, so no step hands it a metacharacter.

    The two ``cmd.exe`` call sites are argv lists, so no shell is involved, but ``subprocess`` joins a list on Windows
    with :func:`subprocess.list2cmdline`, which quotes for the C runtime's rules -- an argument containing a space, a
    tab or a quote, and nothing else. After ``/c``, ``cmd.exe`` re-reads the remainder with its own parser, in which
    ``&``, ``|``, ``<``, ``>``, ``^`` and ``%`` are structure: a ``code.cmd`` under ``C:\\a&b\\`` or a ``--home``
    there would split into two commands, the second a program nobody named.

    The refusal is :func:`run_argv`'s, the one function through which a step's argv reaches the operating system;
    both call sites catch what it raises and have their answer for a ``cmd.exe`` that will not run (the junction
    falls through to its third spelling; the ``vsix`` step reports and leaves the toolchain installed). The probes
    that spawn directly cannot name ``cmd.exe``; :class:`TestTheSpawnSitesAreEnumerated` pins the spawn sites by
    name.

    Not reached: Windows. The refusal is reasoned from ``cmd``'s documented parser and ``list2cmdline``'s source, not
    observed. Whether a quoted argument would have been safe is deliberately not relied on: staking correctness on
    the interaction of two quoting rules on a platform nothing here runs is what the refusal replaces.
    """

    def _recorded(self, monkeypatch) -> list:
        started: list[tuple] = []

        class _Completed:
            returncode = 0

        def record(argv, **kwargs):
            started.append(tuple(argv))
            return _Completed()

        monkeypatch.setattr(_INSTALL.subprocess, "run", record)
        return started

    def test_a_cmd_argument_with_a_separator_starts_nothing(self, monkeypatch) -> None:
        started = self._recorded(monkeypatch)
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.run_argv(["cmd.exe", "/c", r"C:\a&b\code.cmd", "--install-extension", "x.vsix"])
        assert started == [], "the whole point: nothing is handed to CreateProcess"
        message = str(excinfo.value)
        assert r"C:\a&b\code.cmd" in message and "&" in message
        assert "cmd.exe" in message

    @pytest.mark.parametrize("character", list(_INSTALL.CMD_REPARSED_CHARACTERS))
    def test_every_character_cmd_re_reads_is_refused(self, monkeypatch, character: str) -> None:
        """Parametrized over the constant, not a copy of its members, so the case count follows it.

        Not reached: whether the constant is complete -- that ``(`` and ``)`` are rightly absent is decided beside the
        constant and defused by :func:`code_argv`'s ``call``.
        """
        self._recorded(monkeypatch)
        with pytest.raises(_INSTALL.InstallError):
            _INSTALL.run_argv(["cmd.exe", "/c", "C:\\tools\\a" + character + "b\\code.cmd"])

    def test_the_constant_is_the_one_the_refusal_sentence_offers(self, monkeypatch) -> None:
        """The sentence tells the user which characters to avoid; it must be the constant."""
        self._recorded(monkeypatch)
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL.run_argv(["cmd.exe", "/c", r"C:\a&b\code.cmd"])
        assert _INSTALL.CMD_REPARSED_CHARACTERS in str(excinfo.value)

    def test_no_character_may_leave_the_constant_unnoticed(self) -> None:
        """The constant may grow but never drop below the characters ``cmd`` treats as structure.

        Deleting a character makes a parametrized case disappear, which fails nothing, so the floor is asserted as a
        subset, each character with the role ``cmd /?`` gives it. Not reached: whether the set is the right floor --
        it is read from ``cmd``'s documentation, not measured.
        """
        roles = {
            "&": "separates two commands",
            "|": "pipes one command into the next",
            "<": "redirects input",
            ">": "redirects output",
            "^": "escapes the character after it",
            "%": "starts a variable reference",
            '"': "opens a quoted region",
        }
        missing = sorted(character for character in roles if character not in _INSTALL.CMD_REPARSED_CHARACTERS)
        assert missing == [], "".join(f"{character} {roles[character]}; " for character in missing)

    def test_the_refusal_is_the_one_the_two_call_sites_already_catch(self, monkeypatch) -> None:
        """`ProgramWillNotStart`, so `_reason_of` gives the reason without the sentence.

        Both callers embed the reason in a sentence of their own -- "mklink /J
        (...)" and "`code` could not be started (...)" -- and a plain
        `InstallError` would nest a whole sentence inside each.
        """
        self._recorded(monkeypatch)
        with pytest.raises(_INSTALL.ProgramWillNotStart) as excinfo:
            _INSTALL.run_argv(["cmd.exe", "/c", r"C:\a&b\code.cmd"])
        assert excinfo.value.program == "cmd.exe"
        assert "&" in excinfo.value.reason
        assert not excinfo.value.reason.endswith("."), "a reason, not a sentence"

    def test_the_same_characters_reach_a_program_that_is_not_cmd(self, monkeypatch) -> None:
        """The guard is about ``cmd``'s parser, not about paths.

        ``pip``, ``python -m venv`` and a POSIX ``code`` are handed to the operating
        system with their arguments intact, and a ``&`` in a POSIX path is an
        ordinary character. Refusing it everywhere would refuse installs that work.
        """
        started = self._recorded(monkeypatch)
        assert _INSTALL.run_argv(["/usr/local/bin/code", "--install-extension", "/tmp/a&b.vsix"]) == 0
        assert started == [("/usr/local/bin/code", "--install-extension", "/tmp/a&b.vsix")]

    def test_a_windows_code_with_no_metacharacter_is_still_run(self, monkeypatch) -> None:
        started = self._recorded(monkeypatch)
        assert _INSTALL.run_argv(["cmd.exe", "/c", _WIN_CODE, "--version"]) == 0
        assert started == [("cmd.exe", "/c", _WIN_CODE, "--version")]

    def test_the_junction_records_it_as_an_attempt_and_reaches_the_third_spelling(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """The ``mklink /J`` fallback carries ``--home`` into ``cmd``, so an offending target is refused there.

        A refused attempt belongs in the list of attempts, and ``os.symlink`` -- the Developer-Mode spelling -- still
        runs; the refusal comes before ``cmd.exe`` would have been looked for.
        """
        started = self._recorded(monkeypatch)
        _without_the_first_spelling(monkeypatch)
        target = tmp_path / "versions" / _VERSION
        target.mkdir(parents=True)
        symlink_or_skip(tmp_path / "probe", target)
        link = tmp_path / "cur&rent"
        _INSTALL._create_windows_junction(str(link), str(target))
        assert link.is_symlink() and link.resolve() == target.resolve()
        assert started == [], "the second spelling was refused, not attempted"

    def test_the_junction_refusal_names_the_character_among_the_three_attempts(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        self._recorded(monkeypatch)
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory\n", encoding="utf-8", newline="\n")
        target = tmp_path / "versions" / _VERSION
        target.mkdir(parents=True)
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL._create_windows_junction(str(blocked / "cur&rent"), str(target))
        message = str(excinfo.value)
        assert "mklink /J" in message and "os.symlink" in message
        assert "re-reads" in message, "the attempt says why it was not attempted"

    def test_the_vsix_step_reports_it_and_leaves_the_toolchain_installed(self, monkeypatch) -> None:
        """The ``.vsix`` path lies wherever the user unpacked the archive: a refusal there keeps the toolchain.

        The step's own refusal already says the toolchain is installed and the extension is not, the sentence the
        extension matches.
        """
        started = self._recorded(monkeypatch)
        step = _INSTALL.Step(
            "vsix",
            argv=("cmd.exe", "/c", _WIN_CODE, "--install-extension", r"C:\D&D\swcraft.vsix"),
            path=r"C:\D&D\swcraft.vsix",
        )
        echoed: list[str] = []
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            _INSTALL._install_extension(step, _INSTALL.run_argv, echoed.append)
        message = str(excinfo.value)
        assert started == []
        assert "the toolchain is installed and the extension is not" in message
        assert r"C:\D&D\swcraft.vsix" in message


class TestTheDurableWindowsPathIsTheAuthority:
    """On Windows the durable ``PATH`` decides "added once", in both directions.

    Planning the ``path`` step only when the process ``PATH`` lacked the entry meant a terminal that already had it
    (an earlier install's, a shell started from VS Code, a re-run) never read ``HKCU\\Environment``, and the durable
    value a future terminal reads was never written. A comparison that did not trim the quotes and padding a Windows
    ``PATH`` routinely carries read ``"C:\\...\\Scripts"`` as a different directory and appended it again.

    So on Windows the plan always carries the step and the registry decides, because it is the only value that
    outlives the run. On POSIX nothing durable is read: the step prints a line for a shell profile, and the process
    ``PATH`` suppresses it. Not reached: the registry itself -- every case drives the injected seam.
    """

    _ENTRY = str(PureWindowsPath(_WIN_HOME) / "current" / "Scripts")

    def test_a_process_path_that_has_it_does_not_stop_the_registry_edit(self) -> None:
        """The defect itself: the two values disagree and the durable one loses."""
        journal = Journal(value=(r"C:\Windows", Journal.REG_EXPAND_SZ))
        journal.execute(_windows_plan(env={"PATH": r"C:\Windows;" + self._ENTRY}), "nt")
        assert [call for call in journal.calls if call[0] == "registry-write"] == [
            ("registry-write", r"C:\Windows;" + self._ENTRY, Journal.REG_EXPAND_SZ)
        ]

    def test_a_registry_that_already_lists_it_is_read_and_not_written(self) -> None:
        """The other half of "once": the guarantee is kept where the value lives."""
        journal = Journal(value=(r"C:\Windows;" + self._ENTRY, Journal.REG_EXPAND_SZ))
        journal.execute(_windows_plan(), "nt")
        assert journal.kinds.count("registry-read") == 1
        assert "registry-write" not in journal.kinds and "registry-broadcast" not in journal.kinds

    def test_a_quoted_entry_in_the_registry_is_the_same_entry(self) -> None:
        journal = Journal(value=(r'C:\Windows;"' + self._ENTRY + '"', Journal.REG_EXPAND_SZ))
        journal.execute(_windows_plan(), "nt")
        assert "registry-write" not in journal.kinds

    def test_a_padded_entry_in_the_registry_is_the_same_entry(self) -> None:
        journal = Journal(value=(r"C:\Windows; " + self._ENTRY + " ", Journal.REG_EXPAND_SZ))
        journal.execute(_windows_plan(), "nt")
        assert "registry-write" not in journal.kinds

    def test_a_quoted_entry_is_removed_by_the_uninstall(self) -> None:
        """The same normalization, read by ``removed_path`` -- so a quoted entry goes."""
        quoted = '"' + self._ENTRY + '"'
        assert _INSTALL.removed_path(r"C:\Windows;" + quoted + r";C:\tools", self._ENTRY, "nt") == (
            r"C:\Windows;C:\tools"
        )

    def test_the_posix_comparison_trims_nothing(self) -> None:
        """A leading space is part of a POSIX directory name, so it is a different one.

        Trimming is a Windows reading -- its ``PATH`` parser tolerates quotes and padding, and a directory name cannot
        end in a space or a period. On POSIX it would decide that ``/home/dev/ .swcraft`` is already there.
        """
        entry = str(PurePosixPath(_POSIX_HOME) / "current" / "bin")
        assert _INSTALL.appended_path("/usr/bin: " + entry, entry, "posix") is not None


class TestVsixStep:
    def test_an_unresolved_code_is_reported_and_the_run_continues(self) -> None:
        journal = Journal()
        journal.execute(_windows_plan(found_code=None, installed=("0.6.0",)), "nt")
        assert journal.kinds[-1] == "remove", "the run continued past the report"
        reports = [line for line in journal.echoed if ".vsix" in line]
        assert len(reports) == 1 and "code" in reports[0]
        assert "so the extension was not installed" in reports[0], (
            "the VS Code extension reads this sentence out of the installer's transcript to add "
            "the sideload note the temporary directory would otherwise invalidate "
            "(vscode_extension/src/toolchainInstall.ts, EXTENSION_NOT_INSTALLED_MARKERS). There is "
            "no structured channel between the two, deliberately -- so the fragment is pinned on "
            "both sides, and a reword that reaches only one of them loses the note rather than "
            "producing a wrong one. The leading `so ` is load-bearing: the uninstall direction says "
            "'when the extension was not installed' and must not match"
        )

    def test_a_failing_code_stops_the_run(self) -> None:
        journal = Journal(codes=lambda argv: 1 if argv[0] == "cmd.exe" else 0)
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            journal.execute(_windows_plan(), "nt")
        message = str(excinfo.value)
        assert "extension" in message
        assert "the toolchain is installed and the extension is not" in message, (
            "the second half of the pair the extension matches: this branch and "
            "the one that cannot start `code` both name a `.vsix` inside the archive root, which "
            "for a zip source is a temporary directory the editor deletes when the command ends"
        )

    def test_the_uninstall_sentences_match_none_of_the_install_markers(self) -> None:
        """No uninstall sentence matches either install marker the extension reads.

        ``_uninstall_extension``'s sentences also end in the Extensions view, and one reads "which is also what it
        answers when the extension was not installed"; a broader marker would tell a user who had just removed SWCraft
        to install it.
        """
        markers = ("so the extension was not installed", "the toolchain is installed and the extension is not")
        for codes in (None, lambda argv: 1 if argv[0] == "cmd.exe" else 0, _cannot_be_started("cmd.exe")):
            journal = Journal() if codes is None else Journal(codes=codes)
            journal.execute(_windows_plan("--uninstall", found_code=None if codes is None else _WIN_CODE), "nt")
            for line in journal.echoed:
                for marker in markers:
                    assert marker not in line, line

    def test_the_uninstall_direction_reports_an_absent_code_in_its_own_words(self) -> None:
        """An absent ``code`` on uninstall is reported in the uninstall direction, with no hole for a path.

        The install-direction sentence rendered "install  from the Extensions view" -- the wrong direction, with a
        double space where the ``.vsix`` path would be.
        """
        journal = Journal()
        journal.execute(_windows_plan("--uninstall", found_code=None), "nt")
        reports = [line for line in journal.echoed if "code" in line]
        assert len(reports) == 1
        assert "was not removed" in reports[0]
        assert "install " not in reports[0] and "  " not in reports[0]

    def test_a_failing_uninstall_extension_does_not_fail_the_run(self) -> None:
        """`code --uninstall-extension` exits non-zero for an extension that is not there."""
        journal = Journal(codes=lambda argv: 1 if argv[0] == "cmd.exe" else 0)
        unremoved = journal.execute(_windows_plan("--uninstall"), "nt")
        assert unremoved == ()
        assert [line for line in journal.echoed if "did not remove the extension" in line] != []

    def test_a_code_that_cannot_be_started_does_not_fail_the_uninstall(self) -> None:
        """A ``code`` that resolves at plan time and cannot be started at run time does not fail the uninstall.

        By the time ``vsix-remove`` runs the home is already gone, so an escaping refusal from ``run_argv`` would
        report a failed uninstall after a successful one. The shapes: a dangling shim, a wrapper whose interpreter was
        removed, a removal between the ``which`` and the run.
        """
        journal = Journal(codes=_cannot_be_started("cmd.exe"))
        unremoved = journal.execute(_windows_plan("--uninstall"), "nt")
        assert unremoved == (), "the removal happened; nothing here may report otherwise"
        reports = [line for line in journal.echoed if "could not be started" in line]
        assert len(reports) == 1, "one sentence, not a refusal"
        assert "was not removed" in reports[0] and "Extensions view" in reports[0]

    def test_a_code_that_cannot_be_started_stops_the_install_in_the_right_words(self) -> None:
        """The install direction keeps its refusal and loses ``run_argv``'s sentence.

        ``run_argv``'s wording, "so nothing further was changed", is false here: the toolchain is installed and
        ``current`` already points at it. Same exit code, a sentence the reader can act on.
        """
        journal = Journal(codes=_cannot_be_started("cmd.exe"))
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            journal.execute(_windows_plan(), "nt")
        message = str(excinfo.value)
        assert "nothing further was changed" not in message
        assert "the toolchain is installed and the extension is not" in message
        assert ".vsix" in message, "the remedy names the file to sideload"


# ---------------------------------------------------------------------------
# The compatibility floor: `engines.vscode`, read from the archive's own .vsix
# ---------------------------------------------------------------------------


def _vsix_declaring(path: Path, floor, member: str = _INSTALL.VSIX_MANIFEST_MEMBER) -> str:
    """A real ``.vsix`` -- a zip -- whose manifest declares *floor*, or none at all.

    A real zip, because :func:`install.engine_floor` opens the file the run hands to ``code``; reading it there is what
    keeps a literal in the installer from going stale.
    """
    manifest = {"name": "swcraft-vscode"}
    if floor is not None:
        manifest["engines"] = {"vscode": floor}
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, json.dumps(manifest))
    return str(path)


class TestTheFloorIsReadFromTheVsix:
    """``engines.vscode`` comes out of the file being installed, never out of this file.

    The floor is not SWCraft's own requirement: it is the **highest**
    ``engines.vscode`` among the pack's members and moves when one of them moves. A number
    written into ``install.py`` would be a second declaration, going stale in the
    direction that refuses a host VS Code would have accepted.
    """

    def test_the_declared_floor_is_read(self, tmp_path: Path) -> None:
        assert _INSTALL.engine_floor(_vsix_declaring(tmp_path / "a.vsix", "^1.110.0")) == "^1.110.0"

    def test_no_engines_block_is_not_a_floor(self, tmp_path: Path) -> None:
        assert _INSTALL.engine_floor(_vsix_declaring(tmp_path / "b.vsix", None)) is None

    def test_a_vsix_without_the_manifest_member_is_not_a_floor(self, tmp_path: Path) -> None:
        path = _vsix_declaring(tmp_path / "c.vsix", "^1.110.0", member="extension/other.json")
        assert _INSTALL.engine_floor(path) is None

    def test_a_file_that_is_not_a_zip_is_not_a_floor(self, tmp_path: Path) -> None:
        path = tmp_path / "d.vsix"
        path.write_text("not a zip", encoding="utf-8", newline="\n")
        assert _INSTALL.engine_floor(str(path)) is None

    def test_a_vsix_that_is_not_there_is_not_a_floor(self, tmp_path: Path) -> None:
        assert _INSTALL.engine_floor(str(tmp_path / "absent.vsix")) is None

    def test_no_engine_range_is_written_into_the_installers_code(self) -> None:
        """No caret or ``>=`` range is written into the installer's code: a second copy would go stale silently.

        Not reached, deliberately: prose. Comments are not in the AST and docstrings are excluded by name, because the
        argument for reading the floor out of the ``.vsix`` cannot be made without quoting one.
        """
        tree = ast.parse(_SCRIPT_PATH.read_text(encoding="utf-8"))
        documented = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                first = node.body[0] if node.body else None
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    documented.add(id(first.value))
        ranges = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in documented
            and re.search(r"[\^~]\d+\.\d+\.\d+|>=\s*\d+\.\d+\.\d+", node.value)
        ]
        assert ranges == [], f"the floor is read from the .vsix, never written here: {ranges}"


class TestTheHostVersionIsTheFirstLine:
    """``code --version`` answers three lines and only the first is a version."""

    def test_the_first_line_is_the_version(self) -> None:
        assert _INSTALL.host_version("1.134.0\nf1a4fb1\narm64\n") == "1.134.0"

    @pytest.mark.parametrize("output", ["", None, "\n", "not a version\n1.134.0", "1.134", "v1.134.0"])
    def test_anything_else_is_not_comparable(self, output) -> None:
        assert _INSTALL.host_version(output) is None


class TestTheComparisonIsVsCodesOwn:
    """The same semantics ``healthCheck.ts``'s ``satisfiesEngineRange`` implements.

    Transcribed rather than approximated, and the two rows that make the
    difference are the caret's and the bare range's: ``^1.110.0`` accepts 1.134.0
    and **rejects 2.0.0** (a caret on a non-zero major clears ``minorMustEqual``
    and ``patchMustEqual`` but never ``majorMustEqual``), while a bare ``1.63.0``
    is an exact requirement and not a floor. A ``>=`` approximation gets both
    wrong.
    """

    @pytest.mark.parametrize(
        ("version", "floor", "verdict"),
        [
            ("1.134.0", "^1.110.0", "satisfied"),
            ("1.110.0", "^1.110.0", "satisfied"),
            ("1.109.9", "^1.110.0", "below-floor"),
            ("1.108.0", "^1.110.0", "below-floor"),
            ("2.0.0", "^1.110.0", "below-floor"),
            ("0.9.0", "^1.110.0", "below-floor"),
            ("1.63.0", "1.63.0", "satisfied"),
            ("1.64.0", "1.63.0", "below-floor"),
            ("1.64.0", ">=1.63.0", "satisfied"),
            ("1.62.9", ">=1.63.0", "below-floor"),
            ("1.134.0", "*", "satisfied"),
            ("1.134.0", "^1.x.x", "satisfied"),
        ],
    )
    def test_the_verdict(self, version: str, floor: str, verdict: str) -> None:
        assert _INSTALL.engine_floor_verdict(version, floor) == verdict

    @pytest.mark.parametrize(
        ("version", "floor"),
        [
            ("1.134", "^1.110.0"),
            ("", "^1.110.0"),
            ("1.134.0", "^1.110.0 || ^2.0.0"),
            ("1.134.0", "^1.110.0-20260823"),
            ("1.134.0", ""),
            ("1.134.0", "~1.110.0"),
        ],
    )
    def test_what_it_cannot_read_is_not_a_refusal(self, version: str, floor: str) -> None:
        """``not-comparable`` is a real third answer, never a disguised "too old".

        A compound range, a ``notBefore`` build-date tail and a tilde are outside VS Code's own ``isValidVersion``
        grammar; the caller reads anything but ``below-floor`` as "carry on", because refusing an install on a string
        this did not understand would be worse than not checking.
        """
        assert _INSTALL.engine_floor_verdict(version, floor) == "not-comparable"


class TestTheFloorIsAskedBeforeTheHandover:
    """:func:`install.engine_floor_refusal` -- the ``vsix`` step's one question.

    The order is the subject: the ``.vsix`` is read **first**, with no subprocess,
    so an archive whose extension declares no floor asks ``code`` nothing at all.
    Only a real floor buys a ``code --version``.
    """

    @staticmethod
    def _step(path: str, probe=("code", "--version")):
        return _INSTALL.Step("vsix", argv=("code", "--install-extension", path), path=path, probe=tuple(probe))

    def test_a_host_below_the_floor_is_refused_by_name(self, tmp_path: Path) -> None:
        step = self._step(_vsix_declaring(tmp_path / "e.vsix", "^1.110.0"))
        refusal = _INSTALL.engine_floor_refusal(step, ask=lambda argv: "1.108.0\nf1a4fb1\nx64\n")
        assert refusal is not None
        assert "1.108.0" in refusal and "^1.110.0" in refusal, "both numbers, so the reader can act"
        assert step.path in refusal, "and the file to sideload after the editor is updated"
        assert "the toolchain is installed and the extension is not" in refusal, (
            "the fragment vscode_extension/src/toolchainInstall.ts matches "
            "(EXTENSION_NOT_INSTALLED_MARKERS): this is a run that installed the toolchain and not "
            "the extension, which is exactly what that reading is for"
        )

    def test_a_host_above_the_floor_carries_on(self, tmp_path: Path) -> None:
        step = self._step(_vsix_declaring(tmp_path / "f.vsix", "^1.110.0"))
        assert _INSTALL.engine_floor_refusal(step, ask=lambda argv: "1.134.0\n") is None

    def test_a_code_that_answers_nothing_carries_on(self, tmp_path: Path) -> None:
        """A ``code`` that dies on start answers ``None``, and the install step reports it in its usual words.

        For example an ICU error with exit ``2147483651`` before any check of VS Code's own; this probe must not turn
        that into a second, wronger sentence.
        """
        step = self._step(_vsix_declaring(tmp_path / "g.vsix", "^1.110.0"))
        assert _INSTALL.engine_floor_refusal(step, ask=lambda argv: None) is None

    def test_an_unreadable_floor_asks_code_nothing(self, tmp_path: Path) -> None:
        asked: list = []
        step = self._step(_vsix_declaring(tmp_path / "h.vsix", None))

        def ask(argv):  # pragma: no cover - the point is that it is not called
            asked.append(argv)
            return "1.108.0"

        assert _INSTALL.engine_floor_refusal(step, ask=ask) is None
        assert asked == [], "no floor, no subprocess"

    def test_a_step_without_a_probe_asks_nothing(self, tmp_path: Path) -> None:
        """An unresolved ``code`` leaves both argvs empty, and the skip is the report."""
        step = self._step(_vsix_declaring(tmp_path / "i.vsix", "^1.110.0"), probe=())
        assert _INSTALL.engine_floor_refusal(step, ask=lambda argv: "1.108.0") is None


class TestTheVsixStepAsksTheFloorFirst:
    """The seam, wired through :func:`execute_plan` the way every other one is."""

    @staticmethod
    def _journal_with(refusal):
        journal = Journal()
        journal.floor_calls: list = []

        def floor(step):
            journal.floor_calls.append(step)
            return refusal

        journal.floor = floor
        return journal

    def _execute(self, journal, steps):
        return _INSTALL.execute_plan(
            steps,
            "nt",
            run=journal.run,
            link=journal.link,
            remove=journal.remove,
            registry=journal,
            echo=journal.echo,
            stamp=journal.stamp,
            current=journal.current,
            floor=journal.floor,
        )

    def test_a_refusal_stops_the_run_before_code_is_asked_to_install(self) -> None:
        journal = self._journal_with("VS Code 1.108.0 is older than ^1.110.0 ...")
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            self._execute(journal, _windows_plan(installed=("0.6.0",)))
        assert str(excinfo.value) == "VS Code 1.108.0 is older than ^1.110.0 ..."
        installs = [call for call in journal.calls if call[0] == "run" and "--install-extension" in call]
        assert installs == [], "the whole point: `--install-extension` is never run"
        assert journal.kinds[-1] != "remove", "and the plan stops there, like every other vsix refusal"

    def test_no_refusal_leaves_the_run_exactly_as_it_was(self) -> None:
        journal = self._journal_with(None)
        self._execute(journal, _windows_plan(installed=("0.6.0",)))
        installs = [call for call in journal.calls if call[0] == "run" and "--install-extension" in call]
        assert len(installs) == 1
        assert journal.kinds[-1] == "remove", "the run continued past the extension"

    def test_the_plan_carries_the_probe_and_the_install_for_the_same_code(self) -> None:
        step = [entry for entry in _windows_plan() if entry.kind == "vsix"][0]
        assert step.probe == ("cmd.exe", "/c", "call", _WIN_CODE, "--version")
        assert step.argv[:4] == step.probe[:4], "one resolved `code`, asked twice"

    def test_an_unresolved_code_leaves_both_argvs_empty(self) -> None:
        step = [entry for entry in _windows_plan(found_code=None) if entry.kind == "vsix"][0]
        assert step.argv == () and step.probe == ()

    def test_the_uninstall_step_carries_no_probe(self) -> None:
        """There is no floor question when nothing is being installed."""
        step = [entry for entry in _windows_plan("--uninstall") if entry.kind == "vsix-remove"][0]
        assert step.probe == ()


# ---------------------------------------------------------------------------
# The progress lines: `Progress` and `step_label`
# ---------------------------------------------------------------------------


class Console:
    """The reporter's ``console`` seam as one string, so a line is asserted exactly as a reader sees it."""

    def __init__(self) -> None:
        self.parts: list[str] = []

    def __call__(self, text: str, end: str = "\n") -> None:
        self.parts.append(text + end)

    @property
    def text(self) -> str:
        return "".join(self.parts)

    @property
    def lines(self) -> list[str]:
        return self.text.splitlines()


def _clock(*readings: float):
    """A ``clock`` seam answering *readings* in order -- a step reads it once at its start and once at its end."""
    values = iter(readings)
    return lambda: next(values)


def _stepwise_clock(count: int):
    """Step *i* (1-based) starts at ``100 * i`` and takes exactly *i* seconds."""
    return _clock(*[reading for i in range(1, count + 1) for reading in (100.0 * i, 100.0 * i + i)])


#: A whole install plan per platform -- every kind an install emits: the carried Python, Node.js,
#: two companions, an older version removed and the bootstrap copy removed last.
_EVERY_INSTALL_KIND = {
    "check-platform",
    "runtime",
    "venv",
    "pip",
    "node",
    "link",
    "path",
    "vsix-companion",
    "vsix",
    "remove",
    "bootstrap-remove",
}


def _full_posix_plan(**rest):
    return _posix_plan(installed=("0.6.0",), members=_FULL_LINUX_MEMBERS + _COMPANION_MEMBERS, **rest)


def _full_windows_plan(**rest):
    return _windows_plan(installed=("0.6.0",), members=_FULL_WINDOWS_MEMBERS + _COMPANION_MEMBERS, **rest)


class TestTheStepLabels:
    """:func:`install.step_label` -- one fixed phrase per kind, plus the object it names."""

    def test_every_kind_an_install_emits_is_labelled(self) -> None:
        for plan, platform in ((_full_posix_plan, "posix"), (_full_windows_plan, "nt")):
            steps = plan()
            assert {step.kind for step in steps} == _EVERY_INSTALL_KIND, "the fixture reaches every install kind"
            labels = [_INSTALL.step_label(step, platform) for step in steps]
            home = (PurePosixPath if platform == "posix" else PureWindowsPath)(
                _POSIX_HOME if platform == "posix" else _WIN_HOME
            )
            assert labels == [
                "checking the wheels against this machine",
                "unpacking Python",
                "creating the Python environment",
                _PIP_LABEL,
                "unpacking Node.js",
                "placing node",
                f"pointing current at {_VERSION}",
                "PATH entry" if platform == "posix" else "adding bin to PATH",
                "installing the editor extension redhat.vscode-xml",
                "installing the editor extension redhat.vscode-yaml",
                "installing the SWCraft editor extension",
                f"removing {home / 'versions' / '0.6.0'}",
                "removing the installer's temporary Python",
            ], platform

    def test_every_kind_the_uninstall_emits_is_labelled(self) -> None:
        for plan, platform, home in ((_posix_plan, "posix", _POSIX_HOME), (_windows_plan, "nt", _WIN_HOME)):
            labels = [_INSTALL.step_label(step, platform) for step in plan("--uninstall")]
            assert labels == [
                f"removing {home}",
                "removing bin from PATH",
                "removing the SWCraft editor extension",
                "removing the installer's temporary Python",
            ], platform

    def test_the_runtime_label_is_decided_by_the_payload_member(self) -> None:
        archive = PurePosixPath(_POSIX_ARCHIVE)
        python = _INSTALL.Step("runtime", path=str(archive / _INSTALL.PYTHON_PAYLOAD_MEMBER), target="/x/python")
        assert _INSTALL.step_label(python, "posix") == "unpacking Python"
        for member in _INSTALL.NODE_PAYLOAD_MEMBERS:
            for platform, root in (("posix", archive), ("nt", PureWindowsPath(_WIN_ARCHIVE))):
                node = _INSTALL.Step("runtime", path=str(root.joinpath(*member.split("/"))), target="node")
                assert _INSTALL.step_label(node, platform) == "unpacking Node.js", (member, platform)

    def test_the_pip_label_names_what_is_installed_by_name(self) -> None:
        pip = next(step for step in _posix_plan() if step.kind == "pip")
        assert pip.argv[-2:] == tuple(f"{name}=={_VERSION}" for name in _INSTALL.REQUIREMENTS), (
            "the label's objects are what pip is given"
        )
        assert _INSTALL.step_label(pip, "posix") == "installing swcraft-agent, swcraft-specimens and their dependencies"

    def test_the_label_templates_are_ascii(self) -> None:
        """The label templates are ASCII, because the extension decodes a piped Windows console as UTF-8.

        Asserted on ASCII fixtures, so what is pinned is the fixed text: a label embedding a path or an id carries
        whatever that data holds.
        """
        for plan, platform in ((_full_posix_plan, "posix"), (_full_windows_plan, "nt")):
            steps = list(plan()) + list((_posix_plan if platform == "posix" else _windows_plan)("--uninstall"))
            console = Console()
            progress = _INSTALL.Progress(len(steps), console=console, clock=lambda: 0.0)
            for index, step in enumerate(steps, 1):
                progress.start(index, _INSTALL.step_label(step, platform))
                progress.finish(failed=index % 2 == 0, forward=lambda sentence: None)
            assert console.text.isascii(), platform
            assert "\u2026" not in console.text, "three ASCII dots, never an ellipsis"


class TestTheProgressLines:
    """:class:`install.Progress` inside :func:`install.execute_plan` -- one numbered line per step."""

    def test_a_full_plan_prints_one_line_per_step_with_exact_seconds(self) -> None:
        steps = _full_posix_plan()
        console = Console()
        progress = _INSTALL.Progress(len(steps), console=console, clock=_stepwise_clock(len(steps)))
        journal = Journal()
        journal.execute(steps, "posix", progress=progress)
        total = len(steps)
        numbered = [line for line in console.lines if re.match(r"^\d+/\d+ ", line)]
        assert numbered == [
            f"{i}/{total} {_INSTALL.step_label(step, 'posix')} ... done, {i} s" for i, step in enumerate(steps, 1)
        ]
        assert console.lines == numbered, "the console seam carries the progress lines and nothing else"

    def test_an_instant_step_prints_zero_seconds_and_seconds_are_whole(self) -> None:
        console = Console()
        progress = _INSTALL.Progress(2, console=console, clock=_clock(5.0, 5.2, 10.0, 47.6))
        progress.start(1, "pointing current at 0.7.0")
        progress.finish(failed=False, forward=lambda sentence: None)
        progress.start(2, "installing swcraft-agent and its dependencies")
        progress.finish(failed=False, forward=lambda sentence: None)
        assert console.lines == [
            "1/2 pointing current at 0.7.0 ... done, 0 s",
            "2/2 installing swcraft-agent and its dependencies ... done, 38 s",
        ]

    def test_the_line_is_opened_before_the_step_runs(self) -> None:
        """A reader watching a long pip step sees which step it is: the start is printed, unterminated."""
        console = Console()
        seen: list[str] = []
        journal = Journal(codes=lambda argv: seen.append(console.text) or 0)
        steps = _posix_plan("--no-path", found_code=None)
        journal.execute(steps, "posix", progress=_INSTALL.Progress(len(steps), console=console, clock=lambda: 0.0))
        pip = [index for index, step in enumerate(steps, 1) if step.kind == "pip"][0]
        assert seen[-1].endswith(f"{pip}/{len(steps)} {_PIP_LABEL} ... ")

    def test_a_step_that_raises_closes_its_line_failed_and_the_exception_propagates(self) -> None:
        steps = _posix_plan()
        console = Console()
        journal = Journal(codes=lambda argv: 1 if "pip" in argv else 0)
        with pytest.raises(_INSTALL.InstallError) as excinfo:
            journal.execute(
                steps, "posix", progress=_INSTALL.Progress(len(steps), console=console, clock=_clock(0, 0, 1, 1, 3, 14))
            )
        pip = next(step for step in steps if step.kind == "pip")
        assert str(excinfo.value).startswith(f"`{' '.join(pip.argv)}` failed with exit code 1"), "unchanged"
        assert console.lines[-1] == f"3/{len(steps)} {_PIP_LABEL} ... failed, 11 s"
        assert len(console.lines) == 3, "no line is opened after the failing one"

    def test_a_sentence_prints_after_its_line_never_inside_it(self) -> None:
        """The sentence the step echoes is held until the line is closed, then handed to ``echo`` in order."""
        steps = _posix_plan()
        order: list[str] = []
        journal = Journal()
        journal.echo = lambda line: order.append("echo:" + line)
        progress = _INSTALL.Progress(
            len(steps), console=lambda text, end="\n": order.append("console:" + text + end), clock=lambda: 0.0
        )
        journal.execute(steps, "posix", progress=progress)
        path = [index for index, step in enumerate(steps) if step.kind == "path"][0]
        opened = order.index(f"console:{path + 1}/{len(steps)} PATH entry ... ")
        assert order[opened + 1] == "console:done, 0 s\n", "the line is closed first"
        assert order[opened + 2].startswith("echo:Add this to your shell profile"), "and the sentence follows it"

    def test_the_default_echo_prints_the_posix_path_sentence_once_after_the_closed_line(self, capsys) -> None:
        steps = _posix_plan()
        journal = Journal()
        _INSTALL.execute_plan(
            steps,
            "posix",
            run=journal.run,
            link=journal.link,
            remove=journal.remove,
            stamp=journal.stamp,
            current=journal.current,
            floor=journal.floor,
        )
        lines = capsys.readouterr().out.splitlines()
        sentences = [index for index, line in enumerate(lines) if "Add this to your shell profile" in line]
        assert len(sentences) == 1, lines
        assert lines[sentences[0]].startswith("Add this to your shell profile"), "never inside a progress line"
        assert re.fullmatch(rf"\d+/{len(steps)} PATH entry \.\.\. done, \d+ s", lines[sentences[0] - 1]), lines

    def test_the_non_fatal_outcomes_end_done_with_their_unchanged_sentence(self) -> None:
        """No ``code``, a companion not installed and a ``remove`` left behind are ``done`` plus a sentence."""
        cases = (
            (Journal(), dict(found_code=None), "installing the SWCraft editor extension", "VS Code's `code` command"),
            (
                Journal(codes=lambda argv: 1 if "redhat.vscode-xml.vsix" in " ".join(argv) else 0),
                dict(members=_MEMBERS_WITH_COMPANIONS),
                "installing the editor extension redhat.vscode-xml",
                "`redhat.vscode-xml` was not installed (`code` exited with code 1)",
            ),
            (
                Journal(locked="0.6.0"),
                dict(installed=("0.6.0",)),
                "removing /home/dev/.swcraft/versions/0.6.0",
                "/home/dev/.swcraft/versions/0.6.0 could not be removed",
            ),
        )
        for journal, plan_args, label, sentence in cases:
            steps = _posix_plan("--no-path", **plan_args)
            console = Console()
            journal.execute(steps, "posix", progress=_INSTALL.Progress(len(steps), console=console, clock=lambda: 0.0))
            index = [i for i, step in enumerate(steps, 1) if _INSTALL.step_label(step, "posix") == label][0]
            assert f"{index}/{len(steps)} {label} ... done, 0 s" in console.lines, label
            assert [line for line in journal.echoed if line.startswith(sentence)], (label, journal.echoed)

    def test_a_console_inheriting_run_prints_the_start_whole_and_closes_with_its_own_line(self) -> None:
        """Under ``--verbose`` and ``--uninstall`` a subprocess writes to the console, so no line stays open."""
        steps = _posix_plan("--uninstall")
        console = Console()
        journal = Journal()
        progress = _INSTALL.Progress(
            len(steps), console=console, clock=_stepwise_clock(len(steps)), inherits_console=True
        )
        journal.execute(steps, "posix", progress=progress)
        assert all(part.endswith("\n") for part in console.parts), "every console write ends its line"
        assert console.lines == [
            "1/4 removing /home/dev/.swcraft ...",
            "1/4 done, 1 s",
            "2/4 removing bin from PATH ...",
            "2/4 done, 2 s",
            "3/4 removing the SWCraft editor extension ...",
            "3/4 done, 3 s",
            "4/4 removing the installer's temporary Python ...",
            "4/4 done, 4 s",
        ]

    def test_a_console_inheriting_failure_closes_with_failed(self) -> None:
        console = Console()
        progress = _INSTALL.Progress(3, console=console, clock=_clock(0, 2), inherits_console=True)
        progress.start(2, "installing swcraft-agent and its dependencies", spawns=True)
        progress.finish(failed=True, forward=lambda sentence: None)
        assert console.lines == ["2/3 installing swcraft-agent and its dependencies ...", "2/3 failed, 2 s"]

    def test_a_floor_refusal_closes_the_first_companions_line_failed(self) -> None:
        """SWCraft's own floor is asked inside the first ``vsix-companion`` step."""
        steps = _posix_plan(members=_MEMBERS_WITH_COMPANIONS)
        journal = Journal()
        journal.floor = lambda step: "VS Code 1.108.0 is older than ^1.110.0 ..."
        console = Console()
        with pytest.raises(_INSTALL.InstallError, match="1.108.0"):
            journal.execute(steps, "posix", progress=_INSTALL.Progress(len(steps), console=console, clock=lambda: 0.0))
        first = [i for i, step in enumerate(steps, 1) if step.kind == "vsix-companion"][0]
        assert console.lines[-1] == (
            f"{first}/{len(steps)} installing the editor extension redhat.vscode-xml ... failed, 0 s"
        )

    def test_the_default_progress_counts_the_plan(self, capsys) -> None:
        """With no ``progress`` passed, ``N`` is the plan's length and the journal still sees every sentence."""
        steps = _posix_plan()
        journal = Journal()
        journal.execute(steps, "posix")
        out = capsys.readouterr().out
        assert re.findall(r"^(\d+)/(\d+) ", out, flags=re.MULTILINE) == [
            (str(i), str(len(steps))) for i in range(1, len(steps) + 1)
        ]
        assert journal.kinds == ["stamp", "run", "run", "link", "echo", "run"], "the journal's order is unchanged"


# ---------------------------------------------------------------------------
# main(): the exit codes, and what --uninstall is allowed to touch
# ---------------------------------------------------------------------------


class _FlushFails(io.BytesIO):
    """A log handle whose ``flush`` raises ``OSError`` -- a full disk, a log removed mid-run."""

    def flush(self) -> None:
        raise OSError(28, "No space left on device")


#: The number of lines :func:`install._write_log_header` writes; a test double counts on it.
_HEADER_LINES = 5


class _WritesFailAfterHeader(io.BytesIO):
    """A log handle that takes the header and then raises on every write -- a disk that filled mid-run."""

    def write(self, data) -> int:
        if self.getvalue().count(b"\n") >= _HEADER_LINES:
            raise OSError(28, "No space left on device")
        return super().write(data)


class _LinkRaisesOSError(Journal):
    """A journal whose ``link`` raises a raw ``OSError`` -- a reason no seam turned into a sentence."""

    def link(self, path, target):
        raise OSError(30, "Read-only file system")


#: The fragments the extension reads as "SWCraft's extension was not installed" (toolchainInstall.ts).
_EXTENSION_NOT_INSTALLED_MARKERS = (
    "so the extension was not installed",
    "the toolchain is installed and the extension is not",
)


#: A credential the log must never hold, inside an index URL pip is handed.
_SECRET_INDEX = "https://u:secret@h/simple"

#: A progress line at the start of a console line (``i/N label ...``). A bare substring such as
#: ``"1/"`` is not one: a tmp path in the announcement can hold it.
_PROGRESS_LINE = re.compile(r"(?m)^\d+/\d+ ")


class TestTheLog:
    """``install.log``, ``--verbose`` and the failure tail.

    Every case drives :func:`install.main` with a ``run`` fake of its own -- one that declares ``log=None``, which is
    how a fake asks for the handle (the :class:`Journal` never is bound) -- and the journal's other seams, and hands
    over :func:`install.open_install_log` so the log is a real file under ``tmp_path``.
    """

    @staticmethod
    def _main(tmp_path: Path, *extra: str, run, journal=None, archive_root=None, which=None, **rest) -> int:
        journal = Journal() if journal is None else journal
        rest.setdefault("open_log", _INSTALL.open_install_log)
        return _INSTALL.main(
            ["--home", _posix_home(tmp_path / "SWCraft"), *extra],
            env={"PATH": "/usr/bin"},
            platform="posix",
            which=_which({}) if which is None else which,
            archive_root=archive_root if archive_root is not None else _posix_home(tmp_path / "archive"),
            run=run,
            link=journal.link,
            remove=journal.remove,
            registry=journal,
            echo=journal.echo,
            stamp=journal.stamp,
            current=journal.current,
            unpack=journal.unpack,
            place=journal.place,
            floor=journal.floor,
            clock=lambda: 0.0,
            **rest,
        )

    @staticmethod
    def _index_install(*extra: str) -> tuple[str, ...]:
        return ("--version", _VERSION, "--index-url", _SECRET_INDEX, "--no-path", "--no-vsix", *extra)

    @staticmethod
    def _log_path(tmp_path: Path) -> str:
        return os.path.join(_posix_home(tmp_path / "SWCraft"), _INSTALL.LOG_FILENAME)

    @staticmethod
    def _pip_fails(argv, log=None) -> int:
        if "pip" not in argv:
            return 0
        if log is not None:
            log.write(b"ERROR: no matching distribution\n")
        return 1

    # -- the redaction --------------------------------------------------------
    @pytest.mark.parametrize(
        ("text", "masked"),
        [
            ("--index-url https://u:secret@h/simple x", "--index-url https://***@h/simple x"),
            # pip splits the netloc at the LAST `@`: neither half of `p@ss` may survive.
            ("https://u:p@ss@host/simple", "https://***@host/simple"),
            # A raw `/` in the token: the match runs to the `@`, not to the first `/`.
            ("https://u:ab/cd@h/simple", "https://***@h/simple"),
        ],
    )
    def test_the_userinfo_of_a_url_is_masked(self, text: str, masked: str) -> None:
        assert _INSTALL.redacted(text) == masked

    def test_a_query_string_token_is_not_masked_the_stated_limit(self) -> None:
        text = "https://h/simple?token=abc"
        assert _INSTALL.redacted(text) == text

    def test_a_failed_pip_leaves_its_credential_on_the_console_and_nowhere_in_the_log(
        self, tmp_path: Path, capsys
    ) -> None:
        assert self._main(tmp_path, *self._index_install(), run=self._pip_fails) == 1
        captured = capsys.readouterr()
        logged = (tmp_path / "SWCraft" / _INSTALL.LOG_FILENAME).read_bytes()
        assert b"secret" not in logged
        assert b"https://***@h/simple" in logged, "the header's argv, redacted"
        refusal = captured.err.strip()
        assert refusal.count("\n") == 0 and "failed with exit code 1" in refusal
        assert _SECRET_INDEX in refusal, "the console keeps today's sentence, unredacted"
        assert logged.decode("utf-8").splitlines()[-1] == _INSTALL.redacted(refusal), "the refusal is the log's last"
        assert logged.decode("utf-8").splitlines()[-2] == "== failed, 0 s", "the step's outcome, just above it"

    # -- run_argv with a handle -----------------------------------------------
    def test_with_a_log_the_child_gets_no_stdin_and_without_one_it_inherits_it(self, monkeypatch) -> None:
        seen = []

        def record(argv, **kwargs):
            seen.append(kwargs)
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(_INSTALL.subprocess, "run", record)
        log = io.BytesIO()
        _INSTALL.run_argv(["anything"], log=log)
        _INSTALL.run_argv(["anything"])
        assert seen[0]["stdin"] is _INSTALL.subprocess.DEVNULL
        assert seen[0]["stdout"] is log and seen[0]["stderr"] is _INSTALL.subprocess.STDOUT
        assert seen[1] == {"stdin": None, "stdout": None, "stderr": None}, "today's call, unchanged"

    def test_a_child_reading_stdin_gets_eof_at_once(self, tmp_path: Path) -> None:
        path = tmp_path / "install.log"
        with open(path, "wb") as log:
            code = _INSTALL.run_argv([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], log=log)
        assert code == 0
        assert path.read_bytes().splitlines()[-1] == b"''"

    def test_a_real_childs_two_streams_land_in_the_log_and_not_on_the_console(self, tmp_path: Path, capfd) -> None:
        """The Windows CI runner witnesses a file handle handed to ``subprocess.run`` here."""
        path = tmp_path / "install.log"
        program = "import sys; print('to-out', flush=True); print('to-err', file=sys.stderr, flush=True)"
        with open(path, "wb") as log:
            assert _INSTALL.run_argv([sys.executable, "-c", program], log=log) == 0
        captured = capfd.readouterr()
        assert "to-out" not in captured.out + captured.err
        lines = path.read_bytes().decode("utf-8").splitlines()
        assert lines[0] == f"$ {sys.executable} -c {program}"
        assert sorted(line.strip() for line in lines[1:]) == ["to-err", "to-out"]

    def test_without_a_log_a_real_child_writes_to_the_console(self, capfd) -> None:
        assert _INSTALL.run_argv([sys.executable, "-c", "print('to-out')"]) == 0
        assert "to-out" in capfd.readouterr().out

    def test_the_guard_still_refuses_before_anything_is_written(self) -> None:
        log = io.BytesIO()
        with pytest.raises(_INSTALL.ProgramWillNotStart):
            _INSTALL.run_argv(["cmd.exe", "/c", "call", r"C:\a&b\code.cmd"], log=log)
        assert log.getvalue() == b""

    def test_the_dollar_line_is_redacted(self, monkeypatch) -> None:
        monkeypatch.setattr(_INSTALL.subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0))
        log = io.BytesIO()
        _INSTALL.run_argv(["pip", "install", "--index-url", _SECRET_INDEX], log=log)
        assert log.getvalue() == b"$ pip install --index-url https://***@h/simple\n"

    # -- what the log holds ---------------------------------------------------
    def test_the_header_and_the_step_lines(self, tmp_path: Path) -> None:
        assert self._main(tmp_path, *self._index_install(), run=lambda argv, log=None: 0) == 0
        lines = (tmp_path / "SWCraft" / _INSTALL.LOG_FILENAME).read_bytes().decode("utf-8").splitlines()
        assert lines[0] == f"SWCraft {_VERSION} install"
        assert lines[1].startswith("date: ")
        assert datetime.datetime.fromisoformat(lines[1].removeprefix("date: ")).tzinfo is not None
        assert lines[2] == f"platform: posix ({sysconfig.get_platform()})"
        assert lines[3] == f"archive: {_posix_home(tmp_path / 'archive')}"
        assert lines[4].startswith("argv: --home ") and "https://***@h/simple" in lines[4]
        assert lines[5:] == [
            "== 1/3 creating the Python environment",
            "== done, 0 s",
            f"== 2/3 {_PIP_LABEL}",
            "== done, 0 s",
            f"== 3/3 pointing current at {_VERSION}",
            "== done, 0 s",
        ]

    def test_a_second_run_replaces_the_log(self, tmp_path: Path) -> None:
        def marking(argv, log=None):
            log.write(b"first-run-marker\n")
            return 0

        assert self._main(tmp_path, *self._index_install(), run=marking) == 0
        assert self._main(tmp_path, *self._index_install(), run=lambda argv, log=None: 0) == 0
        logged = (tmp_path / "SWCraft" / _INSTALL.LOG_FILENAME).read_bytes()
        assert b"first-run-marker" not in logged
        assert logged.count(b" install\n") == 1

    def test_verbose_leaves_the_output_on_the_console_and_notes_it_per_spawning_step(
        self, tmp_path: Path, capfd
    ) -> None:
        def unbound(argv):  # no `log` parameter: binding it would be a TypeError
            return _INSTALL.run_argv([sys.executable, "-c", "print('child-out')"])

        assert self._main(tmp_path, *self._index_install("--verbose"), run=unbound) == 0
        captured = capfd.readouterr()
        assert captured.out.count("child-out") == 2, "venv and pip each started the child on the console"
        # The child prints in its own text mode, so on Windows its line ends "\r\n"; that is the
        # child's, not the installer's, whose own lines are asserted exactly elsewhere.
        console = captured.out.replace("\r\n", "\n")
        assert "1/3 creating the Python environment ...\nchild-out\n1/3 done, 0 s" in console
        lines = (tmp_path / "SWCraft" / _INSTALL.LOG_FILENAME).read_bytes().decode("utf-8").splitlines()
        note = "(output went to the console: --verbose)"
        assert [lines[index - 1] for index, line in enumerate(lines) if line == note] == [
            "== 1/3 creating the Python environment",
            f"== 2/3 {_PIP_LABEL}",
        ], "the link step spawns nothing and gets no note"
        assert "child-out" not in lines

    def test_a_progress_line_writes_its_sentences_after_its_outcome(self) -> None:
        log = io.BytesIO()
        progress = _INSTALL.Progress(2, console=Console(), clock=_clock(1.0, 3.0), log=log)
        progress.start(1, "PATH entry")
        progress.echo("Add this to your shell profile")
        progress.finish(failed=False, forward=lambda sentence: None)
        assert log.getvalue() == b"== 1/2 PATH entry\n== done, 2 s\nAdd this to your shell profile\n"

    # -- no directory without a log -------------------------------------------
    def test_a_journal_driven_install_creates_no_directory(self, tmp_path: Path, monkeypatch) -> None:
        """The default opener runs only when no ``run`` seam was injected."""
        monkeypatch.chdir(tmp_path)
        assert TestMain()._install(tmp_path / "SWCraft", Journal()) == 0
        assert list(tmp_path.iterdir()) == []

    # -- the failure paths ----------------------------------------------------
    def test_a_failed_step_prints_the_tail_and_the_path_then_the_refusal_last(self, tmp_path: Path, capsys) -> None:
        assert self._main(tmp_path, *self._index_install(), run=self._pip_fails) == 1
        captured = capsys.readouterr()
        out = captured.out.splitlines()
        log_path = self._log_path(tmp_path)
        assert f"2/3 {_PIP_LABEL} ... failed, 0 s" in out
        heading = next(index for index, line in enumerate(out) if line.startswith("the last "))
        assert out[heading].endswith(f" lines of {log_path}:")
        assert "  ERROR: no matching distribution" in out[heading + 1 :]
        assert all(line.startswith("  ") for line in out[heading + 1 : -1])
        assert out[-1] == f"the full log is {log_path}"
        assert captured.err.count("\n") == 1 and "failed with exit code 1" in captured.err

    def test_a_step_that_spawns_nothing_still_prints_a_tail_of_headers(self, tmp_path: Path, capsys) -> None:
        archive = _small_archive(tmp_path / "archive", _LINUX_MEMBERS)
        code = self._main(
            tmp_path,
            "--no-path",
            "--no-vsix",
            run=lambda argv, log=None: 0,
            journal=Journal(unpack_fails="node.tar.xz"),
            archive_root=_posix_home(archive),
        )
        assert code == 1
        captured = capsys.readouterr()
        assert "unpacking Node.js ... failed, 0 s" in captured.out
        out = captured.out.splitlines()
        heading = next(index for index, line in enumerate(out) if line.startswith("the last "))
        tail = out[heading + 1 : -1]
        assert any(line.startswith("  == ") and line.endswith(" unpacking Node.js") for line in tail), tail
        assert tail[-1] == "  == failed, 0 s"
        assert captured.out.splitlines()[-1] == f"the full log is {self._log_path(tmp_path)}"
        assert "could not be unpacked" in captured.err

    def test_a_refusal_before_the_run_creates_no_home_and_no_log(self, tmp_path: Path, capsys) -> None:
        # A damaged archive: no SHA256SUMS where the archive should be.
        assert self._main(tmp_path, "--no-path", "--no-vsix", run=lambda argv, log=None: 0) == 1
        assert not (tmp_path / "SWCraft").exists()
        assert "the last" not in capsys.readouterr().out

    def test_a_relative_home_creates_nothing(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.chdir(tmp_path)
        code = _INSTALL.main(
            ["--home", "relhome", *self._index_install()],
            env={"PATH": "/usr/bin"},
            platform="posix",
            which=_which({}),
            archive_root=_posix_home(tmp_path / "archive"),
            open_log=_INSTALL.open_install_log,
            run=lambda argv, log=None: 0,
        )
        assert code == 1
        assert list(tmp_path.iterdir()) == []
        captured = capsys.readouterr()
        assert "--home must be an absolute path" in captured.err
        assert "the last" not in captured.out

    def test_a_home_that_cannot_be_created_is_its_own_sentence(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "SWCraft").write_text("a file, not a directory\n", encoding="utf-8", newline="\n")
        journal = Journal()
        assert self._main(tmp_path, *self._index_install(), run=journal.run, journal=journal) == 1
        captured = capsys.readouterr()
        assert "could not be created or written" in captured.err and "nothing was installed" in captured.err
        assert "may be incomplete" not in captured.err
        assert captured.err.count("\n") == 1
        # A progress line, not the substring "1/": the announcement names the tmp home, whose
        # path can hold ".../popen-gw1/...".
        assert journal.calls == [] and not _PROGRESS_LINE.search(captured.out)

    def test_an_opener_that_raises_is_the_same_sentence(self, tmp_path: Path, capsys) -> None:
        def refuse(path):
            raise PermissionError(13, "Permission denied", path)

        assert self._main(tmp_path, *self._index_install(), run=lambda argv, log=None: 0, open_log=refuse) == 1
        err = capsys.readouterr().err
        assert f"{self._log_path(tmp_path)} could not be created or written (Permission denied)" in err

    def test_a_header_that_cannot_be_written_is_the_same_sentence(self, tmp_path: Path, capsys) -> None:
        """A full disk at the header: nothing was installed, so never the backstop's sentence."""

        class _NoWrite(io.BytesIO):
            def write(self, data) -> int:
                raise OSError(28, "No space left on device")

        log = _NoWrite()
        journal = Journal()
        code = self._main(
            tmp_path, *self._index_install(), run=lambda argv, log=None: 0, journal=journal, open_log=lambda path: log
        )
        assert code == 1
        captured = capsys.readouterr()
        assert f"{self._log_path(tmp_path)} could not be created or written (No space left on device)" in captured.err
        assert "nothing was installed" in captured.err and "may be incomplete" not in captured.err
        assert captured.err.count("\n") == 1
        assert journal.calls == [] and not _PROGRESS_LINE.search(captured.out)
        assert "the full log is" not in captured.out
        assert log.closed, "the handle whose header failed is closed"

    def test_a_log_whose_flush_fails_still_ends_with_the_refusal_and_no_tail(self, tmp_path: Path, capsys) -> None:
        log = _FlushFails()
        code = self._main(tmp_path, *self._index_install(), run=self._pip_fails, open_log=lambda path: log)
        assert code == 1
        captured = capsys.readouterr()
        assert "the last" not in captured.out and "the full log is" not in captured.out
        assert "failed with exit code 1" in captured.err and captured.err.count("\n") == 1

    def test_a_tail_line_no_code_page_can_print_is_escaped(self, tmp_path: Path, monkeypatch) -> None:
        def mojibake(argv, log=None):
            if "pip" not in argv:
                return 0
            log.write("it\u2019s ".encode() + b"\xff broken\n")
            return 1

        console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        monkeypatch.setattr(sys, "stdout", console)
        assert self._main(tmp_path, *self._index_install(), run=mojibake) == 1
        console.flush()
        printed = console.buffer.getvalue().decode("cp1252")
        assert "  it\\u2019s \\ufffd broken" in printed

    # -- a log or a console that fails mid-run ----------------------------------
    def test_a_log_that_cannot_be_written_still_ends_with_the_steps_refusal(self, tmp_path: Path, capsys) -> None:
        """A full disk after the header: every later log write raises, and the pip refusal still wins."""
        log = _WritesFailAfterHeader()

        def pip_fails(argv, log=None) -> int:
            return 1 if "pip" in argv else 0

        code = self._main(tmp_path, *self._index_install(), run=pip_fails, open_log=lambda path: log)
        assert code == 1
        captured = capsys.readouterr()
        assert f"2/3 {_PIP_LABEL} ... failed, 0 s" in captured.out
        assert "failed with exit code 1" in captured.err and "may be incomplete" not in captured.err
        assert captured.err.count("\n") == 1

    def test_a_console_that_cannot_close_a_failed_line_keeps_the_steps_refusal(self) -> None:
        def console(text, end="\n"):
            if text.startswith("failed"):
                raise BrokenPipeError(32, "Broken pipe")

        progress = _INSTALL.Progress(3, console=console, clock=lambda: 0.0)
        journal = Journal(codes=lambda argv: 1 if "pip" in argv else 0)
        with pytest.raises(_INSTALL.InstallError, match="failed with exit code 1"):
            journal.execute(_posix_plan("--no-path", "--no-vsix"), "posix", progress=progress)

    def test_a_broken_stdout_still_gets_the_refusal_onto_stderr(self, monkeypatch, capsys) -> None:
        """A console whose reader went away: the final flush raises, and the refusal is still written."""

        class _BrokenStdout(io.StringIO):
            def flush(self) -> None:
                raise BrokenPipeError(32, "Broken pipe")

        monkeypatch.setattr(sys, "stdout", _BrokenStdout())
        _INSTALL._report_failure(None, None, "the step failed, so nothing further was changed")
        assert capsys.readouterr().err == "the step failed, so nothing further was changed\n"

    def test_an_operating_system_error_after_the_log_opened_prints_the_tail_then_the_backstop(
        self, tmp_path: Path, capsys
    ) -> None:
        journal = _LinkRaisesOSError()
        assert self._main(tmp_path, *self._index_install(), run=lambda argv, log=None: 0, journal=journal) == 1
        captured = capsys.readouterr()
        out = captured.out.splitlines()
        log_path = self._log_path(tmp_path)
        assert any(line.startswith("the last ") and line.endswith(f" lines of {log_path}:") for line in out)
        assert out[-1] == f"the full log is {log_path}"
        assert captured.err.count("\n") == 1 and "operating-system error" in captured.err
        logged = (tmp_path / "SWCraft" / _INSTALL.LOG_FILENAME).read_bytes().decode("utf-8").splitlines()
        assert logged[-1] == captured.err.strip(), "the backstop's sentence is the log's last line"

    def test_the_tail_is_the_last_twenty_lines_and_its_heading_counts_them(self, tmp_path: Path, capsys) -> None:
        def chatty(argv, log=None) -> int:
            if "pip" not in argv:
                return 0
            log.write(b"".join(b"pip-line %02d\n" % number for number in range(1, 31)))
            return 1

        assert self._main(tmp_path, *self._index_install(), run=chatty) == 1
        out = capsys.readouterr().out.splitlines()
        heading = next(index for index, line in enumerate(out) if line.startswith("the last "))
        assert out[heading] == f"the last {_INSTALL.LOG_TAIL_LINES} lines of {self._log_path(tmp_path)}:"
        tail = out[heading + 1 : -1]
        assert len(tail) == _INSTALL.LOG_TAIL_LINES == 20
        assert tail[-1] == "  == failed, 0 s"
        assert tail[0] == "  pip-line 12" and tail[-2] == "  pip-line 30", "the LAST lines, in order"

    def test_a_short_log_announces_its_own_count(self, tmp_path: Path, capsys) -> None:
        assert self._main(tmp_path, *self._index_install(), run=self._pip_fails) == 1
        out = capsys.readouterr().out.splitlines()
        heading = next(index for index, line in enumerate(out) if line.startswith("the last "))
        shown = len(out) - heading - 2
        assert shown < _INSTALL.LOG_TAIL_LINES
        assert out[heading] == f"the last {shown} lines of {self._log_path(tmp_path)}:"

    def test_on_real_pipes_the_path_is_directly_above_the_refusal(self, tmp_path: Path) -> None:
        """One merged pipe, as the extension reads it: block-buffered stdout must not land after stderr."""
        driver = tmp_path / "driver.py"
        driver.write_text(
            "import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('install', sys.argv[1])\n"
            "install = sys.modules['install'] = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(install)\n"
            "def run(argv, log=None):\n"
            "    if 'pip' in argv:\n"
            "        log.write(b'ERROR: no matching distribution\\n')\n"
            "        return 1\n"
            "    return 0\n"
            "sys.exit(install.main(sys.argv[2:], run=run, open_log=install.open_install_log,\n"
            "    link=lambda path, target: None, which=lambda name: None))\n",
            encoding="utf-8",
            newline="\n",
        )
        home = tmp_path / "SWCraft"
        result = subprocess.run(
            [sys.executable, str(driver), str(_SCRIPT_PATH), "--home", str(home), *self._index_install()],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 1, result.stdout
        lines = result.stdout.splitlines()
        assert "failed with exit code 1" in lines[-1], result.stdout
        assert lines[-2] == f"the full log is {os.path.join(str(home), _INSTALL.LOG_FILENAME)}", result.stdout

    def test_a_verbose_failure_prints_the_tail_of_headers_and_notes_then_the_refusal(
        self, tmp_path: Path, capsys
    ) -> None:
        def pip_fails(argv) -> int:  # unbound under --verbose
            return 1 if "pip" in argv else 0

        assert self._main(tmp_path, *self._index_install("--verbose"), run=pip_fails) == 1
        captured = capsys.readouterr()
        out = captured.out.splitlines()
        heading = next(index for index, line in enumerate(out) if line.startswith("the last "))
        tail = out[heading + 1 : -1]
        pip_header = f"  == 2/3 {_PIP_LABEL}"
        assert tail[tail.index(pip_header) + 1] == "  (output went to the console: --verbose)"
        assert tail[-1] == "  == failed, 0 s"
        assert out[-1] == f"the full log is {self._log_path(tmp_path)}"
        assert captured.err.count("\n") == 1 and "failed with exit code 1" in captured.err

    def test_an_interrupt_during_a_step_closes_its_line_failed_and_propagates(self, tmp_path: Path, capsys) -> None:
        handles = []

        def opener(path):
            handles.append(_INSTALL.open_install_log(path))
            return handles[-1]

        def interrupted(argv, log=None) -> int:
            if "pip" in argv:
                raise KeyboardInterrupt
            return 0

        with pytest.raises(KeyboardInterrupt):
            self._main(tmp_path, *self._index_install(), run=interrupted, open_log=opener)
        out = capsys.readouterr().out.splitlines()
        assert out[-1].endswith(" ... failed, 0 s") and out[-1].startswith("2/3 ")
        assert handles and handles[0].closed, "main's finally closes the log"

    def test_a_companion_code_would_not_install_points_at_the_log_after_the_success(
        self, tmp_path: Path, capsys
    ) -> None:
        archive = _small_archive(tmp_path / "archive", _MEMBERS_WITH_COMPANIONS)

        def code_refuses_yaml(argv, log=None) -> int:
            if any("redhat.vscode-yaml" in str(part) for part in argv):
                log.write(b"Unable to install extension: engines.vscode\n")
                return 1
            return 0

        journal = Journal()
        code = self._main(
            tmp_path,
            "--no-path",
            run=code_refuses_yaml,
            journal=journal,
            archive_root=_posix_home(archive),
            which=_which({"code": _POSIX_CODE}),
        )
        assert code == 0
        out = capsys.readouterr().out.splitlines()
        sentences = journal.echoed
        assert any("`redhat.vscode-yaml` was not installed (`code` exited with code 1)" in line for line in sentences)
        assert out[-2].startswith("SWCraft ") and out[-2].endswith(
            " is installed in " + _posix_home(tmp_path / "SWCraft")
        )
        assert out[-1] == f"the full log is {self._log_path(tmp_path)}"
        assert not any(marker in "\n".join(out + sentences) for marker in _EXTENSION_NOT_INSTALLED_MARKERS)

    def test_a_success_whose_sentences_came_from_no_program_prints_no_log_line(self, tmp_path: Path, capsys) -> None:
        archive = _small_archive(tmp_path / "archive", _MEMBERS_WITH_COMPANIONS)
        journal = Journal()
        assert (
            self._main(tmp_path, run=lambda argv, log=None: 0, journal=journal, archive_root=_posix_home(archive)) == 0
        )
        sentences = "\n".join(journal.echoed)
        assert "Add this to your shell profile" in sentences and "was not installed" in sentences, "PATH, no `code`"
        assert "the full log is" not in capsys.readouterr().out

    def test_a_step_with_no_argv_gets_no_verbose_note(self) -> None:
        log = io.BytesIO()
        progress = _INSTALL.Progress(1, console=Console(), clock=lambda: 0.0, inherits_console=True, log=log)
        steps = _posix_plan("--no-path", members=_MEMBERS_WITH_COMPANIONS, found_code=None)
        Journal().execute(steps, "posix", progress=progress)
        assert b"(output went to the console: --verbose)" in log.getvalue(), "venv and pip still spawn"
        notes = log.getvalue().decode("utf-8").splitlines()
        noted = [notes[index - 1] for index, line in enumerate(notes) if line.startswith("(output went")]
        assert not any("editor extension" in line for line in noted), noted

    @pytest.mark.skipif(os.name == "nt", reason="a POSIX mode; on Windows the file takes the home's access list")
    def test_the_log_is_readable_by_its_owner_only(self, tmp_path: Path) -> None:
        assert self._main(tmp_path, *self._index_install(), run=lambda argv, log=None: 0) == 0
        mode = (tmp_path / "SWCraft" / _INSTALL.LOG_FILENAME).stat().st_mode
        assert stat.S_IMODE(mode) == 0o600

    def test_uninstall_writes_no_log_and_inherits_the_console(self, tmp_path: Path, capsys) -> None:
        home = tmp_path / "SWCraft"
        (home / "versions" / _VERSION).mkdir(parents=True)
        opened = []
        code = self._main(
            tmp_path, "--uninstall", "--no-path", "--no-vsix", run=lambda argv, log=None: 0, open_log=opened.append
        )
        assert code == 0
        assert opened == []
        out = capsys.readouterr().out.splitlines()
        assert out[1].startswith("1/2 removing ") and out[1].endswith(" ...")
        assert out[2] == "1/2 done, 0 s"


class TestMain:
    """The entry point the wrapper calls, driven with every seam injected.

    ``main`` measures the two facts the plan needs -- which versions are installed and where ``code`` resolved -- and
    turns an :class:`InstallError` into one sentence on stderr and an exit code. Most cases pass ``--index-url``,
    which makes the archive unnecessary (an index resolves for the running interpreter by construction), so they
    unpack and hash nothing.
    """

    def _install(self, home: Path, journal: Journal, *extra: str, version_info=None, executable=None) -> int:
        return journal.main(
            [
                "--home",
                _posix_home(home),
                "--version",
                _VERSION,
                "--index-url",
                "https://example.invalid/simple",
                *extra,
            ],
            env={"PATH": "/usr/bin"},
            platform="posix",
            which=_which({"code": _POSIX_CODE, "python3.11": _POSIX_PYTHON[0]}),
            archive_root=_posix_home(home / "archive"),
            version_info=version_info if version_info is not None else sys.version_info,
            executable=executable,
        )

    def test_a_relative_home_is_refused_before_anything_is_planned(self, tmp_path: Path, capsys) -> None:
        """A relative ``--home`` is refused before anything is planned.

        The ``link`` step stores the home as its target, and a relative target resolves against the link's own
        directory: ``--home ./relhome`` built a venv, printed "is installed in relhome", exited 0 and left
        ``relhome/current`` dangling.

        Refused rather than absolutised: absolutising needs the running host's cwd, while ``home_directory`` spells
        paths the way the target platform does (how a Windows plan is built from macOS). The reading is
        :func:`home_is_absolute`'s, under the target platform's flavour only: a both-flavours reading, fine for a check
        that refuses, would here accept a path the plan then spells relative.
        """
        journal = Journal()
        code = journal.main(
            ["--home", "relhome", "--version", _VERSION, "--index-url", "https://example.invalid/simple"],
            env={"PATH": "/usr/bin"},
            platform="posix",
            which=_which({"code": _POSIX_CODE, "python3.11": _POSIX_PYTHON[0]}),
            archive_root=_posix_home(tmp_path / "archive"),
        )
        assert code == 1
        captured = capsys.readouterr()
        assert "--home must be an absolute path" in captured.err and "relhome" in captured.err
        assert captured.err.strip().count("\n") == 0, "a refusal is one line"
        assert journal.kinds == [], "nothing is planned, so nothing is half-installed"
        assert "is installed in" not in captured.out, "and nothing reports success"

    def test_a_relative_home_is_refused_for_uninstall_too(self, tmp_path: Path, capsys) -> None:
        """The same refusal for ``--uninstall``: the flag means one thing, and this path deletes a directory."""
        journal = Journal()
        code = journal.main(
            ["--home", "relhome", "--uninstall", "--no-path", "--no-vsix"],
            env={"PATH": "/usr/bin"},
            platform="posix",
            which=_which({}),
            archive_root=_posix_home(tmp_path / "archive"),
        )
        assert code == 1
        message = capsys.readouterr().err
        # Named, not merely non-zero: `require_installation` refuses a directory that
        # is not there and its sentence also says `--home`, so a weaker assertion
        # here would pass on this path without the guard existing at all.
        assert "--home must be an absolute path" in message and "relhome" in message
        assert journal.kinds == []

    def test_a_windows_drive_relative_home_is_refused_too(self, tmp_path: Path, capsys) -> None:
        """``C:dist`` and ``\\dist`` are refused too.

        Windows keeps a current directory per drive, so both are relative to something this file cannot see.
        """
        for spelling in (r"C:dist", "\\dist"):
            journal = Journal()
            code = journal.main(
                ["--home", spelling, "--version", _VERSION, "--index-url", "https://example.invalid/s"],
                env={"PATH": r"C:\Windows"},
                platform="nt",
                which=_which({"py": r"C:\Windows\py.exe"}),
                archive_root=_WIN_ARCHIVE,
            )
            assert code == 1, spelling
            assert "--home must be an absolute path" in capsys.readouterr().err, spelling
            assert journal.kinds == []

    def test_a_windows_spelled_home_on_a_posix_target_is_refused_too(self, tmp_path: Path, capsys) -> None:
        """A Windows-spelled home on a POSIX target is refused.

        ``C:\\SWCraft`` and ``\\\\srv\\share\\swc`` are absolute to Windows; on a POSIX target ``PurePosixPath`` spells
        them relative, the ``current`` link resolves against its own directory, and the run would still report
        success.
        """
        for spelling in (r"C:\SWCraft", r"\\srv\share\swc"):
            journal = Journal()
            code = journal.main(
                ["--home", spelling, "--version", _VERSION, "--index-url", "https://example.invalid/s"],
                env={"PATH": "/usr/bin"},
                platform="posix",
                which=_which({"code": _POSIX_CODE, "python3.11": _POSIX_PYTHON[0]}),
                archive_root=_posix_home(tmp_path / "archive"),
            )
            captured = capsys.readouterr()
            assert code == 1, spelling
            assert "--home must be an absolute path" in captured.err, spelling
            assert journal.kinds == [], spelling
            assert "is installed in" not in captured.out, spelling

    def test_a_posix_spelled_home_on_a_windows_target_is_refused_too(self, tmp_path: Path, capsys) -> None:
        """A POSIX-spelled home on a Windows target is refused.

        Windows resolves ``/dist`` against the current drive.
        """
        journal = Journal()
        code = journal.main(
            ["--home", "/dist", "--version", _VERSION, "--index-url", "https://example.invalid/s"],
            env={"PATH": r"C:\Windows"},
            platform="nt",
            which=_which({"py": r"C:\Windows\py.exe"}),
            archive_root=_WIN_ARCHIVE,
        )
        assert code == 1
        assert "--home must be an absolute path" in capsys.readouterr().err
        assert journal.kinds == []

    def test_an_absolute_home_is_still_accepted_on_both_platforms(self, tmp_path: Path, monkeypatch) -> None:
        """An absolute home is still accepted on both platforms, and creates no directory.

        The ``run`` seam is injected, so no log is opened; otherwise the ``C:\\Users\\dev`` home would become a relative
        directory in the cwd on POSIX.
        """
        monkeypatch.chdir(tmp_path)
        journal = Journal()
        assert self._install(tmp_path / "SWCraft", journal) == 0
        windows = Journal()
        assert (
            windows.main(
                ["--home", _WIN_HOME, "--version", _VERSION, "--index-url", "https://example.invalid/s"],
                env={"PATH": r"C:\Windows"},
                platform="nt",
                which=_which({"py": r"C:\Windows\py.exe"}),
                archive_root=_WIN_ARCHIVE,
            )
            == 0
        )
        assert list(tmp_path.iterdir()) == [], "neither run created a home or a log"

    def test_a_clean_run_exits_zero(self, tmp_path: Path) -> None:
        journal = Journal()
        assert self._install(tmp_path / "SWCraft", journal) == 0
        assert journal.kinds == ["run", "run", "link", "echo", "run"]

    def test_the_members_the_archive_lists_reach_the_plan(self, tmp_path: Path) -> None:
        """The members ``SHA256SUMS`` lists reach the plan: the listed payloads are what ``runtime`` unpacks.

        The one case here that verifies an archive, so it builds and hashes a small one of its own.
        """
        archive = _small_archive(
            tmp_path / "archive",
            (
                f"wheels/swcraft_agent-{_VERSION}-py3-none-any.whl",
                "wheels/PLATFORM.json",
                "runtime/RUNTIME.json",
                "runtime/node.tar.xz",
            ),
        )
        journal = Journal()
        code = journal.main(
            ["--home", _posix_home(tmp_path / "SWCraft"), "--no-vsix"],
            env={"PATH": "/usr/bin"},
            platform="posix",
            which=_which({"python3.11": _POSIX_PYTHON[0]}),
            archive_root=_posix_home(archive),
            version_info=sys.version_info,
        )
        assert code == 0
        assert journal.kinds == ["stamp", "run", "run", "unpack", "place", "echo", "link", "echo"]
        root = PurePosixPath(_posix_home(archive))
        assert [call[1] for call in journal.calls if call[0] == "unpack"] == [str(root / "runtime" / "node.tar.xz")]

    def test_without_python_and_without_a_payload_the_running_interpreter_builds_the_venv(self, tmp_path: Path) -> None:
        """No ``PATH`` lookup: the ``which`` here would answer ``python3.11`` and is not asked."""
        journal = Journal()
        self._install(tmp_path / "SWCraft", journal, executable="/opt/running/bin/python3")
        assert journal.calls[0][1] == "/opt/running/bin/python3"

    def test_a_python_below_the_floor_is_refused_in_the_floor_sentence(self, tmp_path: Path, capsys) -> None:
        """``python_refusal`` still refuses a 3.10 ``--python`` -- asked of that interpreter, index or not."""
        journal = Journal(current={"python_version": "3.10", "platform_tag": "linux-x86_64"})
        code = self._install(tmp_path / "SWCraft", journal, "--python", "/opt/py310/bin/python3.10")
        assert code == 1
        message = capsys.readouterr().err
        assert "SWCraft needs Python 3.11 and /opt/py310/bin/python3.10 is Python 3.10" in message
        assert "leave --python out" in message
        assert journal.calls == [], "refused before anything is planned"
        assert journal.asked_interpreters == [("/opt/py310/bin/python3.10",)]

    @pytest.mark.parametrize("version_info", [(3, 9, 6), (3, 13, 1)])
    def test_install_py_started_directly_on_another_python_is_sent_to_the_wrapper(
        self, tmp_path: Path, capsys, version_info
    ) -> None:
        """The refusal names the entry point, not the archive.

        With the payload and no ``--python`` the ``check-platform`` step asks the running interpreter, so
        ``python3.13 install.py`` was refused as "built for another environment", whose remedy would not help. Refused
        before anything is planned, above the floor as well as below it: the carried build is one exact version.
        """
        archive = _small_archive(
            tmp_path / "archive",
            (f"wheels/swcraft_agent-{_VERSION}-py3-none-any.whl", "wheels/PLATFORM.json", _PYTHON_MEMBER),
        )
        journal = Journal()
        code = journal.main(
            ["--home", _posix_home(tmp_path / "SWCraft"), "--no-vsix"],
            env={"PATH": "/usr/bin"},
            platform="posix",
            which=_which({}),
            archive_root=_posix_home(archive),
            version_info=version_info,
            executable="/usr/local/bin/python3",
        )
        assert code == 1
        message = capsys.readouterr().err
        assert message.count("\n") == 1, "one sentence"
        assert "swcraft_install.sh" in message and "--python PATH" in message
        assert "another environment" not in message
        assert journal.calls == [], "refused before anything is planned"

    def test_the_carried_python_builds_the_venv_and_the_running_interpreter_is_not_held_to_the_floor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With the payload and no ``--python``, ``python_refusal`` is not consulted at all."""
        consulted: list[tuple] = []

        def python_refusal(*arguments, **keywords):
            consulted.append(arguments)
            return "a refusal nobody should have asked for"

        monkeypatch.setattr(_INSTALL, "python_refusal", python_refusal)
        archive = _small_archive(
            tmp_path / "archive",
            (f"wheels/swcraft_agent-{_VERSION}-py3-none-any.whl", "wheels/PLATFORM.json", _PYTHON_MEMBER),
        )
        journal = Journal()
        bootstrap = _posix_home(archive) + "/.bootstrap/python/bin/python3"
        code = journal.main(
            ["--home", _posix_home(tmp_path / "SWCraft"), "--no-vsix"],
            env={"PATH": "/usr/bin"},
            platform="posix",
            which=_which({}),
            archive_root=_posix_home(archive),
            version_info=(3, 11, 16),
            executable=bootstrap,
        )
        assert code == 0 and consulted == []
        assert journal.kinds == ["stamp", "unpack", "run", "run", "link", "echo", "remove"]
        root = PurePosixPath(_posix_home(archive))
        target = PurePosixPath(_posix_home(tmp_path / "SWCraft")) / "versions" / _VERSION
        assert journal.calls[1] == (
            "unpack",
            str(root / "runtime" / "python.tar.gz"),
            str(target / "runtime" / "python"),
        )
        assert journal.calls[2][1] == str(target / "runtime" / "python" / "bin" / "python3")
        assert journal.asked_interpreters == [(bootstrap,)], "check-platform asks the bootstrap"
        assert journal.calls[-1] == ("remove", str(root / ".bootstrap"))

    def test_a_failing_step_is_one_sentence_on_stderr_and_exit_one(self, tmp_path: Path, capsys) -> None:
        journal = Journal(codes=lambda argv: 1 if "pip" in argv else 0)
        assert self._install(tmp_path / "SWCraft", journal) == 1
        captured = capsys.readouterr()
        assert "pip" in captured.err
        assert captured.err.strip().count("\n") == 0, "a refusal is one line"

    def test_a_locked_version_directory_still_exits_zero(self, tmp_path: Path) -> None:
        """The new version is already current when an old one cannot be removed, so the run succeeded."""
        home = tmp_path / "SWCraft"
        (home / "versions" / "0.6.0").mkdir(parents=True)
        journal = Journal(locked="0.6.0")
        assert self._install(home, journal) == 0
        assert [line for line in journal.echoed if "0.6.0" in line] != []

    def test_an_unresolved_code_still_exits_zero(self, tmp_path: Path) -> None:
        journal = Journal()
        code = journal.main(
            [
                "--home",
                _posix_home(tmp_path / "SWCraft"),
                "--version",
                _VERSION,
                "--index-url",
                "https://example.invalid/s",
            ],
            env={"PATH": "/usr/bin"},
            platform="posix",
            which=_which({"python3.11": _POSIX_PYTHON[0]}),
            archive_root=_posix_home(tmp_path / "archive"),
        )
        assert code == 0
        assert [line for line in journal.echoed if ".vsix" in line] != []

    def test_a_failing_code_exits_non_zero(self, tmp_path: Path) -> None:
        journal = Journal(codes=lambda argv: 1 if argv[0] == _POSIX_CODE else 0)
        assert self._install(tmp_path / "SWCraft", journal) == 1

    def test_an_old_interpreter_prints_the_refusal_and_exits_one(self, tmp_path: Path, capsys) -> None:
        journal = Journal()
        # `--source` with no SHA256SUMS: no Python payload, so the running interpreter builds the venv.
        code = _INSTALL.main(
            ["--home", _posix_home(tmp_path / "SWCraft"), "--version", _VERSION, "--source", _posix_home(tmp_path)],
            env={"PATH": "/usr/bin"},
            platform="posix",
            which=_which({}),
            archive_root=_posix_home(tmp_path / "archive"),
            version_info=(3, 9, 6),
            run=journal.run,
        )
        assert code == 1
        assert "3.11" in capsys.readouterr().err
        assert journal.calls == [], "nothing is attempted by an interpreter that cannot do it"

    def test_an_old_interpreter_that_names_one_with_python_is_not_refused(self, tmp_path: Path) -> None:
        """An old interpreter naming a 3.11 with ``--python`` is not refused.

        The venv is built with the named interpreter; the one running this file only has to run it, which it does far
        below the floor (``TestParseFloor``). The version gate is asked only when nothing else answers, and the named
        interpreter is checked where it matters: ``check-platform`` compares its platform with the wheelhouse
        (``TestExecutionSeams``).
        """
        journal = Journal()
        code = self._install(
            tmp_path / "SWCraft", journal, "--python", "/opt/py311/bin/python3.11", version_info=(3, 9, 6)
        )
        assert code == 0
        assert journal.calls[0][1] == "/opt/py311/bin/python3.11", (
            "the named interpreter is the one the venv is built with"
        )

    def test_an_old_interpreter_without_python_is_still_refused(self, tmp_path: Path, capsys) -> None:
        """The other direction of the same change: nothing else answers, so the gate does."""
        journal = Journal()
        assert self._install(tmp_path / "SWCraft", journal, version_info=(3, 9, 6)) == 1
        assert "3.11" in capsys.readouterr().err
        assert journal.calls == []

    def test_a_usage_error_on_an_old_interpreter_still_carries_the_version_sentence(
        self, tmp_path: Path, capsys
    ) -> None:
        """A usage error on an interpreter below the floor still prints the "needs Python 3.11" sentence.

        The version check runs after ``parse_args`` so that ``--python`` can answer it; without this, an argparse usage
        error exited 2 and the sentence this file parses down to 3.7 to print was never reached.
        """
        code = _INSTALL.main(
            ["--home", _posix_home(tmp_path / "SWCraft"), "--not-a-flag"],
            env={"PATH": "/usr/bin"},
            platform="posix",
            which=_which({}),
            archive_root=_posix_home(tmp_path / "archive"),
            version_info=(3, 9, 6),
        )
        assert code == 1
        message = capsys.readouterr().err
        assert "SWCraft needs Python 3.11" in message
        assert "--not-a-flag" in message, "argparse's own diagnosis is still printed, above it"

    def test_a_usage_error_beside_python_does_not_reprint_the_remedy_that_was_used(self, tmp_path: Path) -> None:
        """With ``--python`` among the tokens, a usage error keeps argparse's own exit 2 and no version sentence.

        The handler fires on a non-zero ``SystemExit`` out of :func:`parse_args`, before argv is parsed, so it cannot
        ask ``args.python``; telling a caller who wrote ``--python`` to name one with ``--python PATH`` would be false,
        since with ``--python`` the running interpreter's version is no requirement. So it scans the tokens itself.
        """
        with pytest.raises(SystemExit) as excinfo:
            _INSTALL.main(
                ["--home", _posix_home(tmp_path / "SWCraft"), "--python", "/opt/py311/bin/python3.11", "--not-a-flag"],
                env={"PATH": "/usr/bin"},
                platform="posix",
                which=_which({}),
                archive_root=_posix_home(tmp_path / "archive"),
                version_info=(3, 9, 6),
            )
        assert excinfo.value.code == 2, "argparse's own usage exit, unconverted"

    def test_the_python_scan_reads_both_spellings_and_only_the_flag(self, tmp_path: Path, capsys) -> None:
        """``--python X`` and ``--python=X``, and nothing that merely looks like them.

        The token scan's vocabulary is its whole contract: ``--python=/opt/py`` is one token, while ``--pythonpath`` is
        not the flag. Not reached: ``--python`` appearing as the value of another option (``--source --python``), and
        whether the named path exists -- asking that interpreter answers that, on a run past argparse.
        """
        for argv, expected in (
            (["--python=/opt/py311/bin/python3.11", "--not-a-flag"], 2),
            (["--python", "/opt/py311/bin/python3.11", "--not-a-flag"], 2),
            (["--pythonpath", "/opt/py", "--not-a-flag"], 1),
            (["--not-a-flag"], 1),
        ):
            if expected == 2:
                with pytest.raises(SystemExit) as excinfo:
                    _INSTALL.main(
                        ["--home", _posix_home(tmp_path / "SWCraft"), *argv],
                        env={"PATH": "/usr/bin"},
                        platform="posix",
                        which=_which({}),
                        archive_root=_posix_home(tmp_path / "archive"),
                        version_info=(3, 9, 6),
                    )
                assert excinfo.value.code == 2, argv
                assert "SWCraft needs Python" not in capsys.readouterr().err, argv
            else:
                code = _INSTALL.main(
                    ["--home", _posix_home(tmp_path / "SWCraft"), *argv],
                    env={"PATH": "/usr/bin"},
                    platform="posix",
                    which=_which({}),
                    archive_root=_posix_home(tmp_path / "archive"),
                    version_info=(3, 9, 6),
                )
                assert code == 1, argv
                assert "SWCraft needs Python 3.11" in capsys.readouterr().err, argv

    def test_a_usage_error_on_a_current_interpreter_is_argparse_s_own_exit(self, tmp_path: Path) -> None:
        """The opposite case: nothing is converted when the interpreter is fine.

        Without this, the handler above could swallow every usage error into exit 1
        on every host and no case would notice.
        """
        with pytest.raises(SystemExit) as excinfo:
            _INSTALL.main(
                ["--home", _posix_home(tmp_path / "SWCraft"), "--not-a-flag"],
                env={"PATH": "/usr/bin"},
                platform="posix",
                which=_which({}),
                archive_root=_posix_home(tmp_path / "archive"),
                version_info=(3, 11, 13),
            )
        assert excinfo.value.code == 2

    def test_help_on_an_old_interpreter_is_still_help(self, tmp_path: Path, capsys) -> None:
        """A zero exit out of ``parse_args`` is ``--help``, and it is left alone."""
        with pytest.raises(SystemExit) as excinfo:
            _INSTALL.main(
                ["--help"],
                env={"PATH": "/usr/bin"},
                platform="posix",
                which=_which({}),
                archive_root=_posix_home(tmp_path / "archive"),
                version_info=(3, 9, 6),
            )
        assert excinfo.value.code == 0
        assert "SWCraft needs Python" not in capsys.readouterr().out

    def test_a_directory_that_is_not_an_unpacked_archive_is_refused_by_name(self, tmp_path: Path, capsys) -> None:
        journal = Journal()
        code = _INSTALL.main(
            ["--home", _posix_home(tmp_path / "SWCraft")],
            env={"PATH": "/usr/bin"},
            platform="posix",
            which=_which({"python3.11": _POSIX_PYTHON[0]}),
            archive_root=_posix_home(tmp_path / "not-an-archive"),
            run=journal.run,
        )
        assert code == 1
        assert _INSTALL.SHA256SUMS_FILENAME in capsys.readouterr().err
        assert journal.calls == []

    def test_uninstall_touches_neither_code_nor_the_registry(self, tmp_path: Path) -> None:
        """``--uninstall --no-path --no-vsix`` reaches only ``remove``: the home, then the wrapper's ``.bootstrap``.

        The ``artifact`` acceptance runs ``--uninstall`` against a real installation; without the flags it would remove
        the developer's own VS Code extension and edit their user ``PATH``.
        """
        home = tmp_path / "SWCraft"
        (home / "versions" / _VERSION).mkdir(parents=True)
        journal = Journal()
        code = journal.main(
            ["--home", _posix_home(home), "--uninstall", "--no-path", "--no-vsix"],
            env={"PATH": "/usr/bin"},
            platform="posix",
            which=_which({"code": _POSIX_CODE}),
            archive_root=_posix_home(tmp_path / "archive"),
        )
        assert code == 0
        assert journal.calls == [
            ("remove", _posix_home(home)),
            ("remove", str(PurePosixPath(_posix_home(tmp_path / "archive")) / _INSTALL.BOOTSTRAP_DIRNAME)),
        ]

    def test_uninstall_refuses_a_directory_that_is_not_an_installation(self, tmp_path: Path, capsys) -> None:
        """``--uninstall`` refuses a ``--home`` that holds no ``versions/`` directory.

        It planned a ``remove`` of whatever ``--home`` named, so a mistyped or shell-expanded value deleted a directory
        whole and exited 0. The guard is one reading, which makes the refusal cheap enough to be unconditional.
        """
        home = tmp_path / "work"
        (home / "important").mkdir(parents=True)
        (home / "important" / "model.yaml").write_text("name: keep\n", encoding="utf-8", newline="\n")
        journal = Journal()
        code = _INSTALL.main(
            ["--home", _posix_home(home), "--uninstall", "--no-path", "--no-vsix"],
            env={"PATH": "/usr/bin"},
            platform="posix",
            which=_which({}),
            archive_root=_posix_home(tmp_path / "archive"),
            run=journal.run,
            remove=journal.remove,
            echo=journal.echo,
        )
        assert code == 1
        assert journal.calls == [], "a refusal removes nothing"
        assert (home / "important" / "model.yaml").is_file()
        assert _INSTALL.VERSIONS_DIRNAME in capsys.readouterr().err

    def test_an_uninstall_that_could_not_remove_the_home_exits_non_zero(self, tmp_path: Path, capsys) -> None:
        """An uninstall that could not remove the home exits non-zero.

        Swallowing the ``OSError`` is right for an update, where the new version is already ``current``; for
        ``--uninstall`` the removal is the whole operation, and "is gone" at exit 0 contradicted the warning above it.
        """
        home = tmp_path / "SWCraft"
        (home / _INSTALL.VERSIONS_DIRNAME / _VERSION).mkdir(parents=True)
        journal = Journal(locked="SWCraft")
        code = _INSTALL.main(
            ["--home", _posix_home(home), "--uninstall", "--no-path", "--no-vsix"],
            env={"PATH": "/usr/bin"},
            platform="posix",
            which=_which({}),
            archive_root=_posix_home(tmp_path / "archive"),
            run=journal.run,
            remove=journal.remove,
            echo=journal.echo,
        )
        assert code == 1
        captured = capsys.readouterr()
        assert "is gone" not in captured.out
        assert _posix_home(home) in captured.err

    def test_a_full_uninstall_removes_the_path_entry_and_the_extension(self, tmp_path: Path) -> None:
        home = tmp_path / "SWCraft"
        (home / _INSTALL.VERSIONS_DIRNAME / _VERSION).mkdir(parents=True)
        # A directory this host can `stat` and a spelling the Windows plan calls absolute: `require_installation`
        # reads the real filesystem while `home_is_absolute` reads the target's rules. On POSIX the `//`-rooted
        # spelling satisfies both (Windows reads it as a UNC root; macOS and Linux resolve `//` to `/`).
        given = str(home) if os.name == "nt" else "//" + str(home).lstrip("/")
        # Spelled the way a Windows plan spells it -- the test lives on POSIX and the
        # plan does not: `str(PureWindowsPath("/a/b"))` is `\\a\\b`.
        entry = str(PureWindowsPath(given) / "current" / "Scripts")
        journal = Journal(value=(r"C:\tools;" + entry, Journal.REG_EXPAND_SZ))
        code = journal.main(
            ["--home", given, "--uninstall"],
            env={"PATH": "C:\\Windows"},
            platform="nt",
            which=_which({"code": _WIN_CODE}),
            archive_root=_posix_home(tmp_path / "archive"),
        )
        assert code == 0
        assert journal.kinds == ["remove", "registry-read", "registry-write", "registry-broadcast", "run", "remove"]
        assert journal.calls[2] == ("registry-write", r"C:\tools", Journal.REG_EXPAND_SZ)
        assert journal.calls[4][1:5] == ("cmd.exe", "/c", "call", _WIN_CODE)


# ---------------------------------------------------------------------------
# The real archive, really installed (deselected; `dev.py test-artifacts`)
# ---------------------------------------------------------------------------


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _generated_arxml_digest(swcraft: Path, work: Path, label: str, arxml_version: str) -> str:
    """The SHA-256 of ``examples/basic``'s ARXML, generated by *swcraft*.

    The example is copied into *work* first, so the checkout's model is never written, and each
    ``(installer, version)`` pair gets its own copy so no run can read another's output.
    """
    project = work / f"{label}-{arxml_version}"
    shutil.copytree(REPO_ROOT / "examples" / "basic", project)
    result = subprocess.run(
        [
            str(swcraft),
            "generate",
            "arxml",
            "--project",
            str(project / "swcraft.yaml"),
            "--arxml-version",
            arxml_version,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return _digest(project / "dist" / "model.arxml")


def _fixture_agent_wheel(directory: Path, version: str, requires: str) -> Path:
    """A metadata-only ``swcraft-agent`` wheel of *version* that requires *requires*.

    The newer version the upgrade case installs (no second release is available to a test): it pulls in the real
    ``swcraft-mcp`` of the wheelhouse beside it, so the installation is a working one.
    """
    return _fixture_wheel(directory, "swcraft-agent", version, {}, requires=requires)


def _fixture_generator_wheel(directory: Path, version: str) -> Path:
    """A ``swcraft-specimens`` wheel of *version* whose console script answers ``swcraft-specimens <version>``.

    The newer generator the upgrade case installs beside the newer ``swcraft-agent``: the
    installer names both at one version, and no second release of the real generator is
    available to a test. It carries only the ``--version`` answer, which is all the case asks.
    """
    files = {
        "swcraft_specimens/__init__.py": "",
        "swcraft_specimens/cli.py": f"def main():\n    print('swcraft-specimens {version}')\n    return 0\n",
    }
    return _fixture_wheel(
        directory,
        "swcraft-specimens",
        version,
        files,
        entry_points="[console_scripts]\nswcraft-specimens = swcraft_specimens.cli:main\n",
    )


def _fixture_wheel(
    directory: Path,
    distribution: str,
    version: str,
    payload: dict[str, str],
    *,
    requires: str | None = None,
    entry_points: str | None = None,
) -> Path:
    """A pure-Python wheel of *distribution* *version* holding *payload*, stdlib only, its ``RECORD`` exact."""
    escaped = distribution.replace("-", "_")
    info = f"{escaped}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n"
    if requires is not None:
        metadata += f"Requires-Dist: {requires}\n"
    files = {
        **payload,
        f"{info}/METADATA": metadata,
        f"{info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: test_workstation_install\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    if entry_points is not None:
        files[f"{info}/entry_points.txt"] = entry_points
    record = []
    for name, text in files.items():
        data = text.encode("utf-8")
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
        record.append(f"{name},sha256={digest},{len(data)}\n")
    record.append(f"{info}/RECORD,,\n")
    wheel = directory / f"{escaped}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
        archive.writestr(f"{info}/RECORD", "".join(record))
    return wheel


def _install_the_archive(unpacked_archive: Path, home: Path) -> Path:
    """Run the archive's own ``install.py`` into *home* and return *home*.

    The argv and its flags' reasons are :func:`conftest.install_workstation_archive`'s, shared with the standalone
    architecture harness.
    """
    install_workstation_archive(unpacked_archive, home)
    return home


@pytest.fixture
def disposable_archive_install(workstation_archive_unpacked: Path, tmp_path: Path) -> Path:
    """An installation of its own, for a case that destroys what it is given.

    Not the shared session-scoped ``archive_install``: a case that uninstalls the shared home would leave every later
    case with no installation, so the class would pass only while that case ran last.
    """
    return _install_the_archive(workstation_archive_unpacked, tmp_path / "SWCraft")


@pytest.fixture(scope="module")
def wheel_installed_reference(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A venv with the same version installed from the wheelhouse, not the archive.

    The independent oracle's other side: the same wheels, installed by ``build_artifacts.verify_clean_install``, the
    call ``test_clean_install.py``'s ``clean_install`` fixture makes. Built here because a pytest fixture cannot cross
    a test module; the cost is one extra venv per ``test-artifacts`` run.
    """
    require_wheelhouse()
    venv_dir = tmp_path_factory.mktemp("wheel-reference") / "venv"
    _ARTIFACTS.verify_clean_install(REPO_ROOT, venv_dir=venv_dir)
    return venv_dir


@pytest.mark.artifact
class TestTheRealArchiveInstalls:
    """The archive built by the preceding ``workstation-archive`` stage, installed.

    Asks the installation what the oracle names, the floor the ``vsix`` step reads out of the archive's ``.vsix``,
    whether the unpacked ``node`` answers with the pinned version beside the wheel's elkjs, and whether
    ``swcraft-specimens`` answers with the release and is replaced by a newer install. This is evidence about the
    platform the suite runs on and no other: the junction, the registry edit and the ``code`` call are reached by no
    case here (``--no-path --no-vsix`` keeps the last two out); their seam-level assertions are in
    :class:`TestExecutionSeams` and :class:`TestMain`.

    No case depends on order: the one that uninstalls builds an installation of its own
    (:func:`disposable_archive_install`), so the shared session-scoped ``archive_install`` is only read or repaired.
    """

    def test_the_installer_in_the_archive_is_the_one_in_the_checkout(self, workstation_archive_unpacked: Path) -> None:
        assert _digest(workstation_archive_unpacked / "install.py") == _digest(_SCRIPT_PATH)

    def test_the_floor_read_from_the_archive_is_the_manifests_own(self, workstation_archive_unpacked: Path) -> None:
        """The floor read from the shipped ``.vsix`` is ``vscode_extension/package.json``'s ``engines.vscode``.

        Every case above works on a zip this file wrote; this one reads the ``.vsix`` the stage shipped. If the two
        differ, the installer compares a host against a floor the extension is not installed under. Not reached:
        whether that floor is the right one -- nothing re-measures the pack members' floors.
        """
        vsix = [path for path in workstation_archive_unpacked.iterdir() if path.suffix == ".vsix"]
        assert len(vsix) == 1, vsix
        manifest = json.loads((REPO_ROOT / "vscode_extension" / "package.json").read_text(encoding="utf-8"))
        assert _INSTALL.engine_floor(str(vsix[0])) == manifest["engines"]["vscode"]

    def test_the_installed_swcraft_reports_the_version(self, archive_install: Path) -> None:
        home = archive_install
        result = subprocess.run(
            [str(installed_executable(home, "swcraft")), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.strip().endswith(_ARTIFACTS.read_version(REPO_ROOT))

    def test_every_swcraft_package_resolves_from_inside_the_version_directory(self, archive_install: Path) -> None:
        """Every SWCraft package of an installation resolves from inside its version directory.

        Paths are compared resolved: ``current`` is a link, so the interpreter may report either spelling.
        """
        home = archive_install
        version = _ARTIFACTS.read_version(REPO_ROOT)
        report = _ARTIFACTS.probe_environment(installed_executable(home, "python"))
        expected = (home / _INSTALL.VERSIONS_DIRNAME / version).resolve()
        assert report["distributions"] == {
            "swcraft-tools": version,
            "swcraft-mcp": version,
            "swcraft-agent": version,
        }
        for name, location in report["modules"].items():
            assert not str(location).startswith("ERROR"), f"{name}: {location}"
            assert expected in Path(str(location)).resolve().parents, f"{name} resolved from {location}"
        assert report["editable_markers"] == []

    def test_the_arxml_is_byte_identical_to_the_wheel_installed_reference(
        self, archive_install: Path, wheel_installed_reference: Path, tmp_path: Path
    ) -> None:
        """The independent oracle for the packaging claim.

        Two installations of the same version -- from the archive and from the wheelhouse -- generate
        ``examples/basic`` at both ARXML versions, and the four files are compared by digest. It proves that composing
        the archive changed no exporter byte; it proves nothing about Windows.
        """
        home = archive_install
        installed = installed_executable(home, "swcraft")
        reference = _ARTIFACTS.venv_interpreter(wheel_installed_reference).parent / installed.name
        digests = {}
        for arxml_version in ("4.2.2", "4.3.0"):
            digests[arxml_version] = (
                _generated_arxml_digest(installed, tmp_path, "archive", arxml_version),
                _generated_arxml_digest(reference, tmp_path, "wheel", arxml_version),
            )
        sys.stdout.write(f"\nARXML digests (archive, wheel): {digests}\n")
        for arxml_version, (from_archive, from_wheel) in digests.items():
            assert from_archive == from_wheel, arxml_version
        assert digests["4.2.2"][0] != digests["4.3.0"][0], "the two schema versions must not be the same bytes"

    def test_a_second_install_of_the_same_archive_is_a_repair(
        self, archive_install: Path, workstation_archive_unpacked: Path
    ) -> None:
        home = archive_install
        version = _ARTIFACTS.read_version(REPO_ROOT)
        before = sorted(path.name for path in (home / _INSTALL.VERSIONS_DIRNAME).iterdir())
        result = subprocess.run(
            [
                sys.executable,
                str(workstation_archive_unpacked / "install.py"),
                "--home",
                str(home),
                "--python",
                sys.executable,
                "--no-path",
                "--no-vsix",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert sorted(path.name for path in (home / _INSTALL.VERSIONS_DIRNAME).iterdir()) == before == [version]
        assert (home / _INSTALL.CURRENT_LINK_NAME).resolve() == (home / _INSTALL.VERSIONS_DIRNAME / version).resolve()
        again = subprocess.run(
            [str(installed_executable(home, "swcraft")), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert again.returncode == 0 and again.stdout.strip().endswith(version)

    def test_the_installed_node_answers_with_its_pinned_version_beside_the_wheels_elkjs(
        self, archive_install: Path
    ) -> None:
        """The Node.js payload, unpacked from the real archive and run; and the elkjs payload, found.

        ``node`` is asked through the installation's own executable directory (the link, or on Windows the copy, the
        ``node`` step placed), and the expected version is read from the committed pins. Node runs only the diagram
        layout, over the elkjs files the installed ``swcraft-tools`` wheel carries, whose version is compared with the
        pin. Not reached: a layout run, and any platform but this one.
        """
        home = archive_install
        # The builder's constant is repository-relative (it resolves against
        # `dev.py`'s working directory), and this stage runs from `packages/tools/`.
        pins = json.loads((REPO_ROOT / _ARTIFACTS.RUNTIME_PINS_FILE).read_text(encoding="utf-8"))
        node = installed_executable(home, "node")
        answered = subprocess.run([str(node), "--version"], capture_output=True, text=True, encoding="utf-8")
        assert answered.returncode == 0, answered.stdout + answered.stderr
        assert answered.stdout.strip() == "v" + pins["node"]["version"]

        version_dir = home / _INSTALL.VERSIONS_DIRNAME / _ARTIFACTS.read_version(REPO_ROOT)
        if node.is_symlink():
            assert os.readlink(node) == "../runtime/node/bin/node"
            assert node.resolve() == (version_dir / "runtime" / "node" / "bin" / "node").resolve()
        assert sorted(path.name for path in (version_dir / "runtime").iterdir()) == ["node"], (
            "version-free directories, no likec4 tree, and no staging directory left behind"
        )
        manifests = list(version_dir.rglob("swcraft_tools/writer/diagrams/elkjs/package.json"))
        assert len(manifests) == 1, manifests
        assert json.loads(manifests[0].read_text(encoding="utf-8"))["version"] == pins["elkjs"]["version"]

    def test_the_installed_generator_reports_the_release_version(self, archive_install: Path) -> None:
        """The specimen generator's console script sits beside ``swcraft`` and answers with the release version.

        Not reached: whether the generator's output is what the specimens pin (``test_archive_specimen_generator.py``).
        """
        home = archive_install
        version = _ARTIFACTS.read_version(REPO_ROOT)
        generator = installed_executable(home, "swcraft-specimens")
        result = subprocess.run([str(generator), "--version"], capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.split() == ["swcraft-specimens", version]
        version_dir = (home / _INSTALL.VERSIONS_DIRNAME / version).resolve()
        assert version_dir in generator.resolve().parents, "installed into the version's own venv"

    def test_a_newer_install_replaces_the_generator_with_its_own(
        self, disposable_archive_install: Path, workstation_archive_unpacked: Path, tmp_path: Path
    ) -> None:
        """An install over an older one replaces ``swcraft-specimens`` by construction.

        A fixture newer ``swcraft-agent`` and a fixture newer ``swcraft-specimens`` are added
        to a copy of the wheelhouse (its ``PLATFORM.json`` travels with it) and installed with
        ``--source``. ``current`` switches to the new venv, the old version directory is
        removed, and ``current/bin/swcraft-specimens`` resolves inside ``versions/<new>/`` and
        answers the newer version: the old generator left with the old SWCraft. What it does
        not reach: an upgrade between two real releases, which no test has.
        """
        home = disposable_archive_install
        version = _ARTIFACTS.read_version(REPO_ROOT)
        newer = "99.0.0"
        wheels = tmp_path / "wheels"
        shutil.copytree(workstation_archive_unpacked / _INSTALL.WHEELS_DIRNAME, wheels)
        _fixture_agent_wheel(wheels, newer, f"swcraft-mcp=={version}")
        _fixture_generator_wheel(wheels, newer)
        result = subprocess.run(
            [
                sys.executable,
                str(workstation_archive_unpacked / "install.py"),
                "--home",
                str(home),
                "--python",
                sys.executable,
                "--source",
                str(wheels),
                "--version",
                newer,
                "--no-path",
                "--no-vsix",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        new_dir = (home / _INSTALL.VERSIONS_DIRNAME / newer).resolve()
        assert sorted(path.name for path in (home / _INSTALL.VERSIONS_DIRNAME).iterdir()) == [newer]
        assert (home / _INSTALL.CURRENT_LINK_NAME).resolve() == new_dir
        generator = installed_executable(home, "swcraft-specimens")
        assert new_dir in generator.resolve().parents, f"{generator} resolves to {generator.resolve()}"
        answered = subprocess.run([str(generator), "--version"], capture_output=True, text=True, encoding="utf-8")
        assert answered.returncode == 0, answered.stdout + answered.stderr
        assert answered.stdout.split() == ["swcraft-specimens", newer]

    def test_the_real_archive_installs_through_its_wrapper_with_no_python_named(
        self, workstation_archive_unpacked: Path, tmp_path: Path
    ) -> None:
        """The wrapper installs the real archive with no ``--python`` named; every other install passes one.

        The wrapper -- ``swcraft_install.sh``, or ``swcraft_install.cmd`` through ``cmd.exe`` on Windows -- checks the
        carried Python, unpacks its bootstrap copy and runs ``install.py`` on it; the venv is built on the
        installation's own unpacked copy, which ``pyvenv.cfg`` names. A copy of the archive is used, because the
        wrapper writes ``.bootstrap/`` into it.

        The console stays quiet: the wrapper's ``preparing the installer ... done``, then one numbered ``done`` line
        per step -- eight on the fresh install, seven on the repair, which builds no venv -- and none of pip's output,
        which is in ``install.log``.

        Not reached: any platform but this one, a machine with no Python at all, and the lines rendered live in an
        interactive console.
        """
        unpacked = tmp_path / "archive"
        shutil.copytree(workstation_archive_unpacked, unpacked)
        home = tmp_path / "SWCraft"
        wrapper = (
            ["cmd.exe", "/c", str(unpacked / "swcraft_install.cmd")]
            if os.name == "nt"
            else [
                "/bin/sh",
                str(unpacked / "swcraft_install.sh"),
            ]
        )
        result = subprocess.run(
            [*wrapper, "--home", str(home), "--no-path", "--no-vsix"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=dict(os.environ, SWCRAFT_NO_PAUSE="1"),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        self._assert_the_quiet_console(result.stdout, total=8)
        # On Windows the locked `.bootstrap` is the wrapper's to remove; on POSIX the removal succeeds. Either way no
        # stdout line names it.
        assert [line for line in result.stdout.splitlines() if ".bootstrap" in line] == [], result.stdout

        version = _ARTIFACTS.read_version(REPO_ROOT)
        answered = subprocess.run(
            [str(installed_executable(home, "swcraft")), "--version"], capture_output=True, text=True, encoding="utf-8"
        )
        assert answered.returncode == 0 and answered.stdout.strip().endswith(version), answered.stdout
        version_dir = home / _INSTALL.VERSIONS_DIRNAME / version
        carried = _INSTALL.installed_python(version_dir, os.name)
        assert Path(carried).is_file()
        config = {
            key.strip(): value.strip()
            for key, _, value in (
                line.partition("=") for line in (version_dir / "pyvenv.cfg").read_text(encoding="utf-8").splitlines()
            )
        }
        assert Path(config["home"]).resolve() == Path(carried).parent.resolve(), (
            "the venv is built on the installation's own copy of the carried Python"
        )
        pins = json.loads((REPO_ROOT / _ARTIFACTS.RUNTIME_PINS_FILE).read_text(encoding="utf-8"))
        reported = subprocess.run(
            [str(installed_executable(home, "python")), "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert reported.stdout.strip() == pins["python"]["version"], reported.stdout + reported.stderr
        # On Windows the interpreter running the installer lives inside `.bootstrap` until `install.py` exits, so the
        # `.cmd` wrapper removes it afterwards.
        assert not (unpacked / ".bootstrap").exists(), "the bootstrap copy is gone once the wrapper returns"

        # A repair on the carried Python: the same wrapper unpacks both runtime trees again beside the ones in place
        # -- renamed aside, never deleted first -- and the installation still answers.
        again = subprocess.run(
            [*wrapper, "--home", str(home), "--no-path", "--no-vsix"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=dict(os.environ, SWCRAFT_NO_PAUSE="1"),
        )
        assert again.returncode == 0, again.stdout + again.stderr
        self._assert_the_quiet_console(again.stdout, total=7)
        logged = (home / _INSTALL.LOG_FILENAME).read_text(encoding="utf-8", errors="replace").splitlines()
        # The one pip call names `swcraft-agent` and, after it, the specimen generator, so the
        # line is found by the item, never by how it ends.
        pip_lines = [line for line in logged if line.startswith("$ ") and f"swcraft-agent=={version}" in line.split()]
        assert len(pip_lines) == 1 and "--force-reinstall" in pip_lines[0], logged
        assert f"swcraft-specimens=={version}" in pip_lines[0].split(), pip_lines[0]
        assert any(line.startswith("Successfully installed") for line in logged), "pip's own output is in the log"
        answered = subprocess.run(
            [str(installed_executable(home, "swcraft")), "--version"], capture_output=True, text=True, encoding="utf-8"
        )
        assert answered.returncode == 0 and answered.stdout.strip().endswith(version), answered.stdout
        runtime = sorted(path.name for path in (version_dir / "runtime").iterdir())
        if os.name == "nt":
            assert {"node", "python"} <= set(runtime), runtime
        else:
            assert runtime == ["node", "python"], "no staging or superseded tree left behind"

    @staticmethod
    def _assert_the_quiet_console(stdout: str, *, total: int) -> None:
        """The wrapper's line first, then ``1/N`` .. ``N/N`` each ``done``, and none of pip's output."""
        lines = stdout.splitlines()
        assert lines[0] == "preparing the installer ... done", stdout
        numbered = [
            match
            for match in (re.fullmatch(r"(\d+)/(\d+) .+ \.\.\. (done|failed), \d+ s", line) for line in lines)
            if match
        ]
        assert [(int(match.group(1)), int(match.group(2)), match.group(3)) for match in numbered] == [
            (index, total, "done") for index in range(1, total + 1)
        ], stdout
        assert lines[1].startswith("Installing SWCraft ") and lines[2] == numbered[0].group(0), stdout
        assert not any(line.startswith(("Collecting", "Successfully installed")) for line in lines), stdout

    def test_uninstall_leaves_no_home_and_never_reaches_code(
        self, disposable_archive_install: Path, workstation_archive_unpacked: Path, tmp_path: Path
    ) -> None:
        """An uninstall of the real installation leaves no home and never runs ``code``.

        A fake ``code`` first on the subprocess' ``PATH`` writes a marker when run, so a ``--no-vsix`` that stopped
        applying to ``--uninstall`` would show. The registry half cannot be witnessed on this platform; it is
        :meth:`TestMain.test_uninstall_touches_neither_code_nor_the_registry`.
        """
        home = disposable_archive_install
        marker = tmp_path / "code-was-run"
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        fake_code = fake_bin / "code"
        fake_code.write_text(f'#!/bin/sh\necho "$@" >> "{marker}"\n', encoding="utf-8", newline="\n")
        fake_code.chmod(0o755)
        environment = dict(os.environ)
        environment["PATH"] = str(fake_bin) + os.pathsep + environment.get("PATH", "")
        result = subprocess.run(
            [
                sys.executable,
                str(workstation_archive_unpacked / "install.py"),
                "--home",
                str(home),
                "--uninstall",
                "--no-path",
                "--no-vsix",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert not home.exists()
        assert not marker.exists(), "--no-vsix applies to --uninstall: code must not be run"
