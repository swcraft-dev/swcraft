"""The Resources view's recorded documents: what the CLI writes, the TypeScript side reads.

Each file under ``vscode_extension/src/fixtures/resources/`` is the stdout of
``swcraft resource-report --format json``, which ``resourceCore.test.ts`` parses
and draws; the tests here run the CLI afresh and hold each committed file equal to it.

- ``basic.json`` -- ``examples/basic``, no fit question;
- ``fit-core.json`` -- ``examples/basic`` asked ``--fit-utilization 0.05``;
- ``over-budget.json`` -- a strict core over its threshold (an ``error``
  finding), an ``advisory`` memory over its own (an ``info`` finding), and a task
  in an undeclared partition (a topology record);
- ``unknown.json`` -- a strict core and a strict memory each with a consumer that
  declared nothing, the memory with three declared zeros;
- ``fit-memory.json`` -- five memories asked ``--fit-bytes 12288``, one for each
  of the five answers ``fit_verdicts`` states.

The synthetic models are built into a scratch directory the documents never
name. They are **change detectors, never correctness proofs**:
regenerate them deliberately, after reading the difference, with
``python packages/tools/tests/test_resource_panel_fixtures.py`` -- never to make a red run
green.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import REPO_ROOT, _core, _memory, _processor, _swc, _task, _write_manifest
from swcraft_tools import resource_report
from swcraft_tools.cli import main

FIXTURES = REPO_ROOT / "vscode_extension" / "src" / "fixtures" / "resources"
BASIC_MANIFEST = REPO_ROOT / "examples" / "basic" / "swcraft.yaml"

#: The five memories of ``fit-memory.json``, each declared by every component so
#: no ``strict`` one is unknown unless the case says so.
_FIT_MEMORIES = ("RAM_FITS", "RAM_GAP", "RAM_OVER", "RAM_UNKNOWN", "RAM_UNKNOWN_OVER")


def _over_budget(model_root: Path) -> None:
    _processor(model_root)
    _core(model_root, "CORE0", threshold=0.7)
    _task(model_root, "Task_10ms", period=0.01, priority=20, wcet="5000us")
    _task(model_root, "Task_20ms", period=0.02, priority=10, wcet="6000us")
    _memory(model_root, "RAM", size=4096, threshold=0.8, enforcement="advisory")
    _swc(model_root, "Alpha", memory={"RAM": 4000})
    _task(model_root, "Task_lost", period=0.05, priority=5, partition="NOWHERE", wcet="100us")


def _unknown(model_root: Path) -> None:
    _processor(model_root)
    _core(model_root, "CORE0", threshold=0.7)
    _task(model_root, "Task_10ms", period=0.01, priority=20, wcet="1200us")
    _task(model_root, "Task_20ms", period=0.02, priority=10)
    _memory(model_root, "RAM", size=8192, threshold=0.8)
    _swc(model_root, "Alpha", memory={"RAM": 2048})
    _swc(model_root, "Beta")
    _swc(model_root, "Delta", memory={"RAM": 0})
    _swc(model_root, "Epsilon", memory={"RAM": 0})
    _swc(model_root, "Gamma", memory={"RAM": 0})


def _fit_memory(model_root: Path) -> None:
    _processor(model_root)
    _core(model_root, "CORE0", threshold=0.7)
    _task(model_root, "Task_10ms", period=0.01, priority=10, wcet="1500us")
    # both yes: 2048 used of a 52428.8 byte limit in 65536.
    _memory(model_root, "RAM_FITS", size=65536, threshold=0.8)
    # the gap: 14336 used of 24576 in 32768, so 12288 breaks the threshold only.
    _memory(model_root, "RAM_GAP", size=32768, threshold=0.75)
    # over the size: 8192 used in 16384.
    _memory(model_root, "RAM_OVER", size=16384, threshold=0.75)
    # unknown: an undeclared consumer, within both limits as far as the figure goes.
    _memory(model_root, "RAM_UNKNOWN", size=65536, threshold=0.8)
    # over the threshold already, with an undeclared consumer: the size's "yes" is unknown.
    _memory(model_root, "RAM_UNKNOWN_OVER", size=32768, threshold=0.5)
    budgets = {
        "Alpha": {"RAM_FITS": 2048, "RAM_GAP": 8192, "RAM_OVER": 8192, "RAM_UNKNOWN": 1024, "RAM_UNKNOWN_OVER": 17000},
        "Beta": {"RAM_FITS": 0, "RAM_GAP": 6144, "RAM_OVER": 0, "RAM_UNKNOWN": 0, "RAM_UNKNOWN_OVER": 0},
    }
    for name, memory in budgets.items():
        assert set(memory) == set(_FIT_MEMORIES)
        _swc(model_root, name, memory=memory)
    _swc(model_root, "Gamma", memory={"RAM_FITS": 0, "RAM_GAP": 0, "RAM_OVER": 0})


#: Each fixture: the model builder (``None``: ``examples/basic``) and the CLI's extra arguments.
CASES: dict[str, tuple[Callable[[Path], None] | None, tuple[str, ...]]] = {
    "basic.json": (None, ()),
    "fit-core.json": (None, ("--fit-utilization", "0.05")),
    "over-budget.json": (_over_budget, ()),
    "unknown.json": (_unknown, ()),
    "fit-memory.json": (_fit_memory, ("--fit-bytes", "12288")),
}


def _cli_json(manifest: Path, extra: tuple[str, ...]) -> str:
    """``swcraft resource-report --format json``'s stdout, run in this process."""
    argv = ["swcraft", "resource-report", "--project", str(manifest), "--format", "json", *extra]
    out = io.StringIO()
    saved = sys.argv
    sys.argv = argv
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            main()
    finally:
        sys.argv = saved
    return out.getvalue()


def generated(name: str) -> str:
    """The fixture *name* as the CLI writes it today."""
    build, extra = CASES[name]
    if build is None:
        return _cli_json(BASIC_MANIFEST, extra)
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        build(root / "model")
        return _cli_json(_write_manifest(root), extra)


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_committed_document_is_the_cli_s(name: str) -> None:
    assert (FIXTURES / name).read_text(encoding="utf-8") == generated(name)


def test_the_documents_hold_what_the_panel_tests_need() -> None:
    """Each case shows what its name says, so a builder edit cannot hollow one out."""
    read = {name: json.loads((FIXTURES / name).read_text(encoding="utf-8")) for name in CASES}
    over = read["over-budget.json"]
    assert [row["status"] for row in over["cores"] + over["memories"]] == ["over budget", "over budget"]
    assert over["memories"][0]["enforcement"] == "advisory"
    assert {finding["severity"] for finding in over["memories"][0]["findings"]} == {"info"}
    assert over["topology"]
    unknown = read["unknown.json"]
    assert [row["status"] for row in unknown["cores"] + unknown["memories"]] == [
        "unknown (not every consumer declared a budget)"
    ] * 2
    assert [consumer["amount"] for consumer in unknown["memories"][0]["consumers"]].count("0 bytes") == 3
    fits = {row["name"]: row["fit"] for row in read["fit-memory.json"]["memories"]}
    assert fits["RAM_GAP"]["answer"] == "fits physically, breaks the threshold by 2048 bytes"
    # One memory per answer: each answer is its own template's, at its own pair of verdicts, so
    # five distinct sentences cannot be two cases differing only in their figures.
    expected = {
        "RAM_FITS": (resource_report.ANSWER_FITS, (True, True)),
        "RAM_GAP": (resource_report.ANSWER_WITHIN_SIZE_ONLY, (False, True)),
        "RAM_OVER": (resource_report.ANSWER_OVER_SIZE, (False, False)),
        "RAM_UNKNOWN": (resource_report.ANSWER_UNKNOWN, (None, None)),
        "RAM_UNKNOWN_OVER": (resource_report.ANSWER_OVER_THRESHOLD_SIZE_UNKNOWN, (False, None)),
    }
    assert set(expected) == set(_FIT_MEMORIES) == set(fits)
    for name, (template, pair) in expected.items():
        pattern = "".join(
            ".+" if index % 2 else re.escape(part) for index, part in enumerate(re.split(r"(\{\w+\})", template))
        )
        assert re.fullmatch(pattern, fits[name]["answer"]), name
        assert (fits[name]["within_threshold"]["fits"], fits[name]["within_size"]["fits"]) == pair, name
    assert read["fit-core.json"]["fit"] == {"kind": "core", "asked": "5.00%"}


if __name__ == "__main__":
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for fixture in sorted(CASES):
        (FIXTURES / fixture).write_text(generated(fixture), encoding="utf-8", newline="\n")
