"""Native SWCraft development commands: the repository's only build contour.

* **stdlib only** -- it runs before anything is installed;
* one program per step, a list argv, never a shell: the shell available on Windows is
  PowerShell 5.x, which has no ``&&``;
* every command is a *plan* -- an ordered list of :class:`Step` s -- built without running
  anything, so argv, order and working directory are testable without a real build.

**The repo-local ``.venv`` is the point.** One shared interpreter lets a second checkout or a
worktree repoint the first one's editable installs at its own ``src/``. Every command therefore
resolves ``.venv/bin/python`` (POSIX) or ``.venv\\Scripts\\python.exe`` (Windows) and **fails with an
actionable message when it is absent**, never falling back to ``sys.executable``.

**Network policy.** Exactly five commands use the network: ``install``, ``wheelhouse`` and
``runtime`` provision; ``update-pins`` and ``update-dev-lock`` install nothing and rewrite committed
pins or the lock, maintainer's commands outside ``verify``. Every *assertion* downstream resolves
from local files only, so any other command that installs must pass ``--no-index``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Repo-local virtual environment every command runs out of.
VENV_DIRNAME = ".venv"

#: The folder holding the Python projects; a project's label is its folder name under it.
PACKAGES_DIR = Path("packages")

#: The specimen generator's project folder (under :data:`PACKAGES_DIR`).
SPECIMEN_GENERATOR_PROJECT = "specimens"

#: Python projects, in dependency order (tools <- mcp <- agent), then the specimen generator,
#: an independent oracle that is started as a process and never imported.
PYTHON_PROJECTS = ("tools", "mcp", "agent", SPECIMEN_GENERATOR_PROJECT)

#: Test projects that install nothing: a root folder with a ``pytest.ini`` and a ``tests/`` tree,
#: run by the fast gate and the marker stages it owns (:data:`MARKER_STAGE_OWNERS`), never installed,
#: linted as a project or covered (``lint`` lints ``scripts/`` as a tree of its own).
TEST_ONLY_PROJECTS = ("scripts",)

#: Every project a pytest step runs in: the Python projects, then the test-only ones.
TEST_PROJECTS = (*PYTHON_PROJECTS, *TEST_ONLY_PROJECTS)

#: The projects whose initial conftest registers ``--skip-node``; for any other the option is an
#: argparse error.
SKIP_NODE_PROJECTS = ("tools", "scripts")

#: The variable the specimen generator's ``integration`` tests read for the SWCraft CLI they run;
#: ``test-model-integration`` sets it (:func:`plan_test_model_integration`).
SWCRAFT_BIN_VARIABLE = "SWCRAFT_BIN"

#: The VS Code extension project, driven through its npm scripts.
VSCODE_PROJECT = "vscode_extension"

#: The reference model ``dev.py build`` runs the generation chain against: a plain development
#: fixture, not a template instance of the standalone architecture project.
REFERENCE_PROJECT = "examples/basic"

#: The reference project's manifest, relative to ``REFERENCE_PROJECT``.
REFERENCE_MANIFEST = "swcraft.yaml"

#: Every file the chain generates, relative to ``REFERENCE_PROJECT``, in generation order: the
#: manifest's ``outputs:`` and, between them, every file ``generate diagrams`` writes. Mirrored
#: here because this script cannot parse YAML or ask the generator; ``test_dev_build.py`` pins
#: the list. Named file by file because the build's first step removes named files, never a
#: directory.
REFERENCE_OUTPUTS = (
    "dist/model.arxml",
    "views/ibd-Root.svg",
    "views/ibd-Root.html",
    "views/overview-Root.svg",
    "views/overview-Root.html",
    "views/nesting.svg",
    "views/nesting.html",
    "views/deployment.svg",
    "views/deployment.html",
    "views/sequence-task10ms_consumer_cycle.svg",
    "views/sequence-task10ms_consumer_cycle.html",
    "views/report.txt",
    "views/index.html",
    "dist/needs.json",
)

#: Where ``dev.py schemas`` writes the editor JSON Schemas (from ``swcraft_tools.editor_schema``),
#: the directory the extension ships. A drift test gates the committed files, so the command has
#: no ``--check`` mode. The same step rewrites ``contributes.yamlValidation`` in
#: ``EXTENSION_PACKAGE_JSON``, so the block and its files never move separately.
EDITOR_SCHEMAS_DIR = "vscode_extension/schemas"

#: The agent profiles whose model schema section ``dev.py schemas`` regenerates: the template's,
#: the extension's bundled copy and the example project's, which must stay byte-identical.
AGENT_PROFILES = (
    "packages/tools/src/swcraft_tools/templates/vehicle-architecture/.github/agents/swcraft-architect.agent.md",
    "vscode_extension/fixtures/agents/swcraft-architect.agent.md",
    "examples/basic/.github/agents/swcraft-architect.agent.md",
)
EXTENSION_PACKAGE_JSON = "vscode_extension/package.json"

#: The files whose guide index region ``dev.py docs-reference`` regenerates: the agent profiles
#: and ``swcraft-agent``'s system prompt.
GUIDE_INDEX_FILES = (*AGENT_PROFILES, "packages/agent/src/swcraft_agent/agent-system-prompt.md")

#: The project ``AGENTS.md`` files: the template profile's body behind a fixed host preamble.
#: ``schemas`` and ``docs-reference`` rewrite them after the profiles' regions, so they cannot lag.
AGENTS_MD_PROFILE = AGENT_PROFILES[0]
AGENTS_MD_FILES = (
    "packages/tools/src/swcraft_tools/templates/vehicle-architecture/AGENTS.md",
    "vscode_extension/fixtures/agents/AGENTS.md",
)

#: Kiro's steering files: the same profile body behind the Kiro preamble, in the template and
#: the onboarding assets.
KIRO_STEERING_FILES = (
    "packages/tools/src/swcraft_tools/templates/vehicle-architecture/.kiro/steering/swcraft.md",
    "vscode_extension/fixtures/agents/swcraft.kiro-steering.md",
)

#: The user guide's source tree and its rendered site, named only for the summary: ``mkdocs.yml``
#: and ``scripts/build_artifacts.py`` drive the build.
GUIDE_DIR = "docs/guide"
DOCS_SITE_DIR = "dist/docs/site"

#: Where ``dev.py docs-reference`` writes the guide's generated reference pages (from
#: ``swcraft_tools.guide_reference``), and the mkdocs configuration whose navigation names them.
#: A drift test gates them, so the command has no ``--check`` mode.
GUIDE_REFERENCE_DIR = f"{GUIDE_DIR}/reference"
MKDOCS_CONFIG = "mkdocs.yml"

#: Coverage target package per Python project (``--cov=<package>``).
COVERAGE_PACKAGES = {
    "tools": "swcraft_tools",
    "mcp": "swcraft_mcp",
    "agent": "swcraft_agent",
    "specimens": "swcraft_specimens",
}

#: The fast gate deselects every marker that has a stage of its own; each is run by exactly one
#: other command, or it would never run anywhere. ``test_dev_script.py`` holds the pairing.
FAST_TEST_DESELECT = (
    "not benchmark and not slow and not artifact and not integration and not large_bench and not real_models"
)

#: The fast gate's pytest-xdist arguments, passed by :func:`plan_test` **only**. ``--dist
#: loadgroup`` runs every test of one ``xdist_group`` on one worker, in order; passed whether or
#: not a group exists, so a new mark needs no edit here. The marker stages and ``coverage`` take
#: none: they share ``dist/``, archives and worktrees, build benches once, or measure time.
FAST_TEST_PARALLEL = ("-n", "auto", "--dist", "loadgroup")

#: The fast gate's per-test timeout, passed by :func:`plan_test` **only**; every other invocation
#: takes the projects' ``timeout = 1200``. Without it a hung test held the Windows fast gate for an
#: hour. The plugin's option, not ``-o timeout=720``: a ``.venv`` without the plugin refuses
#: ``--timeout`` where ``-o`` would be accepted and time nothing. The value: the slowest fast test
#: takes 37.02 s on the Mac, the Windows runner is 4.7x slower (~175 s), times 4, rounded up. On
#: Windows (the thread method) a timeout ends the xdist worker, reported as its crash.
FAST_TEST_TIMEOUT = ("--timeout=720",)

#: The suites ``test-xsd`` runs, selected by *path*, not a marker: they run in the fast gate too,
#: skipping loudly without the licensed schemas, and the stage re-runs them with
#: ``--require-autosar-xsd`` to turn that skip into a failure. The second suite re-derives the
#: oracle's XSD map from the same schemas.
XSD_CONFORMANCE_SUITE: tuple[str, ...] = ("tests/test_arxml_xsd_conformance.py", "tests/test_io_oracle_xsd_map.py")

#: The **only** commands allowed to reach the network; anything else that installs uses
#: ``--no-index``. ``runtime`` fetches the pinned Node.js, CPython and companion extensions and is a
#: command of its own so ``workstation-archive`` downloads nothing. ``update-pins`` and
#: ``update-dev-lock`` rewrite tracked files, so they are never ``verify`` stages. An aggregate
#: inherits its stages' use (:func:`uses_network`).
#:
#: The flag describes a command's own steps. ``test-artifacts``' fixtures can still provision when
#: the artifacts or wheelhouse are absent or stale; ``verify`` orders ``artifacts`` and
#: ``wheelhouse`` first so they are reused. ``artifacts`` is not listed though ``npm ci`` and PEP 517
#: backends fetch dependencies: the flag governs the offline-**install** rule, not network use.
NETWORK_COMMANDS = ("install", "wheelhouse", "runtime", "update-pins", "update-dev-lock")

#: The commands ``--skip-node`` covers: those whose Node half (``npm ci``, Biome, ``node --test``)
#: is optional. ``artifacts`` (and ``verify``) is not: the ``.vsix`` needs npm, so
#: :func:`plan_artifacts` refuses the flag at plan time rather than failing an hour into ``verify``.
SKIP_NODE_COMMANDS = ("install", "lint", "test")

#: The one command ``--maxfail`` means anything to; every other command refuses it, which keeps
#: it out of ``verify`` (a full verdict names every failure). It does not reach the extension's
#: ``npm run test``, nor a project whose failures stay under the limit, which ends the plan at its
#: own step as any failure does.
MAXFAIL_COMMAND = "test"

#: The development lock, written by ``update-dev-lock``; ``install`` applies it to its pip steps
#: and ``wheelhouse`` to its ``pip download``. It does not reach ``npm ci``, the product's declared
#: ranges, or a wheelhouse distribution it has no line for.
DEV_LOCK = "constraints/dev.txt"

#: The one command ``--unlocked`` (the canary) means anything to; every other command refuses it,
#: which keeps it out of ``verify``.
UNLOCKED_COMMAND = "install"

#: The stage that runs the work repository's internal tests, and their folder (code internal to
#: the work repository, the public tree does not carry). Not in :data:`VERIFY_STAGES`: ``verify``
#: must pass in the public tree. It **fails**, never skips, where the folder is absent.
#: ``--python EXE``, the ``PATH`` arguments and the ``--require-*-export`` flags are its alone (the
#: real-export suites are closed because nothing open names a real export); every other command
#: refuses them. Each flag turns the skip on its variable into a failure, one flag per variable.
TEST_INTERNAL_COMMAND = "test-internal"
INTERNAL_DIR = "internal"

#: The file ``scripts/public_snapshot.py`` writes at the root of every open copy, and its text
#: (a test holds them equal to that script's). The **positive** sign :func:`open_copy_notice` keys
#: on: a work repository that lost ``internal/`` must still fail.
OPEN_COPY_MARKER = ".swcraft-open-copy"
OPEN_COPY_MARKER_TEXT = (
    "SWCraft's open copy: scripts/public_snapshot.py writes this file into every snapshot it assembles.\n"
    "scripts/dev.py reads it: the work repository's internal tests are not part of this copy.\n"
)

#: What ``test-internal`` prints, with exit 0, in an open copy.
OPEN_COPY_NOTICE = (
    f"{TEST_INTERNAL_COMMAND}: internal tests are not part of this repository (this is SWCraft's open copy); "
    "nothing ran."
)

#: The command that rewrites :data:`DEV_LOCK`; ``build_context`` asks uv for its version for it alone.
UPDATE_DEV_LOCK_COMMAND = "update-dev-lock"

#: What ``update-dev-lock`` compiles, on uv's standard input: the projects, the build backend and
#: pip, which no project declares. Paths are relative, so the step runs in the repo root.
DEV_LOCK_INPUT = "".join(f"-e ./{(PACKAGES_DIR / p).as_posix()}\n" for p in PYTHON_PROJECTS) + "hatchling\npip\n"

#: The ``dev`` dependency group every project declares and ``install`` installs.
DEV_GROUP = "dev"

#: ``update-dev-lock``'s ``--group`` arguments: each project's ``dev`` group, by its
#: ``pyproject.toml`` path relative to the repo root.
DEV_LOCK_GROUPS = tuple(f"{(PACKAGES_DIR / p / 'pyproject.toml').as_posix()}:{DEV_GROUP}" for p in PYTHON_PROJECTS)

#: Left out of :data:`DEV_LOCK`: a pin of the editable projects would constrain the checkout by itself.
DEV_LOCK_NO_EMIT = ("swcraft-tools", "swcraft-mcp", "swcraft-agent", "swcraft-specimens")

#: The one command ``--require-real-models`` means anything to; not a ``verify`` stage, so the flag
#: cannot ride along a gate run.
REQUIRE_REAL_MODELS_COMMAND = "test-real-models"

#: The real-export tier's pytest arguments. **Never ``-s``**: every one keeps what pytest would
#: print about the private exports off the terminal (``-p no:warnings``, ``--show-capture=no``,
#: ``--tb=no``), and the retention policy deletes the basetemp holding the imported model.
#: ``--timeout=0``: the thread method writes captured output straight to the terminal, so this
#: stage has no timeout and a hang is ended by a person; the command-line option, because
#: ``PYTEST_TIMEOUT`` would beat an ini override. ``-vv`` and ``-rfEs`` keep a names-free failure
#: message and skip reasons legible; ``--capture=fd`` is stated so ``addopts`` cannot turn it off.
REAL_MODELS_PYTEST_ARGS = (
    "-m",
    "real_models",
    "-vv",
    "-rfEs",
    "-p",
    "no:cacheprovider",
    "-p",
    "no:warnings",
    "--capture=fd",
    "--show-capture=no",
    "--tb=no",
    "-o",
    "tmp_path_retention_policy=none",
    "--timeout=0",
)

#: Environment variables that could undo :data:`REAL_MODELS_PYTEST_ARGS`; ``test-real-models``
#: refuses to start while any is set. ``PYTEST_ADDOPTS``/``PYTEST_PLUGINS`` are read before the argv;
#: ``PYTEST_DEBUG`` traces hook arguments past capture; ``PYTEST_DEBUG_TEMPROOT`` moves the basetemp.
#: ``PYTEST_TIMEOUT`` loses to ``--timeout=0``. Not refused, each judged unable to leak an export:
#: the colour and theme variables, ``CI``/``BUILD_NUMBER``, ``PYTEST_XDIST_AUTO_NUM_WORKERS``,
#: ``TOX_ENV_DIR``, ``PY_IGNORE_IMPORTMISMATCH``, ``PYTEST_DISABLE_PLUGIN_AUTOLOAD`` (fails the run
#: as a usage error). Outside the scan: ``COVERAGE_*``, ``PYTHON*``, any plugin installed later.
REAL_MODELS_REFUSED_ENVIRONMENT = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DEBUG", "PYTEST_DEBUG_TEMPROOT")

#: The variable naming the directory of real exports. A **relative** value is refused before any
#: step (the stage's pytest runs in ``packages/mcp/``); the message never names its value.
REAL_MODELS_VARIABLE = "SWCRAFT_REAL_MODELS"

#: The variable naming the specimen generator's executable (``specimen_generator.VARIABLE``,
#: spelled here because this script loads no other; a test holds them equal). A **relative**
#: value is refused before any step, as for :data:`REAL_MODELS_VARIABLE`.
SPECIMEN_GENERATOR_VARIABLE = "SWCRAFT_SPECIMEN_GENERATOR"


class ExportFlag(NamedTuple):
    """One machine-local export gate: its flag, the command that honours it, its :class:`Context` field.

    Every consumer reads flag and value through the row, so a reordering cannot misname a flag.
    """

    flag: str
    command: str
    attribute: str


#: Every machine-local export gate, in the order a stage's flags reach its pytest step.
EXPORT_FLAG_COMMANDS = (
    ExportFlag("--require-rhapsody-export", TEST_INTERNAL_COMMAND, "require_rhapsody_export"),
    ExportFlag("--require-second-export", TEST_INTERNAL_COMMAND, "require_second_export"),
    ExportFlag("--require-real-models", REQUIRE_REAL_MODELS_COMMAND, "require_real_models"),
)

#: The projects that must really collect tests for a marker-only stage. Elsewhere the marker may
#: collect nothing, and ``scripts/pytest_allow_no_tests.py`` maps exit 5 to 0; an owner never gets
#: that tolerance, or dropping its last marked test would leave a stage that runs nothing and
#: passes. ``real_models`` always collects a placeholder case, so an empty collection means the
#: module went missing.
MARKER_STAGE_OWNERS = {
    "artifact": ("tools", "scripts"),
    "slow": ("tools", "agent", "mcp", "specimens", "scripts"),
    "integration": ("tools", "specimens", "scripts"),
    "large_bench": ("mcp",),
    "real_models": ("mcp",),
}

#: ``verify``'s stages, in order. ``artifacts``, ``runtime`` and ``wheelhouse`` precede the
#: acceptances that install what they produce; ``runtime`` precedes ``wheelhouse``, which is
#: downloaded for the interpreter ``runtime`` unpacks (and refuses by name without it).
#: ``workstation-archive`` precedes ``test-artifacts`` so that suite installs a **real** archive.
#: The marker stages are here because their markers are deselected from ``test``.
#: ``test-model-integration`` is last: the slowest, consuming what every stage produced. Not stages:
#: the VS Code integration suite, the report-only ``coverage``/``benchmark``, and
#: ``test-real-models``, whose exports exist on one machine only.
VERIFY_STAGES = (
    "install",
    "lint",
    "test",
    "build",
    "artifacts",
    "runtime",
    "wheelhouse",
    "workstation-archive",
    "test-artifacts",
    "test-slow",
    "test-large-bench",
    "test-model-integration",
)

#: ``run(argv, cwd, stdin, env) -> returncode``: never raises on a non-zero exit. ``stdin`` is the
#: text fed to the process (``None``: inherited); ``env`` is set on top of the inherited environment.
Runner = Callable[[Sequence[str], Path, str | None, tuple[tuple[str, str], ...]], int]


class DevError(RuntimeError):
    """A precondition failed in a way this script can explain and act on."""


def _run(argv: Sequence[str], cwd: Path, stdin: str | None = None, env: tuple[tuple[str, str], ...] = ()) -> int:
    return subprocess.run(
        [str(part) for part in argv],
        cwd=str(cwd),
        input=None if stdin is None else stdin.encode("utf-8"),
        env={**os.environ, **dict(env)} if env else None,
    ).returncode


# ---------------------------------------------------------------------------
# Environment resolution
# ---------------------------------------------------------------------------


def venv_dir(repo_root: Path) -> Path:
    """The repo-local virtual environment directory."""
    return repo_root / VENV_DIRNAME


def venv_python(repo_root: Path, os_name: str = os.name) -> Path:
    """Interpreter path inside the repo-local venv; *os_name* makes both layouts testable anywhere."""
    if os_name == "nt":
        return venv_dir(repo_root) / "Scripts" / "python.exe"
    return venv_dir(repo_root) / "bin" / "python"


def venv_python_version(repo_root: Path) -> str | None:
    """``major.minor`` recorded in ``.venv/pyvenv.cfg``, or ``None``.

    ``None`` means no answer (no venv, an unreadable config, no version), never a mismatch.
    """
    config = venv_dir(repo_root) / "pyvenv.cfg"
    if not config.is_file():
        return None
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    recorded: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            recorded[key.strip()] = value.strip()
    # ``version`` is what 3.11 writes; 3.12+ also writes ``version_info``.
    raw = recorded.get("version") or recorded.get("version_info")
    if not raw:
        return None
    parts = raw.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    return f"{parts[0]}.{parts[1]}"


def check_venv_matches_bootstrap(repo_root: Path, bootstrap_version: str) -> None:
    """Refuse to re-run ``python -m venv`` over a venv from another interpreter.

    ``venv`` rewrites ``pyvenv.cfg`` but not the interpreter links, so the next step would die with
    an unexplained ``No module named 'encodings'``.
    """
    recorded = venv_python_version(repo_root)
    if recorded is None or recorded == bootstrap_version:
        return
    raise DevError(
        f"{venv_dir(repo_root)} was created by Python {recorded}, but this script is running on "
        f"Python {bootstrap_version}. Re-creating it in place would leave an unusable environment "
        "(its interpreter links and pyvenv.cfg would disagree). Either re-run with the pinned "
        f"interpreter (see .prototools), or delete {venv_dir(repo_root)} and run "
        "`python scripts/dev.py install` again."
    )


def refuse_bootstrap_from_inside_the_venv(repo_root: Path, bootstrap_python: str) -> None:
    """Refuse to re-create ``.venv`` with an interpreter that lives inside it.

    ``install`` (and ``verify``) starts with ``<bootstrap_python> -m venv .venv``. On Windows the
    running interpreter is a locked file, so that step fails with a bare ``Permission denied``; on
    POSIX it silently rebuilds the environment from inside itself. The refusal is unconditional.
    :func:`check_venv_matches_bootstrap` cannot catch this: the versions agree.

    Compared as paths, never resolved: on POSIX ``.venv/bin/python`` is a symlink to the base
    interpreter, so resolving would refuse every ordinary install. ``normcase`` for Windows.

    Does **not** reach an interpreter that is a *copy* of the venv's, or a venv reached through a
    symlinked or substituted parent directory; both fail at the step with the original error.
    """
    interpreter = Path(os.path.normcase(os.path.abspath(bootstrap_python)))
    venv = Path(os.path.normcase(os.path.abspath(venv_dir(repo_root))))
    if not interpreter.is_relative_to(venv):
        return
    raise DevError(
        f"this script is running on {bootstrap_python}, which is inside {venv_dir(repo_root)} -- "
        "the environment `install` is about to re-create. On Windows that step fails with a bare "
        "'[Errno 13] Permission denied' (a running interpreter is a locked file); elsewhere it "
        "quietly rebuilds the environment from inside itself. Re-run with an interpreter outside "
        "the venv: `python scripts/dev.py install` (the pinned version is in .prototools)."
    )


def require_venv_python(repo_root: Path, os_name: str = os.name) -> Path:
    """The venv interpreter, proven present; never falls back to ``sys.executable``."""
    interpreter = venv_python(repo_root, os_name)
    if not interpreter.exists():
        raise DevError(
            f"{interpreter} is missing -- this repository's virtual environment has not been created. "
            "Run `python scripts/dev.py install` first."
        )
    return interpreter


def moon_available() -> bool:
    """Whether the moon executable is on PATH.

    The standalone-integration acceptance runs a real ``moon run :build``, so its absence fails that
    command, never skips it.
    """
    return shutil.which("moon") is not None


def resolve_npm() -> str | None:
    """Resolve the npm executable, or ``None`` when it is not on PATH.

    ``shutil.which`` honours ``PATHEXT`` and returns ``npm.cmd`` on Windows, which runs without a shell.
    """
    return shutil.which("npm")


def resolve_uv() -> str | None:
    """The uv executable from ``PATH``, or ``None``; only ``update-dev-lock`` needs it."""
    return shutil.which("uv")


def read_uv_version(uv: str) -> str:
    """``uv <version>``, the two words the lock's header records."""
    result = subprocess.run([uv, "--version"], capture_output=True, text=True, encoding="utf-8")
    words = result.stdout.split()
    if result.returncode != 0 or len(words) < 2:
        raise DevError(f"`{uv} --version` did not name a version, so the lock's header cannot record one.")
    return " ".join(words[:2])


@dataclass(frozen=True)
class Context:
    """Everything a plan needs, resolved once before any step runs."""

    repo_root: Path
    #: The repo-local venv interpreter; the dispatcher checks it exists, per ``needs_venv``.
    python: str
    #: The interpreter that *creates* the venv; only ``install`` uses it.
    bootstrap_python: str = sys.executable
    #: ``major.minor`` of :attr:`bootstrap_python`, compared against an existing ``.venv``.
    bootstrap_version: str = f"{sys.version_info.major}.{sys.version_info.minor}"
    #: ``None`` when npm is not on PATH -- reported by the plan that needs it.
    npm: str | None = None
    #: False when moon is not on PATH: a failure for ``test-model-integration``, never a skip.
    has_moon: bool = False
    #: Skip the Node half of ``install``/``test``/``lint``.
    skip_node: bool = False
    #: Turn ``test-internal``'s Rhapsody-export skip into a failure.
    require_rhapsody_export: bool = False
    #: Turn ``test-internal``'s skip on ``SWCRAFT_SECOND_EXPORT`` into a failure.
    require_second_export: bool = False
    #: Turn ``test-real-models``' skip on an unset ``SWCRAFT_REAL_MODELS`` into a failure.
    require_real_models: bool = False
    #: Stop each pytest step of ``test`` after this many failures; ``None`` runs every test.
    maxfail: int | None = None
    #: ``install`` only: leave out the lock and the pin of pip (:data:`UNLOCKED_COMMAND`).
    unlocked: bool = False
    #: Whether :data:`DEV_LOCK` is absent; a locked ``install`` refuses to plan while it is.
    dev_lock_missing: bool = False
    #: ``None`` when uv is not on PATH -- reported by the plan that needs it.
    uv: str | None = None
    #: ``uv <version>``, read by ``build_context`` for ``update-dev-lock`` alone.
    uv_version: str = ""
    #: The :data:`REAL_MODELS_REFUSED_ENVIRONMENT` variables set; ``test-real-models`` refuses while any is.
    refused_pytest_environment: tuple[str, ...] = ()
    #: Whether :data:`REAL_MODELS_VARIABLE` is relative; ``test-real-models`` refuses while it is.
    real_models_relative: bool = False
    #: Whether :data:`SPECIMEN_GENERATOR_VARIABLE` is relative; ``test-real-models`` refuses while it is.
    specimen_generator_relative: bool = False
    #: ``test-internal`` only: the absolute pytest paths to run; empty runs its ``tests``.
    internal_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class Step:
    """One subprocess invocation, with the label reported when it fails."""

    label: str
    argv: tuple[str, ...]
    cwd: Path
    #: Text fed to the process's standard input.
    stdin: str | None = None
    #: ``(name, value)`` pairs set on top of the inherited environment; empty inherits it unchanged.
    env: tuple[tuple[str, str], ...] = ()


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def _skip_node_commands_phrase() -> str:
    """``install`, `lint` and `test``` -- rendered from :data:`SKIP_NODE_COMMANDS`, so messages cannot drift."""
    names = [f"`{name}`" for name in SKIP_NODE_COMMANDS]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _require_npm(context: Context) -> str:
    """The npm executable, proven present, with an actionable error when absent."""
    if context.npm is None:
        raise DevError(
            "npm was not found on PATH -- the VS Code extension's dependencies cannot be installed "
            "and its npm scripts cannot be run. Install Node.js 22 (see .prototools), or run the "
            f"Python-only commands ({_skip_node_commands_phrase()}) with `--skip-node`: that flag "
            "covers those three and not `artifacts`, so it cannot carry the full `verify` gate."
        )
    return context.npm


def _npm_step(context: Context, script: str) -> Step:
    """One ``npm run <script>`` in the extension project."""
    return Step(
        label=f"npm run {script} ({VSCODE_PROJECT})",
        argv=(_require_npm(context), "run", script),
        cwd=context.repo_root / VSCODE_PROJECT,
    )


def _pytest_step(context: Context, project: str, *args: str, label: str, allow_no_tests: bool = False) -> Step:
    """One pytest invocation inside a Python project.

    ``allow_no_tests`` maps pytest's exit 5 ("no tests collected") to 0, for the projects of a
    marker-only stage that legitimately collect nothing; never for an owner
    (:data:`MARKER_STAGE_OWNERS`), and never in the fast gate.
    """
    if allow_no_tests:
        head: tuple[str, ...] = (
            context.python,
            str(context.repo_root / "scripts" / "pytest_allow_no_tests.py"),
        )
    else:
        head = (context.python, "-m", "pytest")
    return Step(label=label, argv=(*head, *args), cwd=project_test_dir(context.repo_root, project))


def project_test_dir(repo_root: Path, project: str) -> Path:
    """The directory a pytest step of *project* runs in: ``packages/<project>`` or a test-only folder."""
    if project in TEST_ONLY_PROJECTS:
        return repo_root / project
    return repo_root / PACKAGES_DIR / project


def dev_lock_path(repo_root: Path) -> Path:
    """The absolute path of :data:`DEV_LOCK` in this checkout."""
    return repo_root / DEV_LOCK


def _refuse_an_install_it_cannot_lock(context: Context) -> None:
    """A locked ``install`` needs the lock; an unlocked one needs a new ``.venv``."""
    if context.unlocked:
        if venv_dir(context.repo_root).exists():
            raise DevError(
                f"--unlocked refuses an existing {VENV_DIRNAME}: pip would keep the versions already "
                "there and the run would test nothing new. Run it in a fresh checkout, as quick.yml's "
                "`unlocked` input does."
            )
    elif context.dev_lock_missing:
        raise DevError(
            f"{DEV_LOCK} is missing, and `install` pins the development environment to it. Restore it "
            f"from git, or refresh it with `python scripts/dev.py {UPDATE_DEV_LOCK_COMMAND}`."
        )


def plan_install(context: Context) -> list[Step]:
    """Create ``.venv``, install the four Python projects into it, stage elkjs, then ``npm ci``.

    The elkjs step stages the pinned tarball as ``swcraft-tools`` package data, so an editable
    install lays diagrams out with the engine the wheel carries.

    The lock (:data:`DEV_LOCK`): the first pip step installs the locked pip, which knows
    ``--build-constraint``; every later ``pip install`` carries it (for isolated builds) and
    ``--constraint``. Both are arguments, never pip's variables, which pip splits on whitespace
    (a checkout path with a space would break them). ``pip check`` ends the pip steps.
    ``--unlocked`` omits both, upgrades pip instead of pinning it and prints ``pip freeze``.
    """
    refuse_bootstrap_from_inside_the_venv(context.repo_root, context.bootstrap_python)
    check_venv_matches_bootstrap(context.repo_root, context.bootstrap_version)
    _refuse_an_install_it_cannot_lock(context)
    lock = str(dev_lock_path(context.repo_root))
    lock_options = () if context.unlocked else ("--constraint", lock, "--build-constraint", lock)
    steps = [
        Step(
            label="create .venv",
            argv=(context.bootstrap_python, "-m", "venv", str(venv_dir(context.repo_root))),
            cwd=context.repo_root,
        )
    ]
    if context.unlocked:
        # The bundled pip may predate `--group` (pip 25.1); the canary takes the newest one.
        steps.append(
            Step(
                label="upgrade pip",
                argv=(context.python, "-m", "pip", "install", "--upgrade", "pip"),
                cwd=context.repo_root,
            )
        )
    else:
        # Without `--build-constraint`: this is the bundled pip, which may predate the option,
        # and a pip wheel needs no build.
        steps.append(
            Step(
                label="install the locked pip",
                argv=(context.python, "-m", "pip", "install", "--constraint", lock, "pip"),
                cwd=context.repo_root,
            )
        )
    for project in PYTHON_PROJECTS:
        steps.append(
            Step(
                label=f"install {project}",
                argv=(context.python, "-m", "pip", "install", *lock_options, "-e", ".", "--group", DEV_GROUP),
                cwd=context.repo_root / PACKAGES_DIR / project,
            )
        )
    steps.append(Step(label="pip check", argv=(context.python, "-m", "pip", "check"), cwd=context.repo_root))
    if context.unlocked:
        steps.append(Step(label="pip freeze", argv=(context.python, "-m", "pip", "freeze"), cwd=context.repo_root))
    # Before the Node half, so `--skip-node install` stages it too: the fetch needs no npm,
    # and the diagram tests' only precondition is then Node itself.
    steps.append(_build_artifacts_step(context, "--elkjs", label="stage the elkjs payload"))
    if not context.skip_node:
        steps.append(
            Step(
                label=f"npm ci ({VSCODE_PROJECT})",
                argv=(_require_npm(context), "ci"),
                cwd=context.repo_root / VSCODE_PROJECT,
            )
        )
    return steps


def _build_artifacts_step(context: Context, *args: str, label: str) -> Step:
    """One ``scripts/build_artifacts.py`` invocation, run by the venv interpreter.

    The interpreter matters: the script builds with its ``sys.executable``, and a wheelhouse is
    usable only by the interpreter and platform it was resolved for.
    """
    return Step(
        label=label,
        argv=(
            context.python,
            str(context.repo_root / "scripts" / "build_artifacts.py"),
            "--repo-root",
            str(context.repo_root),
            *args,
        ),
        cwd=context.repo_root,
    )


def plan_wheelhouse(context: Context) -> list[Step]:
    """Populate ``dist/wheelhouse/`` for the Python the workstation archive carries.

    The script downloads on the interpreter ``runtime`` unpacked and refuses by name when it is absent.
    """
    return [_build_artifacts_step(context, "--wheelhouse", label="populate dist/wheelhouse")]


def plan_workstation_archive(context: Context) -> list[Step]:
    """Compose ``dist/workstation/`` from what ``artifacts``, ``runtime`` and ``wheelhouse`` left.

    Needs no Node: the ``.vsix`` is already packaged.
    """
    return [_build_artifacts_step(context, "--workstation-archive", label="build dist/workstation")]


def plan_runtime(context: Context) -> list[Step]:
    """Populate ``dist/runtime/`` with the pinned payloads the workstation archive carries.

    It reads the committed pins and writes only ``dist/runtime/`` and the gitignored elkjs payload:
    a ``verify`` stage that rewrote a tracked file would dirty every gate run. npm is not a
    precondition and ``--skip-node`` does not refuse it: every payload is a download checked
    against a committed digest.
    """
    return [_build_artifacts_step(context, "--runtime", label="populate dist/runtime")]


def plan_update_pins(context: Context) -> list[Step]:
    """Rewrite ``scripts/workstation/pins/runtime-pins.json`` from the publishers.

    Transcribes the Node.js and CPython releases' digests and mints each companion extension at its
    newest release. Not a ``verify`` stage: its output is a diff for a person to review.
    """
    return [_build_artifacts_step(context, "--update-pins", label="rewrite scripts/workstation/pins/runtime-pins.json")]


def plan_update_dev_lock(context: Context) -> list[Step]:
    """Rewrite :data:`DEV_LOCK` with ``uv pip compile``, the newest versions the projects admit.

    The custom compile command is how the header names the uv version. ``--upgrade``, because uv
    otherwise keeps an existing file's pins. Not a ``verify`` stage: its output is a diff to review.
    """
    if context.uv is None:
        raise DevError(
            f"uv was not found on PATH -- `{UPDATE_DEV_LOCK_COMMAND}` compiles {DEV_LOCK} with it. uv is "
            "needed only on the machine that refreshes the lock: install it there and run this again."
        )
    no_emit = tuple(part for name in DEV_LOCK_NO_EMIT for part in ("--no-emit-package", name))
    groups = tuple(part for group in DEV_LOCK_GROUPS for part in ("--group", group))
    return [
        Step(
            label=f"rewrite {DEV_LOCK}",
            argv=(
                context.uv,
                "pip",
                "compile",
                "--universal",
                "--python-version",
                "3.11",
                "--upgrade",
                "--quiet",
                "--custom-compile-command",
                f"python scripts/dev.py {UPDATE_DEV_LOCK_COMMAND} ({context.uv_version})",
                *no_emit,
                *groups,
                "--output-file",
                DEV_LOCK,
                "-",
            ),
            cwd=context.repo_root,
            stdin=DEV_LOCK_INPUT,
        )
    ]


def runtime_dir(repo_root: Path) -> Path:
    """``dist/runtime/`` -- where the payloads and their ``RUNTIME.json`` land."""
    return repo_root / "dist" / "runtime"


def verify_runtime_payload(context: Context) -> None:
    """Assert the stage really left the record, the payload files and the unpacked interpreter.

    ``dist/runtime/`` is in no manifest, so nothing downstream would notice an empty stage. It
    re-derives no version or platform token, so it does not reach a payload of another platform left
    by an earlier run (the builder's whole-directory swap prevents that), and it does not run the
    interpreter.
    """
    directory = runtime_dir(context.repo_root)
    node = sorted(directory.glob("node.*"), key=lambda path: path.as_posix()) if directory.is_dir() else []
    expected = [
        directory / "RUNTIME.json",
        directory / "python.tar.gz",
        directory / "THIRD-PARTY-NOTICES.md",
    ]
    problems = [
        f"{path} is {'empty' if path.is_file() else 'missing'}"
        for path in expected
        if not path.is_file() or path.stat().st_size == 0
    ]
    if not (directory.is_dir() and any(path.is_file() for path in directory.glob("*.vsix"))):
        problems.append(f"{directory} holds no companion extension (*.vsix)")
    if len(node) != 1:
        problems.append(f"{directory} holds {len(node)} Node payload files (node.*), not one")
    elif node[0].stat().st_size == 0:
        problems.append(f"{node[0]} is empty")
    if not (directory / "python").is_dir():
        problems.append(f"{directory / 'python'} is missing, so `wheelhouse` has no interpreter to resolve for")
    if problems:
        raise DevError("the runtime stage did not produce its payloads: " + ", ".join(problems))


def workstation_archive_dir(repo_root: Path) -> Path:
    """``dist/workstation/`` -- where the archive and its checksums land."""
    return repo_root / "dist" / "workstation"


def verify_workstation_archive(context: Context) -> None:
    """Assert the stage really left one archive and its ``SHA256SUMS`` behind.

    The archive is in no manifest, so nothing downstream would notice an empty stage. It re-derives
    no version or platform token, so it does not reach an archive left by a previous version (the
    builder empties the directory first).
    """
    directory = workstation_archive_dir(context.repo_root)
    archives = sorted(directory.glob("swcraft-*.zip"), key=lambda path: path.as_posix()) if directory.is_dir() else []
    if not archives:
        raise DevError(
            f"the workstation-archive stage produced no archive in {directory}: expected "
            "dist/workstation/swcraft-<version>-<platform>.zip"
        )
    sums = directory / "SHA256SUMS"
    problems = [
        f"{path} is {'empty' if path.is_file() else 'missing'}"
        for path in (*archives, sums)
        if not path.is_file() or path.stat().st_size == 0
    ]
    if problems:
        raise DevError("the workstation archive is incomplete: " + ", ".join(problems))


def plan_artifacts(context: Context) -> list[Step]:
    """Build the versioned local artifacts (``dist/python``, ``dist/vscode``, ``dist/docs``).

    It also copies the rendered guide into ``packages/tools/src/swcraft_tools/guide_site/`` for the
    wheel and sdist (gitignored, rewritten every run, removed by no clean step). One step: ``docs``
    and this command build the site through the same definition, rendered once.

    It reaches the network (``npm ci``, PEP 517 backends) though not in :data:`NETWORK_COMMANDS`.
    npm is checked here, and ``--skip-node`` refused, at plan time: the script needs npm first, and
    ``--skip-node verify`` would otherwise fail after ``install``, ``lint``, ``test`` and ``build``.
    """
    if context.skip_node:
        raise DevError(
            "`--skip-node` cannot build the artifacts: half of what `artifacts` produces is the "
            "packaged .vsix, and only npm can build it. The flag covers "
            f"{_skip_node_commands_phrase()}; it does not cover `artifacts`, and therefore not the "
            "full `verify` gate either. Install Node.js 22 (see .prototools) to run `verify`, or "
            "run those Python-only commands individually on a machine without Node."
        )
    _require_npm(context)
    return [
        _build_artifacts_step(context, label="build dist/python, dist/vscode, dist/docs and the wheel's guide payload")
    ]


def plan_docs(context: Context) -> list[Step]:
    """Render the user guide into ``dist/docs/site/``.

    Through ``build_artifacts.py --docs-only``, not an mkdocs argv here: the site build (flags,
    version, output) is defined once there. Needs no npm.
    """
    return [_build_artifacts_step(context, "--docs-only", label="build the user guide into dist/docs/site")]


#: The label of ``lint``'s import-graph step, which tests tell apart from the per-project lint steps.
IMPORT_LINT_LABEL = "lint-imports tools"

#: import-linter's arguments. ``--no-cache``: the graph builds in under a second, and a cache would be
#: one more untracked file in ``packages/tools/``.
IMPORT_LINT_ARGS = ("--no-cache",)


def import_lint_executable(python: str | Path) -> str:
    """import-linter's console script, ``lint-imports``, beside *python* in the same environment.

    Not a ``PATH`` lookup, which could resolve to another environment's. ``.exe`` beside a ``.exe``
    interpreter, no suffix otherwise.
    """
    interpreter = Path(python)
    suffix = ".exe" if interpreter.suffix.lower() == ".exe" else ""
    return str(interpreter.with_name(f"lint-imports{suffix}"))


#: The mypy configuration of ``scripts/``, relative to the repository root, the "lint scripts" step's directory.
SCRIPTS_MYPY_CONFIG = "scripts/mypy-scripts.ini"

#: The label of ``lint``'s quality-ratchet step, which tests tell apart from the per-project lint steps.
QUALITY_RATCHET_LABEL = "quality ratchet"


def plan_lint(context: Context) -> list[Step]:
    """ruff over the four Python projects **and** ``scripts/``, the import-graph gate, the quality ratchet, then Biome.

    ``run_python_lint.py`` adds mypy in each project declaring ``[tool.mypy]``. ``scripts/`` has no
    project, so it needs its own entry or goes unlinted, and its mypy configuration is
    :data:`SCRIPTS_MYPY_CONFIG`. The import-graph step checks the contracts
    of ``packages/tools/pyproject.toml`` and reaches ``swcraft_tools`` alone. The quality ratchet
    runs from the repository root; what it does not reach is in its docstring.
    """
    runner = str(context.repo_root / "scripts" / "run_python_lint.py")
    steps = [
        Step(label=f"lint {project}", argv=(context.python, runner), cwd=context.repo_root / PACKAGES_DIR / project)
        for project in PYTHON_PROJECTS
    ]
    steps.append(
        Step(
            label="lint scripts",
            argv=(context.python, runner, "--mypy-config", SCRIPTS_MYPY_CONFIG, "scripts"),
            cwd=context.repo_root,
        )
    )
    steps.append(
        Step(
            label=IMPORT_LINT_LABEL,
            argv=(import_lint_executable(context.python), *IMPORT_LINT_ARGS),
            cwd=context.repo_root / PACKAGES_DIR / "tools",
        )
    )
    steps.append(
        Step(
            label=QUALITY_RATCHET_LABEL,
            argv=(context.python, str(context.repo_root / "scripts" / "quality_ratchet.py"), "check"),
            cwd=context.repo_root,
        )
    )
    if not context.skip_node:
        steps.append(_npm_step(context, "lint"))
    return steps


def plan_test(context: Context) -> list[Step]:
    """The fast gate: pytest per Python project, then the extension's npm test.

    Each pytest step runs under :data:`FAST_TEST_PARALLEL` and :data:`FAST_TEST_TIMEOUT`. ``npm run
    test``, not ``node --test``, recompiles first, so a stale ``out/`` never passes. ``--skip-node``
    reaches only :data:`SKIP_NODE_PROJECTS`' pytest steps (an argparse error elsewhere); without it
    the diagram layout tests **fail** without Node. ``--maxfail`` reaches every pytest step, never
    the npm step.
    """
    steps = [
        _pytest_step(
            context,
            project,
            "tests/",
            "-v",
            *FAST_TEST_PARALLEL,
            *FAST_TEST_TIMEOUT,
            *(("--skip-node",) if context.skip_node and project in SKIP_NODE_PROJECTS else ()),
            *((f"--maxfail={context.maxfail}",) if context.maxfail is not None else ()),
            "-m",
            FAST_TEST_DESELECT,
            label=f"pytest {project}",
        )
        for project in TEST_PROJECTS
    ]
    if not context.skip_node:
        steps.append(_npm_step(context, "test"))
    return steps


def reference_project(repo_root: Path) -> Path:
    """The reference model project directory (``examples/basic``)."""
    return repo_root / Path(REFERENCE_PROJECT)


def reference_manifest(repo_root: Path) -> Path:
    """The reference project's ``swcraft.yaml``."""
    return reference_project(repo_root) / REFERENCE_MANIFEST


def reference_outputs(repo_root: Path) -> tuple[Path, ...]:
    """The generated files the reference build must produce (:data:`REFERENCE_OUTPUTS`)."""
    return tuple(reference_project(repo_root) / Path(relative) for relative in REFERENCE_OUTPUTS)


def _cli_step(context: Context, *args: str, label: str) -> Step:
    """One ``swcraft`` CLI invocation against the reference manifest.

    The module form is bound to this interpreter; a bare ``swcraft`` could resolve to another
    environment's entry point.
    """
    return Step(
        label=label,
        argv=(
            context.python,
            "-m",
            "swcraft_tools.cli",
            *args,
            "--project",
            str(reference_manifest(context.repo_root)),
        ),
        cwd=context.repo_root,
    )


def plan_build(context: Context) -> list[Step]:
    """The reference-model chain: remove the previous outputs, ``check-model``, generate, validate.

    After a failing build no output of the previous run is left under its own name (the previous
    ARXML is kept as ``_old``):

    * the removal runs first, because a stale output after a failed rebuild is indistinguishable
      from a fresh, passing build;
    * ``check-model`` runs before any generator because it writes nothing; ``swcraft validate``
      would rewrite the ARXML the last step validates.

    A missing licensed XSD is not an error: the download script exits 0 and the validation falls
    back to a structural check.
    """
    outputs = reference_outputs(context.repo_root)
    return [
        Step(
            label="remove the previous outputs (examples/basic)",
            argv=(
                context.python,
                str(context.repo_root / "scripts" / "clean_outputs.py"),
                *(str(path) for path in outputs),
            ),
            cwd=context.repo_root,
        ),
        _cli_step(context, "check-model", label="check-model (examples/basic)"),
        _cli_step(context, "generate", "arxml", label="generate arxml (examples/basic)"),
        _cli_step(context, "generate", "diagrams", label="generate diagrams (examples/basic)"),
        _cli_step(context, "generate", "needs", label="generate needs (examples/basic)"),
        Step(
            label="provision the AUTOSAR XSD (optional)",
            argv=(context.python, str(context.repo_root / "scripts" / "download_autosar_schema.py")),
            cwd=context.repo_root,
        ),
        Step(
            label="validate the generated ARXML",
            argv=(context.python, "-m", "swcraft_tools.arxml_validation", str(outputs[0])),
            cwd=context.repo_root,
        ),
    ]


def verify_build_outputs(context: Context) -> None:
    """Assert the chain really produced every declared output, non-empty.

    A generator that exits 0 without writing (or truncated) would otherwise pass. Runs only after
    every step succeeded.
    """
    problems = [
        f"{path} is {'empty' if path.is_file() else 'missing'}"
        for path in reference_outputs(context.repo_root)
        if not path.is_file() or path.stat().st_size == 0
    ]
    if problems:
        raise DevError("the reference build did not produce its declared outputs: " + ", ".join(problems))


def _marker_stage_steps(context: Context, marker: str, *extra_args: str) -> list[Step]:
    """One pytest step per Python project selecting *marker*, and one per test-only project that owns it.

    Owners (:data:`MARKER_STAGE_OWNERS`) must collect something; other Python projects may collect
    nothing. *extra_args* go to every project, so none may be an option only one project's initial
    conftest registers (an argparse error elsewhere): :func:`plan_test_real_models` builds its own steps.
    """
    owners = MARKER_STAGE_OWNERS[marker]
    return [
        _pytest_step(
            context,
            project,
            "tests/",
            "-v",
            *extra_args,
            "-m",
            marker,
            label=f"pytest -m {marker} {project}",
            allow_no_tests=project not in owners,
        )
        for project in TEST_PROJECTS
        if project in PYTHON_PROJECTS or project in owners
    ]


def plan_test_artifacts(context: Context) -> list[Step]:
    """The **only** stage that runs the ``artifact`` suite (deselected everywhere else)."""
    return _marker_stage_steps(context, "artifact")


def plan_test_slow(context: Context) -> list[Step]:
    """The **only** stage that runs the open ``slow`` suite (the worktree scenario replay among them).

    It takes no export flag: the real-export suites are closed and run under ``test-internal``. The
    one export-dependent case left, the ``benchmark``+``slow`` import, skips when
    ``SWCRAFT_RHAPSODY_EXPORT`` is unset and asserts no figure.
    """
    return _marker_stage_steps(context, "slow", "-s")


def _requested_export_flags(context: Context, command: str) -> tuple[str, ...]:
    """The :data:`EXPORT_FLAG_COMMANDS` flags *context* carries whose row names *command*.

    Filtered by command too: a direct call bypasses ``build_context``'s refusal, and another stage's
    flag would be an argparse error.
    """
    return tuple(row.flag for row in EXPORT_FLAG_COMMANDS if row.command == command and getattr(context, row.attribute))


def plan_test_large_bench(context: Context) -> list[Step]:
    """The **only** stage that runs the ``large_bench`` suite: the functional scenarios on the large bench.

    A stage of its own so its red reads as the large tier; out of the fast gate because its
    scenarios exceed the per-test budget.
    """
    return _marker_stage_steps(context, "large_bench")


def plan_test_real_models(context: Context) -> list[Step]:
    """The **only** stage that runs the ``real_models`` suite: the passport chain's calibration.

    Its module skips its placeholder case when ``SWCRAFT_REAL_MODELS`` is unset, or **fails** under
    ``--require-real-models``, which reaches ``mcp`` alone. Every project takes exactly
    :data:`REAL_MODELS_PYTEST_ARGS`. The exports are a calibration set, never an oracle: report-only
    until the chain is green on every export (``real_models.CALIBRATION_BLOCKING``). Not a ``verify``,
    CI or bump step: the exports exist on one machine only.

    Raises:
        DevError: a :data:`REAL_MODELS_REFUSED_ENVIRONMENT` variable is set, or
            :data:`REAL_MODELS_VARIABLE` or :data:`SPECIMEN_GENERATOR_VARIABLE` is relative.
    """
    refused = context.refused_pytest_environment
    if refused:
        named = " and ".join(refused)
        raise DevError(
            f"{named} {'is' if len(refused) == 1 else 'are'} set, and pytest reads "
            f"{'it' if len(refused) == 1 else 'them'} beside the stage's own arguments: an option such as "
            "-s or --basetemp, a hook trace or a moved temporary root would put a "
            f"real export's output on the terminal or leave its import on the disk. Unset {named} and run "
            f"`{REQUIRE_REAL_MODELS_COMMAND}` again."
        )
    if context.real_models_relative:
        raise DevError(
            f"{REAL_MODELS_VARIABLE} is a relative path, and the stage's pytest runs in packages/mcp/, "
            "where it would name another directory than the one this command was run from. Set "
            f"{REAL_MODELS_VARIABLE} to the directory's absolute path and run `{REQUIRE_REAL_MODELS_COMMAND}` again."
        )
    if context.specimen_generator_relative:
        raise DevError(
            f"{SPECIMEN_GENERATOR_VARIABLE} is a relative path, and the stage's pytest runs in packages/mcp/, "
            "where it would name another file than the one this command was run beside. Set "
            f"{SPECIMEN_GENERATOR_VARIABLE} to the generator's absolute path, or unset it to use the one "
            f"`install` installs, and run `{REQUIRE_REAL_MODELS_COMMAND}` again."
        )
    owners = MARKER_STAGE_OWNERS["real_models"]
    flags = _requested_export_flags(context, REQUIRE_REAL_MODELS_COMMAND)
    return [
        _pytest_step(
            context,
            project,
            "tests/",
            *REAL_MODELS_PYTEST_ARGS,
            *(flags if project == "mcp" else ()),
            label=f"pytest -m real_models {project}",
            allow_no_tests=project not in owners,
        )
        for project in PYTHON_PROJECTS
    ]


def plan_test_model_integration(context: Context) -> list[Step]:
    """The **only** stage that runs the ``integration`` suite -- and it needs moon.

    An unresolvable ``moon`` fails here: this acceptance must not be closable by a skip. The
    specimen generator's step sets :data:`SWCRAFT_BIN_VARIABLE` to the ``swcraft`` beside the
    stage's interpreter, the checkout's own CLI. It does not reach a skip inside that project for
    another reason while one case still passes.
    """
    if not context.has_moon:
        raise DevError(
            "moon was not found on PATH -- the standalone-architecture integration acceptance "
            "cannot run, and it must not be skipped. Install moon (see the architecture project's "
            "own .prototools) and re-run `python scripts/dev.py test-model-integration`."
        )
    interpreter = Path(context.python)
    swcraft = str(interpreter.with_name(f"swcraft{interpreter.suffix}"))
    return [
        replace(step, env=((SWCRAFT_BIN_VARIABLE, swcraft),)) if step.cwd.name == SPECIMEN_GENERATOR_PROJECT else step
        for step in _marker_stage_steps(context, "integration")
    ]


def plan_test_xsd(context: Context) -> list[Step]:
    """The XSD-conformance gate: the licensed AUTOSAR schemas, or a failure.

    ``--require-autosar-xsd`` turns the fast gate's skip into a failure. Not a ``verify`` stage: the
    schemas are licensed and not redistributable. Point ``SWCRAFT_AUTOSAR_XSD_PATH_4_2_2`` /
    ``SWCRAFT_AUTOSAR_XSD_PATH_4_3_0`` at them or place them in ``schemas/``; a git worktree does not
    inherit the main checkout's ``schemas/`` (gitignored).
    """
    return [
        _pytest_step(
            context,
            "tools",
            *XSD_CONFORMANCE_SUITE,
            "-v",
            "--require-autosar-xsd",
            label="pytest tools (XSD conformance)",
        )
    ]


def open_copy_notice(repo_root: Path, *, export_required: bool = False) -> str | None:
    """:data:`OPEN_COPY_NOTICE` in an open copy, ``None`` in the work repository; raises when unsure.

    The open copy is :data:`OPEN_COPY_MARKER` holding exactly :data:`OPEN_COPY_MARKER_TEXT` (line
    endings aside) and **no** ``internal/``. No marker: ``None``, and :func:`plan_test_internal`
    fails where ``internal/`` is missing.

    Raises:
        DevError: the marker is not a regular file or holds other text; a marker beside an
            ``internal/``; an open copy asked for an export gate.
    """
    marker = repo_root / OPEN_COPY_MARKER
    if not marker.exists() and not marker.is_symlink():
        return None
    if marker.is_symlink() or not marker.is_file():
        raise DevError(f"{OPEN_COPY_MARKER} is not a regular file; only the open copy's own marker file is read.")
    if marker.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n") != OPEN_COPY_MARKER_TEXT:
        raise DevError(
            f"{OPEN_COPY_MARKER} does not hold the text scripts/public_snapshot.py writes; this tree is neither "
            f"an open copy nor the work repository, and `{TEST_INTERNAL_COMMAND}` decides nothing in it."
        )
    if (repo_root / INTERNAL_DIR).exists():
        raise DevError(
            f"{OPEN_COPY_MARKER} and {INTERNAL_DIR}/ are both present: the open copy has no {INTERNAL_DIR}/ and "
            f"the work repository no {OPEN_COPY_MARKER}. Remove the one that does not belong to this tree."
        )
    if export_required:
        raise DevError(
            f"a --require-*-export flag demands the internal tests' export gates, and this open copy has no "
            f"internal tests: {OPEN_COPY_NOTICE}"
        )
    return OPEN_COPY_NOTICE


def plan_test_internal(context: Context) -> list[Step]:
    """The work repository's internal tests: ruff over ``internal/``, then pytest there.

    Serial, with :attr:`Context.python` (``--python`` overrides it), over
    :attr:`Context.internal_paths` or ``tests``. The export flags reach the pytest step and turn the
    real-export suites' skip into a failure. A missing folder is a failure, never a skip; the open
    copy never reaches this plan (:func:`open_copy_notice` answers first).
    """
    internal = context.repo_root / INTERNAL_DIR
    if not internal.is_dir():
        raise DevError(
            f"{INTERNAL_DIR}/ is missing: `{TEST_INTERNAL_COMMAND}` runs the record-consistency tests of the "
            f"work repository, which the open copy does not carry (an open copy carries {OPEN_COPY_MARKER} and "
            "is told so instead). Run it in a full checkout of the work repository."
        )
    runner = str(context.repo_root / "scripts" / "run_python_lint.py")
    return [
        Step(label=f"lint {INTERNAL_DIR}", argv=(context.python, runner, "."), cwd=internal),
        Step(
            label=f"pytest {INTERNAL_DIR}",
            argv=(
                context.python,
                "-m",
                "pytest",
                *(context.internal_paths or ("tests",)),
                "-v",
                *_requested_export_flags(context, TEST_INTERNAL_COMMAND),
            ),
            cwd=internal,
        ),
    ]


def plan_coverage(context: Context) -> list[Step]:
    """Report-only coverage per Python project; never wired into ``verify``."""
    return [
        _pytest_step(
            context,
            project,
            "tests/",
            "-m",
            FAST_TEST_DESELECT,
            f"--cov={COVERAGE_PACKAGES[project]}",
            "--cov-report=term",
            "--cov-report=json",
            label=f"coverage {project}",
            allow_no_tests=True,
        )
        for project in PYTHON_PROJECTS
    ]


def plan_benchmark(context: Context) -> list[Step]:
    """Report-only pytest-benchmark baseline suite; ``tools`` only, on demand."""
    return [
        Step(
            label="benchmark tools",
            argv=(
                context.python,
                str(context.repo_root / "scripts" / "run_benchmark.py"),
                "tests/",
                "-v",
                "-m",
                "benchmark",
                "--benchmark-only",
            ),
            cwd=context.repo_root / PACKAGES_DIR / "tools",
        )
    ]


def plan_package(context: Context) -> list[Step]:
    """Package the extension into an installable ``.vsix``."""
    return [_npm_step(context, "package")]


def _agents_md_step(context: Context, command: str) -> Step:
    """The step that renders :data:`AGENTS_MD_FILES` and :data:`KIRO_STEERING_FILES` from :data:`AGENTS_MD_PROFILE`.

    ``schemas`` and ``docs-reference`` both end with it, since each rewrites a region of that profile.
    """
    return Step(
        label=f"{command} (the project AGENTS.md and Kiro steering file from the template profile)",
        argv=(
            context.python,
            "-m",
            "swcraft_tools.agents_md",
            "--profile",
            str(context.repo_root / Path(AGENTS_MD_PROFILE)),
            *(
                word
                for relative in KIRO_STEERING_FILES
                for word in ("--kiro-steering", str(context.repo_root / Path(relative)))
            ),
            *(str(context.repo_root / Path(relative)) for relative in AGENTS_MD_FILES),
        ),
        cwd=context.repo_root,
    )


def plan_schemas(context: Context) -> list[Step]:
    """Regenerate the editor JSON Schemas, ``references.json`` and the association block.

    Paths are passed absolute, so the steps do not depend on the working directory.
    """
    return [
        Step(
            label="schemas (editor JSON Schemas from the model contract)",
            argv=(
                context.python,
                "-m",
                "swcraft_tools.editor_schema",
                "--output-dir",
                str(context.repo_root / Path(EDITOR_SCHEMAS_DIR)),
                "--package-json",
                str(context.repo_root / Path(EXTENSION_PACKAGE_JSON)),
            ),
            cwd=context.repo_root,
        ),
        Step(
            label="schemas (the model schema section of the agent profiles)",
            argv=(
                context.python,
                "-m",
                "swcraft_tools.editing.agent_schema",
                *(str(context.repo_root / Path(profile)) for profile in AGENT_PROFILES),
            ),
            cwd=context.repo_root,
        ),
        _agents_md_step(context, "schemas"),
    ]


def plan_docs_reference(context: Context) -> list[Step]:
    """Regenerate the guide's committed reference pages and the nav section that names them.

    One step per generating package: ``swcraft_tools`` may not import ``swcraft_mcp``, and this
    script imports neither. ``--output-dir`` is the guide root, which the artifact paths and nav rows
    are relative to. **Only the first step gets ``--mkdocs-yml``**: its rewrite keeps rows it does
    not own, and a second writer would fight it; the tool page's row is committed and tested. The
    last steps rewrite the guide index of :data:`GUIDE_INDEX_FILES` from the guide as the generators
    left it, then the ``AGENTS.md`` files.
    """
    return [
        Step(
            label="docs-reference (the guide's generated reference pages)",
            argv=(
                context.python,
                "-m",
                "swcraft_tools.guide_reference",
                "--output-dir",
                str(context.repo_root / Path(GUIDE_DIR)),
                "--mkdocs-yml",
                str(context.repo_root / Path(MKDOCS_CONFIG)),
            ),
            cwd=context.repo_root,
        ),
        Step(
            label="docs-reference (the guide's generated tool reference)",
            argv=(
                context.python,
                "-m",
                "swcraft_mcp.tool_reference",
                "--output-dir",
                str(context.repo_root / Path(GUIDE_DIR)),
            ),
            cwd=context.repo_root,
        ),
        Step(
            label="docs-reference (the guide index of the agent profiles)",
            argv=(
                context.python,
                "-m",
                "swcraft_tools.guide_corpus",
                "--guide-dir",
                str(context.repo_root / Path(GUIDE_DIR)),
                "--mkdocs-config",
                str(context.repo_root / Path(MKDOCS_CONFIG)),
                "--profiles",
                *(str(context.repo_root / Path(profile)) for profile in GUIDE_INDEX_FILES),
            ),
            cwd=context.repo_root,
        ),
        _agents_md_step(context, "docs-reference"),
    ]


def plan_test_integration(context: Context) -> list[Step]:
    """The VS Code activation suite -- on demand **only** (network + display)."""
    return [_npm_step(context, "test-integration")]


@dataclass(frozen=True)
class Command:
    """One ``dev.py`` subcommand: what it does, and how to plan it; its name is its :data:`COMMANDS` key."""

    summary: str
    plan: Callable[[Context], list[Step]]
    #: False for ``install`` (it creates the venv), the aggregate starting with it, and ``update-dev-lock``.
    needs_venv: bool = True
    #: True only for :data:`NETWORK_COMMANDS`; an aggregate inherits (:func:`uses_network`).
    uses_network: bool = False
    #: Post-condition checked in-process after every step succeeded, raising :class:`DevError` when
    #: the command exited 0 without producing what it promised.
    verify: Callable[[Context], None] | None = None
    #: The commands this one aggregates, in order (``verify`` only); non-empty means no steps of its own.
    stages: tuple[str, ...] = ()


def plan_stages(context: Context, stages: Sequence[str]) -> list[Step]:
    """Concatenate other commands' plans, prefixing each step's label with its stage.

    Every stage is planned up front, so a precondition failure anywhere is reported before the
    first subprocess runs.
    """
    steps: list[Step] = []
    for name in stages:
        command = COMMANDS[name]
        steps.extend(replace(step, label=f"{name}: {step.label}") for step in command.plan(context))
    return steps


def verify_stage_postconditions(context: Context) -> None:
    """Run every aggregated stage's own post-condition, once the steps all passed.

    Without it ``verify`` would pass a ``build`` that generated nothing.
    """
    for name in VERIFY_STAGES:
        postcondition = COMMANDS[name].verify
        if postcondition is not None:
            postcondition(context)


def plan_verify(context: Context) -> list[Step]:
    """The aggregate gate: every stage that must pass before a change lands."""
    return plan_stages(context, VERIFY_STAGES)


def uses_network(name: str) -> bool:
    """Effective network use of a command; an aggregate inherits its stages'."""
    command = COMMANDS[name]
    if command.stages:
        return any(uses_network(stage) for stage in command.stages)
    return command.uses_network


COMMANDS: dict[str, Command] = {
    "install": Command(
        summary=(
            "create .venv, install the Python projects editable into it, stage the pinned elkjs payload, "
            "run npm ci (uses the network)"
        ),
        plan=plan_install,
        needs_venv=False,
        uses_network=True,
    ),
    "wheelhouse": Command(
        summary=(
            "populate dist/wheelhouse/ with the wheels and their third-party closure, resolved for the "
            "Python `runtime` unpacked (uses the network)"
        ),
        plan=plan_wheelhouse,
        uses_network=True,
    ),
    "runtime": Command(
        summary=(
            "populate dist/runtime/ with the pinned Node.js and CPython releases and the companion editor "
            "extensions the workstation archive carries, and restage the elkjs payload the wheel carries "
            "(uses the network; reads the committed pins, edits none)"
        ),
        plan=plan_runtime,
        uses_network=True,
        verify=verify_runtime_payload,
    ),
    "update-pins": Command(
        summary=(
            "rewrite scripts/workstation/pins/runtime-pins.json from the publishers: the Node.js and CPython "
            "digests, and the companion extensions at their newest Marketplace release (uses the network; "
            "a maintainer's command, not part of verify)"
        ),
        plan=plan_update_pins,
        uses_network=True,
    ),
    UPDATE_DEV_LOCK_COMMAND: Command(
        summary=(
            f"rewrite {DEV_LOCK}, the versions `install` pins, with `uv pip compile`, the uv version in its "
            "header (uses the network; needs uv on PATH; a maintainer's command, not part of verify)"
        ),
        plan=plan_update_dev_lock,
        needs_venv=False,
        uses_network=True,
    ),
    "lint": Command(
        summary=(
            "ruff and mypy over the four Python projects and scripts/, the import-graph gate and the "
            "quality ratchet, then Biome over the extension"
        ),
        plan=plan_lint,
    ),
    "test": Command(
        summary="the fast gate: pytest per Python project (staged markers deselected), then npm run test",
        plan=plan_test,
    ),
    "build": Command(
        summary=(
            "run the reference-model chain against examples/basic: check-model, generate arxml/diagrams/needs, validate"
        ),
        plan=plan_build,
        verify=verify_build_outputs,
    ),
    "artifacts": Command(
        summary=(
            "build the versioned local artifacts: dist/python wheels + sdists, dist/vscode .vsix, "
            "dist/docs guide archive, manifest"
        ),
        plan=plan_artifacts,
    ),
    "workstation-archive": Command(
        summary=(
            "compose dist/workstation/swcraft-<version>-<platform>.zip from dist/vscode, "
            "dist/wheelhouse, dist/runtime and scripts/workstation (downloads nothing)"
        ),
        plan=plan_workstation_archive,
        verify=verify_workstation_archive,
    ),
    "test-artifacts": Command(
        summary="run the `artifact`-marked suite (wheels, .vsix, venvs) -- the only stage that does",
        plan=plan_test_artifacts,
    ),
    "test-slow": Command(
        summary="run the `slow`-marked suite -- the only stage that does",
        plan=plan_test_slow,
    ),
    "test-large-bench": Command(
        summary="run the `large_bench`-marked functional scenarios on the large bench -- the only stage that does",
        plan=plan_test_large_bench,
    ),
    "test-real-models": Command(
        summary=(
            "run the `real_models`-marked suite: a calibration of the passport chain on the real exports "
            "`SWCRAFT_REAL_MODELS` names, report-only until the chain is green on every export, then "
            "blocking; skips when unset, FAILS with `--require-real-models` (not part of verify, not a "
            "bump step)"
        ),
        plan=plan_test_real_models,
    ),
    "test-model-integration": Command(
        summary="run the `integration`-marked standalone-architecture acceptance; FAILS when moon is unresolvable",
        plan=plan_test_model_integration,
    ),
    "test-xsd": Command(
        summary=(
            "validate both output variants against the licensed AUTOSAR 4.2.2 and 4.3.0 XSDs; "
            "FAILS (never skips) when a schema is unresolvable"
        ),
        plan=plan_test_xsd,
    ),
    TEST_INTERNAL_COMMAND: Command(
        summary=(
            "ruff over internal/, then pytest over internal/tests (or the PATHs given), serially: the work "
            "repository's record-consistency tests; FAILS where internal/ is absent, except in the open copy "
            f"(its {OPEN_COPY_MARKER} file), where it says the tests are not part of it (not part of verify)"
        ),
        plan=plan_test_internal,
    ),
    "coverage": Command(
        summary="report-only coverage per Python project (on demand, not part of verify)",
        plan=plan_coverage,
    ),
    "benchmark": Command(
        summary="report-only pytest-benchmark baseline suite for packages/tools/ (on demand, not part of verify)",
        plan=plan_benchmark,
    ),
    "package": Command(
        summary="package the VS Code extension into an installable .vsix",
        plan=plan_package,
    ),
    "schemas": Command(
        summary=(
            "regenerate the editor JSON Schemas (one per model layer + the manifest) and the reference table "
            f"references.json into {EDITOR_SCHEMAS_DIR}/, and the agent profiles' model schema section, "
            "then the project AGENTS.md rendered from the template profile"
        ),
        plan=plan_schemas,
    ),
    "docs": Command(
        summary=f"render the user guide ({GUIDE_DIR}/) into {DOCS_SITE_DIR}/ with mkdocs --strict",
        plan=plan_docs,
    ),
    "docs-reference": Command(
        summary=(
            f"regenerate the user guide's reference pages into {GUIDE_REFERENCE_DIR}/ "
            f"and the reference nav section in {MKDOCS_CONFIG}, then the agent profiles' guide index "
            "and the project AGENTS.md rendered from the template profile"
        ),
        plan=plan_docs_reference,
    ),
    "test-integration": Command(
        summary="run the VS Code activation suite (on demand only -- it downloads VS Code and needs a display)",
        plan=plan_test_integration,
    ),
    "verify": Command(
        summary=(
            "the full gate: install, lint, test, build, artifacts, runtime, wheelhouse, workstation-archive, "
            "test-artifacts, "
            "test-slow, test-large-bench, test-model-integration"
        ),
        plan=plan_verify,
        # `install`, its first stage, creates the environment, so a fresh checkout has none.
        needs_venv=False,
        verify=verify_stage_postconditions,
        stages=VERIFY_STAGES,
    ),
}


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def usage() -> str:
    """The message printed for a missing or unknown subcommand."""
    lines = [
        "Usage: python scripts/dev.py [--repo-root DIR] [--skip-node] [--require-rhapsody-export] "
        "[--require-second-export] [--require-real-models] [--maxfail N] [--unlocked] [--python EXE] <command> "
        "[PATH ...]",
        "",
        "Commands:",
    ]
    width = max(len(name) for name in COMMANDS)
    lines.extend(f"  {name.ljust(width)}  {command.summary}" for name, command in COMMANDS.items())
    lines.extend(
        [
            "",
            f"--skip-node covers {_skip_node_commands_phrase()}. It does not cover `artifacts` "
            "(the .vsix needs npm), so the full `verify` gate requires Node.js 22.",
            "",
            f"--require-rhapsody-export applies to `{TEST_INTERNAL_COMMAND}` alone and turns "
            "its machine-local gates on SWCRAFT_RHAPSODY_EXPORT -- the closed real-export suites' "
            "cases on that variable -- from a skip into a failure; pass it where that file really is.",
            "",
            f"--require-second-export does the same for `{TEST_INTERNAL_COMMAND}`'s gates on "
            "SWCRAFT_SECOND_EXPORT -- the closed suites' cases on that variable. The flags are "
            "independent: a host passes each one only where that variable's file really is.",
            "",
            f"--require-real-models applies to `{REQUIRE_REAL_MODELS_COMMAND}` alone and turns its skip on "
            "an unset SWCRAFT_REAL_MODELS -- the directory of real exports the calibration reads -- "
            "into a failure; a set variable naming no readable export fails with or without it.",
            "",
            f"--maxfail N applies to `{MAXFAIL_COMMAND}` alone and stops each of its pytest steps after N "
            "failures, N at least 1; it does not reach the extension's npm test.",
            "",
            f"--unlocked applies to `{UNLOCKED_COMMAND}` alone and installs without {DEV_LOCK} and the pin "
            f"of pip, then prints `pip freeze`; it refuses an existing {VENV_DIRNAME}. It is the canary "
            "at a version bump, dispatched through quick.yml's `unlocked` input.",
            "",
            f"--python EXE and PATH ... apply to `{TEST_INTERNAL_COMMAND}` alone: EXE is the interpreter it runs "
            "instead of .venv's, and each PATH a pytest path under internal/ instead of internal/tests.",
        ]
    )
    return "\n".join(lines)


def run_steps(steps: Sequence[Step], *, run: Runner = _run) -> int:
    """Run a plan in order, stopping at (and propagating) the first failure."""
    for step in steps:
        print(f"==> {step.label}", flush=True)
        returncode = run(step.argv, step.cwd, step.stdin, step.env)
        if returncode != 0:
            print(f"Error: stage `{step.label}` failed (exit {returncode})", file=sys.stderr)
            return returncode
    return 0


def build_context(
    repo_root: Path,
    *,
    command: Command,
    skip_node: bool = False,
    require_rhapsody_export: bool = False,
    require_second_export: bool = False,
    require_real_models: bool = False,
    maxfail: int | None = None,
    unlocked: bool = False,
    python: str | None = None,
    internal_paths: Sequence[str] = (),
    command_name: str | None = None,
    os_name: str = os.name,
) -> Context:
    """Resolve the environment for one command, failing before any step runs.

    ``command_name`` is *command*'s key in :data:`COMMANDS`; each command-specific flag (the export
    flags, ``--maxfail``, ``--unlocked``, ``--python``, PATHs) is refused on any other command. A
    caller naming no command gets no refusal, as the direct-call tests want.

    Raises:
        DevError: a flag on the wrong command, ``--maxfail`` below 1, a PATH outside ``internal/``,
            a ``--python`` that is not a file, or a missing venv the command needs.
    """
    # Taken before any local is bound: a row whose `attribute` no parameter answers is a `KeyError`
    # on the first call, never a silently skipped refusal.
    requested = dict(locals())
    for row in EXPORT_FLAG_COMMANDS:
        if requested[row.attribute] and command_name not in (None, row.command):
            raise DevError(
                f"{row.flag} applies to `{row.command}` and not to `{command_name}`: it turns that "
                f"stage's machine-local export gates from a skip into a failure, and no other "
                f"command runs them. Run `python scripts/dev.py {row.command} {row.flag}`."
            )
    if maxfail is not None:
        if command_name not in (None, MAXFAIL_COMMAND):
            raise DevError(
                f"--maxfail applies to `{MAXFAIL_COMMAND}` and not to `{command_name}`: it stops the fast "
                f"gate's pytest steps early, and no other command takes it. Run "
                f"`python scripts/dev.py {MAXFAIL_COMMAND} --maxfail {maxfail}`."
            )
        if maxfail < 1:
            raise DevError(f"--maxfail must be at least 1, not {maxfail}: pytest reads 0 as no limit at all.")
    if unlocked and command_name not in (None, UNLOCKED_COMMAND):
        raise DevError(
            f"--unlocked applies to `{UNLOCKED_COMMAND}` and not to `{command_name}`: it leaves the lock out "
            f"of a new environment, and no other command creates one. Run "
            f"`python scripts/dev.py {UNLOCKED_COMMAND} --unlocked` in a fresh checkout."
        )
    if (python is not None or internal_paths) and command_name not in (None, TEST_INTERNAL_COMMAND):
        named = "--python" if python is not None else "a PATH argument"
        raise DevError(
            f"{named} applies to `{TEST_INTERNAL_COMMAND}` and not to `{command_name}`: it picks the "
            "interpreter or the paths of the internal tests, and no other command takes it. Run "
            f"`python scripts/dev.py {TEST_INTERNAL_COMMAND} [--python EXE] [PATH ...]`."
        )
    internal_root = (repo_root / INTERNAL_DIR).resolve()
    for path in internal_paths:
        # The file part of a pytest node id (`a.py::TestX::test_y`), resolved for the comparison only.
        file_part = Path(path.split("::", 1)[0]).resolve()
        if not file_part.is_relative_to(internal_root):
            raise DevError(
                f"{path} is not under {INTERNAL_DIR}/: `{TEST_INTERNAL_COMMAND}` runs pytest in {INTERNAL_DIR}/ "
                f"under its conftest, which loads packages/tools/tests/conftest.py; a test of another project "
                f"would bring that project's conftest beside it and register its options twice. Run that test "
                f"with its own project's pytest."
            )
    if python is not None:
        if not Path(python).is_file():
            raise DevError(f"--python names {python}, which is not a file: name the interpreter to run the tests with.")
        # Absolute and never resolved: the steps run in another directory, and a venv's interpreter
        # is a symlink whose target lies outside the venv it belongs to.
        interpreter = str(Path(python).absolute())
    elif command.needs_venv:
        interpreter = str(require_venv_python(repo_root, os_name))
    else:
        interpreter = str(venv_python(repo_root, os_name))
    uv = resolve_uv()
    return Context(
        repo_root=repo_root,
        python=interpreter,
        npm=resolve_npm(),
        has_moon=moon_available(),
        skip_node=skip_node,
        require_rhapsody_export=require_rhapsody_export,
        require_second_export=require_second_export,
        require_real_models=require_real_models,
        maxfail=maxfail,
        unlocked=unlocked,
        dev_lock_missing=not dev_lock_path(repo_root).is_file(),
        uv=uv,
        uv_version=read_uv_version(uv) if uv is not None and command_name == UPDATE_DEV_LOCK_COMMAND else "",
        refused_pytest_environment=tuple(name for name in REAL_MODELS_REFUSED_ENVIRONMENT if os.environ.get(name)),
        real_models_relative=_is_relative_setting(os.environ.get(REAL_MODELS_VARIABLE, "")),
        specimen_generator_relative=_is_relative_setting(os.environ.get(SPECIMEN_GENERATOR_VARIABLE, "")),
        # Absolute and never resolved, like --python. Only the file part goes through Path: a parameter
        # id in the `::` suffix may hold a `/` that Path would turn into a backslash on Windows.
        internal_paths=tuple(_absolute_node_id(path) for path in internal_paths),
    )


def _absolute_node_id(node_id: str) -> str:
    """*node_id* with its file part made absolute (never resolved) and its ``::`` suffix kept verbatim."""
    file_part, separator, suffix = node_id.partition("::")
    return str(Path(file_part).absolute()) + separator + suffix


def _is_relative_setting(setting: str) -> bool:
    """Whether *setting* is a non-empty path that is not absolute (an empty one is unset)."""
    return bool(setting) and not Path(setting).is_absolute()


def main(argv: list[str] | None = None, *, run: Runner = _run) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--skip-node", action="store_true")
    parser.add_argument("--require-rhapsody-export", dest="require_rhapsody_export", action="store_true")
    parser.add_argument("--require-second-export", dest="require_second_export", action="store_true")
    parser.add_argument("--require-real-models", dest="require_real_models", action="store_true")
    parser.add_argument("--maxfail", type=int, default=None, metavar="N")
    parser.add_argument("--unlocked", action="store_true")
    parser.add_argument("--python", default=None, metavar="EXE")
    parser.add_argument("-h", "--help", action="store_true", dest="help")
    parser.add_argument("command", nargs="?")
    parser.add_argument("paths", nargs="*", metavar="PATH")
    # Intermixed, so an option may follow the command and the PATHs (`test-internal --python X a`):
    # plain parsing would hand `a` to no positional once `paths` had matched empty after the command.
    args = parser.parse_intermixed_args(argv)

    if args.help:
        print(usage())
        return 0
    if args.command is None:
        print(usage(), file=sys.stderr)
        return 2
    if args.command not in COMMANDS:
        print(f"Unknown command: {args.command}\n", file=sys.stderr)
        print(usage(), file=sys.stderr)
        return 2

    command = COMMANDS[args.command]
    repo_root = args.repo_root.resolve()
    try:
        # Before the context, which needs a .venv: an open copy's answer needs none.
        notice = (
            open_copy_notice(repo_root, export_required=args.require_rhapsody_export or args.require_second_export)
            if args.command == TEST_INTERNAL_COMMAND
            else None
        )
        if notice is not None:
            print(notice)
            return 0
        context = build_context(
            repo_root,
            command=command,
            skip_node=args.skip_node,
            require_rhapsody_export=args.require_rhapsody_export,
            require_second_export=args.require_second_export,
            require_real_models=args.require_real_models,
            maxfail=args.maxfail,
            unlocked=args.unlocked,
            python=args.python,
            internal_paths=args.paths,
            command_name=args.command,
        )
        steps = command.plan(context)
    except DevError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    returncode = run_steps(steps, run=run)
    if returncode != 0 or command.verify is None:
        return returncode

    try:
        command.verify(context)
    except DevError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
