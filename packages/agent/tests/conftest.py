"""Shared pytest fixtures for swcraft_agent tests."""

import ast
import functools
import os
import subprocess
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path

import mcp.client.stdio
import pytest
import yaml

# The real Node.js, found at import, before any fixture redirects the home; in a module of its own, because this
# conftest is held to running `git` alone and the probe runs `node -e`.
from node_on_path import REAL_NODE_DIRECTORY

# The real home, captured at import: the worktree fixture's git reads its global configuration from it.
from real_home import HOME_VARIABLES, REAL_HOME_ENVIRONMENT, REAL_HOME_UNSET
from scratch_git import NO_AUTO_MAINTENANCE
from swcraft_agent.llm_client import LLMClient
from swcraft_tools.model_registry import current_registry, production_registry


@pytest.fixture(autouse=True)
def _a_user_home_of_its_own(tmp_path_factory, monkeypatch):
    """Every test, and every server it spawns, gets a SWCraft home of its own.

    A ``panel`` server writes its pending reviews into the user's SWCraft home, so this keeps tests out of the
    developer's real ``~/.swcraft/reviews`` (where an open VS Code window would show them) and xdist workers out
    of each other's.
    """
    user_home = os.fspath(tmp_path_factory.mktemp("user-home"))
    for variable in HOME_VARIABLES:
        monkeypatch.setenv(variable, user_home)

    # The redirected home hides Node from a version manager's shim, so the real binary goes first on PATH.
    if REAL_NODE_DIRECTORY is not None:
        monkeypatch.setenv("PATH", REAL_NODE_DIRECTORY + os.pathsep + os.environ.get("PATH", ""))


@pytest.fixture(autouse=True)
def _server_stderr_goes_to_a_file(tmp_path: Path, monkeypatch):
    """Every ``stdio_client`` spawn in this suite writes the server's stderr to a file.

    The MCP SDK binds ``stdio_client``'s ``errlog`` default to ``sys.stderr`` at import, and ``Popen`` needs its
    ``fileno()``: if the first import lands under ``capsys`` (an in-memory stream), every later spawn on that
    xdist worker fails with ``io.UnsupportedOperation: fileno``. Patching the module attribute reaches both spawn
    sites, which import ``stdio_client`` at call time.

    Not reached: a caller that bound ``stdio_client`` at import time (none does). Not a product defect: outside
    pytest ``sys.stderr`` is a real stream.
    """
    original = mcp.client.stdio.stdio_client
    log = (tmp_path / "mcp-server-stderr.log").open("w", encoding="utf-8", newline="\n")
    monkeypatch.setattr(mcp.client.stdio, "stdio_client", functools.partial(original, errlog=log))
    try:
        yield
    finally:
        log.close()


class FakeLLMClient(LLMClient):
    """Deterministic test double for LLMClient.

    Driven by an explicit ordered *program*: a list of rounds, where each round is:
    - list[tuple[str, dict]]: tool calls to issue via tool_executor (in order),
      feeding results back before moving to the next round.
    - str: final plain-text response — ends this complete() call.

    A complete() call consumes rounds from the front of the program until it
    hits a str round (which it returns) or the program is exhausted (raises).
    Multiple complete() calls on the same FakeLLMClient consume successive rounds.

    ``complete_structured()`` consumes a separate program, ``structured``: per call, the payload dict to return or
    an ``Exception`` to raise. A missing round raises, so a test that forgot to queue a scope proposal fails
    instead of silently authorizing one.

    Attributes:
        calls: All (tool_name, tool_args) pairs executed so far.
        results: The string each of those calls returned, so a refused call is observable.
        structured_calls: All (messages, schema) pairs passed to ``complete_structured()``.
    """

    def __init__(self, program: list, structured: list | None = None) -> None:
        self._program: deque = deque(program)
        self._structured: deque = deque(structured or [])
        self.calls: list[tuple[str, dict]] = []
        self.results: list[str] = []
        self.structured_calls: list[tuple[list[dict], dict]] = []

    def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        tool_executor: Callable[[str, dict], str],
    ) -> str:
        while self._program:
            round_item = self._program.popleft()
            if isinstance(round_item, str):
                return round_item
            for tool_name, tool_args in round_item:
                self.calls.append((tool_name, tool_args))
                self.results.append(tool_executor(tool_name, tool_args))
        raise RuntimeError("FakeLLMClient program exhausted before returning text")

    def complete_structured(self, messages: list[dict], schema: dict) -> dict:
        self.structured_calls.append((list(messages), schema))
        if not self._structured:
            raise RuntimeError("FakeLLMClient structured program exhausted — no structured round is queued")
        item = self._structured.popleft()
        if isinstance(item, Exception):
            raise item
        return item


class FakeMcp:
    """Minimal MCP stand-in — avoids subprocess overhead in prompt-only tests."""

    def list_openai_tools(self):
        return []

    def call_tool(self, name, args):
        return ""

    def close(self):
        pass


#: Every declared layer's storage directory, read off the registry so no stale layer list can spread.
MODEL_SUBDIRS = tuple(facts.storage.directory for facts in production_registry().layers)

#: The same, less the optional layers: the set ``swcraft init`` creates. Equal to it while no layer is optional;
#: both names are kept because each states a different rule.
REQUIRED_MODEL_SUBDIRS = tuple(facts.storage.directory for facts in production_registry().layers if not facts.optional)


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n")


# One element file, its directory derived from the registry: by the payload's `kind`, or by an explicit `layer`
# for a payload without one, so a moved directory reaches every test with no edit there. The same three
# definitions live in the tools and mcp conftests (no suite can import another's), and a test holds them equal.
#
# Not reached: a file whose name is not its element's storage name, a malformed file without a `kind`, and a
# test whose subject is a directory itself.


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


def seed_minimal_hardware(model_root: str | Path) -> None:
    """Write the one ``kind: processor`` element ``hardware/`` requires.

    Without it the engine's whole-model validation refuses every change set (``hardware.no-processor``). No core
    and no memory, so no budget exists and no ``missing_budget`` finding can arise.
    """
    from swcraft_tools import writer

    writer.create_hardware_element(str(model_root), "CPU", "processor")


def seed_root_composition(model_root: str | Path, members: Sequence[str] = ()) -> None:
    """Write the one ``root: true`` composition ``compositions/`` requires.

    Without it the engine's whole-model validation refuses every change set (``composition.no-root``).
    *members* are the component types the root places, each named after its type; a project with a connector
    needs them (``composition.connector-endpoint-not-a-member``).
    """
    _write_yaml(
        Path(model_root) / "compositions" / "Root.yaml",
        {
            "name": "Root",
            "title": "Root composition",
            "root": True,
            "members": [{"name": name, "type": name} for name in members],
        },
    )


def declare_no_support(model_root: str | Path, group: str, name: str) -> None:
    """Append ``handle_termination_and_restart: no_support`` to the component *name*.

    Without the field, the engine's whole-model validation refuses every change set; ``writer.create_swc`` takes
    no such parameter, so the line is appended to its file. Explicit, never a schema default, so absent and
    ``no_support`` stay distinct.
    """
    path = Path(model_root) / "swc" / group / f"{name}.yaml"
    path.write_text(
        path.read_text(encoding="utf-8") + "handle_termination_and_restart: no_support\n",
        encoding="utf-8",
        newline="\n",
    )


@pytest.fixture()
def scaffolded_project(tmp_path: Path) -> Path:
    """Create a minimal SWCraft project in tmp_path and return the manifest path.

    Creates the manifest and one directory per non-optional layer, as ``swcraft init`` does, but not its hardware
    elements: the unscoped MCP client tests assert on an empty project. A suite whose change set must commit
    adds :func:`seed_minimal_hardware`.
    """
    model_dir = tmp_path / "model"
    for subdir in REQUIRED_MODEL_SUBDIRS:
        (model_dir / subdir).mkdir(parents=True, exist_ok=True)

    (tmp_path / "dist").mkdir(parents=True, exist_ok=True)

    manifest = tmp_path / "swcraft.yaml"
    _write_yaml(
        manifest,
        {
            "name": "agent-test-project",
            "schema_version": "1.0",
            "model_root": "model",
            "dist_root": "dist",
            "views_root": "views",
        },
    )
    return manifest


_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture()
def worktree_project(tmp_path: Path):
    """Create a git worktree at tmp_path and return the swcraft.yaml path.

    Uses ``git worktree add <tmp_path> HEAD``, removed with ``--force`` after the test. The worktree needs no
    environment of its own (the build override names the suite's interpreter by absolute path), so teardown
    repairs nothing.

    Both ``git`` calls run without ``GIT_CONFIG_*``: the scenario blanks the credential helper with
    ``GIT_CONFIG_VALUE_0=""``, and on Windows a variable set to ``""`` is deleted, so git would refuse every
    command if teardown ran while that guard is in place. They also run under the real home
    (``REAL_HOME_ENVIRONMENT``, ``REAL_HOME_UNSET``), so git reads the user's global configuration, which on the
    Windows runner carries the ``core.longpaths true`` the deep temp checkout needs.

    Not reached: a git the scenario itself runs (none does), and a global configuration kept elsewhere through
    ``GIT_CONFIG_GLOBAL``, which the strip removes.
    """
    worktree_path = tmp_path / "wt"
    git_env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_CONFIG_")}
    git_env.update(REAL_HOME_ENVIRONMENT)
    for name in REAL_HOME_UNSET:
        git_env.pop(name, None)
    print(f"\n  [slow-test] Creating git worktree at {worktree_path} ...", flush=True)
    # The checkout is not the test's, so its configuration is not written: both commands carry the keys.
    _r = subprocess.run(
        ["git", *NO_AUTO_MAINTENANCE, "worktree", "add", str(worktree_path), "HEAD"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        env=git_env,
    )
    if _r.returncode != 0:
        raise RuntimeError(
            f"git worktree add failed rc={_r.returncode} cwd={_REPO_ROOT}\n"
            f"--- stdout ---\n{_r.stdout.decode('utf-8', 'replace')}\n"
            f"--- stderr ---\n{_r.stderr.decode('utf-8', 'replace')}"
        )
    print("  [slow-test] Worktree ready.", flush=True)
    try:
        yield worktree_path / "examples" / "basic" / "swcraft.yaml"
    finally:
        print("\n  [slow-test] Removing git worktree ...", flush=True)
        _r = subprocess.run(
            ["git", *NO_AUTO_MAINTENANCE, "worktree", "remove", "--force", str(worktree_path)],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            env=git_env,
        )
        if _r.returncode != 0:
            raise RuntimeError(
                f"git worktree remove failed rc={_r.returncode}\n--- stderr ---\n{_r.stderr.decode('utf-8', 'replace')}"
            )
        print("  [slow-test] Worktree removed.", flush=True)


def module_imports(path: Path) -> list[str]:
    """Every module name the module at *path* imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    return imported


def code_string_constants(path: Path) -> list[str]:
    """Every string literal in a module **except** its docstrings.

    Comments never reach the AST, so this is what the module computes with: import paths, argv parts, filenames.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]
