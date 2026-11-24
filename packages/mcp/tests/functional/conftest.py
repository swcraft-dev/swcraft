"""The functional tree's fixtures: projects copied from the specimens, and the two surfaces.

- ``functional_project(name)`` -- the session's copy of the specimen *name*, taken from the cache
  and held to its pin; copied so no step touches the cache in place. For scenarios that only read.
- ``fresh_project(name)`` -- the test's own copy, for a scenario whose steps write into the project.
- ``large_bench_project`` -- the ``large-bench`` specimen, copied once per session and refused unless
  its ``model/`` digest is the pin's; asked for only by a large-bench case.
- ``pytest_runtest_makereport`` -- a ``real_models`` test's uncaught exception is reported by its
  class alone, since its text may hold a name of a private export.
- ``mcp(project)`` -- points the server at *project*; a ``cli`` step runs ``swcraft`` in its directory.
- ``guide(state)`` -- ``installed`` installs the real guide's corpus, ``absent`` none.

A project's passport is the specimen's committed source passport. No fixture here generates a model.
"""

from __future__ import annotations

import ast
import itertools
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import swcraft_mcp.server as _server
import swcraft_tools
from conftest import REPO_ROOT, copy_specimen, specimen_generator_module, specimen_passport
from guide_support import install_no_site, install_site, real_corpus, write_site
from swcraft_tools.copilot_guard import CODEX_HOOK_FILE
from swcraft_tools.passport import compute_passport

from tests.functional import real_models
from tests.functional.harness import LARGE_BENCH, FunctionalProject
from tests.functional.scenario_locator import ScenarioLocator

#: The template's Codex hook file, copied into every project: ``swcraft guard --host codex`` guards
#: only a directory holding both ``swcraft.yaml`` and this file.
TEMPLATE_CODEX_HOOK = (
    Path(swcraft_tools.__file__).resolve().parent / "templates" / "vehicle-architecture" / CODEX_HOOK_FILE
)


def dev_assignment(name: str) -> object:
    """The literal ``scripts/dev.py`` assigns to *name*, read by ``ast`` (the script is not imported)."""
    tree = ast.parse((REPO_ROOT / "scripts" / "dev.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and [getattr(target, "id", None) for target in node.targets] == [name]:
            return ast.literal_eval(node.value)
    raise AssertionError(f"scripts/dev.py assigns no {name}")


#: How a test (never the stage) starts a nested pytest: every 80 s every thread's stack goes to a
#: duplicate of fd 2 taken before ``--capture=fd`` replaces it, also at the session's end, which
#: pytest's ``faulthandler_timeout`` does not cover. A dump could name a real path, so only tests on
#: sentinel data use it.
_HANG_PROBE = (
    "import faulthandler, os, sys\n"
    "faulthandler.dump_traceback_later(80, repeat=True, file=os.fdopen(os.dup(2), 'w'))\n"
    "import pytest\n"
    "sys.exit(pytest.main(sys.argv[1:]))\n"
)


def diagnosed_pytest(pytest_args: list[str], basetemp: Path) -> list[str]:
    """The argv of a nested pytest of *pytest_args*, under the hang probe, in its own *basetemp*.

    ``--basetemp`` keeps the child off the shared ``pytest-of-<user>`` root, where on Windows its
    end-of-session cleanup hung beside the outer run.
    """
    return [sys.executable, "-c", _HANG_PROBE, *pytest_args, f"--basetemp={basetemp}"]


def run_diagnosed(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
    """*argv* run like the stage (stdin closed, output captured as UTF-8), a timeout a named failure.

    A child that outlives *timeout* fails the test with the tail of what it wrote, the hang
    probe's dump among it.
    """
    try:
        return subprocess.run(  # noqa: S603 - this interpreter, a test's argv
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as expired:

        def tail(stream: object) -> str:
            text = stream.decode("utf-8", "replace") if isinstance(stream, bytes) else (stream or "")
            return text[-6000:]

        pytest.fail(
            f"the nested run did not end within {timeout} s\n--- stdout ---\n{tail(expired.stdout)}"
            f"\n--- stderr ---\n{tail(expired.stderr)}",
            pytrace=False,
        )


def specimen_project(root: Path, name: str) -> FunctionalProject:
    """A copy of the specimen *name* at *root* (which must not exist), as a scenario's project.

    The template's Codex hook file is copied outside the model directory, so no passport leaf sees it.
    """
    copy_specimen(name, root)
    hook = root / CODEX_HOOK_FILE
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_bytes(TEMPLATE_CODEX_HOOK.read_bytes())
    result = compute_passport(root / "model")
    return FunctionalProject(
        root=root,
        manifest=root / "swcraft.yaml",
        passport=specimen_passport(name),
        locator=ScenarioLocator(result.locator, root / "model"),
    )


#: A module attribute, so a test can stand a small specimen in for the large bench.
LARGE_BENCH_SPECIMEN = LARGE_BENCH


def large_bench_project_at(target: Path) -> FunctionalProject:
    """The ``large-bench`` specimen copied to *target* (which must not exist), its pin checked before any step."""
    generator = specimen_generator_module()
    project = specimen_project(target, LARGE_BENCH_SPECIMEN)
    pinned = generator.load_pins(REPO_ROOT)["specimens"][LARGE_BENCH_SPECIMEN]["model_sha256"]
    found = generator.digest(target / "model")
    if found != pinned:
        pytest.fail(
            f"the large bench's digest is {found}, not the pin's model_sha256 {pinned}: the copy is not the "
            "pinned specimen, so a large-bench scenario would read another model than the one it was written on",
            pytrace=False,
        )
    return project


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> None:
    """A ``real_models`` test's uncaught exception is reported by its class alone (the real tier)."""
    real_models.withhold_exception_text(item, call)


@pytest.fixture(scope="session")
def functional_project(tmp_path_factory: pytest.TempPathFactory) -> Callable[[str], FunctionalProject]:
    cache: dict[str, FunctionalProject] = {}

    def make(name: str) -> FunctionalProject:
        if name not in cache:
            cache[name] = specimen_project(tmp_path_factory.mktemp("functional") / name, name)
        return cache[name]

    return make


@pytest.fixture(scope="session")
def large_bench_project(tmp_path_factory: pytest.TempPathFactory) -> FunctionalProject:
    return large_bench_project_at(tmp_path_factory.mktemp("large-bench") / LARGE_BENCH_SPECIMEN)


@pytest.fixture
def fresh_project(tmp_path: Path) -> Callable[[str], FunctionalProject]:
    made = itertools.count(1)

    def make(name: str) -> FunctionalProject:
        # <basetemp>/<test dir>/<specimen name>/, never deeper: the path budget's slack.
        index = next(made)
        return specimen_project(tmp_path / (name if index == 1 else f"{name}-{index}"), name)

    return make


@pytest.fixture
def mcp(monkeypatch: pytest.MonkeyPatch) -> Callable[[FunctionalProject], None]:
    def point_at(project: FunctionalProject) -> None:
        monkeypatch.setenv("SWCRAFT_MANIFEST", str(project.manifest))
        _server._PROJECT = None

    return point_at


@pytest.fixture(scope="session")
def installed_guide_site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The real guide's corpus in a site of its own, written once per session (``guide_support``)."""
    return write_site(tmp_path_factory.mktemp("installed-guide"), real_corpus())


@pytest.fixture
def guide(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Callable[[str], None]:
    """``guide(state)``: the scenario's ``guide`` -- ``installed`` installs the real guide, ``absent`` none."""

    def install(state: str) -> None:
        if state == "installed":
            install_site(monkeypatch, request.getfixturevalue("installed_guide_site"))
        else:
            install_no_site(monkeypatch, request.getfixturevalue("tmp_path"))

    return install
