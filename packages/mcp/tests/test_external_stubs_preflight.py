"""A connector across an external interface stub, sent as a ``change_set`` in ``stage-only`` mode.

Pass D judges port compatibility before anything is written or reviewed:

* **not judged** -- staged: a stub whose ``DEST`` names the declared interface's kind, and a stub whose abstract
  ``DEST`` names no kind;
* **incompatible** -- refused with ``consistency-violation`` (``connector.interface-mismatch``, ``interface kinds
  differ``): a stub whose ``DEST`` names another kind, and two stubs whose ``DEST``s differ.

The validator-level cases are ``packages/tools/tests/test_external_stubs_validator.py``'s ``TestThePreFlight``; here
the staging answer carries the verdict and names no storage path. What it does not reach: anything beyond kind,
which nothing in the project can compare across a stub.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import external_stubs_support as stubs
import pytest
import swcraft_mcp.server as _server
from protocol_client import call


@pytest.fixture()
def stub_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the server at the stub model; return the project root."""
    model_root = stubs.external_stubs_model_root(tmp_path / "stubs")
    monkeypatch.setenv("SWCRAFT_MANIFEST", str(model_root.parent / "swcraft.yaml"))
    _server._PROJECT = None
    return model_root.parent


def _connect(project: Path, provider: tuple[str, str], requirer: tuple[str, str]) -> dict[str, Any]:
    """Send the change set that creates the connector from *provider* to *requirer*."""
    structured, _texts = call(
        "change_set",
        baseline=fixtures.current_baseline_token(project),
        intent=f"Connect {provider[0]}.{provider[1]} to {requirer[0]}.{requirer[1]}",
        operations=[
            {
                "operation": "create",
                "address": {"layer": "connectors", "element": "AcrossTheStub"},
                "value": {
                    "name": "AcrossTheStub",
                    "from": {"swc": provider[0], "port": provider[1]},
                    "to": {"swc": requirer[0], "port": requirer[1]},
                },
            }
        ],
    )
    assert isinstance(structured, dict), structured
    return structured


@pytest.mark.parametrize(
    ("provider", "requirer"),
    [
        # IForeignSR (SENDER-RECEIVER-INTERFACE) against the declared SR IExample.
        (("ExampleSWC", "pSr"), ("ExampleConsumerSWC", "rIn")),
        # The declared IExample against IForeignAny, whose DEST is the abstract PORT-INTERFACE.
        (("ExampleSWC", "pOut"), ("ExampleConsumerSWC", "rAny")),
    ],
)
def test_a_pair_it_cannot_judge_is_staged(stub_project, provider, requirer) -> None:
    answer = _connect(stub_project, provider, requirer)

    assert answer["response"] == "change_set", answer
    assert answer["status"] == "staged", answer
    assert [entry["address"] for entry in answer["delta"]] == [{"layer": "connectors", "element": "AcrossTheStub"}]


@pytest.mark.parametrize(
    ("provider", "requirer"),
    [
        # IForeignCS (CLIENT-SERVER-INTERFACE) offered where the declared SR IExample is required.
        (("ExampleSWC", "pCs"), ("ExampleConsumerSWC", "rIn")),
        # Two stubs whose DESTs name different kinds.
        (("ExampleSWC", "pCs"), ("ExampleConsumerSWC", "rSr")),
        # A mode-switch stub is its own kind, not a sender-receiver one.
        (("ExampleSWC", "pMode"), ("ExampleConsumerSWC", "rSr")),
    ],
)
def test_a_kind_mismatch_across_a_stub_is_refused_at_staging(stub_project, provider, requirer) -> None:
    answer = _connect(stub_project, provider, requirer)

    assert answer["response"] == "error", answer
    assert answer["error"] == "consistency-violation", answer
    assert "interface mismatch" in answer["message"], answer["message"]
    assert "interface kinds differ" in answer["message"], answer["message"]
    assert "not judged" not in answer["message"]


def test_no_answer_names_where_the_project_is_stored(stub_project) -> None:
    pairs = [
        (("ExampleSWC", "pSr"), ("ExampleConsumerSWC", "rIn")),
        (("ExampleSWC", "pOut"), ("ExampleConsumerSWC", "rAny")),
        (("ExampleSWC", "pCs"), ("ExampleConsumerSWC", "rIn")),
    ]
    for provider, requirer in pairs:
        text = repr(_connect(stub_project, provider, requirer))
        assert str(stub_project) not in text
        assert stub_project.as_posix() not in text
        assert ".yaml" not in text
