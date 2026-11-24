"""``model_state.single_read``: one load of a layer per block, and nothing outside one.

Its sections, in order: the block outside and inside, its life, its key and its context,
which are the six rules its docstring states; the observer; the tripwire's negative cases;
the CLI opener; the editing openers. The server's three openers are in
``packages/mcp/tests/test_single_read_tools.py``, the closed list of openers is the gate in
``test_single_read_gate.py``, and the byte differential is ``test_single_read_bytes.py``.

**What these tests do not reach.** They count ``parse_element``, so a read outside the
layer loader inside a block is not counted. What the tripwire itself does not reach is
``single_read_support.py``'s.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import threading
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import single_read_support
import yaml
from conftest import REPO_ROOT, _write_manifest
from node_support import requires_node
from single_read_support import read_twice
from swcraft_tools import cli
from swcraft_tools import model_state as ms
from swcraft_tools.auditor import AuditLog
from swcraft_tools.cli import dispatch
from swcraft_tools.cli.dispatch import build_parser
from swcraft_tools.editing import engine, passes, trace_candidates
from swcraft_tools.editing.audit import AUDIT_FILENAME, CHANGE_SET_REJECTED, CHANGE_SET_STAGED
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.engine import ChangeSetProposal, handle_request
from swcraft_tools.editing.staging import StagedChangeSet
from swcraft_tools.model_registry import ModelRegistry, production_registry
from swcraft_tools.model_state import ElementParseError, load_layer, load_model_state, single_read
from swcraft_tools.project import ManifestPathOverlapsModelError, load_project
from swcraft_tools.validator import check_cross_layer_consistency
from trace_models import tree

BASIC_MODEL = REPO_ROOT / "examples" / "basic" / "model"


@pytest.fixture
def model(tmp_path: Path) -> Path:
    """A private copy of the shipped example, so a test can edit a file of it."""
    root = tmp_path / "model"
    shutil.copytree(BASIC_MODEL, root)
    return root


@pytest.fixture
def registry() -> ModelRegistry:
    return production_registry()


@pytest.fixture
def parses(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Each file ``parse_element`` opens, in order: one entry is one read off the disk."""
    return single_read_support.parse_spy(monkeypatch, lambda file: file)


def _layer_size(root: Path, registry: ModelRegistry, layer: str) -> int:
    size = len(ms.scan_layer(root, layer, registry=registry).files)
    assert size, f"{layer}: the example has no file in this layer, so a count over it says nothing"
    return size


# ---------------------------------------------------------------------------
# Outside and inside a block
# ---------------------------------------------------------------------------


def test_no_block_reads_the_disk_each_time(model: Path, registry: ModelRegistry, parses: list[Path]) -> None:
    size = _layer_size(model, registry, "tasks")
    load_layer(model, "tasks", registry=registry)
    load_layer(model, "tasks", registry=registry)
    assert len(parses) == 2 * size


def test_a_second_load_in_a_block_reads_nothing(model: Path, registry: ModelRegistry, parses: list[Path]) -> None:
    size = _layer_size(model, registry, "tasks")
    with single_read():
        first = load_layer(model, "tasks", registry=registry)
        assert len(parses) == size
        second = load_layer(model, "tasks", registry=registry)
    assert len(parses) == size
    assert second == first


def test_every_loader_path_shares_one_memo(model: Path, registry: ModelRegistry, parses: list[Path]) -> None:
    """``load_model_state`` goes through ``_read_state``, ``load_layer`` does not: one memo answers both."""
    with single_read():
        load_model_state(model, registry=registry)
        once = len(parses)
        assert once
        load_model_state(model, registry=registry)
        for layer in registry.layer_names:
            load_layer(model, layer, registry=registry)
    assert len(parses) == once


def test_the_answer_equals_the_answer_of_no_block(model: Path, registry: ModelRegistry) -> None:
    """Order and placement included, on a file that is not named after its element.

    The rename is what makes the order the element order: a memo that stored the
    scan's file order would pass on a tree whose files all sit at their composed paths.
    """
    first = sorted((model / "swc").rglob("*.yaml"), key=Path.as_posix)[0]
    first.rename(first.with_name("zzz_not_the_element_name.yaml"))

    for layer in registry.layer_names:
        expected = ms.load_layer_with_placement(model, layer, registry)
        with single_read():
            assert ms.load_layer_with_placement(model, layer, registry) == expected
            assert ms.load_layer_with_placement(model, layer, registry) == expected
    assert any(placement for _, placement in ms.load_layer_with_placement(model, "swc", registry)[1])


def test_a_missing_layer_is_held_as_absent(
    model: Path, registry: ModelRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.rmtree(model / "hardware")
    loads: list[str] = []
    real = ms._parse_layer

    def counting(root: Any, layer: str, of: ModelRegistry) -> Any:
        loads.append(layer)
        return real(root, layer, of)

    monkeypatch.setattr(ms, "_parse_layer", counting)
    with single_read():
        assert ms.load_layer_with_placement(model, "hardware", registry) == (False, [])
        assert ms.load_layer_with_placement(model, "hardware", registry) == (False, [])
        assert loads == ["hardware"], "the second call read again, so the absence was not held"
        assert len(ms._SINGLE_READ.get()) == 1


def test_a_callers_list_is_its_own_and_the_elements_are_shared(model: Path, registry: ModelRegistry) -> None:
    with single_read():
        _, pairs = ms.load_layer_with_placement(model, "tasks", registry)
        expected = list(pairs)
        pairs.clear()
        elements = load_layer(model, "tasks", registry=registry)
        again = load_layer(model, "tasks", registry=registry)
        assert [element for element, _ in expected] == elements
        elements.clear()
        assert load_layer(model, "tasks", registry=registry) == again
        assert all(a is b for a, b in zip(again, (element for element, _ in expected), strict=True))


# ---------------------------------------------------------------------------
# The block's life
# ---------------------------------------------------------------------------


def test_no_block_is_open_by_default() -> None:
    assert ms._SINGLE_READ.get() is None


def test_a_block_is_empty_on_entry_and_dropped_on_exit(
    model: Path, registry: ModelRegistry, parses: list[Path]
) -> None:
    size = _layer_size(model, registry, "tasks")
    with single_read():
        assert ms._SINGLE_READ.get() == {}
        load_layer(model, "tasks", registry=registry)
    assert ms._SINGLE_READ.get() is None
    with single_read():
        assert ms._SINGLE_READ.get() == {}
        load_layer(model, "tasks", registry=registry)
    assert len(parses) == 2 * size


def test_a_block_is_dropped_on_an_exception(model: Path, registry: ModelRegistry) -> None:
    class Boom(Exception):
        pass

    with pytest.raises(Boom), single_read():
        load_layer(model, "tasks", registry=registry)
        raise Boom
    assert ms._SINGLE_READ.get() is None


def test_an_edit_between_two_blocks_is_seen(model: Path, registry: ModelRegistry) -> None:
    file = ms.scan_layer(model, "tasks", registry=registry).files[0].path
    with single_read():
        before = load_layer(model, "tasks", registry=registry)
    file.unlink()
    with single_read():
        after = load_layer(model, "tasks", registry=registry)
    assert len(after) == len(before) - 1


def test_a_nested_block_starts_empty_and_restores_the_outer(
    model: Path, registry: ModelRegistry, parses: list[Path]
) -> None:
    tasks = _layer_size(model, registry, "tasks")
    swc = _layer_size(model, registry, "swc")
    with single_read():
        load_layer(model, "tasks", registry=registry)
        outer = ms._SINGLE_READ.get()
        with single_read():
            assert ms._SINGLE_READ.get() == {}
            load_layer(model, "tasks", registry=registry)
            load_layer(model, "swc", registry=registry)
        assert ms._SINGLE_READ.get() is outer
        assert len(parses) == 2 * tasks + swc
        load_layer(model, "tasks", registry=registry)
        assert len(parses) == 2 * tasks + swc, "the outer block lost what it held"
        load_layer(model, "swc", registry=registry)
        assert len(parses) == 2 * tasks + 2 * swc, "the inner block's memo reached the outer one"


def test_a_failed_load_is_not_held_and_raises_the_same_message_twice(
    model: Path, registry: ModelRegistry, parses: list[Path]
) -> None:
    broken = model / "tasks" / "Broken.yaml"
    broken.write_text("kind: [", encoding="utf-8", newline="\n")
    with single_read():
        with pytest.raises(ElementParseError) as first:
            load_layer(model, "tasks", registry=registry)
        reads = len(parses)
        with pytest.raises(ElementParseError) as second:
            load_layer(model, "tasks", registry=registry)
        assert len(parses) > reads, "the second call answered without reading"
        assert str(second.value) == str(first.value)
        broken.unlink()
        assert load_layer(model, "tasks", registry=registry), "the refusal was held past the file's repair"


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------


def test_another_root_misses(model: Path, tmp_path: Path, registry: ModelRegistry, parses: list[Path]) -> None:
    size = _layer_size(model, registry, "tasks")
    other = tmp_path / "other"
    shutil.copytree(model, other)
    (other / "tasks" / ms.scan_layer(model, "tasks", registry=registry).files[0].path.name).unlink()
    with single_read():
        here = load_layer(model, "tasks", registry=registry)
        there = load_layer(other, "tasks", registry=registry)
    assert len(there) == len(here) - 1
    assert len(parses) == 2 * size - 1


def test_another_layer_misses(model: Path, registry: ModelRegistry, parses: list[Path]) -> None:
    tasks = _layer_size(model, registry, "tasks")
    swc = _layer_size(model, registry, "swc")
    with single_read():
        load_layer(model, "tasks", registry=registry)
        load_layer(model, "swc", registry=registry)
    assert len(parses) == tasks + swc


def test_another_registry_misses(model: Path, registry: ModelRegistry, parses: list[Path]) -> None:
    """A registry is a key by identity: an equal copy is another contract as far as the memo knows."""
    size = _layer_size(model, registry, "tasks")
    copy = dataclasses.replace(registry)
    assert copy is not registry
    with single_read():
        load_layer(model, "tasks", registry=registry)
        load_layer(model, "tasks", registry=copy)
    assert len(parses) == 2 * size


def test_relative_and_absolute_spellings_of_one_root_hit(
    model: Path, registry: ModelRegistry, parses: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    size = _layer_size(model, registry, "tasks")
    monkeypatch.chdir(model.parent)
    with single_read():
        load_layer("model", "tasks", registry=registry)
        load_layer(model, "tasks", registry=registry)
        load_layer(str(model), "tasks", registry=registry)
        load_layer(Path("model"), "tasks", registry=registry)
        load_layer("./model/../model", "tasks", registry=registry)
    assert len(parses) == size


# ---------------------------------------------------------------------------
# The context
# ---------------------------------------------------------------------------


def test_a_thread_started_in_a_block_reads_the_disk(model: Path, registry: ModelRegistry, parses: list[Path]) -> None:
    size = _layer_size(model, registry, "tasks")
    seen: list[object] = []

    def worker() -> None:
        seen.append(ms._SINGLE_READ.get())
        load_layer(model, "tasks", registry=registry)

    with single_read():
        load_layer(model, "tasks", registry=registry)
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
    assert seen == [None]
    assert len(parses) == 2 * size


# ---------------------------------------------------------------------------
# The observer: the seam the tripwire stands on
# ---------------------------------------------------------------------------


@pytest.fixture
def observed(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """A copy of each memo the observer was handed."""
    calls: list[dict] = []
    monkeypatch.setattr(ms, "_single_read_observer", lambda held: calls.append(dict(held)))
    return calls


def test_the_observer_sees_what_the_block_held(model: Path, registry: ModelRegistry, observed: list[dict]) -> None:
    with single_read():
        load_layer(model, "tasks", registry=registry)
    assert len(observed) == 1
    assert [(key[1], key[2]) for key in observed[0]] == [("tasks", registry)]
    assert ms._SINGLE_READ.get() is None


def test_the_observer_runs_on_an_exception_too(model: Path, registry: ModelRegistry, observed: list[dict]) -> None:
    class Boom(Exception):
        pass

    with pytest.raises(Boom), single_read():
        load_layer(model, "tasks", registry=registry)
        raise Boom
    assert len(observed) == 1
    assert ms._SINGLE_READ.get() is None


def test_single_read_is_public() -> None:
    assert "single_read" in ms.__all__


# ---------------------------------------------------------------------------
# The tripwire: what fails a test, and what does not
# ---------------------------------------------------------------------------


def _changed_a_shared_element(root: Path, registry: ModelRegistry) -> None:
    """What a consumer must never do to a layer it read in a block: ``mutations.py``'s kind of edit."""
    next(swc for swc in load_layer(root, "swc", registry=registry) if swc.ports).ports.clear()


def test_every_test_of_this_tree_runs_under_the_tripwire() -> None:
    assert single_read_support.armed()


def test_a_mutated_element_fails(model: Path, registry: ModelRegistry) -> None:
    with pytest.raises(pytest.fail.Exception, match=r"swc of .*ExampleConsumerSWC"), single_read_support.watching():
        with single_read():
            _changed_a_shared_element(model, registry)


def test_a_file_rewritten_inside_a_block_fails(model: Path, registry: ModelRegistry) -> None:
    file = model / "tasks" / "Task_10ms.yaml"
    text = file.read_text(encoding="utf-8")
    assert "priority: 5" in text
    with pytest.raises(pytest.fail.Exception, match=r"tasks of .*Task_10ms"), single_read_support.watching():
        with single_read():
            load_layer(model, "tasks", registry=registry)
            file.write_text(text.replace("priority: 5", "priority: 6"), encoding="utf-8", newline="\n")


def test_a_file_broken_inside_a_block_fails_and_raises_nothing_at_the_exit(
    model: Path, registry: ModelRegistry
) -> None:
    """A fresh load that raises is a difference, and the observer must not raise it itself."""
    file = model / "tasks" / "Task_10ms.yaml"
    with pytest.raises(pytest.fail.Exception, match=r"tasks of .*ElementParseError"), single_read_support.watching():
        with single_read():
            load_layer(model, "tasks", registry=registry)
            file.write_text("kind: [", encoding="utf-8", newline="\n")
        assert ms._SINGLE_READ.get() is None


def test_a_hit_inside_swallowing_code_still_fails(model: Path, registry: ModelRegistry) -> None:
    """``_run_with_error_shape``'s shape: everything is caught, and an error answer goes back."""

    def swallowing() -> dict[str, str]:
        try:
            with single_read():
                _changed_a_shared_element(model, registry)
                raise ValueError("the tool's own failure")
        except Exception as error:  # noqa: BLE001 - the shape under test
            return {"error": str(error)}

    with pytest.raises(pytest.fail.Exception, match=r"swc of "), single_read_support.watching():
        assert swallowing() == {"error": "the tool's own failure"}


def test_a_root_gone_by_the_exit_is_a_difference_never_a_pass(model: Path, registry: ModelRegistry) -> None:
    with pytest.raises(pytest.fail.Exception, match=r"tasks of .*a fresh load differs"), single_read_support.watching():
        with single_read():
            load_layer(model, "tasks", registry=registry)
            shutil.rmtree(model)


def test_a_root_that_never_was_held_nothing_to_differ_from(tmp_path: Path, registry: ModelRegistry) -> None:
    """Absent at the load and absent at the exit: the composer on a missing root is such a block."""
    with single_read_support.watching(), single_read():
        assert load_layer(tmp_path / "no-model", "tasks", registry=registry) == []


def test_a_clean_block_passes_and_the_comparison_is_counted_by_no_spy(
    model: Path, registry: ModelRegistry, parses: list[Path]
) -> None:
    size = _layer_size(model, registry, "tasks")
    with single_read_support.watching(), single_read():
        load_layer(model, "tasks", registry=registry)
    assert len(parses) == size
    assert ms.parse_element is not single_read_support._UNPATCHED_PARSE_ELEMENT, "the spy was put back"


def test_the_fixture_fails_a_test_at_its_teardown(model: Path, tmp_path: Path) -> None:
    """The real fixture under a real pytest: the body passes, its swallowed hit errors at teardown."""
    tree = tmp_path / "run"
    tree.mkdir()
    (tree / "conftest.py").write_text(
        "from single_read_support import single_read_tripwire  # noqa: F401\n", encoding="utf-8", newline="\n"
    )
    (tree / "test_hit.py").write_text(
        "from swcraft_tools.model_state import load_layer, single_read\n"
        "\n"
        "def test_hit():\n"
        "    try:\n"
        "        with single_read():\n"
        f"            layer = load_layer({os.fspath(model)!r}, 'swc')\n"
        "            next(swc for swc in layer if swc.ports).ports.clear()\n"
        "            raise ValueError('swallowed')\n"
        "    except ValueError:\n"
        "        pass\n",
        encoding="utf-8",
        newline="\n",
    )
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "test_hit.py", "-q", "-p", "no:cacheprovider"],
        cwd=tree,
        env={**os.environ, "PYTHONPATH": os.fspath(Path(single_read_support.__file__).parent)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert run.returncode == 1, run.stdout + run.stderr
    assert "1 passed, 1 error" in run.stdout, run.stdout
    assert "swc of " in run.stdout, run.stdout


# ---------------------------------------------------------------------------
# The CLI opener
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A private copy of the shipped example's project: the manifest and the model."""
    root = tmp_path / "project"
    root.mkdir()
    shutil.copy(BASIC_MODEL.parent / "swcraft.yaml", root / "swcraft.yaml")
    shutil.copytree(BASIC_MODEL, root / "model")
    return root


def _main(monkeypatch: pytest.MonkeyPatch, *argv: str | Path) -> int:
    """``cli.main`` on *argv*, in this process: its exit code, 0 when it returned."""
    monkeypatch.setattr("sys.argv", ["swcraft", *map(os.fspath, argv)])
    try:
        cli.main()
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else 1
    return 0


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(("check-model",), id="check-model"),
        pytest.param(("validate",), id="validate"),
        pytest.param(("generate", "arxml"), id="generate-arxml"),
        pytest.param(("generate", "needs"), id="generate-needs"),
        pytest.param(("generate", "diagrams"), id="generate-diagrams"),
        pytest.param(("resource-report",), id="resource-report"),
    ],
)
def test_an_opener_command_reads_each_file_once_and_only_in_its_block(
    project: Path,
    reads: list[tuple[Path, bool]],
    monkeypatch: pytest.MonkeyPatch,
    argv: tuple[str, ...],
) -> None:
    if argv == ("generate", "diagrams"):
        requires_node()
    assert _main(monkeypatch, *argv, "--project", project / "swcraft.yaml") == 0
    assert reads, "the command read nothing, so the count says nothing"
    assert read_twice(reads) == {}
    assert all(in_block for _, in_block in reads), "a read ran outside the block"


def test_check_model_reads_every_file_of_the_model_once(
    project: Path, reads: list[tuple[Path, bool]], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _main(monkeypatch, "check-model", "--project", project / "swcraft.yaml") == 0
    expected = {file.resolve() for file in (project / "model").rglob("*.yaml")}
    assert {file.resolve() for file, _ in reads} == expected
    assert len(reads) == len(expected)


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(("generate", "arxml", "--component", "ExampleConsumerSWC"), id="component"),
        pytest.param(("generate", "arxml", "--all-components", "--output-dir", "{out}"), id="all-components"),
        pytest.param(("query", "swc"), id="query"),
        pytest.param(("impact", "ExampleConsumerSWC"), id="impact"),
        pytest.param(("list", "swc"), id="list"),
    ],
)
def test_a_command_off_the_list_reads_with_no_block(
    project: Path,
    reads: list[tuple[Path, bool]],
    monkeypatch: pytest.MonkeyPatch,
    argv: tuple[str, ...],
) -> None:
    written = [word.format(out=project / "exports") for word in argv]
    assert _main(monkeypatch, *written, "--project", project / "swcraft.yaml") == 0
    assert reads, "the command read nothing, so it says nothing of a block"
    assert not any(in_block for _, in_block in reads)


@pytest.mark.parametrize(
    ("argv", "handler", "opened"),
    [
        pytest.param(("check-model",), "cmd_check_model", True, id="check-model"),
        pytest.param(("validate",), "cmd_validate", True, id="validate"),
        pytest.param(("resource-report",), "cmd_resource_report", True, id="resource-report"),
        pytest.param(("generate", "arxml"), "cmd_generate", True, id="generate-arxml"),
        pytest.param(("generate", "diagrams"), "cmd_generate", True, id="generate-diagrams"),
        pytest.param(("generate", "needs"), "cmd_generate", True, id="generate-needs"),
        pytest.param(("generate", "arxml", "--component", "A"), "cmd_generate", False, id="component"),
        pytest.param(("generate", "arxml", "--all-components"), "cmd_generate", False, id="all-components"),
        pytest.param(("query", "swc"), "cmd_query", False, id="query"),
        pytest.param(("impact", "A"), "cmd_impact", False, id="impact"),
        pytest.param(("list", "swc"), "cmd_list", False, id="list"),
        pytest.param(("diff",), "cmd_diff", False, id="diff"),
        pytest.param(("change-report",), "cmd_change_report", False, id="change-report"),
        pytest.param(("experiment", "allocation"), "cmd_experiment", False, id="experiment-allocation"),
        pytest.param(("experiment", "trace"), "cmd_experiment", False, id="experiment-trace"),
    ],
)
def test_the_dispatch_enters_a_handler_in_the_block_of_its_command(
    monkeypatch: pytest.MonkeyPatch, argv: tuple[str, ...], handler: str, opened: bool
) -> None:
    """What the handler is entered with.

    ``change-report`` and ``experiment`` need a git history or a solver to read at all, and the question here is the
    dispatch's. That no other command opens one is the next test's."""
    entered: list[bool] = []
    monkeypatch.setattr(dispatch, handler, lambda _args: entered.append(ms._SINGLE_READ.get() is not None))
    assert _main(monkeypatch, *argv) == 0
    assert entered == [opened]
    assert ms._SINGLE_READ.get() is None


def _subcommands() -> list[str]:
    """Every subcommand the parser declares, so that a new one is a row of the next test unasked."""
    (subparsers,) = (
        action
        for action in build_parser()._actions  # noqa: SLF001 - argparse has no public listing
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
    )
    return sorted(subparsers.choices)


#: The commands ``_single_read_for`` opens a block for; ``generate`` without a component flag.
BLOCK_COMMANDS = frozenset({"check-model", "validate", "resource-report", "generate"})


@pytest.mark.parametrize("command", _subcommands())
def test_a_block_is_opened_for_the_list_and_for_nothing_else(command: str) -> None:
    """A block is opened for the listed commands and for nothing else.

    ``import`` writes the model, so a word added to the set would break rule 2, and the opener gate would not see it:
    the gate counts the two calls in ``_single_read_for`` and not their reach."""
    args = argparse.Namespace(command=command, component=None, all_components=False)

    is_a_block = not isinstance(dispatch._single_read_for(args), contextlib.nullcontext)

    assert is_a_block is (command in BLOCK_COMMANDS)


def test_the_list_names_real_subcommands() -> None:
    assert BLOCK_COMMANDS <= set(_subcommands())


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(("check-model", "--no-such-flag"), id="unknown-flag"),
        pytest.param(("generate", "pdf"), id="unknown-format"),
    ],
)
def test_a_usage_error_opens_no_block(monkeypatch: pytest.MonkeyPatch, argv: tuple[str, ...]) -> None:
    opened: list[None] = []
    real = ms.single_read

    def counting() -> Any:
        opened.append(None)
        return real()

    monkeypatch.setattr(ms, "single_read", counting)
    assert _main(monkeypatch, *argv) == 2
    assert opened == []


def test_a_run_opens_its_block_through_the_patched_name(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The usage-error test's positive twin, through the same patch.

    A run that reaches the dispatch asks the patched name, so the refusal above cannot pass because the CLI never looks
    the name up."""
    opened: list[None] = []
    real = ms.single_read

    def counting() -> Any:
        opened.append(None)
        return real()

    monkeypatch.setattr(ms, "single_read", counting)
    assert _main(monkeypatch, "check-model", "--project", project / "swcraft.yaml") == 0
    assert opened == [None]


def test_a_refused_run_leaves_no_block_open(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``sys.exit`` inside the dispatch goes through the block's ``finally``."""
    (project / "model" / "tasks" / "Broken.yaml").write_text("kind: [", encoding="utf-8", newline="\n")
    assert _main(monkeypatch, "check-model", "--project", project / "swcraft.yaml") == 1
    assert ms._SINGLE_READ.get() is None


def test_two_runs_in_one_process_see_the_edit_between_them(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = project / "swcraft.yaml"
    assert _main(monkeypatch, "check-model", "--project", manifest) == 0
    capsys.readouterr()

    task = project / "model" / "tasks" / "Task_10ms.yaml"
    text = task.read_text(encoding="utf-8")
    assert "partition: PARTITION0" in text
    task.write_text(text.replace("PARTITION0", "NoSuchPartition"), encoding="utf-8", newline="\n")

    assert _main(monkeypatch, "check-model", "--project", manifest) == 1
    assert "NoSuchPartition" in capsys.readouterr().err


def _rules(root: Path) -> Counter[str]:
    result = check_cross_layer_consistency(os.fspath(root))
    return Counter(issue.rule for issue in (*result.records, *result.warnings))


def test_two_task_files_of_one_name_stay_two_owners_in_a_block(model: Path) -> None:
    """``validator._split_identities`` tells owners by ``id(task)``, and a block shares elements.

    Two files of one task name in two partitions: the finding exists only because the two are
    two objects, and the composer's second read inside the block answers from the memo.
    """
    partition = yaml.safe_load((model / "hardware" / "PARTITION0.yaml").read_text(encoding="utf-8"))
    partition["name"] = "PARTITION1"
    (model / "hardware" / "PARTITION1.yaml").write_text(yaml.safe_dump(partition), encoding="utf-8", newline="\n")
    twin = yaml.safe_load((model / "tasks" / "Task_10ms.yaml").read_text(encoding="utf-8"))
    twin["partition"] = "PARTITION1"
    (model / "tasks" / "Task_10ms~1.yaml").write_text(yaml.safe_dump(twin), encoding="utf-8", newline="\n")

    expected = _rules(model)
    assert expected["task.instance-in-multiple-partitions"] >= 1
    assert expected["task.duplicate-name"] >= 1
    with single_read():
        assert _rules(model) == expected
        assert _rules(model) == expected


@pytest.mark.parametrize(
    ("model_root", "dist_root"),
    [
        pytest.param("model", "model/dist", id="dist-under-model"),
        pytest.param("dist/model", "dist", id="model-under-dist"),
    ],
)
def test_no_build_writes_under_the_model_the_block_holds(tmp_path: Path, model_root: str, dist_root: str) -> None:
    """``validate`` and ``generate`` write inside a block, and rule 2 holds because it is never here."""
    _write_manifest(
        tmp_path,
        {
            "name": "overlap",
            "schema_version": "1.0",
            "model_root": model_root,
            "dist_root": dist_root,
            "views_root": "views",
            "outputs": {"arxml": "out/model.arxml", "needs": "out/needs.json"},
        },
    )
    with pytest.raises(ManifestPathOverlapsModelError, match="dist_root"):
        load_project(os.fspath(tmp_path))


# ---------------------------------------------------------------------------
# The editing openers
# ---------------------------------------------------------------------------


_NEW_DESCRIPTION = {
    "operation": "update",
    "address": {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "description"}]},
    "value": "Rewritten",
}


def _misspelt_interface(name: str = "IExampl") -> dict[str, Any]:
    """An operation pointing the consumer's one port at *name*: only pass D can refuse it."""
    path = [{"field": "ports"}, {"member": "rIn"}, {"field": "interface"}]
    address = {"layer": "swc", "element": "ExampleConsumerSWC", "path": path}
    return {"operation": "update", "address": address, "value": name}


def _approve(proposal: ChangeSetProposal) -> ApprovalDecision:
    return ApprovalDecision(change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True)


def _baseline(project: Path) -> str:
    return handle_request({"operation": "discover", "form": "baseline"}, project=project)["baseline"]


def _change_set(project: Path, *operations: dict[str, Any], review: Any = _approve, **fields: Any) -> dict[str, Any]:
    request = {"operation": "change_set", "baseline": _baseline(project), "operations": list(operations), **fields}
    return handle_request(request, project=project, review=review)


def _element(project: Path, name: str) -> dict[str, Any]:
    request = {"operation": "read", "address": {"layer": "swc", "element": name}}
    return handle_request(request, project=project)["value"]


@pytest.fixture
def memos_at_pass_d(monkeypatch: pytest.MonkeyPatch) -> list[ms._Held | None]:
    """What each call of pass D's composer found open: the memo itself, or ``None``."""
    seen: list[ms._Held | None] = []
    real = passes.check_cross_layer_consistency

    def spying(model_root: str, arxml_version: str | None = None) -> Any:
        seen.append(ms._SINGLE_READ.get())
        return real(model_root, arxml_version)

    monkeypatch.setattr(passes, "check_cross_layer_consistency", spying)
    return seen


def _plant(model_root: Path, relative: str, text: str) -> None:
    planted = model_root / relative
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(text, encoding="utf-8", newline="\n")


def _plant_after_materialize(monkeypatch: pytest.MonkeyPatch, relative: str, text: str) -> list[str]:
    """Add a file to the staged tree once it is materialized, so that pass D is what meets it.

    Returns the staged model roots planted in, filled as the change set runs.
    """
    real = StagedChangeSet.materialize
    staged_roots: list[str] = []

    def planting(self: StagedChangeSet, *args: Any, **kwargs: Any) -> Any:
        answer = real(self, *args, **kwargs)
        staged_roots.append(os.fspath(self.model_root))
        _plant(Path(self.model_root), relative, text)
        return answer

    monkeypatch.setattr(StagedChangeSet, "materialize", planting)
    return staged_roots


def test_a_committed_change_set_is_read_back_in_one_process(project: Path) -> None:
    assert _element(project, "ExampleSWC")["description"] != "Rewritten"
    assert _change_set(project, _NEW_DESCRIPTION)["status"] == "committed"
    assert _element(project, "ExampleSWC")["description"] == "Rewritten"


def test_a_change_set_refused_at_pass_d_leaves_nothing_for_the_corrected_one(
    project: Path, memos_at_pass_d: list[ms._Held | None]
) -> None:
    refused = _change_set(project, _NEW_DESCRIPTION, _misspelt_interface())
    assert refused["error"] == "broken-reference"
    assert _element(project, "ExampleSWC")["description"] != "Rewritten"

    assert _change_set(project, _NEW_DESCRIPTION)["status"] == "committed"
    assert _element(project, "ExampleSWC")["description"] == "Rewritten"
    assert _element(project, "ExampleConsumerSWC")["ports"][0]["interface"] == "IExample"

    first, second = memos_at_pass_d
    assert first is not None
    assert second is not None
    assert first is not second, "the corrected staged tree shares the refused one's memo"


def test_the_completed_second_pass_validates_the_completed_tree_in_a_block_of_its_own(
    project: Path, memos_at_pass_d: list[ms._Held | None]
) -> None:
    model = project / "model"
    (model / "data_types/application/Temp_C.yaml").write_text(
        "name: Temp_C\nkind: application_type\n", encoding="utf-8", newline="\n"
    )
    (model / "interfaces/ITemp.yaml").write_text(
        "name: ITemp\nkind: sender_receiver\ndata_elements:\n  - name: t\n    application_type: Temp_C\n",
        encoding="utf-8",
        newline="\n",
    )
    (model / "data_types/mapping_sets/Thermal.yaml").write_text(
        "name: Thermal\nmaps:\n  - application: Temp_C\n    implementation: float32\n",
        encoding="utf-8",
        newline="\n",
    )
    logger = {
        "name": "Logger",
        "group": "example",
        "type": "application",
        "ports": [{"name": "rTemp", "direction": "require", "interface": "ITemp"}],
        "memory": {"RAM0": 0},
        "handle_termination_and_restart": "no_support",
    }
    create = {"operation": "create", "address": {"layer": "swc", "element": "Logger"}, "value": logger}
    answer = _change_set(project, create, complete=True)
    assert answer["status"] == "committed"
    assert answer["delta"][0]["after"]["data_type_mappings"] == ["Thermal"]

    first, second = memos_at_pass_d
    assert first is not None
    assert second is not None
    assert first is not second, "complete: true's second pass reused the first pass's memo"
    assert ms._SINGLE_READ.get() is None


def test_nothing_but_pass_d_runs_in_a_block(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The baseline, ``materialize``, the commit and each protocol read see no memo."""
    open_at: dict[str, bool] = {}

    def observed(owner: Any, name: str) -> None:
        real = getattr(owner, name)

        def observing(*args: Any, **kwargs: Any) -> Any:
            open_at[name] = open_at.get(name, False) or ms._SINGLE_READ.get() is not None
            return real(*args, **kwargs)

        monkeypatch.setattr(owner, name, observing)

    observed(StagedChangeSet, "materialize")
    observed(StagedChangeSet, "commit")
    observed(engine, "stage")
    observed(engine, "read")
    observed(engine, "discover")
    observed(engine, "resolve_names")

    handle_request({"operation": "find", "names": ["ExampleSWC"]}, project=project)
    _element(project, "ExampleSWC")
    assert _change_set(project, _NEW_DESCRIPTION)["status"] == "committed"
    assert open_at == dict.fromkeys(("materialize", "commit", "stage", "read", "discover", "resolve_names"), False)


@pytest.mark.parametrize(
    "operations",
    [
        pytest.param((_NEW_DESCRIPTION,), id="valid"),
        pytest.param((_misspelt_interface(),), id="refused-with-a-suggestion"),
    ],
)
def test_pass_d_reads_each_staged_file_once(
    project: Path,
    reads: list[tuple[Path, bool]],
    monkeypatch: pytest.MonkeyPatch,
    operations: tuple[dict[str, Any], ...],
) -> None:
    """The nearest-name suggestion's snapshot comes from the same memo as the composer's reads."""
    snapshots: list[str] = []
    real = passes.load_snapshot

    def counting(model_root: str) -> Any:
        snapshots.append(model_root)
        return real(model_root)

    monkeypatch.setattr(passes, "load_snapshot", counting)
    baseline = _baseline(project)
    del reads[:]
    answer = handle_request(
        {"operation": "change_set", "baseline": baseline, "operations": list(operations)}, project=project
    )
    refused = "error" in answer
    assert ("nearest: IExample" in answer.get("message", "")) is refused
    assert len(snapshots) == int(refused), "the suggestion's snapshot is what this case is for"

    in_block = [file for file, opened in reads if opened]
    assert in_block, "pass D read nothing inside a block"
    assert len(in_block) == len(set(in_block)), "a staged file was read twice in pass D's block"
    assert len(in_block) == len(list((project / "model").rglob("*.yaml")))


def test_an_element_parse_error_out_of_pass_d_reaches_the_refusal_and_closes_the_block(
    project: Path, monkeypatch: pytest.MonkeyPatch, memos_at_pass_d: list[ms._Held | None]
) -> None:
    _plant_after_materialize(monkeypatch, "hardware/Broken.yaml", "name: [\n")
    answer = _change_set(project, _NEW_DESCRIPTION)
    assert answer["error"] == "consistency-violation"
    assert "'hardware' element 'Broken'" in answer["message"]
    assert memos_at_pass_d[0] is not None, "the parse failure was not met inside the block"
    assert ms._SINGLE_READ.get() is None


#: A file in a directory the contract retired, as (model-root-relative path, text).
_RETIRED_FILE = ("data_types/Retired.yaml", "name: Retired\n")


def _plant_in_the_real_model_and_discover(project: Path) -> dict[str, Any]:
    """Leave the file in the model itself, for good, and return what the request's first check answers."""
    _plant(project / "model", *_RETIRED_FILE)
    return handle_request({"operation": "discover", "form": "baseline"}, project=project)


def test_a_retired_storage_error_out_of_pass_d_reaches_the_refusal_and_closes_the_block(
    project: Path, monkeypatch: pytest.MonkeyPatch, memos_at_pass_d: list[ms._Held | None]
) -> None:
    """The engine refuses a retired layout at the request's door (``_refuse_retired_layout``).

    So pass D meets one only in a staged tree that gained it after the door: here a planted
    file. The answer is the door's: the code, no address, the kernel's own sentence.
    """
    _plant_after_materialize(monkeypatch, *_RETIRED_FILE)
    answer = _change_set(project, _NEW_DESCRIPTION)
    assert answer["error"] == "consistency-violation"
    assert answer["message"].startswith("The model keeps its elements in a retired layout: 1 element file")
    assert "addresses" not in answer
    assert answer == _plant_in_the_real_model_and_discover(project)
    assert memos_at_pass_d[0] is not None, "the refusal was not met inside the block"
    assert ms._SINGLE_READ.get() is None


def test_the_retired_storage_answer_of_pass_d_names_no_location_and_changes_no_file(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = tree(project / "model")
    staged_roots = _plant_after_materialize(monkeypatch, *_RETIRED_FILE)
    answer = json.dumps(_change_set(project, _NEW_DESCRIPTION))
    assert staged_roots, "nothing was materialized, so this says nothing about the answer"
    for root in (*staged_roots, project, project.resolve()):
        for spelling in (os.fspath(root), Path(root).as_posix()):
            assert json.dumps(spelling)[1:-1] not in answer  # as the wire spells it: a backslash doubles
    assert tree(project / "model") == before
    assert not [root for root in staged_roots if Path(root).exists()], "a staged tree outlived the refusal"


@pytest.mark.parametrize(
    ("review", "fields"),
    [(_approve, {}), (None, {}), (_approve, {"complete": True}), (None, {"complete": True})],
    ids=["reviewed", "no-review", "reviewed-complete", "no-review-complete"],
)
def test_the_retired_storage_refusal_of_pass_d_leaves_a_rejection_on_the_real_log(
    project: Path, monkeypatch: pytest.MonkeyPatch, review: Any, fields: dict[str, Any]
) -> None:
    """A review never runs and ``complete: true`` looks for no repairs: the refusal leaves pass D first."""
    repaired: list[object] = []
    monkeypatch.setattr(engine, "_completion_operations", lambda *args: repaired.append(args))
    _plant_after_materialize(monkeypatch, *_RETIRED_FILE)
    answer = _change_set(project, _NEW_DESCRIPTION, review=review, **fields)
    records = AuditLog(str(project / "dist" / AUDIT_FILENAME)).get_entries()
    last = records[-1]
    assert last["operation"] == CHANGE_SET_REJECTED
    assert last["args"]["error"] == "consistency-violation"
    assert last["args"]["addresses"] == []
    assert CHANGE_SET_STAGED not in [record["operation"] for record in records]
    assert repaired == []
    assert answer == _plant_in_the_real_model_and_discover(project)


def test_candidates_for_reads_each_file_once_in_the_composer_and_nothing_else_in_a_block(
    project: Path, reads: list[tuple[Path, bool]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The block holds the composer call: the snapshot before it and the search after it stand outside."""
    open_at: dict[str, bool] = {}

    def observed(name: str) -> None:
        real = getattr(trace_candidates, name)

        def observing(*args: Any, **kwargs: Any) -> Any:
            open_at[name] = ms._SINGLE_READ.get() is not None
            return real(*args, **kwargs)

        monkeypatch.setattr(trace_candidates, name, observing)

    for name in ("check_cross_layer_consistency", "read_decisions", "build_index"):
        observed(name)

    assert handle_request({"operation": "find", "for_requirement": "REQ_EXAMPLE_001"}, project=project)["response"] == (
        "find"
    )
    assert open_at == {"check_cross_layer_consistency": True, "read_decisions": False, "build_index": False}

    in_block = [file for file, opened in reads if opened]
    assert len(in_block) == len(set(in_block)), "the composer read a file twice in its block"
    assert len(in_block) == len(list((project / "model").rglob("*.yaml")))
    assert any(not opened for _, opened in reads), "the request's own snapshot moved into the block"
    assert ms._SINGLE_READ.get() is None
