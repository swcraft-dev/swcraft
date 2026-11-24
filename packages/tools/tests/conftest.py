"""Shared helpers for the packages/tools/ test suite."""

from __future__ import annotations

import argparse
import ast
import contextlib
import dataclasses
import functools
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from types import ModuleType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from pythonpath_bootstrap import absent_from_sys_path, declared_pythonpath

# A test module run as a script (``test_diagram_generation.py --freeze``) gets no pytest configuration,
# so the declared helper directories ``sys.path`` lacks are appended here. Under pytest every root
# already declares them and nothing is appended; no helper is named like an importable module, so
# appending changes no import.
sys.path.extend(absent_from_sys_path(declared_pythonpath(Path(__file__).resolve().parents[1])))

import node_support
import pytest
import scale_model
import swcraft_tools
import xsd_support
import yaml
from copilot_guard_support import (
    UNREACHABLE_JUDGE_SECONDS,
    write_claude_settings,
    write_codex_hook,
    write_kiro_hooks,
)
from pydantic import BaseModel
from scratch_git import git as _git
from single_read_support import reads, single_read_tripwire  # noqa: F401 - fixtures, found by name
from swcraft_tools import copilot_guard, guard_model_paths, guide_site, writer
from swcraft_tools.differ import _model_members
from swcraft_tools.model_registry import (
    LayerFacts,
    ModelRegistry,
    current_registry,
    longest_directory_match,
    production_registry,
)
from swcraft_tools.model_state import ModelState
from swcraft_tools.reader import find_swc_file
from swcraft_tools.resource_analysis import Finding
from swcraft_tools.snapshot import ModelSnapshot

REPO_ROOT = Path(__file__).resolve().parents[3]

#: This file: the mandatory-acceptance modules scan it for skips too (:func:`no_skip_offenders`).
CONFTEST_PATH = Path(__file__).resolve()

#: The ``vehicle-architecture`` template inside the package, the path ``importlib.resources`` resolves.
TEMPLATE_ROOT = Path(swcraft_tools.__file__).resolve().parent / "templates" / "vehicle-architecture"


@pytest.fixture(autouse=True)
def _a_fresh_guard_run(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make each guard decision a hook run of its own, as in use.

    The run's bound and model entry index are reset before every :func:`swcraft_tools.copilot_guard.decide` and around
    the test, so many decisions on a slow host never share one run's bound and no patched index or bound outlives the
    test."""
    decide = copilot_guard.decide

    @functools.wraps(decide)
    def a_run_each(*args: Any, **kwargs: Any) -> copilot_guard.Decision:
        guard_model_paths.new_run()
        return decide(*args, **kwargs)

    monkeypatch.setattr(copilot_guard, "decide", a_run_each)
    guard_model_paths.new_run()
    yield
    guard_model_paths.new_run()


#: The smallest limit ``sys.set_int_max_str_digits()`` accepts besides 0, read from the interpreter
#: rather than written as 640. Cases PIN the limit at it rather than read it live: a case deriving both
#: its input and its expected message from one live call would pass under every configuration.
DIGIT_LIMIT_FLOOR = sys.int_info.str_digits_check_threshold


@contextlib.contextmanager
def at_most_steps(limit: int = 1_000_000):
    """Fail a block that makes more than *limit* Python and C calls: a bound on a walk, with no clock.

    A clock is a flake on a loaded runner. ``pytest.fail`` is a ``BaseException``, so the ``except
    Exception`` of ``edited_text`` cannot take it for a refusal. What this does not reach: a spin
    inside a C function makes no call to count, so it stays the per-test timeout's to end.
    """
    steps = 0
    previous = sys.getprofile()

    def count(_frame: Any, _event: str, _arg: Any) -> None:
        nonlocal steps
        steps += 1
        if steps > limit:
            sys.setprofile(previous)
            pytest.fail(f"the walk made more than {limit} calls")

    sys.setprofile(count)
    try:
        yield
    finally:
        sys.setprofile(previous)


@contextlib.contextmanager
def int_max_str_digits(limit: int):
    """Run the block with CPython's integer-string limit set to *limit*.

    Process-global state, so the previous value is restored on the way out, failure included.
    """
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(limit)
    try:
        yield
    finally:
        sys.set_int_max_str_digits(previous)


# ---------------------------------------------------------------------------
# The layer vocabulary, read off the kernel registry: each helper delegates to
# `production_registry()`, so a new layer, a renamed directory or a second
# nesting layer reaches every fixture with no edit here.
# ---------------------------------------------------------------------------


def layer_directories(*, required_only: bool = False) -> tuple[str, ...]:
    """Every declared layer's storage directory, in contract order.

    ``required_only`` drops the optional layers -- today none; kept because optionality is a contract
    fact a later layer may carry again.
    """
    return tuple(
        facts.storage.directory for facts in production_registry().layers if not (required_only and facts.optional)
    )


def arxml_layer_directories() -> tuple[str, ...]:
    """The directories ``generate_arxml()`` reads, by ``layerDecl.arxmlBound``."""
    return tuple(facts.storage.directory for facts in production_registry().layers if facts.arxml_bound)


def layer_for_directory(directory: str, registry: ModelRegistry) -> LayerFacts:
    """The one layer of *registry* stored under *directory*; an unknown directory is a test defect."""
    (layer,) = [layer for layer in registry.layers if layer.storage.directory == directory]
    return layer


def layer_for_path(relative_path: str, registry: ModelRegistry) -> LayerFacts:
    """The layer of *registry* whose directory holds the model-root-relative *relative_path*.

    The longest declared directory that prefixes the path (:func:`longest_directory_match`): a layer's
    directory may have two segments, so a first segment alone does not name the layer. A path no
    directory holds is a test defect.
    """
    match = longest_directory_match((layer.storage.directory for layer in registry.layers), relative_path)
    assert match is not None, f"no layer directory holds {relative_path!r}"
    return layer_for_directory(match[0], registry)


def element_path(model_root: Path | str, layer: str, identity: str, **placement: str) -> Path:
    """Where one element of *layer* is stored, composed by the registry.

    ``placement`` carries the nesting layer's directory component --
    ``element_path(root, "swc", "FooSWC", group="sensors")``. A flat layer takes
    none, and a nesting layer with none raises rather than composing a path the
    reader would not find.
    """
    return Path(model_root) / production_registry().compose_path(layer, identity, placement or None)


# ---------------------------------------------------------------------------
# One element file, its directory derived from the registry by the payload's
# `kind` (every kind belongs to one layer) or an explicit `layer`.
#
# The same three definitions live in `packages/mcp/tests/conftest.py` and
# `packages/agent/tests/conftest.py` (no suite can import another's conftest);
# `test_model_element_helpers.py` holds the copies equal.
#
# What they do not reach: a file whose name is not its element's storage name
# is still composed from the stem the caller passes, a malformed file without a
# `kind` cannot be routed by kind, and a test whose subject is a directory itself
# spells it on purpose.
# ---------------------------------------------------------------------------


def model_element_storage_name(name: str, *, kind: str | None = None, layer: str | None = None) -> str:
    """The model-root-relative file of element *name*, in the layer of *kind* or in *layer*.

    Exactly one of the two is named: *kind* is a payload's ``kind`` value, which
    the registry answers with the layer that declares it; *layer* names the
    layer directly. *name* is the file's stem -- the element's storage name, or
    any other stem a test means to write -- and is not validated.
    """
    if (kind is None) == (layer is None):
        raise TypeError("name exactly one of kind= and layer=")
    registry = current_registry()
    if layer is None:
        if kind not in registry.kinds:
            raise ValueError(f"no layer declares the kind {kind!r}")
        layer = registry.kind(kind).layer
    return registry.compose_path(layer, name)


def model_element_path(model_root: Path | str, name: str, *, kind: str | None = None, layer: str | None = None) -> Path:
    """The file of element *name* under *model_root*: :func:`model_element_storage_name` joined to the root."""
    return Path(model_root) / model_element_storage_name(name, kind=kind, layer=layer)


def write_model_element(model_root: Path | str, payload: dict, *, layer: str | None = None) -> Path:
    """Write *payload* as the YAML file of its element and return the file's path.

    The stem is the payload's ``name``; the layer is *layer* when it is named,
    else the one that declares the payload's ``kind``. Missing directories are
    created, and the document is dumped in the payload's own key order.
    """
    if layer is None:
        path = model_element_path(model_root, payload["name"], kind=payload["kind"])
    else:
        path = model_element_path(model_root, payload["name"], layer=layer)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(dict(payload), sort_keys=False, allow_unicode=True), encoding="utf-8", newline="\n")
    return path


# ---------------------------------------------------------------------------
# The differ helpers shared by `test_differ.py`, `test_differ_identity.py` and
# `test_differ_coverage.py`: the dumped form the differ compares and the shape
# `schema.py` declares, one definition each so the suites cannot disagree.
# ---------------------------------------------------------------------------


def _dump(model: BaseModel) -> Any:
    """The form the differ reports an item in -- what ``_diff_element`` compares."""
    return model.model_dump(by_alias=True)


def _list_or_mapping_backed(annotation: Any) -> bool:
    """Whether one annotation really declares a ``list`` or a ``dict``.

    ``Annotated`` and an optional union are unwrapped first, because
    ``get_origin(list[X] | None)`` is ``types.UnionType``: reading that as "not a
    collection" would classify an optional collection as a scalar.
    ``test_differ_coverage._container_kind`` widens this to count a nested model as a mapping.
    """
    if hasattr(annotation, "__metadata__"):
        annotation = annotation.__origin__
    origin = get_origin(annotation)
    if origin in (list, dict):
        return True
    if origin in (Union, UnionType):
        return any(_list_or_mapping_backed(member) for member in get_args(annotation) if member is not type(None))
    return False


def _layer_element_classes() -> tuple[type[BaseModel], ...]:
    """The element classes of ``ModelSnapshot``'s fields, deduplicated.

    The production ``differ._model_members`` does the unwrapping, so the schema walks seeded from this
    agree with the one the differ recurses through.
    """
    hints = get_type_hints(ModelSnapshot)
    roots: list[type[BaseModel]] = []
    for layer in dataclasses.fields(ModelSnapshot):
        roots.extend(member for member in _model_members(hints[layer.name]) if member not in roots)
    return tuple(roots)


# ---------------------------------------------------------------------------
# Repo-root script loading. `scripts/` is not an installable package, so its
# modules are imported by path, and two details are easy to get wrong:
#
#  * the module must be registered in ``sys.modules`` **before** ``exec_module``
#    -- ``dev.py``'s dataclasses resolve their annotations through
#    ``sys.modules[cls.__module__]``, which a bare ``exec_module`` leaves unset;
#  * one script must yield **one** module object across the whole suite,
#    otherwise ``isinstance`` checks and ``monkeypatch.setattr(_DEV, ...)``
#    silently stop meaning the same thing in two different test files.
# ---------------------------------------------------------------------------

DEV_SCRIPT_MODULE = "swcraft_dev_script"
BUILD_ARTIFACTS_MODULE = "swcraft_build_artifacts"
DOWNLOAD_SCHEMA_MODULE = "swcraft_download_autosar_schema"
MEASURE_MCP_SCALE_MODULE = "swcraft_measure_mcp_scale"
MEASURE_FORMS_MODULE = "swcraft_measure_forms"
AGENT_TASK_CHECK_MODULE = "swcraft_agent_task_check"
AGENT_TASK_BIND_MODULE = "swcraft_agent_task_bind"
WORKSTATION_INSTALL_MODULE = "swcraft_workstation_install"
PUBLIC_SNAPSHOT_MODULE = "swcraft_public_snapshot"
QUALITY_RATCHET_MODULE = "swcraft_quality_ratchet"
HISTORY_MARKERS_MODULE = "swcraft_history_markers"
#: The name ``specimens.py`` and ``agent_task_bench.py`` load ``scripts/specimen_generator.py`` by, so
#: they and the test trees share one module object and one per-process specimen memo.
SPECIMEN_GENERATOR_MODULE = "swcraft_specimen_generator"
#: ``scripts/specimens.py``'s name: never ``swcraft_specimens``, the generator's package in ``.venv``,
#: which this name would shadow.
SPECIMENS_MODULE = "swcraft_specimens_pin_script"
#: Unprefixed, unlike its neighbours: the name every caller already loads the script by.
AGENT_TASK_BENCH_MODULE = "agent_task_bench"


def load_repo_script(name: str, relative: str) -> ModuleType:
    """Import a repo-root script by path, once per interpreter."""
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def dev_module() -> ModuleType:
    """``scripts/dev.py`` -- the native build contour."""
    return load_repo_script(DEV_SCRIPT_MODULE, "scripts/dev.py")


def quality_ratchet_module() -> ModuleType:
    """``scripts/quality_ratchet.py`` -- ``lint``'s quality ratchets."""
    return load_repo_script(QUALITY_RATCHET_MODULE, "scripts/quality_ratchet.py")


def build_artifacts_module() -> ModuleType:
    """``scripts/build_artifacts.py`` -- the local artifact builder."""
    return load_repo_script(BUILD_ARTIFACTS_MODULE, "scripts/build_artifacts.py")


def workstation_install_module() -> ModuleType:
    """``scripts/workstation/install.py`` -- the workstation archive's installer.

    The installer is shipped by copying into the archive, never installed, so it has no import path.
    """
    return load_repo_script(WORKSTATION_INSTALL_MODULE, "scripts/workstation/install.py")


def download_autosar_schema_module() -> ModuleType:
    """``scripts/download_autosar_schema.py`` -- the optional licensed-XSD fetch."""
    return load_repo_script(DOWNLOAD_SCHEMA_MODULE, "scripts/download_autosar_schema.py")


def public_snapshot_module() -> ModuleType:
    """``scripts/public_snapshot.py`` -- the open copy's assembler and its check."""
    return load_repo_script(PUBLIC_SNAPSHOT_MODULE, "scripts/public_snapshot.py")


def history_markers_module() -> ModuleType:
    """``scripts/history_markers.py`` -- the gate on development-record markers in the public files.

    The script imports ``public_snapshot`` under that name, as Python resolves it beside a script it
    runs; the loaded snapshot module is registered under the name first, so both share one object.
    """
    sys.modules.setdefault("public_snapshot", public_snapshot_module())
    return load_repo_script(HISTORY_MARKERS_MODULE, "scripts/history_markers.py")


def agent_task_check_module() -> ModuleType:
    """``scripts/agent_task_check.py`` -- the agent task benchmark's run checker."""
    return load_repo_script(AGENT_TASK_CHECK_MODULE, "scripts/agent_task_check.py")


def agent_task_bind_module() -> ModuleType:
    """``scripts/agent_task_bind.py`` -- the agent task benchmark's any-model set, bound to one model."""
    return load_repo_script(AGENT_TASK_BIND_MODULE, "scripts/agent_task_bind.py")


#: The agent task benchmark's sets: the small set's ``tasks/`` and ``bench/``, and the large set's ``large/``.
AGENT_BENCH = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "agent-bench"

#: The agent task benchmark's small bench: seven components, six of them run by a partition. Read in
#: place; a test that writes copies it.
AGENT_TASK_BENCH = AGENT_BENCH / "bench"

#: The small bench's manifest, the ``--project`` / ``project=`` argument a test reading it in place passes.
AGENT_TASK_BENCH_MANIFEST = AGENT_TASK_BENCH / "swcraft.yaml"


def copy_agent_task_bench(directory: Path) -> Path:
    """A copy of :data:`AGENT_TASK_BENCH` at ``directory / "bench"``, its project root returned."""
    target = directory / "bench"
    shutil.copytree(AGENT_TASK_BENCH, target)
    return target


@dataclasses.dataclass(frozen=True)
class StateSnapshot:
    """Anything that answers ``.state`` is a snapshot to ``resolve`` and ``read_answer``.

    Deliberately not ``ModelSnapshot``: ``test_generic_core_gate.py`` counts that adapter's importers,
    and both functions read only the generic kernel state.
    """

    state: ModelState


def measure_mcp_scale_module() -> ModuleType:
    """``scripts/measure_mcp_scale.py`` -- the kept MCP scale measurement oracle.

    Its callers live in ``packages/mcp/tests/``, which imports this file as the top-level ``conftest``;
    loading it here keeps one module object per script.
    """
    return load_repo_script(MEASURE_MCP_SCALE_MODULE, "scripts/measure_mcp_scale.py")


def measure_forms_module() -> ModuleType:
    """``scripts/measure_forms.py`` -- the recipe forms and reference calls, figures only.

    Its callers live in ``packages/mcp/tests/`` (the script drives an MCP client), loaded from here
    for the reason :func:`measure_mcp_scale_module` gives: one module object per script.
    """
    return load_repo_script(MEASURE_FORMS_MODULE, "scripts/measure_forms.py")


def specimen_generator_module() -> ModuleType:
    """``scripts/specimen_generator.py`` -- the one place a specimen generator is started."""
    return load_repo_script(SPECIMEN_GENERATOR_MODULE, "scripts/specimen_generator.py")


def specimens_module() -> ModuleType:
    """``scripts/specimens.py`` -- ``pin`` and the leaf comparison ``passport_support.py`` names."""
    return load_repo_script(SPECIMENS_MODULE, "scripts/specimens.py")


def agent_task_bench_module() -> ModuleType:
    """``scripts/agent_task_bench.py`` -- the agent task benchmark, its manifest and its large bench.

    Under the name every caller loads it by (``agent_task_bench``), so all share one module object.
    """
    return load_repo_script(AGENT_TASK_BENCH_MODULE, "scripts/agent_task_bench.py")


def dev_context(repo_root: Path, **overrides):
    """A ``dev.py`` ``Context`` with fake interpreter/npm/moon values, built without touching the filesystem."""
    dev = dev_module()
    defaults = {
        "repo_root": repo_root,
        "python": str(dev.venv_python(repo_root, "posix")),
        "bootstrap_python": "/usr/bin/python3",
        "npm": "/usr/local/bin/npm",
        "has_moon": True,
        "skip_node": False,
        "uv": "/usr/local/bin/uv",
        "uv_version": "uv 0.0.0",
    }
    defaults.update(overrides)
    return dev.Context(**defaults)


# ---------------------------------------------------------------------------
# The installed guide's locator, shared by test_guide_site.py and test_cli.py:
# one copy of the three-step neutralisation, so a change to the search order
# reaches both suites.
# ---------------------------------------------------------------------------


def make_guide_site(directory: Path) -> Path:
    """A directory that counts as a site: it exists and it holds an ``index.html``."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / guide_site.SITE_INDEX_NAME).write_text(
        "<html><body>guide</body></html>", encoding="utf-8", newline="\n"
    )
    return directory


def neutralise_guide_site_search(monkeypatch, tmp_path: Path) -> Path:
    """Point all three of :func:`~swcraft_tools.guide_site.site_root`'s steps at empty places.

    The search reads real process state -- an environment variable, the installed
    package, ``sys.executable`` -- so without this a suite answers differently on a
    machine where the guide happens to be rendered into the checkout. Returns
    *tmp_path*, so a caller can then furnish exactly the one step it is about.

    The two-step ``delenv`` is load-bearing: ``delenv(..., raising=False)`` on an absent key records no
    undo entry, so a test that then writes the key (``load_env_file()`` writes ``os.environ``) would
    leave it set for the session. ``setenv`` first always records one, so teardown restores the key.

    What this does not reach: it neutralises this one variable. Nothing here
    stops a test from setting some *other* variable the locator does not read,
    and no assertion in this file notices if one does.
    """
    monkeypatch.setenv(guide_site.GUIDE_SITE_ENV, "")
    monkeypatch.delenv(guide_site.GUIDE_SITE_ENV)

    package_root = tmp_path / "site-packages" / "swcraft_tools"
    package_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(guide_site, "resource_files", lambda package: package_root)

    interpreter = tmp_path / "venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text("", encoding="utf-8", newline="\n")
    monkeypatch.setattr("sys.executable", str(interpreter))

    return tmp_path


def make_venv_marker(repo_root: Path) -> Path:
    """Materialize just enough of a ``.venv`` for ``require_venv_python`` to pass."""
    interpreter = dev_module().venv_python(repo_root)
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text("", encoding="utf-8", newline="\n")
    return interpreter


# ---------------------------------------------------------------------------
# Subprocess, parser and ast helpers.
# ---------------------------------------------------------------------------


def run_captured(
    argv: Iterable[object],
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Run *argv* with output captured, never raising on a non-zero exit.

    *timeout* is opt-in: callers run real builds, and a default ceiling would turn a slow host red. A
    caller running something that could fail to return passes one; ``subprocess.TimeoutExpired`` is
    then the failure.
    """
    return subprocess.run(
        [str(part) for part in argv],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        encoding="utf-8",
    )


def output_text(completed: subprocess.CompletedProcess) -> str:
    """Both streams, for a message assertion that does not care which one carried it."""
    return f"{completed.stdout}\n{completed.stderr}"


def need(requirement_id: str, **fields: Any) -> dict[str, Any]:
    """A requirements interchange need with the three required texts defaulted from its id."""
    return {
        "id": requirement_id,
        "title": fields.pop("title", f"Title of {requirement_id}"),
        "content": fields.pop("content", f"Text of {requirement_id}."),
        "type": fields.pop("type", "req"),
        **fields,
    }


def needs_document(*needs: dict[str, Any], baseline: str = "SRS-1") -> dict[str, Any]:
    """A requirements interchange document whose one version, *baseline*, holds *needs* and is current."""
    return {"current_version": baseline, "versions": {baseline: {"needs": {item["id"]: item for item in needs}}}}


def parser_option_strings(parser: argparse.ArgumentParser) -> frozenset[str]:
    """Every option string *parser* itself accepts, subcommands excluded.

    The whole surface of a parser with no subcommand, such as ``scripts/workstation/install.py``'s.
    """
    return frozenset(option for action in parser._actions for option in action.option_strings)


def parser_positional_dests(parser: argparse.ArgumentParser) -> list[str]:
    """*parser*'s positional arguments, in declaration order, by ``dest``.

    The other half of a command's surface beside :func:`parser_option_strings`.
    """
    return [action.dest for action in parser._actions if not action.option_strings]


def subcommand_parser(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
    """*parser*'s sub-parser for the ``name`` subcommand.

    For a test that needs the sub-parser itself, e.g. its positionals, which
    :func:`parser_subcommand_options` does not carry.

    Raises:
        KeyError: naming the subcommand when the parser has no such subcommand.
    """
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            if name not in action.choices:
                raise KeyError(f"{name!r} is not a subcommand of this parser: {sorted(action.choices)}")
            return action.choices[name]
    raise KeyError("this parser declares no subcommands at all")


def parser_subcommand_options(parser: argparse.ArgumentParser) -> dict[str, frozenset[str]]:
    """Subcommand -> the option strings it accepts (top-level options included).

    The four parser helpers here are the one reach into argparse's private ``_actions`` API. Not
    reached: ``test_guide_reference_cli.py`` walks ``_actions`` itself on purpose, since an oracle
    calling the generator's own helper would agree with it by construction.
    """
    top = parser_option_strings(parser)
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return {
                name: frozenset(top | {option for sub_action in sub._actions for option in sub_action.option_strings})
                for name, sub in action.choices.items()
            }
    return {}


def pytest_addoption(parser) -> None:
    """Register ``--require-autosar-xsd`` and ``--skip-node``.

    Here because pytest reads ``pytest_addoption`` only from an initial conftest; the skip/fail decision
    lives in ``xsd_support.py`` and ``node_support.py``, because :func:`no_skip_offenders` scans this
    file. The export flags are registered by the conftest of the suites on the machine-local exports.
    """
    parser.addoption(
        "--require-autosar-xsd",
        action="store_true",
        default=False,
        help=(
            "Fail, rather than skip, when a licensed AUTOSAR XSD is missing. "
            "Used by `python scripts/dev.py test-xsd`, the release gate for XSD conformance."
        ),
    )
    parser.addoption(
        "--skip-node",
        action="store_true",
        default=False,
        help=(
            "Skip, rather than fail, the diagram layout tests when no Node.js resolves. "
            "Passed by `python scripts/dev.py --skip-node test`; see node_support.py."
        ),
    )


def pytest_configure(config) -> None:
    xsd_support.set_require_licensed_xsd(bool(config.getoption("--require-autosar-xsd")))
    node_support.set_skip_node(bool(config.getoption("--skip-node")))


def pinned_specimen_names() -> tuple[str, ...]:
    """Every specimen the pin file pins: the ones this project reads (``test_specimens.py`` takes all)."""
    pins = specimen_generator_module().load_pins(REPO_ROOT)
    return tuple(sorted(pins["specimens"]))


def take_specimen(name: str) -> Path:
    """The cache directory of the specimen *name*, held to its pin; a test copies it, never writes it."""
    return specimen_generator_module().take(name, repo_root=REPO_ROOT)


def copy_specimen(name: str, target: Path) -> Path:
    """A copy of the specimen *name* (``swcraft.yaml`` and ``model/``) at *target*, which must not exist.

    The way a test reads a specimen: the copy is the test's to write; the cache is never read in place.
    """
    shutil.copytree(take_specimen(name), target)
    return target


def specimen_passport(name: str) -> dict[str, Any]:
    """The committed source passport of the specimen *name* (``fixtures/specimens/<name>.passport.json``)."""
    path = specimen_generator_module().passport_path(REPO_ROOT, name)
    return json.loads(path.read_text(encoding="utf-8"))


def warm_specimens(session, names: Iterable[str]) -> None:
    """Take every specimen of *names* once, in the controller, before any xdist worker starts.

    A cold cache is generated here sequentially, so a worker only digests. Any failure is left to the
    first test that takes the specimen (a host without the generator fails there, never skips): an
    exception here would end the run as an internal error and hide which tests read specimens.
    """
    if hasattr(session.config, "workerinput"):
        return
    generator = specimen_generator_module()
    for name in names:
        try:
            take_specimen(name)
        except generator.GeneratorNotConfigured:
            return
        except Exception:  # noqa: BLE001 -- raised again by the first test that takes the specimen
            continue


def pytest_sessionstart(session) -> None:
    """Warm the specimen cache once per pytest run: every pinned specimen (:func:`warm_specimens`)."""
    warm_specimens(session, pinned_specimen_names())


#: ``pytest.<attr>`` calls that would turn a mandatory acceptance into an optional one.
SKIP_ATTRIBUTES = frozenset({"skip", "skipif", "importorskip"})


def no_skip_offenders(path: Path) -> list[str]:
    """Every ``pytest.skip``/``skipif``/``importorskip`` in the module at *path*.

    A mandatory acceptance may not contain one: on a machine lacking a prerequisite the proof would
    silently become optional.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        f"{node.attr} (line {node.lineno})"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in SKIP_ATTRIBUTES
        and isinstance(node.value, ast.Name)
        and node.value.id == "pytest"
    ]


# ---------------------------------------------------------------------------
# The standalone architecture project: scaffold, seed and build a real template
# instance outside the checkout, against the workstation archive installed
# beside it. Shared by test_template_acceptance.py and test_reproducibility.py.
# ---------------------------------------------------------------------------

#: The offline install source. Populated by ``python scripts/dev.py wheelhouse``
#: (a provisioning step, network allowed) and consumed here with ``--no-index``
#: (an assertion, offline always).
WHEELHOUSE = REPO_ROOT / "dist" / "wheelhouse"

#: The name given to the scaffolded project; also the manifest's ``name``.
PROJECT_NAME = "vehicle-architecture"


def require_moon() -> str:
    """The ``moon`` executable, or a hard failure naming what to install."""
    moon = shutil.which("moon")
    if moon is None:
        pytest.fail(
            "moon was not found on PATH -- the standalone-architecture integration acceptance "
            "cannot run, and it must not be skipped. Install moon (see the template's .prototools) "
            "and re-run `python scripts/dev.py test-model-integration`."
        )
    return moon


def require_wheelhouse() -> Path:
    """The local wheelhouse, or a hard failure naming the command that fills it."""
    wheels = sorted(WHEELHOUSE.glob("swcraft_*.whl"), key=lambda path: path.as_posix()) if WHEELHOUSE.is_dir() else []
    # One wheel per SWCraft distribution, the specimen generator's among them (the installer requires it).
    expected = len(build_artifacts_module().SWCRAFT_DISTRIBUTIONS)
    if len(wheels) < expected:
        pytest.fail(
            f"{WHEELHOUSE} does not hold the {expected} SWCraft wheels (found {len(wheels)}). "
            "This acceptance installs offline from it. Run `python scripts/dev.py artifacts`, "
            "`python scripts/dev.py runtime` and `python scripts/dev.py wheelhouse`, in that order, "
            "or run the whole gate with `python scripts/dev.py verify`."
        )
    return WHEELHOUSE


#: Where ``python scripts/dev.py workstation-archive`` writes the archive (provisioning, like the wheelhouse).
WORKSTATION_DIR = REPO_ROOT / "dist" / "workstation"


def require_workstation_archive() -> Path:
    """The workstation archive for **this** version and platform, or a hard failure.

    Fails naming the command that builds one rather than skip: a skipped packaging acceptance reports
    the opposite of what it checked. Resolved by exact name, the builder's own derivation of version
    and platform token, never "the newest zip": a switched checkout can leave a foreign archive.

    What it does **not** reach: it stats a filename. Nothing here opens the zip, so
    an archive of the right name built from stale inputs is accepted; the staleness
    of the *inputs* is `dev.py verify`'s stage order, and the archive's own
    ``SHA256SUMS`` is checked by the installer that unpacks it.
    """
    artifacts = build_artifacts_module()
    version = artifacts.read_version(REPO_ROOT)
    token = artifacts.workstation_platform_token()
    expected = artifacts.workstation_archive_filename(version, token)
    archive = WORKSTATION_DIR / expected
    if not archive.is_file():
        present = (
            sorted(path.name for path in WORKSTATION_DIR.iterdir() if path.is_file())
            if WORKSTATION_DIR.is_dir()
            else []
        )
        pytest.fail(
            f"{WORKSTATION_DIR} does not hold {expected} (found: {', '.join(present) or 'nothing'}). "
            "This acceptance installs that archive into a temporary home. Run "
            "`python scripts/dev.py workstation-archive` (it needs `artifacts`, `runtime` and "
            "`wheelhouse` first), or run the whole gate with `python scripts/dev.py verify`."
        )
    return archive


#: The platform tag the hermetic workstation cases are built against, pinned rather than read off the
#: host: ``workstation_platform_token()`` refuses a tag outside the three the archive is built for, so
#: these fast-gate cases about the builder would error on any other host.
MAPPED_BUILD_TAG = "macosx-11.0-arm64"


def pin_build_platform(monkeypatch: pytest.MonkeyPatch) -> str:
    """Pin ``sysconfig.get_platform()`` to :data:`MAPPED_BUILD_TAG`; return its token.

    A plain function too, because a helper method cannot request the :func:`mapped_build_host` fixture.

    What it does **not** reach: it makes the *token* deterministic and nothing
    else. Whether this host could actually run the wheels an archive carries is
    ``check_wheelhouse_platform``'s question and the installer's.
    """
    monkeypatch.setattr(sysconfig, "get_platform", lambda: MAPPED_BUILD_TAG)
    return build_artifacts_module().workstation_platform_token(MAPPED_BUILD_TAG)


@pytest.fixture
def mapped_build_host(monkeypatch: pytest.MonkeyPatch) -> str:
    """:func:`pin_build_platform` as a fixture; the token the pinned tag maps to."""
    return pin_build_platform(monkeypatch)


#: The directory the workstation archive is installed into, a **sibling** of the scaffolded project:
#: an architecture project holds no toolchain
#: (``test_template_contents.py::test_no_template_file_puts_a_toolchain_back_inside_the_project``).
#: Derived twice, off a project (:func:`toolchain_home`) and off the session base directory
#: (:func:`archive_install`); :func:`architecture_project` asserts the two agree.
TOOLCHAIN_HOME_DIRNAME = "swcraft-home"


def installed_bin(home: Path) -> Path:
    """``<home>/current/bin`` -- ``<home>\\current\\Scripts`` on Windows.

    The names come from ``scripts/workstation/install.py``, which defines the layout.
    """
    install = workstation_install_module()
    return home / install.CURRENT_LINK_NAME / install.bin_dirname(os.name)


def installed_executable(home: Path, name: str) -> Path:
    """``<home>/current/bin/<name>``, with Windows' ``.exe``."""
    return installed_bin(home) / (name + (".exe" if os.name == "nt" else ""))


def toolchain_home(project: Path) -> Path:
    """The installation the *project*'s build runs against."""
    return project.parent / TOOLCHAIN_HOME_DIRNAME


def toolchain_bin(project: Path) -> Path:
    """The directory :func:`build_environment` puts first on the build's ``PATH``."""
    return installed_bin(toolchain_home(project))


def toolchain_python(project: Path) -> Path:
    """The installation's own interpreter -- the one the console scripts run on."""
    return installed_executable(toolchain_home(project), "python")


def build_environment(project: Path, env: dict[str, str] | None = None) -> dict[str, str]:
    """*env* (default the current process') with the installation first on ``PATH``.

    Prepended and de-duplicated: this installation is *the* toolchain, so it must win over anything the
    developer's environment already offers.
    """
    environ = dict(os.environ if env is None else env)
    entry = str(toolchain_bin(project))
    kept = [part for part in environ.get("PATH", "").split(os.pathsep) if part and part != entry]
    environ["PATH"] = os.pathsep.join([entry, *kept])
    return environ


@pytest.fixture(scope="session")
def large_bench(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The agent task benchmark's large bench, with task 10's export, built once per session.

    The ``large-bench`` specimen (held to its pin) plus task 10's export, a writer run over the whole
    bench. Building is the larger half of a reading module's time, so it is built once per pytest
    process. Read-only: a test that changes a file works on its own copy.

    What it does not reach: a module run alone still builds it (the session is the module's), and
    under ``xdist`` each worker that collects a module requesting it builds its own copy.
    """
    bench_module = agent_task_bench_module()
    return bench_module.build_large(tmp_path_factory.mktemp("large") / "bench", with_import=True)


@pytest.fixture(scope="session")
def workstation_archive_unpacked(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The real archive of this version and platform, unpacked once for the session.

    An unpacked root with no ``wheels/PLATFORM.json`` is not a SWCraft archive, so it is refused here
    rather than further on.

    What it does **not** reach: it extracts, it does not verify. The archive's own
    ``SHA256SUMS`` is checked by the ``install.py`` inside it, and
    :func:`require_workstation_archive` stats a filename and never opens the zip.
    """
    root = tmp_path_factory.mktemp("workstation-archive")
    with zipfile.ZipFile(require_workstation_archive()) as archive:
        archive.extractall(root)
    install = workstation_install_module()
    platform_stamp = root / install.WHEELS_DIRNAME / install.PLATFORM_FILENAME
    assert platform_stamp.is_file(), f"the unpacked archive holds no {platform_stamp}"
    return root


def install_workstation_archive(unpacked: Path, home: Path) -> subprocess.CompletedProcess:
    """Run *unpacked*'s own ``install.py`` into *home*; the completed process.

    ``--python sys.executable`` is the installer's developer seam: build on this interpreter (a 3.11
    of the wheelhouse's platform) and unpack no Python payload; the carried-Python run is
    ``test_workstation_install.py``'s own. ``--no-path --no-vsix``: an acceptance must not edit the
    developer's environment or touch their VS Code.

    A failed run's message carries the home's ``install.log``: the run is the quiet one under test,
    so pip's output is only in the log.
    """
    completed = run_captured(
        [
            sys.executable,
            unpacked / "install.py",
            "--home",
            home,
            "--python",
            sys.executable,
            "--no-path",
            "--no-vsix",
        ],
        cwd=unpacked,
    )
    if completed.returncode != 0:
        log = home / "install.log"
        logged = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else f"(no {log})"
        raise AssertionError(f"{output_text(completed)}\n--- {log} ---\n{logged}")
    return completed


@pytest.fixture(scope="session")
def archive_install(tmp_path_factory: pytest.TempPathFactory, workstation_archive_unpacked: Path) -> Path:
    """The real workstation archive, unpacked and installed **once per session** into a temporary home.

    Returns the installed home; the argv is :func:`install_workstation_archive`'s. Shared because
    installing is ~84 wheels of work: every consumer reads it or repairs it (reinstalling the same
    archive leaves the same installation), and a case that destroys an installation builds its own.
    It is **not** evidence about Windows.
    """
    home = tmp_path_factory.getbasetemp() / TOOLCHAIN_HOME_DIRNAME
    completed = install_workstation_archive(workstation_archive_unpacked, home)
    executable = installed_executable(home, "swcraft")
    assert executable.is_file(), f"the installer wrote no {executable}\n{output_text(completed)}"
    return home


@pytest.fixture(scope="session")
def workstation_installation(archive_install: Path) -> Path:
    """The home of :func:`archive_install` -- the installation the ``moon`` builds run against."""
    return archive_install


@pytest.fixture(scope="session")
def swcraft_tools_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One freshly built ``swcraft-tools`` wheel, shared by every guard that needs one.

    Built rather than read from ``dist/python/``, where a wheel may predate the sources. Building may use
    the network; every assertion reads local files only. Shared so the guards that compare package
    data against it read the same artifact. Only ``artifact``-marked tests request it.
    """
    outdir = tmp_path_factory.mktemp("swcraft-tools-wheel")
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(outdir), str(REPO_ROOT / "packages" / "tools")],
        cwd=str(REPO_ROOT),
        check=True,
    )
    wheels = sorted(outdir.glob("swcraft_tools-*.whl"), key=lambda path: path.as_posix())
    assert len(wheels) == 1, f"expected exactly one swcraft-tools wheel, found: {[wheel.name for wheel in wheels]}"
    return wheels[0]


def declared_outputs(project: Path) -> dict[str, Path]:
    """The artifacts the project's **own manifest** declares, as absolute paths.

    Read from ``swcraft.yaml`` rather than hardcoded, so a disagreement between the build graph and the
    manifest stays visible. Every key names one file.
    """
    manifest = yaml.safe_load((project / "swcraft.yaml").read_text(encoding="utf-8"))
    outputs = manifest["outputs"]
    assert outputs, "the template's manifest declares no outputs"
    return {key: project / value for key, value in outputs.items()}


def declare_no_support(swc_file: str | Path) -> None:
    """Append ``handle_termination_and_restart: no_support`` to the component file *swc_file*.

    A component that sets no ``handle_termination_and_restart`` is
    ``rule:swc.undeclared-termination-and-restart``'s finding and the ARXML writer's refusal, and no
    schema default stands in for it. ``writer.create_swc`` takes no such parameter, so the line is
    appended to the file it has just written.
    """
    path = Path(swc_file)
    path.write_text(
        path.read_text(encoding="utf-8") + "handle_termination_and_restart: no_support\n",
        encoding="utf-8",
        newline="\n",
    )


def seed_valid_model(project: Path) -> None:
    """Write a minimal *valid* model: one processor, data type, interface, SWC, requirement.

    Authored through ``swcraft_tools.writer``, the functions the MCP write tools call: the CLI has no
    model-authoring subcommands.
    """
    model_root = str(project / "model")
    # Every declared layer, not only the ones authored here: a stale element in any
    # layer would be read by `check-model` and the writers alike.
    for directory in layer_directories():
        for stale in sorted((project / "model" / directory).rglob("*.yaml"), key=lambda path: path.as_posix()):
            stale.unlink()

    # The wipe removes the scaffolded hardware, and `hardware.no-processor` refuses a
    # model without a processor. No core (no task) and no memory (no budget) needed.
    writer.create_hardware_element(model_root, "CPU", "processor")
    # No data prototype may be typed by a base type, so `float32` is a value type over a base type.
    writer.create_data_type(
        model_root, "float32_base", "base_type", size=32, encoding="IEEE754", native_declaration="float"
    )
    writer.create_data_type(model_root, "float32", "value_type", base="float32_base")
    writer.create_interface(model_root, "ISpeed", [{"name": "speed", "implementation_type": "float32"}])
    declare_no_support(writer.create_swc(model_root, "SpeedSensor", "sensors"))
    writer.add_port(model_root, "SpeedSensor", "pSpeed", "provide", "ISpeed")
    # The wipe also removes the root composition, which `composition.no-root` requires.
    # Written after `SpeedSensor` because it places it.
    _root_composition(Path(model_root), ["SpeedSensor"])
    writer.create_requirement(
        model_root,
        "REQ-SPEED-001",
        "Vehicle speed shall be measured",
        "The speed sensor provides the measured vehicle speed.",
    )
    # `SpeedSensor` satisfies the requirement, keeping the model free of the
    # `trace.requirement-unallocated` warning.
    writer.add_trace_link(model_root, "REQ-SPEED-001", "SpeedSensor")


def break_model(project: Path) -> None:
    """Introduce two deliberate errors and one warning, each the only finding of its kind.

    **The asserted error** is a ``satisfies`` member of ``SpeedSensor`` naming a requirement no file
    declares (``trace.link-unknown-requirement``): every YAML file stays individually valid, so only
    the cross-layer ``check-model`` raises it. Spelled with a version, so no version warning joins it;
    written by hand because ``add_trace_link`` refuses an undeclared requirement. Its sentence is ASCII.

    **The non-ASCII witness** is the identifier rule's error (``identifier.declared``) on an SWC named
    ``Bad-Swc``, written by hand because the writer refuses the name. Its sentence carries an em dash
    (``Rename 'Bad-Swc' — for example to 'Bad_Swc'``): the one check on Windows CI that a non-ASCII
    finding line survives moon's captured output; no warning sentence carries a non-ASCII character.
    The file declares its termination handling, no memory and no composition placement, so it
    introduces that one finding only.

    **The warning** beside them is a requirement nothing satisfies
    (``trace.requirement-unallocated``) — it refuses nothing.
    """
    model_root = project / "model"
    bad = model_root / "swc" / "sensors" / "Bad-Swc.yaml"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(
        "name: Bad-Swc\ntype: application\nhandle_termination_and_restart: no_support\n",
        encoding="utf-8",
        newline="\n",
    )
    writer.create_requirement(
        str(model_root),
        "REQ-UNALLOCATED-001",
        "Nothing satisfies this requirement",
        "No element of the seeded model realizes it.",
    )
    sensor = Path(find_swc_file(str(model_root), "SpeedSensor"))
    data = yaml.safe_load(sensor.read_text(encoding="utf-8"))
    data["satisfies"] = [*data.get("satisfies", []), "REQ-GHOST-001@1"]
    sensor.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n")


def moon_build(project: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """``moon run :build`` in *project* -- the whole local contract, in one call.

    The subprocess always gets :func:`build_environment`, so moon spawns the installed ``swcraft``; the
    installation's entry is added to a caller's *env*, not instead of it.
    """
    return run_captured([require_moon(), "run", ":build"], cwd=project, env=build_environment(project, env))


def scaffold(project: Path) -> None:
    """``swcraft init <dir> --integration moon`` from *this* checkout's CLI.

    The repo's interpreter, because the scaffolded project installs nothing and has no interpreter of
    its own; its build runs the workstation installation.
    """
    completed = run_captured(
        [
            sys.executable,
            "-m",
            "swcraft_tools.cli",
            "init",
            str(project),
            "--integration",
            "moon",
            "--name",
            PROJECT_NAME,
        ],
        cwd=REPO_ROOT,
    )
    assert completed.returncode == 0, output_text(completed)


def make_git_repository(project: Path) -> None:
    """``git init`` + one commit -- a *precondition* of the cache cases, not a nicety.

    Moon derives its file hashes from the VCS: with no repository it never reports a task ``cached``
    (moon 2.3.5), so the case that catches a cached clean step would pass vacuously.
    """
    _git("init", cwd=project)
    _git("add", "-A", cwd=project)
    _git("commit", "-m", "scaffold the architecture project", cwd=project)


@pytest.fixture(scope="module")
def architecture_project(tmp_path_factory, workstation_installation: Path) -> Path:
    """A scaffolded, git-backed standalone project -- built once, reused.

    Every case reseeds the model itself, so cases stay independent; the scaffold and the installation
    are shared. The assertion keeps the two derivations of the toolchain home in agreement.
    """
    require_moon()
    project = tmp_path_factory.mktemp(PROJECT_NAME).resolve()
    assert REPO_ROOT not in project.parents, f"{project} is inside the SWCraft checkout"
    assert toolchain_home(project).resolve() == workstation_installation.resolve(), (
        f"{toolchain_home(project)} is not the installed home {workstation_installation}"
    )
    scaffold(project)
    make_git_repository(project)
    return project


# ---------------------------------------------------------------------------
# Editing-protocol vector fixtures; the helpers live in `editing_vector_fixtures.py`,
# imported inside the factory because it imports REPO_ROOT from this module.
# ---------------------------------------------------------------------------


#: The vector harness's plain base fixture — ``examples/basic`` with no overlay.
BASE_VECTOR = "read-swc-element"


@pytest.fixture
def editing_vector_project(tmp_path):
    """Build a seeded editing-protocol baseline project, by vector name.

    Returns:
        A callable taking a vector's stable ``name`` and returning the project
        root (the directory holding ``swcraft.yaml``).
    """
    import editing_vector_fixtures

    def build(vector_name: str) -> Path:
        return editing_vector_fixtures.build_fixture(tmp_path / "project", vector_name)

    return build


# ---------------------------------------------------------------------------
# Git scratch-repo helpers for load_snapshot's rev=<str> path.
# ---------------------------------------------------------------------------


def commit_link_out_of_the_tree(repo: Path, scratch: Path) -> None:
    """Commit a symbolic link at the top of *repo*'s tree, pointing at `../outside`.

    At the top, so an extraction into a temporary directory leaves it by one level; deeper in
    the tree the link would stay inside and be refused by nothing. Committed through the index,
    so no symlink is made on disk and this runs on Windows too. *scratch* is a directory outside
    the tree, for the blob's source file.
    """
    target = scratch / "link-target.txt"
    target.write_bytes(b"../outside")
    blob = _git("hash-object", "-w", str(target), cwd=repo).stdout.strip()
    _git("update-index", "--add", "--cacheinfo", f"120000,{blob},escape", cwd=repo)
    _git("commit", "-m", "a link out of the tree", cwd=repo)


def commit_into_a_damaged_repository(project: Path, git_directory: Path) -> str:
    """Commit *project* with *git_directory* beside it, empty the root tree's object, return its name.

    The git directory sits outside the project, so `rev-parse --show-toplevel` still succeeds and
    the refusal is `git archive`'s, naming the emptied object file.
    """
    _git("init", "--separate-git-dir", str(git_directory), cwd=project)
    _git("add", "-A", cwd=project)
    _git("commit", "-m", "initial commit", cwd=project)
    tree = _git("rev-parse", "HEAD^{tree}", cwd=project).stdout.strip()
    loose_object = git_directory / "objects" / tree[:2] / tree[2:]
    loose_object.chmod(0o644)
    loose_object.write_bytes(b"")
    return tree


@pytest.fixture
def git_repo(tmp_path):
    """A scratch git repo at tmp_path, model root model/, holding one committed SWC SwcOne."""
    _git("init", cwd=tmp_path)
    model_root = tmp_path / "model"
    swc_dir = model_root / "swc"
    swc_dir.mkdir(parents=True, exist_ok=True)
    (swc_dir / "SwcOne.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "SwcOne",
                "type": "application",
                "description": "test swc",
                "handle_termination_and_restart": "no_support",
            },
            default_flow_style=False,
            sort_keys=False,
        ),
        encoding="utf-8",
        newline="\n",
    )
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-m", "initial commit", cwd=tmp_path)
    return tmp_path, model_root


# ---------------------------------------------------------------------------
# ARXML golden helpers. Slices are addressed by ``SHORT-NAME``, never by line
# number, so an unrelated golden change cannot silently retarget a test.
# ---------------------------------------------------------------------------

GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "arxml_golden"

#: The golden name -> the model root it was generated from. ``empty`` has no entry: git tracks no
#: empty directory, so it is generated from a fresh empty tree.
GOLDEN_MODEL_ROOTS = {
    "basic-4.2.2": REPO_ROOT / "examples" / "basic" / "model",
    "composite-4.2.2": Path(__file__).resolve().parent / "fixtures" / "component_exchange" / "model",
}

#: A committed model tree used **in place** and deliberately **not** a golden: no committed ``.arxml``
#: is generated from it, so a golden is never regenerated to exercise something new. The same holds for
#: every ``*_MODEL_ROOT`` below. ``fixtures/init_values/README.md`` says what every element exercises.
INIT_VALUES_MODEL_ROOT = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "init_values" / "model"

#: The access-point model (``fixtures/access_points/README.md``): the four access containers -- a
#: by-argument read, a by-value read, a write and a call -- one runnable declaring all three lists, one
#: declaring none, one port no runnable touches, and a component with no access point at all, so the
#: marker's presence and absence are both in one generated document.
ACCESS_POINTS_MODEL_ROOT = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "access_points" / "model"

#: The safety model (``fixtures/safety/README.md``, whose element list ``test_validator_coexistence.py``
#: holds equal to the tree's). It carries every case of the six coexistence rules in one model -- a
#: provider below a declared level and below a derived one, a derived ``QM``
#: producing a rated element, a consumer above an element with and without a
#: measure, a measure whose text is BLANK (``safety.measure-blank``'s one
#: finding since contract ``12.4.0``), a dangling key on a sender-receiver
#: interface, a measure map on a provide port of a client-server one, and three
#: tasks: mixed and unargued, mixed and argued, homogeneous and argued anyway.
#: It mints exactly SEVENTEEN safety findings: eight from the coexistence check and nine from
#: the placement check -- each of the three ``D`` partitions on the ``B``-capability ``CORE0``,
#: ``TorqueLimiter``'s non-zero budget on the ``B``-capability ``RAM0``, ``P_Mixed``'s two
#: lower components with no argument, and ``RAM0`` below each of the three partitions. Its one
#: other cross-layer finding is ``task.instance-in-multiple-partitions`` on ``TorqueLimiter``;
#: ``test_validator_coexistence.py``'s ``TestTheComposition`` asserts both lists. Every component
#: budgets ``RAM0`` -- ``SpeedSensor`` with ``0`` -- so ``hardware.missing-budget`` stays silent
#: under ``enforcement: strict``.
SAFETY_MODEL_ROOT = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "safety" / "model"

#: The composition-emitter model root (``fixtures/compositions/README.md``), one of several committed
#: composition HIERARCHIES (``compatible_interfaces``, ``diagrams`` and others nest too): a root whose
#: two members are compositions, two boundary ports, three delegations across two levels, one
#: assembly connector per level, a member whose name differs from its type (``Alpha`` of type
#: ``Beta``) and an external stub.
COMPOSITIONS_MODEL_ROOT = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "compositions" / "model"

#: One of the two committed models whose ``connectors/`` join two **different**
#: interfaces (the other is :data:`COMPATIBLE_INTERFACES_MODEL_ROOT`, the
#: hierarchical second root below) -- a sender-receiver pair whose required side is a
#: subset by element name and a client-server pair whose required side is a
#: subset by operation name, both written in a second spelling of one data type.
#: On every other committed root the two ports of an assembly connector name one
#: interface, so the same-name short-circuit answers before the compatibility
#: predicate is asked.
#: ``fixtures/interface_compatibility/README.md`` says what every element is for.
INTERFACE_COMPATIBILITY_MODEL_ROOT = (
    REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "interface_compatibility" / "model"
)

#: The compatible-interface HIERARCHY (``fixtures/compatible_interfaces/README.md``); the root above is
#: flat because the validator's cases perturb it. It holds a ``DELEGATION-SW-CONNECTOR`` whose two
#: ports name different interfaces the standard joins, and all four compatible-but-different shapes —
#: a sender-receiver and a client-server assembly connector, and a provide and a require delegation.
COMPATIBLE_INTERFACES_MODEL_ROOT = (
    REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "compatible_interfaces" / "model"
)

#: The committed model that sets each of ``handle_termination_and_restart``'s three values once beside
#: one non-stub component that declares none (``fixtures/handle_termination_and_restart/README.md``).
#: Many other trees set the field -- ``examples/basic`` and most fixtures declare ``no_support``, the
#: agent-bench models the other two values too -- but none pairs the three with an undeclared
#: non-stub component. The writer refuses the tree as committed
#: (``rule:swc.undeclared-termination-and-restart``, for ``SilentSwc``), so a test that generates it
#: generates :func:`handle_termination_and_restart_declared_copy` instead.
HANDLE_TERMINATION_AND_RESTART_MODEL_ROOT = (
    REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "handle_termination_and_restart" / "model"
)

#: The fixture's one component that declares no value (the absence fixture).
HANDLE_TERMINATION_AND_RESTART_SILENT = "SilentSwc"

#: Fixture folders that hold other tests' data, not generator inputs: the agent benchmark's models
#: and the requirements interchange build. The model-tree gates of the byte-identity and termination
#: records leave every tree under them out: no generator record covers these trees.
DATA_FIXTURE_ROOTS = (
    "packages/tools/tests/fixtures/agent-bench/",
    "packages/tools/tests/fixtures/contracts/requirements-interchange/",
)


def is_data_fixture_tree(tree: str) -> bool:
    """Return whether the repository-relative POSIX *tree* lies under one of :data:`DATA_FIXTURE_ROOTS`."""
    return tree.startswith(DATA_FIXTURE_ROOTS)


def handle_termination_and_restart_declared_copy(destination: Path) -> Path:
    """Copy the fixture to *destination* without its absence component, and return it.

    ``SilentSwc`` declares none and is refused by ``rule:swc.undeclared-termination-and-restart``, so it
    stays in the committed tree for the tests about the absence only.
    """
    shutil.copytree(HANDLE_TERMINATION_AND_RESTART_MODEL_ROOT, destination)
    (silent,) = list(destination.glob(f"swc/*/{HANDLE_TERMINATION_AND_RESTART_SILENT}.yaml"))
    silent.unlink()
    return destination


def read_golden(name: str) -> str:
    """Return a committed golden's exact text (``name`` without the extension)."""
    return (GOLDEN_DIR / f"{name}.arxml").read_text(encoding="utf-8")


def make_empty_model_root(base: Path) -> Path:
    """Create an empty-but-well-formed model tree under *base* and return it.

    The degenerate input behind ``empty-4.2.2.arxml``: every layer directory
    exists and every one of them is empty.
    """
    model_root = base / "model"
    for subdir in arxml_layer_directories():
        (model_root / subdir).mkdir(parents=True, exist_ok=True)
    return model_root


def golden_slice(text: str, short_name: str, tag: str | None = None) -> str:
    """Return the element in *text* whose direct ``SHORT-NAME`` child is *short_name*.

    The returned fragment is dedented by the opening tag's own indentation, so a
    section nested six levels deep compares equal to the same section emitted at
    the top of a fresh sub-document.

    Relies on two properties of the generated format, both pinned by
    ``test_arxml_golden.py``: ``SHORT-NAME`` is always the element's first child,
    and no element is ever written in the self-closing ``<X/>`` form.

    Args:
        text: a whole ARXML document.
        short_name: the ``SHORT-NAME`` text identifying the element.
        tag: the element's tag, required only to disambiguate when one
            ``SHORT-NAME`` is carried by more than one element (an SWC and its
            ``SW-COMPONENT-PROTOTYPE`` in the composition, for instance).

    Returns:
        The element's source lines, dedented, without a trailing newline.

    Raises:
        ValueError: if no element matches, or if several do and *tag* was not
            given (or does not narrow it to one).
    """
    lines = text.splitlines()
    needle = f"<SHORT-NAME>{short_name}</SHORT-NAME>"
    matches: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        if line.strip() != needle or index == 0:
            continue
        opening = lines[index - 1].strip()
        if not opening.startswith("<") or opening.startswith("</"):
            continue
        open_tag = opening[1:].split(">")[0].split(" ")[0]
        indent = lines[index - 1][: len(lines[index - 1]) - len(lines[index - 1].lstrip())]
        matches.append((index - 1, open_tag, indent))

    if tag is not None:
        matches = [m for m in matches if m[1] == tag]

    if not matches:
        raise ValueError(
            f"No element with SHORT-NAME '{short_name}'"
            + (f" and tag '{tag}'" if tag else "")
            + " found in the given ARXML document."
        )
    if len(matches) > 1:
        raise ValueError(
            f"SHORT-NAME '{short_name}' is carried by {len(matches)} elements "
            f"({', '.join(sorted(m[1] for m in matches))}); pass tag= to disambiguate."
        )

    start, open_tag, indent = matches[0]
    closing = f"{indent}</{open_tag}>"
    for end in range(start + 1, len(lines)):
        if lines[end] == closing:
            return "\n".join(
                line[len(indent) :] if line.startswith(indent) else line for line in lines[start : end + 1]
            )
    raise ValueError(f"Unterminated <{open_tag}> for SHORT-NAME '{short_name}' — expected a line {closing!r}.")


# ---------------------------------------------------------------------------
# AUTOSAR version relabelling: a 4.3.0-labelled input made by relabelling a real
# 4.2.2 document, byte-identical to its twin except for the one token -- what
# "the version is not an input to the conflict policy" needs.
# ---------------------------------------------------------------------------


def relabel_arxml_version(text: str, *, source: str, target: str) -> str:
    """Return *text* with the *source* XSD filename replaced by *target*.

    Args:
        text: a whole ARXML document.
        source: the XSD filename the document currently declares.
        target: the XSD filename it should declare instead.

    Returns:
        The relabelled document.

    Raises:
        ValueError: if *source* does not occur exactly once. A silent no-op is the failure mode
            that matters here: a test relabelling nothing would assert that a 4.2.2 file reads
            like a 4.2.2 file and report a green 4.3.0 acceptance.
    """
    occurrences = text.count(source)
    if occurrences != 1:
        raise ValueError(f"Expected exactly one occurrence of {source!r} to relabel, found {occurrences}.")
    return text.replace(source, target)


# ---------------------------------------------------------------------------
# Hardware-layer model fixture builders.
# ---------------------------------------------------------------------------


def _write(path: Path, data: dict) -> None:
    """Write one YAML element file, creating its parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n")


def _codes(findings: Iterable[Finding]) -> list[str]:
    """The finding codes of *findings*, in order."""
    return [f.code for f in findings]


#: The core name every builder defaults to, so a task and a core written without names still resolve.
DEFAULT_CORE = "CORE0"


def partition_on(core: str) -> str:
    """The name of the unrated partition :func:`_core` writes on *core*.

    A task runs on its partition's core; a test names it with
    ``_task(…, partition=partition_on("CORE1"))``. ``CORE<n>`` carries ``PARTITION<n>``, as in the
    in-repo fixtures (``examples/basic``), and any other core ``<core>_PARTITION``.
    """
    if core.startswith("CORE"):
        return "PARTITION" + core[len("CORE") :]
    return f"{core}_PARTITION"


#: The partition every task builder defaults to: the one :func:`_core` writes on
#: :data:`DEFAULT_CORE`.
DEFAULT_PARTITION = partition_on(DEFAULT_CORE)


def _processor(model_root: Path | str, name: str = "CPU", **extra) -> None:
    """The one processor every complete model declares.

    Needed by a fixture that runs ``check-model``: ``hardware.no-processor`` refuses a model without one.
    """
    _write(element_path(model_root, "hardware", name), {"name": name, "kind": "processor", **extra})


def _core(model_root: Path | str, name: str, threshold: float = 0.7, *, partition: bool = True, **extra) -> None:
    """One ``kind: core`` and, unless *partition* is false, the unrated partition :func:`partition_on` names.

    A test about the hardware layer's own element set passes ``partition=False``.
    """
    _write(
        element_path(model_root, "hardware", name),
        {"name": name, "kind": "core", "threshold": threshold, **extra},
    )
    if partition:
        _partition(model_root, partition_on(name), core=name)


def _partition(model_root: Path | str, name: str = DEFAULT_PARTITION, core: str = DEFAULT_CORE, **extra) -> None:
    """One ``kind: partition`` on *core*, unrated unless *extra* carries an ``asil``."""
    _write(
        element_path(model_root, "hardware", name),
        {"name": name, "kind": "partition", "core": core, **extra},
    )


def _memory(
    model_root: Path | str,
    name: str,
    size: int = 1024,
    threshold: float = 0.8,
    accessible: list[str] | None = None,
    memory_type: str = "RAM",
    **extra,
) -> None:
    """One ``kind: memory``; ``accessible`` defaults to :data:`DEFAULT_CORE`."""
    _write(
        element_path(model_root, "hardware", name),
        {
            "name": name,
            "kind": "memory",
            "type": memory_type,
            "size": size,
            "accessible": list(accessible) if accessible is not None else [DEFAULT_CORE],
            "threshold": threshold,
            **extra,
        },
    )


def _task(
    model_root: Path | str,
    name: str,
    period: float = 0.01,
    priority: int = 10,
    partition: str = DEFAULT_PARTITION,
    **extra,
) -> None:
    """One task in *partition*.

    By default the one :func:`_core` writes on :data:`DEFAULT_CORE`."""
    _write(
        element_path(model_root, "tasks", name),
        {"name": name, "period": period, "priority": priority, "partition": partition, **extra},
    )


def _swc(model_root: Path | str, name: str, handle_termination_and_restart: str | None = "no_support", **extra) -> None:
    """One SWC, in the fixed group ``group``.

    It declares ``handle_termination_and_restart: no_support`` unless the caller
    passes another value, or ``None`` for a component that declares none (a test
    about the absence); see :func:`declare_no_support`.
    """
    termination = (
        {}
        if handle_termination_and_restart is None
        else {"handle_termination_and_restart": handle_termination_and_restart}
    )
    _write(
        element_path(model_root, "swc", name, group="group"),
        {"name": name, "type": "application", **extra, **termination},
    )


def _safety_reading(model_root: Path | str):
    """The validator's :class:`~swcraft_tools.validator.SafetyReading` of the model under *model_root*."""
    from swcraft_tools.reader import (
        read_compositions,
        read_hardware,
        read_interfaces,
        read_requirements,
        read_swc_model,
        read_tasks,
    )
    from swcraft_tools.validator import safety_reading

    root = str(model_root)
    return safety_reading(
        read_swc_model(root),
        read_interfaces(root),
        read_tasks(root),
        read_requirements(root),
        read_hardware(root),
        read_compositions(root),
    )


def _root_composition(
    model_root: Path | str,
    members: Iterable[str | dict] = (),
    name: str = "Root",
    title: str = "Root composition",
    **extra,
) -> None:
    """The root composition every model owes: `rule:composition.no-root` refuses a model without one.

    A connector naming no composition belongs to the root, and ``generate arxml`` refuses one it cannot
    place. *members* are bare type names (member named after its type) or mappings written verbatim; the
    writer emits the ``SW-COMPONENT-PROTOTYPE``s in their declared order.
    """
    declared = [{"name": m, "type": m} if isinstance(m, str) else dict(m) for m in members]
    _write(
        element_path(model_root, "compositions", name),
        {"name": name, "title": title, "root": True, **extra, "members": declared},
    )


def _write_manifest(path: Path, data: dict | None = None) -> Path:
    """Write swcraft.yaml to *path* (directory); return path to yaml file.

    When *data* is omitted a minimal manifest with all required fields and a
    full ``outputs`` block is written, matching the defaults used by the CLI.
    """
    if data is None:
        data = {
            "name": "test-project",
            "schema_version": "1.0",
            "model_root": "model",
            "dist_root": "dist",
            "views_root": "views",
            "outputs": {
                "arxml": "dist/model.arxml",
                "needs": "dist/needs.json",
            },
        }
    path.mkdir(parents=True, exist_ok=True)
    manifest = path / "swcraft.yaml"
    manifest.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n")
    return manifest


# ---------------------------------------------------------------------------
# The wide model — one model big enough that a per-element cost is visible
# ---------------------------------------------------------------------------
#
# The builder is `scale_model.py`, a plain module, because the mcp suite needs the
# SAME model and a fixture cannot cross a project boundary. The constants are
# re-exported because `test_validator_scale.py` reads them off `conftest`.

WIDE_CONNECTORS = scale_model.WIDE_CONNECTORS
WIDE_PORTS_PER_SWC = scale_model.WIDE_PORTS_PER_SWC
WIDE_SWCS = scale_model.WIDE_SWCS


@pytest.fixture
def wide_model(tmp_path: Path) -> Path:
    """A model root holding ``WIDE_SWCS`` wide components and ``WIDE_CONNECTORS`` connectors."""
    return scale_model.build_wide_model(tmp_path)


@pytest.fixture(autouse=True)
def _copilot_guard_isolation(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every ``test_copilot_guard_*`` test: no inherited ``CLAUDE_PROJECT_DIR``, no reachable judge deadline.

    A Claude Code session sets ``CLAUDE_PROJECT_DIR``; a test that needs one sets it itself. An over-deadline
    judgement denies, so a loaded host would turn an allow into a deny, and would stop a growth test's larger run
    early and flatter its ratio; a deadline test names its own. Keyed on the module's name, so a new guard module
    is isolated without an import. What this does not reach: a child process
    keeps the shipped deadline (``packages/mcp/tests/functional``'s ``guard --host codex`` runs assert an allow and
    can flip; ``test_the_console_script_answers_without_importing_cli`` asserts a deny, so it cannot).
    """
    if not request.module.__name__.startswith("test_copilot_guard_"):
        return
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setattr(copilot_guard, "_JUDGE_SECONDS", UNREACHABLE_JUDGE_SECONDS)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A project directory with a manifest, a model file and a file outside the model."""
    root = tmp_path / "proj"
    _write_manifest(root)
    (root / "model" / "swc").mkdir(parents=True)
    (root / "model" / "swc" / "Engine.yaml").write_text("name: Engine\n", encoding="utf-8", newline="\n")
    (root / "notes.md").write_text("notes\n", encoding="utf-8", newline="\n")
    return root


@pytest.fixture
def codex_dir(project_dir: Path) -> Path:
    """:func:`project_dir` with the Codex hook file, so the Codex dialect guards it."""
    write_codex_hook(project_dir)
    (project_dir / "notes").mkdir()
    return project_dir


@pytest.fixture
def user_home(tmp_path: Path, monkeypatch) -> Path:
    """A SWCraft home of the test's own, outside the project, never the developer's.

    The hand-off directory is ``<it>/.swcraft/reviews`` (``<it>/SWCraft/reviews`` on Windows)."""
    home = tmp_path / "user-home"
    home.mkdir()
    for variable in ("HOME", "LOCALAPPDATA", "USERPROFILE"):
        monkeypatch.setenv(variable, str(home))
    return home


@pytest.fixture
def claude_dir(project_dir: Path, user_home: Path) -> Path:
    """:func:`project_dir` with Claude Code's project settings file, so the Claude dialect guards it."""
    write_claude_settings(project_dir)
    (project_dir / "notes").mkdir()
    (project_dir / "sub").mkdir()
    return project_dir


@pytest.fixture
def elsewhere(tmp_path: Path) -> Path:
    """A folder in no project."""
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    return folder


@pytest.fixture
def kiro_dir(project_dir: Path, user_home: Path, elsewhere: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """:func:`project_dir` with a Kiro hook folder, so the Kiro dialect guards it.

    The process's working directory is in no project (a test that needs the hook's own anchor in the project moves it
    there)."""
    write_kiro_hooks(project_dir)
    (project_dir / "notes").mkdir()
    monkeypatch.chdir(elsewhere)
    return project_dir
