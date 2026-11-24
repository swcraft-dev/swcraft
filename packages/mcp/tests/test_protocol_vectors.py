"""The editing acceptance vectors, replayed through the named MCP tools.

``packages/tools/tests/test_editing_vectors.py`` runs every vector against the engine. This
module runs the routable ones through ``tools/call`` and holds each answer against the engine's
own answer to the same request, not the vector's expected response: the property under test is
that the MCP surface is a pass-through, so the tool name, FastMCP's argument binding and the
sync/async bridge change nothing about the answer.

A vector is **routable** when its ``input`` names a protocol operation and its top-level values
bind to that tool's signature (the tool name supplies ``operation``). The three **unroutable**
ones never reach ``handle_request``, so they have no protocol response and are pinned as the
FastMCP transport errors they are. ``ROUTABLE | UNROUTABLE`` is asserted to be the whole vector
set, disjoint, so no vector goes unreplayed.

Each vector gets two fixture copies, because a ``committed`` vector really commits and the second
run would otherwise fail as ``stale-baseline``. Both copies come from one seed, so their baseline
tokens must be equal, which is asserted.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import swcraft_mcp.server as server
from fastmcp.exceptions import ToolError
from protocol_client import DECLINE, ScriptedHuman, call_reviewed
from swcraft_tools.editing.delta import summarize_response

from tests.test_approved_scope_channel import set_scope
from tests.test_review_mode import set_mode

#: The per-turn correlation id: the MCP side is a scoped run wherever the engine side is one,
#: because the scope and its id are set or unset together.
HOST_SCOPE_ID = "vector-turn"


def host_approved_scope(vector: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The scope the host hands the engine for *vector*, on both transports.

    The vector's own `context.authorization.approved_scope`: `None` for an unscoped exchange,
    else the published list, possibly empty.
    """
    return fixtures.approved_scope(vector)


#: `cs-<UTC timestamp>-<6 hex>`: newly allocated per request, so two runs of one
#: exchange differ here and nowhere else.
AUDIT_REF = re.compile(r"^cs-\d{8}T\d{6}Z-[0-9a-f]{6}$")

#: The vectors with no routable tool call, and why. Written out rather than derived so that a
#: fourth one is a failure a person looks at, not a silently growing exemption list.
UNROUTABLE: dict[str, str] = {
    "envelope-unknown-rename-operation": "`rename` is not a protocol operation, so there is no tool of that name",
    "envelope-storage-path-as-address": "a string storage path cannot bind to `read`'s `address: dict`",
    "envelope-change-set-without-baseline": "`change_set` requires `baseline`, so the call cannot be made at all",
}


def tool_call(vector: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The named tool call a vector's `input` routes to, or `None`.

    Structural rather than found by trying the call: "whatever did not raise" is not an
    inventory, and a changed binding rule would silently move a vector between the groups.
    """
    request = vector["input"]
    if not isinstance(request, dict):
        return None
    operation = request.get("operation")
    if operation == "discover":
        # Added when present rather than defaulted, as for `read` below.
        named: dict[str, Any] = {}
        for key in ("layer", "form"):
            if key in request:
                named[key] = request[key]
        return "discover", named
    if operation == "find":
        # One form per call, the others omitted, as a host would.
        forms = [key for key in ("names", "text", "for_requirement") if key in request]
        named = {key: request[key] for key in ("names", "text", "for_requirement", "candidates") if key in request}
        return ("find", named) if len(forms) == 1 else None
    if operation == "read":
        address = request.get("address")
        if "addresses" in request:
            batch: dict[str, Any] = {"addresses": request["addresses"]}
            if "form" in request:
                batch["form"] = request["form"]
            return "read", batch
        if "address" not in request:
            return "read", {}  # the model map: a read naming no address
        if not isinstance(address, dict):
            return None
        # The optional keys are added rather than defaulted: an explicit `None` is not the same
        # call, since the tool reads it as "no value named" and drops the key.
        named: dict[str, Any] = {"address": address}
        for key in ("form", "where", "limit", "after", "members", "derived", "join"):
            if key in request:
                named[key] = request[key]
        return "read", named
    if operation == "change_set":
        baseline = request.get("baseline")
        operations = request.get("operations")
        if not isinstance(baseline, str) or not isinstance(operations, list):
            return None
        if not all(isinstance(entry, dict) for entry in operations):
            return None
        named: dict[str, Any] = {"baseline": baseline, "operations": operations}
        if request.get("intent"):
            named["intent"] = request["intent"]
        if request.get("complete") is True:
            named["complete"] = True
        return "change_set", named
    return None


ROUTABLE: list[str] = sorted(name for name, vector in fixtures.VECTORS.items() if tool_call(vector) is not None)


def expected_status(vector: dict[str, Any]) -> str | None:
    """The host act the vector's expectation states, or `None` for a rejection."""
    response = vector.get("expected", {}).get("response")
    if not isinstance(response, dict) or response.get("response") != "change_set":
        return None
    return response.get("status")


def comparable(response: dict[str, Any]) -> dict[str, Any]:
    """The response with `audit_ref`, the one value two runs may differ in, shape-checked and masked."""
    if "audit_ref" not in response:
        return response
    assert AUDIT_REF.fullmatch(response["audit_ref"]), response["audit_ref"]
    return {**response, "audit_ref": "<change-set id>"}


class TestTheVectorInventoryIsPartitioned:
    def test_every_vector_is_in_exactly_one_group(self) -> None:
        # Written by hand when a vector is added or removed; the groups are derived.
        assert len(fixtures.VECTORS) == 169
        assert set(ROUTABLE).isdisjoint(UNROUTABLE)
        assert set(ROUTABLE) | set(UNROUTABLE) == set(fixtures.VECTORS)

    def test_the_unroutable_group_is_exactly_the_three_envelope_negatives(self) -> None:
        # Every other vector, every negative included, is a well-formed call whose content the
        # engine refuses: the distinction between the transport boundary and pass A.
        assert set(UNROUTABLE) == {name for name, vector in fixtures.VECTORS.items() if tool_call(vector) is None}


class TestTheRoutableVectorsAnswerExactlyAsTheEngineDoes:
    @pytest.mark.parametrize("vector_name", ROUTABLE)
    def test_the_wire_answer_is_the_engine_answer(
        self, vector_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vector = fixtures.VECTORS[vector_name]
        name, named = tool_call(vector)  # type: ignore[misc]  # ROUTABLE excludes None

        engine_project = fixtures.build_fixture(tmp_path / "engine", vector_name)
        mcp_project = fixtures.build_fixture(tmp_path / "mcp", vector_name)
        fixtures.check_preconditions(engine_project, vector)
        token = fixtures.baseline_token(engine_project, vector_name)
        assert token == fixtures.baseline_token(mcp_project, vector_name), (
            "two copies of one seeded model must share a baseline token; without that the two runs "
            "below would not be the same exchange"
        )

        request = fixtures.prepare_request(vector, token)
        if vector["context"].get("baseline_state") == "stale":
            # After the token is substituted, so the vector fails for its own stated reason.
            fixtures.make_stale(engine_project)
            fixtures.make_stale(mcp_project)

        scope = host_approved_scope(vector)
        # The server passes `HOST_DISCOVER_DEFAULT` on every call, so the engine is asked the same way.
        engine_response = fixtures.dispatch(
            request,
            project=engine_project,
            approved_scope=scope,
            review=fixtures.host_review(vector),
            discover_default=server.HOST_DISCOVER_DEFAULT,
        )

        status = expected_status(vector)
        monkeypatch.setenv("SWCRAFT_MANIFEST", str(mcp_project / "swcraft.yaml"))
        server._PROJECT = None
        # The same scope over the host's channel; both variables or neither, since one without the
        # other is a startup configuration error.
        set_scope(monkeypatch, scope, scope_id=None if scope is None else HOST_SCOPE_ID)
        human: ScriptedHuman | None = None
        if status == "committed":
            # Yes to the delta and, where the change set weakens a check, to the safety question.
            set_mode(monkeypatch, server.REVIEW_ELICIT)
            human = ScriptedHuman(True, True)
        elif status == "abandoned":
            set_mode(monkeypatch, server.REVIEW_ELICIT)
            human = ScriptedHuman(DECLINE)
        else:
            set_mode(monkeypatch, None)

        if name == "change_set":
            named = {**named, "baseline": request["baseline"]}
        structured, _texts = call_reviewed(human, name, **named)

        # The edge summarises a whole element's removal's `before`; every other answer is unchanged.
        assert comparable(structured) == comparable(summarize_response(engine_response))


class TestTheUnroutableVectorsAreTransportErrors:
    """No protocol response is fabricated for a call that cannot be made."""

    @pytest.mark.parametrize("vector_name", sorted(UNROUTABLE))
    def test_the_call_fails_at_the_mcp_boundary(
        self, vector_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vector = fixtures.VECTORS[vector_name]
        project = fixtures.build_fixture(tmp_path / "project", vector_name)
        monkeypatch.setenv("SWCRAFT_MANIFEST", str(project / "swcraft.yaml"))
        server._PROJECT = None
        set_mode(monkeypatch, None)
        before = fixtures.model_state_key(project / "model")

        # The operation as the tool name and every other key as a named argument: an unknown tool,
        # a value that cannot bind or a missing required argument, none reaching the engine.
        request = vector["input"]
        attempted = {key: value for key, value in request.items() if key != "operation"}
        with pytest.raises(ToolError):
            call_reviewed(None, request["operation"], **attempted)

        assert fixtures.model_state_key(project / "model") == before
