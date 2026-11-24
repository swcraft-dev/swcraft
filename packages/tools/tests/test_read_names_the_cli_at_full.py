"""A size refusal of a layer ``read`` at ``full`` names the CLI, since ``swcraft query --form full`` answers it.

``editing/read.py::_refuse_over_budget`` names the CLI for a layer address served in ``index`` **or**
``full`` form, so the over-budget refusal of either carries
:data:`~swcraft_tools.editing.read.CLI_ANSWERS_SENTENCE`; a ``summary`` read does not, because the CLI
refuses ``summary``. The ``index`` cases, the join-ceiling case and the refusals that never name the CLI
are in ``test_editing_read.py``.

The bench is read in place: the budget is lowered by monkeypatching, and nothing is written to it.
What this does not reach: the join-ceiling refusal at ``full`` (``join`` is refused beside ``full``
on both surfaces), and an address below a layer (the CLI answers none).
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import pytest
import swcraft_tools.editing.read as read_module
from cli_runner import run_swcraft
from conftest import AGENT_TASK_BENCH
from conftest import AGENT_TASK_BENCH_MANIFEST as BENCH_MANIFEST
from swcraft_tools.editing import engine

#: A layer read at ``full``, without and with a member slice and a member path.
FULL_REQUESTS: dict[str, dict[str, Any]] = {
    "layer": {"operation": "read", "address": {"layer": "swc"}, "form": "full"},
    "members": {"operation": "read", "address": {"layer": "swc"}, "form": "full", "members": "ports"},
    "member path": {"operation": "read", "address": {"layer": "swc"}, "form": "full", "members": "runnables.calls"},
}


def _refused(request: dict[str, Any]) -> dict[str, Any]:
    response = engine.handle_request(request, project=str(BENCH_MANIFEST))
    assert response.get("error") == "over-budget", response
    return response


@pytest.mark.parametrize("case", sorted(FULL_REQUESTS))
def test_a_refusal_of_a_layer_read_at_full_names_the_cli(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The sentence is appended once, at the end; the protocol's hints are the ones it always offered."""
    request = FULL_REQUESTS[case]
    monkeypatch.setattr(read_module, "RESPONSE_BUDGET_WIRE_BYTES", 10)
    refused = _refused(request)
    message = refused["message"]
    assert message.endswith(f"; {read_module.CLI_ANSWERS_SENTENCE}"), message
    assert message.count(read_module.CLI_ANSWERS_SENTENCE) == 1
    assert all(hint.get("operation") in ("read", "discover") for hint in refused.get("hints", [])), refused
    assert str(AGENT_TASK_BENCH) not in message


@pytest.mark.parametrize("case", sorted(FULL_REQUESTS))
def test_the_same_read_at_summary_still_does_not_name_it(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """``summary`` is refused by ``swcraft query``, so its size refusal names no command."""
    monkeypatch.setattr(read_module, "RESPONSE_BUDGET_WIRE_BYTES", 10)
    refused = _refused(FULL_REQUESTS[case] | {"form": "summary"})
    assert "swcraft query" not in refused["message"], refused["message"]


@pytest.mark.parametrize("case", sorted(FULL_REQUESTS))
def test_the_named_command_answers_the_refused_full_read_whole(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """What the sentence promises at ``full``: the request, written to a file, answered whole by the CLI.

    The argv is read off the sentence (``FILE`` and ``PATH`` substituted), and the budget stays
    lowered while the CLI runs, so the answer is not one the budget happened to admit.
    """
    request = FULL_REQUESTS[case]
    whole = engine.handle_request(request, project=str(BENCH_MANIFEST))
    assert whole["response"] == "read", whole
    monkeypatch.setattr(read_module, "RESPONSE_BUDGET_WIRE_BYTES", 10)
    _refused(request | {"limit": 2})
    body = tmp_path / "request.json"
    body.write_text(
        json.dumps({key: value for key, value in request.items() if key != "operation"}), encoding="utf-8", newline="\n"
    )
    written = shlex.split(read_module.CLI_ANSWERS_SENTENCE.split("`")[1])
    substitutes = {"FILE": str(body), "PATH": str(BENCH_MANIFEST)}
    argv = [substitutes.get(token, token) for token in written] + ["--format", "json"]
    assert argv[0] == "swcraft", argv
    code, out, err = run_swcraft(argv[1:], monkeypatch, capsys)
    assert code == 0, err
    document = json.loads(out)
    assert document["form"] == "full"
    assert document["elements"] == whole["value"]
    assert whole["value"], "the equality is vacuous on an empty answer"
