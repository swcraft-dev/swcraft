"""Shared fixtures for MCP server tests.

``_project_env`` is function-scoped and autouse: ``monkeypatch`` is function-scoped, so a
session-scoped fixture using it raises ``ScopeMismatch``. ``_project_env`` and ``model_root``
share the test's ``tmp_path``, so every test is isolated and order-independent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import swcraft_mcp.server as _server
import yaml
from guide_support import install_no_site

# An autouse fixture, found by name; defined once in `tools`.
from single_read_support import reads, single_read_tripwire  # noqa: F401
from swcraft_tools.model_registry import current_registry, production_registry

from tests.functional import real_models

# The editing-protocol vectors are replayed against the model
# `packages/tools/tests/support/editing_vector_fixtures.py` builds; a second seed table here would
# diverge silently. The `tools` helpers come through the declared `pythonpath`; `tests/__init__.py`
# makes this directory a package, so `conftest` names do not collide. A missing helper fails
# collection rather than skipping, because the vectors are the independent oracle.

# ---------------------------------------------------------------------------
# The real Node.js, found before any test redirects the home
# ---------------------------------------------------------------------------

#: How long the one ``node -e`` probe of a session may take.
NODE_PROBE_TIMEOUT_SECONDS = 30


def node_directory_from_answer(returncode: int, answer: str) -> str | None:
    """The directory of the binary ``node -e "process.stdout.write(process.execPath)"`` named.

    ``None`` unless the probe exited 0 and answered the absolute path of an existing file:
    an empty or whitespace answer, a relative one, a directory or a missing path changes nothing.
    """
    if returncode != 0:
        return None
    candidate = Path(answer.strip())
    if not answer.strip() or not candidate.is_absolute() or not candidate.is_file():
        return None
    return os.fspath(candidate.parent)


def real_node_directory(which=shutil.which) -> str | None:
    """The directory of the Node.js binary ``PATH``'s ``node`` runs, or ``None``.

    A version manager's shim finds the real binary through the user's home, so it fails once a
    test redirects the home; asked once under the real environment, Node names itself
    (``process.execPath``). ``None`` when there is no ``node`` or the probe fails, times out or
    answers no existing file.

    Does not reach a real binary that itself reads the home, nor a server that finds Node
    beside its interpreter (``resolve_node`` prefers it over ``PATH``). Every other program in
    that directory moves ahead on ``PATH`` with it.
    """
    node = which("node")
    if node is None:
        return None
    try:
        completed = subprocess.run(
            [node, "-e", "process.stdout.write(process.execPath)"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=NODE_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return node_directory_from_answer(completed.returncode, completed.stdout or "")


#: Computed at import, so it runs before any function-scoped fixture: the probe must see the
#: real ``HOME`` / ``LOCALAPPDATA`` / ``USERPROFILE``, never a test's redirected ones.
REAL_NODE_DIRECTORY = real_node_directory()


def pytest_addoption(parser) -> None:
    """Register ``--require-real-models``, the real-export tier's flag.

    Pytest reads ``pytest_addoption`` only from an initial conftest; the skip-or-fail decision
    is ``tests/functional/real_models.py``'s.
    """
    parser.addoption(
        real_models.FLAG,
        action="store_true",
        default=False,
        help=(
            f"Fail, rather than skip, when {real_models.ENV_VAR} is not set. Passed by "
            "`python scripts/dev.py test-real-models --require-real-models`, where the exports really are; "
            "see tests/functional/real_models.py for the three states it separates."
        ),
    )


#: The specimens this project reads; each is a pinned name.
MCP_SPECIMENS = (
    "basic-twin",
    "example-minimal",
    "harness-two-rows",
    "large-bench",
    "no-memory",
    "passport-small",
    "scenario-data-element-readers-paged",
    "scenario-effective-level-paged",
    "scenario-find-sixteen-names",
    "scenario-fits-two-verdicts",
    "scenario-impact-summary-bounded",
    "scenario-import-conflicts-reported",
    "scenario-level-list-paged",
    "scenario-sender-receiver-table-paged",
    "scenario-unconnected-one-component",
)


def pytest_sessionstart(session) -> None:
    """Warm the specimens this project reads once per pytest run, in the controller.

    Uses the `tools` conftest's ``warm_specimens``, so both trees warm the cache one way.
    """
    from conftest import warm_specimens  # noqa: PLC0415 - the tools tree's conftest, not this package's

    warm_specimens(session, MCP_SPECIMENS)


def _write_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n")


# The layer directories and element paths come off the kernel registry, never a restated list.


def _required_layer_directories():
    """The directories ``swcraft init`` creates: every non-optional layer's.

    Does not write the two hardware elements ``swcraft init`` adds: a suite that needs a model
    passing ``check-model`` seeds them itself.
    """
    return tuple(facts.storage.directory for facts in production_registry().layers if not facts.optional)


def _element_path(model_root, layer, identity, **placement):
    """Where one element of *layer* is stored, composed by the registry."""
    return Path(model_root) / production_registry().compose_path(layer, identity, placement or None)


# One element file, its directory derived from the registry by the payload's `kind` (or an
# explicit `layer`), so a moved layer directory needs no test edit. The same three definitions
# live in the `tools` and `agent` conftests (no suite can import another's) and
# `packages/tools/tests/test_model_element_helpers.py` holds the copies equal.
#
# Not reached: a file whose name is not its element's storage name, a malformed file without a
# `kind`, and a test whose subject is a directory itself (it spells it on purpose).


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


@pytest.fixture(autouse=True)
def _project_env(tmp_path, tmp_path_factory, monkeypatch):
    """Give every test its own project, home and fresh server state.

    Writes a minimal ``swcraft.yaml`` and one directory per required layer, sets
    ``SWCRAFT_MANIFEST``, points ``HOME``/``LOCALAPPDATA``/``USERPROFILE`` at a per-test
    directory, puts the real Node.js first on ``PATH``, resets ``_PROJECT`` (the server's only
    module state; re-assigning a removed name would put it back on the module) and installs no
    user guide.
    """
    model_dir = tmp_path / "model"
    for subdir in _required_layer_directories():
        (model_dir / subdir).mkdir(parents=True, exist_ok=True)

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = tmp_path / "swcraft.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            {
                "name": "test-project",
                "schema_version": "1.0",
                "model_root": "model",
                "dist_root": "dist",
                "views_root": "views",
            },
            default_flow_style=False,
            sort_keys=False,
        ),
        encoding="utf-8",
        newline="\n",
    )

    monkeypatch.setenv("SWCRAFT_MANIFEST", str(manifest_path))

    # The review mode and the scope channel are read at server start into module globals, which
    # `monkeypatch` does not revert: re-read them, or one `elicit` or scoped test would leak
    # into every following one.
    monkeypatch.delenv("SWCRAFT_REVIEW", raising=False)
    _server._configure_review_mode()

    monkeypatch.delenv("SWCRAFT_APPROVED_SCOPE", raising=False)
    monkeypatch.delenv("SWCRAFT_SCOPE_ID", raising=False)
    _server._configure_scope_channel()

    # A home per test: nothing reaches the real `~/.swcraft/reviews`, and under xdist one
    # test's sweep cannot delete another's pending records.
    user_home = os.fspath(tmp_path_factory.mktemp("user-home"))
    for variable in ("HOME", "LOCALAPPDATA", "USERPROFILE"):
        monkeypatch.setenv(variable, user_home)

    # The redirected home hides Node from a version manager's shim.
    if REAL_NODE_DIRECTORY is not None:
        monkeypatch.setenv("PATH", REAL_NODE_DIRECTORY + os.pathsep + os.environ.get("PATH", ""))

    _server._PROJECT = None

    # A test that installs no site gets `guide-missing`, never the checkout's last built payload.
    install_no_site(monkeypatch, tmp_path)


@pytest.fixture()
def model_root(tmp_path):
    """Populate the model directories with a minimal tree covering all element types; return its root.

    Both components set ``handle_termination_and_restart: no_support`` explicitly: the writer
    refuses an emitted component that sets none.
    """
    root = tmp_path / "model"

    # The connector belongs to the root composition and the writer resolves its endpoints to
    # members there, so without the members `validate`/`build` refuse the model.
    _write_yaml(
        _element_path(root, "compositions", "PowertrainCtrl"),
        {
            "name": "PowertrainCtrl",
            "title": "Powertrain Controller",
            "root": True,
            "members": [
                {"name": "SpeedSensorSWC", "type": "SpeedSensorSWC"},
                {"name": "SpeedCtrlSWC", "type": "SpeedCtrlSWC"},
            ],
        },
    )

    _write_yaml(
        model_element_path(root, "float32_base", kind="base_type"),
        {
            "name": "float32_base",
            "kind": "base_type",
            "size": 32,
            "encoding": "IEEE754",
            "native_declaration": "float",
        },
    )

    _write_yaml(
        model_element_path(root, "float32", kind="value_type"),
        {"name": "float32", "kind": "value_type", "base": "float32_base"},
    )

    _write_yaml(
        _element_path(root, "interfaces", "ISpeed"),
        {
            "name": "ISpeed",
            "kind": "sender_receiver",
            "data_elements": [{"name": "speed", "implementation_type": "float32"}],
        },
    )

    _write_yaml(
        _element_path(root, "swc", "SpeedSensorSWC", group="example"),
        {
            "name": "SpeedSensorSWC",
            "type": "application",
            "ports": [{"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}],
            "runnables": [],
            "handle_termination_and_restart": "no_support",
            "satisfies": ["REQ_001@1"],
        },
    )

    _write_yaml(
        _element_path(root, "swc", "SpeedCtrlSWC", group="example"),
        {
            "name": "SpeedCtrlSWC",
            "type": "application",
            "ports": [{"name": "rSpeed", "direction": "require", "interface": "ISpeed"}],
            "runnables": [],
            "handle_termination_and_restart": "no_support",
        },
    )

    _write_yaml(
        _element_path(root, "requirements", "REQ_001"),
        {
            "id": "REQ_001",
            "title": "Speed monitoring",
            "description": "The system shall monitor vehicle speed.",
            "req_type": "req",
            "version": 1,
            "source": "architecture",
        },
    )

    _write_yaml(
        _element_path(root, "connectors", "Speed_to_Ctrl"),
        {
            "name": "Speed_to_Ctrl",
            "from": {"swc": "SpeedSensorSWC", "port": "pSpeed"},
            "to": {"swc": "SpeedCtrlSWC", "port": "rSpeed"},
            "description": "Speed sensor to controller",
        },
    )

    return str(root)
