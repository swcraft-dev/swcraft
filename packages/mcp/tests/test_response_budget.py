"""The wire gate: the bytes the engine measures are the bytes a host receives.

:func:`~swcraft_tools.editing.read.measure` builds the one copy the server puts in the JSON-RPC message
(the compact document as one escaped text block) and holds it to ``RESPONSE_BUDGET_WIRE_BYTES``. This
module measures that claim through the in-memory ``fastmcp.Client`` with the same ``wire_bytes`` that
``scripts/measure_mcp_scale.py`` uses. Escaping is unbounded relative to separator spacing, so the built
shape is measured rather than bounded by a factor.

Three parts, none sufficient alone:

* every response the engine admits on the wide model is at or under the budget on the wire;
* at least one response is refused and would have been over it (else refusing everything would pass);
* the shape the engine assumes is the shape the server sends: no structured half, one text block,
  byte-identical to the compact dump of its body.

A tool answering a plain ``str`` (``impact_of``) is sent as one text block holding the string, and
``server._text_wire_bytes`` is asserted against a real call.

What it does not reach: JSON-RPC framing (a fixed ~70 bytes no model content moves, so both the engine
and this gate under-count by that much); the ratio of escaped copy to raw document, which must not be
asserted (framing dominates small payloads); ``discover``, which this module does not measure.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import scale_model
from conftest import REPO_ROOT, measure_mcp_scale_module
from fastmcp import Client
from protocol_client import body_of
from swcraft_mcp.server import mcp
from swcraft_tools.editing.read import (
    HINT_PAGE_SIZE,
    NARROWING_ORDER,
    RESPONSE_BUDGET_WIRE_BYTES,
    compact_json,
    measure,
)
from swcraft_tools.model_registry import current_registry

#: The acceptance oracle's own ``wire_bytes``: a second definition of "what the host receives" could
#: disagree with the measurement table.
wire_bytes = measure_mcp_scale_module().wire_bytes


@pytest.fixture
def wide_project(tmp_path: Path) -> Path:
    """The shared wide model, written into the autouse fixture's own project.

    ``scale_model`` is the engine-level budget suite's builder, so this gate speaks of the same model.
    """
    scale_model.build_wide_model(tmp_path / "model")
    return Path(os.environ["SWCRAFT_MANIFEST"])


def _raw_call(name: str, /, **arguments: Any) -> Any:
    """``tools/call``, returning FastMCP's own unsplit result object: the wire is exactly its blocks."""

    async def run() -> Any:
        async with Client(mcp) as client:
            return await client.call_tool(name, arguments, raise_on_error=False)

    return asyncio.run(run())


#: Every read the gate sweeps: each declared layer in each form, plus the hub element and its ports
#: collection, the two shapes that grow unbounded. Off the registry, never a literal: a layer missing
#: from a hand-written tuple is an absent row, not a failing one.
LAYERS = current_registry().display_order
#: The server's own chain, so a form added to the protocol is swept too.
FORMS = NARROWING_ORDER

ADDRESSES: tuple[dict[str, Any], ...] = tuple({"layer": layer} for layer in LAYERS) + (
    {"layer": "swc", "element": "S0"},
    {"layer": "swc", "element": "S0", "path": [{"field": "ports"}]},
)


def _sweep(wide_project: Path) -> list[tuple[dict[str, Any], str, int, bool]]:
    rows = []
    for address in ADDRESSES:
        for form in FORMS:
            result = _raw_call("read", address=address, form=form)
            body = body_of(result)
            refused = bool(body and body.get("response") == "error")
            rows.append((address, form, wire_bytes(result), refused))
    return rows


def test_every_admitted_response_is_under_the_budget_on_the_wire(wide_project: Path) -> None:
    over = [
        (address, form, size)
        for address, form, size, refused in _sweep(wide_project)
        if not refused and size > RESPONSE_BUDGET_WIRE_BYTES
    ]
    assert not over, (
        "the engine admitted a response the host receives as more than "
        f"{RESPONSE_BUDGET_WIRE_BYTES} bytes: {over}. What the engine measures is the compact "
        "document escaped as the one text block; if that is not the one copy the server sent, "
        "the test below on the shape is the one that says so."
    )


def test_the_wire_shape_the_engine_measures_is_the_shape_fastmcp_sends(wide_project: Path) -> None:
    """``read.measure``'s assumption, observed off a live result rather than asserted.

    The server sends one copy of a mapping result: no structured half, one text block holding the
    compact document. The budget, the refusal's ``measured`` and the acceptance table apply that formula.
    """
    result = _raw_call("read", address={"layer": "swc"}, form="summary")
    texts = [block.text for block in result.content if getattr(block, "type", None) == "text"]

    assert result.structured_content is None, "the server sends no structured half: one copy on the wire"
    assert len(texts) == 1, "a mapping result arrives as exactly one text block"
    body = json.loads(texts[0])
    assert texts[0] == compact_json(body), (
        "the text block is the COMPACT dump of the body; `read.measure` counts it as such"
    )
    assert measure(body) == wire_bytes(result)


def test_at_least_one_response_is_refused_and_would_have_been_over(wide_project: Path) -> None:
    # Keeps the test above from holding vacuously. A refusal itself is small, so what is checked is its
    # `measured`: the size of the answer not sent.
    refusals = [row for row in _sweep(wide_project) if row[3]]
    assert refusals, "nothing on the wide model was refused; the gate above quantifies over nothing"
    result = _raw_call("read", address={"layer": "swc"}, form="full")
    measured = body_of(result)["measured"]
    assert measured["wire_bytes"] > RESPONSE_BUDGET_WIRE_BYTES
    assert wire_bytes(result) < RESPONSE_BUDGET_WIRE_BYTES, "the refusal itself must fit"


def test_the_refusal_reaches_the_host_with_its_chain_intact(wide_project: Path) -> None:
    # The keys are optional in the schema: check they survive the one-block body as a replayable request.
    result = _raw_call("read", address={"layer": "swc"}, form="full")
    body = body_of(result)
    assert body["error"] == "over-budget"
    assert body["hints"] == [
        {"operation": "read", "address": {"layer": "swc"}, "form": "summary"},
        {"operation": "read", "address": {"layer": "swc"}, "form": "index"},
    ]
    replayed = _raw_call("read", address=body["hints"][0]["address"], form=body["hints"][0]["form"])
    assert body_of(replayed)["response"] == "read"
    assert wire_bytes(replayed) <= RESPONSE_BUDGET_WIRE_BYTES


def test_the_default_form_of_the_hub_layer_reaches_the_host_under_budget(wide_project: Path) -> None:
    # What an agent that sends no form gets on a model whose `full` layer does not fit.
    result = _raw_call("read", address={"layer": "swc"})
    body = body_of(result)
    assert body["form"] == "summary"
    assert wire_bytes(result) <= RESPONSE_BUDGET_WIRE_BYTES
    assert len(body["value"]) == scale_model.WIDE_SWCS


def test_a_text_tools_wire_size_is_exactly_what_it_holds_itself_to(wide_project: Path) -> None:
    """``impact_of`` is measured in the same unit, and the unit is measured here.

    A string answer arrives as one text block holding the string, with no ``{"result": ...}`` envelope,
    escaped on the wire. The independent assertion is the count of escaped newlines, which no copy of
    the ``json.dumps`` formula supplies.
    """
    from swcraft_mcp.server import _text_wire_bytes

    result = _raw_call("impact_of", name="ISig", form="full")
    texts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
    text = texts[0]

    assert result.structured_content is None, "the server sends no structured half: one copy on the wire"
    assert text.startswith("Impact of "), "the answer must be ADMITTED; a refusal fits trivially"
    assert _text_wire_bytes(text) == wire_bytes(result)
    assert wire_bytes(result) == len(json.dumps(text, ensure_ascii=False).encode("utf-8"))
    # Every newline of the report travels as the two bytes `\n`.
    assert text.count("\n") > 0, "a full impact report has more than one line"
    assert wire_bytes(result) - len(text.encode("utf-8")) - 2 >= text.count("\n"), (
        "the escaping is part of what the host counts"
    )
    assert wire_bytes(result) <= RESPONSE_BUDGET_WIRE_BYTES, "the admitted answer must fit, like every other"


def test_an_unknown_form_is_still_a_schema_violation_at_the_wire(wide_project: Path) -> None:
    body = body_of(_raw_call("read", address={"layer": "swc"}, form="tiny"))
    assert body["error"] == "schema-violation"
    assert "hints" not in body


#: Every port joined with the connector it provides: one row per connector, with its computed ends.
_WIDE_JOIN: list[dict[str, Any]] = [
    {
        "with": {"address": {"layer": "connectors"}, "derived": ["connection_ends"]},
        "on": [
            {"left": "element", "right": "derived.connection_ends.provider.swc"},
            {"left": "member", "right": "derived.connection_ends.provider.port"},
        ],
        "as": "connection",
    }
]


def test_a_joined_read_is_refused_whole_and_its_page_hint_replays_under_budget(wide_project: Path) -> None:
    """Under the join row ceiling a join is a set like any other, at the wire.

    The 500-row product is over the budget unpaged; the refusal offers the first page, carrying
    ``form``, ``members`` and ``join`` verbatim, and that page replays under budget.

    What it does not reach: the no-hint case and ``derived`` without ``join``
    (``packages/tools/tests/test_editing_read.py``).
    """
    arguments = {"address": {"layer": "swc"}, "members": "ports", "form": "index", "join": _WIDE_JOIN}
    body = body_of(_raw_call("read", **arguments))
    assert body["error"] == "over-budget"
    assert body["measured"]["wire_bytes"] > RESPONSE_BUDGET_WIRE_BYTES
    assert body["measured"]["elements"] == scale_model.WIDE_CONNECTORS
    [hint] = body["hints"]
    assert hint == {"operation": "read", **arguments, "limit": HINT_PAGE_SIZE}, "the size `discover` publishes"

    replayed = _raw_call("read", **{key: value for key, value in hint.items() if key != "operation"})
    page = body_of(replayed)
    assert page["response"] == "read"
    assert page["join"] == _WIDE_JOIN
    assert page["page"]["total"] == scale_model.WIDE_CONNECTORS
    assert page["page"]["returned"] == HINT_PAGE_SIZE
    assert wire_bytes(replayed) <= RESPONSE_BUDGET_WIRE_BYTES


def test_a_joined_read_over_the_row_ceiling_is_refused_before_it_is_built_and_offered_no_page(
    wide_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The join row ceiling at the wire.

    With the ceiling one row under the product, the join is refused before a combined row is built,
    with the row count and no hint: every page builds the whole product again, so a ``limit`` is
    refused the same way. Narrowed with ``where`` it answers, and fits.

    What it does not reach: the exactness of the weight and the entry-by-entry refusal of a two-entry
    join (``packages/tools/tests/test_editing_read.py``).
    """
    import swcraft_tools.editing.read as read_module

    monkeypatch.setattr(read_module, "JOIN_ROW_CEILING", scale_model.WIDE_CONNECTORS - 1)
    arguments = {"address": {"layer": "swc"}, "members": "ports", "form": "index", "join": _WIDE_JOIN}
    for request in (arguments, arguments | {"limit": HINT_PAGE_SIZE}):
        result = _raw_call("read", **request)
        body = body_of(result)
        assert body["error"] == "over-budget", body
        assert body["measured"]["wire_bytes"] > RESPONSE_BUDGET_WIRE_BYTES
        assert body["measured"]["elements"] == scale_model.WIDE_CONNECTORS
        assert "hints" not in body, "a page does not bound the work, and no `where` value is the server's to pick"
        assert f"join row ceiling of {scale_model.WIDE_CONNECTORS - 1}" in body["message"]
        assert wire_bytes(result) <= RESPONSE_BUDGET_WIRE_BYTES

    narrowed_join = [
        {**_WIDE_JOIN[0], "with": {**_WIDE_JOIN[0]["with"], "where": [{"field": "name", "equals": "S0_p0__S1_r0"}]}}
    ]
    narrowed = _raw_call("read", **(arguments | {"join": narrowed_join}))
    page = body_of(narrowed)
    assert page["response"] == "read", page
    assert [(row["element"], row["member"]) for row in page["value"]["identities"]] == [("S0", "p0")]
    assert wire_bytes(narrowed) <= RESPONSE_BUDGET_WIRE_BYTES


def test_a_resource_answer_is_measured_in_the_unit_it_holds_itself_to(tmp_path: Path) -> None:
    """``resource_report`` answers a mapping, and ``measure`` of it is the wire."""
    shutil.copytree(REPO_ROOT / "examples" / "basic" / "model", tmp_path / "model", dirs_exist_ok=True)

    for arguments in ({}, {"memory": "RAM0"}, {"fits": {"memory": "RAM0", "bytes": 1}}):
        result = _raw_call("resource_report", **arguments)
        body = body_of(result)

        assert result.structured_content is None, "the server sends no structured half: one copy on the wire"
        assert body["response"] in {"resources", "resource", "fits"}, body
        assert measure(body) == wire_bytes(result)
