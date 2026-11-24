"""``protocol.schema.json`` accepts exactly what ``editing/protocol.py`` accepts.

The oracle **mutates** every shipped vector and synthetic seed — a key dropped,
set to one of :data:`WRONG_VALUES` or a declared sibling, an unexpected key
added, an array emptied, a first member duplicated — and asserts that the schema
and the parser give the **same verdict** on every mutant.

What it does **not** reach (each exemption is pinned by a test asserting it
still hides a real divergence):

* the ``layers`` inventory of a discover response (:data:`EXEMPT_SUBTREE`),
  which :class:`DiscoverResponse` keeps as the registry produced it, including a
  mutant the ``form`` overlays refuse only inside it;
* an ``additional`` repeating the primary and a ``join`` repeating an alias:
  Draft 2020-12 cannot compare one value with another;
* semantics (passes B and C): parity here is parity of *shape*;
* a defect needing two edits, an edit deeper than four steps, or a shape no
  vector or seed carries.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT
from jsonschema import Draft202012Validator, ValidationError, validators
from referencing import Registry, Resource
from swcraft_tools import query
from swcraft_tools.editing import protocol
from swcraft_tools.editing.address import LAYER_NAME_PATTERN
from swcraft_tools.editing.passes import pass_a
from swcraft_tools.editing.protocol import (
    # The first two are private closed vocabularies, imported rather than
    # re-spelled: a copy can stop matching the parser's, and then the mutants
    # stop reaching the rule they were generated for.
    _ARXML_VERSIONS,
    _CHANGE_SET_STATUSES,
    CHANGE_OPERATION_NAMES,
    ERROR_CODES,
    FAILURE_CLASSES,
    READ_FORMS,
    EnvelopeError,
    ProtocolError,
    ReadRequest,
    parse_response,
)
from swcraft_tools.query import QueryError, parse_request
from test_editing_protocol_models import PARITY_CORPUS

CONTRACTS_DIR = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "contracts" / "editing-protocol"
PROTOCOL_PATH = CONTRACTS_DIR / "protocol.schema.json"
VECTOR_PATHS = sorted(
    [*(CONTRACTS_DIR / "examples").glob("*.json"), *(CONTRACTS_DIR / "negative").glob("*.json")],
    key=lambda path: path.as_posix(),
)

#: The one subtree where the schema is deliberately the stricter side. Keyed by
#: the response discriminator it belongs to, so an ``errorResult`` that grew a
#: ``layers`` key would not inherit the exemption.
EXEMPT_SUBTREE: dict[str, str] = {"discover": "layers"}

#: The values a mutant substitutes, each legal JSON a stale or hostile peer can
#: put at any key. ``{}`` and ``[]`` are unhashable, so a closed-vocabulary
#: membership test must answer ``EnvelopeError`` for them, never a bare
#: ``TypeError``. ``""``, ``-1`` and ``3.5`` are in-type but outside a bound
#: (``minLength: 1``, ``minimum: 0``, ``type: integer``), so a bound written on
#: one side alone diverges. ``2.0`` is an integer to JSON Schema and to the
#: parser (``query.integral``), so a field parsed past that validator diverges.
WRONG_VALUES: tuple[Any, ...] = (None, 42, True, "", "not-a-declared-value", [], {}, -1, 3.5, 2.0)

#: The protocol's closed string vocabularies. A mutant also swaps a value for
#: another declared member of its own vocabulary, the one family that reaches a
#: conditional rule (an ``unknown-field`` carrying a refusal chain, a ``staged``
#: response carrying a commit handle), which no wrong-typed value can.
VOCABULARIES: tuple[tuple[str, ...], ...] = (
    tuple(ERROR_CODES),
    tuple(sorted(FAILURE_CLASSES)),
    READ_FORMS,
    tuple(sorted(_CHANGE_SET_STATUSES)),
    CHANGE_OPERATION_NAMES,
    tuple(sorted(_ARXML_VERSIONS)),
    ("discover", "read", "change_set", "error"),
)

#: Response shapes no vector ships, one seed per shape the schema declares. A
#: negative vector states its expected fields and the engine produces the
#: document at replay (``test_editing_vectors.py``), so the error side is here.
_ADDRESS = {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "ports"}]}
SYNTHETIC_RESPONSES: dict[str, dict[str, Any]] = {
    "error-minimal": {
        "response": "error",
        "error": "unknown-field",
        "failure_class": "invalid",
        "message": "the message is not contract",
    },
    "error-addressed": {
        "response": "error",
        "error": "broken-reference",
        "failure_class": "invalid",
        "message": "the message is not contract",
        "addresses": [_ADDRESS],
    },
    "error-over-budget": {
        "response": "error",
        "error": "over-budget",
        "failure_class": "invalid",
        "message": "the message is not contract",
        "addresses": [{"layer": "swc"}],
        "hints": [{"operation": "read", "address": {"layer": "swc"}, "form": "summary"}],
        "measured": {"wire_bytes": 100000, "budget": 80000, "elements": 538},
    },
    # The chain's last step, the first page of the address: the only hint
    # carrying `limit` and `after`, which no shipped vector puts inside `hints`.
    "error-over-budget-paged-hint": {
        "response": "error",
        "error": "over-budget",
        "failure_class": "invalid",
        "message": "the message is not contract",
        "addresses": [{"layer": "connectors"}],
        "hints": [
            {"operation": "read", "address": {"layer": "connectors"}, "form": "summary"},
            {"operation": "read", "address": {"layer": "connectors"}, "form": "index"},
            {
                "operation": "read",
                "address": {"layer": "connectors"},
                "form": "full",
                "limit": 50,
                "after": "S0_p00__S1_r00",
            },
        ],
        "measured": {"wire_bytes": 413810, "budget": 80000, "elements": 960},
    },
    # A refused `discover`: addressless, its hints `discoverRequest`s in the
    # shape `discovery.narrower_discovers` emits. No shipped vector is one.
    "error-over-budget-discover-hints": {
        "response": "error",
        "error": "over-budget",
        "failure_class": "invalid",
        "message": "the message is not contract",
        "hints": [
            {"operation": "discover", "form": "summary"},
            {"operation": "discover", "layer": "swc", "form": "full"},
        ],
        "measured": {"wire_bytes": 100000, "budget": 80000, "elements": 10},
    },
    "error-additional": {
        "response": "error",
        "error": "unknown-field",
        "failure_class": "invalid",
        "message": "the message is not contract",
        "additional": [
            {"error": "immutable-field", "failure_class": "invalid", "message": "the message is not contract"}
        ],
    },
    # The chain keys inside `additional`, the only place `$defs/errorDetail`'s
    # own conditional is reached. No pass produces this pairing; the schema
    # declares it, and the corpus is about what the two sides admit.
    "error-additional-carrying-the-chain": {
        "response": "error",
        "error": "over-budget",
        "failure_class": "invalid",
        "message": "the message is not contract",
        "measured": {"wire_bytes": 100000, "budget": 80000, "elements": 538},
        "additional": [
            {
                "error": "over-budget",
                "failure_class": "invalid",
                "message": "a second refusal, so `additional` does not repeat the primary",
                "hints": [{"operation": "read", "address": {"layer": "swc"}, "form": "index"}],
                "measured": {"wire_bytes": 90000, "budget": 80000, "elements": 400},
            }
        ],
    },
    "read": {"response": "read", "address": _ADDRESS, "form": "full", "value": [{"name": "pSpeed"}]},
    # The identity-row echo: no vector ships one.
    "read-rows": {
        "response": "read",
        "address": {"layer": "swc"},
        "form": "index",
        "members": "runnables.writes",
        "derived": ["base_type"],
        "join": [
            {
                "with": {"address": {"layer": "swc"}, "members": "runnables.reads", "derived": ["base_type"]},
                "on": [{"left": "member.element", "right": "member.element"}],
                "as": "reader",
            }
        ],
        "value": {"count": 0, "identities": []},
    },
    # The count of links left out and the read that pages them, carried together
    # beside a full list of sixteen, with `places` at "none": the branch the shipped
    # `find-for-requirement-existing-links-bounded` vector does not take.
    "find-for-requirement-links-left-out": {
        "response": "find",
        "baseline": "rev1:9f4c2a7be3d18056",
        "for_requirement": "REQ_EXAMPLE_002",
        "neighbours": 0,
        "places": "none",
        "existing_links": [
            {
                "address": {"layer": "interfaces", "element": f"ILink{index:02d}"},
                "target": {"kind": "interface", "owner": None, "name": f"ILink{index:02d}"},
                "member": "REQ_EXAMPLE_002@1",
                "version": 1,
                "confirmed": True,
            }
            for index in range(1, protocol.FIND_EXISTING_LINKS_MAX + 1)
        ],
        "existing_links_more": 4,
        "existing_links_read": {
            "operation": "read",
            "address": {"layer": "requirements"},
            "form": "index",
            "where": [{"field": "id", "equals": "REQ_EXAMPLE_002"}],
            "limit": 50,
            "derived": ["realised_by"],
        },
    },
    "change-set-committed": {
        "response": "change_set",
        "status": "committed",
        "baseline": "rev1:9f4c2a7be3d18056",
        "audit_ref": "8f1c",
        "delta": [{"operation": "create", "address": _ADDRESS, "after": {"name": "pSpeed"}, "summary": "+ pSpeed"}],
    },
}


#: Request shapes no vector sends (the request corpus is the vectors' ``input``
#: blocks). Every shipped ``where`` clause uses ``equals``, so ``exists`` is
#: seeded here; ``hw_id`` is the ``hardware`` layer's optional identifier.
SYNTHETIC_REQUESTS: dict[str, dict[str, Any]] = {
    "read-layer-where-exists": {
        "operation": "read",
        "address": {"layer": "hardware"},
        "where": [{"field": "hw_id", "exists": False}],
    },
    "read-slice-where-exists-and-equals": {
        "operation": "read",
        "address": {"layer": "swc"},
        "members": "ports",
        "where": [{"field": "description", "exists": True}, {"field": "direction", "equals": "provide"}],
        "limit": 5,
        "after": {"element": "ExampleSWC", "member": "pOut"},
    },
    # A members path, `derived`, and a two-entry join whose second `left`
    # reaches the first `as`: keys no vector sends.
    "read-rows-path-derived-join": {
        "operation": "read",
        "address": {"layer": "swc"},
        "form": "index",
        "members": "runnables.writes",
        "derived": ["base_type"],
        "join": [
            {
                "with": {"address": {"layer": "connectors"}, "derived": ["connection_ends"]},
                "on": [
                    {"left": "element", "right": "derived.connection_ends.provider.swc"},
                    {"left": "member.port", "right": "derived.connection_ends.provider.port"},
                ],
                "as": "connection",
            },
            {
                "with": {
                    "address": {"layer": "swc"},
                    "members": "runnables.reads",
                    "where": [{"field": "port", "exists": True}],
                },
                "on": [
                    {"left": "connection.derived.connection_ends.requester.swc", "right": "element"},
                    {"left": "member.element", "right": "member.element"},
                ],
                "as": "reader",
            },
        ],
        "limit": 10,
        "after": {"element": "ExampleSWC"},
    },
    # No shipped vector counts with `derived`.
    "count-rows-derived": {
        "operation": "read",
        "address": {"layer": "tasks"},
        "form": "count",
        "derived": ["effective_level"],
    },
}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _validator(ref: str) -> Draft202012Validator:
    schema = _load(PROTOCOL_PATH)
    registry = Registry().with_resources([(schema["$id"], Resource.from_contents(schema))])
    return Draft202012Validator({"$ref": f"{schema['$id']}#/$defs/{ref}"}, registry=registry)


def _ecma_pattern(validator: Any, pattern: str, instance: Any, schema: dict[str, Any]) -> Iterator[ValidationError]:
    """``pattern`` with ECMA-262's ``$``: the end of the input only (no ``m`` flag), Python's ``\\Z``."""
    if validator.is_type(instance, "string") and re.search(re.sub(r"(?<!\\)\$", r"\\Z", pattern), instance) is None:
        yield ValidationError(f"{instance!r} does not match {pattern!r} read as ECMA-262")


def _ecma_validator(ref: str) -> Draft202012Validator:
    schema = _load(PROTOCOL_PATH)
    registry = Registry().with_resources([(schema["$id"], Resource.from_contents(schema))])
    ecma = validators.extend(Draft202012Validator, {"pattern": _ecma_pattern})
    return ecma({"$ref": f"{schema['$id']}#/$defs/{ref}"}, registry=registry)


REQUEST_VALIDATOR = _validator("request")
RESPONSE_VALIDATOR = _validator("response")
#: ``$defs/discoverResponse`` on its own, so its errors carry their own paths
#: rather than one wrapped under ``$defs/response``'s ``oneOf``.
DISCOVER_RESPONSE_VALIDATOR = _validator("discoverResponse")


def _request_seeds() -> list[tuple[str, dict[str, Any]]]:
    seeds = []
    for path in VECTOR_PATHS:
        vector = _load(path)
        if isinstance(vector.get("input"), dict):
            seeds.append((vector["name"], vector["input"]))
    return [*seeds, *SYNTHETIC_REQUESTS.items()]


def _trimmed(response: dict[str, Any]) -> dict[str, Any]:
    """A discover response's inventory cut to its first layer.

    Cost only: ``layers`` is never mutated, but every mutant is re-validated
    whole and the shipped inventory is thousands of descriptors deep.
    """
    if response.get("response") != "discover" or len(response.get("layers", [])) <= 1:
        return response
    return response | {"layers": response["layers"][:1]}


def _response_seeds() -> list[tuple[str, dict[str, Any]]]:
    seeds = []
    for path in VECTOR_PATHS:
        vector = _load(path)
        expected = vector.get("expected")
        if isinstance(expected, dict) and isinstance(expected.get("response"), dict):
            seeds.append((vector["name"], _trimmed(expected["response"])))
    return [*seeds, *SYNTHETIC_RESPONSES.items()]


REQUEST_SEEDS = _request_seeds()
RESPONSE_SEEDS = _response_seeds()


def _exempt(seed: Any, path: tuple[Any, ...]) -> bool:
    """Whether *path* is inside the one subtree the parser deliberately does not check."""
    if not path or not isinstance(seed, dict):
        return False
    return EXEMPT_SUBTREE.get(seed.get("response")) == path[0]


def _nodes(document: Any, prefix: tuple[Any, ...] = (), depth: int = 0) -> Iterator[tuple[tuple[Any, ...], Any]]:
    """Every object and array of *document*, no deeper than four steps."""
    if depth > 4:
        return
    if isinstance(document, dict):
        yield prefix, document
        for key, value in document.items():
            yield from _nodes(value, (*prefix, key), depth + 1)
    elif isinstance(document, list):
        yield prefix, document
        for index, value in enumerate(document):
            yield from _nodes(value, (*prefix, index), depth + 1)


def _siblings(value: Any) -> list[str]:
    """Every OTHER declared member of *value*'s own vocabulary."""
    if not isinstance(value, str):
        return []
    return [member for vocabulary in VOCABULARIES if value in vocabulary for member in vocabulary if member != value]


def _at(document: Any, path: tuple[Any, ...]) -> Any:
    for step in path:
        document = document[step]
    return document


def _with(document: Any, path: tuple[Any, ...], value: Any) -> Any:
    clone = copy.deepcopy(document)
    _at(clone, path[:-1])[path[-1]] = value
    return clone


def _without(document: Any, path: tuple[Any, ...]) -> Any:
    clone = copy.deepcopy(document)
    del _at(clone, path[:-1])[path[-1]]
    return clone


def mutants(seed: Any, *, exempt: bool = True) -> Iterator[tuple[str, Any]]:
    """One edit to one node, over and over."""
    for path, node in _nodes(seed):
        if exempt and _exempt(seed, path):
            continue
        if isinstance(node, dict):
            if path:
                yield f"{path} + an unexpected key", _with(seed, path, dict(node) | {"unexpected": 1})
            else:
                yield "an unexpected key", dict(node) | {"unexpected": 1}
            for key in list(node):
                child = (*path, key)
                yield f"{child} dropped", _without(seed, child)
                for wrong in WRONG_VALUES:
                    yield f"{child} = {wrong!r}", _with(seed, child, wrong)
                for sibling in _siblings(node[key]):
                    yield (
                        f"{child} = {sibling!r} (a declared member of its own vocabulary)",
                        _with(seed, child, sibling),
                    )
        elif isinstance(node, list) and path:
            yield f"{path} emptied", _with(seed, path, [])
            if node:
                yield f"{path[0]} first member duplicated", _with(seed, path, [node[0], *node])


#: The fields ``errorResult`` hoists from its primary violation; comparing
#: exactly these says "``additional`` repeats the primary".
_HOISTED = ("error", "failure_class", "message", "addresses", "hints", "measured")


def _repeats_the_primary(document: Any) -> bool:
    """Whether this is an ``errorResult`` whose ``additional`` repeats the primary.

    JSON Schema 2020-12 cannot compare one property's value with another's, so
    only :meth:`ErrorResult.__post_init__` can refuse it.
    """
    if not isinstance(document, dict) or document.get("response") != "error":
        return False
    additional = document.get("additional")
    if not isinstance(additional, list):
        return False
    primary = {key: document[key] for key in _HOISTED if key in document}
    return any(entry == primary for entry in additional)


def _repeats_an_alias(document: Any) -> bool:
    """Whether this is a read request or response whose ``join`` repeats an ``as``.

    Draft 2020-12 cannot compare one array item with another, so the parser
    (through ``check_rows``) is the stricter side.
    """
    if not isinstance(document, dict):
        return False
    join = document.get("join")
    if not isinstance(join, list):
        return False
    aliases = [entry.get("as") for entry in join if isinstance(entry, dict) and isinstance(entry.get("as"), str)]
    return len(set(aliases)) != len(aliases)


def _refused_only_inside_the_exemption(document: Any) -> bool:
    """Whether the schema refuses this discover response for something inside ``layers`` alone.

    The ``form`` overlays make the descriptor booleans depend on ``form``, so a
    mutant edited outside ``layers`` can be refused inside it. Any error outside
    ``layers`` keeps the mutant in the sweep.
    """
    if not isinstance(document, dict) or document.get("response") != "discover":
        return False
    errors = list(DISCOVER_RESPONSE_VALIDATOR.iter_errors(document))
    return bool(errors) and all(tuple(error.absolute_path)[:1] == (EXEMPT_SUBTREE["discover"],) for error in errors)


def _schema_takes(validator: Draft202012Validator, document: Any) -> bool:
    return validator.is_valid(document)


def _parser_takes_request(document: Any) -> bool:
    try:
        pass_a(document)
    except ProtocolError:
        return False
    return True


def _parser_takes_response(document: Any) -> bool:
    try:
        parse_response(document)
    except ValueError:
        return False
    return True


def _disagreements(seeds, validator, parser, *, exempt: bool = True) -> list[str]:
    findings = []
    for name, seed in seeds:
        for mutation, document in mutants(seed, exempt=exempt):
            if exempt and (_repeats_the_primary(document) or _repeats_an_alias(document)):
                continue
            schema_takes = _schema_takes(validator, document)
            # The second validation runs only on a refusal — an accepted mutant cannot be refused inside `layers`.
            if exempt and not schema_takes and _refused_only_inside_the_exemption(document):
                continue
            if schema_takes is not parser(document):
                findings.append(
                    f"{name}: {mutation} — schema {'accepts' if schema_takes else 'rejects'}, "
                    f"parser {'rejects' if schema_takes else 'accepts'}"
                )
    return findings


class TestTheCorpusIsReal:
    """The sweep is worth nothing if it runs over nothing."""

    def test_every_shipped_vector_input_is_a_seed(self) -> None:
        assert len(REQUEST_SEEDS) == len(VECTOR_PATHS) + len(SYNTHETIC_REQUESTS), (
            "every vector ships an `input`; each one is a request seed, and the synthetic shapes join them"
        )

    def test_the_request_corpus_carries_an_exists_clause(self) -> None:
        """An ``exists`` clause, which no shipped vector sends, is in the request corpus."""
        clauses = [
            clause for _name, seed in REQUEST_SEEDS for clause in seed.get("where", ()) if isinstance(clause, dict)
        ]
        assert [clause for clause in clauses if "exists" in clause], "no request seed carries an `exists` clause"
        assert [clause for clause in clauses if "equals" in clause], "no request seed carries an `equals` clause"

    def test_the_response_corpus_covers_every_shape(self) -> None:
        assert {seed["response"] for _name, seed in RESPONSE_SEEDS} == {
            "discover",
            "read",
            "reads",
            "map",
            "find",
            "count",
            "change_set",
            "error",
        }

    def test_every_seed_is_accepted_by_both_sides(self) -> None:
        # The floor under every parity assertion below: an unmutated seed that
        # one side already refuses would make every mutant of it meaningless.
        for name, seed in REQUEST_SEEDS:
            assert REQUEST_VALIDATOR.is_valid(seed) is _parser_takes_request(seed), name
        for name, seed in RESPONSE_SEEDS:
            assert RESPONSE_VALIDATOR.is_valid(seed) is _parser_takes_response(seed), name


class TestSchemaAndParserAgree:
    """The sweep itself, one direction each way, over every mutant."""

    @pytest.mark.parametrize(
        "rows",
        [
            {"derived": ["effective_level"]},
            {
                "join": [
                    {
                        "with": {"address": {"layer": "swc"}},
                        "on": [{"left": "element", "right": "element"}],
                        "as": "other",
                    }
                ]
            },
        ],
        ids=["derived", "join"],
    )
    def test_a_count_of_rows_at_an_element_address_is_refused_by_both(self, rows: dict[str, Any]) -> None:
        """``$defs/countRequest`` holds identity rows to a layer address, as pass A does."""
        layer = {"operation": "read", "address": {"layer": "tasks"}, "form": "count", **rows}
        element = {**layer, "address": {"layer": "tasks", "element": "Task_10ms"}}
        assert REQUEST_VALIDATOR.is_valid(layer) and _parser_takes_request(layer)
        assert not REQUEST_VALIDATOR.is_valid(element)
        assert not _parser_takes_request(element)

    def test_over_every_request_mutant(self) -> None:
        findings = _disagreements(REQUEST_SEEDS, REQUEST_VALIDATOR, _parser_takes_request)
        assert not findings, "\n".join(findings[:20])

    # One test per response seed, not one sweep: the whole sweep in one test crossed the fast
    # gate's per-test timeout on Windows, and the split lets xdist spread the seeds.
    @pytest.mark.parametrize(("name", "seed"), RESPONSE_SEEDS, ids=[name for name, _seed in RESPONSE_SEEDS])
    def test_over_every_response_mutant(self, name: str, seed: dict[str, Any]) -> None:
        findings = _disagreements([(name, seed)], RESPONSE_VALIDATOR, _parser_takes_response)
        assert not findings, "\n".join(findings[:20])

    def test_the_exemption_is_load_bearing(self) -> None:
        """``layers`` is still where the two sides differ, so the exemption is no no-op."""
        discover = [(name, seed) for name, seed in RESPONSE_SEEDS if seed["response"] == "discover"]
        assert discover, "the response corpus carries a discover response to exempt part of"
        unexempted = _disagreements(discover, RESPONSE_VALIDATOR, _parser_takes_response, exempt=False)
        assert unexempted, (
            "`layers` is exempt because DiscoverResponse keeps the inventory as the registry produced it. "
            "If the parser now checks $defs/layerDescriptor, delete EXEMPT_SUBTREE rather than this assertion"
        )

    def test_the_form_overlay_exemption_is_load_bearing_and_one_sided(self) -> None:
        """A ``form`` swapped for its sibling is refused inside ``layers`` only, and the parser takes it.

        A ``baseline`` response carries no layer descriptor, so it is left out of the swap.
        """
        discover = [(name, seed) for name, seed in RESPONSE_SEEDS if seed["response"] == "discover"]
        assert {seed["form"] for _name, seed in discover} == {"summary", "full", "baseline"}, (
            "the corpus carries a discover response in each form"
        )
        assert all(seed["layers"] == [] for _name, seed in discover if seed["form"] == "baseline")
        for name, seed in ((name, seed) for name, seed in discover if seed["form"] != "baseline"):
            swapped = copy.deepcopy(seed)
            swapped["form"] = "full" if seed["form"] == "summary" else "summary"
            assert not RESPONSE_VALIDATOR.is_valid(swapped), name
            assert _parser_takes_response(swapped), name
            assert _refused_only_inside_the_exemption(swapped), name

    def test_the_repeated_primary_exemption_is_load_bearing_and_one_sided(self) -> None:
        """A validator accepts an ``errorResult`` whose ``additional`` repeats the primary; the parser refuses it."""
        seed = SYNTHETIC_RESPONSES["error-additional"]
        repeated = copy.deepcopy(seed)
        repeated["additional"] = [{key: seed[key] for key in _HOISTED if key in seed}]
        assert _repeats_the_primary(repeated)
        assert RESPONSE_VALIDATOR.is_valid(repeated), "the schema has no way to state the rule"
        assert not _parser_takes_response(repeated), "and the parser enforces it anyway"


class TestTheRepeatedAliasExemption:
    """The repeated-alias exemption, pinned as a fact rather than skipped."""

    @pytest.mark.parametrize(
        ("seeds", "validator", "parser", "seed_name"),
        [
            (SYNTHETIC_REQUESTS, REQUEST_VALIDATOR, _parser_takes_request, "read-rows-path-derived-join"),
            (SYNTHETIC_RESPONSES, RESPONSE_VALIDATOR, _parser_takes_response, "read-rows"),
        ],
        ids=["request", "response"],
    )
    def test_the_repeated_alias_exemption_is_load_bearing_and_one_sided(
        self, seeds: dict[str, Any], validator: Draft202012Validator, parser: Any, seed_name: str
    ) -> None:
        """A validator accepts a ``join`` whose entries share an ``as``; the parser refuses it."""
        repeated = copy.deepcopy(seeds[seed_name])
        repeated["join"] = [repeated["join"][0], repeated["join"][0]]
        assert _repeats_an_alias(repeated)
        assert validator.is_valid(repeated), "the schema has no way to compare one item's alias with another's"
        assert not parser(repeated), "and the parser refuses the collision anyway"

    def test_the_exemption_is_reached_by_the_sweep(self) -> None:
        """The sweep's own duplication mutant produces the collision, so the filter is not a no-op."""
        seed = SYNTHETIC_REQUESTS["read-rows-path-derived-join"]
        assert any(_repeats_an_alias(document) for _mutation, document in mutants(seed))


class TestTheSweepHasTeeth:
    """A parity gate that cannot fail is a green light, not an oracle."""

    @pytest.mark.parametrize(
        ("definition", "branch"),
        [
            ("errorDetail", "else"),
            ("errorResult", "else"),
            ("errorDetail", "allOf"),
            ("errorResult", "allOf"),
            ("changeSetResponse", "then"),
        ],
    )
    def test_deleting_a_conditional_branch_reopens_a_divergence(self, definition: str, branch: str) -> None:
        """Deleting any of the schema's conditional branches reopens a divergence the sweep reports.

        Five sites, three rules: no ``hints``/``measured`` off ``over-budget``
        (``else``), a ``failure_class`` agreeing with ``error_class_map``
        (``allOf``), no ``audit_ref`` on a ``staged`` response (``then``).
        """
        schema = _load(PROTOCOL_PATH)
        del schema["$defs"][definition][branch]
        registry = Registry().with_resources([(schema["$id"], Resource.from_contents(schema))])
        weakened = Draft202012Validator({"$ref": f"{schema['$id']}#/$defs/response"}, registry=registry)
        # The rejection shapes alone: all three conditionals live on them.
        seeds = [(name, seed) for name, seed in RESPONSE_SEEDS if seed["response"] in {"error", "change_set"}]
        findings = _disagreements(seeds, weakened, _parser_takes_response)
        assert findings, f"deleting $defs/{definition}'s `{branch}` must reopen a divergence the sweep reports"

    def test_a_parser_that_stopped_refusing_is_caught(self) -> None:
        # The other direction: a schema constraint (`forms: minItems 1`) the
        # parser stops enforcing.
        seed = next(seed for _name, seed in RESPONSE_SEEDS if seed["response"] == "discover")
        loosened = copy.deepcopy(seed)
        loosened["reads"]["forms"] = []
        assert not RESPONSE_VALIDATOR.is_valid(loosened)
        assert not _parser_takes_response(loosened), "an empty `forms` is refused on both sides"


# The kernel parser, `swcraft query --request`, held to the protocol's parser.


def _outside_the_sub_language(body: Any) -> bool:
    """Whether *body* is a request ``query.parse_request`` refuses by design and is not compared on.

    The CLI's sub-language is a layer address and a ``form`` absent, ``index`` or
    ``full``; an ``element``/``path`` address and ``summary`` are the protocol's alone.
    """
    if not isinstance(body, dict):
        return False
    address = body.get("address")
    if isinstance(address, dict) and ("element" in address or "path" in address):
        return True
    return body.get("form") == "summary"


def _protocol_answer(body: Any) -> dict[str, Any] | None:
    """The protocol's normalized request for *body* sent as a read, minus ``operation``, or ``None`` on a refusal."""
    try:
        parsed = ReadRequest.from_json({"operation": "read", **body} if isinstance(body, dict) else body)
    except EnvelopeError:
        return None
    normalized = parsed.to_json()
    del normalized["operation"]
    return normalized


def _kernel_answer(body: Any) -> dict[str, Any] | None:
    try:
        return parse_request(body).as_json()
    except QueryError:
        return None


def _read_bodies() -> list[tuple[str, dict[str, Any]]]:
    """Every read request seed, ``operation`` removed — the CLI's file carries none.

    ``PARITY_CORPUS`` joins the sweep's seeds: no mutant is an empty ``members``
    step or a reserved alias, so only its hand-built cases reach those rules.
    """
    seeds = [*REQUEST_SEEDS, *PARITY_CORPUS]
    return [
        (name, {key: value for key, value in seed.items() if key != "operation"})
        for name, seed in seeds
        if isinstance(seed, dict) and seed.get("operation") == "read"
    ]


def _kernel_disagreements(bodies: list[tuple[str, dict[str, Any]]]) -> tuple[list[str], int, set[bool]]:
    findings: list[str] = []
    compared = 0
    verdicts: set[bool] = set()
    for name, body in bodies:
        for mutation, document in [("unmutated", body), *mutants(body)]:
            if _outside_the_sub_language(document):
                continue
            compared += 1
            protocol = _protocol_answer(document)
            kernel = _kernel_answer(document)
            verdicts.add(protocol is not None)
            if protocol != kernel:
                findings.append(f"{name}: {mutation} — protocol {protocol!r}, kernel {kernel!r}")
    return findings, compared, verdicts


class TestTheKernelParserAgrees:
    """``query.parse_request`` and ``ReadRequest.from_json`` give one verdict and one request.

    Over every read seed and its mutants: both accept with **equal** normalized
    requests, or both refuse. What it does not reach is :func:`mutants`' own limit
    (one edit, four steps deep) and anything outside the sub-language, which the
    kernel refuses by design.
    """

    def test_over_every_read_seed_and_mutant(self) -> None:
        findings, compared, verdicts = _kernel_disagreements(_read_bodies())
        assert not findings, "\n".join(findings[:20])
        # A floor just under the measured comparison count, so losing a tenth of
        # the corpus fails here; it moves with the seeds and the mutation families.
        assert compared >= 5000, f"the sweep compares the corpus, not a remnant of it ({compared} compared)"
        assert verdicts == {True, False}, "the compared set holds accepted and refused requests alike"

    def test_the_corpus_reaches_every_new_key(self) -> None:
        keys = {key for _name, body in _read_bodies() if not _outside_the_sub_language(body) for key in body}
        assert {"members", "derived", "join", "where", "limit", "after", "form"} <= keys

    @pytest.mark.parametrize(
        ("body", "match"),
        [
            ({"operation": "read", "address": {"layer": "swc"}}, "carries no \\['operation'\\]"),
            ({"address": {"layer": "swc", "element": "ExampleSWC"}}, "names a layer and nothing in it"),
            ({"address": {"layer": "swc"}, "form": "summary"}, "`full`"),
            ({"address": {"layer": "swc"}, "derived": ["x"]}, 'states `form: "index"`'),
            ([{"address": {"layer": "swc"}}], "is a JSON object"),
        ],
        ids=["operation", "element", "summary", "derived-without-form", "array"],
    )
    def test_the_kernel_refuses_outside_its_sub_language(self, body: Any, match: str) -> None:
        with pytest.raises(QueryError, match=match):
            parse_request(body)

    def test_the_kernel_request_carries_the_pilot_request(self) -> None:
        body = {key: value for key, value in SYNTHETIC_REQUESTS["read-rows-path-derived-join"].items()}
        del body["operation"]
        parsed = parse_request(body)
        assert parsed.layer == "swc"
        assert parsed.members == "runnables.writes"
        assert parsed.derived == ("base_type",)
        assert [entry.alias for entry in parsed.join] == ["connection", "reader"]
        assert parsed.join[1].read.where[0].exists is True
        assert parsed.as_json() == body

    def test_a_repeated_alias_is_refused_by_the_kernel_too(self) -> None:
        """Not an exemption on this side: both parsers refuse what the schema cannot state."""
        body = {key: value for key, value in SYNTHETIC_REQUESTS["read-rows-path-derived-join"].items()}
        del body["operation"]
        body["join"] = [body["join"][0], body["join"][0]]
        with pytest.raises(QueryError, match="already an earlier entry's `as`"):
            parse_request(body)

    @pytest.mark.parametrize(
        ("body", "refusal"),
        [
            ({"address": {"layer": "swc\n"}}, "names a layer"),
            (
                {
                    "address": {"layer": "swc"},
                    "form": "index",
                    "join": [
                        {"with": {"address": {"layer": "connectors"}}, "on": [{"left": "a", "right": "b"}], "as": "x\n"}
                    ],
                },
                "a join alias matches",
            ),
        ],
    )
    def test_a_trailing_newline_is_refused_by_both_parsers(self, body: dict[str, Any], refusal: str) -> None:
        """`$` in Python matches before a final newline and ECMA-262's does not: both parsers match the whole string."""
        with pytest.raises(QueryError, match=refusal):
            parse_request(body)
        with pytest.raises(EnvelopeError):
            ReadRequest.from_json({"operation": "read", **body})

    @pytest.mark.parametrize(
        "body",
        [
            {"address": {"layer": "swc\n"}},
            {
                "address": {"layer": "swc"},
                "form": "index",
                "join": [
                    {"with": {"address": {"layer": "connectors"}}, "on": [{"left": "a", "right": "b"}], "as": "x\n"}
                ],
            },
        ],
    )
    def test_the_schema_read_as_ecma_262_refuses_a_trailing_newline(self, body: dict[str, Any]) -> None:
        """The published patterns refuse what both parsers refuse; this gate's own validator does not.

        python-jsonschema's ``$`` also matches before a final newline (ECMA-262's
        does not); pinned so a library fix is noticed. What this does not reach:
        any dialect difference but ``$`` (rewritten to ``\\Z``), and the sweep
        runs on the stock validator alone.
        """
        document = {"operation": "read", **body}
        assert REQUEST_VALIDATOR.is_valid(document), "python-jsonschema's `$` now refuses a trailing newline"
        assert not _ecma_validator("request").is_valid(document)

    def test_the_kernel_restates_the_protocols_patterns(self) -> None:
        """Three patterns and one reserved set, stated by both the kernel and the protocol, are equal."""
        assert query.MEMBERS_PATH_PATTERN.pattern == protocol.MEMBERS_PATH_PATTERN.pattern
        assert query.JOIN_ALIAS_PATTERN.pattern == protocol.JOIN_ALIAS_PATTERN.pattern
        assert query.LAYER_NAME_PATTERN.pattern == LAYER_NAME_PATTERN.pattern
        assert query.RESERVED_JOIN_ALIASES == protocol.RESERVED_JOIN_ALIASES
        schema = _load(PROTOCOL_PATH)["$defs"]
        assert schema["memberPath"]["pattern"] == protocol.MEMBERS_PATH_PATTERN.pattern
        assert schema["joinAlias"]["pattern"] == protocol.JOIN_ALIAS_PATTERN.pattern
        assert set(schema["joinAlias"]["not"]["enum"]) == protocol.RESERVED_JOIN_ALIASES

    def test_the_schemas_join_max_items_is_the_kernels_entry_cap(self) -> None:
        """``$defs/join``'s ``maxItems`` is ``query.JOIN_MAX_ENTRIES``: stated once in the kernel, here as a literal."""
        schema = _load(PROTOCOL_PATH)["$defs"]
        assert schema["join"]["maxItems"] == query.JOIN_MAX_ENTRIES == 8
        assert schema["readsCapability"]["properties"]["join"]["required"] == ["row_ceiling", "max_entries"]

    def test_the_schemas_map_bound_is_the_protocols_list_max(self) -> None:
        """``$defs/modelMapResponse`` lists at most ``MODEL_MAP_LIST_MAX`` names a layer, requires ``counts``."""
        schema = _load(PROTOCOL_PATH)["$defs"]["modelMapResponse"]
        assert schema["properties"]["layers"]["additionalProperties"]["maxItems"] == protocol.MODEL_MAP_LIST_MAX == 50
        assert schema["required"] == ["response", "baseline", "counts", "layers"]

    def test_the_schemas_batch_bound_is_the_protocols_read_batch_max(self) -> None:
        """``$defs/readManyRequest``'s ``maxItems`` is ``READ_BATCH_MAX``, the bound the parser refuses above."""
        schema = _load(PROTOCOL_PATH)["$defs"]["readManyRequest"]
        assert schema["properties"]["addresses"]["maxItems"] == protocol.READ_BATCH_MAX == 16
        assert schema["properties"]["addresses"]["minItems"] == 1

    def test_the_schemas_existing_links_bound_is_the_protocols(self) -> None:
        """``existing_links``' ``maxItems``, and its ``minItems`` beside a count, are ``FIND_EXISTING_LINKS_MAX``."""
        schema = _load(PROTOCOL_PATH)["$defs"]["findCandidatesResponse"]
        assert schema["properties"]["existing_links"]["maxItems"] == protocol.FIND_EXISTING_LINKS_MAX == 16
        listed = schema["dependentSchemas"]["existing_links_more"]["properties"]["existing_links"]
        assert listed == {"minItems": protocol.FIND_EXISTING_LINKS_MAX}

    @pytest.mark.parametrize(
        ("listed", "left_out", "taken"),
        [(16, False, True), (17, False, False), (16, True, True), (17, True, False), (15, True, False)],
        ids=["sixteen", "seventeen", "sixteen-and-a-count", "seventeen-and-a-count", "fifteen-and-a-count"],
    )
    def test_the_schema_and_the_parser_take_a_count_only_beside_sixteen_links(
        self, listed: int, left_out: bool, taken: bool
    ) -> None:
        """At most sixteen links, and ``existing_links_more`` only beside exactly sixteen -- on both sides."""
        document = copy.deepcopy(SYNTHETIC_RESPONSES["find-for-requirement-links-left-out"])
        document["existing_links"] = [document["existing_links"][0]] * listed
        if not left_out:
            del document["existing_links_more"], document["existing_links_read"]
        assert RESPONSE_VALIDATOR.is_valid(document) is taken
        assert _parser_takes_response(document) is taken
        if not taken:
            with pytest.raises(EnvelopeError):
                protocol.FindCandidatesResponse.from_json(document)

    def test_the_schema_and_both_parsers_take_max_entries_and_refuse_one_more(self) -> None:
        """Eight self-joins are a request on all three; a ninth is refused by all three."""

        def body(entries: int) -> dict[str, Any]:
            return {
                "address": {"layer": "swc"},
                "form": "index",
                "join": [
                    {
                        "with": {"address": {"layer": "swc"}},
                        "on": [{"left": "element", "right": "element"}],
                        "as": f"s{n}",
                    }
                    for n in range(1, entries + 1)
                ],
            }

        admitted, refused = body(query.JOIN_MAX_ENTRIES), body(query.JOIN_MAX_ENTRIES + 1)
        assert REQUEST_VALIDATOR.is_valid({"operation": "read", **admitted})
        assert not REQUEST_VALIDATOR.is_valid({"operation": "read", **refused})
        assert len(protocol.ReadRequest.from_json({"operation": "read", **admitted}).join) == 8
        assert len(query.parse_request(admitted).join) == 8
        sentence = query.join_entries_refusal(9)
        with pytest.raises(protocol.EnvelopeError) as protocol_refusal:
            protocol.ReadRequest.from_json({"operation": "read", **refused})
        with pytest.raises(query.QueryError) as kernel_refusal:
            query.parse_request(refused)
        assert str(protocol_refusal.value) == str(kernel_refusal.value) == sentence
