"""The server's three ``single_read`` openers, on one long-lived server module.

One tool call reads a layer once, and the next call of the same process sees the disk as it is now. Each case
drives the tool over ``tools/call`` through the live ``mcp`` object, in one process.

**What these tests do not reach.** The count is of ``parse_element``, so a read outside the layer loader is not
counted. ``validate`` and ``build`` are witnessed on their in-process path alone: a configured override command is a
subprocess and opens no block.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import swcraft_mcp.server as server
from protocol_client import ScriptedHuman, call, call_reviewed
from single_read_support import read_twice
from swcraft_tools import model_state as ms

#: A vector on the shipped example: the model validates, and its change set creates a type the ARXML then names.
VECTOR = "create-data-type-alias"

CREATED = "VehicleSpeed_kph"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private copy of the shipped example, served: the manifest moved, so the project is reset."""
    root = fixtures.build_fixture(tmp_path / "project", VECTOR)
    monkeypatch.setenv("SWCRAFT_MANIFEST", str(root / "swcraft.yaml"))
    server._PROJECT = None
    return root


@pytest.fixture
def block_exits(single_read_tripwire: None) -> Iterator[list[ms._Held]]:
    """Each block's memo as it exits, seen beside the tripwire and never in its place.

    ``call`` runs the tool in a copied context, so only this global hook can witness that a block closed. It uses its
    own patch, not ``monkeypatch``: that fixture's undo runs after the tripwire's and would leave a dead observer.
    """
    tripwire = ms._single_read_observer
    assert tripwire is not None
    exits: list[ms._Held] = []

    def observing(held: ms._Held) -> None:
        exits.append(held)
        tripwire(held)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ms, "_single_read_observer", observing)
        yield exits


def _arxml(project: Path) -> str:
    return (project / "dist" / "model.arxml").read_text(encoding="utf-8")


def _break_the_connector(project: Path) -> Path:
    """Point the example's connector at a port nothing declares; the path of the file is the answer."""
    connector = project / "model" / "connectors" / "Example_to_Consumer.yaml"
    text = connector.read_text(encoding="utf-8")
    assert "port: rIn" in text
    connector.write_text(text.replace("port: rIn", "port: rNowhere"), encoding="utf-8", newline="\n")
    return connector


def _ram_size(answer: dict[str, Any]) -> int:
    (ram,) = (row for row in answer["memories"] if row["name"] == "RAM0")
    return ram["size"]


@pytest.mark.parametrize("tool", ["validate", "build"])
def test_an_edit_between_two_calls_is_reported(tool: str, project: Path) -> None:
    assert call(tool).structured["success"] is True

    _break_the_connector(project)

    verdict = call(tool).structured
    assert verdict["success"] is False
    assert any("rNowhere" in error["message"] for error in verdict["errors"]), verdict["errors"]


def test_a_repaired_model_is_seen_by_the_next_call(project: Path) -> None:
    original = (project / "model" / "connectors" / "Example_to_Consumer.yaml").read_text(encoding="utf-8")
    connector = _break_the_connector(project)
    assert call("validate").structured["success"] is False

    connector.write_text(original, encoding="utf-8", newline="\n")

    assert call("validate").structured["success"] is True


def test_resource_report_reports_an_edit_between_two_calls(project: Path) -> None:
    assert _ram_size(call("resource_report").structured) == 262144

    memory = project / "model" / "hardware" / "RAM0.yaml"
    text = memory.read_text(encoding="utf-8")
    memory.write_text(text.replace("size: 262144", "size: 524288"), encoding="utf-8", newline="\n")

    assert _ram_size(call("resource_report").structured) == 524288


@pytest.mark.parametrize("before", [False, True], ids=["change-set-first", "validate-first"])
def test_a_committed_change_set_is_seen_by_the_next_validate(
    before: bool, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real path: the commit on ``_handle``'s worker, then a tool on the same server module."""
    monkeypatch.setenv("SWCRAFT_REVIEW", server.REVIEW_ELICIT)
    server._configure_review_mode()
    if before:
        assert call("validate").structured["success"] is True
        assert CREATED not in _arxml(project)

    vector = fixtures.VECTORS[VECTOR]
    answered, _texts = call_reviewed(
        ScriptedHuman(True),
        "change_set",
        baseline=call("discover").structured["baseline"],
        operations=vector["input"]["operations"],
        intent=vector["input"]["intent"],
    )
    assert answered["status"] == "committed", answered

    verdict = call("validate").structured
    assert verdict["success"] is True, verdict["errors"]
    assert CREATED in _arxml(project)


@pytest.mark.parametrize("tool", ["validate", "build", "resource_report"])
def test_a_tool_call_reads_each_file_once_and_only_in_its_block(
    tool: str, project: Path, reads: list[tuple[Path, bool]], block_exits: list[ms._Held]
) -> None:
    answer = call(tool).structured

    if tool == "resource_report":
        assert _ram_size(answer) == 262144
    else:
        assert answer["success"] is True, answer["errors"]
    assert reads, "the tool read nothing, so the count says nothing"
    assert all(opened for _, opened in reads), [file for file, opened in reads if not opened]
    assert read_twice(reads) == {}
    assert len(block_exits) == 1


def test_validate_reads_the_whole_model_once(project: Path, reads: list[tuple[Path, bool]]) -> None:
    assert call("validate").structured["success"] is True

    assert sorted((file for file, _ in reads), key=Path.as_posix) == sorted(
        (project / "model").rglob("*.yaml"), key=Path.as_posix
    )


def test_two_calls_of_a_tool_read_twice(project: Path, reads: list[tuple[Path, bool]]) -> None:
    """A long-lived process has no run-long memo: each tool call is one block of its own."""
    assert call("validate").structured["success"] is True
    once = len(reads)
    assert once

    assert call("validate").structured["success"] is True

    assert len(reads) == 2 * once


@pytest.mark.parametrize("tool", ["validate", "build"])
def test_a_refused_model_leaves_no_block_and_answers_the_error_shape(
    tool: str, project: Path, reads: list[tuple[Path, bool]], block_exits: list[ms._Held]
) -> None:
    """The exception reaches ``_run_with_error_shape`` through the block's own exit."""
    broken = project / "model" / "hardware" / "RAM0.yaml"
    broken.write_text("name: [unclosed\n", encoding="utf-8", newline="\n")

    verdict = call(tool).structured

    assert verdict["success"] is False
    assert (broken, True) in reads, "the broken file was not met inside the block"
    assert len(block_exits) == 1, "the block did not close, or another opened"
