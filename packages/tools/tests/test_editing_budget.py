"""The response budget and the ``over-budget`` refusal.

* **The unit.** :func:`~swcraft_tools.editing.read.measure` is the one copy the
  MCP server sends — the compact document as one text block, escaped as a JSON
  string — and :func:`~swcraft_tools.editing.read.measure_response` the whole
  response, so an admitted answer satisfies ``wire <= RESPONSE_BUDGET_WIRE_BYTES``
  with no factor between them. That the unit matches what a host receives is
  proved at the wire, in ``packages/mcp/tests/test_response_budget.py``.
* **The refusal.** An over-budget response is refused with the eleventh error
  code, never cut, carrying ``measured`` and the chain of narrower requests the
  address admits.
* **What no other code carries.** :meth:`~swcraft_tools.editing.protocol.Violation.__post_init__`
  refuses ``hints`` and ``measured`` on any other code.

Does **not** reach: the sizes of the three forms on a real model
(``scripts/measure_mcp_scale.py`` takes them); whether a hint *fits* — a hint is
a form the address admits, not a promise; and ``impact_of`` and the reports,
which this budget is not enforced on.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import scale_model
import yaml
from conftest import REPO_ROOT, StateSnapshot, model_element_path
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.address import Address, FieldStep, Resolved, resolve
from swcraft_tools.editing.engine import _over_budget, handle_request
from swcraft_tools.editing.protocol import (
    ERROR_CODES,
    DiscoverRequest,
    EnvelopeError,
    ErrorResult,
    Measured,
    ReadRequest,
    Violation,
)
from swcraft_tools.editing.read import (
    HINT_PAGE_SIZE,
    NARROWING_ORDER,
    RESPONSE_BUDGET_WIRE_BYTES,
    OverBudget,
    _narrowing_chain,
    _summary_narrows_collection,
    address_shape,
    collection_index,
    compact_json,
    measure,
    measure_response,
    narrower_forms,
    read_answer,
    summary_field_names,
)
from swcraft_tools.model_state import load_model_state
from swcraft_tools.project import require_project

#: The one knob every lowering test turns. It is compared against directly, so
#: what is enforced, what the refusal reports and what ``discover`` publishes
#: cannot be lowered apart.
BUDGET = "swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES"


# Projects: the wide model for a real refusal, `examples/basic` for a lowered one


def _manifest(project_root: Path) -> Path:
    """A manifest beside a model root, so the engine seam can open it."""
    document = {
        "name": "budget-fixture",
        "schema_version": "1.0",
        "model_root": "model",
        "dist_root": "dist",
        "views_root": "views",
        "outputs": {"arxml": "dist/model.arxml", "needs": "dist/needs.json"},
    }
    manifest = project_root / "swcraft.yaml"
    manifest.write_text(yaml.safe_dump(document, default_flow_style=False, sort_keys=False), encoding="utf-8")
    return manifest


@pytest.fixture
def wide_project(tmp_path: Path) -> Any:
    """The shared wide model as a project, its manifest one level above the model root."""
    root = tmp_path / "wide-project"
    scale_model.build_wide_model(root / "model")
    return require_project(str(_manifest(root)))


@pytest.fixture
def wide_snapshot(wide_project: Any) -> Any:
    return StateSnapshot(state=load_model_state(Path(wide_project.resolved_model_root)))


@pytest.fixture
def basic_project(tmp_path: Path) -> Any:
    root = fixtures.build_fixture(tmp_path / "basic", "read-swc-element")
    return require_project(str(root / "swcraft.yaml"))


def _read(project: Any, address: dict[str, Any], form: str | None = None) -> dict[str, Any]:
    request: dict[str, Any] = {"operation": "read", "address": address}
    if form is not None:
        request["form"] = form
    return handle_request(request, project=project)


# The unit


@pytest.mark.parametrize(
    "value",
    [
        [],
        {},
        [{"name": "S0", "ports": []}],
        {"name": "Olmenge", "description": "a non-ASCII description — measured as UTF-8"},
        "a bare string",
        1200,
    ],
)
def test_measure_is_the_one_text_block_of_a_mapping_body(value: Any) -> None:
    # A mapping result is sent once: one text block carrying the compact
    # document, escaped as a JSON string, and no `structuredContent`. The
    # scalars stay because `measure` accepts any JSON value. A `str` tool's
    # block is measured by `server._text_wire_bytes`, never by this function.
    compact = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    assert compact == compact_json(value)
    assert measure(value) == len(json.dumps(compact, ensure_ascii=False).encode("utf-8"))


@pytest.mark.parametrize(
    "value",
    [
        [{"description": 'quotes "everywhere" and more "quotes"'}],
        [{"description": "a\nmulti\nline\nrequirement\ndescription\n"}],
    ],
)
def test_the_escaping_is_why_a_doubled_spaced_measure_was_not_a_bound(value: Any) -> None:
    # Escaping has no bound in terms of separator spacing: the escaped copy is
    # larger than even the spaced document, so no rendering of the raw document
    # bounds the wire.
    spaced = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    assert measure(value) > spaced


def test_measure_response_is_the_measure_of_the_whole_response() -> None:
    # The envelope is inside the figure: the escaped copy is not additive over
    # the keys, so an envelope allowance would only model the response.
    address = Address(layer="swc", element="ExampleSWC")
    value = [{"name": "ExampleSWC"}]
    response = {"response": "read", "address": address.to_json(), "form": "full", "value": value}
    assert measure_response(value, address, "full") == measure(response)


def test_the_budget_is_compared_against_directly_with_no_factor() -> None:
    # One constant, in the wire unit, and nothing derived from it: the removed
    # `WIRE_FACTOR` and `budget_bytes()` must not come back unnoticed.
    import swcraft_tools.editing.read as read_module

    assert RESPONSE_BUDGET_WIRE_BYTES == 80_000
    assert not hasattr(read_module, "WIRE_FACTOR")
    assert not hasattr(read_module, "budget_bytes")


def test_lowering_the_wire_budget_lowers_what_is_enforced(basic_project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing can lower what is enforced without lowering what a refusal reports.
    monkeypatch.setattr(BUDGET, 800)
    refusal = _read(basic_project, {"layer": "swc"}, "full")
    assert refusal["error"] == "over-budget"
    assert refusal["measured"]["budget"] == 800


def test_the_narrowing_order_is_not_the_published_order() -> None:
    # `READ_FORMS` is what `discover` publishes; this one is the refusal chain.
    assert NARROWING_ORDER == ("full", "summary", "index")


# The refusal, on a model that really is too wide


def test_a_full_layer_read_over_budget_is_refused_with_the_over_budget_code(wide_project: Any) -> None:
    response = _read(wide_project, {"layer": "swc"}, "full")
    assert response["error"] == "over-budget"
    assert response["failure_class"] == "invalid"
    assert response["addresses"] == [{"layer": "swc"}]
    assert "additional" not in response


def test_the_refusal_carries_the_sizes_and_the_element_count(wide_project: Any) -> None:
    measured = _read(wide_project, {"layer": "swc"}, "full")["measured"]
    assert measured["budget"] == RESPONSE_BUDGET_WIRE_BYTES
    assert measured["wire_bytes"] > measured["budget"]
    # The count is of elements, not of published fields.
    assert measured["elements"] == scale_model.WIDE_SWCS


def test_the_refusal_carries_the_whole_chain_in_narrowing_order(wide_project: Any) -> None:
    hints = _read(wide_project, {"layer": "swc"}, "full")["hints"]
    assert hints == [
        {"operation": "read", "address": {"layer": "swc"}, "form": "summary"},
        {"operation": "read", "address": {"layer": "swc"}, "form": "index"},
    ]


def test_replaying_the_first_hint_answers_under_budget(wide_project: Any) -> None:
    # A hint is a complete request, replayable verbatim.
    hint = _read(wide_project, {"layer": "swc"}, "full")["hints"][0]
    replayed = handle_request(hint, project=wide_project)
    assert replayed["response"] == "read"
    assert replayed["form"] == "summary"
    assert len(replayed["value"]) == scale_model.WIDE_SWCS


def test_the_default_form_answers_where_full_refuses(wide_project: Any) -> None:
    answered = _read(wide_project, {"layer": "swc"})
    assert answered["response"] == "read"
    assert answered["form"] == "summary"


def test_a_summary_over_a_lowered_budget_hints_the_index_alone(
    wide_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(BUDGET, 2_000)
    response = _read(wide_project, {"layer": "swc"}, "summary")
    assert response["error"] == "over-budget"
    assert response["hints"] == [{"operation": "read", "address": {"layer": "swc"}, "form": "index"}]


def test_an_index_over_a_still_lower_budget_refuses_with_no_hints_at_all(
    wide_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The chain's end: nothing narrower exists, so the key is omitted rather
    # than sent empty, and `measured` is what is left.
    monkeypatch.setattr(BUDGET, 200)
    response = _read(wide_project, {"layer": "swc"}, "index")
    assert response["error"] == "over-budget"
    assert "hints" not in response
    assert response["measured"]["elements"] == scale_model.WIDE_SWCS


def test_an_element_read_over_budget_hints_the_summary_and_never_the_index(
    wide_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `index` has nothing to index at an element, so hinting it would offer the
    # same bytes that just refused.
    monkeypatch.setattr(BUDGET, 2_000)
    response = _read(wide_project, {"layer": "swc", "element": "S0"}, "full")
    assert response["error"] == "over-budget"
    assert response["hints"] == [{"operation": "read", "address": {"layer": "swc", "element": "S0"}, "form": "summary"}]
    assert response["measured"]["elements"] == 1


def test_a_scalar_over_budget_refuses_with_an_empty_chain(wide_project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(BUDGET, 2)
    response = _read(wide_project, {"layer": "swc", "element": "S0", "path": [{"field": "name"}]}, "full")
    assert response["error"] == "over-budget"
    assert "hints" not in response
    assert response["measured"]["elements"] == 1


def test_a_collection_address_admits_all_three_forms(wide_project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(BUDGET, 1_000)
    address = {"layer": "swc", "element": "S0", "path": [{"field": "ports"}]}
    response = _read(wide_project, address, "full")
    assert [hint["form"] for hint in response["hints"]] == ["summary", "index"]
    assert response["measured"]["elements"] == scale_model.WIDE_PORTS_PER_SWC


# A lowered budget on `examples/basic` — the same rule, a model small enough to read


def test_a_lowered_budget_refuses_the_full_swc_layer_of_examples_basic(
    basic_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _read(basic_project, {"layer": "swc"}, "full")["response"] == "read"
    monkeypatch.setattr(BUDGET, 800)
    assert _read(basic_project, {"layer": "swc"}, "full")["error"] == "over-budget"


def test_the_same_lowered_budget_still_answers_the_summary(basic_project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # The hint the refusal above carries is the request that answers.
    monkeypatch.setattr(BUDGET, 800)
    answered = _read(basic_project, {"layer": "swc"}, "summary")
    assert answered["response"] == "read"
    assert [element["name"] for element in answered["value"]] == ["ExampleConsumerSWC", "ExampleSWC"]


def test_a_response_exactly_at_the_budget_is_admitted(basic_project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # The boundary is `<=`.
    snapshot = StateSnapshot(state=load_model_state(Path(basic_project.resolved_model_root)))
    address = Address(layer="swc")
    exact = measure_response(read_answer(snapshot, address, form="full").value, address, "full")
    monkeypatch.setattr(BUDGET, exact)
    assert _read(basic_project, {"layer": "swc"}, "full")["response"] == "read"
    monkeypatch.setattr(BUDGET, exact - 1)
    assert _read(basic_project, {"layer": "swc"}, "full")["error"] == "over-budget"


@pytest.mark.parametrize("form", NARROWING_ORDER)
def test_an_empty_layer_is_answered_in_every_form(basic_project: Any, form: str) -> None:
    # The envelope cannot be narrowed away, so an empty layer answers in every
    # form. Every layer of the base holds an element, so the one sequence is removed.
    for stored in Path(basic_project.resolved_model_root, "sequences").glob("*.yaml"):
        stored.unlink()
    answer = _read(basic_project, {"layer": "sequences"}, form)
    assert answer["response"] == "read"
    assert answer["value"] == ({"count": 0, "identities": []} if form == "index" else [])


# The exception itself, and what no other code carries


def test_read_answer_raises_over_budget_rather_than_returning_a_short_answer(
    wide_snapshot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(BUDGET, 2_000)
    with pytest.raises(OverBudget) as refused:
        read_answer(wide_snapshot, Address(layer="swc"), form="full", registry=reg.current())
    assert refused.value.measured.budget == 2_000
    assert [hint.form for hint in refused.value.hints] == ["summary", "index"]


def test_the_message_names_no_storage_path(wide_project: Any) -> None:
    # No protocol message names where the project is stored.
    message = _read(wide_project, {"layer": "swc"}, "full")["message"]
    assert "/" not in message
    assert "\\" not in message


@pytest.mark.parametrize("code", sorted(set(ERROR_CODES) - {"over-budget"}))
def test_no_other_code_may_be_built_carrying_hints_or_measured(code: str) -> None:
    # A statement about what can be built, not about the dataclass defaults.
    hint = ReadRequest(address=Address(layer="swc"), form="index")
    measured = Measured(wire_bytes=1, budget=1, elements=1)
    with pytest.raises(ValueError, match="over-budget"):
        Violation(error=code, message="the message is not contract", hints=(hint,))
    with pytest.raises(ValueError, match="over-budget"):
        Violation(error=code, message="the message is not contract", measured=measured)
    detail = Violation(error=code, message="the message is not contract").to_json()
    assert "hints" not in detail
    assert "measured" not in detail


def test_over_budget_may_not_be_built_or_parsed_without_measured() -> None:
    """The other direction of the pairing rule: ``over-budget`` always carries ``measured``.

    ``hints`` is legitimately absent at the chain's end, so ``measured`` is all
    that is left to act on; an ``over-budget`` with neither key is refused at
    construction and at parse.
    """
    with pytest.raises(ValueError, match="carries `measured`"):
        Violation(error="over-budget", message="the message is not contract")
    with pytest.raises(ValueError, match="carries `measured`"):
        Violation(error="over-budget", message="x", hints=(ReadRequest(address=Address(layer="swc"), form="index"),))
    with pytest.raises(EnvelopeError, match="carries `measured`"):
        ErrorResult.from_json({"response": "error", "error": "over-budget", "failure_class": "invalid", "message": "x"})


def test_the_shipped_schema_requires_measured_on_over_budget_and_forbids_both_keys_elsewhere() -> None:
    """The schema half of the pairing rule, read off ``$defs/errorDetail`` and ``$defs/errorResult``.

    Without the ``then`` the schema admitted an ``over-budget`` with no
    ``measured``; without the ``else``, a chain on another code the parser
    refuses. ``hints`` stays out of the ``then``: the terminal step omits it.
    """
    schema = json.loads(
        (REPO_ROOT / "packages/tools/tests/fixtures/contracts/editing-protocol/protocol.schema.json").read_text(
            encoding="utf-8"
        )
    )
    for name in ("errorDetail", "errorResult"):
        definition = schema["$defs"][name]
        assert definition["if"]["properties"]["error"]["const"] == "over-budget", name
        assert definition["if"]["required"] == ["error"], name
        assert definition["then"]["required"] == ["measured"], name
        assert "measured" not in definition["required"], name
        assert "hints" not in definition["then"]["required"], name
        forbidden = definition["else"]["not"]["anyOf"]
        assert [branch["required"] for branch in forbidden] == [["hints"], ["measured"]], name
        # Both keys must also stay declared on the object itself: `additionalProperties`
        # is evaluated against `properties` alone and does not see into `then`.
        assert {"hints", "measured"} <= set(definition["properties"]), name


def test_the_shipped_schema_refuses_the_chain_keys_on_every_other_code() -> None:
    """The schema's `else` fires on a real refusal moved to another code, not only is written."""
    schema_path = REPO_ROOT / "packages/tools/tests/fixtures/contracts/editing-protocol/protocol.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    registry = Registry().with_resources([(schema["$id"], Resource.from_contents(schema))])
    validator = Draft202012Validator({"$ref": f"{schema['$id']}#/$defs/errorResult"}, registry=registry)
    refusal = {
        "response": "error",
        "error": "over-budget",
        "failure_class": "invalid",
        "message": "the message is not contract",
        "hints": [{"operation": "read", "address": {"layer": "swc"}, "form": "summary"}],
        "measured": {"wire_bytes": 100000, "budget": 80000, "elements": 538},
    }
    assert validator.is_valid(refusal)
    for code, failure in (("unknown-field", "invalid"), ("stale-baseline", "stale")):
        moved = refusal | {"error": code, "failure_class": failure}
        assert not validator.is_valid(moved), f"{code} may not carry the chain keys"
        assert not validator.is_valid({key: value for key, value in moved.items() if key != "hints"})
        assert not validator.is_valid({key: value for key, value in moved.items() if key != "measured"})
        with pytest.raises(EnvelopeError, match="over-budget"):
            ErrorResult.from_json(moved)


def test_a_read_miss_carries_neither_key(basic_project: Any) -> None:
    response = _read(basic_project, {"layer": "swc", "element": "NoSuchSWC"})
    assert response["error"] == "broken-reference"
    assert "hints" not in response
    assert "measured" not in response


def test_a_refused_change_set_carries_neither_key(basic_project: Any) -> None:
    # `over-budget` is a read refusal alone; no change-set pass produces it.
    response = handle_request({"operation": "change_set"}, project=basic_project)
    assert response["error"] == "schema-violation"
    assert "hints" not in response
    assert "measured" not in response


# The two optional keys, round-tripped through the protocol models


def test_the_refusal_round_trips_through_error_result(wide_project: Any) -> None:
    response = _read(wide_project, {"layer": "swc"}, "full")
    parsed = ErrorResult.from_json(response)
    assert parsed.error == "over-budget"
    assert parsed.primary.measured == Measured(**response["measured"])
    assert parsed.primary.hints == tuple(ReadRequest.from_json(hint) for hint in response["hints"])
    assert parsed.to_json() == response


def test_an_empty_hints_array_is_refused_rather_than_read_as_no_hints(wide_project: Any) -> None:
    # The terminal step omits the key, as `addresses` does; an empty array says nothing.
    response = _read(wide_project, {"layer": "swc"}, "full") | {"hints": []}
    with pytest.raises(ValueError, match="hints"):
        ErrorResult.from_json(response)


@pytest.mark.parametrize(
    "bad",
    [
        {"wire_bytes": -1, "budget": 1, "elements": 1},
        {"wire_bytes": 1, "budget": 1},
        {"wire_bytes": 1, "budget": 1, "elements": 1, "extra": 1},
        {"wire_bytes": "1", "budget": 1, "elements": 1},
    ],
)
def test_a_malformed_measured_block_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Measured.from_json(bad)


def test_a_hint_narrows_the_address_it_was_asked_about_and_never_moves_it(wide_snapshot: Any) -> None:
    # A hint that moved the address would send a client where it had not asked.
    address = Address(layer="swc", element="S0", path=(FieldStep("ports"),))
    registry = reg.current()
    resolution = resolve(address, wide_snapshot, registry=registry, placement={"group": "wide"})
    shape = address_shape(address, resolution)
    layer = registry.layer("swc")
    assert narrower_forms(resolution, shape, "full", registry, layer) == ("summary", "index")
    assert narrower_forms(resolution, shape, "index", registry, layer) == ()


def test_a_whole_value_list_has_no_projection_below_its_full_answer(
    basic_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``whole_value`` **list** publishes one value, so ``full`` is the whole chain.

    Its ``summary`` is verbatim and its index strictly larger, so neither is a
    step below ``full``; a served ``summary`` or ``index`` carries no less than
    ``full``, so both offer ``full``.
    """
    monkeypatch.setattr(BUDGET, 100)
    address = {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "satisfies"}]}
    exhausted = _read(basic_project, address, "full")
    assert exhausted["error"] == "over-budget"
    assert "hints" not in exhausted
    for form in (None, "summary", "index"):
        served = _read(basic_project, address, form)
        assert [hint["form"] for hint in served["hints"]] == ["full"], form


def test_the_default_read_of_a_whole_value_list_offers_the_full_that_answers(
    basic_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The three-byte window: the default form refuses where ``full`` at the same address answers.

    The default ``summary`` is three wire bytes larger than ``full`` (the longer
    echoed ``form``); a chain starting after ``full`` would call such an address
    unreadable, so the refusal offers ``full``. The budget is measured off the ``full`` answer.
    """
    address = {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "satisfies"}]}
    full = _read(basic_project, address, "full")
    exact = measure_response(full["value"], Address.from_json(address), "full")

    monkeypatch.setattr(BUDGET, exact)
    refused = _read(basic_project, address, None)
    assert refused["error"] == "over-budget"
    assert refused["measured"]["wire_bytes"] == exact + 3
    assert [hint["form"] for hint in refused["hints"]] == ["full"]

    replayed = _read(basic_project, address, refused["hints"][0]["form"])
    assert replayed["response"] == "read"
    assert replayed["form"] == "full"
    assert replayed["value"] == full["value"]


def test_an_explicit_index_on_a_whole_value_list_offers_the_full_that_answers(
    basic_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same rule for an explicit ``index``, which wraps the list and is larger than ``full``.

    The refused index offers ``full``, which answers; the chain is not exhausted.
    The budget is measured off the two answers.
    """
    address = {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "satisfies"}]}
    full = _read(basic_project, address, "full")
    index = _read(basic_project, address, "index")
    full_size = measure_response(full["value"], Address.from_json(address), "full")
    index_size = measure_response(index["value"], Address.from_json(address), "index")
    assert index_size > full_size, "the index of a whole_value list wraps the values it repeats"

    monkeypatch.setattr(BUDGET, index_size - 1)
    refused = _read(basic_project, address, "index")
    assert refused["error"] == "over-budget"
    assert refused["measured"]["wire_bytes"] == index_size
    assert [hint["form"] for hint in refused["hints"]] == ["full"]

    replayed = _read(basic_project, address, refused["hints"][0]["form"])
    assert replayed["response"] == "read"
    assert replayed["form"] == "full"
    assert replayed["value"] == full["value"]


def test_a_whole_value_map_offers_its_index_from_every_wider_form(
    basic_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``swc.memory`` is ``whole_value`` too, and its ``index`` (the keys alone) is narrower.

    The container's shape decides the chain, not ``member_identity`` alone.
    """
    monkeypatch.setattr(BUDGET, 100)
    address = {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "memory"}]}
    for form in (None, "summary"):
        response = _read(basic_project, address, form)
        assert response["error"] == "over-budget", form
        assert [hint["form"] for hint in response["hints"]] == ["full", "index"], form
    from_full = _read(basic_project, address, "full")
    assert from_full["error"] == "over-budget"
    assert [hint["form"] for hint in from_full["hints"]] == ["index"]
    exhausted = _read(basic_project, address, "index")
    assert exhausted["error"] == "over-budget"
    assert "hints" not in exhausted


def test_a_wide_whole_value_maps_index_really_is_the_smaller_answer() -> None:
    """A wide map's index is smaller than the map; 400 entries is not a threshold.

    On one entry the ``{"count", "identities"}`` wrapper costs more than it
    saves: a hint is a form the address admits, not a promise that it fits.
    """
    field = reg.current().element_kind("swc").get_field("memory")
    wide = {f"RAM{i:03d}": 4096 + i for i in range(400)}
    assert measure(collection_index(wide, field)) < measure(wide)


def test_a_collection_whose_members_summarize_whole_does_not_offer_summary(
    basic_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mapping_set.maps`` skips ``summary``: the summary rules select every field of ``map``.

    The ``summary`` would be the same value, larger on the wire, so it is no
    step; a served ``summary`` offers ``full`` and the narrower ``index``. The
    test writes its own mapping set; a read resolves nothing across layers.
    """
    monkeypatch.setattr(BUDGET, 100)
    (model_element_path(Path(basic_project.resolved_model_root), "ExampleMappings", layer="mapping_sets")).write_text(
        "name: ExampleMappings\nmaps:\n- application: float32\n  implementation: float32_base\n",
        encoding="utf-8",
    )
    address = {"layer": "mapping_sets", "element": "ExampleMappings", "path": [{"field": "maps"}]}
    for form in (None, "summary"):
        response = _read(basic_project, address, form)
        assert response["error"] == "over-budget", form
        assert [hint["form"] for hint in response["hints"]] == ["full", "index"], form
    from_full = _read(basic_project, address, "full")
    assert from_full["error"] == "over-budget"
    assert [hint["form"] for hint in from_full["hints"]] == ["index"]
    exhausted = _read(basic_project, address, "index")
    assert exhausted["error"] == "over-budget"
    assert "hints" not in exhausted


def test_a_collection_whose_members_do_narrow_still_offers_summary(
    basic_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the predicate: a member kind whose summary narrows offers ``summary``.

    ``client_server.operations`` narrows by its ``arguments`` count;
    ``data_elements`` and ``operation.arguments`` narrow by an optional field
    the summary rules drop.
    """
    monkeypatch.setattr(BUDGET, 100)
    address = {"layer": "interfaces", "element": "IDiagnosticService", "path": [{"field": "operations"}]}
    refused = _read(basic_project, address, "full")
    assert refused["error"] == "over-budget"
    assert [hint["form"] for hint in refused["hints"]] == ["summary", "index"]

    elements = {"layer": "interfaces", "element": "IExample", "path": [{"field": "data_elements"}]}
    from_full = _read(basic_project, elements, "full")
    assert from_full["error"] == "over-budget"
    assert [hint["form"] for hint in from_full["hints"]] == ["summary", "index"]
    for form in (None, "summary"):
        default = _read(basic_project, elements, form)
        assert default["error"] == "over-budget", form
        assert [hint["form"] for hint in default["hints"]] == ["index"], form

    arguments = {
        "layer": "interfaces",
        "element": "IDiagnosticService",
        "path": [{"field": "operations"}, {"member": "ReadDtc"}, {"field": "arguments"}],
    }
    from_full = _read(basic_project, arguments, "full")
    assert from_full["error"] == "over-budget"
    assert [hint["form"] for hint in from_full["hints"]] == ["summary", "index"]
    for form in (None, "summary"):
        default = _read(basic_project, arguments, form)
        assert default["error"] == "over-budget", form
        assert [hint["form"] for hint in default["hints"]] == ["index"], form


def test_the_summary_of_such_a_collection_really_is_the_full_answer() -> None:
    """Every collection the chain skips ``summary`` for answers the same value in both forms.

    Derived over every declared collection. Does not reach the wire's
    three-byte echoed ``form``, asserted at the response level above.
    """
    registry = reg.current()
    collections = [
        (kind.kind, field.name, field.collection)
        for kind in registry.kinds.values()
        for field in kind.fields
        if field.collection is not None
    ]
    assert collections, "the shipped contract declares at least one collection"

    skipped: set[str] = set()
    for owner, field_name, collection in collections:
        offered = _summary_narrows_collection(collection, registry)
        if collection.member_identity == "whole_value":
            assert not offered, f"{owner}.{field_name} answers verbatim"
            continue
        narrows = any(
            frozenset(summary_field_names(registry.element_kind(member), registry))
            != frozenset(registry.element_kind(member).field_names)
            or any(declared.collection is not None for declared in registry.element_kind(member).fields)
            for member in collection.member_kinds
        )
        assert offered is narrows, f"{owner}.{field_name}"
        if not offered:
            skipped.add(f"{owner}.{field_name}")

    # Named rather than counted: a contract change that moves one of these
    # changes what a client is told to replay. A member kind with an optional
    # field (`description`, a stated `name`) leaves the set.
    assert skipped == {
        "task.runnables",
        # A `map` declares the application type it pairs and the implementation
        # type that stores it and nothing else.
        "mapping_set.maps",
        # Entry kinds that declare the area they name and nothing else.
        "runnable.can_enter",
        "runnable.runs_inside",
    }


# The chain at every shape, over every address the contract declares


def _summary_is_a_narrower_projection(kind: Any, registry: reg.Registry) -> bool:
    """Whether ``summarize`` publishes less of *kind* than ``publish_element`` does.

    Restated from what the two functions do, never by calling the predicate
    under test. An empty selection is not a narrowing: ``{}`` is the full
    answer for ``conversion: {}``, which is a declaration.
    """
    names = frozenset(summary_field_names(kind, registry))
    if not names:
        return False
    return names != frozenset(kind.field_names) or any(field.collection is not None for field in kind.fields)


def _index_is_a_narrower_projection(collection: Any, registry: reg.Registry) -> bool:
    """Whether a LIST collection's index drops something its members publish.

    An index narrows where a member kind declares a field the identity does not
    name. Restated from the identity, never by calling
    ``_index_narrows_collection``; a map is decided by its caller.
    """
    if collection.member_identity == "whole_value":
        return False
    identity = {collection.identity_field} if collection.member_identity == "field" else set(collection.identity_fields)
    return any(set(registry.element_kind(name).field_names) - identity for name in collection.member_kinds)


def _declared_addresses(registry: reg.Registry) -> list[tuple[str, Any, Resolved, Any]]:
    """Every address **shape** the contract declares, as ``(name, shape, resolution, layer)``.

    Built from the registry, not a project; the resolutions carry no value,
    since the chain is a function of the declaration alone. The shapes are
    spelled as :func:`~swcraft_tools.editing.address.resolve` produces them.
    """
    rows: list[tuple[str, Any, Resolved, Any]] = []
    for layer in registry.layers:
        rows.append((f"layer:{layer.name}", "layer", Resolved(value=[]), layer))
    for kind in registry.kinds.values():
        owner = registry.layer(kind.layer)
        shape = "field" if kind.member_only else "element"
        rows.append((f"kind:{kind.kind}", shape, Resolved(value=None, kind=kind), owner))
        for field in kind.fields:
            if field.collection is None:
                continue
            resolution = Resolved(value=None, field=field, collection=field.collection)
            rows.append((f"collection:{kind.kind}.{field.name}", "collection", resolution, owner))
    return rows


def test_the_narrowing_chain_offers_one_step_per_distinct_projection_at_every_shape() -> None:
    """A form is a step of the chain exactly when it publishes something the form above does not.

    Over every declared layer, element kind and collection, restated from what
    the publishing functions do. Does not reach the wire's three-byte echoed
    ``form`` or whether a step fits.
    """
    registry = reg.current()

    for name, shape, resolution, layer in _declared_addresses(registry):
        if shape == "layer":
            narrows_summary = any(_summary_is_a_narrower_projection(k, registry) for k in layer.top_level_kinds)
            narrows_index = True
        elif shape == "collection":
            collection = resolution.collection
            narrows_summary = collection.member_identity != "whole_value" and any(
                _summary_is_a_narrower_projection(registry.element_kind(member), registry)
                for member in collection.member_kinds
            )
            narrows_index = collection.kind == "map" or _index_is_a_narrower_projection(collection, registry)
        else:
            narrows_summary = _summary_is_a_narrower_projection(resolution.kind, registry)
            narrows_index = False

        expected = ("full",) + (("summary",) if narrows_summary else ()) + (("index",) if narrows_index else ())
        assert _narrowing_chain(resolution, shape, registry, layer) == expected, name


def test_a_served_form_that_is_not_a_step_continues_the_chain_from_where_it_stands() -> None:
    """``narrower_forms`` for every served form, at every declared shape.

    A served step is followed by the steps after it; a served form that is not
    a step carries no less than ``full``, so the whole chain is offered.
    Computed without calling :func:`~swcraft_tools.editing.read._chain_position`.
    Does not reach which combinations ``_answer_at`` can serve: some rows
    assert a total function rather than a reachable response.
    """
    registry = reg.current()
    for name, shape, resolution, layer in _declared_addresses(registry):
        chain = _narrowing_chain(resolution, shape, registry, layer)
        for served in NARROWING_ORDER:
            expected = chain[chain.index(served) + 1 :] if served in chain else chain
            assert narrower_forms(resolution, shape, served, registry, layer) == expected, (name, served)


def test_every_declared_address_shape_is_covered_by_the_oracle() -> None:
    """The walk above reaches every declaration, counted from the registry, so it is not vacuous."""
    registry = reg.current()
    rows = _declared_addresses(registry)
    kinds = list(registry.kinds.values())
    collections = [field for kind in kinds for field in kind.fields if field.collection is not None]

    assert [name for name, *_ in rows if name.startswith("layer:")] == [
        f"layer:{layer.name}" for layer in registry.layers
    ]
    assert len([name for name, *_ in rows if name.startswith("kind:")]) == len(kinds)
    assert len([name for name, *_ in rows if name.startswith("collection:")]) == len(collections)
    assert kinds and collections and registry.layers


def test_the_kinds_whose_summary_is_their_full_answer_are_named() -> None:
    """Which kinds the chain skips ``summary`` for — named, so a contract change reads as one.

    A kind is here when the summary rules select all its fields, or none (an
    empty selection is the full answer of an empty nested object). Also
    asserted: every layer keeps its ``summary`` step, and every top-level kind
    carries a field beyond its identity, so a layer's ``index`` narrows.
    """
    registry = reg.current()
    whole = {kind.kind for kind in registry.kinds.values() if not _summary_is_a_narrower_projection(kind, registry)}
    assert whole == {
        # The port and the two alternative owners, all scalar references the
        # summary rules select.
        "connector_endpoint",
        "task_runnable_ref",
        "disabled_mode",
        # Two fields, both its identity.
        "map",
        # Only optional scalars under an `element_kind` field: the selection is
        # empty, and `{}` is the full answer of `conversion: {}`.
        "conversion",
        "range",
        # The same ground: only two optional bounds, each reached through an
        # `element_kind` field.
        "linear_limits",
        "conversion_limits",
        # The one field each declares is the identity.
        "can_enter_area",
        "runs_inside_area",
    }
    assert all(registry.element_kind(name).member_only for name in whole), (
        "a top-level kind whose summary is its full answer would cost its layer the summary step"
    )
    for layer in registry.layers:
        assert any(_summary_is_a_narrower_projection(kind, registry) for kind in layer.top_level_kinds), layer.name
        for kind in layer.top_level_kinds:
            assert set(kind.field_names) - {layer.identity_field}, (
                f"'{kind.kind}' is its identity field alone, so a layer index would drop nothing"
            )


def test_a_required_object_narrows_like_any_other_member_address(
    basic_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At ``connector.from``, the one required object, the chain reads the declaration as at a member.

    The endpoint's three fields are references the summary selects, so an
    explicit ``summary`` is served ``full`` and a refused ``full`` offers no
    ``summary``. The budget is measured off the answer.

    The narrowing at a member address is held at a port by ``test_editing_summary.py``'s
    :func:`test_a_port_summary_publishes_a_set_interface_and_omits_an_unset_one`, and over
    every kind by this module's :func:`test_the_kinds_whose_summary_is_their_full_answer_are_named`.
    """
    address = {"layer": "connectors", "element": "Example_to_Consumer", "path": [{"field": "from"}]}
    full = _read(basic_project, address, "full")
    summary = _read(basic_project, address, "summary")
    assert summary["form"] == "full", "an explicit summary of a whole kind falls back to full"
    assert summary["value"] == full["value"]
    assert set(full["value"]) == {"swc", "composition", "port"}

    size = measure_response(full["value"], Address.from_json(address), "full")
    monkeypatch.setattr(BUDGET, size - 1)
    refused = _read(basic_project, address, "full")
    assert refused["error"] == "over-budget"
    assert "hints" not in refused, "no narrower form exists at a whole kind's address"


# The discover hint item and the addressless refusal, built by hand and held to
# the same verdict by the parser (`editing/protocol.py`) and the published schema


def _error_result_validator() -> Draft202012Validator:
    """A validator for the shipped ``protocol.schema.json#/$defs/errorResult``, never a copy."""
    schema = json.loads(
        (REPO_ROOT / "packages/tools/tests/fixtures/contracts/editing-protocol/protocol.schema.json").read_text(
            encoding="utf-8"
        )
    )
    registry = Registry().with_resources([(schema["$id"], Resource.from_contents(schema))])
    return Draft202012Validator({"$ref": f"{schema['$id']}#/$defs/errorResult"}, registry=registry)


def _refusal(hints: Any, **overrides: Any) -> dict[str, Any]:
    """One ``over-budget`` document carrying *hints*, as a server would send it."""
    return (
        {
            "response": "error",
            "error": "over-budget",
            "failure_class": "invalid",
            "message": "the message is not contract",
            "measured": {"wire_bytes": 100_000, "budget": 80_000, "elements": 10},
        }
        | overrides
        | {"hints": hints}
    )


@pytest.mark.parametrize(
    "hint",
    [
        {"operation": "discover"},
        {"operation": "discover", "layer": "swc"},
    ],
    ids=["whole-project", "one-layer"],
)
def test_a_hint_may_be_a_discover_request_and_round_trips(hint: dict[str, Any]) -> None:
    """``$defs/readHints`` admits a ``discoverRequest``, parsed as a :class:`DiscoverRequest`.

    ``discover`` has no address, so its narrower requests are discoveries.
    """
    document = _refusal([hint], addresses=[{"layer": "swc"}])
    parsed = ErrorResult.from_json(document)
    assert parsed.primary.hints == (DiscoverRequest.from_json(hint),)
    assert isinstance(parsed.primary.hints[0], DiscoverRequest)
    assert parsed.to_json() == document
    assert _error_result_validator().is_valid(document)


def test_a_chain_may_mix_read_and_discover_hints_in_order() -> None:
    # The two shapes are alternatives inside one array, in the chain's order.
    read_hint = {"operation": "read", "address": {"layer": "swc"}, "form": "summary"}
    discover_hint = {"operation": "discover", "layer": "swc"}
    document = _refusal([read_hint, discover_hint])
    parsed = ErrorResult.from_json(document)
    assert [type(hint) for hint in parsed.primary.hints] == [ReadRequest, DiscoverRequest]
    assert parsed.to_json() == document
    assert _error_result_validator().is_valid(document)


def test_a_change_set_is_never_a_hint() -> None:
    """A hint is a cheaper way to look, so a ``change_set`` hint is refused on both sides.

    The shared discriminator ``request_operation`` admits all three operations,
    so without the explicit branch a ``change_set`` hint was admitted.
    """
    change_set = {
        "operation": "change_set",
        "baseline": "rev1:9f4c2a7be3d18056",
        "operations": [{"operation": "remove", "address": {"layer": "swc", "element": "S0"}}],
    }
    document = _refusal([change_set])
    with pytest.raises(EnvelopeError, match="not a 'change_set'"):
        ErrorResult.from_json(document)
    assert not _error_result_validator().is_valid(document)


@pytest.mark.parametrize("code", sorted(set(ERROR_CODES) - {"over-budget"}))
def test_a_discover_hint_is_refused_on_every_code_but_over_budget(code: str) -> None:
    # The pairing guard is about the key, not about what is inside it.
    with pytest.raises(ValueError, match="over-budget"):
        Violation(error=code, message="the message is not contract", hints=(DiscoverRequest(layer="swc"),))


@pytest.mark.parametrize("hints", [[], None], ids=["empty-array", "explicit-null"])
def test_an_empty_hints_array_names_both_shapes_it_would_have_admitted(hints: Any) -> None:
    with pytest.raises(EnvelopeError, match="read or discover request"):
        ErrorResult.from_json(_refusal(hints))


def test_an_addressless_over_budget_composes_an_error_result_with_addresses_omitted() -> None:
    """The refusal ``discover`` makes: no address, ``addresses`` omitted, ``measured`` intact.

    ``engine._over_budget`` is the one composition site for both refusals, so
    they cannot drift apart.
    """
    refused = OverBudget(
        address=None,
        measured=Measured(wire_bytes=100_000, budget=RESPONSE_BUDGET_WIRE_BYTES, elements=10),
        hints=(DiscoverRequest(layer="swc"),),
    )
    assert refused.address is None
    document = _over_budget(refused).result.to_json()
    assert document["error"] == "over-budget"
    assert "addresses" not in document
    assert document["hints"] == [{"operation": "discover", "layer": "swc"}]
    assert document["measured"] == {"wire_bytes": 100_000, "budget": RESPONSE_BUDGET_WIRE_BYTES, "elements": 10}
    assert ErrorResult.from_json(document).to_json() == document
    assert _error_result_validator().is_valid(document)


def test_the_addressless_message_names_the_sizes_and_no_address() -> None:
    # The two message shapes differ by the address clause and nothing else.
    measured = Measured(wire_bytes=100_000, budget=80_000, elements=10)
    addressless = str(OverBudget(address=None, measured=measured, hints=()))
    addressed = str(OverBudget(address=Address(layer="swc"), measured=measured, hints=()))
    assert addressless == (
        "the response measures 100000 wire bytes over 10 element(s), "
        "above the server's 80000-byte budget; it is refused rather than truncated"
    )
    assert addressed == addressless.replace("the response", "the response at {'layer': 'swc'}", 1)
    assert "{" not in addressless


def test_measured_elements_carries_a_descriptor_count_for_an_addressless_refusal() -> None:
    """``elements`` counts the refused response: for a refused ``discover``, the layer descriptors."""
    descriptors = len(reg.current().layers)
    # The shipped contract's declared layers; a new layer moves it.
    assert descriptors == 15
    refused = OverBudget(
        address=None,
        measured=Measured(wire_bytes=100_000, budget=80_000, elements=descriptors),
        hints=(),
    )
    document = _over_budget(refused).result.to_json()
    assert document["measured"]["elements"] == descriptors
    assert f"over {descriptors} element(s)" in document["message"]
    assert "hints" not in document  # an exhausted chain still omits the key
    assert _error_result_validator().is_valid(document)

    schema = json.loads(
        (REPO_ROOT / "packages/tools/tests/fixtures/contracts/editing-protocol/protocol.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert "LAYER DESCRIPTORS of a refused discovery" in schema["$defs"]["measured"]["description"]


@pytest.mark.parametrize(
    ("address", "form"),
    [
        ({"layer": "swc"}, "full"),
        ({"layer": "swc"}, "summary"),
        ({"layer": "swc"}, None),
        ({"layer": "swc", "element": "S0"}, "full"),
        ({"layer": "swc", "element": "S0", "path": [{"field": "ports"}]}, "full"),
        ({"layer": "swc", "element": "S0", "path": [{"field": "ports"}]}, "summary"),
    ],
)
def test_no_hint_of_any_refusal_ever_carries_a_different_address(
    wide_project: Any, monkeypatch: pytest.MonkeyPatch, address: dict[str, Any], form: str | None
) -> None:
    """No hint of an engine refusal moves the address, at every shape the refusal path reaches.

    Descending into a collection is the agent's own move, never a step this
    chain offers.
    """
    monkeypatch.setattr(BUDGET, 120)
    response = _read(wide_project, address, form)
    assert response["error"] == "over-budget", response
    assert response["addresses"] == [address]
    # Non-vacuity: a `for` over an absent key asserts nothing.
    assert response["hints"], f"{address} at {form} carried no chain to check"
    for hint in response["hints"]:
        assert hint["operation"] == "read", hint
        assert hint["address"] == address, hint


# The paged chain position. The bounded claim: a refusal at a layer, a collection
# or a member slice never ends with an empty chain wherever the set is longer than
# one declared page; what can still dead-end is
# `test_the_addresses_that_can_still_dead_end_are_named`.


#: A `whole_value` list long enough to page. `swc.satisfies` narrows in neither
#: form, so there the paged step is the only step.
_LONG_LINKS = 150


@pytest.fixture
def long_list_project(tmp_path: Path) -> Any:
    """A project whose one component carries more links than a page holds.

    The links name no declared requirement: a read validates nothing. Not added
    to the shared wide model, whose element counts other suites assert.
    """
    root = tmp_path / "long-list-project"
    model = root / "model"
    (model / "hardware").mkdir(parents=True)
    (model / "hardware" / "CPU.yaml").write_text(
        yaml.safe_dump({"name": "CPU", "kind": "processor"}, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )
    (model / "swc" / "long").mkdir(parents=True)
    (model / "swc" / "long" / "LongSWC.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "LongSWC",
                "type": "application",
                "satisfies": [f"REQ_{index:03d}@1" for index in range(_LONG_LINKS)],
            },
            default_flow_style=False,
            sort_keys=False,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return require_project(str(_manifest(root)))


def test_a_layer_longer_than_one_page_hints_its_first_page(wide_project: Any) -> None:
    """At the shipped budget the ``connectors`` chain ends with its first page at the served form.

    The page size is :data:`~swcraft_tools.editing.read.HINT_PAGE_SIZE`, never a
    size searched for by re-measuring.
    """
    refused = _read(wide_project, {"layer": "connectors"}, "full")
    assert refused["error"] == "over-budget"
    assert refused["measured"]["elements"] == scale_model.WIDE_CONNECTORS
    assert [hint["form"] for hint in refused["hints"]] == ["summary", "index", "full"]
    assert refused["hints"][-1] == {
        "operation": "read",
        "address": {"layer": "connectors"},
        "form": "full",
        "limit": HINT_PAGE_SIZE,
    }


def test_the_paged_hint_answers_and_says_how_much_is_left(wide_project: Any) -> None:
    """Replaying the paged hint verbatim answers a bounded page that carries its continuation."""
    hint = _read(wide_project, {"layer": "connectors"}, "full")["hints"][-1]
    replayed = handle_request(hint, project=wide_project)
    assert replayed["response"] == "read"
    assert replayed["form"] == "full"
    assert len(replayed["value"]) == HINT_PAGE_SIZE
    assert replayed["page"]["total"] == scale_model.WIDE_CONNECTORS
    assert replayed["page"]["returned"] == HINT_PAGE_SIZE
    assert replayed["page"]["truncated"] is True
    assert replayed["page"]["next"] == replayed["value"][-1]["name"]


def test_a_set_of_one_page_or_less_is_offered_no_page(wide_project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A set of one page is offered no page: that hint would replay the refused request.

    The rule is about the count, never how far over the budget the answer was.
    """
    assert scale_model.WIDE_SWCS <= HINT_PAGE_SIZE, "the premise this test is about"
    for budget in (RESPONSE_BUDGET_WIRE_BYTES, 4_000, 800):
        monkeypatch.setattr(BUDGET, budget)
        refused = _read(wide_project, {"layer": "swc"}, "full")
        assert refused["error"] == "over-budget", budget
        assert [hint["form"] for hint in refused["hints"]] == ["summary", "index"], budget
        assert all("limit" not in hint for hint in refused["hints"]), budget


def test_a_collection_that_narrows_in_neither_form_skips_straight_to_the_paged_hint(
    long_list_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A collection that narrows in neither form has the page as its one step from ``full``.

    A served ``summary`` offers ``full`` first, then the page. The budget is
    the exact size of the paged answer, so the chain is walked to an answer.
    """
    address = {"layer": "swc", "element": "LongSWC", "path": [{"field": "satisfies"}]}
    snapshot = StateSnapshot(state=load_model_state(Path(long_list_project.resolved_model_root)))
    offered = read_answer(
        snapshot, Address.from_json(address), form="full", limit=HINT_PAGE_SIZE, registry=reg.current()
    )
    exact = measure_response(offered.value, Address.from_json(address), offered.form, page=offered.page)
    monkeypatch.setattr(BUDGET, exact)

    from_full = _read(long_list_project, address, "full")
    assert from_full["error"] == "over-budget"
    assert from_full["measured"]["elements"] == _LONG_LINKS
    assert from_full["hints"] == [{"operation": "read", "address": address, "form": "full", "limit": HINT_PAGE_SIZE}], (
        "the page is the whole chain where no form narrows"
    )

    from_summary = _read(long_list_project, address, "summary")
    assert [hint["form"] for hint in from_summary["hints"]] == ["full", "summary"]
    assert [hint.get("limit") for hint in from_summary["hints"]] == [None, HINT_PAGE_SIZE]

    replayed = handle_request(from_full["hints"][0], project=long_list_project)
    assert replayed["response"] == "read"
    assert replayed["page"]["total"] == _LONG_LINKS
    assert len(replayed["value"]) == HINT_PAGE_SIZE


def test_the_paged_hint_keeps_the_cursor_the_request_carried(
    wide_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A paged hint keeps ``after``: a hint changes the width of the request and nothing else."""
    monkeypatch.setattr(BUDGET, 20_000)
    first = _read(wide_project, {"layer": "connectors"}, "full")["hints"][-1]
    cursor = handle_request(first, project=wide_project)["page"]["next"]

    refused = handle_request(dict(first) | {"limit": 400, "after": cursor}, project=wide_project)
    assert refused["error"] == "over-budget"
    assert refused["hints"][-1] == {
        "operation": "read",
        "address": {"layer": "connectors"},
        "form": "full",
        "limit": HINT_PAGE_SIZE,
        "after": cursor,
    }


# The bounded claim, over every set address the wide fixture holds


def _set_requests(project: Any, registry: reg.Registry) -> list[tuple[str, dict[str, Any]]]:
    """Every layer and member-slice request the wide fixture admits, and some collections.

    Derived from the registry and the model; an unpopulated address drops out
    by its miss. The collection half covers the first two elements of each
    layer only: it does not reach a later element whose collection is wider.
    """
    requests: list[tuple[str, dict[str, Any]]] = []
    for layer in registry.layers:
        requests.append((f"layer:{layer.name}", {"operation": "read", "address": {"layer": layer.name}}))
        for kind in layer.top_level_kinds:
            for field in kind.fields:
                if field.collection is None:
                    continue
                requests.append(
                    (
                        f"slice:{layer.name}.{field.name}",
                        {"operation": "read", "address": {"layer": layer.name}, "members": field.name},
                    )
                )
        index = handle_request(
            {"operation": "read", "address": {"layer": layer.name}, "form": "index"}, project=project
        )
        for identity in index["value"]["identities"][:2]:
            for kind in layer.top_level_kinds:
                for field in kind.fields:
                    if field.collection is None:
                        continue
                    address = {"layer": layer.name, "element": identity, "path": [{"field": field.name}]}
                    probe = handle_request({"operation": "read", "address": address}, project=project)
                    if probe.get("response") != "read" and probe.get("error") != "over-budget":
                        continue  # this element is not of that kind: a miss, not an address
                    requests.append(
                        (f"collection:{layer.name}/{identity}.{field.name}", {"operation": "read", "address": address})
                    )
    return requests


def test_no_refusal_at_a_set_address_of_the_wide_fixture_ends_with_an_empty_chain(wide_project: Any) -> None:
    """The bounded claim, walked at the shipped budget over every set request of the wide fixture.

    Whatever refuses carries a chain of reads at the asked address. Bounded to
    set addresses of one model on one contract. The inventory is asserted
    first, because no ``collection:`` request refuses and an empty generator
    would otherwise pass.
    """
    registry = reg.current()
    requests = _set_requests(wide_project, registry)
    inventory = Counter(name.split(":", 1)[0] for name, _ in requests)
    # `layer` counts the declared layers; `slice` one per collection field per
    # top-level kind of a layer (a field several kinds of a layer declare is
    # named once per kind); `collection` the same over each layer's first two
    # elements of the wide fixture. A collection field declared on a top-level
    # kind moves `slice`, and `collection` too where the wide fixture populates
    # that kind.
    assert inventory == {"layer": 15, "slice": 32, "collection": 23}, inventory

    refused: list[tuple[str, str | None]] = []
    for name, request in requests:
        for form in (None, *NARROWING_ORDER):
            response = handle_request(request | ({"form": form} if form else {}), project=wide_project)
            if response.get("error") != "over-budget":
                continue
            refused.append((name, form))
            assert response.get("hints"), f"{name} at {form} ended with an empty chain"
            for hint in response["hints"]:
                assert hint["operation"] == "read", (name, form, hint)
                assert hint["address"] == request["address"], (name, form, hint)

    # Non-vacuity: named rather than counted, as each row is an answer a client
    # cannot be given.
    assert set(refused) == {
        ("layer:swc", "full"),
        ("layer:connectors", "full"),
        ("slice:swc.ports", None),
        ("slice:swc.ports", "full"),
        ("slice:swc.ports", "summary"),
    }


def test_the_addresses_that_can_still_dead_end_are_named(
    wide_project: Any, basic_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the chain does **not** reach: two shapes.

    * an **element**, a **member** or a scalar whose own value exceeds the
      budget: nothing to window, so ``measured`` is the whole answer;
    * a set of **one page or less** whose narrowest form still does not fit.
    """
    # One byte below the scalar read's own wire size (143); every other read
    # below is larger, so each refuses too. Moves with the wire unit.
    monkeypatch.setattr(BUDGET, 142)
    scalar = _read(wide_project, {"layer": "swc", "element": "S0", "path": [{"field": "name"}]}, "full")
    assert scalar["error"] == "over-budget"
    assert "hints" not in scalar

    # One port, addressed by its member identity: no set to window.
    member = _read(
        wide_project,
        {"layer": "swc", "element": "S0", "path": [{"field": "ports"}, {"member": "p0"}]},
        "summary",
    )
    assert member["error"] == "over-budget"
    assert "hints" not in member

    element = _read(basic_project, {"layer": "swc", "element": "ExampleSWC"}, "summary")
    assert element["error"] == "over-budget"
    assert "hints" not in element

    one_page = _read(wide_project, {"layer": "swc"}, "index")
    assert one_page["error"] == "over-budget"
    assert one_page["measured"]["elements"] == scale_model.WIDE_SWCS
    assert "hints" not in one_page
