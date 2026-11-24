"""Tests for the offline wheelhouse and the clean-install acceptance.

* The fast half exercises the wheelhouse logic -- requirements, ``PLATFORM.json``,
  refusal of a foreign or corrupt wheelhouse, the acceptance's pip argv -- with an
  injected runner: nothing is downloaded, no venv created.
* The ``artifact`` half (``dev.py test-artifacts``) builds the artifacts, populates
  the wheelhouse (the network) and installs **only** ``swcraft-agent==<version>``
  with ``--no-index`` into a fresh venv: ``swcraft-mcp`` and ``swcraft-tools`` must
  arrive through the declared dependency graph, never an editable install.

The workstation archive is composed from this wheelhouse, so a bad wheelhouse is
refused here, before it is zipped; ``build_artifacts.verify_clean_install`` builds
this venv and the archive acceptance's reference alike.

The artifact half also runs the in-repo greenfield protocol flow (the same scenario
module) from the installed venv, in two probes under ``python -I``:
:data:`_INSTALLED_SURFACE_PROBE` with nothing of this repository on ``sys.path``
(``tools/list``, the templates through ``importlib.resources``, the registry), and
:data:`_INSTALLED_GREENFIELD_PROBE`, which imports the shared scenario definition
and asserts no ``swcraft*`` module came from the checkout. Neither may skip: a
skipped acceptance reports the opposite of what it checked.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import sysconfig
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import REPO_ROOT, build_artifacts_module

_ARTIFACTS = build_artifacts_module()

_VERSION = "9.9.9"


# ---------------------------------------------------------------------------
# The MCP inventory, derived from the frozen decision table
# ---------------------------------------------------------------------------
#
# Read from `tool-projections.json`, as `packages/mcp/tests/test_protocol_surface.py`
# does (that suite cannot be imported from here), and pinned in the fast gate by
# `test_the_derived_inventory_is_thirteen_and_the_removed_set_is_thirty_two`.
# Scoped to `tools/list`: most removed names are also internal `swcraft_tools`
# writer functions, so a broader grep would be wrong.

_TOOL_PROJECTIONS: dict[str, list[str]] = json.loads(
    (
        REPO_ROOT
        / "packages"
        / "tools"
        / "tests"
        / "fixtures"
        / "contracts"
        / "editing-protocol"
        / "tool-projections.json"
    ).read_text(encoding="utf-8")
)["projections"]

#: The protocol's generic tools and `find`; none has a row in the decision table.
PROTOCOL_TOOLS: tuple[str, ...] = ("discover", "read", "find", "change_set")


def _withdrawn_tools() -> frozenset[str]:
    return frozenset(_TOOL_PROJECTIONS["withdrawn"])


#: The schema-coupled tools the protocol replaced, and the retained tools withdrawn since.
REMOVED_TOOLS: frozenset[str] = frozenset(_TOOL_PROJECTIONS["replaced"]) | _withdrawn_tools()

#: The tools registered after the table, each a question of its own with no row.
ADDED_QUESTIONS: frozenset[str] = frozenset({"architecture_metrics", "guide"})

#: The final surface: protocol tools, retained minus withdrawn, and the added questions.
EXPECTED_INVENTORY: frozenset[str] = (
    frozenset(PROTOCOL_TOOLS) | (frozenset(_TOOL_PROJECTIONS["retained"]) - _withdrawn_tools()) | ADDED_QUESTIONS
)

#: The ``--integration`` profile whose template is package data inside the
#: ``swcraft-tools`` wheel, and files that must come out of the installed wheel.
TEMPLATE_PROFILE = "moon"
TEMPLATE_PACKAGE_DIRNAME = "vehicle-architecture"
TEMPLATE_CARRIED_FILES = (
    ".moon/workspace.yml",
    ".prototools",
    # A dot-directory, which packaging tools have historically dropped; also `init`'s
    # one derived write (cSpell ignore paths rendered from the manifest's roots).
    ".vscode/settings.json",
    # The model gate needs exactly one processor, so `init` scaffolds a processor,
    # a core and a partition on it.
    "model/hardware/CPU.yaml",
    "model/hardware/CORE0.yaml",
    "model/hardware/PARTITION0.yaml",
    "moon.yml",
    # A plain text member: ordinary files come out of the wheel as dot-directories do.
    "README.md",
)


def test_the_derived_inventory_is_thirteen_and_the_removed_set_is_thirty_two() -> None:
    """The artifact tests' two sets are pinned in the fast gate, where a drifted derivation shows."""
    rows = _TOOL_PROJECTIONS["rows"]
    assert len(rows) == 39, "the decision table is the frozen 39-row disposition of the pre-19.3 surface"
    assert len(EXPECTED_INVENTORY) == 13
    assert len(REMOVED_TOOLS) == 32
    assert len(_withdrawn_tools()) == 3
    assert EXPECTED_INVENTORY & REMOVED_TOOLS == frozenset()
    assert frozenset(PROTOCOL_TOOLS) <= EXPECTED_INVENTORY
    # The names the protocol added are new, not recycled removals.
    assert frozenset(PROTOCOL_TOOLS) & frozenset(rows) == frozenset()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _make_repo_with_wheels(tmp_path: Path, version: str = _VERSION) -> Path:
    """A repo tree carrying the version module and four built wheels."""
    repo_root = tmp_path / "repo"
    version_module = repo_root / _ARTIFACTS.VERSION_MODULE
    version_module.parent.mkdir(parents=True)
    version_module.write_text(f'__version__ = "{version}"\n', encoding="utf-8")

    for project, pyproject in (
        ("tools", '[project]\nname = "swcraft-tools"\ndependencies = ["pyyaml>=6.0", "lxml>=5.0"]\n'),
        (
            "mcp",
            '[project]\nname = "swcraft-mcp"\ndependencies = ["fastmcp>=2.0", "pyyaml>=6.0", "swcraft-tools==9.9.9"]\n',
        ),
        (
            "agent",
            '[project]\nname = "swcraft-agent"\ndependencies = ["openai>=1.30", "mcp[cli]", "swcraft_mcp == 9.9.9"]\n',
        ),
        (
            "specimens",
            '[project]\nname = "swcraft-specimens"\n'
            'dependencies = ["PyYAML>=6.0,<7", "numpy>=2.4.6,<3", "scipy>=1.17.1,<2"]\n',
        ),
    ):
        (repo_root / _ARTIFACTS.PACKAGES_DIR / project).mkdir(parents=True, exist_ok=True)
        (repo_root / _ARTIFACTS.PACKAGES_DIR / project / "pyproject.toml").write_text(pyproject, encoding="utf-8")

    python_dir = repo_root / _ARTIFACTS.DIST_DIRNAME / _ARTIFACTS.PYTHON_DIRNAME
    python_dir.mkdir(parents=True)
    for distribution in _ARTIFACTS.SWCRAFT_DISTRIBUTIONS:
        _write_wheel(python_dir / f"{distribution}-{version}-py3-none-any.whl", distribution)
    _seed_the_lock(repo_root)
    return repo_root


#: The fixture lock, in ``uv``'s shape: exact pins, ``# via`` lines, and the three marker kinds the
#: committed lock carries -- an interpreter-version clause alone, a platform clause alone, and both.
_FIXTURE_LOCK = (
    "# This file was autogenerated by uv via the following command:\n"
    "#    python scripts/dev.py update-dev-lock (uv 0.13.6)\n"
    "backports-tarfile==1.2.0 ; python_full_version < '3.12'\n"
    "    # via jaraco-context\n"
    "httpx2-jsfetch==1.0 ; python_full_version >= '3.12' and sys_platform == 'emscripten'\n"
    "jeepney==0.9.0 ; sys_platform == 'linux'\n"
    "numpy==2.4.6\n"
    "    # via\n"
    "    #   swcraft-specimens\n"
    "    #   swcraft-tools\n"
    "pyyaml==6.0.3\n"
    "scipy==1.17.1\n"
)

#: :data:`_FIXTURE_LOCK` rendered for the runtime's CPython 3.11.
_FIXTURE_CONSTRAINTS = (
    "backports-tarfile==1.2.0\njeepney==0.9.0 ; sys_platform == 'linux'\nnumpy==2.4.6\npyyaml==6.0.3\nscipy==1.17.1\n"
)


def _seed_the_lock(repo_root: Path, lock: str = _FIXTURE_LOCK) -> None:
    """Seed the two inputs the wheelhouse's constraints come from.

    They are the committed runtime pins (the runtime's CPython version) and a fixture lock.
    """
    runtime_pins = repo_root / _ARTIFACTS.RUNTIME_PINS_FILE
    runtime_pins.parent.mkdir(parents=True, exist_ok=True)
    runtime_pins.write_bytes((REPO_ROOT / _ARTIFACTS.RUNTIME_PINS_FILE).read_bytes())
    constraints = repo_root / _ARTIFACTS.DEV_LOCK_FILE
    constraints.parent.mkdir(parents=True, exist_ok=True)
    constraints.write_text(lock, encoding="utf-8", newline="\n")


def _write_wheel(path: Path, distribution: str) -> Path:
    """A minimal but genuinely readable zip, so the corrupt-archive guard is real."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{distribution}/__init__.py", "")
    return path


class WheelhouseRunner:
    """Records argv, captures the requirements file, materializes fake downloads."""

    def __init__(self, downloads: tuple[str, ...] = ("pyyaml-6.0.3-py3-none-any.whl",)) -> None:
        self.calls: list[list[str]] = []
        self.requirements_files: list[str] = []
        self.downloads = downloads

    def __call__(self, argv, cwd) -> None:
        argv = [str(part) for part in argv]
        self.calls.append(argv)
        if "-r" in argv:
            # Read during the call: the builder deletes the temp dir when pip returns.
            self.requirements_files.append(Path(argv[argv.index("-r") + 1]).read_text(encoding="utf-8"))
        if "download" in argv:
            dest = Path(argv[argv.index("--dest") + 1])
            dest.mkdir(parents=True, exist_ok=True)
            for filename in self.downloads:
                (dest / filename).write_text("third-party wheel", encoding="utf-8")


# ---------------------------------------------------------------------------
# Requirement collection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requirement", "name"),
    [
        ("pyyaml>=6.0", "pyyaml"),
        ("mcp[cli]", "mcp"),
        ("swcraft-tools==0.1.0", "swcraft-tools"),
        ("swcraft_mcp == 9.9.9", "swcraft-mcp"),
        # No current pin uses `<`, so it would otherwise go unexercised.
        ("lxml<6", "lxml"),
        ("python-dotenv>=1.0; python_version >= '3.11'", "python-dotenv"),
    ],
)
def test_requirement_name_canonicalizes(requirement: str, name: str) -> None:
    assert _ARTIFACTS.requirement_name(requirement) == name


def test_third_party_requirements_covers_all_four_projects_and_excludes_our_own(tmp_path: Path) -> None:
    repo_root = _make_repo_with_wheels(tmp_path)

    requirements = _ARTIFACTS.third_party_requirements(repo_root)

    # Deduplicated and sorted; the lock alone pins the versions.
    assert requirements == [
        "PyYAML>=6.0,<7",
        "fastmcp>=2.0",
        "lxml>=5.0",
        "mcp[cli]",
        "numpy>=2.4.6,<3",
        "openai>=1.30",
        "pyyaml>=6.0",
        "scipy>=1.17.1,<2",
    ]
    assert not any(entry.startswith("swcraft") for entry in requirements), (
        "the SWCraft wheels come from dist/python/, never from an index"
    )


def test_third_party_requirements_matches_the_real_pyprojects() -> None:
    """Grounded in the actual tree: a new dependency lands in the wheelhouse for free."""
    requirements = _ARTIFACTS.third_party_requirements(REPO_ROOT)

    names = {_ARTIFACTS.requirement_name(entry) for entry in requirements}
    assert {"lxml", "scipy", "pydantic", "pyyaml", "xmlschema", "fastmcp", "openai", "mcp", "numpy"} <= names
    assert not any(name.startswith("swcraft-") for name in names)
    # The ARXML writer is first-party, so `autosar` must not reach the wheelhouse;
    # a subset assertion cannot say that.
    assert "autosar" not in names


# ---------------------------------------------------------------------------
# PLATFORM.json
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _the_stand_in_python_answers_as_this_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the stand-in ``python_executable="python"`` as the running interpreter.

    ``wheelhouse_platform`` asks the interpreter it resolved on, and the bare name would
    reach whatever the host's ``PATH`` holds; every other path is asked for real.
    """
    asked = _ARTIFACTS.probe_interpreter

    def probe(executable):
        if executable == "python":
            version = ".".join(str(part) for part in sys.version_info[:3])
            return {"python_version": version, "platform_tag": sysconfig.get_platform()}
        return asked(executable)

    monkeypatch.setattr(_ARTIFACTS, "probe_interpreter", probe)


def test_wheelhouse_platform_records_the_running_interpreter() -> None:
    stamp = _ARTIFACTS.wheelhouse_platform(datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC))

    assert stamp == {
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "platform_tag": sysconfig.get_platform(),
        "created_at": "2026-08-01T12:00:00Z",
    }


def test_check_wheelhouse_platform_accepts_a_matching_stamp(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / _ARTIFACTS.WHEELHOUSE_PLATFORM_FILENAME).write_text(
        json.dumps(_ARTIFACTS.wheelhouse_platform()), encoding="utf-8"
    )

    assert _ARTIFACTS.check_wheelhouse_platform(wheelhouse)["platform_tag"]


def test_check_wheelhouse_platform_reports_a_never_populated_wheelhouse(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()

    with pytest.raises(_ARTIFACTS.BuildArtifactsError, match="--wheelhouse"):
        _ARTIFACTS.check_wheelhouse_platform(wheelhouse)


def test_check_wheelhouse_platform_rejects_a_foreign_platform_actionably(tmp_path: Path) -> None:
    """A foreign wheelhouse must not be left to produce an opaque pip error."""
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    stamp = _ARTIFACTS.wheelhouse_platform()
    stamp["platform_tag"] = "win-amd64-from-another-machine"
    stamp["python_version"] = "3.9"
    (wheelhouse / _ARTIFACTS.WHEELHOUSE_PLATFORM_FILENAME).write_text(json.dumps(stamp), encoding="utf-8")

    with pytest.raises(_ARTIFACTS.BuildArtifactsError) as error:
        _ARTIFACTS.check_wheelhouse_platform(wheelhouse)

    message = str(error.value)
    assert "win-amd64-from-another-machine" in message
    assert "'3.9'" in message
    assert "python scripts/build_artifacts.py --wheelhouse" in message


def _write_stamp(wheelhouse: Path, **changes: str) -> dict[str, str]:
    wheelhouse.mkdir(parents=True, exist_ok=True)
    stamp = dict(_ARTIFACTS.wheelhouse_platform(), **changes)
    (wheelhouse / _ARTIFACTS.WHEELHOUSE_PLATFORM_FILENAME).write_text(json.dumps(stamp), encoding="utf-8", newline="\n")
    return stamp


def test_check_wheelhouse_platform_reads_a_macos_deployment_target_the_way_the_installer_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Against the running interpreter only family and architecture are compared, as ``install.py`` does.

    The carried build reports ``macosx-11.0-arm64`` and a development interpreter
    ``macosx-26.0-arm64`` for the same wheels; another architecture is still refused.
    """
    wheelhouse = tmp_path / "wheelhouse"
    _write_stamp(wheelhouse, platform_tag="macosx-11.0-arm64")

    monkeypatch.setattr(sysconfig, "get_platform", lambda: "macosx-26.0-arm64")
    assert _ARTIFACTS.check_wheelhouse_platform(wheelhouse)["platform_tag"] == "macosx-11.0-arm64"

    monkeypatch.setattr(sysconfig, "get_platform", lambda: "macosx-26.0-x86_64")
    with pytest.raises(_ARTIFACTS.BuildArtifactsError, match="this interpreter is 'macosx-26.0-x86_64'"):
        _ARTIFACTS.check_wheelhouse_platform(wheelhouse)


def test_check_wheelhouse_platform_against_the_carried_python_is_exact(tmp_path: Path) -> None:
    """Given the interpreter the closure was resolved FOR, a deployment target differing is a refusal."""
    wheelhouse = tmp_path / "wheelhouse"
    stamp = _write_stamp(wheelhouse, platform_tag="macosx-11.0-arm64")
    carried = {"python_version": stamp["python_version"], "platform_tag": "macosx-11.0-arm64"}

    assert _ARTIFACTS.check_wheelhouse_platform(wheelhouse, carried)["platform_tag"] == "macosx-11.0-arm64"
    with pytest.raises(_ARTIFACTS.BuildArtifactsError, match="the Python the archive carries is 'macosx-26.0-arm64'"):
        _ARTIFACTS.check_wheelhouse_platform(wheelhouse, dict(carried, platform_tag="macosx-26.0-arm64"))


def test_wheelhouse_platform_asks_a_named_interpreter_and_not_the_running_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """``PLATFORM.json`` describes the interpreter ``pip download`` ran on."""
    asked: list[str] = []

    def probe(executable: str) -> dict[str, str]:
        asked.append(executable)
        return {"python_version": "3.11.99", "platform_tag": "macosx-11.0-arm64"}

    monkeypatch.setattr(_ARTIFACTS, "probe_interpreter", probe)
    moment = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)

    assert _ARTIFACTS.wheelhouse_platform(moment, "/repo/dist/runtime/python/bin/python3") == {
        "python_version": "3.11",
        "platform_tag": "macosx-11.0-arm64",
        "created_at": "2026-08-01T12:00:00Z",
    }
    assert asked == ["/repo/dist/runtime/python/bin/python3"]
    _ARTIFACTS.wheelhouse_platform(moment, sys.executable)
    assert asked == ["/repo/dist/runtime/python/bin/python3"], "the running interpreter answers for itself"


def test_probe_interpreter_answers_what_a_real_interpreter_is() -> None:
    assert _ARTIFACTS.probe_interpreter(sys.executable) == {
        "python_version": ".".join(str(part) for part in sys.version_info[:3]),
        "platform_tag": sysconfig.get_platform(),
    }


def test_probe_interpreter_names_an_interpreter_that_will_not_start(tmp_path: Path) -> None:
    missing = tmp_path / "no-python-here"

    with pytest.raises(_ARTIFACTS.BuildArtifactsError) as excinfo:
        _ARTIFACTS.probe_interpreter(missing)

    assert str(missing) in str(excinfo.value)


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        pytest.param(1, "", "Fatal Python error: init_fs_encoding\n", id="non-zero-exit"),
        pytest.param(
            1, '{"python_version": "3.11.9", "platform_tag": "linux-x86_64"}\n', "", id="non-zero-exit-despite-json"
        ),
        pytest.param(0, "Python 3.11.9\n", "", id="not-json"),
        pytest.param(0, '{"python_version": "3.11.9"}\n', "", id="partial-json"),
        pytest.param(0, '{"python_version": "3.11.9", "platform_tag": 11}\n', "", id="a-value-that-is-not-text"),
        pytest.param(0, '["3.11.9", "linux-x86_64"]\n', "", id="json-that-is-not-an-object"),
    ],
)
def test_probe_interpreter_refuses_an_answer_it_cannot_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str, stderr: str
) -> None:
    """Every way the question fails that is not "will not start", each one sentence naming the interpreter."""
    interpreter = tmp_path / "python3"
    asked: list[list[str]] = []

    def run(argv, **_kwargs):
        asked.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    monkeypatch.setattr(_ARTIFACTS.subprocess, "run", run)

    with pytest.raises(_ARTIFACTS.BuildArtifactsError) as excinfo:
        _ARTIFACTS.probe_interpreter(interpreter)

    message = str(excinfo.value)
    assert message.startswith(f"{interpreter} did not say which Python and platform it is (exit {returncode}): ")
    assert (stderr or stdout).strip() in message
    assert asked == [[str(interpreter), "-I", "-c", _ARTIFACTS._INTERPRETER_STAMP_PROGRAM]]


def test_check_wheelhouse_platform_reports_unreadable_json(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / _ARTIFACTS.WHEELHOUSE_PLATFORM_FILENAME).write_text("{not json", encoding="utf-8")

    with pytest.raises(_ARTIFACTS.BuildArtifactsError, match="not readable JSON"):
        _ARTIFACTS.check_wheelhouse_platform(wheelhouse)


# ---------------------------------------------------------------------------
# Wheel presence / integrity
# ---------------------------------------------------------------------------


def test_require_swcraft_wheels_returns_all_four(tmp_path: Path) -> None:
    repo_root = _make_repo_with_wheels(tmp_path)

    wheels = _ARTIFACTS.require_swcraft_wheels(
        repo_root / _ARTIFACTS.DIST_DIRNAME / _ARTIFACTS.PYTHON_DIRNAME, _VERSION
    )

    assert [path.name for path in wheels] == [
        f"swcraft_tools-{_VERSION}-py3-none-any.whl",
        f"swcraft_mcp-{_VERSION}-py3-none-any.whl",
        f"swcraft_agent-{_VERSION}-py3-none-any.whl",
        f"swcraft_specimens-{_VERSION}-py3-none-any.whl",
    ]


def test_require_swcraft_wheels_names_a_missing_artifact(tmp_path: Path) -> None:
    repo_root = _make_repo_with_wheels(tmp_path)
    python_dir = repo_root / _ARTIFACTS.DIST_DIRNAME / _ARTIFACTS.PYTHON_DIRNAME
    (python_dir / f"swcraft_mcp-{_VERSION}-py3-none-any.whl").unlink()

    with pytest.raises(_ARTIFACTS.BuildArtifactsError) as error:
        _ARTIFACTS.require_swcraft_wheels(python_dir, _VERSION)

    assert f"swcraft_mcp-{_VERSION}-*.whl is missing" in str(error.value)


def test_require_swcraft_wheels_names_a_corrupt_artifact(tmp_path: Path) -> None:
    repo_root = _make_repo_with_wheels(tmp_path)
    python_dir = repo_root / _ARTIFACTS.DIST_DIRNAME / _ARTIFACTS.PYTHON_DIRNAME
    corrupt = python_dir / f"swcraft_agent-{_VERSION}-py3-none-any.whl"
    corrupt.write_text("truncated download", encoding="utf-8")

    with pytest.raises(_ARTIFACTS.BuildArtifactsError) as error:
        _ARTIFACTS.require_swcraft_wheels(python_dir, _VERSION)

    assert corrupt.name in str(error.value)
    assert "corrupt archive" in str(error.value)


# ---------------------------------------------------------------------------
# Wheelhouse builder
# ---------------------------------------------------------------------------


def test_build_wheelhouse_downloads_the_closure_and_copies_our_wheels(tmp_path: Path) -> None:
    repo_root = _make_repo_with_wheels(tmp_path)
    runner = WheelhouseRunner()

    wheelhouse = _ARTIFACTS.build_wheelhouse(repo_root, run=runner, python_executable="python", now=None)

    assert wheelhouse == repo_root / _ARTIFACTS.DIST_DIRNAME / _ARTIFACTS.WHEELHOUSE_DIRNAME

    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[:5] == ["python", "-m", "pip", "download", "--dest"]
    assert argv[5] == str(wheelhouse) + ".new", (
        "the download lands in a staging directory beside the wheelhouse, which is moved into "
        "place only once the closure is complete"
    )
    assert runner.requirements_files == [
        "PyYAML>=6.0,<7\nfastmcp>=2.0\nlxml>=5.0\nmcp[cli]\nnumpy>=2.4.6,<3\nopenai>=1.30\npyyaml>=6.0\nscipy>=1.17.1,<2\n"
    ]

    assert {path.name for path in wheelhouse.glob("*.whl")} == {
        f"swcraft_tools-{_VERSION}-py3-none-any.whl",
        f"swcraft_mcp-{_VERSION}-py3-none-any.whl",
        f"swcraft_agent-{_VERSION}-py3-none-any.whl",
        f"swcraft_specimens-{_VERSION}-py3-none-any.whl",
        "pyyaml-6.0.3-py3-none-any.whl",
    }


def test_build_wheelhouse_writes_platform_json(tmp_path: Path) -> None:
    repo_root = _make_repo_with_wheels(tmp_path)

    wheelhouse = _ARTIFACTS.build_wheelhouse(repo_root, run=WheelhouseRunner(), python_executable="python")

    recorded = json.loads((wheelhouse / _ARTIFACTS.WHEELHOUSE_PLATFORM_FILENAME).read_text(encoding="utf-8"))
    assert set(recorded) == {"python_version", "platform_tag", "created_at"}, "no bundled rows"
    assert _ARTIFACTS.check_wheelhouse_platform(wheelhouse) == recorded


# ---------------------------------------------------------------------------
# The resolution under the lock
# ---------------------------------------------------------------------------


def _download_argv(runner: WheelhouseRunner) -> list[str]:
    (argv,) = [call for call in runner.calls if "download" in call]
    return argv


def test_the_resolution_is_constrained_by_the_locks_lines_for_the_runtime_python(tmp_path: Path) -> None:
    """SWCraft's four projects are resolved in one ``pip download``, constrained by the lock.

    ``-c`` takes the lock's lines whose marker holds for the runtime's CPython -- so what ships is
    what ``.venv`` resolved.
    """
    repo_root = _make_repo_with_wheels(tmp_path)
    runner = WheelhouseRunner()
    constraints: list[str] = []

    def recording(argv, cwd) -> None:
        argv = [str(part) for part in argv]
        if "-c" in argv:
            constraints.append(Path(argv[argv.index("-c") + 1]).read_text(encoding="utf-8"))
        runner(argv, cwd)

    _ARTIFACTS.build_wheelhouse(repo_root, run=recording, python_executable="python")

    argv = _download_argv(runner)
    assert "-r" in argv and "-c" in argv
    assert not any(part.endswith(".whl") for part in argv), "no wheel is handed to the resolution any more"
    assert constraints == [_FIXTURE_CONSTRAINTS]


@pytest.mark.parametrize(
    ("python_version", "expected"),
    [
        ("3.11.16", _FIXTURE_CONSTRAINTS),
        (
            "3.12.0",
            "httpx2-jsfetch==1.0 ; sys_platform == 'emscripten'\njeepney==0.9.0 ; sys_platform == 'linux'\n"
            "numpy==2.4.6\npyyaml==6.0.3\nscipy==1.17.1\n",
        ),
    ],
)
def test_the_interpreter_clauses_are_decided_and_the_rest_left_to_pip(python_version: str, expected: str) -> None:
    """A failing version clause drops the line, a holding one is struck; a platform clause stays for pip."""
    assert _ARTIFACTS.lock_constraints_text(_FIXTURE_LOCK, python_version) == expected


@pytest.mark.parametrize(
    ("line", "python_version", "kept"),
    [
        ("a==1 ; python_version < '3.12'", "3.11.16", "a==1"),
        ("a==1 ; python_version >= '3.12'", "3.11.16", None),
        ("a==1 ; python_version == '3.11'", "3.11.16", "a==1"),
        ("a==1 ; python_full_version <= '3.11.15'", "3.11.16", None),
        ("a==1 ; python_full_version != '3.11.16'", "3.11.16", None),
        ('a==1 ; python_full_version > "3.11"', "3.11.16", "a==1"),
        # `or` and parentheses are pip's: the line is kept whole, for pip to evaluate.
        (
            "a==1 ; python_version < '3.12' or sys_platform == 'win32'",
            "3.12.0",
            "a==1 ; python_version < '3.12' or sys_platform == 'win32'",
        ),
        ("a==1 ; (python_version < '3.12')", "3.12.0", "a==1 ; (python_version < '3.12')"),
    ],
)
def test_one_lock_line_for_one_interpreter(line: str, python_version: str, kept: str | None) -> None:
    """``python_version`` is ``X.Y`` and ``python_full_version`` ``X.Y.Z``; ``or`` and parentheses are pip's."""
    rendered = _ARTIFACTS.lock_constraints_text(line + "\n", python_version)
    assert rendered == ("" if kept is None else kept + "\n")


def test_a_lock_line_that_is_no_pin_is_refused_by_name() -> None:
    with pytest.raises(_ARTIFACTS.BuildArtifactsError) as error:
        _ARTIFACTS.lock_constraints_text("numpy>=2\n", "3.11.16")
    assert "constraints/dev.txt" in str(error.value) and "numpy>=2" in str(error.value)


def test_an_unreadable_lock_is_a_sentence_and_nothing_is_downloaded(tmp_path: Path) -> None:
    repo_root = _make_repo_with_wheels(tmp_path)
    (repo_root / _ARTIFACTS.DEV_LOCK_FILE).unlink()
    runner = WheelhouseRunner()

    with pytest.raises(_ARTIFACTS.BuildArtifactsError) as error:
        _ARTIFACTS.build_wheelhouse(repo_root, run=runner, python_executable="python")

    assert "constraints/dev.txt cannot be read" in str(error.value)
    assert runner.calls == []
    assert not (repo_root / _ARTIFACTS.DIST_DIRNAME / _ARTIFACTS.WHEELHOUSE_DIRNAME).exists()


def test_a_conflict_only_pip_sees_is_refused_naming_the_lock(tmp_path: Path) -> None:
    """A conflict is pip's to find; the stage then fails with a sentence, not a traceback."""
    repo_root = _make_repo_with_wheels(tmp_path)

    def failing(argv, cwd) -> None:
        raise subprocess.CalledProcessError(1, list(argv))

    with pytest.raises(_ARTIFACTS.BuildArtifactsError) as error:
        _ARTIFACTS.build_wheelhouse(repo_root, run=failing, python_executable="python")

    message = str(error.value)
    assert "under the lock's versions" in message and "constraints/dev.txt" in message


def test_the_retired_closure_readers_are_gone() -> None:
    """The generator closure, its conflict check and the fetched wheel went with the private generator."""
    for retired in (
        "bundled_wheels",
        "FETCH_COMMAND",
        "closure_pins",
        "closure_conflicts",
        "closure_constraints_text",
        "specifier_admits",
        "wheel_requirements",
        "require_bundled_wheels",
    ):
        assert not hasattr(_ARTIFACTS, retired), retired


def test_build_wheelhouse_replaces_a_same_version_wheel_from_an_earlier_build(tmp_path: Path) -> None:
    """A stale wheel of the same version would shadow the one just built."""
    repo_root = _make_repo_with_wheels(tmp_path)
    wheelhouse = repo_root / _ARTIFACTS.DIST_DIRNAME / _ARTIFACTS.WHEELHOUSE_DIRNAME
    wheelhouse.mkdir(parents=True)
    stale = wheelhouse / f"swcraft_tools-{_VERSION}-py3-none-any.whl"
    stale.write_text("yesterday's build", encoding="utf-8")

    _ARTIFACTS.build_wheelhouse(repo_root, run=WheelhouseRunner(), python_executable="python")

    fresh = repo_root / _ARTIFACTS.DIST_DIRNAME / _ARTIFACTS.PYTHON_DIRNAME / stale.name
    assert stale.read_bytes() == fresh.read_bytes()
    assert zipfile.is_zipfile(stale)


def test_build_wheelhouse_empties_the_directory_before_it_downloads(tmp_path: Path) -> None:
    """A wheelhouse is rebuilt empty, so old versions do not accrete.

    With several versions of one distribution present, which one a ``--find-links``
    install picks is decided by pip's ordering over a directory listing.
    """
    repo_root = _make_repo_with_wheels(tmp_path)
    wheelhouse = repo_root / _ARTIFACTS.DIST_DIRNAME / _ARTIFACTS.WHEELHOUSE_DIRNAME
    wheelhouse.mkdir(parents=True)
    (wheelhouse / "lxml-6.1.1-cp311-cp311-macosx_11_0_arm64.whl").write_text("older", encoding="utf-8")
    (wheelhouse / _ARTIFACTS.WHEELHOUSE_PLATFORM_FILENAME).write_text("{}", encoding="utf-8")

    _ARTIFACTS.build_wheelhouse(repo_root, run=WheelhouseRunner(), python_executable="python")

    assert not (wheelhouse / "lxml-6.1.1-cp311-cp311-macosx_11_0_arm64.whl").exists()
    assert {path.name for path in wheelhouse.glob("*.whl")} == {
        f"swcraft_tools-{_VERSION}-py3-none-any.whl",
        f"swcraft_mcp-{_VERSION}-py3-none-any.whl",
        f"swcraft_agent-{_VERSION}-py3-none-any.whl",
        f"swcraft_specimens-{_VERSION}-py3-none-any.whl",
        "pyyaml-6.0.3-py3-none-any.whl",
    }


def test_build_wheelhouse_keeps_the_previous_closure_when_the_download_fails(tmp_path: Path) -> None:
    """A failed `pip download` keeps the previous closure.

    The defect: the directory was emptied before the network call, so a dropped
    connection left it empty. The new closure is assembled beside the old one and
    swapped in only when complete.
    """
    repo_root = _make_repo_with_wheels(tmp_path)
    wheelhouse = repo_root / _ARTIFACTS.DIST_DIRNAME / _ARTIFACTS.WHEELHOUSE_DIRNAME
    wheelhouse.mkdir(parents=True)
    good = wheelhouse / "lxml-6.1.3-cp311-cp311-macosx_11_0_arm64.whl"
    good.write_text("yesterday's working closure", encoding="utf-8")

    def failing(argv, cwd) -> None:
        raise _ARTIFACTS.BuildArtifactsError("the index could not be reached")

    with pytest.raises(_ARTIFACTS.BuildArtifactsError):
        _ARTIFACTS.build_wheelhouse(repo_root, run=failing, python_executable="python")

    assert good.read_text(encoding="utf-8") == "yesterday's working closure"
    assert not (wheelhouse.parent / f"{_ARTIFACTS.WHEELHOUSE_DIRNAME}.new").exists(), (
        "the staging directory does not survive the failure that produced it"
    )


@pytest.mark.skipif(os.name == "nt", reason="the mode bit is not what makes a directory removable on Windows")
def test_a_wheelhouse_that_cannot_be_removed_is_one_sentence_and_not_a_traceback(tmp_path: Path) -> None:
    """A swap that cannot clear its way is one refusal, with the previous closure intact.

    The defect: ``rmtree(..., ignore_errors=True)`` half-emptied the live directory
    silently and the rename after it raised a raw ``OSError``, a traceback. Produced
    with the real ``rmtree`` on a directory whose mode denies unlinking; a stub would
    raise where ``ignore_errors`` swallows. Not reached: Windows, and a failure
    between the two renames.
    """
    repo_root = _make_repo_with_wheels(tmp_path)
    dist_root = repo_root / _ARTIFACTS.DIST_DIRNAME
    wheelhouse = dist_root / _ARTIFACTS.WHEELHOUSE_DIRNAME
    wheelhouse.mkdir(parents=True)
    good = wheelhouse / "lxml-6.1.3-cp311-cp311-macosx_11_0_arm64.whl"
    good.write_text("yesterday's working closure", encoding="utf-8")

    # A crashed earlier run's leftover at the aside name, which cannot be emptied.
    superseded = dist_root / f"{_ARTIFACTS.WHEELHOUSE_DIRNAME}.previous"
    superseded.mkdir()
    (superseded / "left-behind.whl").write_text("from a run that died", encoding="utf-8")
    superseded.chmod(0o555)

    try:
        with pytest.raises(_ARTIFACTS.BuildArtifactsError) as excinfo:
            _ARTIFACTS.build_wheelhouse(repo_root, run=WheelhouseRunner(), python_executable="python")
    finally:
        superseded.chmod(0o755)

    assert str(wheelhouse) in str(excinfo.value)
    assert good.read_text(encoding="utf-8") == "yesterday's working closure", (
        "the previous closure is intact -- a half-emptied one is what `ignore_errors=True` produced"
    )
    assert not (dist_root / f"{_ARTIFACTS.WHEELHOUSE_DIRNAME}.new").exists(), (
        "the staged closure is discarded rather than left beside the live one"
    )


def test_build_wheelhouse_refuses_to_run_before_the_wheels_are_built(tmp_path: Path) -> None:
    repo_root = _make_repo_with_wheels(tmp_path)
    for wheel in (repo_root / _ARTIFACTS.DIST_DIRNAME / _ARTIFACTS.PYTHON_DIRNAME).glob("*.whl"):
        wheel.unlink()
    runner = WheelhouseRunner()

    with pytest.raises(_ARTIFACTS.BuildArtifactsError, match="is missing from"):
        _ARTIFACTS.build_wheelhouse(repo_root, run=runner, python_executable="python")

    assert runner.calls == [], "nothing is downloaded before the local wheels are known to exist"


def _carried_interpreter(repo_root: Path, token: str) -> Path:
    """What `dev.py runtime` leaves for `--wheelhouse` to resolve on."""
    interpreter = _ARTIFACTS.runtime_python(repo_root.resolve() / _ARTIFACTS.DIST_DIRNAME, token)
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_bytes(b"")
    return interpreter


def test_main_wheelhouse_flag_populates_and_reports(tmp_path, monkeypatch, capsys, mapped_build_host) -> None:
    repo_root = _make_repo_with_wheels(tmp_path)
    _carried_interpreter(repo_root, mapped_build_host)
    monkeypatch.setattr(
        _ARTIFACTS,
        "build_wheelhouse",
        lambda root, **_: root / _ARTIFACTS.DIST_DIRNAME / _ARTIFACTS.WHEELHOUSE_DIRNAME,
    )

    assert _ARTIFACTS.main(["--repo-root", str(repo_root), "--wheelhouse"]) == 0
    assert _ARTIFACTS.WHEELHOUSE_DIRNAME in capsys.readouterr().out


def test_main_reports_a_missing_wheelhouse_input_with_a_non_zero_exit(tmp_path, capsys, mapped_build_host) -> None:
    repo_root = _make_repo_with_wheels(tmp_path)
    _carried_interpreter(repo_root, mapped_build_host)
    for wheel in (repo_root / _ARTIFACTS.DIST_DIRNAME / _ARTIFACTS.PYTHON_DIRNAME).glob("*.whl"):
        wheel.unlink()

    assert _ARTIFACTS.main(["--repo-root", str(repo_root), "--wheelhouse"]) == 1
    assert "swcraft_tools" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The acceptance install argv (asserted without running it)
# ---------------------------------------------------------------------------


def test_clean_install_argv_installs_only_the_agent_offline(tmp_path: Path) -> None:
    argv = _ARTIFACTS.clean_install_argv(tmp_path / "venv" / "bin" / "python", tmp_path / "wheelhouse", "0.1.0")

    assert argv[1:5] == ["-m", "pip", "install", "--no-index"]
    assert argv[5:7] == ["--find-links", str(tmp_path / "wheelhouse")]
    # One requirement: the others must arrive through the declared graph.
    assert argv[7:] == ["swcraft-agent==0.1.0"]


def test_clean_install_argv_configures_no_index_url() -> None:
    """`--no-index` disables PyPI *and* any configured index, so no network is needed."""
    argv = _ARTIFACTS.clean_install_argv("python", Path("wheelhouse"), "0.1.0")

    assert "--no-index" in argv
    assert not [part for part in argv if part.startswith(("--index-url", "--extra-index-url", "-i"))]


def test_venv_interpreter_resolves_per_platform(tmp_path: Path) -> None:
    """Both layouts, from either platform, through the ``os_name`` parameter."""
    venv_dir = tmp_path / ".venv"

    assert _ARTIFACTS.venv_interpreter(venv_dir, "posix") == venv_dir / "bin" / "python"
    assert _ARTIFACTS.venv_interpreter(venv_dir, "nt") == venv_dir / "Scripts" / "python.exe"

    expected = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    assert _ARTIFACTS.venv_interpreter(venv_dir) == venv_dir / expected[0] / expected[1]


def test_verify_clean_install_rejects_a_wheelhouse_with_a_corrupt_wheel(tmp_path: Path) -> None:
    repo_root = _make_repo_with_wheels(tmp_path)
    wheelhouse = _ARTIFACTS.build_wheelhouse(repo_root, run=WheelhouseRunner(), python_executable="python")
    corrupt = wheelhouse / f"swcraft_mcp-{_VERSION}-py3-none-any.whl"
    corrupt.write_text("truncated copy", encoding="utf-8")
    runner = WheelhouseRunner()

    with pytest.raises(_ARTIFACTS.BuildArtifactsError) as error:
        _ARTIFACTS.verify_clean_install(repo_root, venv_dir=tmp_path / "venv", run=runner, python_executable="python")

    assert corrupt.name in str(error.value)
    assert runner.calls == [], "no venv is created once the wheelhouse is known to be unusable"


def test_verify_clean_install_rejects_an_unpopulated_wheelhouse(tmp_path: Path) -> None:
    repo_root = _make_repo_with_wheels(tmp_path)
    runner = WheelhouseRunner()

    with pytest.raises(_ARTIFACTS.BuildArtifactsError, match="--wheelhouse"):
        _ARTIFACTS.verify_clean_install(repo_root, venv_dir=tmp_path / "venv", run=runner, python_executable="python")

    assert runner.calls == []


# ---------------------------------------------------------------------------
# The editable-install detector, exercised positively
# ---------------------------------------------------------------------------


def test_probe_environment_reports_editable_markers_when_there_are_any(tmp_path: Path) -> None:
    """The positive control for ``editable_markers``: a broken detector also answers ``[]``.

    Both markers ``pip install -e`` leaves are planted: the ``__editable__*`` finder
    module and a ``.pth`` file pointing outside the environment.
    """
    venv_dir = tmp_path / "probe-venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv_dir)], check=True)
    interpreter = _ARTIFACTS.venv_interpreter(venv_dir)

    clean = _ARTIFACTS.probe_environment(interpreter)
    assert clean["editable_markers"] == [], "a fresh venv carries no editable install"

    purelib = Path(str(clean["purelib"]))
    (purelib / "__editable__.swcraft_tools-0.1.0.finder.py").write_text("", encoding="utf-8")
    (purelib / "_editable_impl_swcraft_tools.pth").write_text(f"{tmp_path / 'checkout' / 'src'}\n", encoding="utf-8")

    probed = _ARTIFACTS.probe_environment(interpreter)

    markers = probed["editable_markers"]
    assert isinstance(markers, list)
    assert any(marker.startswith("__editable__") for marker in markers), markers
    assert any("_editable_impl_swcraft_tools.pth ->" in marker for marker in markers), markers


# ---------------------------------------------------------------------------
# End-to-end (deselected from the fast run; executed by `dev.py test-artifacts`)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def local_artifacts() -> dict[str, object]:
    """The built artifact set for the current sources, rebuilt when it is not.

    Provisioning may use the network; the assertions read local files only.
    "Current": the manifest names this version, every artifact it lists is on disk,
    and no packaged source changed since (``stale_artifact_source``) -- the version
    alone is constant through development.
    """
    version = _ARTIFACTS.read_version(REPO_ROOT)
    dist_root = REPO_ROOT / _ARTIFACTS.DIST_DIRNAME
    manifest_path = dist_root / _ARTIFACTS.MANIFEST_FILENAME

    def current() -> dict[str, object] | None:
        if not manifest_path.is_file():
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("version") != version:
            return None
        if any(not (dist_root / _ARTIFACTS.artifact_relative_path(entry)).is_file() for entry in manifest["artifacts"]):
            return None
        if _ARTIFACTS.stale_artifact_source(dist_root, manifest, REPO_ROOT) is not None:
            return None
        return manifest

    manifest = current()
    if manifest is None:
        _ARTIFACTS.build_all(REPO_ROOT)
        manifest = current()
    assert manifest is not None
    return manifest


@pytest.fixture(scope="module")
def wheelhouse(local_artifacts: dict[str, object]) -> Path:
    """``dist/wheelhouse/``, reused when it is current and repopulated when it is not.

    Needs ``dev.py runtime`` first: the closure is resolved on the carried Python, so
    reuse is ``workstation-archive``'s exact comparison with ``RUNTIME.json`` and a
    repopulation (``pip download``, the network) runs on that interpreter. Without a
    runtime payload the fixture errors rather than put a host-platform closure in
    ``dist/wheelhouse/``. Reused only when its SWCraft wheels are byte-identical to
    ``dist/python``'s, since a same-version wheel from an earlier build would shadow
    the new one.
    """
    dist_root = REPO_ROOT / _ARTIFACTS.DIST_DIRNAME
    directory = dist_root / _ARTIFACTS.WHEELHOUSE_DIRNAME
    version = str(local_artifacts["version"])
    built = dist_root / _ARTIFACTS.PYTHON_DIRNAME
    # Outside the `try`: no runtime payload is an error to report, not a reason to repopulate.
    carried = _ARTIFACTS.runtime_interpreter_stamp(dist_root / _ARTIFACTS.RUNTIME_DIRNAME)

    def repopulate() -> Path:
        return _ARTIFACTS.build_wheelhouse(
            REPO_ROOT, python_executable=str(_ARTIFACTS.require_runtime_python(REPO_ROOT))
        )

    try:
        _ARTIFACTS.check_wheelhouse_platform(directory, carried)
        present = _ARTIFACTS.require_swcraft_wheels(directory, version)
        current = _ARTIFACTS.require_swcraft_wheels(built, version)
    except _ARTIFACTS.BuildArtifactsError:
        return repopulate()

    digests = {path.name: _ARTIFACTS.sha256_file(path) for path in present}
    if any(digests.get(path.name) != _ARTIFACTS.sha256_file(path) for path in current):
        return repopulate()
    return directory


@pytest.fixture(scope="module")
def clean_install(wheelhouse: Path, tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    """A fresh venv with **only** ``swcraft-agent`` installed, offline."""
    venv_dir = tmp_path_factory.mktemp("clean-install") / "venv"
    report = _ARTIFACTS.verify_clean_install(REPO_ROOT, venv_dir=venv_dir)
    report["venv_dir"] = str(venv_dir)
    return report


@pytest.mark.artifact
def test_wheelhouse_holds_the_swcraft_wheels_and_the_third_party_closure(wheelhouse: Path) -> None:
    version = _ARTIFACTS.read_version(REPO_ROOT)
    names = {path.name for path in wheelhouse.glob("*.whl")}

    for distribution in _ARTIFACTS.SWCRAFT_DISTRIBUTIONS:
        assert any(name.startswith(f"{distribution}-{version}-") for name in names), distribution

    # The closure, not just the direct requirements.
    downloaded = {name.split("-")[0].lower().replace("_", "-") for name in names}
    assert {"pydantic", "pydantic-core", "xmlschema", "elementpath", "openai", "mcp"} <= downloaded
    # A subset assertion is blind to a wheel that should no longer be there.
    assert "autosar" not in downloaded

    recorded = _ARTIFACTS.check_wheelhouse_platform(wheelhouse)
    assert recorded["created_at"].endswith("Z")


@pytest.mark.artifact
def test_the_wheelhouse_pyyaml_wheel_carries_libyaml(wheelhouse: Path) -> None:
    """libyaml is the one reader of every YAML file, so the archive must carry it by construction.

    A pure ``py3-none-any`` wheel would run the product on the slower pure-Python
    parser. Not reached: whether the extension loads on the installing machine.
    """
    wheels = [path for path in wheelhouse.glob("*.whl") if path.name.lower().startswith("pyyaml-")]
    assert len(wheels) == 1, sorted(path.name for path in wheels)
    with zipfile.ZipFile(wheels[0]) as archive:
        extensions = [name for name in archive.namelist() if re.fullmatch(r"yaml/_yaml\.[^/]+\.(?:so|pyd)", name)]
    assert extensions, f"{wheels[0].name} carries no compiled yaml/_yaml extension"


@pytest.mark.artifact
def test_clean_install_pulls_mcp_and_tools_transitively(clean_install: dict[str, object]) -> None:
    """The declared graph, exercised rather than bypassed."""
    version = clean_install["version"]
    assert clean_install["distributions"] == {
        "swcraft-tools": version,
        "swcraft-mcp": version,
        "swcraft-agent": version,
    }


@pytest.mark.artifact
def test_clean_install_resolves_every_package_from_the_new_venv(clean_install: dict[str, object]) -> None:
    purelib = Path(str(clean_install["purelib"])).resolve()
    modules = clean_install["modules"]
    assert isinstance(modules, dict)

    for name, location in modules.items():
        assert location and not str(location).startswith("ERROR:"), f"{name}: {location}"
        resolved = Path(str(location)).resolve()
        assert purelib in resolved.parents, f"{name} resolved from {resolved}, not the venv's site-packages"
        assert REPO_ROOT not in resolved.parents, f"{name} resolved from the source checkout: {resolved}"


@pytest.mark.artifact
def test_clean_install_leaves_no_editable_install(clean_install: dict[str, object]) -> None:
    assert clean_install["editable_markers"] == [], (
        "no acceptance path may be satisfied by an editable install or a source-checkout import"
    )


@pytest.mark.artifact
def test_installed_cli_reports_the_manifest_version(
    clean_install: dict[str, object], local_artifacts: dict[str, object]
) -> None:
    venv_dir = Path(str(clean_install["venv_dir"]))
    script = _ARTIFACTS.venv_interpreter(venv_dir).parent / ("swcraft.exe" if os.name == "nt" else "swcraft")

    result = subprocess.run([str(script), "--version"], capture_output=True, text=True, check=True, encoding="utf-8")

    assert result.stdout.strip().endswith(str(local_artifacts["version"]))


@pytest.mark.artifact
def test_the_vsix_is_a_well_formed_installable_artifact(local_artifacts: dict[str, object]) -> None:
    dist_root = REPO_ROOT / _ARTIFACTS.DIST_DIRNAME
    entries = local_artifacts["artifacts"]
    assert isinstance(entries, list)
    vsix_entries = [entry for entry in entries if entry["kind"] == "vsix"]
    assert len(vsix_entries) == 1
    vsix_path = dist_root / _ARTIFACTS.artifact_relative_path(vsix_entries[0])

    assert zipfile.is_zipfile(vsix_path)
    with zipfile.ZipFile(vsix_path) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        assert "extension/package.json" in names
        assert "extension/dist/extension.js" in names
        assert any(name.startswith("extension/dist/onboarding-assets/") for name in names), (
            "the onboarding assets must ship in the .vsix — the extension reads them at runtime"
        )
        package_json = json.loads(archive.read("extension/package.json").decode("utf-8"))

    assert package_json["version"] == local_artifacts["version"]


@pytest.mark.artifact
def test_the_guide_archive_and_the_wheel_payload_carry_the_release_notes(
    local_artifacts: dict[str, object],
) -> None:
    """The release notes travel inside the guide, on both of its delivery paths.

    The guide zip and the ``swcraft-tools`` wheel's ``guide_site/`` must both hold the
    section's index, one page per carried note and ``guide-corpus.json`` (present
    only; its contents are the producer's tests'). Not reached: whether a browser
    renders the pages.
    """
    dist_root = REPO_ROOT / _ARTIFACTS.DIST_DIRNAME
    entries = local_artifacts["artifacts"]
    assert isinstance(entries, list)
    carried = [note.stem for note in _ARTIFACTS.guide_release_notes(REPO_ROOT)]
    assert "0.6.0" in carried
    expected = {
        "release-notes/index.html",
        _ARTIFACTS.GUIDE_CORPUS_FILENAME,
        *(f"release-notes/{version}/index.html" for version in carried),
    }

    (docs_entry,) = [entry for entry in entries if entry["kind"] == "docs"]
    with zipfile.ZipFile(dist_root / _ARTIFACTS.artifact_relative_path(docs_entry)) as archive:
        root = f"{_ARTIFACTS.DOCS_ARCHIVE_PREFIX}{local_artifacts['version']}/"
        in_zip = {name.removeprefix(root) for name in archive.namelist() if name.startswith(root)}
    assert expected <= in_zip, f"the guide archive is missing {sorted(expected - in_zip)}"

    (wheel_entry,) = [
        entry
        for entry in entries
        if entry["kind"] == "wheel" and Path(_ARTIFACTS.artifact_relative_path(entry)).name.startswith("swcraft_tools-")
    ]
    with zipfile.ZipFile(dist_root / _ARTIFACTS.artifact_relative_path(wheel_entry)) as wheel:
        prefix = f"swcraft_tools/{_ARTIFACTS.PACKAGE_GUIDE_SITE_DIRNAME}/"
        in_wheel = {name.removeprefix(prefix) for name in wheel.namelist() if name.startswith(prefix)}
    assert expected <= in_wheel, f"the wheel's guide payload is missing {sorted(expected - in_wheel)}"


# ---------------------------------------------------------------------------
# The protocol surface and the greenfield flow, from the wheel
# ---------------------------------------------------------------------------

#: Shared by both probes; source text, since the installed interpreter imports nothing from here.
_PROBE_PRELUDE = '''
import json
import pathlib
import sys
import sysconfig

PAYLOAD = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
REPO_ROOT = pathlib.Path(PAYLOAD["repo_root"]).resolve()
PURELIB = pathlib.Path(sysconfig.get_paths()["purelib"]).resolve()


def under(path, root):
    """Is *path* *root* itself or anything inside it?"""
    resolved = pathlib.Path(str(path)).resolve()
    return resolved == root or root in resolved.parents


def module_origins(names):
    """Where every already-imported ``swcraft*`` module came from."""
    origins = {}
    for name, module in sorted(sys.modules.items()):
        if name.split(".")[0] in names:
            origin = getattr(module, "__file__", None)
            if origin:
                origins[name] = origin
    return origins


def emit(report):
    report["cwd"] = str(pathlib.Path.cwd())
    report["cwd_in_repo"] = under(pathlib.Path.cwd(), REPO_ROOT)
    report["purelib"] = str(PURELIB)
    origins = module_origins({"swcraft_tools", "swcraft_mcp", "swcraft_agent"})
    report["swcraft_modules"] = len(origins)
    report["swcraft_modules_in_repo"] = sorted(name for name, path in origins.items() if under(path, REPO_ROOT))
    report["swcraft_modules_outside_purelib"] = sorted(
        name for name, path in origins.items() if not under(path, PURELIB)
    )
    pathlib.Path(PAYLOAD["report"]).write_text(json.dumps(report), encoding="utf-8")
'''

#: Probe 1: the installed surface, with no repository checkout reachable; ``sys.path``
#: is reported so that is asserted, and ``swcraft init`` really scaffolds the template.
_INSTALLED_SURFACE_PROBE = (
    _PROBE_PRELUDE
    + """
import asyncio
import importlib.resources

report = {"sys_path_in_repo": [entry for entry in sys.path if entry and under(entry, REPO_ROOT)]}

import swcraft_mcp.server as server
import swcraft_tools
from fastmcp import Client
from swcraft_tools import cli
from swcraft_tools.editing import registry as reg

report["module_files"] = {
    "swcraft_tools": swcraft_tools.__file__,
    "swcraft_tools.cli": cli.__file__,
    "swcraft_tools.editing.registry": reg.__file__,
    "swcraft_mcp.server": server.__file__,
}


async def _tool_names():
    async with Client(server.mcp) as client:
        return sorted(tool.name for tool in await client.list_tools())


report["tools"] = asyncio.run(_tool_names())

# The registry: derived from the installed `schema.py`, with no contract file,
# no checkout and no data directory to read.
report["registry_layers"] = [layer.name for layer in reg.REGISTRY.layers]
report["registry_kinds"] = sorted(
    kind.kind for layer in reg.REGISTRY.layers for kind in layer.element_kinds
)
report["registry_rebuilds"] = [layer.name for layer in reg.build_registry().layers]

# The templates: package data inside the wheel, reached only through
# `importlib.resources.files`.
template_root = importlib.resources.files("swcraft_tools") / "templates" / PAYLOAD["template_dirname"]
report["template_root"] = str(template_root)
report["template_root_in_purelib"] = under(template_root, PURELIB)
report["template_root_in_repo"] = under(template_root, REPO_ROOT)
report["template_root_is_dir"] = template_root.is_dir()

project = pathlib.Path(PAYLOAD["project"])
sys.argv = ["swcraft", "init", str(project), "--name", PAYLOAD["project_name"], "--integration", PAYLOAD["profile"]]
try:
    cli.main()
except SystemExit as error:  # `init` may exit explicitly; a non-zero code is a failure
    assert not error.code, error.code
report["scaffolded"] = sorted(
    str(path.relative_to(project).as_posix()) for path in project.rglob("*") if path.is_file()
)
report["scaffolded_dirs"] = sorted(
    str(path.relative_to(project).as_posix()) for path in (project / "model").rglob("*") if path.is_dir()
)

emit(report)
"""
)

#: Probe 2: the whole greenfield flow from the installed distributions. The scenario
#: definition (test code, in no wheel) is imported from the checkout; product code
#: must still come from the venv (`swcraft_modules_in_repo`).
_INSTALLED_GREENFIELD_PROBE = (
    _PROBE_PRELUDE
    + """
sys.path.insert(0, PAYLOAD["scenario_root"])

from greenfield_scenario import (  # noqa: E402
    ALLOWED_TOOLS,
    ARTIFACTS,
    INVALID_CHANGE_SETS,
    NON_INVALID_REFUSALS,
    PROJECT_NAME,
    REMOVED_IDENTITIES,
    SURVIVORS,
    replay_refusals,
    run_scenario,
)

run = run_scenario(pathlib.Path(PAYLOAD["project"]))
stored = b"\\n".join(run.model.values())

report = {
    "scenario_module": sys.modules["greenfield_scenario"].__file__,
    "project_name": PROJECT_NAME,
    "statuses": {
        key: run.responses[key]["status"]
        for key in ("seed", "pair", "elements", "values", "names", "update", "remove")
    },
    # Contract `8.0.0`'s same-name pair: the layers its step created in, and the
    # kind each of the two layers answers under the shared name.
    "pair_addresses": sorted(
        [entry["address"]["layer"], entry["address"]["element"]] for entry in run.responses["pair"]["delta"]
    ),
    "pair_kinds": {
        layer: run.reads["pair_" + layer]["value"]["kind"] for layer in ("application_types", "implementation_types")
    },
    # Contract `8.0.0`'s data description elements: the layers and elements their
    # step created, and what the element layers and the two types answer.
    "elements_addresses": sorted(
        [entry["address"]["layer"], entry["address"]["element"]] for entry in run.responses["elements"]["delta"]
    ),
    "elements_reads": {
        "units": [entry["name"] for entry in run.reads["elements_units"]["value"]],
        "compu_methods": run.reads["elements_compu_methods"]["value"],
        "type_refs": [
            run.reads["elements_SyntheticDemand"]["value"]["unit_ref"],
            run.reads["elements_SyntheticDemand"]["value"]["compu_method_ref"],
            run.reads["elements_SyntheticGear"]["value"]["compu_method_ref"],
        ],
    },
    # The application value form: the two initial values as the model holds them,
    # and the form the installed writer gave each, read back from the built ARXML
    # by the scenario's own reader.
    "values_stated": [
        [row["name"], row["application_type"], row["init"]] for row in run.reads["values"]["value"]
    ],
    "value_forms": run.value_forms,
    # Contract `8.0.0`'s stated names and stub members: what the model answers for
    # the stub's list, the stated behaviour name and the call, and the names and
    # member path the installed writer wrote, read back by the scenario's reader.
    "names_reads": [
        run.reads["names_stub"]["value"],
        run.reads["names_behaviour"]["value"],
        run.reads["names_call"]["value"],
    ],
    "stated_names": run.stated_names,
    "discovered_layers": [layer["layer"] for layer in run.responses["discover"]["layers"]],
    "seed_layers": sorted({entry["address"]["layer"] for entry in run.responses["seed"]["delta"]}),
    "update_delta": run.responses["update"]["delta"],
    "removed_addresses": sorted(
        entry["address"]["layer"] for entry in run.responses["remove"]["delta"] if entry["operation"] == "remove"
    ),
    "reads": {
        "layer": sorted(entry["name"] for entry in run.reads["layer"]["value"]),
        # The served form, recorded so the installed wheel is held to the
        # `readResponse` key that is REQUIRED: values alone would pass a
        # wheel that dropped the echo.
        "forms": {key: run.reads[key]["form"] for key in ("layer", "element", "field")},
        "element": run.reads["element"]["value"]["name"],
        "field": run.reads["field"]["value"],
    },
    # The bounded-read request keys, carried across the process boundary as the
    # values the wheel answered with. Recorded rather than asserted here: the
    # probe runs with no pytest, so every judgement stays with the driver.
    "bounded_reads": {
        "where_echo": run.reads["filtered"]["where"],
        "filtered": run.reads["filtered"]["value"]["identities"],
        "page": run.reads["paged"]["page"],
        "page_identities": run.reads["paged"]["value"]["identities"],
        "continued": run.reads["paged_next"]["value"]["identities"],
        "continued_has_cursor": "next" in run.reads["paged_next"]["page"],
        "members_echo": run.reads["members"]["members"],
        "members": sorted([row["element"], row["member"]["name"]] for row in run.reads["members"]["value"]),
    },
    # The project-wide `full` is a refusal:
    # its shape is recorded as the wheel answered it, and the served `full`
    # form and its prose come from the layer-narrowed call the refusal hints.
    "discover_forms": [run.responses["discover"]["form"], run.responses["discover_layer_full"]["form"]],
    "discover_refusal": [
        run.responses["discover_full"].get("response"),
        run.responses["discover_full"].get("error"),
        "form" in run.responses["discover_full"],
        "addresses" in run.responses["discover_full"],
    ],
    "discover_prose": [
        any("description" in layer for layer in run.responses["discover"]["layers"]),
        any("description" in layer for layer in run.responses["discover_layer_full"]["layers"]),
    ],
    "validate": run.responses["validate"]["success"],
    "validate_errors": run.responses["validate"]["errors"],
    "build": run.responses["build"]["success"],
    "build_errors": run.responses["build"]["errors"],
    "tool_calls": sorted(set(run.tool_calls)),
    "allowed_tools": sorted(ALLOWED_TOOLS),
    "change_set_calls": run.tool_calls.count("change_set"),
    "questions": len(run.questions),
    "audit_lifecycle": [
        record["operation"] for record in run.audit if record["operation"].startswith("change_set")
    ],
    "model_files": sorted(run.model),
    "artifact_sizes": {name: len(run.artifacts[name]) for name in ARTIFACTS},
    "survivors_missing": [name for name in SURVIVORS if name.encode() not in stored],
    "removed_still_present": [name for name in REMOVED_IDENTITIES if name.encode() in stored],
    "arxml_survivors": [name for name in SURVIVORS if name in run.artifacts["dist/model.arxml"].decode("utf-8")],
    "arxml_removed": [name for name in REMOVED_IDENTITIES if name in run.artifacts["dist/model.arxml"].decode("utf-8")],
    "diagram_removed": [
        name for name in REMOVED_IDENTITIES if name in run.artifacts["views/ibd-Root.svg"].decode("utf-8")
    ],
    "needs_requirement": "REQ_BRAKE_001" in run.artifacts["dist/needs.json"].decode("utf-8"),
}

report["refusal_codes"] = replay_refusals(run)
report["refusal_cases"] = sorted({*INVALID_CHANGE_SETS, *NON_INVALID_REFUSALS})
# The INSTALLED protocol's own error vocabulary. Read from the wheel, so the
# "every class is answered" claim is made about the distribution rather than
# about the scenario's copy of the list.
from swcraft_tools.editing.protocol import ERROR_CODES  # noqa: E402

report["error_codes"] = sorted(ERROR_CODES)
report["model_unchanged_after_refusals"] = run.model == {
    str(path.relative_to(pathlib.Path(PAYLOAD["project"]) / "model").as_posix()): path.read_bytes()
    for path in sorted((pathlib.Path(PAYLOAD["project"]) / "model").rglob("*"))
    if path.is_file()
}

emit(report)
"""
)


def _run_probe(interpreter: Path, source: str, payload: dict[str, object], workdir: Path, name: str) -> dict:
    """Run *source* in the installed interpreter and return the report it wrote.

    Under ``-I`` from *workdir* (outside the repository), with ``SWCRAFT_*`` scrubbed
    too. A non-zero exit fails with both streams, never a skip.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    script = workdir / f"{name}.py"
    script.write_text(source, encoding="utf-8")
    report_path = workdir / f"{name}-report.json"
    payload = {**payload, "report": str(report_path), "repo_root": str(REPO_ROOT)}
    (workdir / f"{name}-payload.json").write_text(json.dumps(payload), encoding="utf-8")

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PYTHON") and not key.startswith("SWCRAFT_")
    }
    result = subprocess.run(
        [str(interpreter), "-I", str(script), str(workdir / f"{name}-payload.json")],
        capture_output=True,
        text=True,
        cwd=str(workdir),
        env=environment,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"the {name} probe failed inside the clean installation "
        f"(exit {result.returncode})\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert report_path.is_file(), f"the {name} probe exited 0 without writing a report:\n{result.stdout}"
    return json.loads(report_path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def installed_surface(clean_install: dict[str, object], tmp_path_factory: pytest.TempPathFactory) -> dict:
    workdir = tmp_path_factory.mktemp("installed-surface")
    return _run_probe(
        _ARTIFACTS.venv_interpreter(Path(str(clean_install["venv_dir"]))),
        _INSTALLED_SURFACE_PROBE,
        {
            "template_dirname": TEMPLATE_PACKAGE_DIRNAME,
            "profile": TEMPLATE_PROFILE,
            "project": str(workdir / "scaffold"),
            "project_name": "clean-install",
        },
        workdir,
        "surface",
    )


@pytest.fixture(scope="module")
def installed_greenfield(clean_install: dict[str, object], tmp_path_factory: pytest.TempPathFactory) -> dict:
    workdir = tmp_path_factory.mktemp("installed-greenfield")
    return _run_probe(
        _ARTIFACTS.venv_interpreter(Path(str(clean_install["venv_dir"]))),
        _INSTALLED_GREENFIELD_PROBE,
        {
            "scenario_root": str(REPO_ROOT / "packages" / "mcp" / "tests" / "support"),
            "project": str(workdir / "project"),
        },
        workdir,
        "greenfield",
    )


@pytest.mark.artifact
def test_the_installed_probes_ran_against_the_venv_and_not_the_checkout(
    installed_surface: dict, installed_greenfield: dict
) -> None:
    """The probes ran against the venv, not the repository.

    Nothing of the repository is on the surface probe's ``sys.path``, and every product module comes
    from the venv in both.
    """
    assert installed_surface["sys_path_in_repo"] == [], installed_surface["sys_path_in_repo"]
    assert installed_surface["cwd_in_repo"] is False
    assert installed_greenfield["cwd_in_repo"] is False

    for report in (installed_surface, installed_greenfield):
        assert report["swcraft_modules"] > 0, "the probe imported no SWCraft module at all"
        assert report["swcraft_modules_in_repo"] == [], report["swcraft_modules_in_repo"]
        assert report["swcraft_modules_outside_purelib"] == [], report["swcraft_modules_outside_purelib"]

    assert installed_greenfield["scenario_module"].startswith(str(REPO_ROOT / "packages" / "mcp" / "tests" / "support"))


@pytest.mark.artifact
def test_installed_tools_list_returns_the_thirteen_names_and_no_removed_tool(installed_surface: dict) -> None:
    """The MCP surface, driven through the installed ``swcraft_mcp``."""
    registered = set(installed_surface["tools"])

    assert registered == set(EXPECTED_INVENTORY), (
        f"missing: {sorted(EXPECTED_INVENTORY - registered)}; unexpected: {sorted(registered - EXPECTED_INVENTORY)}"
    )
    assert len(registered) == 13
    # Stated separately: an equality whose expected set drifted would not say which half is wrong.
    assert registered & REMOVED_TOOLS == set(), sorted(registered & REMOVED_TOOLS)


@pytest.mark.artifact
def test_the_templates_resolve_through_importlib_resources_from_the_wheel(installed_surface: dict) -> None:
    """``swcraft init --integration moon`` takes its template from the venv's ``site-packages``."""
    assert installed_surface["template_root_is_dir"] is True
    assert installed_surface["template_root_in_purelib"] is True, installed_surface["template_root"]
    assert installed_surface["template_root_in_repo"] is False, installed_surface["template_root"]

    scaffolded = set(installed_surface["scaffolded"])
    for carried in TEMPLATE_CARRIED_FILES:
        assert carried in scaffolded, f"{carried} did not come out of the installed template: {sorted(scaffolded)}"
    # One directory per declared layer (all mandatory), plus the `data_types/` parent.
    assert installed_surface["scaffolded_dirs"] == [
        "model/compositions",
        "model/connectors",
        "model/data_types",
        "model/data_types/application",
        "model/data_types/base",
        "model/data_types/compu_methods",
        "model/data_types/constraints",
        "model/data_types/implementation",
        "model/data_types/mapping_sets",
        "model/data_types/units",
        "model/hardware",
        "model/interfaces",
        "model/requirements",
        "model/sequences",
        "model/swc",
        "model/tasks",
    ], installed_surface["scaffolded_dirs"]


@pytest.mark.artifact
def test_the_registry_resolves_from_the_installed_package(installed_surface: dict) -> None:
    """The schema-derived registry, built inside the venv.

    Derived from the installed ``schema.py`` with no checkout: every layer and kind,
    and the same answer when re-derived.
    """
    assert installed_surface["registry_layers"] == [
        "swc",
        "interfaces",
        "connectors",
        "application_types",
        "implementation_types",
        "base_types",
        "requirements",
        "compositions",
        "tasks",
        "sequences",
        "hardware",
        "mapping_sets",
        "units",
        "compu_methods",
        "data_constraints",
    ]
    assert installed_surface["registry_rebuilds"] == installed_surface["registry_layers"]
    assert len(installed_surface["registry_kinds"]) >= len(installed_surface["registry_layers"])
    assert "swc" in installed_surface["registry_kinds"]


@pytest.mark.artifact
def test_the_installed_greenfield_flow_discovers_creates_reads_updates_and_removes(
    installed_greenfield: dict,
) -> None:
    """The whole greenfield flow, from the wheel -- not only the init-and-build half."""
    assert installed_greenfield["statuses"] == {
        "seed": "committed",
        "pair": "committed",
        "elements": "committed",
        "values": "committed",
        "names": "committed",
        "update": "committed",
        "remove": "committed",
    }
    # Stated names and stub members, as the in-repo twin asserts.
    assert installed_greenfield["names_reads"] == [
        ["ReadFault"],
        "BrakeCtrlBehaviour",
        # Synchrony and result side at their defaults.
        {
            "port": "rDiag",
            "operation": "ReadFault",
            "name": "ReadBrakeFault",
            "asynchronous": False,
            "poll": False,
            "result": None,
            "timeout": None,
        },
    ]
    member = "/Synthetic/PortInterfaces/ISyntheticDiagnosis/ReadFault"
    assert installed_greenfield["stated_names"] == {
        "behaviours": [["BrakeCtrlBehaviour"], ["SpeedSensorSWC_InternalBehavior"]],
        "call_points": [["ReadBrakeFault", member]],
        "client_com_specs": [[member]],
    }
    # The application value form: a primitive with its unit, an enumeration as the entry's name.
    assert installed_greenfield["values_stated"] == [
        ["demand", "SyntheticDemand", 12.5],
        ["gear", "SyntheticGear", "SyntheticDrive"],
    ]
    assert installed_greenfield["value_forms"] == {
        "ISyntheticValues_demand_Init": [
            "APPLICATION-VALUE-SPECIFICATION",
            "VALUE",
            "/DataTypes/Units/SyntheticNewtonMetre",
            "V",
            "12.5",
        ],
        "ISyntheticValues_gear_Init": ["APPLICATION-VALUE-SPECIFICATION", "VALUE", None, "VT", "SyntheticDrive"],
    }
    # One name in two layers: two identities, each layer answering its own element.
    assert installed_greenfield["pair_addresses"] == [
        ["application_types", "SyntheticTorque"],
        ["implementation_types", "SyntheticTorque"],
        ["interfaces", "ISyntheticTorque"],
        ["mapping_sets", "SyntheticTorqueMapping"],
    ]
    assert installed_greenfield["pair_kinds"] == {
        "application_types": "application_type",
        "implementation_types": "value_type",
    }
    # Data description elements, each in its own layer, named by reference.
    assert installed_greenfield["elements_addresses"] == [
        ["application_types", "SyntheticDemand"],
        ["application_types", "SyntheticGear"],
        ["base_types", "uint8_base"],
        ["compu_methods", "SyntheticGearTable"],
        ["compu_methods", "SyntheticTorqueScale"],
        ["implementation_types", "SyntheticGearRaw"],
        ["units", "SyntheticNewtonMetre"],
    ]
    assert installed_greenfield["elements_reads"] == {
        "units": ["SyntheticNewtonMetre"],
        "compu_methods": [
            {"name": "SyntheticGearTable", "kind": "texttable", "entries": 2},
            {"name": "SyntheticTorqueScale", "kind": "linear", "unit": "SyntheticNewtonMetre"},
        ],
        "type_refs": ["SyntheticNewtonMetre", "SyntheticTorqueScale", "SyntheticGearTable"],
    }
    assert installed_greenfield["discovered_layers"] == [
        "swc",
        "interfaces",
        "connectors",
        "application_types",
        "implementation_types",
        "base_types",
        "requirements",
        "compositions",
        "tasks",
        "sequences",
        "hardware",
        "mapping_sets",
        "units",
        "compu_methods",
        "data_constraints",
    ]
    assert installed_greenfield["seed_layers"] == [
        # Includes the scaffolded root composition, edited at a member address.
        "base_types",
        "compositions",
        "connectors",
        "implementation_types",
        "interfaces",
        "requirements",
        "sequences",
        "swc",
        "tasks",
    ]

    assert installed_greenfield["reads"] == {
        # Sorted by name: two value types and the reference type the seed adds.
        "layer": ["Percent", "SyntheticRef", "float32"],
        # The wheel serves the requested width and says which one it served.
        "forms": {"layer": "full", "element": "full", "field": "full"},
        "element": "Task_20ms",
        "field": "IVehicleSpeed",
    }

    (nested,) = installed_greenfield["update_delta"]
    assert nested["address"]["path"] == [{"field": "ports"}, {"member": "rSpeed"}, {"field": "description"}]
    assert nested["after"] == "Receives the measured vehicle speed"

    assert installed_greenfield["removed_addresses"] == ["implementation_types", "interfaces", "swc"]
    assert installed_greenfield["survivors_missing"] == []
    assert installed_greenfield["removed_still_present"] == []


@pytest.mark.artifact
def test_the_installed_read_answers_the_filter_the_page_and_the_member_slice(
    installed_greenfield: dict,
) -> None:
    """The ``read`` filter, page and member keys answer from the wheel.

    A distribution could ship the tool parameter without the protocol key and every
    in-repo suite would stay green. Not reached: the refusal classes and the response
    budget.
    """
    bounded = installed_greenfield["bounded_reads"]
    assert bounded["where_echo"] == [{"field": "kind", "equals": "value_type"}, {"field": "unit", "exists": True}]
    assert bounded["filtered"] == ["Percent"], (
        "both clauses are AND-composed; float32's empty unit fails the existence clause, the reference type fails both"
    )
    assert bounded["page"] == {"total": 3, "returned": 2, "truncated": True, "next": "float32"}
    assert bounded["page_identities"] == ["Percent", "float32"]
    assert bounded["continued"] == ["SyntheticRef"], "the cursor the first page published continued past it"
    assert bounded["continued_has_cursor"] is False, "the last page offers no cursor"
    assert bounded["members_echo"] == "ports"
    assert bounded["members"] == [
        ["BrakeCtrlSWC", "pLoad"],
        ["BrakeCtrlSWC", "rSpeed"],
        ["SpeedSensorSWC", "pSpeed"],
    ]


@pytest.mark.artifact
def test_the_installed_discover_answers_summary_by_default_and_full_when_asked(
    installed_greenfield: dict,
) -> None:
    """``discover`` defaults to ``summary`` and says so; a project-wide ``full`` is refused.

    The refusal is an addressless ``over-budget`` with no form echo; the served
    ``full`` is the layer-narrowed one.
    """
    assert installed_greenfield["discover_forms"] == ["summary", "full"]
    assert installed_greenfield["discover_refusal"] == ["error", "over-budget", False, False], (
        "the project-wide `full` is a refusal, addressless and without a form echo"
    )
    assert installed_greenfield["discover_prose"] == [False, True], (
        "the default strips every descriptor `description`; a layer's `full` is where the prose lives"
    )


@pytest.mark.artifact
def test_the_installed_greenfield_flow_used_only_the_protocol_tools(installed_greenfield: dict) -> None:
    """No layer-specific function was reachable, because none is registered."""
    assert installed_greenfield["tool_calls"] == installed_greenfield["allowed_tools"]
    assert set(installed_greenfield["tool_calls"]) & REMOVED_TOOLS == set()
    # One per scenario step; this test never runs locally, so a new step must update it.
    assert installed_greenfield["change_set_calls"] == 7
    assert installed_greenfield["questions"] == 7, "one exact-delta question per committed change set"
    assert (
        installed_greenfield["audit_lifecycle"]
        == [
            "change_set_staged",
            "change_set_approved",
            "change_set_commit_intent",
            "change_set_committed",
        ]
        * 7
    )


@pytest.mark.artifact
def test_the_installed_greenfield_flow_validates_and_builds(installed_greenfield: dict) -> None:
    assert installed_greenfield["validate"] is True, installed_greenfield["validate_errors"]
    assert installed_greenfield["validate_errors"] == []
    assert installed_greenfield["build"] is True, installed_greenfield["build_errors"]

    sizes = installed_greenfield["artifact_sizes"]
    assert set(sizes) == {
        "dist/model.arxml",
        "views/ibd-Root.svg",
        "views/index.html",
        "dist/needs.json",
    }
    for name, size in sizes.items():
        assert size > 0, f"{name} was reported as built but is empty"

    # The artifacts describe this model: a build from a cached model, or a removal
    # missing from the exporter's input, fails one of these.
    assert {
        "float32",
        "IVehicleSpeed",
        "SpeedSensorSWC",
        "BrakeCtrlSWC",
        "Speed_to_BrakeCtrl",
    } <= set(installed_greenfield["arxml_survivors"])
    assert installed_greenfield["arxml_removed"] == []
    assert installed_greenfield["diagram_removed"] == []
    assert installed_greenfield["needs_requirement"] is True


@pytest.mark.artifact
def test_the_installed_refusals_answer_every_class_and_change_nothing(installed_greenfield: dict) -> None:
    """The negative half of the scenario, replayed from the wheel.

    Each case is answered with its own code and leaves the model byte-identical.
    """
    codes = installed_greenfield["refusal_codes"]
    assert all(code == case for case, code in codes.items()), codes
    # Compared with the installed protocol's codes, not the scenario's list (both
    # sides of that would come from the probe). `over-budget` needs a wide model, so
    # a greenfield project cannot produce it; named, so a second unexercised code fails.
    unexercised = sorted(set(installed_greenfield["error_codes"]) - set(codes))
    assert unexercised == ["over-budget"]
    assert sorted(codes) == sorted(set(installed_greenfield["error_codes"]) - {"over-budget"})
    assert installed_greenfield["model_unchanged_after_refusals"] is True
